# 目录用途

存放由 Gateway 插件运行时托管的 MCP stdio 到 Streamable HTTP 桥接插件源码。

# 可修改内容

- 可以修改 `bridge.py` 的正式插件入口和 MCP 协议桥接实现。
- 可以维护该插件独立的配置 schema 和必要的包元数据。

# 不可修改内容

- 不在 Backend 启动 MCP stdio Server；stdio 子进程只由 Gateway 插件实例拥有。
- 不复制官方 MCP SDK 的 JSON-RPC 类型、Streamable HTTP transport 或 stdio 编解码实现。
- 不保存跨重启的 MCP 运行会话、消息正文或凭据。

# 规范

- 插件入口通过 `main()` 启动本地 HTTP 服务，端口和配置只从 Gateway 注入环境读取。
- 使用项目 MCP bridge 和官方 SDK transport；不支持的协议能力必须显式报错。
- 原始 MCP 子进程随插件实例关闭并等待实际退出。
