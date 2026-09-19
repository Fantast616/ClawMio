# Bot 自助接口与部署

基础地址由部署者配置，文中使用 `https://bots.example.com` 作为示例。先完成 [接入准备](prerequisites.md) 和 [部署](deployment.md)。

## 更新 Linux 服务

更新 Git 工作区或将更新包解压覆盖 `/opt/clawbridge` 下的代码（保留原 `.env`、`data/`、数据库和虚拟环境）。在服务器 `.env` 添加：

```dotenv
CLAW_API_BASE_URL=https://bots.example.com
```

随后在项目目录执行 `bash start.sh restart`（若现有服务由 systemd 管理，则重启对应 service，不要并行启动第二个实例）。
启动时自动迁移数据库，为已有 Bot 补发 Token；新 Bot 创建时自动生成。Token 无过期时间，新建 Session 沿用同一个 Bot Token。
后台 Bot 的“API 凭证”入口可查看 Token。数据库保存 Token 原文以便再次注入，同时保存 SHA-256 摘要用于接口身份查找；不返回在列表或日志中。继续保护原有数据库备份与文件权限。

运行 `python scripts/package_skill.py`，然后上传生成的 `artifacts/claw-bot-self-service.zip` 到百炼自定义 Skill，扫描通过后挂载到需要使用的 Agent，并确保其有 Python 3 和 bash 工具。
**已有 Session 不会自动补注入环境变量或更新 Agent 快照**：挂载 Skill 后为 Bot 新建 Session（后台或微信 `/clear`）。初始、后台新建、微信 `/clear`、记忆升级和 API 新建会话均走同一环境变量注入代码。

创建 Session 的 `environment_variables` 包含：

- `CLAW_BOT_TOKEN`：此 Bot 的长期 Token。
- `CLAW_API_BASE_URL`：上述基础地址，不包含 `/api/bot`。

环境变量只在服务端创建 Session 时传递，不写入共享 Agent 配置。API 不使用百炼 API Key，也不接受管理员 cookie 代替 Bot Token。

## 请求约定

所有自助接口请求头：`Authorization: Bearer <Bot Token>`。不需要管理接口的 `X-Claw-Request`，不接受 URL 参数里的 Token。
身份完全由 Token 决定，不需要传 Bot ID，也不能通过指定他人 Bot ID 查询他人。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/bot/usage` | 自己的额度、历史已结算用量和当前 Session 累计用量 |
| POST | `/api/bot/sessions/reset` | 申请新会话；在当前轮结束、回传和结算后执行 |
| GET | `/api/bot/session-requests/{id}` | 查询自己的新会话请求状态 |

### 用量

金额字符串单位为元，保留六位小数。`budget.total/spent/remaining` 分别为总额、已扣、剩余；总额和剩余为 null 且 `unlimited=true` 表示不设上限。`pending_billing_count` 表示尚未结算的任务数。

`settled_usage`：从扣费明细累计的历史已结算 Token、缓存创建/读取、搜索调用次数、活跃微秒；不包含未启用计费的历史任务。
`current_session`：本次实时查询到的当前会话累计统计，与已结算总量有重叠，不能相加。远端查询失败时该字段为 null，仍返回额度和已结算数据，并通过 `current_session_error` 告知失败。

### 新建 Session

额外请求头 `Idempotency-Key: <UUID>`。请求体：

```json
{"expected_session_id":"sesn_当前会话ID"}
```

会话 ID 从用量接口取得。成功受理 HTTP 202，返回：

```json
{"id":"操作ID","status":"queued","from_session_id":"sesn_旧会话","session_id":"","error":"","created_at":"时间"}
```

相同 Bot 下相同键和参数总是返回同一操作；同键不同参数返回 409。同一 Bot 只能有一个待处理请求。网络超时后重试必须使用原键和原参数。
当前 Agent 调用后应正常结束这一轮，不要在同一轮等待完成，否则后端无法进入安全切换时机。

- `queued`：等待当前任务和费用处理完毕；暂停状态下不会执行。
- `creating`：正在创建新 Session。
- `uncertain`：创建结果暂不确定，后台仅查询远端 metadata 恢复，不重复创建。
- `completed`：已切换；`session_id` 为新会话。
- `superseded`：期间已通过其他入口切换，返回当前会话，不再额外创建。
- `failed`：创建被拒绝或结算后欠费，参见 `error`。

新会话保留原 Agent、环境、私有 Memory Store、Token、历史会话和扣费记录。不会删除长期记忆。历史记录写入、当前会话切换、操作完成状态在同一 SQLite 事务内提交。

HTTP 401 凭证错误；402 欠费；404 操作不存在或不属于当前 Bot；409 当前会话变更、已有待处理请求或 Bot 暂不可用；422 参数错误。

## Agent Skill

源文件：`skills/claw-bot-self-service/SKILL.md` 和 `scripts/bot_api.py`，ZIP 根目录直接包含 `SKILL.md`。
脚本提供 `usage`、`reset --session-id ... --request-id ...`、`status --operation-id ...` 三个子命令，只依赖 Python 标准库。
脚本只向已配置的基础地址发送凭证，拒绝跟随 HTTP 重定向；不打印 Token，不将 Token 放入命令参数。


## 定时任务接口

同样使用 Bot Bearer Token。新增接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/bot/schedules` | 创建，携带 `Idempotency-Key` |
| GET | `/api/bot/schedules` | 查询自己的未删除任务 |
| GET | `/api/bot/schedules/{id}` | 单个任务（包括已删除状态） |
| GET | `/api/bot/schedules/{id}/runs?offset=0` | 每页 50 条执行记录 |
| DELETE | `/api/bot/schedules/{id}` | 停止后续触发，保留历史 |

