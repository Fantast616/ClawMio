# 接入前需要准备什么

建议先完成基础聊天，再开启文件、计费和定时任务。账号、开通权限和 Key 由部署者准备；项目提供 [前置脚本](bootstrap.md) 自动创建 Agent 与环境，不赠送云资源或额度。

推荐使用 [CLI 软件包](cli.md)：安装后 `clawmio start --open`，向导先输入 API Key，再填写工作空间和可选 Base URL；自动创建资源、生成密码，并在填写 Base URL 时处理 Skill 上传及挂载。以下手工步骤仍可用于源码部署和排查。CLI 不依赖 Linux 的 flock 命令，软件包已包含静态前端和 Skill 源文件。

## 1. 材料清单

| 材料 | 是否必需 | 获取/确认方式 | 配置位置 |
| --- | --- | --- | --- |
| Python 3.11+、Git 或源码 ZIP | 必需 | `python --version` / `python3 --version` | 本机或服务器 |
| Linux venv/pip/flock | Linux 必需 | Ubuntu/Debian 安装 `python3-venv python3-pip util-linux` | 系统环境 |
| 百炼 Managed Agent 访问权限与可用额度 | 必需 | 在自己的阿里云账号确认开通并能访问控制台 | 云账号 |
| 工作空间 ID 与区域 | 必需 | MA 控制台工作空间信息 | `BAILIAN_WORKSPACE_ID`、`BAILIAN_REGION` |
| 工作空间 API Key | 必需 | 由有权限的管理员创建，覆盖本项目资源操作 | `DASHSCOPE_API_KEY` |
| Agent ID | 运行时必需 | 初始化脚本自动创建并回填，或复用已有资源 | `DEFAULT_AGENT_ID` |
| Environment ID | 运行时必需 | 初始化脚本自动创建并回填，或复用已有资源 | `DEFAULT_ENVIRONMENT_ID` |
| 支持 ClawBot 绑定的微信账号和扫码手机 | 必需 | 能扫描项目申请的二维码并确认；以微信实际可用性为准 | 后台扫码，不需手写微信 Token |
| 随机管理密码与 Cookie 密钥 | 必需 | `python scripts/setup_env.py` 生成 | `.env` |
| 持久化磁盘和备份位置 | 必需 | 保留 SQLite 与配置 | 默认 `data/` |
| 微信/MA/CDN 出站网络 | 必需 | 服务器能访问官方 API 和媒体地址 | 网络、防火墙 |
| 用户可访问的绑定页地址 | 分享链接时需要 | 自有域名或可达地址 | `PUBLIC_BASE_URL` |
| MA 沙箱可访问的后端地址 | Agent 自助/定时 Skill 需要 | 推荐自有 HTTPS 域名 | `CLAW_API_BASE_URL` |
| 自定义 Skill 上传权限 | 自助/定时 Skill 需要 | 上传 ZIP、通过审核、挂载 Agent | 百炼控制台 |
| 模型 Token、缓存等本地费率 | 启用预算计费时需要 | 按自己的模型和业务规则填写 | 后台「费用与额度」 |

不要混用其他百炼产品的标识和密钥；需要的是 **Managed Agent 工作空间、Agent、Environment**。原生 Memory Store 由应用按 Bot 创建，不是独立“记忆库”产品的 UserId 接口。

## 2. 准备 Agent 与运行环境

推荐先使用 [自动准备脚本](bootstrap.md)，填写 Key、工作空间 ID、区域后即可创建基础环境与 Agent，微信提示词及常用工具一并配置。下面是手工配置/检查路径：

1. 打开 [MA 控制台](https://agent.console.aliyun.com/managed-agent)，选择正确工作空间和区域。
2. 创建或选择一个可用运行环境；运行过初始化脚本可直接使用它创建的资源。
3. 创建或选择 Agent，设置实际可用的模型。图片理解取决于模型与读取工具能力，后端支持上传并不代表模型能理解图片。
4. 按用途启用工具：文件处理常需 bash、read/write/edit；记忆检索需相应文件/记忆读取能力；交付产物需 `mark_artifacts`；新闻查询需 `web_search`；自助 Skill 需要 bash 和 Python 3。
5. 将 [通用微信提示词](agent-system-prompt.txt) 合并到 Agent system，移除旧的冲突约束。保留业务提示词及工具审批策略，不必把所有工具设为自动批准。
6. 先在百炼控制台用该 Agent 与环境跑通一条普通任务，记录两者 ID，填写 `.env`。

Session 锁定创建时的 Agent 快照；修改工具、提示词、模型或 Skill 后，应新建 Session。会话通过 `environment_variables` 注入字符串键值，参见 [创建 Session 文档](https://docs.agent.bailian.aliyun.com/zh/api/managed-agents/session/create)。

默认 ID 留空时，应用只在对应资源恰好有一个时自动选择；多个资源时应明确填写。

## 3. 地址与网络怎么选

| 场景 | PUBLIC_BASE_URL | CLAW_API_BASE_URL |
| --- | --- | --- |
| 本机体验聊天，分享二维码图片 | `http://127.0.0.1:8000` | 可留空 |
| 其他人需要打开绑定页面 | 他们能访问的服务地址 | 不使用自助 Skill 可留空 |
| Agent 查询余额、重开会话、创建定时任务 | 用户可访问的地址 | MA 沙箱能访问的地址，如 `https://bots.example.com` |

`bots.example.com` 是示例，必须替换。基础地址不加 `/api/bot`；脚本会追加路径。更改地址后重启后端，并新建 Session 更新环境变量。

微信消息使用长轮询，不需要微信公网 webhook；MA 调用自助 API 则需要访问你的后端。微信回复需要入站消息中的 `context_token`，因此扫码后先发一条文字验证。参见 [微信协议](https://www.wechatbot.dev/zh/protocol)。

## 4. 准备 Agent Skill

```bash
python scripts/package_skill.py
```

上传生成的 `artifacts/claw-bot-self-service.zip`；ZIP 根目录为 `SKILL.md`，脚本在 `scripts/bot_api.py`。等待安全扫描通过，再挂载到 Agent。包中没有账号、Token 或服务器地址。

后端在创建 Session 时注入 `CLAW_BOT_TOKEN` 与 `CLAW_API_BASE_URL`。**不要把某个 Bot 的 Token 填进共享 Agent 或 Skill 包。** 已有 Bot 自动补发 Token，但旧 Session 需重新创建。

## 5. 首次验收清单

- [ ] 服务启动无报错，可以用自己生成的管理密码登录。
- [ ] 「MA 资源」列出正确工作空间中的 Agent 与环境。
- [ ] Bot 扫码后，初始 Session 与 Memory Store 就绪。
- [ ] 微信发送文字，能收到真实回答。
- [ ] `/usage` 返回额度；`/clear` 后历史保留、当前 Session ID 改变。
- [ ] 图片/文件经审核后可被读取；产物经 `mark_artifacts` 注册并在微信收到。
- [ ] 配置费率后执行一轮，核对 Token、缓存、搜索、活跃时间与总费用。
- [ ] 挂载 Skill 并新建 Session 后，自然语言询问额度能调用自己的接口。
- [ ] 创建几分钟后的单次任务，能查到执行记录和回传，独立 Session 不替换聊天。

请求样例：[每日新闻](../examples/schedule-daily.json)、[单次任务](../examples/schedule-once.json)。单次任务的示例时间必须改为未来时间。
