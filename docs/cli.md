# ClawMio CLI 使用指南

CLI 支持 Windows、Linux、macOS，需要 Python 3.11+。安装包包含管理后台静态文件、默认 Agent 提示词和云端自助 Skill，无需 Node.js 或前端构建。它仍然通过百炼执行 Agent 任务，不是离线模型软件。

## 安装与第一次启动

Linux 可以直接下载 `clawmio-0.0.1-linux.tar.gz`，执行：

```bash
tar -xzf clawmio-0.0.1-linux.tar.gz
cd clawmio-0.0.1-linux
./install.sh
./clawmio start
```

安装脚本在解压目录建立 `.venv` 并安装软件包和依赖，不需要 sudo。需要 Python 3.11+、venv/pip 和下载依赖的网络；这是带安装入口的发布包，不是免 Python 二进制。包内命令使用 `./clawmio`，后续文档中的 `clawmio` 均可据此替换。服务器无桌面环境时无需 `--open`。

下载项目提供的 wheel 后安装（文件名随版本变化）：

```bash
python -m pip install ./clawmio-0.0.1-py3-none-any.whl
clawmio
```

也可以在源码根目录 `python -m pip install .`。尚未发布到 PyPI，不能假定 `pip install clawmio` 能取得本项目。Linux 若系统 Python 禁止直接安装包，请先创建并激活 venv；也可用 `pipx install ./clawmio-0.0.1-py3-none-any.whl`。

命令不在 PATH 时可使用 `python -m clawmio`。不带子命令等价于 `clawmio run`：首次进入向导，完成后前台启动；Ctrl+C 停止。希望关闭终端后继续运行，使用 `clawmio start --open`。

向导按顺序收集：

