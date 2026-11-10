# 目录用途

`app/gateway/server/` 存放 Gateway 服务启动装配与透明代理路由实现。

# 可修改内容

- Gateway 注册表和托管运行时的启动恢复流程
- 工作区 API 的 HTTP/SSE 透明代理路由
- 仅服务于 Gateway 入口装配的辅助函数
- `management_routes.py` 是扩展与 MCP 插件服务共用的目标路由解析；沿原登记和路由引用冻结 Gateway／Backend／显式连接，联邦入站不得再次 Relay。MCP 插件安装与运行使用 Gateway 的通用插件包 owner；外部 MCP 仍按工作区连接配置经 Gateway 代理。

# 不可修改内容

- 不实现 Agent、会话或工具业务逻辑
- 不直接读写工作区 `.boxteam/` 业务数据
- 不在此目录维护浏览器前端展示状态

# 规范

- 启动失败和代理失败必须返回或抛出明确错误
- SSH 与本地工作区的持久化权威来源仍由 Gateway registry 管理
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

- 受管 HTTP 进程统一使用 `app.runtime.http_server`；Gateway lifespan 可交付 listener-ready 启动回调及 `before_http_shutdown` 钩子，分别用于开放恢复与关闭前结清受理中的工作区操作，不再维护 Gateway 专用 runner。
