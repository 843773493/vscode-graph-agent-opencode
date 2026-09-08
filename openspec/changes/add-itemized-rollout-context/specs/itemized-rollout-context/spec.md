## Purpose

为会话 rollout 建立独立于 LangChain 和具体 Provider 的不可变语义 item 层，使上下文、历史、恢复和 Provider 请求都能从同一份有序事实重建。

## ADDED Requirements

### Requirement: Canonical item 具有稳定身份和语义顺序

`CanonicalItemRecord.status` 完整枚举固定为 `completed | partial | incomplete | cancelled | failed | unknown`，六者在 JSONL 中都表示终态；`completed` 表示 semantic item 的声明 payload 已完整收敛并可按 schema 正常投影，`partial` 表示截至中断/停止边界已持久化的完整 payload 快照但尚未达到正常语义完成边界；二者都只能写成一个 immutable JSONL item，不能把已提交的 `partial` 原地改成 `completed`。`open`、`active`、`running`、`draft` 和 `completed_empty` 只允许出现在内存 draft 或 assembly/Turn/control state，不能写入 canonical item。ItemDraft 只能从内部 `draft` 转移到上述六个终态之一；item 写入后不得更新、删除、覆盖、插入重排或改变 status。retry、resume、纠错必须追加新的 item identity，并用 `retry_of`/`resumes`/`supersedes` 关系连接旧 item；没有稳定 payload 的崩溃 draft 只能通过 control outcome 记录 execution lost，不能补造 `status=unknown` item。非 `completed` item 不得作为正常 final response；tool result 的已提交 payload 如果外部执行结果未确认，必须在其 typed payload 内使用 `tool_outcome=unknown` marker，且不可作为成功 replay input。`tool_outcome` 不是 `CanonicalItemRecord.status`，执行/控制记录的 outcome 也不得引入带 outcome 前缀的 unknown 状态别名。

系统 SHALL 将用户输入、assistant 输出、reasoning、tool call、tool result、需要持久化的 runtime notice、压缩摘要和未知 Provider 扩展表达为带 schema version 的 `CanonicalItemRecord`。v2 的必填核心字段必须非空且同时存在：`format_version=2`、`record_type=item`、`item_sequence`、`item_id`、`semantic_kind`、`payload_kind`、`status`、`producer_ref`、`payload`、`content_hash`、`created_at` 和 `metadata`；其中 `metadata` 至少是空 object，`producer_ref` 是单一完整 payload producer，`payload` 必须与 `payload_kind` 匹配。`turn_id`、`turn_scope`、`message_group_id` 和 `wire_role` 是按语义可空/可省略的关联或投影字段，不得被 reader 当成隐含默认值。`semantic_kind` MUST 使用固定枚举 `user_input | assistant_output | reasoning | tool_call | tool_result | runtime_notice | compaction_summary | attachment | extension`，`payload_kind` MUST 使用完整枚举 `text | structured_content | tool_call | tool_result | summary | attachment_ref | opaque | extension`。不得同时使用含义重叠的通用 `kind` 作为 canonical 语义字段。`assistant_text` 和 `final_response` 是 projection，不是 `semantic_kind` 枚举值。只对当前请求生效的静态或动态 system context 不得因为最终使用了 system/developer wire role 就自动成为 canonical item。

`turn_scope=turn_root` 必须有非空 `turn_id`，且只能用于该 Turn 唯一的 `semantic_kind=user_input` root；有非空 `turn_id` 的普通 Turn item 必须使用 `turn_scope=turn_member`。反向地，任何非空 `turn_id` 都必须配合 `turn_root` 或 `turn_member`，不得出现 `turn_id != NULL` 且 `turn_scope=NULL`。`turn_scope=ambient` 或 `turn_scope=pending_next_turn` 必须 `turn_id=NULL`，不得进入普通 Turn member/root 集合；持久化的 pending runtime notice 必须是 `semantic_kind=runtime_notice` 且 `turn_scope=pending_next_turn`，request-only notice 不产生 CanonicalItemRecord。若 `turn_id`、`turn_scope`、`message_group_id` 均为 null，item 不属于任何 Turn，reader 不得从相邻 item、wire role 或 message group 推断归属；`message_group_id` 非空也不能改变 root/member/ambient 约束。

`payload_kind` 的完整枚举固定为 `text`、`structured_content`、`tool_call`、`tool_result`、`summary`、`attachment_ref`、`opaque` 和 `extension`：分别表示精确 Unicode 文本、已知 schema 的 JSON object/array、规范化工具调用、规范化工具结果、摘要结构、稳定附件引用、显式编码的不可解释/受保护值和扩展 envelope。`opaque` 必须带非空 encoding、value、provider 或 wire type、schema version；`extension` 必须带非空字符串 `extension_schema` 与 `extension_version`，其中 schema 是稳定 namespaced identifier、version 是该 schema 的显式版本值，另有 value 和 protection/encoding metadata。`semantic_kind=extension` 只能使用 `extension|opaque`；其它 semantic/payload 组合必须经过固定 compatibility table。v2 reader 遇未知 payload kind、缺 schema/version、非法 shape 或不支持的组合必须返回 format/schema recovery error，不得静默降级为空 payload、普通文本或 opaque；已知 extension 但无 handler 时可以保留 immutable raw/hash/offset 并标记 unsupported，但不能进入普通 context，必要来源则返回 `extension-unsupported`。

下表就是本 change 的唯一 semantic/payload/status compatibility matrix；它不是示意列表。每个 `semantic_kind` 的允许 payload、status 和 marker 规则均以表为准，未列出的组合一律在 JSONL durability barrier 前以 `item-schema-incompatible` 拒绝：

| `semantic_kind` | 允许的 `payload_kind` | 允许的 item `status` | 额外 marker/约束 |
|---|---|---|---|
| `user_input` | `text`, `structured_content` | `completed` | `turn_root` 必须是唯一 user root；不得有 `tool_outcome` |
| `assistant_output` | `text`, `structured_content` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 只有 `completed` 可参与 finalization；不得有 `tool_outcome` |
| `reasoning` | `text`, `summary`, `opaque`, `extension` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | `opaque`/`extension` 必须有 protection/encoding metadata；不得作为 final item |
| `tool_call` | `tool_call`, `structured_content` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 必须有 tool invocation/call identity；不得用 `tool_outcome` 表示 call status |
| `tool_result` | `text`, `structured_content`, `tool_result`, `opaque`, `extension` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 必须有 tool attempt/result identity；`status=completed` 时 `tool_outcome` 可为 `success|failure|cancelled|unknown`，但外部结果未确认时必须为 `unknown`；其它 status 只能省略 marker 或使用 `unknown`；只有 `status=completed` 且 `tool_outcome=success` 才可 replay |
| `runtime_notice` | `text`, `structured_content`, `opaque`, `extension` | `completed` | pending notice 必须使用 `pending_next_turn` 或 `ambient` scope；append 失败由 control outcome 记录，不补造 item；不得有 `tool_outcome` |
| `compaction_summary` | `summary`, `structured_content` | `completed` | 必须绑定 compaction/view revision；失败由 control outcome 记录，不补造 item；不得有 `tool_outcome` |
| `attachment` | `attachment_ref` | `completed` | payload 必须含稳定 ref、长度和 hash/availability；不得有 `tool_outcome` |
| `extension` | `extension`, `opaque` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 必须有非空 `extension_schema`/`extension_version` 和 protection metadata；自定义 outcome 只能是 namespaced 字段，核心不得解释 |

item `status` 只描述单个 canonical payload 的持久化/语义完成事实；`ExecutionRecord`、`ModelCallRecord`、assembly、storage commit 和控制记录的结果字段固定命名为 `outcome`，其值只能是独立的 `ControlOutcome=completed|completed_empty|failed|interrupted|cancelled|execution_lost|unknown`，不得把 `ControlOutcome` 值写入 item status，也不得把 item 的 `unknown` 推导成 execution lost。`tool_outcome=success|failure|cancelled|unknown` 仅是 `tool_result` typed payload 的额外结果 marker；它不能出现在其它 semantic kind，也不能替代控制记录的 `outcome`。非法的 semantic/payload/status/marker 组合不写 JSONL、不写 catalog、不推进 offset；未知 semantic/payload kind 或非 namespaced marker 进入 format/schema recovery error，不能降级成 `opaque`、`unknown` 或普通文本。

#### Scenario: 混合模型输出保持 item 顺序

- **WHEN** 一次模型输出依次产生 reasoning、assistant output text 和 tool call
- **THEN** canonical history 保留可定位的 `reasoning`、`assistant_output` 和 `tool_call` item 及其相对顺序，历史 `assistant_text` 只作为 assistant output 的文本 projection，tool call 不被压入文本，reasoning 不被拼接为普通文本

#### Scenario: 重试不会复用错误的 item 身份

- **WHEN** 同一个 Turn 因业务校验重新发起第二次 model call
- **THEN** 第二次 call 使用新的 source/model-call identity 和新的 item identity，旧 call 的 item 保持不可变并可被 projection 标记为 intermediate 或 superseded

### Requirement: Turn 起点必须由真实用户输入显式确定

