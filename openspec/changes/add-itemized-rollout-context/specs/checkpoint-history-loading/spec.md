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

系统 SHALL 为历史 summary、历史 detail 和 live 流式事件提供同一套有序 response part 语义模型。part 的来源坐标必须能够表达 v1 `AIMessage.content` 块、v2 canonical item/content part、reasoning item、tool call identity/call index、tool result 的 `tool_call_id` 和 `assistant_output` 的 text projection；不得要求历史事件拥有 live 流式 delta 才能渲染。live 尚未提交到 rollout 时可以只携带稳定 `part_id`，不得伪造 JSONL item/message sequence 或新增全局 part index。

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
- **THEN** SQLite projection 按逻辑 Item 顺序返回每个中间 part 的 `item_id`、`item_sequence`、`part_ordinal`、`created_at` 和 `elapsed_ms`，并返回不包含 user/final text 的 `item_count`、`first_item_sequence`、`last_item_sequence` 与 Turn `duration_ms`
- **AND** 同一 assistant carrier 中的多个 tool call 按各自 `part_ordinal` 计数，tool_call 与 tool_result 分别计数；checkpoint/provider 重影只按持久 producer identity/tool relation 解析，禁止按正文相等去重

#### Scenario: history detail 只补正文且前端整体替换

- **WHEN** detail 通过索引命中目标 JSONL item/message offset 并读取工具参数或结果正文
- **THEN** detail 只丰富后端已有 canonical part，不重新推断 identity、数量或顺序；Web 以返回数组整体替换同 Turn 的旧 summary/live response parts，不合并重排、不比较文本去重

#### Scenario: 最终 checkpoint 引用既有 reasoning part

- **WHEN** 最终 assistant checkpoint 的 `content_part_refs.id` 指向同一 Turn 已提交 reasoning Item 的 `metadata.block_id` 或相同 provider reasoning item identity
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

Saver 返回的 `ContextRequestPlan` 结构必须包含 `plan_id`、`plan_state`、可空的 `assembly_id`、`refs[]`（ContextRef registry）、`tool_set_refs[]`（ToolSetRef registry）、`contributions[]` 和唯一有序的 `selection[]`。`create_context_plan` 先产生 session-local `plan_id` 和 `plan_state=unsealed`；在 Saver 成功 seal 前 `assembly_id=NULL`、`selection=[]`，未 seal plan 不是可恢复/可 dispatch 的 sealed assembly，且不存在 `ContextSelectionEntry` 或 `plan_ordinal`。ContextRef 在 draft 只按 `session_id` 加 item/plan registry identity 解析，不携带 assembly binding；最终 request-only `detail_ref` 也不得在 draft 解析物理 assembly path。Saver 只有在 source/manifest 完整校验、必要 detail 已解析/物化后分配 session-local `assembly_id`、生成 selection/ordinal 和 sealed detail manifest，并提交 `ContextAssemblySnapshot`；seal 成功后 plan 与 snapshot 的 `plan_id`/`assembly_id` 逐字段不可变且一对一。plan 创建幂等和 seal 幂等使用不同的 session-scoped keys，冲突分别返回 `plan-idempotency-conflict` 和 `assembly-idempotency-conflict`；空 selection 只有在已分配 assembly 的 sealed snapshot 中合法。

每个 `ContextSelectionEntry` 必须带 `assembly_id`、`plan_ordinal`、tagged-union `ref`、`selection_kind`、`included`、omission 时的 `omission_reason`、`loss[]`、visibility/protection/availability、base/delta role 和 overlay epoch。只有 `included=true` 的 entry 才强制 source revision、logical `content_length` 和恰一个 `content_hash` 或 `redacted_stable_digest`；其中 included=true 的 request-only entry 还必须带解析为 `{session_id,assembly_id,detail_id}` 的最终 `detail_ref`，有 contribution-backed source 时还必须带 `contribution_ordinal`。`included=false` 只允许 optional omission，仍保留 tagged source ref、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 source identity；source revision、length、hash token、detail_ref、contribution_ordinal 可以为 null/未分配，已知 metadata 必须逐字段等于对应 manifest。`selection_kind=canonical_history|request_only|overlay_base|overlay_delta` 时 `ref` 恰为一个 `ContextRef{session_id,ref_type,ref_id}`；`selection_kind=tool_set` 时 `ref` 恰为一个独立 `ToolSetRef{ref_type=tool_set,ref_id=tool_set_snapshot_id}`，不把 `tool_set` 解释为 ContextRef 类型。ContextRef 的 assembly binding 不在 ref 本体中，而在 sealed entry/manifest 的 `assembly_id` 中；draft request-only ref 不解析 assembly detail，omitted request-only entry 不拥有最终 `detail_ref`。included canonical ref 的 length/hash/source 来自已提交 `item_catalog`，included request-only ref/contribution 的对应值来自同一 assembly 的 sealed detail/source manifest，included ToolSetRef 的对应值来自同一 plan/assembly 的 `tool_set_manifest[]`；被 selection 绑定且 included 的 `ContextContribution.request_only` 必须为 `true`，omitted entry 不分配 `contribution_ordinal`，ToolSetRef 永不绑定 contribution ordinal。selection 中存在的字段必须与对应 registry/detail manifest 完全一致；缺项、重复、union 类型与 selection_kind 不匹配或顺序绑定不一致返回 `plan-order-integrity`，canonical/tool-set source 不一致返回 `source-mismatch`，request-only detail 不可用/不一致返回 `detail-unavailable`。`refs[]`、`tool_set_refs[]` 与 `contributions[]` 只是 registry，selection 才是 history、LangChain 和 native Provider 共同消费的顺序权威；Provider 只把 included ToolSetRef 投影到 tools/tool-config，history 只保留策略允许的 ToolSetRef metadata，不将工具定义拼成 canonical message；optional omitted canonical/request-only/tool_set entry 在 restore/history 中只保留 omission/loss，分别跳过正文、detail 和工具定义，不从当前 source/registry 回退。

