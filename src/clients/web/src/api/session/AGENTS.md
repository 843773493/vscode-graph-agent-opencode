# 目录用途

`src/clients/web/src/api/session/` 存放浏览器前端的会话域 API 客户端：会话列表与子线程、会话消息、会话目标、会话资源、会话活动（SSE）、会话目录树、会话上下文读取，以及 Turn 历史分页装载。

与相邻文件的边界：

- `api/http.ts`（上级目录）：共享 HTTP 传输基础设施（`requestJson`、`unwrapApiData`、`workspaceHeader`、`HttpRequestError`）。本包所有模块都基于它发起请求，不再自带传输层。
- `api/sessionMessageStream.ts`、`api/sessionTraceStream.ts`、`api/messageStreamSnapshot.ts`、`api/workspaceFileEvents.ts`（上级目录，属 stream 组）：SSE 实时流与快照校验，与本包的会话快照/分页语义不同，不得互相内联复制。
- `types/`：后端协议类型的权威来源，本包只消费不定义。

# 可修改内容

- 会话域各 API 客户端的请求构造、响应解包与专用错误类型。
- 会话目录、Turn 历史分页游标等面向会话的客户端契约。
- 本包内部的模块拆分与符号导出调整。

# 不可修改内容

- 不维护 React 展示状态或会话业务状态；本包只做请求与响应转换。
- 不在客户端推导后端协议中不存在的默认业务数据。
- 不把共享 HTTP 传输逻辑复制进本包；传输能力只来自 `api/http.ts`。
- 专用 HTTP 错误定义在其所属业务客户端中，不上移到共享层。

# 规范

- 业务 API 按语义模块组织，根级 `api.ts` 只重导出迁移后的公开符号。
- 请求失败必须透明抛出，不得静默降级；调用方以 `HttpRequestError` 区分的语义保持不变。
- 导入上级共享模块使用显式相对路径（如 `../http`），禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
