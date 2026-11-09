# 目录用途

`plugin_bridge/` 提供 Gateway 自有 MCP 插件使用的 stdio 到 Streamable HTTP 协议桥。

# 可修改内容

- 使用 MCP 官方 SDK 的 StdioServerParameters、SessionMessage 与 StreamableHTTPServerTransport。
- 复用插件既有 HTTP 生命周期和认证管理接口。

# 不可修改内容

- 不管理 MCP 目录、Backend 配置候选、连接资格或工具执行记录。
- 不拼装 SSE、JSON-RPC 编码或另建 MCP 工具目录。

# 规范

- stdio 子进程归当前插件实例持有，插件正常关闭时等待实际子进程退出。
- 每个工作区仅支持一个当前 Backend client；替换 session 时拒绝旧请求和迟到响应，不把旧结果投给新 session。
- 不支持的协议能力显式拒绝，不伪称支持。
