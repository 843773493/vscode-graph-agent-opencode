# 目录用途

`src/clients/web/src/api/stream/` 存放浏览器前端的实时事件传输与 SSE 解析与消息流快照 API 客户端：Turn 消息流订阅、会话 Trace 历史与事件流、工作区文件变更监听流，以及消息流快照的严格 DTO 校验。

`federatedSearch.ts` 消费 Gateway 的请求级搜索流，复用共享 SSE 与生成校验器，核对范围、事件顺序、领域归属及唯一终态。跨目标并发、配额、独立 cursor 和错误均由 Gateway 调度，本模块不预检工作区、不另建 fan-out、不自动重试分支。一个总期限覆盖建连与正文消费，取消回收原 reader。

流正常结束但缺少最终汇总时直接抛出 `IncompleteSearchStreamError`，携带机器代码及原响应头的 request_id；即使尚无范围事件也保留请求归属，不把取消、超时或 HTTP 拒绝改标为该错误。

真实响应正文读取失败使用 `SearchStreamReadError`，保留响应头 request_id、机器代码及原异常 cause；只有 reader 的拒绝进入该出口，解码、领域校验、取消和总期限保持原错误。

与相邻文件的边界：

- `api/http.ts`（上级目录）：统一请求屏障（Gateway 本机凭据、重试、超时）与 JSON 解包。本包所有 HTTP 入口都基于它，不自带传输层。历史查询走 `requestJson`，常驻事件统一走 `realtime.ts`，有限请求流走 `requestGatewayResponse`。
- `api/session/`（上级目录）：会话域 API。大多数模块提供一次性请求-响应操作；`sessionActivity.ts` 按会话领域拥有活动流 endpoint，通过 `realtime.ts` 消费原 SSE。本包负责共享连接及消息、Trace、工作区文件流客户端，不重复实现会话活动 endpoint。
- `messageStreamSnapshot.ts`：本包内唯一负责**严格 DTO 校验**的模块——对快照帧逐字段校验（未知字段、非负整数、布尔、必填数组），与 `api/http.ts` 的统一请求屏障职责不同，不得把校验逻辑上移到 http。
- 包内依赖：`sessionMessageStream.ts` 使用 `messageStreamSnapshot.ts` 校验事件和快照；`sessionTraceStream.ts`、`workspaceFileEvents.ts` 使用 `realtime.ts` 共享 Gateway WebSocket。测试可以跨这些客户端做集成断言。
- `types/`、`sseClient`、`sseRuntimeSchemas`：协议类型与 SSE 运行时原语的权威来源，本包只消费。

# 可修改内容

- SSE 事件订阅的通道建立、帧解析、游标（`Last-Event-ID`）处理与专用游标失效错误。
- 消息流快照与事件的严格 DTO 校验规则。
- 工作区文件变更批次的校验与转发。
- 本包内部的模块拆分与符号导出调整。

# 不可修改内容

- 不维护 React 展示状态或会话业务状态；本包只做订阅、解析与转换。
- 不复制 `api/http.ts` 的凭据获取／失效机制，也不在业务客户端复制 `src/shared/realtime.js` 的连接管理；`realtime.ts` 负责实时订阅的认证失败处理和有界重连。
- 不放宽 `messageStreamSnapshot` 的严格校验；协议形状以公共投影为准。
- 不定义后端协议类型的权威来源；协议类型只能来自 `src/clients/web/src/types/`。

# 规范

- 请求失败与协议错误必须透明抛出，不得静默降级；410 游标失效映射为专用错误类型。
- 同一旧连接的并发 401 按原凭据 Promise 身份合并刷新；迟到的拒绝不能撤销已经换好的新凭据连接。每次逻辑请求冻结一次请求工厂结果，错误归属不得再调用工厂取得不同路径或游标。
- 同一目标工作区的 Terminal 观察共享一个无游标逻辑 source；视图各自取消，source 统一处理超时／错误，增减非最后一个视图不重建 source。迟到视图只补读自身，Trace、文件等独立游标仍保持各自 source；这些 source 共用页面到入口 Gateway 的物理连接。
- Thread 的留言摘要同样通过页面共享 WebSocket订阅，保留精确 Session／Thread 归属与独立取消；不得每个编辑器另开留言 SSE，避免 HTTP/1.1 同源连接被占满，使普通请求排队超时。空闲心跳必须来自对应逻辑 source。
- 导入上级共享模块使用显式相对路径（如 `../http`、`../../types/backend`），包内互相引用使用同目录 `./`，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

- `realtime.ts` 是业务事件唯一订阅入口，凭据复用 `http.ts`，连接、动态订阅和字节反压复用 `src/shared/realtime.js`；本层负责 SSE 消费与有界重连；重连重新调用请求工厂获取各 owner 最新游标，不能叠加传输重试。