`Turn.status` 在本 change 内统一使用闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`；`open`/`active` 非终态，其余为 terminal outcome。`completed_empty` 是唯一的“请求正常结束但没有 canonical output item”名称，不能使用 `empty`、`no_output` 或其它别名，且不属于 `CanonicalItemRecord.status`。合法转移固定为：

| 当前 `Turn.status` | 允许的下一状态 | 条件 |
|---|---|---|
| `open` | `active`, `cancelled`, `failed`, `unknown` | acceptance 已提交后由执行启动或明确控制结果收敛 |
| `active` | `completed`, `completed_empty`, `interrupted`, `cancelled`, `failed`, `unknown` | provider/工具/控制结果在 terminal convergence 中一次提交 |
| `interrupted` | `active` | 仅显式 resume；必须创建新的 `execution_id`，无新用户输入继续原 Turn |
| `unknown` | `active` | 仅 reason=`execution_lost` 的显式 resume；必须创建新的 `execution_id` |
| `completed` | 无 | terminal；`final_item_id` 必须非空 |
| `completed_empty` | 无 | terminal；`final_item_id=NULL` |
| `cancelled` | 无 | terminal；对该 Turn 的 `resume_turn` 或绑定原 `turn_id` 的 `dispatch_replay` 均返回 `turn_not_resumable`；新执行只能调用独立的 `replay_as_new_turn` |
| `failed` | 无 | terminal；只能由新的真实用户输入创建新 Turn |

回放操作的 API 语义固定分离：`history_replay` 只生成历史 projection，不创建 execution；`resume_turn` 只对状态表允许的 `interrupted` 或 reason=`execution_lost` 的 `unknown` 复用原 Turn 并创建新 execution；`dispatch_replay` 表示把 Provider dispatch 绑定到原 `turn_id`，对普通 `cancelled` 和 `full_rollout_copy` 的 cancelled historical 均返回 `turn_not_resumable`，不得写入新 execution 或修改原 Turn；`replay_as_new_turn` 才是重新执行能力的独立显式新 Turn 创建操作，创建新的 target-local Turn/root/accepted ingress/acceptance/initial execution，并用 `replay_of_turn_id` 保存 lineage。它不是原 cancelled Turn 的 `dispatch_replay` 或 `resume_turn`，同一个 API 请求不得一边按 `dispatch_replay` 返回错误、一边创建新 Turn。

`cancelled` 是吸收态。特别是 `full_rollout_copy` 为未复制 source runtime 产生的 `cancelled` historical 必须保持不可运行；`resume_turn` 不创建 execution/model-call、不改变该 Turn，并返回 `turn_not_resumable`。对该历史的 `history_replay` 只能生成 projection；若要重新执行，调用方必须明确选择独立的 `replay_as_new_turn`，而不是要求原 Turn 的 Provider dispatch。所有 status 转移、execution outcome 和 `final_item_id` 约束必须在同一 SQLite 收敛事务中可见。

`accepted_ingress_id` 与 `acceptance_idempotency_key` 各自在 `(session_id, accepted_ingress_id)` 与 `(session_id, acceptance_idempotency_key)` 范围内唯一，并各自一对一指向一个 accepted Turn。相同 key、相同 ingress、相同 payload hash 和相同 source branch 的重试返回原 Turn/root/initial execution；相同 key 但 ingress、payload 或 branch 不同，或相同 ingress 但 key/payload 不同，必须返回明确 acceptance idempotency conflict，不创建第二个 Turn/root，也不修改原记录。检查与创建必须在同一 SQLite 事务内完成。

系统 SHALL 在接受真实用户输入时创建或恢复一个 `TurnRecord`，并为新 Turn 保存唯一的 `root_input_item_id` 和对应的物理 item sequence。权威 `TurnRecord` 至少包含 `turn_id`、session-global 且不可重排的 `turn_ordinal`、`accepted_ingress_id`、session 内唯一的 `acceptance_idempotency_key`、`root_input_item_id`、`root_input_item_sequence`、`initial_execution_id`、`last_execution_id?`、`final_item_id?`、`status` 和不可变的 origin `source_branch_id`。`root_input_item_id` MUST 指向 `semantic_kind=user_input` 且 `producer_ref.producer_kind=user` 的 canonical item；Turn 的 identity 和起点不得从 `wire_role`、LangChain message 类型、首个物理 item、`message_group_id` 或 Provider model call 推断。一个 Turn 可以包含多个 execution/model-call，并允许在没有新用户输入时 resume 原 Turn。

`acceptance_idempotency_key` 在 `(session_id, acceptance_idempotency_key)` 范围内唯一。同一 key 重复提交相同 ingress payload hash 时，系统 MUST 返回原 `turn_id`、root item 和 `initial_execution_id`；同一 key 对应不同 payload 时 MUST 返回幂等冲突，不得创建第二个 Turn/root。acceptance-time 的 Turn、root item 和首次 execution 必须作为同一可重试提交边界可见。

#### Scenario: 普通用户消息开启新 Turn

- **WHEN** 一条真实用户输入被接受并准备进入 AgentLoop
- **THEN** 系统以 acceptance idempotency key 原子确定新的 `turn_id`、`turn_ordinal`、`root_input_item_id`、`source_branch_id` 和 `initial_execution_id`，再将用户 input item 纳入该 Turn；后续 assistant、tool 和持久化 runtime notice 通过明确关系加入，而不是重新猜测 Turn 起点

#### Scenario: 打断提醒不创建新 Turn

- **WHEN** AgentLoop 被打断，系统注入一个语义为 runtime notice 的 `system_reminder`，且下一条才是普通用户输入
- **THEN** reminder 可以作为 `semantic_kind=runtime_notice`、`turn_scope=pending_next_turn` 的 canonical item 或 request-only contribution 记录，但不得创建 normal Turn、占用 `root_input_item_id` 或因为其 LangChain/wire role 为 user 而改变 Turn 顺序；下一条真实用户输入才创建新的 root

#### Scenario: Provider 改变 wire role

- **WHEN** 同一个用户 input 或 runtime notice 被不同 Provider 编码为 user、system、developer 或其它等价角色
- **THEN** canonical item 的 `turn_id`、`root_input_item_id`、semantic kind 和 producer 保持不变，wire role 变化不影响 Turn 分组

#### Scenario: 无新用户输入的 resume

- **WHEN** 上一次 execution 被中断或执行丢失，但用户通过 continue/resume 继续同一个请求
- **THEN** 对符合 Turn.status 转移表的 `interrupted` 或 reason=`execution_lost` 的 `unknown` Turn，系统保留原 `turn_id` 和 `root_input_item_id`，创建新的 `execution_id`/model-call identity，并通过 `resumes_execution_id` 或等价关系连接两次执行；对 `cancelled` Turn 不适用 `resume_turn`，必须返回 `turn_not_resumable`

#### Scenario: Turn status 的显式恢复边界

- **WHEN** 用户对 `interrupted` Turn，或 reason=`execution_lost` 的 `unknown` Turn 发起显式 resume
- **THEN** Turn 才能转回 `active`，并创建新的 execution/model-call lineage；`completed`、`completed_empty`、`failed` 和 `cancelled`（包括 `full_rollout_copy` 的 cancelled historical）均返回 `turn_not_resumable`，不修改原终态

### Requirement: Execution、model call、retry/resume 和 final item identity 必须分层

系统 SHALL 将用户交互 `turn_id`、AgentLoop `execution_id`、Provider 请求 `model_call_id` 和 canonical output `item_id` 作为不同 identity，并通过显式的 `TurnExecutionLink(turn_id, execution_id)` 与 `ModelCallRecord(execution_id, model_call_id)` 关联。一次 execution 内的 Provider retry MUST 创建新的 `model_call_id` 和 attempt ordinal；整个 execution 重启或 `resume_turn` MUST 创建新的 `execution_id`，且只有 Turn.status 转移表允许时才可在没有新用户输入时继续原 Turn；`cancelled` Turn 不得恢复。任何 retry/resume 不得复用已经提交的 output `item_id`。

`TurnRecord.final_item_id` MUST 只在 `turn_finalize` 与对应 canonical `assistant_output` item 的 terminal convergence 提交边界内写入。`Turn.status=completed` 时 `final_item_id` 必须非空，并指向同一 Turn 内 `status=completed` 的 `assistant_output` item；`completed_empty`、`open`、`active`、`interrupted`、`cancelled`、`failed` 和 `unknown` 时必须为空。Provider 空输出使用 `completed_empty`，不创建伪造 output item。`assistant_text` 和 `final_response` 是 projection；未完成 finalization 时，partial、failed、cancelled 或 unknown item 不得仅因其是最后一个 assistant item 就成为 final response。

#### Scenario: Provider retry 保留旧 item

- **WHEN** 一次 model call 因超时或 provider 错误重新请求
- **THEN** retry 使用新的 `model_call_id`、attempt ordinal 和 output item identity，旧 call 的已提交 item 保持不可变，并通过 retry relation 标记其结果状态

#### Scenario: final item 原子确定

- **WHEN** AgentLoop 明确完成 Turn 并选择最终 assistant output
- **THEN** `turn_finalize`、`final_item_id` 和该 canonical output item 在同一提交边界内可见；reader 不根据最后一条 assistant item 猜测 final response

#### Scenario: 中断没有 final item

- **WHEN** assistant output 在中断时只有 partial item，且没有成功的 Turn finalization
- **THEN** Turn 保留 partial/interrupted outcome，`final_item_id` 为空，历史 projection 不返回该 partial item 作为 `final_response`

### Requirement: Turn、branch 和 view identity 必须避免隐式复制

系统 SHALL 将 `turn_id` 作为 session 内逻辑用户交互的全局不可变 local identity，将 `turn_ordinal` 作为首次 acceptance 分配的 session-global ordinal，将 `source_branch_id` 作为首次接受该 Turn 的 origin branch。在同一 session 内，派生 branch/view 只能复制对既有 Turn/item 的引用，不得复制或重新编号 `TurnRecord`；`resume_turn` 仅按 Turn.status 转移表复用同一 Turn 并创建新的 execution/model-call lineage；`history_replay` 是唯一可以在同一 owner session 的历史 view 中复用 source Turn/root 的回放操作，且不创建 execution；`replay_as_new_turn` 必须创建新的 Turn/root/acceptance/initial execution 并以 `replay_of_turn_id` 关联，不能把该操作解释为原 Turn 的 `dispatch_replay`。跨 session fork 按后文 `GlobalEntityRef` 和 target-local mapping 合同建立新的 target Turn/item identity。fork 后新接受的用户输入或显式 `replay_as_new_turn` 才创建新的（同一 session 内）`turn_id`、`turn_ordinal` 和 origin `source_branch_id`；`context_view_turns.logical_turn_ordinal` 是 view-local 顺序，必须与 global `turn_ordinal` 分开存储和解释。

#### Scenario: history_replay 复用历史 Turn

- **WHEN** 从已有 Turn 的历史 anchor 在同一 owner session 内创建 history view 并执行 `history_replay`
- **THEN** 新 history view 复用原 `turn_id`、`turn_ordinal`、`source_branch_id` 和 `root_input_item_id`，只为该 view 登记对应的 `logical_turn_ordinal`；不创建 execution，且 source Turn/root 仍是被投影的历史实体

#### Scenario: replay_as_new_turn 创建独立 active Turn

- **WHEN** 调用方明确选择 `replay_as_new_turn`，并从 source Turn、view 或 anchor 取得重放所需历史
- **THEN** active view 可以复制或引用 source history 作为上下文前缀，但必须另登记新的 target-local `TurnRecord`、新的 `user_input` root、accepted ingress、acceptance、initial execution 和新的 `logical_turn_ordinal`；即使 root payload 与 source root 相同，新的 root item identity 也不能复用 source `root_input_item_id`
- **AND** source Turn/root 只能作为上下文前缀或 `replay_of_turn_id` lineage，Provider dispatch 绑定新 Turn；该操作不是原 Turn 的 `history_replay`、`resume_turn` 或 `dispatch_replay`

#### Scenario: fork 后接受新输入

- **WHEN** 用户在 fork 后提交一条新的真实输入
- **THEN** 系统创建新的 TurnRecord、全局递增的 `turn_ordinal`、新的 root item 和以新 branch 为 origin 的 `source_branch_id`，不重排旧 Turn 的全局 ordinal

#### Scenario: view-local Turn 顺序

- **WHEN** 同一个 Turn 出现在两个具有不同 fork lineage 的 context view 中
- **THEN** 两个 view 可以拥有不同的 `logical_turn_ordinal`，但 root lookup 都解析到同一个全局 `root_input_item_id`，不得用物理 sequence 或 wire role 产生第二个 Turn

上述 session 内 identity 复用不适用于跨 session fork。跨 session 引用 MUST 使用 `GlobalEntityRef=(session_id, entity_type, local_id)`；`session_id` 是实体正文、控制状态和索引的 owner namespace，裸 `local_id` 只在其 owner session 内唯一。`context_fork`、`history_prefix_fork` 和 `full_rollout_copy` 都必须在 target session 创建新的 target-local Turn、root item、item sequence/JSONL offset、tool invocation/call/attempt、execution、model-call、assembly、view、branch 和操作 anchor，并在不可变的 `fork_entity_mappings`/等价 provenance 中保存 source ref 到 target ref 的一对一映射；source ref/offset 只能作为 lineage/audit 坐标，不能被 target reader 当作 canonical identity 或直接打开。source overlay epoch/base/delta、canonical ambient item 和 assembly detail 也必须建立 target-local mapping；target reader 不能读取 source detail path。三种模式分别复制 source active view 的有效范围、指定 inclusive/before anchor 的有效 prefix、以及全部 canonical/legacy rollout 和 SQLite control/channel state；三者都创建新的 target active branch/view，target 使用 target-local logical ordinal 和 committed offset。target rollout 一律为 v2；v1 source 的 message identity/sequence/offset 仅保留为 `legacy_source_ref`，不成为 target v2 identity。

跨 session fork 的 source/target retention 与 lineage 独立：`fork_origins` 保存两侧 session、source checkpoint/view/branch、mode、mapping version、overlay/detail mapping 和 relationship；detached fork 在物化提交后不依赖 source，pinned fork 只为审计保留 source retention ref，target active plan 仍只读 target-local copy。source 未终态 Turn/active execution/未终态 assembly 在 `context_fork`/`history_prefix_fork` 中导致 preflight 拒绝且不创建 target；`full_rollout_copy` 则允许完整历史 target，但对应 target Turn 标记为 `cancelled`、reason=`fork_source_runtime_not_copied`，不可运行。复制后的 target Turn 只有在 Turn.status 转移表允许时才可复用 target `turn_id` 做显式 `resume_turn` 并创建新的 target execution/model-call/assembly；`history_replay` 只在相应 owner namespace 的 history view 中复用已有 Turn/root 且不创建 execution，跨 session fork 本身不以它复用 source 裸 identity；显式 `replay_as_new_turn` 才创建新的 target Turn/root/acceptance/initial execution，并在 target active view 登记新的 `logical_turn_ordinal`，可以复制或引用已映射 source history 作为上下文前缀，source Turn/root 不成为新 Turn 的 root，并以 `replay_of_turn_id` 关联，且不属于原 Turn 的 `dispatch_replay`；普通或 historical `cancelled` Turn 的 `resume_turn`/`dispatch_replay` 均返回 `turn_not_resumable`。target 新用户输入创建新的 target Turn/root/initial execution，且 target `turn_ordinal` 大于已复制 Turn 的最大值。required detail 无法复制时 fork 失败，optional detail 显式为 unavailable。

跨 session fork 还必须映射 Turn 的 acceptance identity：source `accepted_ingress_id` 和 `acceptance_idempotency_key` 通过 `fork_entity_mappings` 映射为 target session 新的 target-local 值，分别满足 `(target_session_id, target_accepted_ingress_id)` 与 `(target_session_id, target_acceptance_idempotency_key)` 唯一。复制值必须标记 `identity_origin=fork_copied`，source 值只在 source `GlobalEntityRef`/lineage 中保留；target 普通 ingress 不得复用这些 copied key，target 新输入必须由 target ingress 重新生成新值。对 copied Turn 的显式 `resume_turn` 仅在 Turn.status 转移表允许时复用 target Turn 和 copied acceptance 关联，并创建新的 target execution/model-call；`history_replay` 不创建 execution，显式 `replay_as_new_turn` 才创建新的 target Turn/root/acceptance/initial execution 并以 `replay_of_turn_id` 关联，且不属于原 Turn 的 `dispatch_replay`；普通或 historical `cancelled` Turn 的 `resume_turn`/`dispatch_replay` 均返回 `turn_not_resumable`。重复 fork 使用同一 fork idempotency key 返回原 mapping，source/target acceptance mapping 不得重复或覆盖。

#### Scenario: 跨 session fork 不复用 source item identity

- **WHEN** `afork(source_session_id, target_session_id, mode)` 完成任一三种物化模式
- **THEN** target active view 的每个 root/item/offset/tool/execution/model-call/assembly ref 都属于 target namespace，source ref 仅可从 fork lineage/mapping 查询；target reader 不扫描或打开 source JSONL/SQLite offset

#### Scenario: 跨 session fork 后恢复、重放与新输入

- **WHEN** target 对已复制且状态允许的历史 Turn 执行显式 resume、对历史执行只读 replay、明确选择 `replay_as_new_turn`，或接受新的普通用户输入
- **THEN** `resume_turn` 仅复用允许恢复的 target Turn 并创建新的 target execution/model-call/assembly；`history_replay` 仅在相应 owner namespace 的 history view 中复用已有 Turn 引用且不创建 execution；`replay_as_new_turn` 在 target active view 新建 target-local Turn/root/acceptance/initial execution 和新的 `logical_turn_ordinal`，可以复制或引用已映射 source history 作为上下文前缀，source Turn/root 不成为新 Turn 的 root；新输入也创建新的 target Turn/root/initial execution 和递增的 target ordinal，不回到 source identity；原 Turn 的 `dispatch_replay` 不会被这些操作替代

#### Scenario: 跨 session fork 后 source overlay 独立保留

- **WHEN** target 复制范围包含 source overlay 的 base/delta 或 sealed assembly detail
- **THEN** target 为 overlay 重新分配 target-local `source_overlay_epoch`、base/delta/item identity，并把 required detail 复制到 target session 的 detail store；source epoch、source ref 和 source offset 只保留为 lineage。detached source 删除不影响 target，pinned 只延长 source lineage/detail 的 retention；history prefix cutoff 不自动删除 target overlay，下一次 reconciliation 可复用、追加或物化 target-local overlay

### Requirement: Rollout JSONL 是不可变 item 事实日志

v2 `rollout.jsonl` MUST be append-only：每行只能保存一个完整且已终态化的 `CanonicalItemRecord`，已提交行的 UTF-8 字节范围、`item_sequence`、payload、status 和 hash 不得更新、删除、覆盖、插入或重排。JSONL 行先完成 durability barrier，再由 SQLite `storage_commits`/`item_catalog` 宣布可见；只有 committed offset 内且有对应 catalog/commit 的行可被 reader 使用。已写但未提交的尾部只能回收或隔离，不能被 reader 推断为事实；修正、retry、resume 或 supersede 只能追加新的 item identity 和显式 relation。`ItemDraft` 的非终态不写 JSONL，provider 空输出也不创建空 item。

新格式 rollout MUST 以一条 JSONL 记录表达一个已经终态化的语义 item，而不是以 raw provider chunk 或完整 LangChain message 作为唯一持久化单元。每条记录 MUST 包含 `item_id`、`item_sequence`、`semantic_kind`、`payload_kind`、status、payload、`producer_ref`、content hash 和可恢复的 format version；SQLite 只能保存 offset、索引、context view、checkpoint 和派生 projection，不得成为 item 正文的第二事实源。

#### Scenario: 正常 item 提交

- **WHEN** 一个 `assistant_output` 或 `tool_result` item 完成并提交
- **THEN** rollout.jsonl 追加一条独立 item 记录，reader 可以仅凭该记录恢复其 payload 和 identity；历史需要的 `assistant_text` 从 assistant output content part 派生

#### Scenario: partial 与 completed 的终态语义

- **WHEN** 一个 item 在正常 semantic boundary 前被用户中断，或在正常 boundary 收到完整 payload
- **THEN** 前者只追加一个 `status=partial` 的 immutable JSONL item，后者只追加一个 `status=completed` 的 immutable JSONL item；两者都不再更新旧行，只有 `completed` item 才能参与正常 finalization
- **AND** 后续继续生成、重试或纠错必须追加新 `item_id` 并通过 relation 连接，不能把 partial 行改写为 completed

#### Scenario: raw chunk 不直接成为历史 item

- **WHEN** Provider 将一个文本 block 分成多个网络 chunk
- **THEN** 网络 chunk 可以产生实时 delta，但 canonical rollout 只保存对应语义 item 的终态或明确 partial 终态，不为每个 chunk 追加一条历史 item

### Requirement: Item 写入与 SQLite 索引具有可恢复提交边界

本要求中的 `storage_commits` 合同固定为：`commit_kind` 只能是 `acceptance`、`assembly_sealed`、`item_convergence` 或 `terminal_convergence`，另有正交的 `commit_mode`=`item_bearing|metadata_only`。一个逻辑 SQLite 事务只能对应一条 storage commit，item-bearing commit 可以覆盖多个 JSONL item；`acceptance` 必须为 item-bearing，`assembly_sealed` 必须为 metadata-only，`item_convergence` 必须为 item-bearing，`terminal_convergence` 可为 item-bearing 或 metadata-only。同一 terminal outcome 不能同时写 item-bearing 和 metadata-only 两条 commit；同一 `(session_id, commit_kind, subject_id, idempotency_key)` 重放时 payload、outcome、mode、record count 或 offset span 不同必须报幂等冲突。

系统 SHALL 将存储提交区分为 sealed-before-dispatch 与 terminal convergence 两个阶段，并在 SQLite `storage_commits` 中记录 `commit_id`、`commit_kind`、subject identity、idempotency key、`jsonl_offset_before`、`jsonl_offset_after`、`jsonl_record_count` 和 outcome。`database_meta.committed_jsonl_offset` 是每个 session/rollout 的单一权威 committed boundary；`storage_commits.jsonl_offset_after` 是同一边界的不可脱离副本，不是 reader 可择一使用的第二权威。每条已提交 commit 必须满足 `jsonl_offset_before` 等于该事务开始时的 database meta offset、`jsonl_offset_after >= jsonl_offset_before`，且事务成功后 `database_meta.committed_jsonl_offset == jsonl_offset_after`；下一条 commit 的 before 必须等于上一条已提交 commit 的 after。插入 storage commit、更新 database meta offset、item index/view/checkpoint/control outcome 必须在同一个 SQLite 事务内完成；item-bearing commit 之前必须完成 JSONL durability barrier。provider 空输出、失败和 execution lost 等没有 JSONL item 的 terminal convergence MUST 使用 metadata-only/control commit 原子记录 assembly、execution/model-call outcome 和 Turn 状态，metadata-only 的 before/after offset 相等且 record count 为零。启动恢复必须同时校验 database meta、storage commit chain、JSONL 文件大小和 item index；缺失、等值不成立、回退、越界或链断裂时必须停止并报告 commit-boundary conflict，不得由 reader 自行选择一个 offset 继续。

#### Scenario: JSONL 已追加但 SQLite 事务失败

- **WHEN** 进程在 JSONL durability barrier 之后、SQLite 提交之前退出
- **THEN** 重启恢复不会把该 item 返回给 active view，且能够根据提交边界继续追加或明确报告未收敛尾部

#### Scenario: provider 空输出的 terminal metadata-only convergence

- **WHEN** provider 请求已经完成但没有产生可持久化 canonical output item
- **THEN** 系统以 `commit_kind=terminal_convergence`、`commit_mode=metadata_only` 提交不推进 JSONL offset 的 terminal convergence，原子保存 `assembly/model_call=completed_empty`、`Turn.status=completed_empty` 和空的 `final_item_id`，恢复不会伪造 output item

#### Scenario: sealed 与 terminal convergence 分离

- **WHEN** assembly 已完成 sealed-before-dispatch 提交，随后 provider 失败、中断或执行丢失
- **THEN** 系统保留不可变 sealed plan，再以独立的 `terminal_convergence`/`metadata_only` 提交 assembly、execution/model-call 和 Turn outcome；重复提交相同 subject/idempotency key 不创建第二条终态或第二个 item

#### Scenario: 提交幂等冲突

- **WHEN** 相同 storage commit idempotency key 被重试但 payload、outcome 或 JSONL offset span 不一致
- **THEN** 系统返回明确的 commit idempotency conflict，不覆盖原 commit，不推进 committed offset，也不改变 Turn finalization

#### Scenario: SQLite 索引指向错误正文

- **WHEN** item offset、长度或 content hash 与 JSONL 正文不匹配
- **THEN** reader 返回可诊断的 rollout/index 不一致错误，不用空 payload 或旧 message projection 静默替代

### Requirement: Draft 和终态 item 的生命周期必须区分

系统 SHALL 在内存中维护流式 item draft；只有收到语义完成边界，或中断、Provider failure、execution lost 等终态事实已经确定时，才允许将 draft finalization 为不可变 canonical item。未完成 draft 不得在 checkpoint 恢复为已完成的 assistant message；partial、failed 或 `status=unknown` 必须显式记录其状态和完成原因。`tool_outcome=unknown` 仅是某些已提交 tool_result payload 的独立 outcome marker，不是额外的 item status 名称。

#### Scenario: 用户中断文本生成

- **WHEN** assistant_output 只生成了一部分后用户发起中断
- **THEN** 系统可以提交一个标记为 partial/user_interrupt 的终态 item，但不得把它标记为正常 completed 或 Turn final response

#### Scenario: 进程崩溃丢失内存 draft

- **WHEN** 进程在 item draft 尚未 finalization 时崩溃
- **THEN** 恢复结果不得凭空生成该 draft 的 canonical item，并返回可识别的未完成执行状态

### Requirement: Middleware 通过结构化 contribution 参与上下文组装

middleware MUST NOT 原地修改既有 canonical item，也不得把对 `ModelRequest.messages`、`system_message` 或 `tools` 的修改作为 canonical history 写回。middleware 只能返回一种或多种结构化结果：请求级 prompt/context overlay、tool set/policy snapshot、context view transform，或需要持久化的 canonical item append intent。只有最后的 LangChain/Provider adapter 可以将这些结果编译为目标请求对象。

#### Scenario: 临时 system prompt 注入

- **WHEN** workspace instructions、skill、memory 或运行时身份只对本次 model call 生效
- **THEN** middleware 返回带来源、版本、顺序和 hash 的 prompt contribution，ContextRequestPlan 使用它生成 system/developer input，但不会生成普通历史 message 或修改既有 canonical item

#### Scenario: 持久化提醒注入

- **WHEN** 中断提醒、compaction summary 或其它事实必须进入后续上下文
- **THEN** middleware 返回 append intent，由 writer 追加新的 canonical item 并由 active view 选择它，不能覆盖旧 item 或旧 prompt contribution

### Requirement: Canonical item、request-only context 和 wire role 必须分离

系统 SHALL 将 context plan 中的引用区分为 canonical item reference 和 request-only reference。静态 system prompt、动态 skill 说明、workspace/environment snapshot、memory injection 以及 tool definition 默认 MUST 以 request-only contribution 或 tool-set snapshot 参与本次请求；只有显式声明为后续上下文事实时，才允许追加对应的 canonical item。Provider wire role 只是编码投影，不得改变引用的生命周期、来源或关系。

已被某次 sealed request 使用的 source revision 可以作为 request-only overlay 的稳定 base；后续 source revision 不得静默替换该 base。需要跨 checkpoint 延续的 source diff 必须通过结构化 `PersistItemIntent` 追加为 ambient `runtime_notice` item，或明确标记为只对下一次 request 有效的 request-only delta；二者都必须带 source revision、base/delta 关系和 hash，不得退化为无 provenance 的普通 message。

#### Scenario: 多个动态上下文合并到一个 system wire message

- **WHEN** 一次 model call 同时使用静态 system prompt、skill 说明和环境状态
- **THEN** ContextRequestPlan 保留三个独立的 request-only reference、来源和顺序，wire projector 可以将它们合并到同一个 system/developer 输入，但历史不会把合并结果视为一条 canonical message

#### Scenario: 动态提示提升为持久事实

- **WHEN** 某个环境通知或运行时提醒必须在后续 context view 中继续存在
- **THEN** 系统通过显式 append intent 创建新的 `runtime_notice` canonical item，并保留其来源关系；不能仅因为它曾经出现在 system wire role 中就推断其已经持久化

#### Scenario: 工具定义保持 request-only

- **WHEN** ContextRequestPlan 为本次请求选择工具及其 schema
- **THEN** tool-set snapshot 通过 Provider 的 tools/tool-config 投影发送，不进入 canonical item 序列，也不被编码为普通历史 message

### Requirement: 实时 item 和上下文贡献必须保留可扩展 provenance

系统 SHALL 在内存中的 ItemDraft、ContextContribution 和 ContextRequestPlan 上记录稳定的 provenance metadata，区分真正产生 payload 的单一 `producer_ref` 与影响请求或转换上下文的关系边。`producer_ref` 至少包括 source identity、`invocation_id`（如适用）和 source version/hash；每条 `provenance_edge` 至少包括关系类型、父 item/contribution/assembly 引用、产生顺序和 visibility/protection 状态，并以 `(relation, source_ref, target_ref, edge_idempotency_key)` 唯一。一个 canonical item 不得拥有多个 payload producer；多来源影响必须使用独立 edge。item 终态提交后，SQLite/rollout MUST 保留足以定位来源和详情的稳定引用、hash 和必要摘要；完整内部对象可以按 retention policy 留在受保护的日志或 body reference 中。middleware_id 只能作为 source identity 的组成部分，不能单独表示 item 的 producer，也不能替代 `influenced_by` 或 `transformed_by` 关系。

#### Scenario: 实时 block 追踪 middleware 来源

- **WHEN** 一个实时 assistant block 由 provider 产生，但请求曾被某个 middleware 影响或上下文被其变换
- **THEN** 内存中的 draft 和 message-stream snapshot 可以通过 item/contribution identity 区分 provider producer 与 middleware influence/transform relation，且不依赖前端重新解析 raw chunk

#### Scenario: canonical item 提交后的来源查询

- **WHEN** assistant_output、tool call 或持久化 runtime_notice item 已经提交
- **THEN** 后续历史/扩展读取可以通过稳定 item/source/assembly reference 找到 producer、影响关系和受保护详情的可用性，不要求把完整 middleware 内部状态暴露给默认历史响应

### Requirement: Context contribution 和 plan selection 顺序必须可恢复

被 `selection` 绑定且 `included=true` 的每个 `ContextContribution` MUST 在对应 `assembly_id` scope 内取得独立、稳定、不可重写的 `contribution_ordinal`；unsealed plan 中仅登记为 registry 的 contribution 和 `included=false` optional omission 不带该 binding。`ContextRequestPlan.selection` MUST 是 Saver 冻结的有序列表，每个 canonical/request-only/overlay/tool-set source ref 取得唯一 `plan_ordinal`，并记录 `selection_kind`、source overlay/base/delta role、visibility/protection/availability、omission/loss 及可得 source identity；只有 `included=true` 才强制 source revision、逻辑 content length 和恰一个 source hash token。request-only included entry 才强制 sealed detail_ref，tool_set entry 使用独立 ToolSetRef manifest，canonical entry 不带 detail_ref。`assembly_id` 是该次 plan/selection 的唯一持久范围。`ContextAssemblySnapshot`/SQLite `assembly_item_refs` 必须持久化这些字段及 content hash；omitted entry 的 detail、正文 hash/length 和 contribution ordinal 可以 null/未分配，已知 metadata 必须一致。重启后按 ordinal 恢复，不能按 `created_at`、`contribution_id`、物理邻接或 projector 本地规则猜顺序。LangChain、native Provider 和 Web history 必须消费同一 selection；request-only contribution 可合并到 system/developer wire role，ToolSetRef 只能进入 Provider tools/tool-config，二者都不得被 projector 无条件 prepend 到 canonical messages。optional omitted entry 只保留 omission/loss metadata 并跳过正文/工具定义，required omission 必须拒绝 seal/dispatch。顺序或 ordinal 不一致必须返回 plan-order-integrity error。

该要求的最小结构合同固定如下，字段不得仅由实现内部对象隐含：

```text
ContextRef
├── session_id, ref_type=canonical_item
│   ├── ref_id=item_id, item_sequence, semantic_kind, payload_kind, status
│   ├── source_revision
│   ├── content_length       # payload canonical bytes；不是 JSONL line length
│   └── content_hash
└── ref_type=request_only
    ├── session_id, ref_id=plan_item_id, plan_id
    ├── source_ref/detail_ref? # draft detail_ref NULL/deferred
    ├── source_revision?, content_length? # required only for included selection
    ├── content_hash? / redacted_stable_digest? # included=true exactly one; draft/omitted optional at most one
    └── protection, availability