`selection_kind` 与 selection union 的兼容矩阵固定为：`canonical_history` 只能绑定 `ContextRef.ref_type=canonical_item` 和 item catalog，且 contribution_id、contribution_ordinal、detail_ref、source_overlay_epoch 为 NULL；`request_only` 只能绑定 `ContextRef.ref_type=request_only`，included contribution-backed 时用非空 contribution_id/ordinal 和同 assembly detail_ref；`overlay_base|overlay_delta` 只能绑定 `ContextRef.ref_type=request_only`，included 时必须有 contribution_id/ordinal、detail、对应 base/delta role、source_overlay_epoch 和完整 source/diff chain；`tool_set` 只能绑定 `ToolSetRef.ref_type=tool_set`，base_delta_role=none 且 contribution_id、contribution_ordinal、detail_ref 为 NULL。included=false 仍必须满足相同行的 tag/type，source 可不解析且正文/binding 字段可 NULL；矩阵外组合在 restore 前返回 `plan-order-integrity`，不得改变生命周期。

included 且 contribution-backed 的 request-only/overlay entry 的 `contribution_id` 是其到 `contribution_manifest` 的唯一映射，必须在同一 `(session_id, plan_id)` 唯一解析，并与 `ContextRef.ref_id=plan_item_id`、最终 `detail_ref`/body locator、source revision、逻辑 length、hash token 和 `contribution_ordinal` 逐字段一致；普通非 contribution-backed 的 included request-only 只按同 assembly 的 `detail_ref`/source manifest 读取和校验，不要求 `contribution_id`/`contribution_ordinal`；`overlay_base|overlay_delta` 按矩阵必须 contribution-backed。canonical/tool_set entry（包括 omitted）的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为 NULL；只有 omitted request-only/overlay entry 可保留已有 `contribution_id`，若保留则必须与同一 manifest 一致，不得新分配 contribution_id/ordinal 或读取正文/detail，没有既有映射则为 NULL。restore/history/projector 不得根据 ref_id、detail_ref、hash 或 ordinal 搜索另一 contribution。

其中 included `canonical_history` ref 的 `content_length` 必须等于 `item_catalog.payload_length`，included request-only/overlay ref 的 `content_length` 必须来自同一 assembly 的 sealed detail/contribution source manifest，included `tool_set` ref 的 `content_length` 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest；三者都不能用 JSONL line offset/length、wire message 长度或当前文件长度替代。included request-only contribution 的 `content_hash` 必须覆盖 `{ "contribution_kind": <kind>, "body": <typed body> }` 的 JCS bytes；受保护正文使用显式 `redacted_stable_digest` 和 protected manifest 的内部 hash。optional omitted entry 不要求正文完整性字段，且不得由 history 从旧 message、当前 source 文件或 refs registry 重新拼接；history/restore 只展示或标记 omission/loss。included selection manifest 不完整时返回 `source-mismatch`/`detail-unavailable`，required source 的 omitted/unavailable detail 直接拒绝 seal/dispatch。

history/restore 在消费 selection 前必须校验每个 ToolSetRef 的 `tool_set_snapshot_id`、`source_revision`、`content_length`、`content_hash|redacted_stable_digest`、`tool_set_schema`/`tool_set_schema_version`、`tool_policy_version` 与同一 `tool_set_manifest[]` 的 `tool_policy` 逐字段相等，并把这些 manifest identity 纳入 `context-plan-hash:v2`。schema/config、policy 或 source revision 变化不得静默复用旧 plan；应报告 `plan-hash-mismatch`/`source-mismatch`，不可用 manifest 报告 `detail-unavailable`。`ContextContribution.contribution_kind` 不允许 `tool_set`，legacy 记录只能 quarantine；history 不把 contribution 或 ToolSetRef 伪造成 message。

