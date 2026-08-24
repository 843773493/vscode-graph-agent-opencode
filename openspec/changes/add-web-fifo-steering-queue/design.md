## Context

当前 Job 队列把 `steering` 和 `queued` 当成两条优先级通道：steering 会插入普通消息之前，连续 steering 会被合并，另外还有 reorder 和 send-immediately 控制。Web 客户端也围绕这些语义提供拖拽、分组和立即发送操作。新的行为契约见 `specs/session-fifo-message-delivery/spec.md`。

本次改动只覆盖当前维护的 FastAPI 工作区后端和 `src/clients/web`。项目仍处于原型阶段，不建立旧 pending 语义的兼容层；会话历史本身不能因为队列重构而被删除。

## Goals / Non-Goals

**Goals:**

- 用单一的、每会话严格有序的队列取代 steering 优先队列和 Job 合并。
- 将投递时机建模为队列项属性，并让 turn、tool-result、interrupt 边界通过统一调度入口竞争队首。
- 保证入队、策略修改、撤回和边界消费之间的线性化与可诊断性；不为已取出的消息做执行恢复。
- 让 Web 客户端只显示和修改投递策略，不再暴露会改变顺序的交互。
- 保持后端状态为唯一权威，并为多个 Web 客户端提供版本化队列快照。

**Non-Goals:**

- 不在本 change 中设计 checkpoint、rollout、segment、fork 或 compaction 存储。
- 不把队列做成跨会话的全局 FIFO；每个会话独立排序和消费。
- 不新增消息聚合、自动摘要或多条消息合并策略。
- 不让修改 `after_interrupt` 自动中断当前执行；中断仍由已有的中断控制路径产生。
- 不迁移旧会话中已经持久化的 steering/queued pending 数据，不建立双格式读写兼容层。

## Decisions

### 1. 用不可变入队序号和投递策略替代 pending kind

每个 pending request 保留一个服务端分配的 `enqueue_sequence`，并增加 `delivery_policy`：

- `after_turn`：当前 turn 到达终止边界后释放。
- `after_tool_result`：完整 tool-result 已提交、下一次模型请求开始前释放；当前 turn 没有产生 tool-result 时，使用 turn 结束作为安全回退。
- `after_interrupt`：当前执行在 interrupt 请求后达到已提交的 interrupt 边界后释放；选择该策略不会自行发送 interrupt。

当前显示用的 `position` 仍可作为从零开始的相对位置，但不能作为排序依据。快照同时返回不可变的 `enqueue_sequence`，便于事件去重和诊断。队列快照只包含仍在等待的 `queued` 项；消息一旦被调度器取出，就从队列和待处理快照中消失，不再维护 `delivering`、`delivered` 或 `cancelled` 状态。

替代方案是继续保留 `kind`，把 `steering` 改名解释为策略。该方案会把优先级和边界语义继续混在一个字段里，容易让 API 和 UI 重新出现插队行为，因此不采用。

### 2. 用会话级调度闸门线性化所有队列变化

在现有 JobService 的会话级锁/控制路径上统一处理以下操作：入队、策略更新、撤回、边界通知和队首取出。任何操作都先取得会话闸门，再检查当前活动执行和队首状态，最后一次性提交队列快照版本。

边界通知不直接操作 deque，也不由 Web 客户端推断是否可以投递。它只向会话调度器提交 `turn_end`、`tool_result` 或 `interrupt` 信号；调度器检查队首策略，最多取出一个队列项。若队首策略尚未满足，后续消息即使策略满足也必须等待。

替代方案是让每种边界各自维护一个优先队列。这样可以减少局部代码改动，但会重新产生跨策略插队，无法证明严格 FIFO，因此不采用。

### 3. 保留单条 Job 身份，移除 group/merge

每条用户消息继续拥有独立的 message/job identity。调度器每次只从队首取出一条消息，然后启动独立 Job；取出即表示消息已离开队列，不建立 delivery attempt、确认或失败重试。Agent 执行层如果支持在 tool-result 边界继续当前执行，可以把这条消息作为一次独立的边界输入；如果当前运行时只能在 turn 之间接收输入，则在该边界关闭当前执行并启动下一条独立 Job。两种执行方式都必须保持单消息身份，不能将多个消息拼接成一个 prompt 或一个 `job_merged` 事件。

因此，JobService 中的 `peek_next_group`、`pop_next_group`、连续 steering 合并和合并取消原因都应被单条 `peek_head`/`take_head` 路径替代。队首取出后立即离开队列，下一条消息才成为新的队列头。

### 4. 以持久化 pending store 保存等待中的队列