ContextContribution
├── contribution_id, contribution_kind, request_only=true, body/detail_ref
├── source_revision, content_length
├── content_hash? / redacted_stable_digest? # exactly one
├── protection, visibility
└── ordinal_binding?={assembly_id, contribution_ordinal} # 仅在 sealed assembly selection 中存在

ToolSetRef
├── ref_type=tool_set        # ToolSetRef discriminator；不属于 ContextRef.ref_type
├── ref_id=tool_set_snapshot_id
├── plan_id, assembly_id?    # plan registry required; assembly only after seal
├── source_revision
├── tool_set_schema, tool_set_schema_version
├── tool_policy_version
├── content_length           # tool manifest 的 JCS UTF-8 bytes
├── content_hash? / redacted_stable_digest? # exactly one
├── protection=public|redacted|protected
└── availability=available|unavailable|forbidden|expired

ContextSelectionEntry
├── assembly_id, plan_ordinal, ref={ref_type, ref_id}, selection_kind
├── included, omission_reason?, loss[]
├── visibility, protection, availability
├── source_revision?          # required iff included=true; known iff manifest agrees
├── content_length?           # logical body length; required iff included=true
├── content_hash? / redacted_stable_digest? # included=true exactly one; included=false optional at most one
├── detail_ref?               # required iff included=true and request-only
├── contribution_id?          # required iff included=true and contribution-backed; forbidden canonical/tool_set
├── contribution_ordinal?     # required iff included=true and contribution-backed
├── base_delta_role=none|base|delta
└── source_overlay_epoch?

