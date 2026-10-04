## MODIFIED Requirements

### Requirement: Web 历史以 rollout JSONL 和 SQLite 为唯一来源

系统 SHALL 直接从 v2 会话 `index.sqlite` 和 `rollout.jsonl` 读取 Web 历史摘要、详情、Turn 边界和 cursor 定位。v2 item-line 是正常生产 history 的唯一事实源；v1 message-line 只允许由显式一次性 `legacy_import_v1_to_v2` migration/import operation 读取，不能进入 `/bootstrap`、`/history` 或正常 checkpoint/provider 路径。Trace、日志和旧 turn projection MUST NOT 作为历史数据源，也不得在 rollout 缺失时静默回退。

#### Scenario: v2 fixture 从 rollout 读取

- **WHEN** 测试数据使用 v2 canonical item 写入 rollout
- **THEN** 历史 API 从 v2 SQLite view/index 和 JSONL offset 组装完整 Turn，且不读取 Trace、旧 projection 或 v1 artifact 作为事实来源

#### Scenario: 正常 history 拒绝 v1 artifact

- **WHEN** `/bootstrap`、`/history` 或正常 checkpoint/history service 打开 `rollout_format_version=1` 的 session，且没有处于显式 `legacy_import_v1_to_v2` operation
- **THEN** 系统返回 `v1_migration_required`，不调用 v1 reader、不生成 v1 response parts、不创建 v2 view，也不把该 session 当作可运行的 history/provider/checkpoint 输入

#### Scenario: 旧 Trace 不能伪造历史

- **WHEN** 会话只有旧 Trace 文件而没有有效 rollout
- **THEN** 历史 API 返回可诊断的 rollout 缺失或损坏错误，不返回旧投影中的 Turn

### Requirement: Turn 是默认分页边界

