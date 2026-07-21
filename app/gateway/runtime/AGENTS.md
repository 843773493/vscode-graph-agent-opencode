# 目录用途

`app/gateway/runtime/` 存放 Gateway 所拥有的本地服务进程、工作区运行时和生命周期控制。

# 可修改内容

- 可以维护进程组、命名服务 handle、本地工作区启动、健康检查和运行时控制器。

# 不可修改内容

- 不实现 Job、Agent、会话或工具执行业务规则。
- 不终止 Gateway 无法证明所有权的外部进程。

# 规范

- Workspace API、Terminal、Browser 和 SSH tunnel 必须使用明确名称管理。
- 关闭过程先优雅终止，超时后终止完整进程组，并暴露清理错误。
- 后端重启不得无必要地重启 Terminal 或 Browser。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