不新增 SQLite 或其他外部依赖。沿用现有 pending request store 的持久化抽象，调整记录结构以保存仍在等待的完整消息、附件、`enqueue_sequence`、`delivery_policy` 和队列快照版本。每个会话的序号分配和 pending 状态变更必须在同一个持久化临界区内完成。

入队接口只有在消息和序号落盘后才返回成功；事件在提交成功后发布。服务初始化时可以读取仍在文件中的 queued 项并按 `enqueue_sequence` 装入队列。队首一旦被取出，就先从待处理快照移除再启动 Job；取出后的消息不写入恢复状态，进程在此之后崩溃时允许丢失，不进行重试或重复投递。

旧的 pending kind 数据不做兼容解析；原型环境可在启用新实现前清理旧 pending 状态，但不得删除 messages、rollout 或其他会话历史。

### 5. 收敛 HTTP API 和事件模型

消息发送请求直接携带 `delivery_policy`，默认策略由 Web composer 的明确配置提供；即使当前会话空闲，消息也先形成队列记录。待处理查询返回 `delivery_policy`、`enqueue_sequence`、相对位置、状态、等待原因和队列快照版本。

保留待处理消息的内容编辑和撤回能力，但它们只能修改内容或移除一个队列项。新增一个只修改投递策略的接口，或将该字段纳入现有待处理更新接口时必须执行同一会话闸门校验；策略更新不能接受位置字段。

删除 reorder、send-immediately 和基于 pending kind 的接口/字段，不保留后端隐式兼容行为。事件可以继续沿用现有 pending 事件通道，但 payload 必须包含单条 message ID 和快照版本；消息取出后使用既有 Job 生命周期事件，不新增 delivery 状态机事件。

### 6. Web 队列改为固定顺序行和策略按钮

`ChatPanel` 及其下游类型不再维护拖拽状态、steering/queued 分组或立即发送回调。Composer 上方的队列摘要区域显示序号、消息摘要、队首标识和等待原因；仍在队列中的用户消息保留三个策略按钮，并使用统一文案和 `title`/可访问 tooltip：

- “本轮结束后投递”
- “工具结果后投递”
- “中断边界后投递”

按钮点击只调用策略更新 API。请求成功后用完整响应替换本地 pending 快照；失败时显示错误并重新拉取快照。消息取出后不再出现在队列控制区，也没有可修改的 delivery 状态。

Composer 的默认投递策略只影响新消息入队请求，不改变任何已入队消息的位置；默认值和当前选择必须通过按钮的悬停/聚焦提示解释。界面不再渲染“引导消息”“排队消息”两组，也不渲染拖拽手柄或“立即发送”。

### 7. 用测试覆盖顺序和边界竞态

后端测试重点覆盖入队序号、尾部策略修改、三类边界、tool call/result 原子性、取出即移除、拒绝重排/提升、撤回和两个客户端的快照版本竞态。Web 测试覆盖三种按钮 tooltip、无拖拽/立即发送入口、策略更新后的服务端快照替换、活动 Job 不显示为待处理队列项以及队列摘要位于 Composer 上方。

## Risks / Trade-offs

- [旧 pending 数据无法直接使用] → 原型阶段不做兼容层；发布新实现前清理旧 pending 状态，并在启动时对残留旧字段明确报错。会话历史文件不参与清理。
- [tool-result 边界注入依赖执行器能力] → 将边界释放和执行输入分成两个接口；执行器可在当前运行中继续，也可在安全边界创建下一条独立 Job，但队列身份和单条消费契约不变。
- [after_interrupt 可能长时间等待] → 快照显示明确等待原因；策略变更不伪造中断。已有 interrupt 控制成功到达边界后，调度器立即重新检查队首。
- [多个 Web 客户端产生旧快照覆盖] → 每次变更携带单调快照版本，前端只接受不旧于当前状态的完整快照，失败时主动重新获取。
- [单会话闸门降低极端并发吞吐] → 闸门只保护短暂的队列状态和持久化提交，不持有模型调用或工具执行；20 个以上会话可以并行，各会话互不阻塞。

## Migration Plan

1. 先替换后端队列模型、API schema、持久化记录和边界调度测试。
2. 再切换 Job 调度，删除 steering 合并、reorder 和 immediate 分支，确保旧字段不会被静默解释。
3. 更新 Web 类型、API 封装、composer 默认策略和 ChatPanel 行式控件，完成端到端测试后启用。
4. 原型环境启用新实现前清理旧 pending 队列元数据；不迁移既有 session history。

回滚以代码版本回退为边界，不在运行时同时读写两套队列格式。若新实现已经写入新格式，回退前必须保留会话历史，并明确清理或重新初始化未消费 pending 状态。
