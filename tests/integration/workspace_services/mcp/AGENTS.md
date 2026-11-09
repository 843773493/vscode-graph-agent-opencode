# 目录用途

存放 MCP 协议边界的集成测试及测试专用 MCP Server。

# 可修改内容

- 本地 stdio MCP Server 和其调用链路测试。
- MCP 工具发现、调用和会话状态验证。
- `oauth_server.py` 为标准 OAuth 的真实 HTTP 资产，MCP handler 复用 schema_server；`test_oauth.py` 验证 SDK 的发现、PKCE、state、token交换、冷刷新与凭据日志边界，不冒充产品 connect／UI／持久等待验收。Backend 初次启动与兄弟目录的产品证据归 `backend/sessions/test_mcp_oauth_startup.py`。
- `test_connection_operations.py` 从真实连接操作 service 产生授权，验证 deadline、配置变更 fencing 与断开清理；受保护 HTTP API、重复 callback／intent 与 OS 退出后的持久对账归 `backend/sessions/test_mcp_connection_operations.py`，不代替 Gateway／Agent／Web 门。
- `schema_server.py` 与 `test_full_schema_invocation.py` 通过正式 stdio 插件桥、真实 HTTP SDK 和固定信封，核对完整 schema、版本变化及服务端原参数收据，并覆盖不同 caller task 的启动、刷新、调用和关闭。SDK session 的同 task 生命周期由产品 owner 维持；Backend 连接账本不再拥有 stdio 进程。真实插件桥、Gateway 路由、Agent 结果及 Backend 重启由 `gateway/test_mcp_plugin_lifecycle.py` 验证，不把协议测试称为完整产品验收。
- `test_bridge_catalog.py` 在相同正式插件桥中验证分页与服务端变化通知，原目录 owner 读取全部页面后发布新 revision；资产只改变服务端定义，不替产品发布目录。
- `test_windows_process_owner.py` 在原生 Windows 观察真实 CreateProcess suspended→Job→resume 与原句柄 wait、正常／非合作退出和冷账本；命令启动与公开 SDK 串行对照，显式 cmd fixture 只覆盖进程所有权边界，不代表 MCP 包安装行为。不启动共享 Web/Gateway 服务。
- `test_invocation_lifecycle.py` 复用同一 Server handler，经正式 stdio 桥或原生 HTTP 核对原请求取消、进度、同连接后继调用与断线；取消收据仅证明 Server 收到通知，不推断外部副作用回滚。stdio 资产始终由插件桥创建和关闭，不恢复 Backend 启动分支。
- `test_provider_discovery.py` 使用既有真实 Provider 和受控 schema Server，经 Gateway／Session／Agent 自主发现、两轮调用及冷检查点核对完整参数与请求 hash；分别保留有算法说明的定义 token 估算、实际用量与 cached tokens，不保证命中。它不使用模型回放，缺少凭据须明确报未满足前置条件；MCP 仍是受控资产，不能称为完整外部依赖 E2E。

# 不可修改内容

- 用户真实 MCP 配置或外部 MCP 服务。
- Agent 业务实现和测试运行产物。

# 规范

- 测试专用 Server 只提供确定性、无外部副作用的工具。
- 正式工作区和日志写入 `out/tests/integration/workspace_services/mcp/` 对应测试路径。
