# ClawMio

**用更低的部署成本，提供龙虾类产品的核心助理功能：一个控制面管理多个 Bot，执行交给百炼 Managed Agent，运行时按量付费。**

ClawMio 是一个 Python / FastAPI + SQLite 应用，提供中文管理后台，将微信 ClawBot 消息接入阿里云百炼 Managed Agent（MA）。扫码绑定后自动创建独立会话和专属记忆库，支持文件交互、预算结算、Bot 自助接口及定时任务。

用户在微信里提出需求，Agent 在云端使用工具、处理文件、执行任务，再把文字与产物送回微信。ClawMio 负责接入、身份、会话、调度和额度管理，底层执行由 Managed Agent 托管，适合把个人助理服务提供给多位用户。

这是独立社区项目，不是微信或阿里云官方产品。使用前需自行取得相关服务的使用权限；云服务及模型调用可能产生费用。

## 面向谁

- **为多人提供 AI 助理的开发者和服务运营者**：统一开通 Bot、配置 Agent、分配额度、查询执行记录，减少逐个部署实例的工作。
- **小团队和工作室**：通过微信提供资料检索、文件处理、内容交付和定时汇总，每个 Bot 保留自己的记忆与会话。
- **想快速验证助理产品的独立开发者**：复用现成的管理后台、身份接口、计费和调度逻辑，将精力放在提示词、Skill 和业务能力上。
- **希望在微信使用个人助理的用户**：由部署者提供服务，扫码后使用，无需自己维护一套 Claw 运行环境。

当前项目是单管理员控制面，可作为上述服务的起点；它不包含完整的多租户组织权限、支付充值或高可用集群能力。

## 能替代哪些「龙虾类」使用场景

**对于以微信为入口、在云端完成任务的个人助理需求，当前功能已覆盖主要使用链路，可以作为龙虾类产品的替代方案。**

| 用户需求 | 当前提供的能力 |
| --- | --- |
| 随时交谈、连续追问 | 微信文字交互、多轮 Session、连续消息中断、逐条回复 |
| 记住我的偏好 | 每个 Bot 专属 Memory Store；新开会话时继续挂载长期记忆 |
| 查资料、处理图片和文件 | 文件上传审核与会话挂载，结合 Agent 的模型、工具和 Skill 执行 |
| 帮我生成并交付文件 | 注册 `mark_artifacts` 产物后回传微信，图片直接展示 |
| 每天帮我查信息并发来 | 每日、每周、间隔和单次定时任务，独立 Session 执行并回传结果 |
| 看看用了多少钱、重新开始聊天 | `/usage`、`/clear` 后端指令，以及 Skill 支持的自然语言自助接口 |
| 管理多位用户的助理 | 多 Bot、归档、会话历史、执行记录、预算、扣费明细与欠费拦截 |

这里的替代范围是上述使用场景，不代表兼容所有 Claw 插件、全部 IM 渠道或本机桌面/设备控制。具体任务效果取决于接入模型、启用工具与 Skill；当前支持边界见文末「范围与限制」。

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

### 推荐：安装 CLI 软件包

**[下载 Linux 发布包](https://github.com/Fantast616/ClawMio/releases/download/v0.0.1/clawmio-0.0.1-linux.tar.gz)** · [下载通用 wheel](https://github.com/Fantast616/ClawMio/releases/download/v0.0.1/clawmio-0.0.1-py3-none-any.whl) · [查看 Release](https://github.com/Fantast616/ClawMio/releases/tag/v0.0.1)

Linux 发布包解压后即可安装启动：

```bash
tar -xzf clawmio-0.0.1-linux.tar.gz
cd clawmio-0.0.1-linux
./install.sh
./clawmio start
```

需要 Python 3.11+、venv/pip 和安装依赖的网络；发布包内含 wheel 与安装入口，不是免 Python 的独立二进制。

需要 Python 3.11+。在源码根目录安装，或将第一条换成 `python -m pip install ./clawmio-0.0.1-py3-none-any.whl`：

```bash
python -m pip install .
clawmio start --open
```

首次启动会先隐藏输入百炼 API Key，再填写工作空间 ID、区域和可选 Base URL；自动生成管理员密码，展示计划后创建 Environment、Agent，并按需上传和挂载自助 Skill。Base URL 留空即可先使用基础聊天、`/usage` 和 `/clear`。服务就绪后输出监听与可访问地址；使用 `clawmio password` 查看登录密码。

```bash
clawmio status
clawmio logs -f
clawmio stop
clawmio restart
clawmio configure
clawmio --help
```

不带子命令的 `clawmio` 会前台启动。配置和数据默认放在 `~/.clawmio`，与安装目录分开。完整命令、可选功能、升级和故障恢复见 **[CLI 使用指南](docs/cli.md)**。目前提供本地构建/安装方式，不假定已发布到 PyPI。

### 源码脚本方式（兼容原工作流）

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
