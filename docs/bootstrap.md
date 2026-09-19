# 自动准备百炼资源

此脚本让第一次接入无需手工创建 Environment 和 Agent。你只需先开通 MA、准备有资源创建权限的工作空间 API Key、工作空间 ID 和区域。Key 本身不足以推断工作空间 API 地址。脚本读取项目根目录的 `.env`，不读取 shell 中的同名凭证。

## Linux

在项目根目录执行：

```bash
python3 scripts/setup_env.py
# 编辑 .env：填写 DASHSCOPE_API_KEY、BAILIAN_WORKSPACE_ID、BAILIAN_REGION
bash start.sh install
.venv-linux/bin/python scripts/bootstrap_ma.py
.venv-linux/bin/python scripts/bootstrap_ma.py --apply
bash start.sh start
```

第一条 bootstrap 命令是离线预览；第二条才调用云端创建资源。`--apply` 表示接受预览中的创建范围和工具权限。云端后续使用按账号规则计费。

## Windows

```powershell
python scripts/setup_env.py
# 编辑 .env：填写上述三个字段
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/bootstrap_ma.py
.\.venv\Scripts\python.exe scripts/bootstrap_ma.py --apply
.\start.ps1
```

## 创建内容

- 一个允许出站网络的云端 Environment。
- 一个使用项目微信提示词的 Agent；模型默认 `auto`，可用 `--model 模型ID` 改为当前工作空间支持的模型。预览和执行使用同一组选项。
- 仅显式启用 bash、read、write、edit、glob、grep、web_search、mark_artifacts，策略为 `always_allow`，支持自动执行及交付。可在创建后按业务需要调整审批策略。
- 每个成功的资源 ID 立即保存到 `.env` 的 `DEFAULT_ENVIRONMENT_ID` / `DEFAULT_AGENT_ID`；不会打印 API Key。

不会创建 Session 或执行模型任务；Memory Store 与初始 Session 在微信 Bot 绑定时自动创建。模型、图片理解、Python 运行时和云端权限仍需按首次验收清单实测。

接口依据：[创建 Agent](https://docs.agent.bailian.aliyun.com/zh/api/managed-agents/agent/create)、[创建 Environment](https://docs.agent.bailian.aliyun.com/zh/api/managed-agents/environment/create)。平台能力与模型目录可能变化，400 时检查当前控制台支持的模型/工具，使用 `--model` 修正后重试。

## 复用与失败恢复

已有 DEFAULT ID 时，会读取验证并复用，不修改原有配置。`--model`、`--name`、Skill 参数只作用于新建资源。不要用这个脚本更新已有 Agent；在控制台调整并新开 Session。

脚本在忽略提交的 `data/bootstrap-ma.json` 保存工作空间、安装标识和创建状态；单实例锁在 `data/bootstrap-ma.lock`。同一份状态重复运行不会重复创建。请保留 `.env` 和此状态文件。

- 明确的 400/401/403 等拒绝：修正配置或权限后重跑。
- 超时、5xx 或返回缺少 ID：保留状态；重跑会通过安装标识查找云端资源。如果仍未找到，脚本停止而不再次创建。到控制台核对后，将已创建资源的 ID 填进 `.env` 再重跑；确认根本未创建时，才手动清除状态中对应的 pending 项。
- 环境同名冲突：填入要复用的 Environment ID，或在核实未创建后使用 `--name` 指定另一个名称。
- 进程被强制结束留下锁：确认没有初始化进程运行，再移除 `data/bootstrap-ma.lock`。
- 切换工作空间：使用新的项目配置目录；不跨工作空间复用状态或资源 ID。

## 可选 Skill

基础聊天无需 Skill。自然语言查额度、重开会话和定时任务需先按 [材料清单](prerequisites.md#4-准备-agent-skill) 打包上传并通过审核。初始化新 Agent 时可直接挂载：

```bash
.venv-linux/bin/python scripts/bootstrap_ma.py --skill-id skill_你的ID --skill-version 你的具体版本
.venv-linux/bin/python scripts/bootstrap_ma.py --skill-id skill_你的ID --skill-version 你的具体版本 --apply
```

脚本不自动上传 Skill、不自动修改已有 Agent。已创建的 Agent 可在控制台挂载；同时设置云端可达的 `CLAW_API_BASE_URL`，新建 Session 后生效。后台费率需要部署者单独填写，不根据所选模型猜测价格。