1. **百炼 API Key**：隐藏输入，不在结果和普通配置查看中回显。
2. **MA 工作空间 ID 和区域**：从 [MA 控制台](https://agent.console.aliyun.com/managed-agent) 复制；目前接口地址需要这两项，不能只凭 Key 自动发现。
3. **可选 Base URL**：云端 MA 能访问的后台地址，例如你自己的 HTTPS 域名。首次直接回车即可跳过。
4. **监听 IP / 端口**：默认 `127.0.0.1:8000`，只供本机访问。需要局域网访问或跨容器反向代理时可填 `0.0.0.0`。
5. **可选绑定分享地址**：供人打开绑定页面使用；留空默认使用本机地址。

向导自动生成管理员密码和 Cookie 密钥，展示资源计划，回车确认后创建/复用 Environment 和 Agent，保存资源 ID。填写了 Base URL 时，还会打包上传自助 Skill、等待安全审核、挂载到 Agent。新建 Agent 使用模型 `auto`，显式启用 bash、read、write、edit、glob、grep、web_search、mark_artifacts（自动批准），环境允许出站网络；后续使用可能计费。可在 `init --no-provision` 后用 `config set MA_MODEL 模型ID` 调整，再执行资源准备。

服务就绪后才输出访问地址。通配监听时额外输出本机网卡上的候选地址；CLI 不保证防火墙、反向代理或公网域名已经可达，不会创建隧道。`0.0.0.0` 本身不能当作访问网址。

登录密码使用 `clawmio password` 在交互终端查看。打开后台后添加 Bot、扫码绑定，再配置所需预算和费率；CLI 不猜测云端账单费率。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `clawmio` / `clawmio run` | 首次向导，前台启动，Ctrl+C 停止 |
| `clawmio init` | 交互配置并准备云资源，不启动服务 |
| `clawmio init --no-provision` | 只生成本地配置，不访问云端 |
| `clawmio configure` | 重新填写配置；回车保留旧值，可选 URL 输入 `-` 清空 |
| `clawmio provision` | 离线预览云端配置计划 |
| `clawmio provision --apply` | 执行云端准备 / 续跑审核 / 同步 Skill |
| `clawmio provision --apply --yes` | 接受计划，非交互执行；须先准备配置 |
| `clawmio provision --apply --skill-wait 600` | 最多等待 Skill 审核 600 秒 |
| `clawmio start --open` | 后台启动，就绪后打开浏览器 |
| `clawmio run --host 0.0.0.0 --port 8080` | 本次更换监听地址，不改保存的默认值 |
| `clawmio status` | 查询当前目录的 CLI 服务状态和地址 |
| `clawmio open` | 打开已运行的管理后台 |
| `clawmio stop` | 优雅停止；不删除配置、数据或云资源 |
| `clawmio restart` | 停止后按保存的配置后台启动；临时参数需再次传入 |
| `clawmio logs -n 100` | 查看后台日志最后 100 行 |
| `clawmio logs -f` | 跟踪后台日志，Ctrl+C 退出查看 |
| `clawmio doctor` | 离线检查配置、包资源及数据库进程锁 |
| `clawmio config path` | 显示 `.env` 路径 |
| `clawmio config show` | 查看脱敏配置 |
| `clawmio config set PORT 8080` | 持久更换端口，启动时生效 |
| `clawmio config set DASHSCOPE_API_KEY` | 隐藏输入新 Key；敏感值不可放在命令参数里 |
| `clawmio password` | 在终端显示管理员密码 |
| `clawmio password --reset` | 停服后生成新密码及 Cookie 密钥，使旧登录失效 |
| `clawmio skill-package --output ./self-service.zip` | 只导出 Skill 包，不上传；不覆盖已有文件 |
| `clawmio --version` / `clawmio --help` | 查看版本和命令列表 |

每个子命令均支持 `--help`。修改配置、重新 provision 和重置密码前需先 `stop`，避免运行进程继续使用旧配置。`doctor` 不访问云端，不能验证 Key 权限、模型可用性或微信真实交互。

## 可选 Base URL 与 Skill

`CLAW_API_BASE_URL` 是 **云端 Agent 调用后端** 的地址，不是百炼 API 网关。填写 `https://你的域名` 或实际可达的 `http://服务器IP:端口`，不加 `/api/bot`、路径或查询参数。回环地址如 `127.0.0.1`、`localhost`、`::1` 指向云沙箱自身，CLI 会拒绝；私网地址仅在你已安排连通网络时适用，公网推荐 HTTPS。

Base URL 留空时不会上传或挂载本软件管理的 Skill。自然语言查额度、重开会话、管理定时任务依赖该 Skill；基础聊天、文件能力、精确指令 `/usage` 和 `/clear` 不受影响。清空 URL 不会删除已有定时任务或取消正在执行的任务；需要停止任务请在后台操作。

以后启用：

```bash
clawmio stop
clawmio config set CLAW_API_BASE_URL https://你的域名
clawmio provision --apply
clawmio start
```

以后关闭：

```bash
clawmio stop
clawmio config set CLAW_API_BASE_URL
# 提示新值时直接回车，保存为空
clawmio provision --apply
clawmio start
```

CLI 只增删本安装状态追踪的自助 Skill 挂载，保留 Agent 其他 Skill、工具与业务配置。**配置变更后，已有 Bot 必须新建 Session**，旧 Session 的 Agent 快照和环境变量不会更新。修改模型配置仅影响将来新建的 Agent，不会自动替换已有 Agent 模型。

Skill 审核只在指定版本状态为 `active` 时允许挂载；`checking` 会等待，`rejected` 会停止并提示到控制台查看。参见 [官方 Skill 版本接口](https://docs.agent.bailian.aliyun.com/zh/api/managed-agents/skill/get-version)。默认等待五分钟，超时后进度保留，稍后重跑 `provision --apply`；不重复上传已取得 ID 的资源。

## 配置目录、升级和迁移

默认目录为 `~/.clawmio`，Windows 通常为 `%USERPROFILE%\.clawmio`。也可以设置 `CLAWMIO_HOME`，或每次显式传入：

```bash
clawmio --home /srv/clawmio init
clawmio --home /srv/clawmio start
clawmio --home /srv/clawmio status
```

`--home` 优先于环境变量。多实例使用不同目录、数据库、端口；同一数据库只能有一个进程。配置读取该目录 `.env`，CLI 启动时以文件值为准，避免继承终端内其他账号的同名环境变量。相对 `DATABASE_PATH` 按配置目录解析；新软件包不会读取原仓库的真实 `.env`。

该目录保存 `.env`、`data/claw.db`、`data/bootstrap-ma.json`、后台日志及进程控制文件。Key、管理员密码和数据都需按私密文件保护。Unix 新目录/配置分别使用 0700/0600；Windows 使用用户目录 ACL。`password` 不向重定向输出或管道显示密码。

升级：`stop` 后备份整个配置目录，再 `python -m pip install --upgrade 新版本.whl`，最后 `start`。不要把数据库或 `.env` 复制到安装包目录。后台 `start` 不是开机自启服务；生产环境可让 systemd 等进程管理器运行 `python -m clawmio --home /srv/clawmio run`，始终单进程，不要同时开启 CLI 后台模式。

从旧源码启动方式迁移时，先用原启动方式停服，再备份根目录 `.env` 和 `data/`。可以把这两项复制到新的配置目录，或直接 `clawmio --home 原项目绝对路径 start`。`status` / `stop` 只管理 CLI 服务，不能接管旧脚本的进程；数据库锁会阻止新旧方式重复启动。

## 失败处理

- **API 鉴权或资源参数错误**：检查 Key、工作空间、区域和模型；修正后重新 provision。错误不回显 Key 或云端响应正文。
- **资源创建超时 / 5xx**：保留 `data/bootstrap-ma.json`；Environment/Agent 会按安装标识查找恢复，无法确定时停下，绝不盲目重建。可在控制台找到 ID 后用 `config set DEFAULT_AGENT_ID ...` 等回填。
- **Skill 文件上传或创建结果不确定**：先在控制台核对，再分别用 `config set SETUP_SKILL_FILE_ID file_...` 或 `config set SETUP_SKILL_ID skill_...` 回填并重跑。不要把不相干的文件或 Skill ID 填进来。
- **Skill 挂载更新结果不确定**：重跑先读取 Agent；已达到预期则恢复，仍不一致则要求人工核对，不重复提交。更换目标或计划前先核对状态文件中的 `skill_mount`。
- **审核未完成**：等待后再次运行 `provision --apply`；若急需基础聊天，可清空 Base URL 后重新 provision，再启动。
- **端口占用**：`config set PORT 8080` 后启动；候选监听地址不保证网络可达。
- **启动失败**：查看 `logs`。三十秒未就绪时会提示查询状态，不能把超时当作进程没启动。
- **异常退出**：内核自动释放进程锁，旧 PID 不作为杀进程依据。不要删除正在使用的 `.lock` 文件；CLI 用当前进程随机令牌请求优雅退出。

## 构建与验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m pip install build
python -m build
python scripts/check_distribution.py dist/clawmio-0.0.1-py3-none-any.whl
python scripts/package_release.py
```

输出位于 `dist/`：wheel 可直接安装，`clawmio-版本.tar.gz` 是源码包，`clawmio-版本-linux.tar.gz` 是带安装入口的 Linux 发布包，另附 SHA-256 校验文件。资源打包采用显式文件名单，不包含 `.env`、数据库、日志或生成的 Skill ZIP。`tests/test_cli.py` 覆盖隔离数据库下的本地 HTTP 启停、隐藏配置、锁及模拟云端恢复；它们不等同于真实百炼/微信端到端验证。
