# 目录用途

`src/clients/web/src/state/timeline/` 放浏览器前端聊天时间线族的纯派生逻辑：把权威会话数据（用户消息、Trace 事件、Turn response parts）投影成统一的 `TimelineItem` 展示序列，供 `components/chat/` 渲染。

与相邻子包的边界：

- `state/session/`：会话自身的树结构、作用域键与游标分页（`turnTimeline`/`turnVirtualization`），产出的是会话与 Turn 的组织信息，不产出聊天条目。
- `state/messageStream/`：Turn SSE 的实时实体状态与快照 hydration。其 `responseProjection` 负责把实时流状态转成 `TurnResponsePart` 协议片段，是"实时流到协议 parts"的一环。
- `state/trace/`：Trace 事件的配对、失败补全与聚合 helper，本包通过 `trace/traceAggregation` 消费其聚合结果。
- 本包 `responseParts` 是"响应片段展示投影"：它把已经是权威协议的 `TurnResponsePart`（无论来自实时流还是持久化历史）单向投影成 `TimelineItem`。它不产生也不修改协议 parts，与 `messageStream/responseProjection` 的分工是"消费协议 vs 生成协议"，两者不得互相合并或互设别名。

## 可修改内容

- `TimelineItem` 联合类型及其时间线项结构定义。
- 用户消息归一、Trace 事件时间线聚合入口与 response parts 展示投影的纯函数。
- 本包内部的模块拆分与符号导出调整。

## 不可修改内容

- 不新增第二套手写 DTO 或复制既有协议派生类型；类型只能来自 `src/clients/web/src/types/`。
- 不放 React 组件，不发起 HTTP 请求、读取 DOM 或处理副作用。
- 不生成或回写权威协议数据（`TurnResponsePart`、Trace 事件、快照状态）。

## 规范

- 纯函数优先，输入输出显式，非法输入必须抛出明确错误或按既定边界收敛。
- `TimelineItem` 是跨展示组件的共享契约，新增字段必须同步 `components/chat/` 的消费点与测试。
- 展示投影与实时流投影保持单一分工，不得在两条链路内联复制对方的归一逻辑。
- 代码注释使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”