创建示例：

```json
{"name":"每日科技新闻","prompt":"查询今天的重要科技新闻，简要总结并给出来源。","rule":{"kind":"daily","time":"08:00","timezone":"Asia/Shanghai"}}
```

`rule.kind` 支持 daily、weekly（增加 weekdays，1=周一）、interval（minutes）、once（run_at 为包含时区的未来时间）。缺省时区 Asia/Shanghai。响应 next_run_at 为 UTC ISO 时间。创建请求重复使用同一键时返回同一个任务；不同参数复用同一键返回 409。

后端每 60 秒扫描一次，并用 SQLite 事务领取触发时刻，生成关联 schedule_id 的执行记录。错过多个周期时合并补一次，同一任务上一轮未结束时跳过本次触发并留下原因。暂停 Bot 时不启动定时任务，恢复后按合并补发规则处理。更换绑定用户后旧用户任务会停止。

每个 Bot 的定时任务依次执行，与当前聊天使用不同 Session 和 SSE 缓冲。新 Session 挂载同一个 Bot 的专属记忆库和身份环境变量，保存在会话历史中但不修改 Bot 当前 Session。只在本次任务完成后回传 assistant 消息和文件；工具内部信息不回传。费用沿用锁定单价及本 Bot 预算，搜索费同样结算；欠费不启动。

删除会取消未创建 Session 的排队记录，已经开始的执行继续完成。创建响应丢失时通过远端 metadata 恢复，避免重复创建；提交和微信回传结果不确定时仍沿用原有人工核对入口。微信发送仍依赖有效登录和消息上下文，失败可在执行记录查看，不能保证微信平台允许无限期主动推送。

后台新增“定时任务”：按 Bot 筛选、查看已删除任务、下次执行时间、最近状态和分页执行记录；从“结果 / 明细”进入执行输出、审批/异常处理以及扣费明细。管理 API 为 GET /api/schedules、GET /api/schedules/{id}/runs、DELETE /api/schedules/{id}，沿用管理登录鉴权。

更新 requirements.txt 后使用 `bash start.sh restart` 会自动安装新增的 tzdata 依赖。上传新版 Skill ZIP 并重新挂载，然后新建聊天 Session，Agent 才会读到新的定时任务指令。

## Agent 通用提示词

`docs/agent-system-prompt.txt` 是与代码同步的新版微信通用提示词。将它替换远端 Agent 中旧的微信交互段落（保留其他业务配置），不要同时保留旧的“不要通过工具执行”限制。自然语言额度查询、重开会话和定时任务将使用 Skill；单独发送 `/usage`、`/clear` 仍由后端直接处理。更新 Agent 提示词和 Skill 后新建 Session，已有 Session 保留旧快照。
