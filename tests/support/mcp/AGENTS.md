# 目录用途

集中存放测试 MCP 配置、真实 HTTP Server 装具、session 协议替身和目录封存前提。

```text
mcp/
├── configuration.py   写入隔离工作区 MCP 配置
├── http_endpoint.py   通过真实 SDK 启动共享 Streamable HTTP schema Server
├── gateway_plugin.py  测试包中的多会话 HTTP MCP 入口，复用 SDK 和相同响应资产
├── bridge_endpoint.py 运行正式 stdio 插件桥，供协议及实际断线验证
├── frozen_catalog.py  隔离工作区及真实 Provider 上下文封存 fixture
├── tool_discovery.py  工具发现的发布目录、结果存储和调用上下文 fixture
└── session_doubles.py MCP session、工厂和 relist 控制替身
```

# 可修改内容

- 测试 MCP 配置文件准备。
- 通过真实 MCP SDK manager 与 `schema_server.py` 启动并清理测试用 HTTP MCP endpoint。
- 协议测试可直连正式插件桥，真实 Gateway 路由及安装由独立产品切片验收；不在 Backend 恢复 stdio 分支。
- 通过 pytest fixture 注入的 MCP session fake 和工厂。
- 真实发布目录、freeze/seal 和发现结果存储的跨层共享前提；数据库与执行准入由 `../rollout/context_registry.py` 提供。

# 不可修改内容

- 不放生产 MCP 实现或测试用例。
- 不在本目录创建替代 MCP Server；外部 MCP 子进程只由 Gateway 托管的正式插件桥接。
- 不复制 MCP wire protocol 或 `schema_server.py` 的 handler。

# 规范

- 配置目标必须由调用测试显式指定，不能写入 fixture 源目录。
- HTTP endpoint 的定义与收据写入调用测试对应的 `TestRunContext` artifacts；生命周期由 async context manager 收束。
- fake 只提供测试明确要求的协议行为，未实现的请求必须显式失败。
- `MutableSchemaSessionFactory` 控制单个工具的 schema 和可用性；`FakeMcpSessionFactory` 控制多个 server 的工具名称。二者不合成带模式开关的通用替身。
