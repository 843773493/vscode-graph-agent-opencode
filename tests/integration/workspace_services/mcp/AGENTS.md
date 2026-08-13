# 目录用途

存放 MCP 协议边界的集成测试及测试专用 MCP Server。

# 可修改内容

- 本地 stdio MCP Server 和其调用链路测试。
- MCP 工具发现、调用和会话状态验证。

# 不可修改内容

- 用户真实 MCP 配置或外部 MCP 服务。
- Agent 业务实现和测试运行产物。

# 规范

- 测试专用 Server 只提供确定性、无外部副作用的工具。
- 正式工作区和日志写入 `out/tests/integration/workspace_services/mcp/` 对应测试路径。
