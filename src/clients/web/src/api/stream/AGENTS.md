# 目录用途

`src/clients/web/src/api/stream/` 存放浏览器前端的实时流（SSE）与消息流快照 API 客户端：Turn 消息流订阅、会话 Trace 历史与事件流、工作区文件变更监听流，以及消息流快照的严格 DTO 校验。

与相邻文件的边界：

- `api/http.ts`（上级目录）：统一请求屏障（Gateway 凭据、用户会话、重试、超时）与 JSON 解包。本包所有 HTTP 入口都基于它，不自带传输层。`sessionTraceStream` 走 `requestJson`，SSE 入口走 `requestGatewayResponse`。
- `api/session/`（上级目录）：会话域一次性 API（列表、消息、目标、资源、目录、上下文、Turn 历史分页），是请求-响应语义，不含 SSE 订阅。
- `messageStreamSnapshot.ts`：本包内唯一负责**严格 DTO 校验**的模块——对快照帧逐字段校验（未知字段、非负整数、布尔、必填数组），与 `api/http.ts` 的统一请求屏障职责不同，不得把校验逻辑上移到 http。
- `sessionMessageStream.ts`：依赖同目录 `messageStreamSnapshot` 做事件/快照校验；此为本包唯一的包内依赖边。
- `types/`、`sseClient`、`sseRuntimeSchemas`：协议类型与 SSE 运行时原语的权威来源，本包只消费。

# 可修改内容

- SSE 事件订阅的通道建立、帧解析、游标（`Last-Event-ID`）处理与专用游标失效错误。
- 消息流快照与事件的严格 DTO 校验规则。
- 工作区文件变更批次的校验与转发。
- 本包内部的模块拆分与符号导出调整。

# 不可修改内容

- 不维护 React 展示状态或会话业务状态；本包只做订阅、解析与转换。
- 不把统一请求屏障/凭据重试逻辑复制进本包；传输能力只来自 `api/http.ts`。
- 不放宽 `messageStreamSnapshot` 的严格校验；协议形状以公共投影为准。
- 不定义后端协议类型的权威来源；协议类型只能来自 `src/clients/web/src/types/`。

# 规范

- 请求失败与协议错误必须透明抛出，不得静默降级；410 游标失效映射为专用错误类型。
- 导入上级共享模块使用显式相对路径（如 `../http`、`../../types/backend`），包内互相引用使用同目录 `./`，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