历史加载必须使用统一的 `Turn.status` 闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`；其中 `completed_empty` 是唯一的无 canonical output 正常终态名称。普通 `cancelled` 与 `full_rollout_copy` 产生的 `cancelled` historical 只能作为不可运行历史读取，不能由历史加载触发 `resume_turn` 或 `dispatch_replay`；对原 Turn 的这两种请求必须由业务层显式返回 `turn_not_resumable`。若需要重新执行，只能由独立的 `replay_as_new_turn` 创建新的 Turn/root/acceptance/initial execution，在 active view 登记新的 view-local `logical_turn_ordinal`，source history 可以作为上下文前缀但 source Turn/root 不能成为新 Turn 的 root，并记录 `replay_of_turn_id`，不能把新 Turn 创建操作混入原 Turn 的错误响应。

系统 SHALL 以完整 Turn 作为默认加载和分页单位。Turn 必须由 `TurnRecord.root_input_item_id` 表示其真实用户输入起点；用户输入、合并 steering Job、工具活动和最终状态 MUST 保持在同一个 Turn 中。历史顺序使用当前 context view 的 `logical_turn_ordinal`，不使用会跨 branch/view 保持不变的全局 `turn_ordinal` 重排；root lookup 必须沿 view fork lineage 解析到同一个 TurnRecord。`pending_next_turn` runtime notice、request-only prompt、缓存保持型 source delta 和其它内部消息不得创建 Turn、占用 root 或把一个 Turn 拆成独立分页项；overlay 只通过 assembly/provenance 详情视图可见。

#### Scenario: 合并 steering 消息

- **WHEN** 多个 steering 消息被一次 Job 合并执行
- **THEN** 历史返回一个完整执行 Turn，并保留 root input、源消息和合并 Job 的身份信息

#### Scenario: 工具调用中的 Turn

- **WHEN** 一个 Turn 包含多个工具调用和工具结果
- **THEN** 默认 summary 返回一个 Turn，工具数量和状态作为 Turn 内摘要

#### Scenario: pending runtime notice 位于新 Turn 之前

- **WHEN** 被中断 execution 产生 pending runtime notice，随后用户提交普通输入
- **THEN** 历史以新用户输入的 `root_input_item_id` 开始新 Turn；notice 只能通过 ambient/assembly relation 参与请求，不成为用户消息或 Turn root

#### Scenario: 缓存保持型 source delta 不污染历史分页

- **WHEN** `AGENTS.md` 在两个普通用户 Turn 之间从 revision A 变为 B，并追加了 A→B 的 ambient `runtime_notice` delta
- **THEN** 默认历史仍只按真实用户 root 返回原有 Turn，不新增一条用户消息或独立 Turn；需要审查上下文来源时才通过 assembly/provenance reference 展开该 delta

### Requirement: 加载内容可以按 include 策略选择

系统 SHALL 支持独立选择用户消息、可见 text、工具摘要、工具调用、工具结果、reasoning 摘要/详情、encrypted reasoning 元数据、内部消息和 metadata。`assistant_text` 不作为 canonical 事件类型，只能是 `assistant_output` item/content part 的统一 response-part 派生别名；默认工具展示 MUST 只包含工具名称和状态，完整参数和结果必须显式请求。

#### Scenario: 默认工具摘要

- **WHEN** 调用方未请求工具详情
- **THEN** 系统从 SQLite tool projection 返回工具名称、状态和有界错误摘要，不读取工具正文

#### Scenario: 请求完整工具结果

- **WHEN** 调用方明确请求工具结果并且通过服务端大小限制
- **THEN** 系统根据 v2 item 的 SQLite source offset 读取对应 JSONL，并在预算耗尽时返回 bounded detail cursor；v1 source 只能由一次性 migration/import operation 读取

#### Scenario: 隐藏内部消息

- **WHEN** 调用方未请求内部消息
- **THEN** 系统只返回公开可见内容，visibility 为 internal 的 runtime notice 不会被误当成用户消息或 Turn root

#### Scenario: 加载工具摘要和模型最终响应

- **WHEN** 调用方选择用户消息、tool_summary 和模型最终响应
- **THEN** 每个完整 Turn 返回 root user input、工具名称与状态摘要和 `TurnRecord.final_item_id` 对应的最终 assistant projection，不返回工具参数或完整结果

#### Scenario: 加载工具调用、结果和模型最终响应

- **WHEN** 调用方选择用户消息、tool_call、tool_result 和模型最终响应
- **THEN** 每个完整 Turn 返回用户 root、工具调用、对应工具结果和最终 assistant projection，并遵守详情大小限制

### Requirement: 历史与 live 使用同一 Turn response part 模型

系统 SHALL 为历史 summary、历史 detail 和 live 流式事件提供同一套有序 response part 语义模型。part 的来源坐标必须能够表达 v1 `AIMessage.content` 块、v2 canonical item/content part、reasoning item、tool call identity/call index、tool result 的 `tool_call_id`、canonical `compaction_summary` identity/view revision和`assistant_output` 的 text projection；不得要求历史事件拥有 live 流式 delta 才能渲染。live 尚未提交到 rollout 时可以只携带稳定 `part_id`，不得伪造 JSONL item/message sequence 或新增全局 part index。`compaction_summary`是独立response part类型，不能降级为thinking、assistant text或final response。

#### Scenario: 历史 summary 只缺少投影细节

- **WHEN** 首次加载返回用户消息、reasoning summary、tool summary 和最终文本
- **THEN** 前端将其转换为与 live 相同的 response part 类型，缺失的中间文本和工具正文只标记为未加载，不生成伪造的 delta 事件

#### Scenario: summary include 不泄漏未请求的中间部件

- **WHEN** summary 请求只包含 `user`、`reasoning_summary`、`tool_summary` 和 `final_response`
- **THEN** 返回 reasoning summary、工具名称/状态和最终文本；普通 reasoning、encrypted reasoning、tool_call 参数和 tool_result 正文均不得被 response-part adapter 直接返回

#### Scenario: live 工具缺少真实调用 ID

- **WHEN** live 工具事件只有流式 `part_id` 而没有真实 `tool_call_id`
- **THEN** 前端使用稳定 `part_id` 关联本次调用和结果，但不得把 `part_id` 序列化为 LangChain `tool_call_id`

#### Scenario: 历史 detail 补齐同一 Turn

- **WHEN** 用户展开 Turn 详情
- **THEN** Saver 通过 SQLite item/message source 坐标和命中的 rollout JSONL 记录返回完整有序 response parts，前端替换该 Turn 的 summary projection，用户看到的排序与 live 一致

#### Scenario: 历史不回退到旧消息字段渲染

- **WHEN** 历史 Turn 返回 `response_parts`，但没有 live SSE events
- **THEN** 前端只将 `response_parts` 转换为 TimelineItem 并交给统一 ResponsePart renderer，不再从 `assistant_text`、`thinking_blocks`、`tool_summary` 或旧 trace 字段拼出第二套消息
- **AND** summary 缺少的中间内容保持为未加载状态，只有用户请求 detail 后才通过同一 response-part 模型补齐

#### Scenario: tool_call 位于 assistant output 之后

- **WHEN** v2 assistant output/content parts 后存在 tool_call 与匹配的 tool_result
- **THEN** 渲染顺序由 canonical item/content-part identity 和 tool relation 决定，不用新的全局 part index 改写 canonical 顺序

#### Scenario: 后端返回权威逻辑 Item 统计和计时

- **WHEN** history summary 或 detail 读取一个 Turn
- **THEN** SQLite `turn_item_projection`按`logical_item_ordinal`返回每个`expandable_activity`中间part的`item_id`、`item_sequence`、`part_ordinal`、`created_at`和`elapsed_ms`，并从同revision显式membership返回不包含user/final的`item_count`与Turn`duration_ms`；首项elapsed相对Turn acceptance/root，后续项相对前一个计数成员，duration从acceptance到terminal。`first_item_sequence`/`last_item_sequence`仅是诊断坐标，不得以首尾相减、物理范围或邻接推断数量、顺序或时间
- **AND** 可见reasoning/summary part、tool_call参数、tool_result及显式Turn-member compaction summary各按logical identity计1；聚合tool row、final/user、transport chunk、encrypted-only标记、request-only/CSM/隐藏runtime项和重复carrier不计。同一assistant carrier中的多个tool call按各自`part_ordinal`计数，checkpoint/provider重影只按持久producer identity/tool relation解析，禁止按正文相等去重；时间缺失、倒退或revision不一致显式失败

#### Scenario: queued root 不计入前一个 Turn

- **WHEN**busy thread先提交下一Turn的queued root，前一Turn随后才物理追加tool result、reasoning或final item
- **THEN**两个Turn都按各自显式logical membership/order统计与展示；前一Turn的首尾物理sequence之间出现下一Turn root不会增加其`item_count`、改变`elapsed_ms`或打乱刷新后的DOM顺序

#### Scenario: history detail 只补正文且前端整体替换

- **WHEN** detail 通过索引命中目标 JSONL item/message offset 并读取工具参数或结果正文
- **THEN** detail 只丰富后端已有 canonical part，不重新推断 identity、数量或顺序；Web 以返回数组整体替换同 Turn 的旧 summary/live response parts，不合并重排、不比较文本去重

#### Scenario: 最终 checkpoint 引用既有 reasoning part

- **WHEN** 最终 assistant checkpoint 的 typed `content_part_refs.id` 指向同一 Turn 已提交 reasoning Item 的 `projection_identity.block_id` 或相同 provider reasoning item identity
- **THEN** history projection 将它视为既有逻辑 Item 的 carrier 引用，只返回原 canonical reasoning 的 identity、顺序和计时，不按最终 assistant item 再追加一次
- **AND** 只有正文相等但持久 part/provider identity 不同的 reasoning 必须继续分别返回，后端不得退回正文去重

#### Scenario: live 统计与终态 history 收敛

- **WHEN** live Turn 尚未持久化完整 history projection
- **THEN** Web 可按 message stream 实体 identity 临时计算时长和逻辑 Item 数，不伪造 canonical item sequence
- **AND** 终态 history 到达后以后端统计和顺序替换 live 值；若 live/history `item_count` 不一致，前端显式记录协议错误而不是静默采用任一猜测值

#### Scenario: 历史页携带可淘汰详情的权威摘要

- **WHEN** history API 返回一页 Turn detail
- **THEN** 同一响应为每个 detail 返回 identity、revision、ordinal 和顺序一一对应的后端权威 summary；summary 强制使用 summary projection，并移除工具参数、工具结果和其它详情正文
- **AND** Web 详情缓存超过条数或正文预算、或会话转为非活动 scope 时，只能恢复这份同 revision summary；不得在前端重新摘要，重新展开时通过 canonical history offset 定点补载

#### Scenario: 历史详情与实时消息流隔离并有界驻留

- **WHEN** 用户展开已终态化 Turn、切换会话、重启后端，或实时 block/tool 正文持续增长
- **THEN** 历史详情只请求 canonical Turn history API，不查询或拼接 `message.v1` availability/snapshot；实时消息流只承载当前未终态 Turn
- **AND** 前后端限制终态 stream cache、乱序事件、block 正文、工具正文、跨会话 timeline 和详情正文的驻留规模；启动恢复不 materialize 全部终态 snapshot，越界时明确截断或要求 snapshot 恢复，不得无界增长、静默丢失或卡死服务

### Requirement: 所有 rollout 数据访问使用唯一 RolloutCheckpointSaver

系统 SHALL 通过 `RolloutCheckpointSaver` 作为 checkpoint、Web history、fork context、Turn 状态和 `ContextRequestPlan`/`ContextAssemblySnapshot` 的唯一业务层入口。Saver MUST 在正常运行时只使用 v2 reader 解析已提交 SQLite view/range，验证 branch/view lineage，并提供 projection、detail、full 三种模式；v1 reader 不属于 Saver runtime API，只能由独立的一次性 `legacy_import_v1_to_v2` migration/import operation 调用。业务 service、middleware adapter、LangChain/Provider projector 只能消费 Saver 返回的已提交 plan/snapshot 与显式 runtime contribution，不得直接扫描 `RolloutStorage`、`AppendWriter` 或内部 context reader，也不得自行组合低层 SQLite 和 JSONL primitive。未 sealed 的内存 ledger 不是业务层可消费的恢复事实。

Saver 返回的 `ContextRequestPlan` 结构必须包含 `plan_id`、`plan_state`、可空的 `assembly_id`、`refs[]`（ContextRef registry）、`tool_set_refs[]`（ToolSetRef registry）、`contributions[]` 和唯一有序的 `selection[]`。`create_context_plan` 先在 owner-thread namespace 产生唯一 `plan_id` 和 `plan_state=unsealed`；在 Saver 成功 seal 前 `assembly_id=NULL`、`selection=[]`，未 seal plan 不是可恢复/可 dispatch 的 sealed assembly，且不存在 `ContextSelectionEntry` 或 `plan_ordinal`。ContextRef 在 draft 只按 `(session_id, thread_id)` 加 item/plan registry identity 解析，不携带 assembly binding；最终 request-only `detail_ref` 也不得在 draft 解析物理 assembly path。Saver 只有在 source/manifest 完整校验、必要 detail 已解析/物化后分配 thread-local `assembly_id`、生成 selection/ordinal 和 sealed detail manifest，并提交 `ContextAssemblySnapshot`；seal 成功后 plan 与 snapshot 的 `plan_id`/`assembly_id` 逐字段不可变且一对一。plan 创建幂等和 seal 幂等使用不同的 owner-thread-scoped keys，冲突分别返回 `plan-idempotency-conflict` 和 `assembly-idempotency-conflict`；空 selection 只有在已分配 assembly 的 sealed snapshot 中合法。

每个 `ContextSelectionEntry` 必须带 `assembly_id`、`plan_ordinal`、tagged-union `ref`、`selection_kind`、`included`、omission 时的 `omission_reason`、`loss[]`、visibility/protection/availability、base/delta role 和 overlay epoch。只有 `included=true` 的 entry 才强制 source revision、logical `content_length` 和恰一个 `content_hash` 或 `redacted_stable_digest`；其中 included=true 的 request-only entry 还必须带解析为 `{session_id,thread_id,assembly_id,detail_id}` 的最终 `detail_ref`，有 contribution-backed source 时还必须带 `contribution_ordinal`。`included=false` 只允许 optional omission，仍保留 tagged source ref、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 source identity；source revision、length、hash token、detail_ref、contribution_ordinal 可以为 null/未分配，已知 metadata 必须逐字段等于对应 manifest。`selection_kind=canonical_history|request_only|overlay_base|overlay_delta` 时 `ref` 恰为一个 `ContextRef{session_id,thread_id,ref_type,ref_id}`；`selection_kind=tool_set` 时 `ref` 恰为一个独立 `ToolSetRef{ref_type=tool_set,ref_id=tool_set_snapshot_id}`，不把 `tool_set` 解释为 ContextRef 类型。ContextRef 的 assembly binding 不在 ref 本体中，而在 sealed entry/manifest 的 `assembly_id` 中；draft request-only ref 不解析 assembly detail，omitted request-only entry 不拥有最终 `detail_ref`。included entry 的 length/hash/source 一律按权威矩阵从对应 manifest 读取，不得从当前 source/registry 回退；被 selection 绑定且 included 的 `ContextContribution.request_only` 必须为 `true`，omitted entry 不分配 `contribution_ordinal`，ToolSetRef 永不绑定 contribution ordinal。selection 中存在的字段必须与对应 registry/detail manifest 完全一致；缺项、重复、union 类型与 selection_kind 不匹配或顺序绑定不一致返回 `plan-order-integrity`，canonical/tool-set source 不一致返回 `source-mismatch`，request-only detail 不可用/不一致返回 `detail-unavailable`。`refs[]`、`tool_set_refs[]` 与 `contributions[]` 只是 registry，selection 才是 history、LangChain 和 native Provider 共同消费的顺序权威；Provider 只把 included ToolSetRef 投影到 tools/tool-config，history 只保留策略允许的 ToolSetRef metadata，不将工具定义拼成 canonical message；optional omitted canonical/request-only/tool_set entry 在 restore/history 中只保留 omission/loss，分别跳过正文、detail 和工具定义，不从当前 source/registry 回退。

矩阵明细见 `add-itemized-rollout-context` 的 requirement「selection_kind 与 ref_type 必须使用唯一兼容矩阵」；本节不再复述。history/restore 仍 SHALL 在 restore 之前按该权威矩阵校验，矩阵外组合返回 `plan-order-integrity`，不得改变 entry 生命周期。

included/omitted 各 selection 行的字段归属见权威矩阵，本节不复述。history/restore 的 capability 义务是：included 且 contribution-backed 的 request-only/overlay entry 必须以非空 `contribution_id` 在同一 `(session_id, thread_id, plan_id)` 唯一解析到该 entry 的 `contribution_manifest`，再由 manifest 读取正文/detail 并逐字段校验；普通非 contribution-backed 的 included request-only 只按同 assembly 的 `detail_ref`/source manifest 读取和校验，不要求 `contribution_id`/`contribution_ordinal`。restore/history/projector 不得根据 ref_id、detail_ref、hash 或 ordinal 搜索/猜测另一 contribution。

其中 included `canonical_history` ref 的 `content_length` 必须等于 `item_catalog.payload_length`，included request-only/overlay ref 的 `content_length` 必须来自同一 assembly 的 sealed detail/contribution source manifest，included `tool_set` ref 的 `content_length` 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest；三者都不能用 JSONL line offset/length、wire message 长度或当前文件长度替代。included request-only contribution 的 `content_hash` 必须覆盖 `{ "contribution_kind": <kind>, "body": <typed body> }` 的 JCS bytes；受保护正文使用显式 `redacted_stable_digest` 和 protected manifest 的内部 hash。optional omitted entry 不要求正文完整性字段，且不得由 history 从旧 message、当前 source 文件或 refs registry 重新拼接；history/restore 只展示或标记 omission/loss。included selection manifest 不完整时返回 `source-mismatch`/`detail-unavailable`，required source 的 omitted/unavailable detail 直接拒绝 seal/dispatch。

history/restore 在消费 selection 前必须校验每个 ToolSetRef 的 `tool_set_snapshot_id`、`source_revision`、`content_length`、`content_hash|redacted_stable_digest`、`tool_set_schema`/`tool_set_schema_version`、`tool_policy_version` 与同一 `tool_set_manifest[]` 的 `tool_policy` 逐字段相等，并把这些 manifest identity 纳入 `context-plan-hash:v2`。schema/config、policy 或 source revision 变化不得静默复用旧 plan；应报告 `plan-hash-mismatch`/`source-mismatch`，不可用 manifest 报告 `detail-unavailable`。`ContextContribution.contribution_kind` 不允许 `tool_set`，legacy 记录只能 quarantine；history 不把 contribution 或 ToolSetRef 伪造成 message。

#### Scenario: Saver 提供已提交 selection 给历史读取

- **WHEN** history/restore 请求一个已 sealed 的 ContextAssemblySnapshot
- **THEN** 读取面使用 Saver 返回的同一 `selection[{plan_ordinal, ref}]` 和 manifest，不从低层 storage、当前 source 或 registry 重建顺序

## ADDED Requirements

### Requirement: checkpoint/history loading 必须显式选择 product thread

checkpoint restore、history loading、detail lookup 和 ContextRequestPlan materialization SHALL 以 `(session_id, thread_id)` 解析 owner。未指定 thread 的 Session-facing 产品入口只可从 Session catalog 解析 main thread；内部 API、subagent、retry、rewind 和 compaction 不得依赖该默认。LangGraph `checkpoint_ns` 只在已选定 thread 内继续限定 framework checkpoint，不得改变 owner selection。

#### Scenario: 相同 namespace 的不同 thread 恢复隔离

- **WHEN** 两个 SessionThread 均请求相同 `checkpoint_ns` 的 checkpoint
- **THEN** loader 仅从各自 thread node 读取相应 checkpoint/context view；不得因 namespace 相同返回或合并另一个 thread 的 messages、ToolSet 或 source state

### Requirement: checkpoint/history restore 必须复用 sealed resource activation snapshot

checkpoint restore、history loading、retry、rewind、compaction 和 exact replay SHALL 通过 `RolloutCheckpointSaver` 读取 assembly 已提交的 `ResourceActivationSnapshotRef` 与 `ResourceProvenanceRef`，不能调用 ResourceRegistry、Virtual Resource Resolver、resource loader 或 monitor provider 获取当前内容。`snapshot_kind=turn`时恢复原policy与turn-bound binding；`snapshot_kind=model_call`时必须恢复其`parent_turn_snapshot_id`、逐字节相同的turn-bound binding和该call自己的model-call-bound binding。loader不能把最新Registry generation或配置policy批量覆盖到历史assembly，也不能用一个boundary单值重建混合binding。

loader MUST 校验 resource ref 的 owner session/thread、assembly、activation ordinal、resource identity、revision、availability、length/hash marker 和 protected detail capability。`display_uri` 只用于安全展示和经授权的显式新解析，不是 restore lookup key；历史正文必须按 sealed `snapshot_ref|detail_ref` 定点读取。ref 缺失、owner 冲突、hash 不匹配或正文已被 retention 清理时返回 `source-mismatch`、`detail-unavailable` 或明确 loss，禁止回退文件系统、网络、内存源、当前 Registry 或旧 path 字段。

#### Scenario: rewind 后恢复旧 Turn 的资源版本

- **WHEN** rewind 将 active view 移到仍引用 Skill revision R1 的已提交 Turn，而 Registry 当前 revision 为 R3
- **THEN** history/restore 继续从 sealed ref 读取 R1；CSM 是否为后续新请求重新激活 R3 属于新的 activation/assembly，不改写 R1、不把旧 ref 指向当前 URI

#### Scenario: 当前 locator 不可用

- **WHEN** 资源的绝对路径、网络 endpoint 或内存 key 已不存在，但 sealed body 仍在 retention 范围内
- **THEN** loader 不解析当前 locator，直接从受保护 snapshot/detail 恢复；若 sealed body 也不可用则显式返回 loss/error，而不是读取同名新资源或空内容

#### Scenario: request path 不做资源 I/O

- **WHEN** Saver 为 retry、resume 或后续 tool-loop model call materialize 已提交 plan
- **THEN** 资源选择只来自内存中已冻结 activation snapshot或持久 sealed refs，trace 证明没有额外 `stat`、scan、read、HTTP fetch 或 provider source lookup

### Requirement: 正常历史 reader 只服务 v2，v1 仅用于一次性 migration/import

正常历史 reader SHALL 使用 SQLite manifest/database metadata 的 `rollout_format_version` 和 JSONL envelope 的 `format_version` 做双重 dispatch，并只接受 v2 item-line；v2 通过 `item_catalog`、Turn root、context view 和 item/content-part projection 提供读取。v1 message-line 只能由显式一次性 `legacy_import_v1_to_v2` migration/import reader 读取并写入 migration report/staging，不能提供正常 history projection、checkpoint restore 或 Provider request。版本不一致、未知版本、migration 状态不完整或正常 API 发现 v1 时 MUST 返回明确错误，不得把 v1/v2 混合成一个历史事实。

#### Scenario: 显式 migration 的 v1 预览

- **WHEN** 用户显式调用 `legacy_import_v1_to_v2`，migration reader 读取 v1 session
- **THEN** 只在 migration report/staging 中按 v1 source 坐标生成预览或待导入 parts，明确标记 migration 状态，不把结果作为 history API、checkpoint 或 Provider 的兼容 response，也不声称已经完成 v2 item migration

#### Scenario: v2 assistant text projection

- **WHEN** history API 读取 v2 `assistant_output` item
- **THEN** API 从其 text content part 返回 `assistant_text` projection，并保留 canonical item identity；不得在 SQLite 中创建名为 `assistant_text` 的第二个 canonical 事件

#### Scenario: migration 失败

- **WHEN** v1 到 v2 migration 在临时 artifact 校验或安装前失败
- **THEN** 正常 history 返回 `v1_migration_required` 或 migration incomplete 错误；v1 原 artifact 不被覆盖，不能返回半成品 v2 view，也不能重新启用 v1 只读 history 路径

### Requirement: selection_kind 与 ref_type 的兼容矩阵必须唯一登记且被历史读取消费

本 requirement MUST NOT 登记第二份 `selection_kind` 与 tagged-union `ref` 兼容矩阵；唯一权威登记是 `add-itemized-rollout-context` 的 `specs/itemized-rollout-context/spec.md` 的 requirement「selection_kind 与 ref_type 必须使用唯一兼容矩阵」。history/restore SHALL 在 source lookup、detail 解析和 message/tool projection 之前按该权威矩阵校验 `ContextSelectionEntry.selection_kind` 与 tagged-union `ref`；included 与 omitted entry 都必须满足同一 tag/type 关系。矩阵外组合必须返回 `plan-order-integrity`，不得按历史 role、`ref_id` 或当前 registry 改派。

`ContextRef.ref_id` 仍是 canonical `item_id` 或 request-only 的稳定逻辑 alias（普通 request-only 通常等于其 `contribution_id`，亦可为 producer 声明 source alias；overlay 为 overlay alias）；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref`/source manifest 读取正文；included 且 contribution-backed 的 request-only/overlay 才必须通过非空 `contribution_id` + `contribution_ordinal` 唯一读取同一 `(session_id, thread_id, plan_id)` 的 contribution manifest/body，并校验 detail、source revision、logical length、hash 和 ordinal，overlay 本身必须 contribution-backed。canonical/tool_set 的 contribution_id/ordinal/detail_ref 必须 NULL。omitted entry 仍必须保留矩阵规定的 tag/type，正文和 contribution/detail binding 可 NULL/未分配，已知 identity metadata 必须一致；required omission/detail failure 不得恢复为部分成功。history/restore 与 LangChain/native Provider projector 对 omitted entry 只保留 omission/loss，并分别跳过 canonical message、request-only detail/body、overlay 应用和 tool definition。

