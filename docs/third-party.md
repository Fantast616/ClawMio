# 第三方组件与资料

项目自身许可不替代第三方软件许可、云服务条款或微信使用规则。依赖版本见 requirements.txt；分发时保留相应包的许可与声明，不对第三方商标主张所有权。

直接运行依赖包括 FastAPI、Uvicorn、HTTPX、python-dotenv、qrcode、Pillow、wechatbot-sdk、tzdata。测试依赖为 pytest 与 pytest-asyncio。它们通过 Python 包安装流程取得，仓库不内嵌这些依赖的源码。

接口实现参考：

- [微信 iLink Bot 协议](https://www.wechatbot.dev/zh/protocol)
- [微信 Python SDK 文档](https://www.wechatbot.dev/zh/python)
- [百炼 Session 创建](https://docs.agent.bailian.aliyun.com/zh/api/managed-agents/session/create)
- [百炼 Event 历史](https://docs.agent.bailian.aliyun.com/zh/api/managed-agents/session/list-events)

本地 docs/contracts 下可能存在开发时获取的外部 OpenAPI 缓存，它们被 Git 忽略，不是本项目维护的规范，也未包含在本项目许可证授权中。查询接口以官方现行文档和实际返回为准。
