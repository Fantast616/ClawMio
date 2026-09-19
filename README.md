# ClawBridge

**让微信成为百炼 Managed Agent 的入口。**

ClawBridge 是一个 Python / FastAPI + SQLite 应用，提供中文管理后台，将微信 ClawBot 消息接入阿里云百炼 Managed Agent（MA）。扫码绑定后自动创建独立会话和专属记忆库，支持文件交互、预算结算、Bot 自助接口及定时任务。

这是独立社区项目，不是微信或阿里云官方产品。使用前需自行取得相关服务的使用权限；云服务及模型调用可能产生费用。

## 从这里开始

| 你想做什么 | 阅读入口 |
| --- | --- |
| 第一次接入，不知道要准备什么 | [接入材料清单](docs/prerequisites.md) |
| 本地体验或部署到 Linux | 本页快速开始 → [完整部署指南](docs/deployment.md) |
| 用 Key 自动创建 Agent 和环境 | [资源初始化脚本](docs/bootstrap.md) |
| 配置模型、工具和 Agent Skill | [Agent 接入步骤](docs/prerequisites.md#2-准备-agent-与运行环境) |
| 理解代码或让 AI 修改功能 | [架构说明](docs/architecture.md) · [AGENTS.md](AGENTS.md) |
| 调用 Bot 的用量、会话和定时任务接口 | [Bot API](docs/bot-api.md) |
| 定价和缓存口径 | [计费说明](docs/billing.md) |
| 提交代码或报告问题 | [贡献指南](CONTRIBUTING.md) · [安全说明](SECURITY.md) |

## 功能

- **微信交互**：扫码绑定、多 Bot、文字/图片/文件输入、逐条文本回复和文件交付；支持连续消息中断。
- **会话与记忆**：绑定后自动创建 Session；每个 Bot 一个 MA Memory Store；保留所有已知 Session 历史。
- **管理后台**：执行结果、异常核对、工具审批、Bot 归档、手动刷新用量、预算和扣费明细。
- **计费**：按每轮累计用量差值结算 Token、显式/隐式缓存、活跃时间、web_search；欠费拒绝新执行。
- **自助 API / Skill**：每个 Bot 的长期 Token 通过 Session 环境变量注入，只能查询和操作自己的数据。
- **定时任务**：每天、每周、分钟间隔或单次；每分钟扫描，新建一次性 Session 执行，不替换聊天会话，结果回传微信。

## 快速开始

### 1. 准备

需要 **Python 3.11+**、可用的微信 ClawBot 扫码入口，以及百炼工作空间的 **API Key、工作空间 ID 和区域**。Agent 和运行环境可以由准备脚本自动创建。完整准备步骤见 [接入材料清单](docs/prerequisites.md)。

克隆本仓库或下载源码 ZIP，进入含 start.sh 的项目根目录。仓库不附带真实账号、默认管理员密码或演示数据库。

### 2. 创建并填写配置

```bash
python3 scripts/setup_env.py
```

生成 .env 和随机管理员密码、Cookie 密钥，不覆盖已有文件。Windows 可将 python3 换为 python。用编辑器填写 .env 中的真实值：

```dotenv
DASHSCOPE_API_KEY=你的百炼工作空间APIKey
BAILIAN_WORKSPACE_ID=你的工作空间ID
```

区域由 BAILIAN_REGION 指定，默认 cn-beijing，需与资源匹配。登录密码是 .env 中的 ADMIN_PASSWORD。

**首次接入：自动创建云端资源（Linux）**

```bash
bash start.sh install
.venv-linux/bin/python scripts/bootstrap_ma.py          # 预览
.venv-linux/bin/python scripts/bootstrap_ma.py --apply  # 创建并回填 ID
```

Windows 命令、模型选择、已有资源复用及失败恢复见 [初始化指南](docs/bootstrap.md)。已有资源也可以直接填写 `DEFAULT_AGENT_ID`、`DEFAULT_ENVIRONMENT_ID`，跳过自动创建。

### 3. 启动

Linux（Ubuntu / Debian 需先安装 python3-venv、python3-pip、util-linux）：

```bash
bash start.sh start
bash start.sh status
# 本机体验建议：HOST=127.0.0.1 bash start.sh start
```

Linux 脚本默认监听 0.0.0.0:8000，创建 .venv-linux，并在依赖变化后自动安装。

Windows PowerShell（已创建并编辑 .env）：

```powershell
.\start.ps1
```

Windows 默认监听 127.0.0.1:8000，使用 .venv。打开 [本机管理后台](http://127.0.0.1:8000)，使用自己配置的密码登录。

### 4. 完成第一次微信交互

1. 在「MA 资源」确认能读取 Agent 和环境。
2. 添加 Bot，分享二维码，使用微信扫码确认。
3. 等待初始 Session 创建及专属记忆挂载完成；失败可在 Bot 行查看原因并重试。
4. 从绑定微信发送一条文字，在「执行记录」查看结果；随后测试 /usage 和 /clear。
5. 如要启用预算扣费，先在「费用与额度」配置 Agent 单价。单价是本系统的本地规则，不会自动同步云账单。

### 5. 可选：自然语言自助服务与定时任务

基础聊天和直接发送 /usage、/clear 不依赖 Skill。若希望 Agent 回答“我还剩多少钱”或“每天八点查新闻发给我”，还需要：

1. 为后台准备一个**云端 MA 沙箱可访问的地址**，在 .env 设置 CLAW_API_BASE_URL=https://bots.example.com。示例域名必须换成自己的地址；127.0.0.1 无法指向你的服务器。
2. 运行 `python scripts/package_skill.py`，上传生成的 artifacts/claw-bot-self-service.zip 到百炼，审核通过后挂载到 Agent。
3. 将 [通用提示词](docs/agent-system-prompt.txt) 合并到 Agent 配置，启用 bash / Python 3 等所需能力。
4. 重启后端，并为已有 Bot **新建 Session**，使环境变量、Skill 和 Agent 快照生效。

PUBLIC_BASE_URL 是人打开绑定页面的地址；CLAW_API_BASE_URL 是云端 Agent 调用后端的地址。两者通常相同，但用途不同。

## 运行与升级

```bash
bash start.sh logs
bash start.sh restart
bash start.sh stop
```

**只运行单个服务进程、一个 Uvicorn worker。** 多进程会重复启动轮询和调度器。SQLite 默认位于 data/claw.db；升级保留 .env 和完整 data/，先备份，再更新代码和重启。后台/数据库是本地组件，Agent、Session、文件和 Memory Store 在百炼云端。

公网部署、systemd、反向代理、备份及故障定位见 [部署指南](docs/deployment.md)。

## 开发与验证

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --check static/app.js
python scripts/package_skill.py
python scripts/check_repository.py
```

Node.js 仅用于 JavaScript 语法检查，前端没有 npm 构建步骤。自动化测试使用临时数据库、假凭证和模拟外部接口，不需要真实 .env，见 [测试原理与边界](docs/testing.md)。CI 配置覆盖 Linux / Python 3.11、3.12；真实微信扫码、云端模型能力与端到端交付仍需部署者验证。

## 范围与限制

- 单管理员、单进程；不是多租户 SaaS，也没有支付充值系统。
- 不承诺固定 Bot 容量，容量受消息频率、云端限额、网络和服务器资源影响。
- 入站支持文字、图片、文件；不支持语音和视频。单文件本地上限 10 MB。
- 微信主动发送依赖有效登录和消息上下文，定时任务不代表可以绕过微信发送限制。
- 提交或发送结果不确定时保留人工核对入口，不提供外部系统端到端 exactly-once 保证。
- 归档只隐藏 Bot；暂停不取消已经在云端执行的任务。新会话不删除历史或长期记忆。

## 许可与依赖

许可见 [LICENSE](LICENSE)。第三方 SDK、云服务和协议说明分别受其自身许可或服务条款约束，见 [第三方说明](docs/third-party.md)。请勿把 API Key、Bot Token、数据库、日志或用户文件提交到公开仓库。