#### Scenario: history 在 source lookup 前拒绝 union mismatch

- **WHEN** `canonical_history` 使用 request-only、`request_only|overlay_base|overlay_delta` 使用 canonical_item，或 `tool_set` 使用非 ToolSetRef
- **THEN** history/restore 返回 `plan-order-integrity`，不读取 item/contribution/detail/tool registry，不生成替代 message/tool projection

#### Scenario: history/restore 保留 omitted entry 的矩阵 tag

- **WHEN** optional source 在 sealed selection 中为 `included=false`
- **THEN** 保留对应 tag/type、assembly/plan ordinal、omission/loss/availability 和可得 identity；omitted canonical_history/tool_set 的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为 NULL；omitted request-only/overlay 可保留已有且与同一 manifest 一致的 `contribution_id`，但不得新分配 contribution_id/ordinal 或读取正文/detail，没有既有映射则为 NULL；其它正文 length/hash、detail_ref、contribution_ordinal 可不分配
- **AND** history/restore 只展示 omission/loss metadata，不从当前 source 或 registry 回退，也不生成空 message、detail 或 tools

#### Scenario: 默认 Web projection

- **WHEN** Web 请求最新 Turn 或向前加载历史摘要
- **THEN** RolloutCheckpointSaver 使用内部 reader 的 projection 模式，不调用 full materialize，不构造完整 checkpoint messages 列表

