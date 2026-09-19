const $ = (s) => document.querySelector(s),
  esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
let data = { bots: [], jobs: [] },
  page = "bots",
  showArchived = false,
  resources = null,
  bindingTimer = null,
  bindingRenderKey = "";
const sessionUsage = new Map();
const money = n => n == null ? "未设上限" : "¥" + (Number(n)/1000000).toLocaleString("zh-CN",{maximumFractionDigits:6});
let usageRefreshing = false;
const labels = {
  draft: "待绑定",
  usage_pending: "查询额度",
  bound: "微信已绑定",
  ready: "运行就绪",
  expired: "登录过期",
  wait: "等待扫码",
  scaned: "已扫码，待确认",
  confirmed: "绑定成功",
  need_verifycode: "请输入微信配对码",
  scaned_but_redirect: "正在切换微信服务",
  verify_code_blocked: "配对码锁定，请重新生成",
  error: "绑定失败",
  queued: "等待执行",
  preparing: "下载 / 上传附件",
  scheduled_creating: "定时会话准备中",
  session_creating: "正在新建会话",
  checking: "等待文件审核",
  submitting: "正在提交",
  running: "执行中",
  interrupt_sending: "正在发送中断",
  interrupting: "等待中断完成",
  interrupted: "已被新消息打断",
  interrupt_uncertain: "中断结果待核对",
  requires_action: "等待审批",
  reply_pending: "等待回传",
  done: "已完成",
  failed: "执行失败",
  uncertain: "需人工核对",
  reply_uncertain: "回传需核对",
  cancelled: "已跳过",
};
async function api(path, body, method) {
  const r = await fetch("/api" + path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: { "Content-Type": "application/json", "X-Claw-Request": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const d = await r.json();
  if (!r.ok)
    throw new Error(
      typeof d.detail === "string" ? d.detail : "请求失败，请检查输入",
    );
  return d;
}
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").style.display = "block";
  setTimeout(() => ($("#toast").style.display = "none"), 4500);
}
function badge(status) {
  return `<span class="badge ${["expired", "failed", "error", "uncertain", "reply_uncertain"].includes(status) ? "error" : ["queued", "draft", "requires_action"].includes(status) ? "warn" : ""}">${esc(labels[status] || status)}</span>`;
}
function modal(title, content) {
  $("#modal").classList.remove("history-dialog");
  $("#modal").innerHTML =
    `<div class="dialog-head"><h2>${esc(title)}</h2><button class="close" id="close-modal" aria-label="关闭">×</button></div>${content}`;
  $("#close-modal").onclick = () => $("#modal").close();
  if (!$("#modal").open) $("#modal").showModal();
}
$("#modal").addEventListener("close", () => {
  clearInterval(bindingTimer);
  bindingTimer = null;
});
function login() {
  $("#app").innerHTML =
    `<main class="center"><form class="card" id="login"><div class="brand"><span class="logo">↗</span> ClawBridge</div><div class="eyebrow">LOCAL AGENT WORKSPACE</div><h1>让微信连接执行力</h1><p class="muted">登录管理后台，管理微信 Bot 与百炼 Managed Agent 会话。</p><label for="password">管理密码</label><input id="password" type="password" autocomplete="current-password" required placeholder="输入本地管理密码"><p class="muted">使用部署时 .env 中配置的 ADMIN_PASSWORD 登录</p><p class="error-text" id="login-error"></p><button class="full">进入工作台 →</button></form></main>`;
  $("#login").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/login", { password: $("#password").value });
      await refresh();
    } catch (e) {
      $("#login-error").textContent = e.message;
    }
  };
}
function shell() {
  const done = data.jobs.filter((j) => j.status === "done").length,
    waiting = data.jobs.filter((j) =>
      [
        "running",
        "queued",
        "preparing",
        "checking",
        "requires_action",
      ].includes(j.status),
    ).length;
  $("#app").innerHTML =
    `<div class="layout"><aside class="sidebar"><div class="brand"><span class="logo">↗</span> ClawBridge</div><div class="side-label">WORKSPACE</div><button class="nav ${page === "bots" ? "active" : ""}" data-page="bots">◈　微信 Bot</button><button class="nav ${page === "jobs" ? "active" : ""}" data-page="jobs">▤　执行记录</button><button class="nav ${page === "schedules" ? "active" : ""}" data-page="schedules">◷　定时任务</button><button class="nav ${page === "resources" ? "active" : ""}" data-page="resources">▦　MA 资源</button><button class="nav ${page === "billing" ? "active" : ""}" data-page="billing">¥　费用与额度</button><div class="side-bottom"><span class="online"></span> 本地服务已连接<br>SQLite · Local Workspace<br><br>微信入口 × 百炼执行</div></aside><main class="main"><div class="topline"><span>工作空间 / ${esc(page === "bots" ? "微信 Bot" : page === "schedules" ? "定时任务" : page === "jobs" ? "执行记录" : page === "billing" ? "费用与额度" : "MA 资源")}</span><span><span class="pill">本地模式</span>　<button class="secondary small" id="logout">退出</button></span></div><div class="heading"><div><div class="eyebrow">CONNECT. DELEGATE. GET THINGS DONE.</div><h1>${page === "bots" ? "微信 Bot 工作台" : page === "schedules" ? "按时执行，主动送达" : page === "jobs" ? "每一次执行，都有记录" : page === "billing" ? "每一笔用量，都清晰可查" : "连接百炼 Managed Agent"}</h1><p class="muted">${page === "bots" ? "从一条微信消息开始，让智能体替你完成任务。" : page === "schedules" ? "查看每个 Bot 的定时任务、独立会话和执行费用。" : page === "jobs" ? "查看请求、执行结果与需要你处理的事项。" : page === "billing" ? "管理预算、Agent 单价与按次结算明细。" : "选择已有的 Agent 和运行环境，为 Bot 创建独立会话。"}</p></div><button id="add">＋ 添加 Bot</button></div><div class="metrics"><div class="metric"><div class="metric-title">微信 Bot</div><strong>${data.bots.length.toString().padStart(2, "0")}</strong><span>已添加至工作空间</span></div><div class="metric"><div class="metric-title">运行就绪</div><strong>${data.bots
      .filter((b) => b.session_id && b.enabled && b.status !== "expired")
      .length.toString()
      .padStart(
        2,
        "0",
      )}</strong><span>已连接微信与 MA</span></div><div class="metric"><div class="metric-title">待处理任务</div><strong>${waiting.toString().padStart(2, "0")}</strong><span>排队、执行或等待审批</span></div><div class="metric"><div class="metric-title">已完成执行</div><strong>${done.toString().padStart(2, "0")}</strong><span>最近 100 条任务</span></div></div><div id="content"></div><div class="footer"><span>ClawBridge / 微信 × Managed Agent</span><span>本地存储 · 文字 / 图片 / 文件</span></div></main></div>`;
  document.querySelectorAll("[data-page]").forEach(
    (b) =>
      (b.onclick = () => {
        page = b.dataset.page;
        render();
      }),
  );
  $("#logout").onclick = async () => {
    await api("/logout", {});
    login();
  };
  $("#add").onclick = addBot;
}
function render() {
  shell();
  if (page === "bots") renderBots();
  else if (page === "jobs") renderJobs();
  else if (page === "billing") renderBilling();
  else if (page === "schedules") renderSchedules();
  else renderResources();
}
function usageCell(bot) {
  if (!bot.session_id) return '<span class="muted">尚未创建会话</span>';
  const snapshot = sessionUsage.get(bot.id);
  if (!snapshot || snapshot.session_id !== bot.session_id)
    return '<span class="muted">点击刷新查询</span>';
  const n = (value) =>
    value == null ? "—" : Number(value).toLocaleString("zh-CN");
  let content = "";
  if (snapshot.usage) {
    const u = snapshot.usage,
      stats = snapshot.stats;
    content = `<div>输入 <b>${n(u.input_tokens)}</b> / 输出 <b>${n(u.output_tokens)}</b> Token</div>
      <small>缓存读取 ${n(u.cache_read_input_tokens)} / 创建 ${n(u.cache_creation_input_tokens)} Token</small>
      <small>活跃 ${n(stats.active_seconds)} 秒 / 会话时长 ${n(stats.duration_seconds)} 秒</small>
      <small>${snapshot.error ? "上次成功更新" : "更新于"} ${esc(new Date(snapshot.fetched_at).toLocaleString("zh-CN"))}</small>`;
  }
  if (snapshot.error)
    content += `<small class="error-text">刷新失败：${esc(snapshot.error)}</small>`;
  return content;
}
async function refreshUsage() {
  if (usageRefreshing) return;
  usageRefreshing = true;
  renderBots();
  try {
    const result = await api("/session-usage");
    for (const item of result.items) {
      const old = sessionUsage.get(item.bot_id);
      sessionUsage.set(
        item.bot_id,
        item.error && old?.session_id === item.session_id
          ? { ...old, error: item.error }
          : item,
      );
    }
    const failed = result.items.filter((item) => item.error).length;
    toast(
      failed
        ? `${failed} 个会话刷新失败，已保留上次成功数据`
        : result.items.length
          ? "Session 用量已刷新"
          : "暂无已绑定的 MA 会话",
    );
  } catch (error) {
    toast(error.message);
  } finally {
    usageRefreshing = false;
    if (page === "bots" && !$("#login")) renderBots();
  }
}
function renderBots() {
  $("#content").innerHTML =
    `<section class="panel"><div class="panel-head"><h2>${showArchived ? "已归档 Bot" : "我的 Bot"} <span class="muted"> / ${data.bots.length}</span></h2><button class="secondary small" id="archive-filter">${showArchived ? "查看未归档" : "查看已归档"}</button><button class="secondary small" id="refresh-usage" ${usageRefreshing ? "disabled" : ""}>${usageRefreshing ? "正在刷新…" : "刷新 Session 用量"}</button></div>${data.bots.length ? `<div class="table-wrap"><table><thead><tr><th>BOT 名称</th><th>连接状态</th><th>会话与记忆</th><th>额度 / Session 用量</th><th>操作</th></tr></thead><tbody>${data.bots.map((b) => `<tr><td><b>${esc(b.name)}</b><small>${esc(b.user_id || "等待微信用户绑定")}</small>${b.error ? `<small class="error-text">${esc(b.error)}</small>` : ""}</td><td>${badge(b.status)}${!b.enabled ? "<small>接入已暂停</small>" : ""}</td><td class="session-cell"><small class="mono" title="${esc(b.session_id)}">${esc(b.session_id || "尚未创建")}</small><div class="memory-line"><span class="memory-dot ${b.memory_store_id && b.memory_store_id === b.session_memory_id ? "connected" : "pending"}"></span>${b.memory_store_id ? (b.session_memory_id === b.memory_store_id ? "专属记忆已挂载" : "专属记忆已创建 · 待挂载") : "专属记忆待初始化"}</div><button class="text-button" data-history="${b.id}">会话历史 · ${b.session_count || 0} →</button>${b.memory_error ? `<small class="error-text">${esc(b.memory_error)}</small><button class="text-button" data-memory="${b.id}">重试会话与记忆初始化</button>` : ""}</td><td class="usage-cell"><div>剩余 <b>${money(b.budget_micro == null ? null : b.budget_micro-b.spent_micro)}</b> · 总额 ${money(b.budget_micro)}</div><small>累计扣费 ${money(b.spent_micro)}</small><button class="text-button" data-budget="${b.id}">设置预算</button>　<button class="text-button" data-charges="${b.id}">扣费记录</button>${b.billing_error ? `<small class="error-text">${esc(b.billing_error)}</small>` : ""}${usageCell(b)}</td><td><div class="actions">${["draft", "expired"].includes(b.status) ? `<button class="small" data-bind="${b.id}">获取二维码</button>` : !b.session_id ? `<button class="small" data-session="${b.id}">配置会话</button>` : `<button class="secondary small" data-session="${b.id}">新建 Session</button>`}<button class="secondary small" data-toggle="${b.id}">${b.enabled ? "暂停" : "恢复"}</button><button class="secondary small" data-access="${b.id}">API 凭证</button><button class="secondary small" data-archive="${b.id}">${b.archived ? "取消归档" : "归档"}</button></div></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty"><div class="empty-icon">◈</div><h3>${showArchived ? "暂无已归档的 Bot" : "你的第一个微信智能体，从这里开始"}</h3><p>添加一个 Bot，将二维码分享给绑定人。绑定成功后自动创建 MA 会话，就能在微信中发送任务并收到结果。</p><button id="first-bot">＋ 添加第一个 Bot</button></div>`}<div class="flow"><b>01　添加 Bot</b><span>→</span><b>02　微信扫码绑定</b><span>→</span><b>03　自动创建会话</b><span>→</span><b>04　微信发送任务</b></div></section><div class="notice">归档仅隐藏 Bot，接入和历史记录会保留；停止接入请使用暂停。微信发送 /usage 查询额度；发送 /clear 开启全新会话，专属长期记忆会保留。分享二维码图片即可让他人扫码；分享绑定页面链接时，对方需要能访问本机地址。</div>`;
  document.querySelectorAll('[data-access]').forEach(button=>{
    button.onclick=async()=>{
      try {
        const access=await api(`/bots/${button.dataset.access}/access`);
        modal('Bot API 凭证',`<p class="muted">仅用于此 Bot，长期有效。新建 Session 时自动注入。勿将 Token 发到聊天或写入长期记忆。</p><label>API 基础地址</label><input readonly value="${esc(access.base_url)}"><label>Bot Token</label><input id="bot-api-token" type="password" readonly value="${esc(access.token)}"><button class="secondary small" id="reveal-bot-token">显示 / 隐藏</button><p class="muted">接口：GET /api/bot/usage · POST /api/bot/sessions/reset</p>`);
        $('#reveal-bot-token').onclick=()=>{const field=$('#bot-api-token');field.type=field.type==='password'?'text':'password';};
      } catch(e){toast(e.message);}
    };
  });
  $("#archive-filter").onclick = async () => {
    showArchived = !showArchived;
    try { await refresh(); } catch (e) { toast(e.message); }
  };
  document.querySelectorAll("[data-archive]").forEach(button => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        const bot = data.bots.find(b => b.id === button.dataset.archive);
        await api(`/bots/${bot.id}/archive`, {archived: !bot.archived});
        await refresh();
        toast(bot.archived ? "Bot 已取消归档" : "Bot 已归档，可在归档列表恢复");
      } catch (e) { button.disabled = false; toast(e.message); }
    };
  });
  $("#refresh-usage").onclick = refreshUsage;
  document.querySelectorAll("[data-budget]").forEach(b => b.onclick=()=>budgetForm(b.dataset.budget));
  document.querySelectorAll("[data-charges]").forEach(b => b.onclick=()=>{page="billing";render();renderBilling(b.dataset.charges);});
  document.querySelectorAll("[data-history]").forEach(b => b.onclick = () => sessionHistory(b.dataset.history));
  document.querySelectorAll("[data-memory]").forEach(b => b.onclick = async () => {
    b.disabled = true;
    try { await api(`/bots/${b.dataset.memory}/memory/retry`, {}); toast("正在初始化记忆，运行中的任务结束后会自动挂载"); }
    catch(e) { toast(e.message); }
    finally { b.disabled = false; }
  });
  if ($("#first-bot")) $("#first-bot").onclick = addBot;
  document
    .querySelectorAll("[data-bind]")
    .forEach((b) => (b.onclick = () => showBinding(b.dataset.bind)));
  document
    .querySelectorAll("[data-session]")
    .forEach((b) => (b.onclick = () => sessionForm(b.dataset.session)));
  document.querySelectorAll("[data-toggle]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          await api(`/bots/${b.dataset.toggle}/toggle`, {});
          await refresh();
        } catch (e) {
          toast(e.message);
        }
      }),
  );
}
async function sessionHistory(id, offset = 0, sync = false) {
  const bot = data.bots.find(b => b.id === id);
  modal(`${bot.name} · 会话历史`, '<div class="loading">正在读取会话记录…</div>');
  $("#modal").classList.add("history-dialog");
  try {
    const history = sync ? await api(`/bots/${id}/sessions/sync`, {}) : await api(`/bots/${id}/sessions?offset=${offset}`);
    if (!$("#modal").open) return;
    const date = value => value ? new Date(value.includes("T") ? value : value.replace(" ","T")+"Z").toLocaleString("zh-CN") : "时间未知";
    modal(`${bot.name} · 会话历史`, `
      <div class="memory-summary"><div class="eyebrow">PERSONAL MEMORY</div><h3>会话可以重开，记忆持续保留</h3><p class="muted">每个 Bot 使用独立的 Memory Store。/clear 清空当前对话上下文，不会删除长期记忆。</p><code>${esc(bot.memory_store_id || "记忆库尚未就绪")}</code></div>
      <div class="history-toolbar"><span class="muted">共 ${history.total} 个会话 · 当前会话置顶</span><button class="secondary small" id="sync-history">同步远端历史</button></div>
      <div class="session-history">${history.items.map(s => `<article class="history-item ${s.session_id === history.current_session_id ? "current" : ""}"><div class="history-item-head"><span class="badge ${s.session_id === history.current_session_id ? "" : "neutral"}">${s.session_id === history.current_session_id ? "当前使用" : "历史会话"}</span><span class="muted">${esc(date(s.created_at))}</span></div><div class="history-id"><code>${esc(s.session_id)}</code><button class="secondary small" data-copy-session="${esc(s.session_id)}">复制 ID</button></div><div class="history-meta"><span>${s.job_count} 条任务</span><span>${s.memory_store_id ? "已挂载专属记忆" : "未记录记忆挂载"}</span></div><details><summary>查看关联资源</summary><p class="muted">工作空间：${esc(s.workspace_id || data.workspace)}<br>Agent：${esc(s.agent_id || "历史配置未知")}<br>环境：${esc(s.environment_id || "历史配置未知")}<br>Memory Store：${esc(s.memory_store_id || "无记录")}</p></details></article>`).join("") || '<div class="empty"><h3>还没有会话</h3><p>绑定微信并创建 MA 会话后，记录会自动保存在这里。</p></div>'}</div>
      <div class="history-toolbar"><span class="muted">记录来自本地及标记了该 Bot 的远端会话。</span><div class="actions"><button class="secondary small" id="history-prev" ${offset===0 ? "disabled" : ""}>上一页</button><button class="secondary small" id="history-next" ${offset+50>=history.total ? "disabled" : ""}>下一页</button></div></div>`);
    $("#modal").classList.add("history-dialog");
    $("#sync-history").onclick = () => sessionHistory(id,0,true);
    $("#history-prev").onclick = () => sessionHistory(id,Math.max(0,offset-50));
    $("#history-next").onclick = () => sessionHistory(id,offset+50);
    document.querySelectorAll("[data-copy-session]").forEach(button => button.onclick = async () => {
      try { await navigator.clipboard.writeText(button.dataset.copySession); toast("Session ID 已复制"); }
      catch { toast("复制失败，可选中会话 ID 手动复制"); }
    });
  } catch(e) { modal("读取历史失败", `<p class="error-text">${esc(e.message)}</p>`); }
}

function renderJobs() {
  $("#content").innerHTML =
    `<section class="panel"><div class="panel-head"><h2>执行记录</h2><span class="muted">最近 100 条 · 自动更新</span></div>${data.jobs.length ? `<div class="table-wrap"><table><thead><tr><th>任务 / BOT</th><th>请求内容</th><th>状态</th><th>创建时间</th><th></th></tr></thead><tbody>${data.jobs.map((j) => `<tr><td>#${j.id}<small>${esc(j.bot_name)}</small></td><td class="text-cell">${esc(j.text)}</td><td>${badge(j.status)}<small>${j.charge_id ? `扣费 ${money(j.charge_micro)}` : billingLabel(j.billing_state)}</small></td><td class="muted">${esc(j.created_at)} UTC</td><td><button class="secondary small" data-job="${j.id}">查看详情</button></td></tr>`).join("")}</tbody></table></div>` : '<div class="empty"><h3>还没有执行记录</h3><p>Bot 绑定并连接 MA 后，发送一条微信消息，任务会出现在这里。</p></div>'}</section>`;
  document
    .querySelectorAll("[data-job]")
    .forEach((b) => (b.onclick = () => jobDetail(Number(b.dataset.job))));
}
function billingLabel(state) {
  return ({pending:"待结算",settled:"已结算",unpriced:"未配置单价",not_submitted:"未提交，不计费",rejected:"欠费拒绝",local_command:"本地指令，不计费"})[state] || "历史记录，不补扣";
}
function budgetForm(id) {
  const bot=data.bots.find(b=>b.id===id);
  modal(`${bot.name} · 设置预算`, `<form id="budget-form"><p class="muted">设置累计总额度，已扣费用不会清零。提高总额度即可补充可用余额。</p><p>已用 ${money(bot.spent_micro)} · 剩余 ${money(bot.budget_micro==null?null:bot.budget_micro-bot.spent_micro)}</p><label for="budget-limit">总额度（元）</label><input id="budget-limit" type="number" min="0" max="1000000" step="0.000001" value="${bot.budget_micro==null?'':bot.budget_micro/1000000}" placeholder="留空表示不设上限"><div class="form-actions"><button>保存预算</button></div></form>`);
  $("#budget-form").onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
    try {await api(`/bots/${id}/budget`,{budget:$("#budget-limit").value||null});$("#modal").close();await refresh();toast("预算已更新");}
    catch(e){toast(e.message);button.disabled=false;}};
}
let billingViewVersion=0;
async function renderBilling(botId="",offset=0) {
  const version=++billingViewVersion;
  $("#content").innerHTML='<div class="loading">正在读取计费配置与扣费记录…</div>';
  try {
    const [r,prices,ledger]=await Promise.all([loadResources(),api('/pricing'),api(`/charges?bot_id=${encodeURIComponent(botId)}&offset=${offset}`)]);
    if(page!=="billing" || version!==billingViewVersion)return;
    const map=new Map(prices.items.map(p=>[p.agent_id,p]));
    $("#content").innerHTML=`<section class="panel"><div class="panel-head"><h2>Agent 计费配置</h2><span class="muted">人民币 · Token 单价 / 百万 · 活跃时间 / 小时</span></div><div class="resource-grid">${r.agents.filter(a=>r.agents.length===1||map.has(a.id)||data.bots.some(b=>b.agent_id===a.id)).map(a=>{const p=map.get(a.id);return `<div class="resource"><h3>${esc(a.name)}</h3><small class="muted">${esc(a.id)}</small><p class="muted">${p?`输入 ${money(p.input_micro)} · 缓存读取 ${money(p.cache_micro)} · 输出 ${money(p.output_micro)}<br>${p.cache_mode==='explicit'?`显式缓存 · 创建 ${money(p.cache_creation_micro)} / 百万 Token`:'隐式缓存'}<br>活跃时间 ${money(p.active_hour_micro)} / 小时 · 搜索 ${money(p.web_search_micro)} / 次`:'尚未设置 Token 单价；默认活跃时间 ¥0.5 / 小时、网页搜索 ¥0.03 / 次'}</p><button class="secondary small" data-price="${esc(a.id)}">${p?'编辑单价':'设置单价'}</button></div>`;}).join('')}</div><div class="panel-head"><select id="price-agent-select" aria-label="选择其他 Agent">${r.agents.map(a=>`<option value="${esc(a.id)}">${esc(a.name)} · ${esc(a.id)}</option>`).join('')}</select><button class="secondary small" id="configure-agent-price">配置所选 Agent</button></div></section>
      <div class="notice">首次启用计费先记录 Session 当前用量，不补扣历史费用。每次任务结束按累计差值结算；费率在提交任务时锁定。活跃时间按秒折算，不按整小时进位。欠费拒绝新任务。未设置单价且未设置预算的旧 Bot 暂不计费。</div>
      <section class="panel billing-ledger"><div class="panel-head"><h2>扣费记录 · ${ledger.total}</h2><div class="actions"><select id="charge-filter" aria-label="按 Bot 筛选"><option value="">全部 Bot</option>${data.bots.map(b=>`<option value="${b.id}" ${b.id===botId?'selected':''}>${esc(b.name)}</option>`).join('')}</select><button class="secondary small" id="refresh-charges">刷新</button></div></div>
      ${ledger.items.length?`<div class="table-wrap"><table><thead><tr><th>Bot / 执行记录</th><th>Token 费用</th><th>活跃时间费用</th><th>网页搜索费用</th><th>合计扣费</th><th>时间</th><th></th></tr></thead><tbody>${ledger.items.map(c=>`<tr><td>${esc(c.bot_name)}<small>关联执行 #${c.job_id}</small></td><td>${money(c.token_micro)}</td><td>${money(c.active_micro)}<small>${(c.usage_delta.active_microseconds/1000000).toLocaleString('zh-CN')} 秒</small></td><td>${money(c.components?.web_search_micro||0)}<small>${c.usage_delta.web_search_calls||0} 次</small></td><td><b>${money(c.amount_micro)}</b></td><td>${esc(c.created_at)} UTC</td><td><button class="secondary small" data-charge="${c.id}">查看明细</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty"><h3>暂无扣费记录</h3><p>设置 Agent 单价后，新任务执行结束会自动生成关联明细。</p></div>'}
      <div class="panel-head"><span class="muted">金额保留至小数点后 6 位</span><div class="actions"><button class="secondary small" id="charge-prev" ${offset===0?'disabled':''}>上一页</button><button class="secondary small" id="charge-next" ${offset+50>=ledger.total?'disabled':''}>下一页</button></div></div></section>`;
    $('#configure-agent-price').onclick=()=>{const id=$('#price-agent-select').value;const a=r.agents.find(a=>a.id===id);if(a)priceForm(a,map.get(id));};
    document.querySelectorAll('[data-price]').forEach(b=>b.onclick=()=>priceForm(r.agents.find(a=>a.id===b.dataset.price),map.get(b.dataset.price)));
    document.querySelectorAll('[data-charge]').forEach(b=>b.onclick=()=>chargeDetail(ledger.items.find(c=>String(c.id)===b.dataset.charge)));
    $('#charge-filter').onchange=e=>renderBilling(e.target.value);
    $('#refresh-charges').onclick=()=>renderBilling($('#charge-filter').value);
    $('#charge-prev').onclick=()=>renderBilling(botId,Math.max(0,offset-50));
    $('#charge-next').onclick=()=>renderBilling(botId,offset+50);
  } catch(e){if(page==='billing')$('#content').innerHTML=`<div class="notice error-text">${esc(e.message)}</div>`;}
}
function priceForm(agent, price) {
  modal(`${agent.name} · 单价设置`,`<form id="price-form"><p class="muted">仅影响之后提交的任务。执行中的任务继续使用原单价。</p>${[['input','输入 Token',price?.input_micro],['cache','缓存读取 Token',price?.cache_micro],['output','输出 Token',price?.output_micro],['active_hour','活跃时间',price?.active_hour_micro??500000],['web_search','网页搜索',price?.web_search_micro??30000]].map(([k,label,value])=>`<label for="price-${k}">${label}（元 / ${k==='active_hour'?'小时':k==='web_search'?'次':'百万 Token'}）</label><input id="price-${k}" type="number" min="0" max="1000000" step="0.000001" value="${value==null?'':value/1000000}" required>`).join('')}<label for="cache-mode">模型的缓存模式</label><select id="cache-mode"><option value="implicit" ${price?.cache_mode!=='explicit'?'selected':''}>隐式缓存</option><option value="explicit" ${price?.cache_mode==='explicit'?'selected':''}>显式缓存</option></select><div id="cache-creation-field"><label for="price-cache-creation">缓存创建（元 / 百万 Token）</label><input id="price-cache-creation" type="number" min="0" max="1000000" step="0.000001" value="${price?.cache_creation_micro==null?'':price.cache_creation_micro/1000000}" placeholder="填写此模型的缓存创建单价"></div><p class="muted">根据 Agent 使用的模型选择缓存模式；显式缓存需独立填写创建单价。隐式普通输入 = 总输入 − 缓存读取；显式普通输入还需减去缓存创建。模式和单价在任务提交时锁定。</p><div class="form-actions"><button>保存单价</button></div></form>`);
  const updateCacheMode=()=>{const explicit=$('#cache-mode').value==='explicit';$('#cache-creation-field').hidden=!explicit;$('#price-cache-creation').required=explicit;$('#price-cache-creation').disabled=!explicit;};
  $('#cache-mode').onchange=updateCacheMode;updateCacheMode();
  $('#price-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
    try {await api(`/pricing/${agent.id}`,{input_price:$('#price-input').value,cache_price:$('#price-cache').value,output_price:$('#price-output').value,active_hour_price:$('#price-active_hour').value,web_search_price:$('#price-web_search').value,cache_mode:$('#cache-mode').value,cache_creation_price:$('#cache-mode').value==='explicit'?$('#price-cache-creation').value:null});$('#modal').close();await renderBilling();toast('单价已保存');}
    catch(e){toast(e.message);button.disabled=false;}};
}
function chargeDetail(c) {
  const p=c.price_snapshot,d=c.usage_delta;
  const rows=[['普通输入',c.normal_input_tokens,p.input_micro,'百万 Token',c.components?.input_micro],['缓存读取',d.cache_read_input_tokens,p.cache_micro,'百万 Token',c.components?.cache_micro],['输出',d.output_tokens,p.output_micro,'百万 Token',c.components?.output_micro],['活跃时间',d.active_microseconds/1000000,p.active_hour_micro,'小时',c.active_micro],['网页搜索',d.web_search_calls||0,p.web_search_micro,'次',c.components?.web_search_micro||0]];
  if(p.cache_mode==='explicit') rows.splice(2,0,['缓存创建',d.cache_creation_input_tokens,p.cache_creation_micro,'百万 Token',c.components?.cache_creation_micro]);
  modal(`扣费 #${c.id} · 关联执行 #${c.job_id}`,`<p class="muted">${esc(c.bot_name)} · ${esc(c.created_at)} UTC</p><div class="memory-summary"><h3>本次扣费 ${money(c.amount_micro)}</h3><p class="muted">Token ${money(c.token_micro)} ＋ 活跃时间 ${money(c.active_micro)} ＋ 网页搜索 ${money(c.components?.web_search_micro||0)}</p></div><div class="table-wrap"><table><thead><tr><th>项目</th><th>本次用量</th><th>锁定单价</th><th>费用</th></tr></thead><tbody>${rows.map(([name,n,price,unit,cost])=>`<tr><td>${name}</td><td>${n.toLocaleString('zh-CN')}${name==='活跃时间'?' 秒':name==='网页搜索'?' 次':' Token'}</td><td>${money(price)} / ${unit}</td><td>${cost==null?"—":money(cost)}</td></tr>`).join('')}</tbody></table></div><p class="muted">${p.cache_mode==='explicit'?'显式缓存：普通输入 = 总输入增量 − 缓存读取增量 − 缓存创建增量。':'隐式缓存：普通输入 = 总输入增量 − 缓存读取增量。'}活跃费用 = 活跃秒数 ÷ 3600 × 小时单价。</p><details><summary>用量快照与关联资源</summary><div class="pre">工作空间：${esc(c.workspace_id || data.workspace)}\nSession：${esc(c.session_id)}\nAgent：${esc(c.agent_id)}\n工具调用：${esc(JSON.stringify(c.tool_calls||[],null,2))}\n上次累计：${esc(JSON.stringify(c.usage_before,null,2))}\n本次累计：${esc(JSON.stringify(c.usage_after,null,2))}</div></details><label>关联执行 #${c.job_id} · ${esc(labels[c.job_status]||c.job_status)}</label><div class="pre">${esc(c.job_text)}</div><label>执行结果</label><div class="pre">${esc(c.job_result||'无文本结果')}</div>`);
  $('#modal').classList.add('history-dialog');
}
async function loadResources() {
  if (!resources) resources = await api("/resources");
  return resources;
}
async function renderResources() {
  $("#content").innerHTML =
    '<div class="panel"><div class="loading">正在读取百炼工作空间…</div></div>';
  try {
    const r = await loadResources();
    if (page !== "resources") return;
    $("#content").innerHTML =
      `<section class="panel"><div class="panel-head"><h2>百炼工作空间</h2><span class="muted">${esc(data.workspace)}</span></div><div class="resource-grid">${[
        ["Agent", r.agents],
        ["运行环境", r.environments],
      ]
        .map(
          ([t, list]) =>
            `<div><h3>${t} · ${list.length}</h3>${list.map((v) => `<div class="resource"><b>${esc(v.name)}</b><div class="muted">${esc(v.id)}</div></div>`).join("") || '<p class="muted">尚无资源，请先在百炼控制台创建。</p>'}</div>`,
        )
        .join(
          "",
        )}</div></section><p class="muted">Agent 与环境沿用工作空间现有配置。API Key 仅在本地后端使用。</p>`;
  } catch (e) {
    $("#content").innerHTML =
      `<div class="notice error-text">${esc(e.message)}</div>`;
  }
}
function addBot() {
  modal(
    "添加微信 Bot",
    `<form id="new-bot"><p class="muted">创建时会自动建立专属长期记忆库，再生成微信绑定二维码。</p><label for="name">Bot 名称</label><input id="name" maxlength="80" required placeholder="例如：团队执行助手"><label for="budget">总预算（元）</label><input id="budget" type="number" min="0" max="1000000" step="0.000001" placeholder="留空表示不设上限"><p class="muted">费用按执行用量结算。余额 ≤ 0 时拒绝新任务，/usage 仍可查询。设预算后请在费用与额度中配置 Agent 单价。</p><div class="form-actions"><button>创建并获取二维码 →</button></div></form>`,
  );
  $("#new-bot").onsubmit = async (e) => {
    e.preventDefault();
    const button = e.target.querySelector("button");
    button.disabled = true;
    try {
      const b = await api("/bots", { name: $("#name").value, budget: $("#budget").value || null });
      await refresh();
      if (b.memory_error) toast("Bot 已保存，记忆库暂未就绪，可在工作台重试");
      await showBinding(b.id);
    } catch (e) {
      toast(e.message);
      button.disabled = false;
    }
  };
}
async function showBinding(id) {
  bindingRenderKey = "";
  modal("微信绑定", `<div class="loading">正在向微信申请二维码…</div>`);
  try {
    const b = await api(`/bots/${id}/binding`, {});
    await bindingView(b.ticket, false);
    clearInterval(bindingTimer);
    bindingTimer = setInterval(() => bindingView(b.ticket, false), 2500);
  } catch (e) {
    modal(
      "获取二维码失败",
      `<p class="error-text">${esc(e.message)}</p><p class="muted">Bot 已保存，可关闭后重试。</p>`,
    );
  }
}
async function bindingView(ticket, publicPage) {
  try {
    const b = await api(`/public/bind/${ticket}`);
    const key = ticket + ":" + b.status + ":" + b.session_ready + ":" + b.setup_failed;
    if (key === bindingRenderKey) return;
    bindingRenderKey = key;
    const target = publicPage ? $("#app") : $("#modal");
    if (!publicPage && !$("#modal").open) return;
    const codeValue = $("#verify-code")?.value || "";
    const terminal = [
      "confirmed",
      "expired",
      "error",
      "verify_code_blocked",
    ].includes(b.status);
    const inside = `<div class="binding"><h2>${esc(b.name)}</h2><p>${badge(b.status)}</p>${!terminal ? `<img class="qr" src="/api/public/bind/${encodeURIComponent(ticket)}/qr" alt="微信绑定二维码"><p class="muted">使用微信扫描二维码，并在手机上确认绑定。</p>` : `<div class="empty-icon">${b.status === "confirmed" ? "✓" : "↻"}</div><p class="muted">${b.status === "confirmed" ? (b.session_ready ? "微信绑定和初始会话已准备完成，可以发送任务了。" : b.setup_failed ? "微信绑定成功，会话准备暂未完成，请管理员在工作台重试。" : "微信绑定成功，正在自动创建初始会话，请稍候…") : "二维码已失效或绑定失败，请联系管理员重新生成。"}</p>`}${b.status === "need_verifycode" ? `<form id="verify-form"><input id="verify-code" inputmode="numeric" pattern="[0-9]{4,12}" required placeholder="填写手机上显示的配对码" value="${esc(codeValue)}"><button class="full">提交配对码</button></form>` : ""}${!publicPage && !terminal ? `<label>分享绑定页链接</label><input readonly value="${esc(b.share_url)}"><p class="muted">也可保存上方二维码图片后发给绑定人。<br>127.0.0.1 链接仅本机可访问。</p>` : ""}</div>`;
    if (publicPage)
      target.innerHTML = `<main class="center"><div class="card"><div class="brand"><span class="logo">↗</span> ClawBridge</div>${inside}</div></main>`;
    else modal("微信绑定", inside);
    if ($("#verify-form"))
      $("#verify-form").onsubmit = async (e) => {
        e.preventDefault();
        try {
          await api(`/public/bind/${ticket}/verify`, {
            code: $("#verify-code").value,
          });
          toast("配对码已提交");
        } catch (e) {
          toast(e.message);
        }
      };
    if (terminal && (b.status !== "confirmed" || b.session_ready || b.setup_failed)) {
      clearInterval(bindingTimer);
      bindingTimer = null;
      if (!publicPage) await refresh();
      return true;
    }
  } catch (e) {
    toast(e.message);
    clearInterval(bindingTimer);
  }
}
async function sessionForm(id) {
  modal("连接 Managed Agent", '<div class="loading">读取 Agent 与环境…</div>');
  try {
    const r = await loadResources();
    const b = data.bots.find((b) => b.id === id);
    modal(
      b.session_id ? "新建并切换 Session" : "创建独立 MA 会话",
      `<form id="session-form"><p class="muted">${b.session_id ? `为「${esc(b.name)}」开启全新上下文。专属长期记忆、旧 Session 和历史任务保留；待发送消息及后续消息使用新 Session。当前任务须先执行和回传完成。` : `为「${esc(b.name)}」创建专属会话，自动挂载专属长期记忆，后续消息会复用这个会话。`}</p><label>Agent</label><select id="agent" required><option value="">请选择 Agent</option>${r.agents.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} · ${esc(a.id)}</option>`).join("")}</select><label>运行环境</label><select id="environment" required><option value="">请选择运行环境</option>${r.environments.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} · ${esc(a.id)}</option>`).join("")}</select><p class="muted">实际执行能力由所选 Agent 的工具和运行环境决定。</p><div class="form-actions"><button>创建并绑定会话</button></div></form>`,
    );
    $("#agent").value = b.agent_id || "";
    $("#environment").value = b.environment_id || "";
    $("#session-form").onsubmit = async (e) => {
      e.preventDefault();
      e.target.querySelector("button").disabled = true;
      try {
        await api(`/bots/${id}/session`, {
          agent_id: $("#agent").value,
          environment_id: $("#environment").value,
          new_session: Boolean(b.session_id),
          expected_session_id: b.session_id || "",
        });
        $("#modal").close();
        toast("MA 会话已连接，可在微信发送任务");
        await refresh();
      } catch (err) {
        toast(err.message);
        e.target.querySelector("button").disabled = false;
      }
    };
  } catch (e) {
    modal("MA 连接失败", `<p class="error-text">${esc(e.message)}</p>`);
  }
}
async function jobDetail(id) {
  let j;
  try { j=await api(`/jobs/${id}`); } catch(e) {toast(e.message);return;}
  modal(
    `任务 #${id}`,
    `<p class="muted">${esc(j.bot_name)} · ${esc(j.session_id || "尚未关联会话")}</p><p>${badge(j.status)}</p><label>${j.schedule_id?"定时任务指令":"微信请求"}</label><div class="pre">${esc(j.text)}</div>${j.attachments?.length ? `<label>附件</label><div class="pre">${j.attachments.map((a) => `${esc(a.filename)} · ${esc({ pending: "等待上传", checking: "审核中", available: "审核通过", rejected: "审核拒绝", type_rejected: "不支持此类型" }[a.status] || a.status)}${a.file_id ? ` · ${esc(a.file_id)}` : ""}${a.mounted_path ? `<br>已挂载：${esc(a.mounted_path)}` : ""}`).join("<br>")}</div>` : ""}${j.artifacts?.length ? `<label>回传文件</label><div class="pre">${j.artifacts.map(a => `${esc(a.filename)} · ${esc(({pending:"等待下载",uploaded:"已上传微信",sending:"发送中 / 待确认",sent:"已发送"})[a.status] || a.status)}`).join("<br>")}</div>` : ""}${j.charge_id ? `<button class="secondary small" id="job-charge">查看扣费明细 · ${money(j.charge_micro)}</button>` : `<p class="muted">计费：${esc(billingLabel(j.billing_state))}</p>`}${j.billing_error ? `<p class="error-text">${esc(j.billing_error)}</p>` : ""}<p class="muted">本轮网页搜索：${j.web_search_calls||0} 次</p><label>执行结果</label><div class="pre">${esc(j.result || "等待执行结果…")}</div>${j.error ? `<p class="error-text">${esc(j.error)}</p>` : ""}${["uncertain", "reply_uncertain", "interrupt_uncertain"].includes(j.status) ? `<p class="muted">请先核对百炼会话或微信消息。跳过不会重复提交 MA；重试回传可能重复显示微信回复。</p><div class="form-actions"><button class="secondary" id="skip-job">已核对，跳过记录</button>${j.status === "reply_uncertain" ? '<button id="retry-reply">仅重试微信回传</button>' : ""}</div>` : ""}${j.status === "requires_action" ? `<label>待审批工具调用</label><div class="pre">${esc(JSON.stringify(JSON.parse(j.approval), null, 2))}</div><div class="form-actions"><button class="secondary" id="deny">拒绝</button><button id="allow">允许本批调用</button></div>` : ""}`,
  );
  if ($("#job-charge")) $("#job-charge").onclick=async()=>{const r=await api(`/charges?job_id=${id}`);if(r.items.length) chargeDetail(r.items[0]);};
  for (const [selector, action] of [
    ["#skip-job", "skip"],
    ["#retry-reply", "retry_reply"],
  ]) {
    const control = $(selector);
    if (control)
      control.onclick = async () => {
        try {
          await api(`/jobs/${id}/resolve`, { action });
          $("#modal").close();
          await refresh();
        } catch (e) {
          toast(e.message);
        }
      };
  }
  for (const allow of [true, false]) {
    const b = $(allow ? "#allow" : "#deny");
    if (b)
      b.onclick = async () => {
        try {
          await api(`/jobs/${id}/approve`, { allow });
          $("#modal").close();
          await refresh();
        } catch (e) {
          toast(e.message);
        }
      };
  }
}
async function refresh() {
  data = await api(`/overview?archived=${showArchived}`);
  render();
}
async function start() {
  if (location.pathname.startsWith("/bind/")) {
    const ticket = location.pathname.split("/")[2];
    const finished = await bindingView(ticket, true);
    if (!finished)
      bindingTimer = setInterval(() => bindingView(ticket, true), 2500);
    return;
  }
  try {
    await refresh();
  } catch {
    login();
  }
  setInterval(async () => {
    if ($("#login")) return;
    try {
      data = await api(`/overview?archived=${showArchived}`);
      if (!$("#modal").open && page !== "billing") render();
    } catch {}
  }, 5000);
}
start();

let scheduleBotFilter='',scheduleShowDeleted=false;
function scheduleRuleText(r) {
  const zone=r.timezone||'Asia/Shanghai';
  if(r.kind==='daily') return `每天 ${r.time} · ${zone}`;
  if(r.kind==='weekly') return `每周 ${(r.weekdays||[]).map(d=>['一','二','三','四','五','六','日'][d-1]).join('、')} ${r.time} · ${zone}`;
  if(r.kind==='interval') return `每 ${r.minutes} 分钟`;
  return `单次 · ${r.run_at}`;
}
async function renderSchedules() {
  try {
    const r=await api(`/schedules?bot_id=${encodeURIComponent(scheduleBotFilter)}&include_deleted=${scheduleShowDeleted}`);
    if(page!=='schedules')return;
    $('#content').innerHTML=`<section class="panel"><div class="panel-head"><h2>定时任务 · ${r.items.length}</h2><div class="actions"><select id="schedule-bot" aria-label="按 Bot 筛选"><option value="">全部 Bot</option>${r.bots.map(b=>`<option value="${esc(b.id)}" ${b.id===scheduleBotFilter?'selected':''}>${esc(b.name)}</option>`).join('')}</select><button class="secondary small" id="schedule-deleted">${scheduleShowDeleted?'隐藏已删除':'包括已删除'}</button><button class="secondary small" id="schedule-refresh">刷新</button></div></div>${r.items.length?`<div class="table-wrap"><table><thead><tr><th>任务 / Bot</th><th>时间规则</th><th>下次执行</th><th>最近执行</th><th>操作</th></tr></thead><tbody>${r.items.map(t=>`<tr><td><b>${esc(t.name)}</b><small>${esc(t.bot_name)}${t.deleted?' · 已删除':''}</small></td><td>${esc(scheduleRuleText(t.rule))}</td><td>${t.next_run_at?esc(new Date(t.next_run_at).toLocaleString('zh-CN')):'—'}<small>按浏览器时区显示</small></td><td>${t.last_run?`${badge(t.last_run.status)}<small>#${t.last_run.id}</small>`:'尚未执行'}</td><td><div class="actions"><button class="secondary small" data-schedule-runs="${t.id}">执行记录</button>${!t.deleted?`<button class="secondary small" data-schedule-delete="${t.id}">删除</button>`:''}</div></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty"><h3>暂无定时任务</h3><p>在微信告诉 Agent 执行时间和具体任务，由 Skill 调用接口创建。</p></div>'}</section><div class="notice">每分钟检查到期任务；每次使用独立 Session，不替换当前聊天。执行与搜索费用计入 Bot 预算。删除停止后续触发，已开始的执行继续完成。</div>`;
    $('#schedule-bot').onchange=e=>{scheduleBotFilter=e.target.value;renderSchedules();};
    $('#schedule-deleted').onclick=()=>{scheduleShowDeleted=!scheduleShowDeleted;renderSchedules();};
    $('#schedule-refresh').onclick=renderSchedules;
    document.querySelectorAll('[data-schedule-runs]').forEach(b=>b.onclick=()=>scheduleRuns(b.dataset.scheduleRuns));
    document.querySelectorAll('[data-schedule-delete]').forEach(b=>b.onclick=async()=>{try{await api(`/schedules/${b.dataset.scheduleDelete}`,undefined,'DELETE');toast('已停止后续定时触发，历史记录保留');await renderSchedules();}catch(e){toast(e.message);}});
  } catch(e){toast(e.message);}
}
async function scheduleRuns(id,offset=0) {
  try {
    const r=await api(`/schedules/${id}/runs?offset=${offset}`);
    modal(`${r.schedule.name} · 执行记录`,`<p class="muted">${esc(scheduleRuleText(r.schedule.rule))}</p><label>执行指令</label><div class="pre">${esc(r.schedule.prompt)}</div><div class="table-wrap"><table><thead><tr><th>执行 / 触发时间</th><th>状态</th><th>独立 Session</th><th>费用</th><th></th></tr></thead><tbody>${r.items.map(j=>`<tr><td>#${j.id}<small>${esc(new Date(Number(j.scheduled_for)*1000).toLocaleString('zh-CN'))}</small></td><td>${badge(j.status)}${j.error?`<small class="error-text">${esc(j.error)}</small>`:''}</td><td><small class="mono">${esc(j.session_id||'待创建')}</small></td><td>${j.charge_id?money(j.charge_micro):esc(billingLabel(j.billing_state))}</td><td><button class="secondary small" data-scheduled-job="${j.id}">结果 / 明细</button></td></tr>`).join('')}</tbody></table></div><div class="form-actions"><span>共 ${r.total} 条</span><button class="secondary" id="schedule-run-prev" ${offset===0?'disabled':''}>上一页</button><button class="secondary" id="schedule-run-next" ${offset+50>=r.total?'disabled':''}>下一页</button><button id="schedule-run-refresh">刷新</button></div>`);
    $('#modal').classList.add('history-dialog');
    document.querySelectorAll('[data-scheduled-job]').forEach(b=>b.onclick=()=>jobDetail(Number(b.dataset.scheduledJob)));
    $('#schedule-run-prev').onclick=()=>scheduleRuns(id,Math.max(0,offset-50));
    $('#schedule-run-next').onclick=()=>scheduleRuns(id,offset+50);
    $('#schedule-run-refresh').onclick=()=>scheduleRuns(id,offset);
  }catch(e){toast(e.message);}
}