#### Scenario: Saver 提供已提交 selection 给历史读取

- **WHEN** history/restore 请求一个已 sealed 的 ContextAssemblySnapshot
- **THEN** 读取面使用 Saver 返回的同一 `selection[{plan_ordinal, ref}]` 和 manifest，不从低层 storage、当前 source 或 registry 重建顺序

### Requirement: selection_kind 与 ref_type 必须使用唯一兼容矩阵

history/restore SHALL 在 source lookup、detail 解析和 message/tool projection 之前按下表校验 `ContextSelectionEntry.selection_kind` 与 tagged-union `ref`；included 与 omitted entry 都必须满足同一 tag/type 关系。矩阵外组合必须返回 `plan-order-integrity`，不得按历史 role、`ref_id` 或当前 registry 改派：

| `selection_kind` | 唯一合法 ref | `included=true` 合同 | `included=false` optional 合同及 history/restore 行为 |
|---|---|---|---|
| `canonical_history` | `ContextRef.ref_type=canonical_item` | 只读同一 session `item_catalog`；source revision、logical length、恰一个 hash token 必填；`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` NULL，`base_delta_role=none` | 保留 canonical tag/id、plan ordinal、omission/loss/availability 和可得 identity；不生成 canonical message、不读当前 item |
| `request_only` | `ContextRef.ref_type=request_only` | detail_ref 必须解析同 assembly sealed detail；contribution-backed 时非空 contribution_id/ordinal 唯一指向同一 plan contribution manifest；base role none、source epoch NULL | 保留 request-only tag/id、plan ordinal、omission/loss/availability 和可得 identity；跳过 detail/body，不回退当前 middleware/source |
| `overlay_base` | `ContextRef.ref_type=request_only` | 必须 contribution-backed，非空 contribution_id/ordinal、detail、source revision/length/hash、`base_delta_role=base`、source epoch，并绑定完整 base | 保留 request-only tag/id、plan ordinal、base role/可得 epoch；不应用 base、不重建 overlay |
| `overlay_delta` | `ContextRef.ref_type=request_only` | 必须 contribution-backed，非空 contribution_id/ordinal、detail、source revision/length/hash、`base_delta_role=delta`、source epoch，并校验 from/to revision、diff algorithm/version、diff hash chain | 保留 request-only tag/id、plan ordinal、delta role/可得 epoch；不应用 delta、不重建 overlay |
| `tool_set` | `ToolSetRef.ref_type=tool_set` | 只读同一 plan/assembly ToolSetSnapshot manifest；manifest source/length/hash/schema/policy 必填；base role none，contribution_id/ordinal/detail_ref/source epoch NULL | 保留 tool_set tag/id、plan ordinal、omission/loss/availability 和可得 identity；不生成工具定义、不回退 registry 或空 tools |

`ContextRef.ref_id` 仍是 canonical `item_id` 或 request-only `plan_item_id`，不是 contribution identity；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref`/source manifest 读取正文；included 且 contribution-backed 的 request-only/overlay 才必须通过非空 `contribution_id` + `contribution_ordinal` 唯一读取同一 `(session_id, plan_id)` 的 contribution manifest/body，并校验 detail、source revision、logical length、hash 和 ordinal，overlay 本身必须 contribution-backed。canonical/tool_set 的 contribution_id/ordinal/detail_ref 必须 NULL。omitted entry 仍必须保留矩阵规定的 tag/type，正文和 contribution/detail binding 可 NULL/未分配，已知 identity metadata 必须一致；required omission/detail failure 不得恢复为部分成功。history/restore 与 LangChain/native Provider projector 对 omitted entry 只保留 omission/loss，并分别跳过 canonical message、request-only detail/body、overlay 应用和 tool definition。

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

## ADDED Requirements

### Requirement: checkpoint/history loading 必须显式选择 product thread

checkpoint restore、history loading、detail lookup 和 ContextRequestPlan materialization SHALL 以 `(session_id, thread_id)` 解析 owner。未指定 thread 的 Session-facing 产品入口只可从 Session catalog 解析 main thread；内部 API、subagent、retry、rewind 和 compaction 不得依赖该默认。LangGraph `checkpoint_ns` 只在已选定 thread 内继续限定 framework checkpoint，不得改变 owner selection。

#### Scenario: 相同 namespace 的不同 thread 恢复隔离

- **WHEN** 两个 SessionThread 均请求相同 `checkpoint_ns` 的 checkpoint
- **THEN** loader 仅从各自 thread node 读取相应 checkpoint/context view；不得因 namespace 相同返回或合并另一个 thread 的 messages、ToolSet 或 source state

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