#### Scenario: LangGraph 恢复 full

- **WHEN** LangGraph saver 或 context fork 需要可执行消息列表
- **THEN** Saver 从 active canonical view 构造 ContextRequestPlan，再返回按目标能力投影的 BaseMessage，不把 request-only prompt 或 pending notice 伪造成历史 user message

#### Scenario: history 与 request projector 共享 selection/order

- **WHEN** history projection、LangChain restore 和 native Provider request 读取同一 active view 或同一 source overlay epoch
- **THEN** 三者都消费 Saver 已提交 snapshot 的 `selection[{plan_ordinal, ref}]`；request-only contribution 使用持久 `contribution_ordinal` 与 canonical/base→delta 顺序，ToolSetRef 使用同一 `plan_ordinal` 进入 Provider tools/tool-config，history 仅保留受策略控制的 metadata；不按 `created_at`、`contribution_id`、物理邻接或 projector 本地 prepend 规则重排
- **AND** included canonical ref/contribution 正文必须与 sealed `source_revision`、逻辑 `content_length` 和 `content_hash` 或 `redacted_stable_digest` 匹配；optional omitted entry 只校验其 tagged ref、`plan_ordinal`、omission/loss/availability 及可得 identity metadata，不读取正文或 detail；缺失、覆盖或错误 source 返回 `source-mismatch`/`detail-unavailable`，不得返回看似完整的部分 request

