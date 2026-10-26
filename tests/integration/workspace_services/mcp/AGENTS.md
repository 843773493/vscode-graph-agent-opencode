# 目录用途

存放 MCP 协议边界的集成测试及测试专用 MCP Server。

# 可修改内容

- 本地 stdio MCP Server 和其调用链路测试。
- MCP 工具发现、调用和会话状态验证。
- `schema_server.py` 与 `test_full_schema_invocation.py` 通过真实 SDK/stdio owner 和固定信封，核对完整 schema、版本变化及服务端原参数收据，并覆盖不同 caller task 的启动、刷新、调用和关闭。SDK session 的同 task 生命周期由产品 owner 维持，夹具不替产品维持此约束；不把本项称为完整 Agent/Web 授权验收。
- `test_invocation_lifecycle.py` 复用同一 Server handler，通过真实 stdio／HTTP 核对原请求取消、进度、同连接后继调用与断线；取消收据仅证明 Server 收到通知，不推断外部副作用回滚。Gateway／Session／冷恢复产品证据位于 backend/agents 的结果链测试。

# 不可修改内容

- 用户真实 MCP 配置或外部 MCP 服务。
- Agent 业务实现和测试运行产物。

# 规范

- 测试专用 Server 只提供确定性、无外部副作用的工具。
- 正式工作区和日志写入 `out/tests/integration/workspace_services/mcp/` 对应测试路径。
