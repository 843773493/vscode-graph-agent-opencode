# 目录用途

存放 MCP stdio 到 Streamable HTTP 桥接插件的唯一源码和清单；Gateway 插件运行时托管其发行包。

# 可修改内容

- 可以修改 `bridge.py` 的正式插件入口和 MCP 协议桥接实现。
- 可以维护该插件独立的配置 schema、`pyproject.toml`、`uv.lock` 和必要的包元数据。

# 不可修改内容

- 不在 Backend 启动 MCP stdio Server；stdio 子进程只由 Gateway 插件实例拥有。
- 不在 `app/services/infrastructure/mcp/` 复制 bridge 实现；Backend 只经 Gateway 的 Streamable HTTP 服务连接。
- 不复制官方 MCP SDK 的 JSON-RPC 类型、Streamable HTTP transport 或 stdio 编解码实现。
- 不保存跨重启的 MCP 运行会话、消息正文或凭据。

# 规范

- 发布阶段使用锁定依赖准备插件 ZIP，并把发行 Python 所需包放入包内 `python/site-packages`；安装阶段只校验，不准备依赖。
- 插件入口通过 `main()` 启动本地 HTTP 服务，端口和配置只从 Gateway 注入环境读取。
- 使用项目 MCP bridge 和官方 SDK transport；不支持的协议能力必须显式报错。
- 原始 MCP 子进程随插件实例关闭并等待实际退出。