#### Scenario: history/restore 保留 optional omission

- **WHEN** sealed snapshot 中存在 `included=false` 的 optional canonical、request-only 或 tool-set entry
- **THEN** history 与 restore 保留该 entry 的 tagged ref、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 source identity，但不生成 canonical message、request-only message/detail 或 Provider tool definition
- **AND** projector 不从当前 registry/source 回退、不创建空值；如果同一 source 在该 assembly 中是 required，则 seal/restore 返回 `source-mismatch` 或 `detail-unavailable`，不得将 omission 视为成功

#### Scenario: 非法 view 所有模式统一失败

- **WHEN** SQLite view range、item membership 或 Turn root 缺失、越界或成环
- **THEN** projection、detail 和 full 都返回明确 context view 错误，不返回部分结果或按最大 sequence 猜测边界

#### Scenario: 旁路读取被禁止

- **WHEN** 业务 service 或 projector 试图直接读取 RolloutStorage、AppendWriter 或内部 context reader 以构造 ContextRequestPlan
- **THEN** 系统拒绝该旁路访问；只有 RolloutCheckpointSaver 返回的已提交 view/plan/snapshot 可以进入编译与请求投影

### Requirement: 内部 execution history 由持久 admission 独立加载

历史 loader MUST 从目标 `(session_id, thread_id)` 的持久 `ExecutionAdmission`、execution owner state 与 committed canonical items 加载内部 execution history。无 cursor 时，有效 include 由请求中明确提供的集合决定；未提供时，tail 使用配置 `initial_include`，before/after/around 使用 `anchor_include`。两套内置默认集合各加入 `internal`，并保留原消息/summary 选择；显式配置覆盖按实际集合执行，不再新增 `default_include`。continuation 未提供 include 时沿用 token 冻结的有效集合，显式提供时必须相同；不得重新按方向或当前配置计算默认。只有有效 include 包含 `internal` 时，响应才填充顶层 `internal_executions[]`。Web 的默认集合保留原 messages/summary 并包含 `internal`；明确排除时不得查询后再合并、不得把 internal execution 塞进普通 Turn summary/detail，也不得从 source completed Turn 或邻接项推断归属。

