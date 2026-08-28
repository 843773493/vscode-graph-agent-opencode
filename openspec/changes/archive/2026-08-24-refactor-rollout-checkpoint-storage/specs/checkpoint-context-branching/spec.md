## Purpose

为上下文压缩、rewind、continue/resume、编辑重放和历史 fork 提供基于 SQLite context view 的一致分支语义，使子会话独立运行并可安全管理旧上下文。

## ADDED Requirements

### Requirement: Compaction 创建 SQLite context view

系统 SHALL 将 compaction 表达为 SQLite 中新的 context view 和控制事件，不创建新的 JSONL segment 或控制记录。摘要和保留的上下文消息继续作为不可变 assistant/user/tool 消息追加到 rollout.jsonl，新的 view 通过 `context_view_ranges` 选择摘要和保留范围。

#### Scenario: 压缩后继续运行

- **WHEN** compaction 成功提交并开始下一轮 Agent 执行
- **THEN** 新 checkpoint 引用包含摘要和必要尾部的 SQLite context view，后续消息继续追加到同一个 rollout.jsonl

#### Scenario: 压缩后读取原始历史

- **WHEN** 调用方请求 compaction 之前仍未被 pruning 的历史
- **THEN** reader 通过 SQLite 旧 view 读取原始消息，且不把 compaction 控制事件作为模型消息

### Requirement: Rewind 在 SQLite 中创建新 branch 和 view

系统 SHALL 在同一个 rollout.jsonl 上追加后续消息，并在 SQLite 中创建新的 branch、context view 和 control event。旧消息保持不可变，旧后缀从新 view 中隐藏；不得创建 semantic segment 或 `parent_segment_id`。

#### Scenario: 只回退不继续

- **WHEN** 用户将 active head 回退到历史 checkpoint 或 anchor，但暂不发送新消息
- **THEN** 系统切换到新的 branch/view，旧后缀仍可由旧 checkpoint 读取但不属于当前 view

#### Scenario: Rewind 后继续追加

- **WHEN** 用户 rewind 后执行 continue 或 resume
- **THEN** 新用户消息、工具调用和模型输出追加到同一个 rollout.jsonl，并加入 rewind 创建的新 view

#### Scenario: Rewind 跨越旧后缀

- **WHEN** 当前物理尾部已经包含后续消息，但用户回退到更早 checkpoint
- **THEN** 新 view 只引用目标 checkpoint 的有效范围和新消息范围，不把物理尾部旧后缀加入当前上下文

### Requirement: Replay 是 rewind 与追加消息的业务组合

系统 SHALL 将 replay 定义为 `rewind + 可选编辑消息追加 + continue`。编辑不得修改旧 JSONL 消息，也不得产生 message revision、replace 或 truncate 记录。

#### Scenario: 编辑历史消息后 replay

- **WHEN** 用户编辑历史 user message 并提交重新执行
- **THEN** 系统以 `before` anchor 创建新 view，追加编辑后的 user message，随后追加新的 assistant/tool 消息；旧后缀不属于 active view

#### Scenario: Replay 不复制完整历史

- **WHEN** replay 从历史位置启动
- **THEN** SQLite view 只保存来源 view 和消息范围引用，不在 SQLite 或 JSONL 中重复复制已有历史正文

### Requirement: Continue 和 resume 从当前 view 追加

系统 SHALL 将 continue/resume 视为从当前 active view 继续追加消息。没有发生 rewind、compaction 或其它上下文切换时，不创建新的 branch。

#### Scenario: 当前 view 继续对话

- **WHEN** 用户从当前 active view 发送新消息或恢复未完成执行
- **THEN** 系统追加新的 user/assistant/tool 消息并更新 active view/checkpoint，不改变旧 view

### Requirement: Fork 支持三种独立模式

系统 SHALL 支持：

1. `context_fork`：复制目标 view 的有效消息；
2. `history_prefix_fork`：复制会话开始到 anchor 的有效消息前缀；
3. `full_rollout_copy`：复制父 rollout.jsonl 和 SQLite 控制状态。

#### Scenario: 默认有效上下文 fork

- **WHEN** 前端创建默认 `context_fork` 且请求没有携带 `turn_id`
- **THEN** 后端从当前 active view 的 branch lineage 中选择最近一个已经完成的 normal Turn，而不是按最大物理 message sequence 或最新 checkpoint 选择运行中的 Turn
- **AND** 子会话只物化到该已完成 Turn 的 inclusive 边界，将有效消息复制到自己的 rollout.jsonl，并将源 checkpoint 的非 messages channel、channel versions、`versions_seen`、`pending_sends` 和 pending writes 物化到自己的 SQLite 初始 checkpoint/view；父会话删除不影响子会话

#### Scenario: 运行中的 Turn 禁止 fork

