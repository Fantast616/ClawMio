# 贡献指南

欢迎提交问题、文档修正和功能改进。先阅读 [架构](docs/architecture.md) 与 [开发约定](AGENTS.md)，使用自己的分支或 fork 提交 PR。

## 开发准备

```bash
python -m venv .venv
# 激活虚拟环境后
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --check static/app.js
bash -n start.sh
```

测试不需要真实云账号或 `.env`。手工测试真实微信/MA 时使用自己的测试 Bot，记录调用范围和产生的资源，不把它们当成可公共复用的演示账号。

## 提交前

- 说明具体问题、修改后的行为和验证方式，避免把多个不相关功能混在同一 PR。
- 鉴权、计费、任务恢复、重复请求等行为变化应有对应测试。
- 接口或环境变量变化同步文档、示例与 Agent Skill。
- 修改数据库采用增量迁移，保留旧数据；说明降级限制。
- 执行 `python scripts/check_repository.py --staged`，确认没有凭证、数据库或用户内容。
- 不提交 `artifacts/` 或 Skill ZIP；发布者从源码运行打包脚本生成。

CI 检查模拟协议/状态机，不证明所有模型、微信账号、部署环境均可用。报告真实验证时注明平台、Python 版本及验证范围，但不要泄露资源内容或密钥。

## 问题报告

提供预期行为、实际行为、复现步骤、部署方式、Python 版本和脱敏错误码。不要上传 `.env`、数据库、完整 HTTP 请求头或私人微信内容。安全问题按 [SECURITY.md](SECURITY.md) 私下报告。