`internal_executions[]` MUST 按 execution owner 身份返回 standalone history envelope；每项不得要求 `TurnDetailDTO`、`turn_id`、root 或 final pointer。`RuntimeNoticePayload` 的 history projection 只读取 `display_part`，Provider/checkpoint 消费的 `prompt` 不得出现在 Web history。内部输出仍按该 `execution_id` 的 committed item identity/order 投影，不挂到 source Turn；普通 Turn 的分页、`item_count`、elapsed 和 `final_response` 不因内部 execution 改变。

internal admission MUST 在同一权威事务冻结真实 `source_branch_id` 与 `source_view_id`；幂等重入复用原绑定，不能改绑当前分支。其 notice 和同 execution 的 response items MUST 在既有 `context_view_items` 中明确登记为 `source_kind=display_only`；该 membership 是当前视图中可展示成员的唯一事实。checkpoint 后继、rewind 和 fork 必须按选定源成员显式继承或裁切，fork 同步重映射 target-local view/branch/execution/item identity；不得只凭 admission 的源 view、物理 sequence 范围或时间猜测当前成员，也不得新增第二 context tree。Provider 的 canonical history selection 只消费 `source_kind=canonical`，不从全局 catalog 补入 pending notice；当前 internal execution 的私有 prompt 及其自身已提交输出仍通过 admission 绑定的执行请求恢复路径消费，工具调用与结果保持协议闭合，不因展示成员隔离而丢失。

