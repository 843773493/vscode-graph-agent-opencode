# 目录用途

`src/clients/web/src/state/display/` 放浏览器前端的"展示投影"族纯派生逻辑：把后端权威数据（Agent State 原始消息、工具事件、会话资源、事件队列）转换成组件可直接渲染的展示模型与文案，供 `components/` 与 `hooks/` 消费。

与相邻子包的边界：

- `state/timeline/`：聊天时间线族，产出统一的 `TimelineItem` 序列。`display/toolDisplay` 与之相邻但不属于同一契约——它只负责工具项的展示判定与文案（`isRoutineInternalToolItem`、`formatToolCardContent`、`toolCollapsedText`），由 `timeline` 的渲染方在拿到 `TimelineItem` 后调用，不得反向依赖 `timeline/`。
- `state/trace/`：Trace 事件的配对、失败补全与聚合，产出的是事件聚合结果。`display/eventQueueDisplay` 消费已聚合的 Trace 结果做事件队列文案，不参与事件配对。
- `state/messageStream/`：Turn SSE 实时实体状态与快照 hydration。本包不读写流状态，只对上游产出的权威数据做展示投影。
- 本包是"权威数据到展示模型"的单向投影，不产生也不回写协议数据；与 `messageStream/responseProjection`（生成协议 parts）分工不同。

## 可修改内容

- Agent State、工具、资源、事件队列等展示模型与文案映射的纯函数。
- 展示归一分支、标签表和折叠/摘要文案的维护。
- 本包内部的模块拆分与符号导出调整。

## 不可修改内容

- 不新增第二套手写 DTO 或复制既有协议派生类型；类型只能来自 `src/clients/web/src/types/`。
- 不放 React 组件，不发起 HTTP 请求、读取 DOM 或处理副作用。
- 不生成或回写权威协议数据（`TimelineItem`、Trace 事件、快照状态）。

## 规范

- 纯函数优先，输入输出显式，非法输入必须抛出明确错误或按既定边界收敛。
- 展示文案属于用户可见契约，新增或修改文案必须同步对应测试。
- 展示投影与实时流投影、时间线投影保持单向分工，不得互相内联复制归一逻辑，也不得反向依赖 `timeline/`。
- 代码注释使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”
