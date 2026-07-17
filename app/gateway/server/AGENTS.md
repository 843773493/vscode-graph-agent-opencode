# 目录用途

`app/gateway/server/` 存放 Gateway 服务启动装配与透明代理路由实现。

# 可修改内容

- Gateway 注册表和托管运行时的启动恢复流程
- 工作区 API 的 HTTP/SSE 透明代理路由
- 仅服务于 Gateway 入口装配的辅助函数

# 不可修改内容

- 不实现 Agent、会话或工具业务逻辑
- 不直接读写工作区 `.boxteam/` 业务数据
- 不在此目录维护浏览器前端展示状态

# 规范

- 启动失败和代理失败必须返回或抛出明确错误
- SSH 与本地工作区的持久化权威来源仍由 Gateway registry 管理
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