#### Scenario: 明确排除内部 execution

- **WHEN** history request 明确提供不包含 `internal` 的 include 集合
- **THEN** 响应不填充 `internal_executions[]`，`pending_next_turn` report-back 不成为独立 Turn 或普通 Turn 的 response part

#### Scenario: 显式加载内部 execution history

- **WHEN** include 显式请求 `internal` 且目标 thread 存在已持久 internal admission
- **THEN** loader 从该 admission 绑定的 execution 与 committed item 坐标返回独立 `internal_executions[]`；runtime notice 的 history projection 只使用安全 `display_part`，不得返回私有 prompt 或 branch result；默认 Web 只展示安全固定文案和标签，不渲染来源 ID。模型 prompt、控制 ID 和 source branch 正文不作为展示内容
- **AND** 输出不会被并入 source completed Turn，也不会增加任何 Turn 的 item count 或 elapsed 统计

#### Scenario: 分支切换不吸收其它分支的内部记录

- **WHEN** 两个分支分别有 internal execution，随后读取一个选定视图或执行 rewind/fork
- **THEN** 只返回该视图明确登记或继承的 display-only 成员；Provider 不因全局物理序号补入另一分支的 pending prompt，目标 internal dispatch 仍消费自己的 admission 输入

### Requirement: 同一历史游标有界推进 Turn 与内部 execution

