# 部署、升级与排障

先准备 [材料清单](prerequisites.md)。所有命令在项目根目录执行；示例 `/opt/clawmio` 和 `bots.example.com` 均需替换为自己的路径/地址。

## 本地 Linux

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv python3-pip util-linux
python3 --version  # 必须 >= 3.11；发行版版本过旧时先安装合适的 Python
python3 scripts/setup_env.py
# 用编辑器填写 .env 的 Key、workspace、区域
bash start.sh install
.venv-linux/bin/python scripts/bootstrap_ma.py
.venv-linux/bin/python scripts/bootstrap_ma.py --apply
HOST=127.0.0.1 bash start.sh start
```

启动器创建 `.venv-linux`、自动安装 `requirements.txt`，验证必填字段，并通过文件锁防止同一启动器重复运行。日志在 `logs/server.log`；PID/锁在 `data/run/`。`bash start.sh run` 前台启动；`stop/restart/status/logs/install` 分别停止、重启、查询、看日志和安装依赖。

`HOST`/`PORT` 是启动器的 shell 环境变量，不由 `.env` 配置监听地址。默认 `HOST=0.0.0.0 PORT=8000`；修改端口后应同步两个基础 URL。指定 Python 示例：`PYTHON=python3.12 bash start.sh start`。

## Windows

首次接入先按 [资源初始化指南](bootstrap.md#windows) 自动创建 Agent 和环境；已有资源可直接填写 ID。

```powershell
python --version
python scripts/setup_env.py
# 编辑 .env
.\start.ps1
```

启动器创建 `.venv`，安装/更新依赖并以前台方式启动。默认 `127.0.0.1:8000`，关闭时 Ctrl+C。
若脚本受执行策略限制，可手动执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

## 环境变量

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| DASHSCOPE_API_KEY | 是 | MA 工作空间 Key，仅保存在后端 |
| BAILIAN_WORKSPACE_ID | 是 | 与 Key、资源匹配的工作空间 |
| BAILIAN_REGION | 是 | 资源所在区域，模板默认 cn-beijing |
| ADMIN_PASSWORD | 是 | 管理后台密码，不提供统一默认密码 |
| COOKIE_SECRET | 是 | 随机 Cookie 签名密钥；更换后已有登录失效 |
| DATABASE_PATH | 否 | 默认 data/claw.db，相对路径以工作目录为基准 |
| DEFAULT_AGENT_ID / DEFAULT_ENVIRONMENT_ID | 建议 | 资源不唯一时必须明确选择 |
| PUBLIC_BASE_URL | 分享页面时 | 人打开绑定页面的地址 |
| CLAW_API_BASE_URL | 自助 Skill 时 | MA 沙箱访问后端的地址，不含 /api/bot |

`.env` 按 dotenv 格式解析，不要在其中写 `export`、shell 命令或命令替换。系统进程环境中的同名变量优先于 `.env`。`.env.example` 不可直接当成真实生产配置。

## Linux 长期运行

服务端应使用持久化目录、非 root 服务用户和单进程。可参照 [systemd 示例](../deploy/clawmio.service)：

1. 放置代码于 `/opt/clawmio`，创建服务用户 `clawmio`，使它能读代码和 `.env`，并写入虚拟环境、data、logs。
2. 在该用户身份下准备 `.env`，运行 `bash start.sh install`。
3. 编辑示例中的用户/目录，复制到 `/etc/systemd/system/clawmio.service`。
4. 运行 `sudo systemctl daemon-reload` 和 `sudo systemctl enable --now clawmio`。
5. 用 `sudo systemctl status clawmio`、`sudo journalctl -u clawmio -f` 查看状态。

systemd 使用前台 `start.sh run`。使用 systemd 后，统一由 `systemctl restart/stop` 管理，不要另开 `start.sh start` 或手动 Uvicorn。

## 域名与反向代理

如果需要云端 Agent 调用接口，将服务部署到它可访问的网络。公网建议 HTTPS，反向代理示例见 [nginx.conf.example](../deploy/nginx.conf.example)。替换域名和证书路径并通过 `nginx -t` 后再启用；示例不会自动申请证书。

后端仅监听 `127.0.0.1:8000`；公网开放反向代理端口即可，不必同时公开后端端口。配置：

```dotenv
PUBLIC_BASE_URL=https://bots.example.com
CLAW_API_BASE_URL=https://bots.example.com
```

不要把外部用户提供的 `X-Forwarded-*` 当作可信代理信息；仅信任你控制的反向代理。当前 Uvicorn 默认信任本地代理，本示例也将 nginx 与后端放在同一主机。

## 升级与备份

数据库升级在服务启动时自动执行增量迁移，可能不兼容降级。先停止服务再备份 `.env` 和完整 `data/`（SQLite 使用 WAL，运行中不能只复制主 db 文件）。这些备份包含密钥、微信凭证和用户内容，不应公开。

```bash
bash start.sh stop
# 使用自己的备份策略复制 .env 与完整 data/ 到受保护目录
git pull --ff-only
bash start.sh start
```

systemd 部署改用相应 `systemctl` 命令。不要覆盖已有 `.env`，不要清空测试之外的数据库，也不要在旧/新版本间同时运行同一数据目录。
修改 `requirements.txt` 后启动器会更新依赖。Agent 提示词/Skill 在百炼端更新后，现有 Bot 需新建 Session。只更新本地文本文件不会修改远端 Agent。

## 排障

| 现象 | 检查与处理 |
| --- | --- |
| 无法登录 | 密码取自自己的 .env，不是仓库内固定密码；检查进程环境是否覆盖配置 |
| MA 401/403 或资源列表为空 | 检查 Key、workspace、region、权限和账号额度是否匹配 |
| 绑定后没有 Session | 默认资源是否唯一/ID 是否正确；查看 Bot 的初始化错误并重试 |
| Skill 无法访问接口 | 确认 CLAW_API_BASE_URL 是云端可达地址；重启后端后新建 Session；不要写 localhost |
| 自助 API 401 | 使用 Bot API Token，而非微信 Token、MA Key 或管理员 cookie |
| 文件一直审核 | 查看上传/审核状态和类型限制；应用会在超时或拒绝后停止提交 |
| 定时任务没执行 | 核对时区、next_run_at、Bot 暂停/登录/欠费状态，以及上轮是否仍未结束 |
| 微信未收到定时结果 | 在执行记录区分 MA 失败与微信回传失败；微信登录及 context_token 可能失效 |
| “需人工核对” | 远端可能已接收请求，先核对 MA/微信，勿盲目重新提交以免重复执行/扣费 |
| 启动提示锁占用 | 检查是否已有本项目进程；不要删除锁并强行多开 |

排障时只分享脱敏日志、版本、错误码和最小复现。不要发送 `.env`、数据库或完整请求头。