ContextRequestPlan
├── plan_id
├── plan_state=unsealed|sealed
├── assembly_id?             # NULL before successful Saver seal
├── history_view_revision, source_overlay_epoch
├── refs[], tool_set_refs[], contributions[] # registries; not ordering authorities
└── selection[]              # empty before seal; sole ordered selection

ContextAssemblySnapshot
├── plan_id, assembly_id      # both retained; distinct scopes
├── sealed_plan
├── selection[]               # exact immutable copy of plan.selection
├── ref_manifest[], tool_set_manifest[], contribution_manifest[]
└── plan_hash, request_hash, loss
```

`selection_kind` 与 `ref_type` 的兼容矩阵在本 spec 中冻结如下，适用于 included 和 omitted entry；omitted entry 仍必须保留该行的 tag/id union compatibility，只是不解析 source body。矩阵外组合在 source dereference 前返回 `plan-order-integrity`，不得按当前 registry 或 wire role 猜测生命周期：

| `selection_kind` | 唯一合法 ref | included 条件 | omitted 条件与行为 |
|---|---|---|---|
| `canonical_history` | `ContextRef.ref_type=canonical_item` | 仅解析同 session `item_catalog`；source revision、logical length、恰一个 hash token 必填；`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 必须 NULL，`base_delta_role=none` | 仍保留 `canonical_item` tag/id、plan ordinal、omission/loss/availability 和可得 identity；history/restore 跳过 canonical message，不读当前 item |
| `request_only` | `ContextRef.ref_type=request_only` | `detail_ref` 必须同 assembly；若 contribution-backed，`contribution_id`/`contribution_ordinal` 必填且唯一指向同一 plan 的 contribution manifest；`base_delta_role=none` | 保留 `request_only` tag/id、plan ordinal、omission/loss/availability；detail、正文完整性字段和 contribution binding 可空，不回退当前 middleware/source |
| `overlay_base` | `ContextRef.ref_type=request_only` | 必须 contribution-backed，非空 `contribution_id`/`contribution_ordinal`/detail/source revision/length/hash；`base_delta_role=base`、`source_overlay_epoch` 必填，并绑定完整 base | 保留 request-only tag/id、plan ordinal、`base_delta_role=base` 及可得 epoch/identity；不应用 delta/base，不以当前 source 替代 |
| `overlay_delta` | `ContextRef.ref_type=request_only` | 与 overlay_base 相同，`base_delta_role=delta`、`source_overlay_epoch` 必填，另校验 `from_revision`/`to_revision`/diff algorithm/version/`diff_hash` chain | 保留 request-only tag/id、plan ordinal、`base_delta_role=delta` 及可得 epoch/identity；不应用 delta、不重建 overlay |
| `tool_set` | `ToolSetRef.ref_type=tool_set` | 仅解析同 plan/assembly ToolSetSnapshot manifest；`base_delta_role=none`，`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 必须 NULL | 仍保留 `tool_set` tag/id、plan ordinal、omission/loss/availability 和可得 identity；history/restore 不生成工具定义，Provider 不回退 registry 或空 tools |

`ContextRef.ref_id` 仍是 canonical `item_id` 或 request-only `plan_item_id`，不是 contribution identity；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref` 与 source manifest 定位正文/detail及其 source revision、length/hash，不要求 contribution identity；included 且 contribution-backed 的 request-only/overlay 才必须用非空 `contribution_id` + `contribution_ordinal` 唯一定位 `contribution_manifest` 的正文/detail、source revision、length/hash 与 ordinal，且 overlay 按矩阵必须 contribution-backed。canonical/tool_set 严禁 contribution_id/ordinal。缺失、重复或 selection kind 与 union tag 不匹配统一返回 `plan-order-integrity`；source/hash mismatch 返回 `source-mismatch`，request-only detail/contribution 不可用返回 `detail-unavailable`；required source 的 omission 直接拒绝 seal/dispatch。

`ref_type` 是本 change 在 plan、snapshot、SQLite assembly ref 和 projector 输入中的唯一序列化判别字段，闭合集合只有 `canonical_item | request_only`；不得持久化 `ref_kind` 或 `request_only` boolean 作为 ContextRef/selection discriminator，也不得把 `item_id`/`plan_item_id` 当作第二个判别字段。统一身份字段是 `ref_id`：canonical ref 的 `ref_id` 必须等于 immutable item 的 `item_id`，request-only ref 的 `ref_id` 必须等于 plan 内 target-local 的 `plan_item_id`。ContextRef 显式带 `session_id`；canonical ref 的 scope 是 `(session_id, item_id)`，request-only draft ref 的 scope 是 `(session_id, plan_id, ref_id)`。ContextRef 不承担 assembly binding：draft 中 `assembly_id` 不存在/必须为 NULL，只有 sealed `ContextSelectionEntry.assembly_id`、`ref_manifest` 和 detail manifest 才建立 `(session_id, assembly_id)` scope。request-only ref 的 `detail_ref` 在 draft 中必须为 NULL 或仅表示待 seal 的 source detail；Saver seal 时才解析/物化为 `{session_id, assembly_id, detail_id}`，并逐字段写入 selection/manifest。optional omission 的 selection ref 仍必须保留 tag/id，但可作为未解析完整 manifest 的 typed identity stub；其 source revision、length、hash、detail_ref 可为空，已知值必须与 manifest 一致，且不得被 restore/projector 当作可读正文。旧模型若仍使用 `ref_kind`/boolean，只能由 legacy ingress adapter 在边界处归一化，后续 composer、storage、snapshot 和 projector 不得继续消费别名。canonical ref 必须且只能解析到同一 session 已提交 `item_catalog` item，但该完整解析要求只适用于 `included=true`；optional `included=false` 的 canonical tag/id stub 不声称 item 已可读。request-only ref 在 unsealed 阶段只能解析到同一 `(session_id, plan_id)` registry；只有 `included=true` 的 sealed entry 才解析到该 assembly 的 contribution/detail manifest，并由显式 `contribution_id` 定位 contribution，`included=false` typed identity stub 不解析 assembly detail/contribution；同一 scope 内不能以另一类型重复注册。

工具集合使用独立的 `ToolSetRef`，不扩展 `ContextRef.ref_type`：`ContextRef` 的闭合集合仍只有 `canonical_item | request_only`，而 selection 的 `ref` 是一个 tagged union，可为一个 `ContextRef` 或一个 `ToolSetRef`。`ToolSetRef.ref_type=tool_set` 仅在 `selection_kind=tool_set` 时合法，`ref_id` 必须解析为同一 `(session_id, plan_id)` registry 中的 target-local `tool_set_snapshot_id`；unsealed registry 的 `assembly_id=NULL`，seal 后的 selection binding 必须带当前 `assembly_id`。ToolSetRef/ToolSetSnapshot manifest 必须保存非空 `source_revision`、`tool_set_schema`、`tool_set_schema_version`、`tool_policy_version`、逻辑 `content_length`、恰好一个 `content_hash`/`redacted_stable_digest`、`protection` 和 `availability`；hash/length 覆盖 `{ "tool_set_schema":"tool-set-ref", "tool_set_schema_version":"v1", "tools":<按稳定 tool_id 排序的 schema/config entries>, "tool_policy":<规范 policy>, "tool_policy_version":"v1" }` 的 RFC 8785 JCS UTF-8 bytes，普通 hash 使用 `sha256:jcs:v1`，受保护正文由 protected manifest 保存内部 hash 并向普通 reader 暴露 stable digest。每个 included tool-set selection entry 必须逐字段等于 `tool_set_manifest[]`，`base_delta_role=none`、不绑定 `contribution_ordinal`，但与其它 entry 共用 assembly 内唯一的 `plan_ordinal`；optional omitted tool-set entry 只保留 `ref_type=tool_set`/`ref_id`、plan ordinal、omission/loss、availability 和可得 identity，manifest 正文字段、detail_ref 与 contribution ordinal 可以为 null/未分配，已知 metadata 必须与 registry 一致且不投影 tools。缺失、重复、类型/selection_kind 不匹配或 manifest 不一致返回 `plan-order-integrity`，source/hash/length/schema/policy version 不一致返回 `source-mismatch`，不可用/无权限返回 `detail-unavailable`。ToolSetRef 只投影到 Provider 的 tools/tool-config；history 仅消费受 visibility 策略允许的 selection metadata/摘要，不生成 canonical message、Turn item 或 request-only 正文。

`ContextSelectionEntry.contribution_id` 是 request-only/overlay contribution 到 `contribution_manifest` 的唯一显式映射：included 且 contribution-backed 时必须非空，且只能解析同一 `(session_id, plan_id)` registry 的一个 `contribution_id`；该 manifest 必须进一步提供正文或最终 `detail_ref`、source revision、logical content length、hash token 与 assembly-bound `contribution_ordinal`。`ContextRef.ref_id` 仍是 request-only 的 `plan_item_id`，不等于也不替代 `contribution_id`；restore、LangChain/native Provider/Web history 必须按 selection entry 的 `contribution_id` 读取对应 manifest/body，再校验 entry 与 manifest，不得按 ref_id、detail_ref、hash 或 ordinal 搜索/猜测。canonical_history 与 tool_set entry（包括 omitted）的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为 NULL；只有 omitted request-only/overlay entry 可保留已有 `contribution_id`，且必须与同一 manifest 一致，不得新分配 contribution_id/ordinal 或触发正文/detail 读取，没有既有映射则为 NULL。

`ContextContribution` 固有就是 request-only contribution：其持久化字段 `request_only` 必须存在且恒为 `true`，只作为 contribution manifest 的不变量，不能用来判别 ContextRef union；缺失或为 `false` 必须拒绝。`ContextRef` 和 selection 仍禁止该 boolean，必须使用 `ref_type=request_only`。`ContextContribution.contribution_kind` 的闭合集合只有 `prompt | overlay_base | overlay_delta | notice`，不存在合法的 `tool_set` contribution。Provider tool definitions 只能通过同一 plan/assembly 的 ToolSetSnapshot/ToolSetRef manifest 绑定；`ContextContribution`、request-only body、`contribution_ordinal` 和普通 ContextRef 都不得代表 tool definitions。v2 遇到 `contribution_kind=tool_set` 必须返回 `contribution-kind-unsupported`；只有一次性 `legacy_import_v1_to_v2` migration reader 可以原样保留到 migration report/quarantine，但不得转换为 ContextContribution、ToolSetRef 或 Provider tools，也不得被正常 runtime 调用。

`ContextRequestPlan` 的生命周期和 identity 也属于本结构合同：`create_context_plan` 先在 session namespace 中创建唯一 `plan_id`，状态为 `unsealed`；未 seal 的 plan 可以登记 `refs[]`、`tool_set_refs[]`/`contributions[]`，但必须保持 `assembly_id=NULL`、`selection=[]`，因此不产生 `ContextSelectionEntry`、`plan_ordinal` 或可 dispatch 的 assembly。draft registry 的 ContextRef 约束为 `UNIQUE(session_id, plan_id, ref_type, ref_id)`；canonical ref 可被多个 plan 查询复用，但 request-only ref 不得跨 plan 复用。draft ContextRef 不解析 assembly/detail physical path，新增 detail 只留在内存 ledger 或 source typed ref。Saver 只有在 seal preflight 通过 ContextRef/ToolSetRef/contribution registry manifest 的 source、length、hash、visibility/protection 和顺序校验后，才在同一提交边界分配 session-local `assembly_id`，生成 selection/`plan_ordinal` 与 assembly-bound `contribution_ordinal`，将 required request-only body 写入 detail store，并保存最终 detail_ref 与 `ContextAssemblySnapshot`。seal 失败不得留下可用 assembly、selection 或 detail binding；原 plan 仍为 `unsealed`，可以修正后重试。`(session_id, plan_creation_idempotency_key)` 负责 plan 创建幂等，`(session_id, plan_id, seal_idempotency_key)` 负责 seal 幂等；相同 preimage 返回原 identity，不同 preimage 分别返回 `plan-idempotency-conflict` 或 `assembly-idempotency-conflict`，不得把 `plan_id` 当成 `assembly_id`。跨 session fork 不得复用 source 的 plan/ref/detail identity，必须在 target session 创建 target-local plan/assembly/ref/detail，并仅在 fork lineage/audit 保存 source GlobalEntityRef。成功后 plan 与 snapshot 保留两个 identity，逐字段不可变且一对一；新的 selection 必须创建新的 plan/assembly。未选择任何 source 的 plan 只有在已分配 assembly 后才允许以空 `selection` seal，空 selection 仍属于该 assembly scope。

`ContextRef.content_length` 的来源按 selection union 分流：`ref_type=canonical_item` 的 `canonical_history` 只能来自已提交 `item_catalog.payload_length`；`ref_type=request_only` 的 request-only/overlay selection 必须来自同一 assembly 的 sealed detail/contribution source manifest；`ToolSetRef.ref_type=tool_set` 的 tool_set selection 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest。三者都不能从 JSONL line offset/length、wire message 长度或当前文件猜测或替代。`ContextRequestPlan.refs[]`、`.tool_set_refs[]`/`.contributions[]` 只是 source registries，`selection[]` 是唯一的顺序、inclusion 和 loss authority；selection entry 只能在对应 `(session_id, assembly_id)` scope 中存在。每个 entry 必须恰好解析到一个 tagged-union source ref：非 `tool_set` selection 解析到一个 `ContextRef`，`selection_kind=tool_set` 解析到一个 `ToolSetRef`，且同一 ref/contribution/tool-set snapshot 不得在一个 assembly 重复选择。所有 entry 的 `ref_type`、`ref_id`、visibility、protection、availability、base/delta role 和 `source_overlay_epoch` 必须逐字段等于对应 registry/manifest；只有 `included=true` 时才强制 `source_revision`、逻辑 `content_length` 和恰一个 `content_hash`/`redacted_stable_digest`，且 request-only 才强制非空、同 assembly 的 `detail_ref`，contribution-backed entry 才强制 `contribution_ordinal`。`included=false` 仅允许 optional omission，仍保留 tagged source ref、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 source identity；source revision、length、hash token、detail_ref、contribution_ordinal 可以为 null/未分配，已知 metadata 必须逐字段等于 manifest。缺项、重复、union 类型/selection_kind 不匹配或任意顺序/绑定不一致返回 `plan-order-integrity`，canonical 或 tool-set source 不一致返回 `source-mismatch`，request-only detail 不可用或不一致返回 `detail-unavailable`。`selection_kind=tool_set` 不得引用 `item_catalog`、普通 `ContextContribution` 正文或 ContextRef，ToolSetRef 的正文只来自 tool schema/config manifest 及其 hash。`ContextAssemblySnapshot` 持有 seal 时 selection 的不可变副本以及 `ref_manifest[]`/`tool_set_manifest[]`；`assembly_item_refs` 必须以 `UNIQUE(assembly_id, plan_ordinal)`、`UNIQUE(assembly_id, ref_type, ref_id)`、`UNIQUE(assembly_id, contribution_id)` 和 `UNIQUE(assembly_id, contribution_ordinal)` 保护一致性，omitted entry 不分配 detail/contribution ordinal。plan registry 还必须以 `UNIQUE(session_id, plan_id, tool_set_snapshot_id)` 防止 ToolSetRef 重复。`contribution_ordinal` 是 assembly binding，不是 contribution 跨 assembly 的全局属性；同一 contribution 在不同 assembly 重新绑定时取得新的 ordinal，但同一 sealed assembly 内不可重写。三种 projector 和 history 只能消费该 selection 副本，不得重新从 registry 排序或 prepend；omitted canonical/request-only/tool_set entry 只保留 metadata/loss，分别跳过正文、detail 和工具定义，不从当前 source 回退或生成空值。