系统 SHALL 在现有 history API 使用唯一 v2 opaque cursor，同时保存 Turn stream 的 exclusive `logical_turn_ordinal` 和 internal stream 的 exclusive canonical `item_sequence`。internal stream 按当前视图中显式可见、属于具有唯一 display notice 的 internal admission 的 canonical notice/response record 分页，单页最多 256 条 canonical record；初始 seed/reference admission 没有 notice 时不产生展示项。视图成员关系必须由持久 owner 明确登记，不得从物理邻接、时间或 metadata 推断。

v2 token MUST 绑定 `session_id`、`thread_id`、`checkpoint_ns`、`rollout_id`、`projection_epoch`、`view_id`、`history_view_revision`、首次同一 snapshot 的 canonical `item_sequence` 与 Turn ordinal 读上界、固定 `direction`、递增 `stage`、排序规范化的有效 include 集合、初始查询模式，以及 around 初始 `anchor_turn_id` 与 before/after window；同时保存可空的 `turn_ordinal`、`item_sequence`。continuation 延续初始查询模式与窗口，不能替换过滤条件、anchor 或窗口；请求省略这些参数时使用 token 冻结值，显式提供且不一致时返回 invalid cursor。未请求的 lane 不查询且明确由冻结 include 标为禁用；已启用 lane 的 anchor 为 NULL 只表示该方向真实耗尽，后续不得从头再读，也不能改 include 复活。两路必须在同一个 `RolloutReadSnapshot` 和 resolved view 中进行有界 keyset 查询；不得全量读取后截断。旧开发期 v1 cursor 直接以 invalid cursor 拒绝，不保留兼容解码。owner/direction 不符返回 invalid cursor，epoch/view/history_view_revision 漂移返回 stale cursor。首次读上界必须从真实 snapshot 与选定 view 的显式成员取得，不能以 message sequence 冒充 item 坐标；后续各 lane 查询不得超过这些上界，上界只定位不推 membership。不新建第二历史快照表，也不以活动 view ID 未变为由混入后续追加或终态修订。

每页仅投影本页命中的 response records，并从同一 snapshot 批量关联其 execution admission 和唯一安全 display notice；同一 execution 可跨页出现。Web MUST 按 `execution_id` 合并同一 entry，按稳定 canonical item/part identity 去重，不按正文去重；同一 entry 的 committed outputs 保持其权威成员顺序，standalone notice 与完整 Turn 的合并顺序复用 `session-turn-history`「Internal execution history 使用独立 envelope 和 typed display coordinate」规定的同 view membership ordinal，不使用活动统计范围、物理邻接或时间；重复关联的 display notice 不得重影。Turn 预算与统计保持原语义；`has_more` 由两路实际剩余记录的并集决定，两路耗尽时游标为空。

`around` SHALL 保持现有 `anchor_turn_id` 请求；storage 从选中 Turn window 的显式成员解析 canonical 边界，只把边界作为查询坐标，不作为 membership 判据。窗口内超过 256 条 internal record 时，后续 cursor 继续剩余 record，Turn anchor 保持已返回窗口的边界；空 internal 窗口也必须检查窗口两侧是否尚有可读记录。首尾没有 Turn 的 internal record 和零 Turn view 必须可达。

#### Scenario: 零 Turn 的内部历史仍可续读

- **WHEN** 目标视图没有用户 Turn，但存在超过 256 条可见 internal canonical record
- **THEN** head/tail 返回有界 internal-only 页面和相应 continuation cursor，逐页遍历全部记录而不合成 Turn、不重复耗尽的 Turn stream

#### Scenario: 单次内部 execution 的输出跨页

- **WHEN** 一个 internal execution 的 canonical notice、assistant、reasoning 或 tool records 超过单页预算
- **THEN** 同一 item_sequence lane 继续其后续 records，每页关联同一 admission/display；前端合并后每个真实 item/part 只出现一次，不静默截断输出、不引入第二 API 或 execution 专用分页 phase

#### Scenario: around 窗口的密集内部记录可继续

- **WHEN** anchor Turn 两侧窗口内存在超过 256 条 internal record，或窗口外仍有无 Turn 的 notice
- **THEN** before/after composite cursor 保留真实 continuation；Turn 与 internal anchors 分别推进，不重返已加载 Turn、不把截断窗口标为耗尽

#### Scenario: 游标不能跨 owner 或视图复用

- **WHEN** cursor 被用于其它 thread、namespace、方向，或原视图/epoch 已变化
- **THEN** loader 返回对应 invalid/stale cursor 错误，不混入另一 owner 或新视图的记录

#### Scenario: continuation 保留原 include 与查询窗口

- **WHEN** tail 或 around 返回 cursor 后，调用方省略 include 继续读，或尝试更换 include/around anchor/window
- **THEN** 省略时沿用 token 冻结条件，不因 continuation 方向改变而套用另一套默认；显式不一致时拒绝，不复活已禁用或耗尽 lane

#### Scenario: 活动视图原位修改使旧 cursor 失效

- **WHEN** 首页返回后，在同一 view/epoch 追加 item、更新 execution 终态或进行会改变 history_view_revision 的控制操作
- **THEN** continuation 以 stale cursor 明确拒绝，不能把新 revision 与原页混合；未变化时从首次 snapshot 冻结上界内续读
