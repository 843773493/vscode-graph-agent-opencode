## Why

当前待处理消息同时存在普通排队、steering 优先、立即发送和 steering 合并等多套语义，用户无法稳定预测消息的执行顺序，也不适合 Web 界面表达。需要把所有用户消息统一为每个会话一个严格 FIFO 队列，并把“何时投递”从“是否插队”中分离出来。

## What Changes

- **BREAKING** 将会话中的所有用户消息统一进入严格 FIFO 队列；消息入队顺序成为唯一执行顺序，后来的消息不得插队。
- **BREAKING** 将现有 `queued`、`steering`、`immediate` 调度语义改为单条消息的投递策略：turn 结束、tool-result 边界或 interrupt 边界。
- **BREAKING** 取消 steering 优先队列、连续 steering 合并、消息聚合以及按队列重排和“立即发送”操作；消息必须逐条消费。
- 仅允许队列中尚未消费的消息修改自身投递策略；修改策略不得改变队列位置，也不得触发尾部消息提前投递。
- 明确定义 turn、tool-result 和 interrupt 边界，保证不会在 assistant tool call 与对应 tool result 之间插入用户消息。
- 为并发入队、策略修改、当前 turn 结束和 interrupt 建立单会话线性化规则，确保消息不会被接受后遗失或跳过。
- Web 界面展示只读队列序号、消息摘要、当前队首/执行状态和投递策略；使用按钮切换策略，并为按钮提供悬停提示，不提供拖拽排序、提升优先级或立即发送按钮。
- 后端事件和查询结果返回权威队列快照，使多个 Web 客户端在策略修改或消费后保持一致。

## Capabilities

### New Capabilities

- `session-fifo-message-delivery`: 定义会话用户消息的严格 FIFO、单条消费、边界投递策略、并发一致性和恢复语义。

### Modified Capabilities

<!-- 当前 openspec/specs 中没有待处理消息或 steering 的既有能力规格，因此不声明已有 capability 的 delta。 -->

## Impact

- 后端 Job 调度、待处理请求模型、消息发送 API、队列持久化和实时事件需要重构。
- 现有 steering 合并、immediate、reorder 相关接口和前端交互需要移除或改为策略更新接口。
- `src/clients/web` 的消息面板、队列状态镜像和按钮交互需要同步新的字段与事件。
- 需要补充队列顺序、边界投递、并发竞态、取出即移除和 Web 交互测试；崩溃允许丢失已取出的消息，不引入新的外部依赖。
