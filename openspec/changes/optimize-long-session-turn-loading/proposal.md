## Why

长历史会话在切换时仍会恢复完整 Trace、重算消息分组并同步解析大型 Markdown，导致 Composer 与历史时间线争用主线程，用户必须等待历史加载后才能稳定输入。当前基于单条消息和 checkpoint 的分页也无法保证一个 Job 完整呈现，且普通追加、压缩或终态更新可能使已加载历史失效。

## What Changes

- 新增以执行 Job 为权威边界的 Turn 读取模型和稳定游标，分页永远返回完整 Turn。
- 新增有严格大小上限的会话 bootstrap，优先返回最新 Turn 骨架、活动 Job 状态和增量事件游标，不读取完整 checkpoint 或完整 Trace。
- 将 Composer 的草稿、输入和发送状态从完整会话时间线状态中解耦，使切换长会话和流式更新期间仍可立即输入。
- 将最新 Turn 的完整详情、历史 Turn 和 Trace 改为分层、按需加载，并对大型 Markdown 采用非阻塞的渐进渲染。
- 使用 `turn_id + revision` 合并 bootstrap、分页、终态协调和 SSE 更新，保留已加载的旧历史并处理竞态。
- 将编辑重发、重新生成和失败重试视为破坏性历史重排：同步截断模型 checkpoint 与 Turn 展示投影，递增投影 epoch，并确保后台旧 staging 不能覆盖已发布的新历史。
- 破坏性重排只通过有界 Turn header 定位和隐藏后缀，完整 detail 使用流式复制，避免编辑长历史时批量解析工具 Trace 与大型正文。
- 增加覆盖长历史、大型 Markdown、完整 Turn 分页、稳定游标、SSE 竞态和上下文压缩的后端及真实浏览器 E2E。
- **BREAKING**：浏览器聊天时间线的权威分页协议从 message 列表迁移为 Turn 列表；旧 message 查询保留给兼容的单消息读取和内部诊断，不再作为主时间线加载协议。

## Capabilities

### New Capabilities

- `session-turn-history`: 定义 Job/Turn 投影、bootstrap、稳定游标、详情水合、增量事件合并和破坏性重排语义。
- `responsive-session-composer`: 定义切换长会话时 Composer 的独立生命周期、同步草稿恢复、发送可用性和竞态处理。
- `progressive-turn-rendering`: 定义最新 Turn 优先、可视区域详情加载、Markdown 渐进渲染、虚拟列表分页和性能验收行为。

### Modified Capabilities

无。

## Impact

- 后端：会话、Job、Trace、消息历史基础设施与 `/api/v1/sessions/*` 路由。
- 前端：全局状态分层、会话切换、SSE 恢复、Turn 时间线、Composer、Markdown 和虚拟滚动组件。
- 存储：在每个会话节点内增加可增量恢复和压实的 Turn 投影，不改变 checkpoint 作为模型上下文权威来源的职责。
- 测试：新增后端协议测试、投影恢复测试、破坏性 replay/staging 并发测试和使用隔离工作区的真实浏览器长会话 E2E。