- **WHEN** fork 请求明确携带一个尚未完成的 Turn，或者默认解析发现源会话存在 normal Turn 但没有任何已完成 Turn
- **THEN** 后端返回明确的 fork 错误，不创建可用的子会话，也不通过自动取消运行态 Turn 来伪造 fork 成功

空 rollout 没有运行中的 Turn，仍允许创建没有 checkpoint 的空子会话；这不属于从运行中 Turn fork。

#### Scenario: 历史前缀 fork

- **WHEN** 用户选择 history_prefix_fork
- **THEN** 子会话独立拥有从会话开始到目标 anchor 的消息前缀和对应 checkpoint 的非 messages channel 状态；如果 anchor 没有可恢复的源 checkpoint，系统返回明确错误，不使用空 channel 静默替代

#### Scenario: 完整 rollout fork

- **WHEN** 用户明确选择 full_rollout_copy
- **THEN** 子会话拥有父 rollout 消息、SQLite checkpoint/view/branch 状态和所有 checkpoint channel BLOB 的独立副本，不依赖父数据库；父库的 `fork_origins` 与 `retention_refs` 等会话关系表不被带入，子会话只由统一 writer 写入自己的唯一 provenance

### Requirement: Fork 来源不形成默认运行时依赖

默认 fork SHALL 在子 SQLite 的 `fork_origins` 保存真实父会话、源 checkpoint、源 view、fork 模式和 relationship，但运行时不得读取父 rollout 或父 SQLite。一次 fork 只能产生一条新的 provenance 记录；`full_rollout_copy` 的文件复制步骤不得额外重复插入来源记录。`pinned` fork SHALL 在父 SQLite 的 `retention_refs` 创建 active fork 保留记录，并在子会话删除时标记为 released；本 change 不提供单独的 unpin API。

#### Scenario: 父 rollout 被删除

- **WHEN** 非 pinned 子会话已经完成消息物化，随后父会话和父 rollout 被删除
- **THEN** 子会话仍能读取自己的 checkpoint、发送消息并继续执行

#### Scenario: Pinned 子会话阻止父删除

- **WHEN** 子会话存在 active pinned retention reference
- **THEN** 删除父会话通过父 SQLite 的 active `retention_refs.owner_session_id` 被拒绝，并返回具体子会话

#### Scenario: Pinned 子会话被删除

- **WHEN** 用户删除一个作为 pinned fork 子会话的会话
- **THEN** 系统将父 SQLite 中对应 `reference_kind=fork`、`reference_id=fork_id` 的 retention reference 标记为 `released`，之后父会话和其未被其它关系保护的 view 可以继续删除或 pruning

### Requirement: Fork 物化必须可恢复提交

系统 SHALL 在目标 SQLite 记录 `fork_materializations` journal。消息、checkpoint
channel、pending state 的中间 append 不得直接让目标会话变成可用状态；目标库必须在
同一收敛事务中写入 Turn finalization、运行态 Turn 的取消状态和唯一
`fork_origins`，然后进入 `target_committed`。pinned fork 的父库 retention 成功后
才进入 `committed`。启动时遇到 `prepared` 必须按 journal 清理目标半成品，遇到
`target_committed` 必须重试 retention/最终状态，不能根据 JSONL 猜测 fork 是否成功。

#### Scenario: Fork 物化中途崩溃

- **WHEN** 进程在目标消息或 checkpoint 已部分追加但 journal 仍为 `prepared` 时退出
- **THEN** 下次打开目标 rollout 将其恢复为干净的未物化状态，标记该 journal 为 `aborted`，且历史和 LangGraph checkpoint 不返回半个 fork

#### Scenario: Fork 目标提交后进程退出

- **WHEN** 目标库已经将 finalization、运行态终止和 provenance 一起提交为 `target_committed`，但父 pinned retention 尚未完成时退出
- **THEN** 下次打开目标 rollout 幂等补写父 retention 并将 journal 标记为 `committed`；不得重复创建 provenance 或 retention

### Requirement: Fork 保留 checkpoint-level pending state

系统 SHALL 在 `context_fork` 和 `history_prefix_fork` 中复制源 checkpoint 的 `pending_sends` 以及 `pending_writes` 的完整 SQLite 行，包括 task path、write index、serializer、值 BLOB、hash 和状态；不得为了创建子 checkpoint 而清空这些状态。

#### Scenario: Fork 恢复 pending state

- **WHEN** 源 checkpoint 存在 pending sends 或 pending writes
- **THEN** 子 checkpoint 恢复后返回相同的 pending state，并且 pending write 的 task path 与写入顺序保持不变

### Requirement: Fork anchor 具有明确包含语义

系统 SHALL 区分 `inclusive` 和 `before` 两种 anchor。`context_view_ranges` 必须保存实际选择的逻辑范围。