`ContextContribution.content_hash` 的正文 preimage 固定为 `{ "contribution_kind": <contribution_kind>, "body": <typed body> }` 的 `sha256:jcs:v1` RFC 8785 JCS bytes；`body` 在 draft 可以来自内存 ledger 的 inline typed value 或 typed source ref，sealed manifest 必须从已解析的 body/detail_ref 复核。preimage 不包含 contribution identity、ordinal、assembly、时间或 wire role。敏感/受保护正文不能暴露普通 hash 时，ref 使用恰一个 `redacted_stable_digest=hmac-sha256:session:v1:<64位小写hex>`，protected manifest 仍保存内部 content hash 并完成校验；canonical item ref 不使用该替代 token，始终使用 immutable item `content_hash`。

#### Scenario: 统一 selection 被三个读取面复用

- **WHEN** Saver 为同一 active view 生成 LangChain restore、native Provider request 和 Web history projection
- **THEN** 三者读取相同的 `selection[{plan_ordinal, ref}]`；`ContextRef` 负责 canonical/request-only/overlay source，`ToolSetRef` 负责 `selection_kind=tool_set`，并按相同的 `contribution_ordinal` 应用 request-only 与 base→delta overlay。LangChain/native Provider 只能把 ToolSetRef 编码到 tools/tool-config，history 只能保留受策略控制的 ToolSetRef metadata；只能在 wire 编码阶段合并角色，不得改变 selection 或把工具定义变成 message

#### Scenario: 重启后正文与 ordinal 一致

- **WHEN** 进程在 assembly seal 后重启，或 `included=true` source file 被覆盖、删除或替换
- **THEN** Saver 依据已提交 ref、source revision、length 和 content hash 恢复原正文与 ordinal；缺失/覆盖/错误 source 返回 `source-mismatch` 或 `detail-unavailable`，不发送错误正文，不静默 loss
- **AND** `included=false` 的 optional omission 只恢复 tagged ref、plan ordinal、omission/loss/availability 和可得 identity metadata，不尝试恢复正文或 ordinal，也不把 omission 当作 source mismatch

#### Scenario: contribution-backed request-only ref 唯一定位正文

- **WHEN** 一个 sealed plan 同时包含多个 request-only/overlay contribution，且每个 included selection entry 的 `ref_id` 都是独立的 `plan_item_id`
- **THEN** 每个 entry 通过非空 `contribution_id` 唯一解析到同一 `(session_id, plan_id)` 的 `contribution_manifest`，再由该 manifest 定位正文或最终 `detail_ref`、source revision、逻辑 length、hash token 与 `contribution_ordinal`
- **AND** restore、LangChain/native Provider 和 Web history 按 `plan_ordinal` 读取同一 entry/manifest mapping；缺失、重复、ref/contribution 不一致或 ordinal/body/hash 不匹配返回 `plan-order-integrity`、`source-mismatch` 或 `detail-unavailable`，不得按 `ContextRef.ref_id`、detail_ref、hash 或 ordinal 猜测另一条 contribution
- **AND** canonical_history/tool_set entry（包括 omitted）的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为空；只有 omitted request-only/overlay entry 即使保留已有 `contribution_id` 也只校验同一 manifest identity，不新分配 contribution_id/ordinal，不读取正文/detail；没有既有映射则为 NULL

### Requirement: selection_kind 与 ref_type 必须使用唯一兼容矩阵

系统 SHALL 在任何 source lookup、detail 解析或 projector/restore 之前按下表校验 `ContextSelectionEntry.selection_kind` 与 tagged-union `ref`；`included=true` 和 `included=false` 都必须满足同一行的 tag/type 关系。矩阵外组合必须返回 `plan-order-integrity`，不得根据 payload、wire role、`ref_id` 或当前 registry 猜测另一种生命周期：

| `selection_kind` | 唯一合法 ref | `included=true` 合同 | `included=false` optional 合同及读取行为 |
|---|---|---|---|
| `canonical_history` | `ContextRef.ref_type=canonical_item` | 只能解析同一 session 的 `item_catalog` item；source revision、logical content length、恰一个 hash token 必填；`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 为 NULL，`base_delta_role=none` | 仍保留 `canonical_item` tag/id、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 item identity；正文不解析、不生成 canonical message |
| `request_only` | `ContextRef.ref_type=request_only` | `detail_ref` 必须解析到同 assembly 的 sealed detail；若 contribution-backed，非空 `contribution_id`/`contribution_ordinal` 必须唯一指向同一 plan 的 contribution manifest；`base_delta_role=none`、`source_overlay_epoch` 为 NULL | 仍保留 `request_only` tag/id、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 identity；detail、正文完整性字段及 contribution binding 可 NULL/未分配，不回退当前 middleware/source |
| `overlay_base` | `ContextRef.ref_type=request_only` | 必须 contribution-backed；`contribution_id`、`contribution_ordinal`、detail、source revision、logical length、恰一个 hash token、`base_delta_role=base`、`source_overlay_epoch` 必填，并绑定完整 base | 保留 request-only tag/id、`plan_ordinal`、`base_delta_role=base` 及可得 epoch/identity；不应用 base、不以当前 source 替代 |
| `overlay_delta` | `ContextRef.ref_type=request_only` | 必须 contribution-backed；`contribution_id`、`contribution_ordinal`、detail、source revision/length/hash、`base_delta_role=delta`、`source_overlay_epoch` 必填，并校验 `from_revision`/`to_revision`/diff algorithm/version/`diff_hash` chain | 保留 request-only tag/id、`plan_ordinal`、`base_delta_role=delta` 及可得 epoch/identity；不应用 delta、不重建 overlay |
| `tool_set` | `ToolSetRef.ref_type=tool_set` | 只能解析同一 plan/assembly 的 ToolSetSnapshot manifest；manifest source/length/hash/schema/policy 字段必填；`base_delta_role=none`，`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 为 NULL | 仍保留 `tool_set` tag/id、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 identity；history/restore 不生成工具定义，Provider 不投影该 tool set、不回退 registry 或空 tools |

