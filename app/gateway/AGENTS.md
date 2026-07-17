# 目录用途

`app/gateway/` 存放独立的本地 Gateway 服务。Gateway 负责管理多个工作区目标，并把浏览器前端的 API 请求转发到当前激活的工作区后端。

# 可修改内容

- Gateway FastAPI 入口
- 工作区目标注册、激活状态、完整重连来源信息的持久化和状态检查
- 本机后端进程与 SSH 隧道进程管理
- HTTP/SSE 代理逻辑

# 不可修改内容

- 不要在 Gateway 中实现 Agent 业务逻辑。
- 不要把具体会话、消息、工具执行规则复制到 Gateway。
- 不要让 Gateway 直接读写被代理工作区的 `.boxteam` 业务数据。
- 不要把 Gateway 注册表、激活状态、SSH 隧道日志或 Web 展示状态存入默认工作区。

# 规范

- Gateway 只做路由、目标生命周期和透明转发。
- Gateway 自有持久化数据统一位于 `${BOXTEAM_HOME:-~/.boxteams}/state/gateway/`。
- `workspaces.json` 必须保存重启后重建目标所需的来源信息；SSH 工作区须保存主机、端口、用户、远端路径和私钥路径等重连参数，临时 `backend_url` 不能作为重连权威来源。
- 注册表写入必须原子替换并限制为当前用户可读写；私钥文件内容不得复制进注册表。
- 恢复目标失败时应保留明确的 `connection_error` 供 API 和前端观察，不得伪装为已连接。
- 失败时直接抛出明确错误，不要静默切换到其它工作区。
- 新增子目录必须补充自己的 `AGENTS.md`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