#### Scenario: Inclusive fork

- **WHEN** 用户从 B:3 创建 inclusive context fork
- **THEN** 子 view 包含截至 B:3 的有效消息，不包含 B:3 之后的旧后缀

#### Scenario: Before replay

- **WHEN** 用户编辑并重新执行 B:3
- **THEN** 新 view 只包含 B:3 之前的消息，再追加编辑后的 B:3 和新的后续消息

#### Scenario: 首个真实 checkpoint 带有未落盘的内存父 ID

- **WHEN** LangGraph 第一次提交真实消息时携带一个尚未写入 SQLite 的初始 checkpoint ID
- **THEN** saver 将当前携带完整 messages 快照的 checkpoint 作为根 checkpoint 保存，不写入孤儿 `parent_checkpoint_id`
- **AND** 从该首个 Turn 发起 retry/regenerate/replay 时，系统直接使用当前 checkpoint 的消息前缀，不因缺少内存父 checkpoint 返回 500

### Requirement: 用户操作使用 Turn anchor，compaction 保留 Message anchor

系统 SHALL 将用户可见的 rewind、replay 以及从历史 Turn 发起的上下文操作设计为只接收稳定的 `turn_id` 和 `inclusive`/`before` 语义；fork 可以省略 `turn_id`，省略时由后端解析当前 active lineage 中最近一个已完成 Turn。前端不需要传递 `view_id`、`checkpoint_id` 或物理消息序号。

系统 SHALL 保留 compaction 的 message 级定位能力。自动 compaction 和用户主动 compaction 均可以以 `message_id` 或对应的 `message_sequence` 作为精确截断点，截断点可以位于一个 Turn 的中间；不得因为存在 `context_view_turns` 而把 compaction 强制对齐到 Turn 末尾。

#### Scenario: 从被多次压缩的历史 Turn 发起操作

- **WHEN** 用户只提交 `turn_id=turn-1`，当前 active head 经历了多次 rewind 和 compaction，最新 view 已经不再包含 Turn-1
- **THEN** 后端从 active head 沿 `parent_view_id`/view jump lineage 向祖先查找，选择最近一个仍完整包含 Turn-1 的 view，并用该 view 的 checkpoint/channel 状态执行 fork 或 rewind
- **AND** 前端和调用方不需要知道该 view 的 `view_id`

#### Scenario: Compaction 切入 Turn 中间

- **WHEN** compaction 的 `message_id` 指向 Turn-5 的中间 tool_result 之前
- **THEN** compaction 创建以 message 范围为边界的新 context view，保存精确的 message cutoff 和摘要/保留范围
- **AND** `context_view_turns` 只作为完整可展示 Turn 的派生定位表，不改变 compaction 的 message 范围
- **AND** Agent 的 `full` context reader 仍可读取压缩摘要、保留消息和后续消息，不依赖 Turn 派生表拼接上下文

#### Scenario: Turn 只有部分内容留在当前 view

- **WHEN** 用户从一个 Turn 发起 inclusive fork/rewind，但当前候选 view 只保留该 Turn 的部分消息或只保留 compaction summary
- **THEN** resolver 继续沿祖先 view 查找最近一个完整包含该 Turn 的 view
- **AND** 如果当前 branch lineage 没有任何完整 view，系统返回明确的不可达/不可恢复错误，不把 summary-only view 静默当作原始 Turn

### Requirement: 所有 Turn anchor 操作共享解析器

系统 SHALL 由同一个 backend resolver 实现 `turn_id -> active-lineage source view -> source checkpoint/channel state -> message boundary`，fork、rewind 和 replay 不得各自扫描 JSONL 或只按最大物理序号猜测来源。

#### Scenario: replay 复用 Turn resolver

- **WHEN** 用户以 `turn_id=turn-5` 编辑消息并发起 replay
- **THEN** 业务层先用 resolver 得到 Turn-5 的完整 source view 和 `before` message boundary，再执行 `rewind + continue`
- **AND** 新 view 仍可在 Turn 中间发生过 compaction 的历史上正确保留精确 message 范围

### Requirement: Logical pruning 不破坏 active view

系统 SHALL 通过 SQLite retention_refs 检查 active view、checkpoint、fork、审计和 pinned 关系后才能标记旧消息范围为可 pruning。普通 pruning 只改变有效 view；物理 JSONL 回收必须是显式离线 compaction。

#### Scenario: Rewind 后清理旧后缀

- **WHEN** 旧 branch 没有 checkpoint、fork 或 retention 引用
- **THEN** 系统可以在 SQLite 中标记旧范围为可回收，并保持 active view 完整

#### Scenario: 被 checkpoint 保留的历史

- **WHEN** 旧范围仍被历史 checkpoint 的 view 引用
- **THEN** pruning 必须拒绝该范围，并保留原始消息
