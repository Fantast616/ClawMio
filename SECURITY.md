# 安全说明

## 报告漏洞

不要在公开 Issue 中发布可直接利用的漏洞细节、凭证或用户内容。仓库发布到 GitHub 后，维护者应开启 Private vulnerability reporting，报告者可从 Security → Report a vulnerability 私下提交；如果未开启，请先通过维护者公开提供的联系方式建立私下渠道。本项目尚未承诺响应 SLA 或特定版本支持周期。

## 部署边界

- 本项目是单管理员应用，不具备多租户权限隔离、支付结算或完整审计平台能力。
- `.env`、SQLite、备份及日志都可能包含敏感内容。Bot Token 为长期有效凭证；SQLite 保留原文用于重复注入，未实现静态加密或自助轮换。
- Bot Token 仅授权所属 Bot 的自助 API，不应暴露给普通网页或写入共享 Agent/Skill。网络传输应使用 HTTPS。
- 登录密码和 Cookie 签名密钥由部署者自行生成，不能保留模板占位符。更换 Cookie 密钥会使现有后台登录失效。
- 使用受信任的 Agent 工具、Skill 与运行环境。该 Agent 可以执行工具，并接触挂载的文件和会话环境变量；工具审批不应因部署方便而被统一绕过。
- 服务暂停/归档不意味着取消云端任务。发生异常时核对远端状态，再决定是否重试。

## 仓库维护

`.gitignore` 与 `scripts/check_repository.py` 提供基本防漏检查，不替代人工检查或托管平台的 Secret scanning。提交前审核 staged diff；如果真实密钥曾发布，应撤销/更换对应凭证，而不只是删除当前文件。