`ContextRef.ref_id` 的既定身份不因该矩阵改变：canonical 使用 `item_id`，request-only 使用 `plan_item_id`；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref`/source manifest 解析；included 且 contribution-backed 的 request-only/overlay 才必须通过显式非空 `contribution_id` + `contribution_ordinal` 定位 contribution 正文、detail、source revision、length、hash 和 ordinal，overlay 本身必须 contribution-backed，不得从 `ref_id`、detail_ref、hash 或 ordinal 反推。所有 omitted entry 必须保留其行规定的 tag/type，即使 source 不可解析也不能改派为另一种 ref；required source 的 omission/detail failure 仍必须拒绝 seal/dispatch。projector、restore 和 history 对 omitted entry 只保留 omission/loss metadata，分别跳过 canonical 正文、request-only detail/body、overlay 应用和 tool definition。

#### Scenario: selection union mismatch 在 source lookup 前失败

- **WHEN** `canonical_history` 携带 `ref_type=request_only`、`request_only|overlay_base|overlay_delta` 携带 `ref_type=canonical_item`，或 `tool_set` 携带非 `ToolSetRef.ref_type=tool_set`
- **THEN** Saver 在读取 item catalog、contribution/detail 或 ToolSetSnapshot 之前返回 `plan-order-integrity`；不创建可 dispatch 的 projection，不用另一种 ref 类型修复 selection

#### Scenario: omitted entry 保留 tag/type 但不解析 source

- **WHEN** optional source 形成 `included=false` selection entry
- **THEN** entry 仍满足上述 selection_kind/ref_type 矩阵并保留 `assembly_id`、`plan_ordinal`、`omission_reason`、`loss`、`availability` 及可得 identity；omitted canonical_history/tool_set 的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为 NULL；omitted request-only/overlay 可保留已有且与同一 manifest 一致的 `contribution_id`，但不得新分配 contribution_id/ordinal 或读取正文/detail，没有既有映射则为 NULL；其它正文 length/hash、detail_ref、contribution_ordinal 可不分配
- **AND** LangChain/native Provider/Web history 与 restore 只报告 omission/loss 并跳过相应正文/工具定义，不从当前 source、registry 或空值回退

### Requirement: 每次 Provider 请求必须形成可审计的 ContextAssemblySnapshot

系统 SHALL 为每次 model call 形成在 dispatch 前 sealed 的 `ContextAssemblySnapshot`，记录 `plan_id`、`assembly_id`、`turn_id`、`execution_id`、`model_call_id`、active context view、按逻辑顺序排列的 canonical/request-only `ContextRef` 与 `ToolSetRef` references、应用的 contribution references/order、每个引用的 included/omission reason、tool set manifest、目标 provider/model、编译器版本、`hash_algorithm`、plan/request hash、可见性策略和 loss/redaction 结果。若使用缓存保持型 source overlay，还 MUST 记录 `history_view_revision`、`source_overlay_epoch`、base source revision/reference、按序 delta references、target/materialized revision、materialization reason 和 overlay hash。sealed snapshot 的 plan 内容不可变；其 lifecycle outcome 可以单独记录为 completed、completed_empty、failed、interrupted 或 unknown。实时请求失败、中断、空输出或执行丢失时也 MUST 能区分“canonical item 未提交”和“request-only overlay 已应用”；snapshot 不得反向成为 canonical history。

本合同中的 detail store 物理路径冻结为 resolved session node 下的 `rollout/context-plan-details/<assembly_id>/<detail_id>`。`detail_id` 是 assembly 内 target-local 的不可变物理叶名；`detail_ref` 是规范化为 `{session_id, assembly_id, detail_id}` 的逻辑 typed reference，由 session catalog/path resolver 唯一解析，不是物理路径别名。所有 fork 都必须生成 target-local detail_id/detail_ref 并保留 source ref 到 target ref 的 lineage；source path 不得被 target reader 直接使用。任何父级 symlink、realpath containment 越界、敏感 detail 普通 plaintext、required detail 缺失或 source/content hash 不匹配都必须在 seal/dispatch 前显式失败。

assembly snapshot metadata MUST 在 provider dispatch 前通过 `RolloutCheckpointSaver` 持久化；业务层和 projector 只能消费 Saver 提供的已提交 plan/snapshot，不得直接扫描 `RolloutStorage`、`AppendWriter` 或内部 context reader。若 snapshot 或 required detail reference 无法持久化，系统不得发起 provider 请求。provider 结果、canonical item、Turn finalization 和 assembly outcome 的提交 MUST 遵守 sealed-before-dispatch 与 terminal convergence 两阶段 JSONL/SQLite 收敛边界；没有 item 时使用 `commit_kind=terminal_convergence`、`commit_mode=metadata_only`，不能另造 `metadata_only` commit kind。`plan_hash` 必须是 provider-neutral canonical plan 的 hash，同一 plan 在不同 provider projector 中可比较；`request_hash` 必须覆盖具体 projector 的规范化 request，但排除 provider request ID、时间戳、认证和 retry identity，仅在相同 projector/provider profile 内用于 exact replay。request-only prompt、middleware 输入和完整渲染 prompt 只有在需要精确重放时才进入工作区会话节点内有界、受保护的 detail store；SQLite 只保存 detail reference、长度、hash、retention 和 availability，detail store 不得成为 canonical item 正文的第二事实源。

#### Scenario: 同一历史生成不同 Provider 请求

- **WHEN** 同一个 active view 分别编译为 LangChain request 和原生 Provider item request
- **THEN** 两个 assembly snapshot 共享相同的 item identity/order 和 provenance，但分别记录目标 codec、tool schema 和 loss 结果

#### Scenario: middleware 详情预留

- **WHEN** 将来某个扩展按 item 请求显示“由哪个 middleware 产生、使用了哪个版本和哪些输入”
- **THEN** 服务可以通过 assembly/item/source reference 展开受权限和 retention policy 限制的详情；本 capability 不要求现在实现具体前端页面

#### Scenario: assembly 持久化失败时禁止请求

- **WHEN** ContextRequestPlan 已生成，但 sealed assembly 或其必要的 source/hash metadata 无法持久化
- **THEN** 系统拒绝发起 Provider request，并返回可诊断的 assembly persistence error，不使用未记录来源的临时请求继续执行

#### Scenario: required detail 写入失败

- **WHEN** exact replay 或 provider dispatch 所必需的 ContextPlanDetailStore 内容写入失败、hash 校验失败或无法绑定到当前 session/assembly
- **THEN** sealed assembly 提交失败，Provider request 不发起；非必需 detail 的失败必须作为 sealed metadata 中的 `availability=unavailable` 显式返回

#### Scenario: Provider 调用后在提交前崩溃

- **WHEN** Provider 已收到 request，但进程在 canonical output、Turn outcome 或 assembly terminal outcome 收敛前退出
- **THEN** 重启将该 model call/assembly 标记为 `unknown` 或 `execution_lost`，不得伪造 completed/final item；JSONL 已写但 SQLite 未提交的 item 对 reader 不可见

#### Scenario: detail store 丢失

- **WHEN** 历史仍有 assembly/source reference，但有界 detail store 中的完整 prompt 或 middleware 输入已过期或不可读
- **THEN** 默认历史仍可读取 canonical item 和安全 provenance summary，并明确返回 detail-unavailable，不从当前 middleware 配置重新伪造旧详情

#### Scenario: detail store 拒绝敏感原文和父级 symlink

- **WHEN** `sensitive=true` 的 detail 试图写入普通文件，或从 resolved session node 到 assembly/detail target 的任一父组件是 symlink、realpath 不在该 session/workspace containment 内
- **THEN** write/read/root 统一返回 detail security/path error；系统只能保存 redacted marker 或通过显式 protected/encrypted storage 保存受控正文，不得落盘或读取普通 plaintext，也不得跟随父级 symlink

#### Scenario: contribution source hash 校验

- **WHEN** Saver 恢复 `ContextRef` 或 `ContextContribution`，但正文、`source_revision`、逻辑 `content_length` 或 `content_hash`/`redacted_stable_digest` 与 sealed plan 不一致
- **THEN** projection/dispatch 返回 `source-mismatch` 或 `detail-unavailable`，不发送当前覆盖文件或错误 source 的正文；仅有 ref、hash 或 metadata 不算正文可用

#### Scenario: plan ordinal 决定跨 projector 顺序

- **WHEN** LangChain、native Provider 和 Web history 从同一 snapshot 恢复 canonical item、request-only contribution 及 base→delta overlay
- **THEN** 三者按 Saver 冻结的 `selection[{plan_ordinal, ref}]` 读取，并按持久 `contribution_ordinal` 恢复 contribution 顺序；不得按 `created_at`、`contribution_id`、物理邻接或无条件 prepend request-only refs，顺序冲突必须显式失败

#### Scenario: Saver 是 plan owner

- **WHEN** 业务 service、LangChain projector 或 Provider projector 需要构造请求上下文
- **THEN** 它只能消费 RolloutCheckpointSaver 返回的已提交 context view/plan/snapshot 和显式 runtime contribution；直接访问 RolloutStorage、AppendWriter 或内部 context reader 必须被拒绝

### Requirement: 缓存保持型 source overlay 必须保留稳定基线与增量

对于已经进入某次 sealed request 的 workspace instruction、skill metadata/body 或其它可变 runtime source，系统 SHALL 以 source revision/hash 建立缓存保持型 overlay。首次已应用的完整 revision 是 `base_ref`；后续 revision 只能追加有序 `delta_ref`，每个 delta 必须记录 `from_revision`、`to_revision`、diff algorithm/version、diff hash、source reference、`source_overlay_epoch` 和稳定幂等键。`ContextRequestPlan` MUST 分别携带 `history_view_revision` 与 `source_overlay_epoch`，并同时携带 base 与 delta refs；projector 先保留稳定 base、再按 revision 顺序编码 delta。middleware 不得通过编辑既有 LangChain message、system prompt 或 canonical base item 实现该行为。

每个 `included=true` 的 `base_ref`/`delta_ref` 都必须展开为完整的 source integrity manifest，而不是只保存 ref 字符串：至少包括 `source_revision`、`content_length`、`content_hash` 或 `redacted_stable_digest`、`source_overlay_epoch`、`base_delta_role`、assembly-bound `contribution_id`/`contribution_ordinal` 和 plan-bound `plan_ordinal`。`contribution_id` 必须唯一解析对应的 `ContextContribution`，不能由 overlay ref、detail_ref 或 ordinal 推断。optional overlay omission 仍保留 tagged ref、plan ordinal、omission/loss、availability 和可得 source identity，但 contribution_id、detail、正文 length/hash 与 contribution ordinal 可以 null/未分配，且不得被应用为 delta；若 omitted entry 保留 contribution_id，必须与 manifest 一致。base 的 hash/length 覆盖完整 source body；included delta 的 `diff_hash` 覆盖规范化 diff body，同时另有其 source body `content_hash`/length，`from_revision`/`to_revision` 必须与 source revision chain 相接。included manifest 缺失或与 sealed snapshot 不一致，恢复必须返回 `source-mismatch`/`detail-unavailable`；omitted entry 只恢复 omission/loss metadata，均不得读取当前文件猜测 overlay。

需要跨 checkpoint 或后续请求恢复的 delta MUST 追加为 `semantic_kind=runtime_notice`、`turn_scope=ambient`、`payload_kind=structured_content` 的 canonical item，并通过 `supersedes`/`materializes` relation 连接 source revision；只对当前 request 有效的 delta 可以保持 request-only，不能因此伪造 canonical item。overlay item 不得成为 Turn root/member，也不能因 wire role 为 `user` 而改变 Turn 顺序。多次 source change 必须形成 `A(base) -> B(delta) -> C(delta)` 的可恢复链，不得把 C 的 diff 错当作 A 的完整内容。

rewind、replay、fork、checkpoint restore 和 compaction MUST 先产生新的 `history_view_revision`，并在下一次请求前执行 context reconciliation；它们不因历史尾部变化自动推进 `source_overlay_epoch`。只要 source base/delta 仍兼容，reconciliation MUST 复用原 overlay；即使 delta item 随历史尾部被新 view 隐藏，也必须从独立的 ambient overlay lineage 重新注入。只有 source base/detail 不可恢复、overlay 链需要压缩、source 删除/权限或 policy boundary 改变、Provider/projector 缓存前缀确实不兼容，或显式 source refresh 时，才推进新的 `source_overlay_epoch` 并物化当前完整 revision。物化通过新的 source contribution/item、context view 和 assembly 引用表达，旧 base/delta 的 JSONL 正文与 provenance 保持不可变。普通 source edit 本身不强制物化；sealed assembly 之后观察到的变化只影响下一次 assembly。

每次会改变运行时上下文的操作都必须比较 history view 与 source overlay 两类状态，并形成 reconciliation outcome：`history_view_changed`、`overlay_reused`、`delta_appended`、`overlay_materialized` 或 `overlay_invalid`。只有 `overlay_materialized` 才改变 source base。source 不可读、权限改变、revision/hash mismatch 或 detail 缺失时，系统必须显式返回 source-mismatch/detail-unavailable，不能用当前 source 静默重建旧 request。

#### Scenario: `AGENTS.md` 变化不破坏稳定缓存前缀

- **WHEN** request A 已使用并 seal 了 `AGENTS.md` 的 revision A，随后文件变为 revision B
- **THEN** 下一次普通 request 继续引用 A 作为 base，并追加带 A→B provenance 的 delta；不得直接把 system base 替换为 B，也不得把未标注来源的 `HumanMessage` 当作 delta

#### Scenario: rewind 到 diff 之前仍重新注入当前 source delta

- **WHEN** source overlay 已有 `A(base) + A→B(delta)`，用户 rewind 到不包含 A→B delta item 的较早 history view
- **THEN** 新 assembly 使用 rewind 后的 history view，但复用同一 source overlay epoch，并从 ambient overlay lineage 重新注入 A→B delta；不得因为 delta 不在新 view 的历史范围内而静默丢失，也不得因此物化新的 source base

#### Scenario: 仅历史尾部变化不物化 source base

- **WHEN** rewind、history-prefix replay 或 compaction 只改变 canonical history 的选择，且 source base、delta 和 projector cache contract 仍可恢复
- **THEN** 系统只更新 `history_view_revision` 并生成新的 assembly/plan，`source_overlay_epoch` 与 base/delta identity 保持不变

#### Scenario: source overlay 自身失效才物化

- **WHEN** source base detail 缺失、overlay 链需要压缩或 source policy/Provider cache contract 发生不兼容变化
- **THEN** reconciliation 返回 `overlay_materialized`，推进新的 source overlay epoch 并建立当前完整 source revision；历史 view 的变化本身不作为物化理由

#### Scenario: 多次变化保留有序 delta 链

- **WHEN** 在物化前 source 依次从 A 变为 B、再变为 C
- **THEN** plan 保留 A base、A→B 和 B→C 两个有序 delta，使用每个 delta 的稳定幂等键；重启后可以按引用恢复到 C，而不要求重新读取当前文件

#### Scenario: source overlay 物化最新 source revision

- **WHEN** source overlay 链需要压缩、source/detail 失效或其它明确的 source cache boundary invalidation 发生，且当前 source revision 为 C
- **THEN** 系统创建以 C 为完整 base 的新 `source_overlay_epoch`，新的 history view 可以独立保持或变化；旧 A base 与增量只保留为不可变审计记录，不在新的 active plan 中重复应用

#### Scenario: source 未进入 request 时不制造 diff

- **WHEN** workspace skill 的 `SKILL.md` 在当前 request 中从未被加载或使用，随后只发生 body 变化
- **THEN** 系统不为该未使用 source 额外注入 delta；只有已进入 request 的 skill metadata/body 才建立 base/delta lineage

#### Scenario: source 详情不可用时显式失败

- **WHEN** overlay 需要的 source revision/detail 被删除、越权或 hash 校验失败
- **THEN** 普通历史仍可读取已提交 canonical item 和安全 provenance，但 exact replay/依赖该 overlay 的新 assembly 返回明确的 source-mismatch 或 detail-unavailable，不静默使用当前 source

### Requirement: 历史 view 变化与运行时 source 变化必须独立重协调

系统 SHALL 将一次运行时上下文表示为相互独立的 `history_view_revision`、`source_overlay_epoch`、source revision set、tool/policy snapshot 和 assembly identity。rewind、replay、fork、checkpoint restore、compaction、source edit、skill/environment/memory 变化、tool schema/权限变化以及附件可用性变化等会影响有效上下文的操作，必须在下一次 Provider dispatch 前通过统一的 `ContextReconciliation` 比较上一份已提交 snapshot 与当前状态，并生成明确 outcome：`history_view_changed`、`overlay_reused`、`delta_appended`、`overlay_materialized` 或 `overlay_invalid`。history-only 变化在 source overlay 仍兼容时 MUST 保留 `source_overlay_epoch`、base identity 和 delta identity；source/tool/policy 变化不得伪装成普通 history message 或隐式创建 Turn。reconciliation 未完成或返回 invalid 时不得发起未记录的 Provider request。

#### Scenario: rewind 只改变历史尾部

- **WHEN** rewind 只隐藏 canonical history 的尾部 item，而 source base、delta、tool policy 和 provider cache contract 都未失效
- **THEN** reconciliation 只推进 `history_view_revision`，新 assembly 复用同一 source overlay epoch；plan/hash 反映新的 history view，但不物化 source base

#### Scenario: 被隐藏的 delta 仍参与请求

- **WHEN** rewind 后 A→B 的 ambient delta item 不再属于普通 history view，但该 source overlay 仍是当前有效状态
- **THEN** reconciliation 从独立 overlay lineage 选择 A→B 并重新注入新 plan，delta 不因历史 view 范围变化而丢失或变成新的 Turn

#### Scenario: 非文本上下文变化使用同一重协调边界

- **WHEN** tool schema、工具可见性、环境状态、memory、附件权限或 provider/projector capability 发生变化
- **THEN** reconciliation 根据 source 类型选择 request-only delta、tool/policy snapshot 更新或 overlay materialization，assembly 显式记录变化原因和 hash，不把这些变化追加为普通用户/assistant history

#### Scenario: 重协调失败阻止请求

- **WHEN** history view 已切换但 source revision、tool policy 或所需 detail 无法与上一份 snapshot 对齐
- **THEN** 系统返回 `overlay_invalid` 或对应 mismatch/detail-unavailable，保留旧 assembly 和 canonical history，不使用未重协调的混合上下文发起 Provider request

### Requirement: Context plan 与 provider request hash 必须可重放和比较

`CanonicalItemRecord.content_hash` 的值格式固定为 `sha256:jcs:v1:<64位小写hex>`；其输入恰好是 `{ "payload_kind": <payload_kind>, "payload": <payload> }` 的 RFC 8785 JCS 无空白 UTF-8 字节，SHA-256 后用小写 hexadecimal 编码。文本不 trim 或换行归一化，对象 key 由 JCS 排序，数组保留 payload 语义顺序，二进制由 payload schema 先编码为 typed base64url 等值。item identity/sequence、semantic kind、status、producer、metadata、时间、关系、wire role 和 detail path 不进入该 hash；多 part payload 的 part identity/ordinal/value 在 payload 中并因此被覆盖。`item_catalog.content_hash` 必须与 JSONL envelope 一致，校验失败不得继续读取。

`plan_hash` 的 canonical preimage schema 固定为 `context-plan-hash:v2`。除 format/schema version、active view revision、按 `plan_ordinal` 排序的 canonical/request-only/overlay selection、每个 ContextRef 的 source/version/hash/length、贡献顺序和 selection/visibility policy 外，必须显式包含由 `ContextRequestPlan.tool_set_refs[]` 选出的 `tool_set_refs[]`。每个 ToolSetRef 条目必须包含 `tool_set_snapshot_id`、`source_revision`、`content_length`、恰一个 `content_hash` 或 `redacted_stable_digest`、`tool_set_schema`、`tool_set_schema_version`、`tool_policy_version` 及同一 `tool_set_manifest[]` 绑定的规范 `tool_policy`；不得用脱离 manifest 的 logical tool contract hash 替代这些字段。`tool_set_refs[]` registry portion 按 `tool_set_snapshot_id` 排序，`selection[]` 仍按 `plan_ordinal` 保留语义顺序，manifest 内 tools 按稳定 `tool_id` 排序；未被 selection 选中的 registry entry 不进入该 plan hash。canonical item ref 缺少 content hash 或被选 ToolSetRef 缺少 manifest hash token 时均不可 seal，因此 plan hash 不能只覆盖脱离 item/tool 来源的 message 文本。

系统 SHALL 使用带算法标识的 `sha256:jcs:v1` 对 plan/request preimage 做规范化 hash：对象 key 递归排序，数值按 JCS 规范化，语义数组按 item/content-part/tool/ref ordinal 保序，集合型字段按稳定 identity 排序，缺省字段省略，语义 null 才编码 null。`plan_hash` MUST 只覆盖 provider-neutral 的 format/schema version、active view revision、ordered canonical/request-only refs、source/version/hash/length、贡献顺序、selection/visibility policy 以及完整 ToolSetRef manifest identity（`tool_set_snapshot_id`、source revision、length、hash token、schema/policy version 与绑定的 `tool_policy`）；不得包含 assembly/execution/model-call identity、时间、wire role、provider/model capability、provider request ID 或 provider-specific loss。`request_hash` MUST 覆盖 projector id/version、provider/model profile、规范化 wire request、由同一 ToolSetRef manifest 投影出的 tool schema/config、attachment refs 和 loss/redaction markers；不得包含 provider request ID、retry/attempt identity、时间戳、认证或 transport headers。敏感正文 MUST 在 preimage 中以 redaction class、长度和 session-scoped stable digest 表达，不能把 secret 或 detail store 原文放入 hash。

同一 committed plan、selection 和 ToolSetRef manifest 被不同 provider projector 使用时 MUST 共享相同的 `plan_hash`；工具 schema/config、policy 或 source revision 变化必须产生新的 ToolSetRef/plan hash，不能静默复用旧 hash。`request_hash` 只在相同 projector/version 和 provider/model profile 内用于 exact replay，跨 provider 可以不同。重放先逐字段校验 ToolSetRef 与 manifest，再比较 `plan_hash`；ToolSetRef manifest 不可解析、不可用或不一致返回 `source-mismatch`/`detail-unavailable`，hash preimage 不一致返回 `plan-hash-mismatch`，两者都不得 dispatch。相同 plan 下同一 projector 的 request hash 不一致必须返回 request mismatch，不得声称 exact replay。provider request ID 可以作为 outcome metadata 保存，但永远不参与上述 hash。

#### Scenario: 跨 provider plan 可比

- **WHEN** 相同 active view、request-only contributions 和同一组已选 ToolSetRef manifest 分别编译为 LangChain request 与原生 provider item request
- **THEN** 两个 assembly 按同一 `context-plan-hash:v2` preimage 得到相同 `plan_hash`；preimage 明确包含排序后的 `tool_set_refs[]`（snapshot id、source revision、length、hash token、schema/policy version 和绑定 policy），各自保存独立的 projector/provider-specific `request_hash` 和 loss report

#### Scenario: ToolSetRef manifest 变化导致 plan hash mismatch

- **WHEN** 工具 schema/config、tool policy、manifest schema version 或 tool registry source revision 变化，导致 ToolSetRef manifest 的任一绑定字段、content length 或 hash token 变化
- **THEN** 系统创建新的 target-local ToolSetRef/plan 并得到新的 `plan_hash`；restore/replay 对旧 plan 返回 `plan-hash-mismatch` 或明确的 `source-mismatch`，不得使用当前 registry、空 tools 或旧 logical tool contract hash 静默 dispatch

#### Scenario: tool_set contribution 被拒绝

- **WHEN** v2 composer 收到 `ContextContribution.contribution_kind=tool_set`，或一次性 migration reader 读取到该旧记录
- **THEN** v2 返回 `contribution-kind-unsupported`；migration reader 仅把原始记录保留到 migration report/quarantine，不创建 ToolSetRef、ToolSetSnapshot 或 Provider tool definition，正常 runtime 不开启任何 legacy adapter 路径

#### Scenario: 敏感字段不进入 hash 明文

- **WHEN** request plan 或渲染 request 包含 credential、token、内部 prompt 或其它 protected body
- **THEN** hash preimage 只包含 redaction marker、长度和 stable digest，detail_ref/availability 可被记录但 detail store 原文不得进入 plan/request hash

#### Scenario: replay hash mismatch

- **WHEN** 重放时 view revision、source hash、贡献顺序或同一 projector 的规范化 request 发生变化
- **THEN** 系统分别返回 context mismatch 或 request mismatch，并保留原 assembly；不得使用当前 middleware 配置静默生成新的 exact replay

#### Scenario: content hash 使用真正的 JCS

- **WHEN** writer 为 content、plan、request 或 legacy seed 计算 `sha256:jcs:v1`
- **THEN** serializer 使用 RFC 8785 的 UTF-16 code-unit key ordering、ECMAScript/IEEE-754 有限数字规范化、`-0`/指数最短表示和无空白 UTF-8 输出；`json.dumps(sort_keys=True)`、语言默认 key ordering、NaN/Infinity 或仅按 Unicode code point 排序都必须被拒绝或不得作为该 hash 的实现
- **AND** 跨语言 golden vector 对浮点、Unicode key、数组 ordinal 和非法数字得到相同 digest；provider request ID、时间、认证和 transport header 不进入 preimage

#### Scenario: required detail 在 seal 前缺失

- **WHEN** `seal_context_assembly(required_detail=true)` 收到 `detail=NULL`、缺失 ref、不可读 detail、保护级别不足或 detail hash/length/source revision 不匹配
- **THEN** seal 返回 `detail-unavailable`、`source-mismatch` 或 detail security error，不产生可 dispatch 的 sealed/ready assembly；Provider 不得收到该 request
- **AND** `required_detail=false` 的缺失只能以显式 omission/loss seal，并写入 assembly diagnostics；该 entry 仍保留 tagged request-only ref、plan ordinal 和可得 source identity，但 `detail_ref`、`content_length`、hash token、`contribution_ordinal` 可以为 null/未分配，不得伪造空正文或把已知 metadata 当作可读正文

#### Scenario: optional request-only omission 不创建伪 detail

- **WHEN** optional request-only source 的 detail 缺失、过期、不可读或保护级别不足，但该 source 不是本 assembly 的 required source
- **THEN** Saver 可以 seal 一个 `included=false` entry，保留 `ref={ref_type=request_only,ref_id}`、`assembly_id`、唯一 `plan_ordinal`、`selection_kind=request_only`、`omission_reason`、`loss`、`availability=unavailable|expired|forbidden` 及可得 source identity
- **AND** 该 entry 不分配最终 `detail_ref`、`content_length`、hash token 或 `contribution_ordinal`；若保留已知 revision/length/hash/protection，必须与 source manifest 逐字段一致
- **AND** LangChain/native Provider/Web history 与 restore 均跳过其正文，不查询当前 middleware/source、不生成空 message，也不把 omission 静默当作成功；required detail 仍必须拒绝 seal

#### Scenario: optional canonical 与 tool-set omission 的投影边界

- **WHEN** optional canonical item 或 ToolSetRef source 在 seal preflight 时不可用、被删除、权限不足或完整性校验失败
- **THEN** canonical entry 或 tool-set entry 均可按其原 tagged ref 保留 `included=false`、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 source identity，但不分配正文完整性字段或 contribution/detail binding
- **AND** restore/history 保留 omission/loss 元数据而跳过 canonical message；Provider projector 不生成该 tool definition、空 tools 或当前 registry 的替代值；若 source 是 required，则 seal/dispatch 返回 `source-mismatch` 或 `detail-unavailable`

#### Scenario: request source hash 不匹配

- **WHEN** 重启或投影时 `ContextRef` 指向的 canonical payload 或 `ContextContribution` detail 正文与其 `content_hash`、source revision 或 sealed plan 不一致
- **THEN** Saver 返回 `source-mismatch` 或 `detail-unavailable` 并停止投影/dispatch，不发送覆盖后或错误 source 的正文，也不把 metadata/hash 摘要当作正文已投影

#### Scenario: 所有 projector 复用冻结的 selection

- **WHEN** 同一个 sealed plan 被 LangChain、native Provider 和 Web history 恢复，或在不同 history view revision 下重新选择仍有效的 overlay
- **THEN** 三者消费同一 `selection` 列表及其稳定 `plan_ordinal`；每个 contribution 还保留持久 `contribution_ordinal`，重启后按 ordinal 恢复 canonical/request-only 与 base→delta 顺序
- **AND** projector 不得按 `created_at`、`contribution_id`、物理邻接或 dict insertion order 排序，不得把所有 request-only ref/contribution 无条件 prepend 到 canonical messages；顺序不一致必须返回 plan/order mismatch

### Requirement: SQLite item 索引按用途分层

系统 SHALL 为每个已提交 canonical item 建立最小 catalog 定位记录，至少包含 item identity、物理顺序、`semantic_kind`/`payload_kind`、status、Turn/group 关联、JSONL offset/length、content hash、commit identity 和基本 visibility。request-only reference 不得进入 canonical item catalog 或占用 canonical item sequence；如需精确重放，只能在有界的 assembly/plan detail store 中保留。更重的来源关系、文本/summary projection、tool/reasoning detail 和 middleware assembly metadata MUST 按 item 的用途按需建立；仅当 item 或其 content part 被声明为持久化操作边界时才建立 durable rewind/compaction/fork anchor。未被历史、上下文或操作使用的 canonical item 可以只有最小 catalog 和受保护 reference，不得因此影响 rollout 恢复完整性。Context view membership 的逻辑顺序 MUST 与 rollout 物理 `item_sequence` 分开表达，Turn root MUST 通过 `root_input_item_id` 定位而不是扫描 semantic/wire role。

#### Scenario: 普通 assistant output 的索引

- **WHEN** 一个 `assistant_output` item 已提交但只需要参与 Turn summary
- **THEN** SQLite 至少能定位该 item、校验正文并生成 `assistant_text` projection，但不强制为它建立完整 middleware detail 或 operation anchor

#### Scenario: tool item 的按需详情索引

- **WHEN** 一个 tool call item 需要支持显式详情、tool result 关联和重放审计
- **THEN** SQLite 为其建立 tool relation、受限参数/结果 projection 和 provenance reference；默认 summary 仍可只读取名称与状态

#### Scenario: 不进入当前上下文的内部 item

- **WHEN** 一个内部诊断或 provider opaque item 不属于 active context，也不提供用户详情
- **THEN** 它仍有最小 catalog/hash/offset 以供恢复和审计，但不进入普通 context plan、Turn projection 或默认 API payload

#### Scenario: 动态 request-only context 不占 canonical 索引

- **WHEN** skill 说明、环境快照或 memory injection 只为一次 model call 提供 system/developer context
- **THEN** 系统只在 assembly/plan metadata 中记录其 request-only reference、来源和 hash，不创建 canonical item catalog 行，不占用 canonical item sequence，也不参与 rewind/fork view

### Requirement: 操作 anchor 可以细于 message 和 Turn

系统 SHALL 支持以已提交 canonical `item_id` 以及需要时以 item 内的 `content_part_id`/fragment reference 表达 rewind、replay、compaction 和 fork 的 durable 内部边界。interrupt 首先使用当前 stream 的内存 cursor/ItemDraft；只有在终态化需要恢复或审计时，才将停止位置保存为 item/content-part reference。durable anchor MUST 保存 `inclusive` 或 `before` 语义、所属 view/branch、创建来源、可恢复性和目标 item/content part；request-only reference 不能直接作为历史操作 anchor。Turn 仍可作为用户历史分页和默认操作入口，并由 resolver 映射到精确 item anchor。

`content_part` anchor 的最小规范为：已提交的 `item_id`、稳定的 `content_part_id`、part semantic kind、同一 item 内的 ordinal、part content hash 或 prefix hash、`before`/`inclusive` 边界语义、source view/branch 和 durability/recovery capability。`content_part` 的 canonical 正文必须位于父 item JSONL envelope 的 `payload.parts`（或 payload schema 明确指定的等价字段）内，part value 与 payload_kind 一起受父 `content_hash` 覆盖；detail store 不得成为 part 正文来源。SQLite `item_parts` 如果建立，只能是派生稀疏索引，保存 item/part identity、ordinal、JSON Pointer 或 part ordinal locator、可选的父 JSONL line byte offset/length、父 line hash 与 part hash/prefix hash。offset 是 immutable UTF-8 JSONL 行内的加速坐标，不是第二事实源，读取时必须校验父 line/content hash；索引缺失可以回退到解析命中的 item，索引冲突必须返回 integrity error。任意 token、字符 offset 或 raw chunk index 只有在对应 fragment 具有独立稳定 identity、长度/hash 和可恢复布局时才可以作为 anchor；否则必须退回 item 边界或返回不可操作错误。不得为满足 anchor 而把每个流式 delta 写入 canonical history。

#### Scenario: 在 assistant_output item 中断

- **WHEN** 用户中断发生在一个 `assistant_output` item 的流式 draft 或某个可恢复 content part 之后
- **THEN** stream/turn finalization 可以将该 item 标记为 partial；如需要跨重启恢复，再保存精确到 item/content part 的停止 reference，不要求为每个 raw chunk 建立 durable anchor，也不必回退到上一条完整 message

#### Scenario: 在 tool call 前 rewind

- **WHEN** 用户从一个包含 reasoning、assistant output 和 tool call 的 Turn 发起 rewind
- **THEN** resolver 可以把操作边界落在 tool call item 之前，保留前面的 item，不复制或修改原有 message payload

#### Scenario: 不可操作 item 作为 anchor

- **WHEN** 请求把没有 operation-anchor 能力的内部 item 指定为 rewind/compaction 边界
- **THEN** 系统返回明确的不可操作 anchor 错误，或按照声明的 resolver 规则选择最近的合法 item anchor，不得静默按 message 末尾猜测

#### Scenario: request-only context 不能作为 rewind 边界

- **WHEN** 调用方把某次请求中的 skill/environment prompt reference 指定为 rewind 或 fork 边界
- **THEN** 系统返回明确的 request-only/non-durable anchor 错误，要求解析到所属 view 中合法的 canonical item 或 content-part anchor

#### Scenario: content part anchor 信息不足

- **WHEN** 调用方只提供 assistant 文本的字符 offset 或某个 raw delta index，没有稳定的 content_part_id、hash 和 source view
- **THEN** 系统拒绝该 fragment anchor 或按照显式 resolver 规则退回合法的 item anchor，不把不稳定 offset 当作可恢复边界

### Requirement: Active context view 决定 item 可见范围

系统 SHALL 通过 SQLite context view、branch lineage 和 item range/reference 决定一次执行或历史请求可见的 canonical history item 集合。compaction、rewind、replay 和 fork MUST 通过新 view 或引用表达 history 选择变化，不得修改既有 item、创建依赖物理 segment 的第二份正文，或按最大物理序号猜测 active context。request-only reference 和 source overlay reference 不属于普通 history view membership；ContextRequestPlan 必须在 history view 解析完成后独立执行 reconciliation，必要时重新注入仍然有效的 ambient delta。`pending_next_turn` runtime notice 如果已经持久化为 canonical item，也必须保持 pending/ambient scope，不能作为 normal Turn member 或 Turn root。

#### Scenario: Rewind 隐藏旧后缀

- **WHEN** 用户 rewind 到较早 item 后继续执行
- **THEN** 新 active view 只包含目标边界以前的可见 item 和新追加 item，旧 JSONL 后缀仍可由旧 view 读取但不进入当前请求

#### Scenario: Rewind 不隐藏仍有效的 source overlay

- **WHEN** rewind 隐藏了历史尾部中的 A→B source delta item，但当前 source overlay epoch 仍有效
- **THEN** 新 history view 不包含该 delta 作为普通历史 item，但下一次 ContextRequestPlan 仍通过 overlay lineage 重新注入 A→B；history view revision 改变不导致 source base 物化

#### Scenario: Compaction 位于 Turn 中间

- **WHEN** compaction anchor 指向一个 Turn 中间的 tool result 之前
- **THEN** context view 可以以该 item 边界截断并保留摘要/必要尾部，不被强制对齐到 Turn 末尾

#### Scenario: pending runtime notice 被下一次请求消费

- **WHEN** 被打断 execution 产生一个尚未绑定新用户 Turn 的 `pending_next_turn` runtime notice，随后用户提交普通输入
- **THEN** 新 Turn 以该用户 input 的 `root_input_item_id` 开始；ContextRequestPlan 可以通过 ambient canonical reference 或 request-only projection 使用 pending notice，但它不进入 Turn 分页、root 或普通 Turn item range

#### Scenario: pending runtime notice 尚未被消费

- **WHEN** session 只有 pending runtime notice，尚未出现新的用户 input，也没有 resume execution
- **THEN** notice 保持 pending，不被 active context view 当作普通历史 Turn，不被历史 reader 伪造为用户消息；过期或丢失时返回明确的 pending/detail 状态

### Requirement: Canonical item 必须可编译为多种请求投影

系统 SHALL 从 active context view 和 request-only contribution 先形成有序、可审计的 context request plan，再按目标能力编译为 LangChain message 或 Provider 原生 item/request。编译过程 MUST 保留 canonical/request-only reference、`semantic_kind`/`payload_kind`、可表达的顺序、tool-call/result 关联、附件和 reasoning 保护状态；多个 prompt contribution 可以合并为一个 system/developer wire role，但 plan 必须保留其独立来源和 omission/loss；无法表达的字段必须显式 omit、loss 或 reject，不得静默拼接成普通文本。`assistant_text` 只能作为 `assistant_output` payload/content-part 的 projection，不能在 plan 中作为 canonical item kind。

#### Scenario: 编译为 LangChain 执行消息

- **WHEN** 当前 Agent 选择 LangChain adapter 执行请求
- **THEN** adapter 生成临时的 `list[BaseMessage]`，可将同一 `message_group_id` 下的 item 聚合为一个 `AIMessage`，并把 tool calls 放入 `AIMessage.tool_calls`、结果放入关联的 `ToolMessage`

#### Scenario: 编译为原生 Responses 请求

- **WHEN** Provider adapter 能直接接受 item 化请求
- **THEN** 系统可以从 context request plan 直接生成 Provider item/request，不要求先构造 LangChain message，也不把 LangChain 对象作为 canonical history

#### Scenario: request-only prompt 的 wire 投影

- **WHEN** ContextRequestPlan 包含静态 system prompt、skill contribution 和 workspace/environment contribution
- **THEN** LangChain/provider projector 可以按目标能力将它们合并到一个 system/developer/instructions wire 输入，但必须保留独立 reference、source identity 和 request-only 生命周期，不能从 wire role 反向创建历史 item

### Requirement: Tool 因果关系和 reasoning 保护状态不可丢失

系统 SHALL 为 model-declared tool call、实际 tool execution 和 tool result 保存独立的 item identity，并使用 `tool_invocation_id`、`tool_call_id` 和 `tool_attempt_id` 表达逻辑调用、实际 call attempt 与执行 attempt。一个 logical invocation 可以有多个按序 call attempt；每个 call attempt 至多启动一个 tool attempt；每个 tool attempt 至多有一个已提交 `tool_result` item，缺少 result 只能表示未完成或 unknown。三种 identity 在 session 内唯一，tool attempt 的执行幂等键必须绑定 `tool_attempt_id` 和输入 payload hash；同 key 相同 payload 重试只返回原 outcome，不同 payload 必须报冲突。retry/resume 必须创建新的 call/attempt identity，并以 `retry_of`/`resumes` 关系连接旧 identity，旧 item 不可变。只有当前 active view/lineage 中 `status=completed` 且工具 outcome 成功的 result，才可通过 `replay_input` 关系作为自动 replay 输入；superseded、failed、partial 或 unknown result 默认不可作为 replay 输入。reasoning 的可读文本、provider summary、encrypted/opaque payload 和本地展示策略 MUST 保持可区分；受保护 payload 默认不得进入用户历史响应或普通 assistant 文本。

#### Scenario: tool call 与 result 成对恢复

- **WHEN** 一个 Turn 包含 tool call、工具执行和 tool result
- **THEN** 恢复与 Provider request projection 能通过 invocation/call/attempt 关联还原配对，且重复恢复不会创建第二个可执行 tool call；只有成功 completed result 才会自动作为 replay input

#### Scenario: encrypted reasoning 默认隐藏

- **WHEN** Provider 返回只有恢复用途的 encrypted 或 opaque reasoning
- **THEN** canonical item 可以保留其受保护 payload 或引用，但历史/API 的默认 projection 只返回安全 marker，不返回原始正文

#### Scenario: tool retry 保留因果和结果选择

- **WHEN** provider retry 或 execution resume 对同一个 logical tool invocation 产生新的 call/attempt
- **THEN** 新 identity 通过 `retry_of`/`resumes` 连接旧 identity，旧 result 不被覆盖；active replay 只选择显式成功且未 superseded 的 result，否则重新执行或返回不可重放错误

### Requirement: 旧 message-line rollout 必须显式识别和迁移

`candidate_key` 与 `candidate_status` 属于 v1 migration report 的候选记录字段，不是 `CanonicalItemRecord.status`、`Turn.status` 或 `ControlOutcome`；candidate_status 闭合集合为 `accepted`、`legacy_missing_turn_id`、`legacy_turn_group_ambiguous`、`legacy_multiple_user_messages`、`legacy_orphan`、`legacy_unsupported_role` 和 `legacy_identity_conflict`。只有 `accepted`/`legacy_missing_turn_id` 才允许继续 identity 补全，其余状态必须拒绝或 quarantine，不能创建可运行 Turn。

v1 root candidate 的生成规则冻结为：`message_sequence` 只提供可审计的 user-root window 边界，不能仅凭非 user 记录的物理邻接猜测 Turn。每个 window 必须恰好包含一个 `role=user` 行；窗口内无非空 `turn_id` 时生成 `candidate_key=legacy-missing-turn:<legacy_message_hash>`、状态 `legacy_missing_turn_id`，窗口内恰有一个非空 `turn_id` 时生成 `candidate_key=legacy-turn:<turn_id>`、状态 `accepted`，并将缺失 ID 的可迁移成员归入该 ID。窗口内多个不同非空 ID 时标记 `legacy_turn_group_ambiguous` 并整体拒绝；同一 ID 跨多个 user window 时标记 `legacy_multiple_user_messages` 并整体拒绝；首个 user 前、没有 root 的记录标记 `legacy_orphan`，重复 source coordinate、message identity 冲突或无法确定窗口边界标记 `legacy_identity_conflict` 并拒绝。

window 内的 legacy role 归属固定为：`assistant`、`tool` 以及明确等价的旧 `function` 是可迁移的 Turn member，缺失 `turn_id` 时继承该 window 的唯一 candidate ID；`system` 和 `developer` 记录为 `legacy_request_context`，保留 source coordinate、payload hash 和受保护 detail/lineage reference，但不进入 Turn root/member、不能创建 Turn，也不得静默丢弃。`system_reminder` 只有在该行已有可信 internal/checkpoint metadata 时才映射为 Turn 外的 `runtime_notice`（`turn_scope=pending_next_turn`、`turn_id=NULL`）；缺少该 metadata 时标记 `legacy_unsupported_role` 并整体拒绝 candidate。未知 role、其它 legacy role 或不能解释的 role/payload 组合一律标记 `legacy_unsupported_role`，原始行保留到 report/quarantine 并整体拒绝 candidate。若 `system`/`developer`/受信任 `system_reminder` 携带非空 `turn_id`，该 ID 必须与 window 唯一 candidate ID 相同，否则标记 `legacy_turn_group_ambiguous`；即使相同也不成为 Turn member。整个 rollout 没有任何 user window 时，所有 system/developer/system_reminder/其它行均标记 `legacy_orphan` 或 `legacy_unsupported_role` 并只进入 report/quarantine，不生成 root 或 synthetic acceptance。

只有 `accepted` 或 `legacy_missing_turn_id` candidate 才能进入 synthetic identity 补全，后续 v2 Turn 仍使用新的 target-local identity；被拒绝 candidate 的每一条 source 行都必须在 migration report 中保留归属、拒绝原因和 source coordinate。

系统 SHALL 通过 SQLite manifest/database metadata 的 `rollout_format_version` 与每个 JSONL envelope 的 `format_version` 区分 v1 message-line 与 v2 item-line。reader MUST 同时校验两处版本；不得只根据 `role`、首行形状或字段存在性猜测格式。v1 与 v2 不得混写，未知版本、manifest/envelope 不一致或结构不完整时 MUST 进入明确 recovery/error。旧格式不得进入正常 context compiler；只有一次性 `legacy_import_v1_to_v2` migration/import operation 的 reader 可以在 staging 中读取并交给转换器。新 writer 不得永久双写，遇到未知或无法无损映射的旧字段不得静默丢弃或回退为空历史。

v1 message-line 顶层字段固定为 `format_version=1`、`record_type=message`、`message_sequence`、`message_id`、`turn_id`、`role`、`message` 和 `metadata`；v2 item-line 顶层字段固定为 `format_version=2`、`record_type=item`、`item_sequence`、`item_id`、`turn_id?`、`turn_scope?`、`message_group_id?`、`semantic_kind`、`payload_kind`、`wire_role?`、`status`、`producer_ref`、`payload`、`content_hash`、`created_at` 和 `metadata`。SQLite manifest 的 `rollout_format_version` 必须与 envelope version 一致。v2 中 `assistant_output` 是 canonical semantic kind，`assistant_text` 只能是 projection；v1 的内部 `system_reminder` 只能依据已有 internal/checkpoint metadata 转换为 `runtime_notice`，不能使用当前 middleware 配置补造 provenance 或 Turn root。
v1 迁移不能从 message role、最后一条 assistant 或物理相邻记录推断 v2 身份。对每个 v1 root candidate，先将精确解码的 `role`/`message` 与 source session、message sequence、message id 组成 legacy message object，按 JCS/SHA-256 得到 `legacy_message_hash`，再对包含 source coordinate 和该 hash 的对象按同一算法得到 `legacy_seed_hash`；固定生成 `accepted_ingress_id=legacy-ingress:<legacy_seed_hash>`、`acceptance_idempotency_key=legacy-migration:<legacy_seed_hash>` 和 `initial_execution_id=legacy-execution:<legacy_seed_hash>`，并标记 `identity_origin=legacy_synthetic`。v1 的 `turn_id`、`message_id`、`message_sequence` 和 offset 只保留为 `legacy_source_ref`/lineage；迁移后的 v2 Turn、root item、item sequence 和 execution 使用新的 target-local identity。seed 冲突或同幂等键 payload 不同必须终止迁移。

只有 v1 manifest/checkpoint 或明确的 legacy final marker 能无歧义指向同一 Turn 的 completed assistant output 时，迁移才设置 v2 `final_item_id`/`Turn.status=completed`；否则 `final_item_id=NULL` 且 status=`unknown`（有明确 failure/interrupted marker 时使用对应状态）。合成的 execution outcome 默认是 `unknown`，不表示实际 provider call 已成功。任何跨 session 的 `full_rollout_copy`（无论 source v1/v2）都把 target 固定创建为 `rollout_format_version=2`；source 为 v1 时，必须先由一次性 `legacy_import_v1_to_v2` staging 完成 mapping，再执行 copy。source v1 message identity/sequence/offset 只存映射审计坐标，不能写入 target 的 v2 identity 或 committed offset。

v1 root candidate 必须按 source `message_sequence` 升序切分为 user-root window：每个 `role=user` 行开启窗口，直到下一条 user 行之前，因此每个窗口必须恰好一个 user message。窗口内没有非空 `turn_id` 时生成 `legacy_missing_turn_id` candidate；恰好一个非空 `turn_id` 时使用该 ID，并把窗口内缺失 ID 的 assistant/tool/function 行归入该 Turn；system/developer 只记录为 `legacy_request_context`，受信任的 system_reminder 映射为 Turn 外 runtime_notice，未知或其它 role 使 candidate 进入 `legacy_unsupported_role` 并整体拒绝。多个不同非空 ID 时整体拒绝并报告 `legacy_turn_group_ambiguous`。同一 ID 在一个窗口的可迁移非 user 行上重复是正常成员关系，不是重复 Turn。

同一非空 `turn_id` 如果跨多个 user-root window，表示该 legacy Turn 有多个 user message，整个 ID 组拒绝迁移，不得任选第一条或拆分。首个 user 之前的记录、没有 root 的 ID 组保留为 `legacy_orphan`，不得创建 Turn；重复 source coordinate、同一 message identity 对应多个 turn_id、未知/其它 role 或无法确定窗口边界也必须整体拒绝，并把所有原始行保留在 migration report/quarantine。不同窗口不得因 payload 相同而合并。只有恰好一个 root user message 且通过 role、turn_id 和冲突校验的 candidate 才能生成 synthetic ingress/execution identity。

#### Scenario: 正常运行时拒绝读取旧 session

- **WHEN** 正常 history/provider/checkpoint/runtime 或 context compiler 打开仍是旧 message-line 格式的 session
- **THEN** 系统识别其版本并返回 `v1_migration_required`，不调用 v1 reader、不创建 v2 view、不把旧记录伪装成已经完成 v2 cutover；只有显式的一次性 `legacy_import_v1_to_v2` 命令可以打开 migration staging

#### Scenario: v1 window 的非 user role 归属

- **WHEN** 一个 user-root window 同时包含 assistant/tool/function、system/developer、带可信 internal/checkpoint metadata 的 system_reminder，以及 unknown/其它 legacy role
- **THEN** assistant/tool/function 按唯一 candidate 的缺失 ID 规则作为 Turn member；system/developer 逐行保留为 `legacy_request_context` 而不进入 Turn；可信 system_reminder 映射为 Turn 外、`turn_id=NULL` 的 `runtime_notice`
- **AND** unknown/其它 role，以及缺少可信 metadata 的 system_reminder，使整个 candidate 标记 `legacy_unsupported_role` 并拒绝迁移；所有被排除或拒绝的 source 行都保留 source coordinate、payload hash 和拒绝/归属信息，不得静默丢失或依据物理邻接改派

#### Scenario: 旧字段无法无损映射

- **WHEN** 旧 message 包含新的 canonical item 无法表达的 provider 字段
- **THEN** 系统保留 extension/raw reference 或显式标记 loss/partial，并拒绝声称该 session 已无损迁移

#### Scenario: v1/v2 dispatch 不一致

- **WHEN** SQLite manifest 声明的 rollout format version 与 JSONL envelope version 不一致，或同一文件出现 v1/v2 record
- **THEN** reader 返回明确的 format mismatch/recovery error，不把记录混合成一个 context view，也不静默选择其中一个版本

#### Scenario: 显式迁移保留 v1 原件

- **WHEN** 用户显式执行 v1 到 v2 migration
- **THEN** 系统在临时 v2 artifact 中校验 item、Turn root、final item、view、tool identity 和 reasoning protection 后再原子安装 v2；v1 原 artifact 保持可读，迁移失败不删除或覆盖 v1
