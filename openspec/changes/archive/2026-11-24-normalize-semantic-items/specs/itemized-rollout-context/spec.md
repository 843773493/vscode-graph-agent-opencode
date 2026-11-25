## MODIFIED Requirements

### Requirement: Canonical item 具有稳定身份和语义顺序

`CanonicalItemRecord.status` 完整枚举固定为 `completed | partial | incomplete | cancelled | failed | unknown`，六者在 JSONL 中都表示终态；`completed` 表示 semantic item 的声明 payload 已完整收敛并可按 schema 正常投影，`partial` 表示截至中断/停止边界已持久化的完整 payload 快照但尚未达到正常语义完成边界；二者都只能写成一个 immutable JSONL item，不能把已提交的 `partial` 原地改成 `completed`。`open`、`active`、`running`、`draft` 和 `completed_empty` 只允许出现在内存 draft 或 assembly/Turn/control state，不能写入 canonical item。ItemDraft 只能从内部 `draft` 转移到上述六个终态之一；item 写入后不得更新、删除、覆盖、插入重排或改变 status。retry、resume、纠错必须追加新的 item identity，并用 `retry_of`/`resumes`/`supersedes` 关系连接旧 item；没有稳定 payload 的崩溃 draft 只能通过 control outcome 记录 execution lost，不能补造 `status=unknown` item。非 `completed` item 不得作为正常 final response；tool result 的已提交 payload 如果外部执行结果未确认，必须在其 typed payload 内使用 `tool_outcome=unknown` marker，且不可作为成功 replay input。`tool_outcome` 不是 `CanonicalItemRecord.status`，执行/控制记录的 outcome 也不得引入带 outcome 前缀的 unknown 状态别名。

系统 SHALL 将用户输入、assistant 输出、reasoning、tool call、tool result、需要持久化的 runtime notice、压缩摘要和未知 Provider 扩展表达为带 schema version 的 `CanonicalItemRecord`。v3 的必填核心字段必须非空且同时存在：`format_version=3`、`record_type=item`、`item_sequence`、`item_id`、`semantic_kind`、`payload_kind`、`status`、`producer_ref`、`payload`、`content_hash`、`created_at` 和 `extensions`；其中 `extensions` 至少是空 object，`producer_ref` 是单一完整 payload producer，`payload` 必须与 `payload_kind` 匹配。`turn_id`、`turn_scope`、`message_group_id`、`projection_group`、`projection_identity`、`tool_result_evidence`、`source_manifest` 和 `wire_role` 是按语义可空/可省略的 typed 关联或投影字段，不得被 reader 当成隐含默认值；v3 item-line 禁止通用 `metadata` 顶层字段。`semantic_kind` MUST 使用固定枚举 `user_input | assistant_output | reasoning | tool_call | tool_result | runtime_notice | compaction_summary | attachment | extension`，`payload_kind` MUST 使用完整枚举 `text | structured_content | tool_call | tool_result | summary | attachment_ref | opaque | extension`。不得同时使用含义重叠的通用 `kind` 作为 canonical 语义字段。`assistant_text` 和 `final_response` 是 projection，不是 `semantic_kind` 枚举值。只对当前请求生效的静态或动态 system context 不得因为最终使用了 system/developer wire role 就自动成为 canonical item。

`ProjectionGroup(ordinal,size,content_form)` 的 `content_form` 只能为 `str|list`；它在明确 `message_group_id` 下记录真实消息，要求 `size>=1`；单成员在自身payload不能表达原 `str|list` 形态时同样用它保留（如一次纯工具调用的空数组正文），避免还原为空字符串；已有独立流式block不强制补成完整消息组。多成员的 ordinal 从0连续覆盖至size-1，整组 owner、Turn／execution、producer 和原消息来源一致。组末 anchor 可以是该真实消息的最后一个 assistant_output、reasoning 或 tool_call 等合法语义成员，不以“末项必须为 tool_call”限制普通 assistant 消息。分组仅为原消息协议还原，不是另一份展示／选择集合；消息级 usage、终止原因和 message identity 按唯一归属保存，不能复制到所有成员重复计量。

`ItemProjectionIdentity` 中确有用途的原消息、ModelCall、Provider block／part 来源、`ToolResultEvidence` 的调用／回执身份和确认状态、`ItemSourceManifest` 的修订与保护状态 MUST 按 typed schema 校验；Provider part 不成为通用历史、操作或阅读锚点。缺身份时不得按正文、时间、邻接、item_id 或扩展猜测。`extensions` 继续只接受 namespaced versioned envelope，核心归属、选择、配对、确认、恢复与安全决策不得读取自由扩展 value；需要新决策时先更新唯一 typed 核心合同。正常 reader 只处理当前格式；已支持来源的转换归原离线 owner，不增加 metadata 双读、Part 别名或自动修复。

`turn_scope=turn_root` 必须有非空 `turn_id`，且只能用于该 Turn 唯一的 `semantic_kind=user_input` root；有非空 `turn_id` 的普通 Turn item 必须使用 `turn_scope=turn_member`。反向地，任何非空 `turn_id` 都必须配合 `turn_root` 或 `turn_member`，不得出现 `turn_id != NULL` 且 `turn_scope=NULL`。`turn_scope=ambient` 或 `turn_scope=pending_next_turn` 必须 `turn_id=NULL`，不得进入普通 Turn member/root 集合；持久化的 pending runtime notice 必须是 `semantic_kind=runtime_notice` 且 `turn_scope=pending_next_turn`，request-only notice 不产生 CanonicalItemRecord。若 `turn_id`、`turn_scope`、`message_group_id` 均为 null，item 不属于任何 Turn，reader 不得从相邻 item、wire role 或 message group 推断归属；`message_group_id` 非空也不能改变 root/member/ambient 约束。

`payload_kind` 的完整枚举固定为 `text`、`structured_content`、`tool_call`、`tool_result`、`summary`、`attachment_ref`、`opaque` 和 `extension`：分别表示精确 Unicode 文本、已知 schema 的 JSON object/array、规范化工具调用、规范化工具结果、摘要结构、稳定附件引用、显式编码的不可解释/受保护值和扩展 envelope。`opaque` 必须带非空 encoding、value、provider 或 wire type、schema version；`extension` 必须带非空字符串 `extension_schema` 与 `extension_version`，其中 schema 是稳定 namespaced identifier、version 是该 schema 的显式版本值，另有 value 和 protection/encoding metadata。`semantic_kind=extension` 只能使用 `extension|opaque`；其它 semantic/payload 组合必须经过固定 compatibility table。v3 reader 遇未知 payload kind、缺 schema/version、非法 shape 或不支持的组合必须返回 format/schema recovery error，不得静默降级为空 payload、普通文本或 opaque；已知 extension 但无 handler 时可以保留 immutable raw/hash/offset 并标记 unsupported，但不能进入普通 context，必要来源则返回 `extension-unsupported`。

下表就是本 change 的唯一 semantic/payload/status compatibility matrix；它不是示意列表。每个 `semantic_kind` 的允许 payload、status 和 marker 规则均以表为准，未列出的组合一律在 JSONL durability barrier 前以 `item-schema-incompatible` 拒绝：

| `semantic_kind` | 允许的 `payload_kind` | 允许的 item `status` | 额外 marker/约束 |
|---|---|---|---|
| `user_input` | `text`, `structured_content` | `completed` | `turn_root` 必须是唯一 user root；不得有 `tool_outcome` |
| `assistant_output` | `text`, `structured_content` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 只有 `completed` 可参与 finalization；不得有 `tool_outcome` |
| `reasoning` | `text`, `summary`, `opaque`, `extension` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | `opaque`/`extension` 必须有 protection/encoding metadata；不得作为 final item |
| `tool_call` | `tool_call`, `structured_content` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 必须有 tool invocation/call identity；不得用 `tool_outcome` 表示 call status |
| `tool_result` | `text`, `structured_content`, `tool_result`, `opaque`, `extension` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | text 的 call/result identity 与确认状态必须在 typed `tool_result_evidence`，其它 payload 按自身 typed schema 保存；未确认结果必须改用可承载 `tool_outcome=unknown` 的 typed payload，不能选 text；其它 status 只能省略 marker 或使用 `unknown`；只有 `status=completed` 且 `tool_outcome=success` 才可 replay |
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

### Requirement: Canonical item 必须可编译为多种请求投影

系统 SHALL 从 active context view 和 request-only contribution 先形成有序、可审计的 context request plan，再按目标能力编译为 LangChain message 或 Provider 原生 item/request。编译过程 MUST 保留 canonical/request-only reference、`semantic_kind`/`payload_kind`、可表达的顺序、tool-call/result 关联、附件和 reasoning 保护状态。唯一顶层system root只在首次assembly及实际compaction、rewind、Provider可见ToolSet hard rebase或fork目标首次assembly中编译；合法新epoch只可合并source owner typed `root_placement=root_eligible`的有效完整状态。其它同epoch新增source及`tail_only`source均按plan ordinal成为独立user item，不得提升、前插或按相邻role合并；compaction summary和tool事实保留原语义与因果位置。无法表达时必须显式reject，不得静默改role或拼接文本；Anthropic部分官方模型的mid-context system能力仅保留关闭TODO。`assistant_text` 只能作为 `assistant_output` Item payload 的 projection，不能在 plan 中作为 canonical item kind。

#### Scenario: 编译为 LangChain 执行消息

- **WHEN** 当前 Agent 选择 LangChain adapter 执行请求
- **THEN** adapter 生成临时的 `list[BaseMessage]`，可将同一 `message_group_id` 下的 item 聚合为一个 `AIMessage`，并把 tool calls 放入 `AIMessage.tool_calls`、结果放入关联的 `ToolMessage`

#### Scenario: 编译为原生 Responses 请求

- **WHEN** Provider adapter 能直接接受 item 化请求
- **THEN** 系统可以从 context request plan 直接生成 Provider item/request，不要求先构造 LangChain message，也不把 LangChain 对象作为 canonical history

#### Scenario: request-only prompt 的 wire 投影

- **WHEN** ContextRequestPlan 包含静态 system prompt、skill contribution 和 workspace/environment contribution
- **THEN** 只有initial root中的启用贡献可编译进唯一system/developer/instructions item；第一条真实用户消息后的Skill/workspace/environment full、delta或恢复内容保持独立user item并保留reference/source identity/request-only生命周期，不能从wire role反向创建历史item或为适配Provider合并相邻user item

#### Scenario: 无工具的多模态 assistant 消息往返
- **WHEN** 一条真实消息包含独立 thinking、text 和 image 块，且不含工具调用
- **THEN** 写入独立有序 Item，沿同一消息组恢复其原角色、块顺序、str/list 形状、媒体引用、签名与必要 wire 属性；不能因没有工具而保留一个复合 assistant Item，也不能恢复成多条无关 assistant message

#### Scenario: 不可拆协议边界
- **WHEN** 现有 adapter 无法合法表达所选 Item 边界或保留受保护 carrier
- **THEN** 返回明确的能力／协议错误，不静默合并无关消息、移除签名、改变角色或补造合法文本

### Requirement: 操作 anchor 可以细于 message 和 Turn

系统 SHALL 使用已提交 canonical `item_id` 表达细于 message／Turn 的 rewind、replay、compaction 和 fork 边界，保存 before／inclusive 语义、真实 view／branch、来源与可恢复能力。request-only reference、客户端块号／字符偏移、Provider part ID 或展示 Part MUST NOT 作为通用 durable 操作锚点。Turn 仍是默认入口，由原 resolver 解析真实 Item 边界；操作能力与是否可阅读分开判断。

interrupt SHALL 使用当前语义 draft 的内存进度，终态时提交真实 partial／cancelled 等 Item 或原控制结果；不要求逐 delta 落盘。resume／retry 追加新的 Item 并通过原 provenance 关系关联，不能修改原终态 Item。消息组内违反工具因果、保护或 adapter 能力的切分明确拒绝，不静默移动到整消息末尾；不增加 Part 正文、Part hash／prefix hash 或稀疏 Part 索引来支持任意片段操作。

#### Scenario: 在 assistant_output item 中断
- **WHEN** 用户中断仍在接收 delta 的文本 draft
- **THEN** 已取得的内容按原状态规则提交一个不可变语义 Item，跨重启操作使用该 Item；不把字符偏移转成通用 content_part_id

#### Scenario: 在 tool call 前 rewind
- **WHEN** 选定 view 中已有独立 reasoning、文本、tool_call 和 tool_result Item
- **THEN** resolver 在能力允许时定位 tool_call 之前，保留前序来源；不需要 Item 内 Part 坐标，不修改原 canonical 内容

#### Scenario: 读取位置不是操作能力
- **WHEN** 前端能显示某个 Item，但它不能作为合法 Provider 请求恢复边界
- **THEN** 对该操作返回明确不可操作结果，不以可定位为由承诺 replay，也不自动改选其它边界

#### Scenario: request-only 或局部像素被用作操作锚点
- **WHEN** 请求仅提供 skill 请求引用、Markdown 段落号、字符／像素偏移或 Provider block ID
- **THEN** 拒绝非持久／非 Item 操作边界，要求调用方明确使用具有相应能力的真实 Item

#### Scenario: content part anchor 信息不足
- **WHEN** 操作请求只携带旧 content-part 坐标而没有当前真实 Item
- **THEN** 原操作 owner 明确拒绝不足或不支持的锚点，不猜最近 Item、补造边界或保留旧解析分支。

#### Scenario: request-only context 不能作为 rewind 边界
- **WHEN** request-only context 或展示像素位置被用作 rewind 边界
- **THEN** 原操作 owner 明确拒绝，它们不是 canonical Item，也不因可阅读而获得 rewind 能力。

#### Scenario: 不可操作 item 作为 anchor
- **WHEN** 真实 Item 不满足原 fork/rewind/replay 能力约束
- **THEN** 明确返回原能力错误，不将阅读可定位当作可操作，不重写或丢弃其它已提交成员。

### Requirement: Rollout JSONL 是不可变 item 事实日志

当前格式3的 `rollout.jsonl` MUST be append-only：每行只能保存一个完整且已终态化的 `CanonicalItemRecord`，已提交行的 UTF-8 字节范围、`item_sequence`、payload、status 和 hash 不得更新、删除、覆盖、插入或重排。JSONL 行先完成 durability barrier，再由 SQLite `storage_commits`/`item_catalog` 宣布可见；只有 committed offset 内且有对应 catalog/commit 的行可被 reader 使用。已写但未提交的尾部只能回收或隔离，不能被 reader 推断为事实；修正、retry、resume 或 supersede 只能追加新的 item identity 和显式 relation。`ItemDraft` 的非终态不写 JSONL，不得为了补齐工具组、组大小或显示空层新造空占位 Item；已有真实空消息沿原写入合同保存，completed_empty 仍由控制状态表达，不创建合成 final。

新格式 rollout MUST 以一条 JSONL 记录表达一个已经终态化的语义 item，而不是以 raw provider chunk 或完整 LangChain message 作为唯一持久化单元。每条记录 MUST 包含 `item_id`、`item_sequence`、`semantic_kind`、`payload_kind`、status、payload、`producer_ref`、content hash 和可恢复的 format version；SQLite 只能保存 offset、索引、context view、checkpoint 和派生 projection，不得成为 item 正文的第二事实源。

#### Scenario: 正常 item 提交

- **WHEN** 一个 `assistant_output` 或 `tool_result` item 完成并提交
- **THEN** rollout.jsonl 追加一条独立 item 记录，reader 可以仅凭该记录恢复其 payload 和 identity；历史需要的 `assistant_text` 从 assistant_output Item 派生

#### Scenario: partial 与 completed 的终态语义

- **WHEN** 一个 item 在正常 semantic boundary 前被用户中断，或在正常 boundary 收到完整 payload
- **THEN** 前者只追加一个 `status=partial` 的 immutable JSONL item，后者只追加一个 `status=completed` 的 immutable JSONL item；两者都不再更新旧行，只有 `completed` item 才能参与正常 finalization
- **AND** 后续继续生成、重试或纠错必须追加新 `item_id` 并通过 relation 连接，不能把 partial 行改写为 completed

#### Scenario: raw chunk 不直接成为历史 item

- **WHEN** Provider 将一个文本 block 分成多个网络 chunk
- **THEN** 网络 chunk 可以产生实时 delta，但 canonical rollout 只保存对应语义 item 的终态或明确 partial 终态，不为每个 chunk 追加一条历史 item

### Requirement: Context contribution 和 plan selection 顺序必须可恢复

被 `selection` 绑定且 `included=true` 的每个 `ContextContribution` MUST 在对应 `assembly_id` scope 内取得独立、稳定、不可重写的 `contribution_ordinal`；unsealed plan 中仅登记为 registry 的 contribution 和 `included=false` optional omission 不带该 binding。`ContextContribution` 本体 SHALL 显式声明 `selection_role=direct|backing_only`、`replacement_policy=immutable|replaceable` 和 typed `source_binding?`；source-backed contribution还须携带由source owner声明的typed`root_placement=root_eligible|tail_only`，非source contribution不得伪造资格。selection role决定是否可独立候选，replacement policy只允许同 owner/slot/revision CAS 更新未封存 registry，source binding承载alias/overlay ref、role、epoch并与selection manifest校验。`selection_only`、`replaceable_source`、`source_ref`、`overlay_ref`、`overlay_role`、`overlay_id`、`source_overlay_epoch` 不能作为自由metadata key影响行为，根指令资格也不得放在extensions。owner-thread registry SHALL 在既有类型 `ContextContribution` 上独占分配/恢复 typed `source_ordinal` 字段（非负、持久、不重用），本 change 不引入 `RegisteredContribution` 这一独立返回类型；ledger/composer/plan builder 不得从 contribution 扩展、事件顺序或内存计数补造它。`source_ordinal` 由 registry 独占维护，不等于 assembly-bound `contribution_ordinal` 或 sealed selection 的 `plan_ordinal`，也不进入 sealed identity。`ContextRequestPlan.selection` MUST 是 Saver 冻结的有序列表，每个 canonical/request-only/overlay/tool-set source ref 取得唯一 `plan_ordinal`，并记录 `selection_kind`、source overlay/base/delta role、visibility/protection/availability、omission/loss 及可得 source identity；只有 `included=true` 才强制 source revision、逻辑 content length 和恰一个 source hash token。request-only included entry 才强制 sealed detail_ref，tool_set entry 使用独立 ToolSetRef manifest，canonical entry 不带 detail_ref。`assembly_id` 是该次 plan/selection 的唯一持久范围。`ContextAssemblySnapshot`/SQLite `assembly_item_refs` 必须持久化这些字段及 content hash；omitted entry 的 detail、正文 hash/length 和 contribution ordinal 可以 null/未分配，已知 typed source identity 必须一致。重启后按 ordinal 恢复，不能按 `created_at`、`contribution_id`、物理邻接或 projector 本地规则猜顺序。LangChain、native Provider 和 Web history 必须消费同一 selection；同epoch新增source一律独立user-role item，仅首次assembly或实际compaction/rewind/Provider ToolSet hard rebase/fork目标首次assembly的新root可合并`root_eligible`有效完整状态，`tail_only`始终独立user-role，旧sealed bytes与lineage不改写；Anthropic中途system仅保留TODO。ToolSetRef只能进入Provider tools/tool-config，二者都不得被projector无条件prepend到canonical messages。optional omitted entry只保留omission/loss metadata并跳过正文/工具定义，required omission必须拒绝seal/dispatch。顺序或ordinal不一致必须返回plan-order-integrity error。

该要求的最小结构合同固定如下，字段不得仅由实现内部对象隐含：

```text
ContextRef
├── session_id, ref_type=canonical_item
│   ├── ref_id=item_id, item_sequence, semantic_kind, payload_kind, status
│   ├── source_revision
│   ├── content_length       # payload canonical bytes；不是 JSONL line length
│   └── content_hash
└── ref_type=request_only
    ├── session_id, ref_id=稳定逻辑 alias(普通 request-only 通常为 contribution_id，亦可为 producer 声明 source alias) / overlay alias, plan_id
    ├── source_ref/detail_ref? # draft detail_ref NULL/deferred
    ├── source_revision?, content_length? # required only for included selection
    ├── content_hash? # included=true exactly one; draft/omitted optional at most one
    └── protection, availability

ContextContribution
├── contribution_id, contribution_kind, request_only=true, body/detail_ref
├── selection_role=direct|backing_only, replacement_policy=immutable|replaceable
├── source_binding?={source_ref?,overlay_ref?,overlay_id?,overlay_role,source_overlay_epoch?}
├── source_revision, content_length
├── content_hash? # exactly one
├── protection, visibility
├── extensions                 # namespaced/versioned；不得参与核心决策
└── ordinal_binding?={assembly_id, contribution_ordinal} # 仅在 sealed assembly selection 中存在

source_ordinal              # ContextContribution 的既有 typed 字段，只由 owner-thread registry 分配/恢复；不引入 `RegisteredContribution` 独立类型

ToolSetRef
├── ref_type=tool_set        # ToolSetRef discriminator；不属于 ContextRef.ref_type
├── ref_id=tool_set_snapshot_id
├── plan_id, assembly_id?    # plan registry required; assembly only after seal
├── source_revision
├── tool_set_schema, tool_set_schema_version
├── tool_policy_version
├── content_length           # tool manifest 的 JCS UTF-8 bytes
├── content_hash? # exactly one
├── protection=public|redacted|protected
└── availability=available|unavailable|forbidden|expired

ContextSelectionEntry
├── assembly_id, plan_ordinal, ref={ref_type, ref_id}, selection_kind
├── included, omission_reason?, loss[]
├── visibility, protection, availability
├── source_revision?          # required iff included=true; known iff manifest agrees
├── content_length?           # logical body length; required iff included=true
├── content_hash? # included=true exactly one; included=false optional at most one
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

本 requirement 不再登记 `selection_kind` 与 `ref_type` 兼容矩阵，唯一权威登记见本 spec 的 requirement「selection_kind 与 ref_type 必须使用唯一兼容矩阵」；矩阵外组合仍返回 `plan-order-integrity`。

`ContextRef.ref_id` 仍是 canonical `item_id` 或 request-only 的稳定逻辑 alias：普通 request-only ref 的 `ref_id` 通常等于 `contribution_id`，亦可为 producer 声明的 source alias（`source_ref` 元数据入口：registry 以 `contribution_id == ref_id` 为首选匹配，`source_ref` 为具名来源回退），overlay ref 的 `ref_id` 为其 overlay/source alias 并按 role/epoch 匹配；`ref_id` 不是另立的判别字段，不得用它替代 `ref_type`；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref` 与 source manifest 定位正文/detail及其 source revision、length/hash，不要求 contribution identity；included 且 contribution-backed 的 request-only/overlay 才必须用非空 `contribution_id` + `contribution_ordinal` 唯一定位 `contribution_manifest` 的正文/detail、source revision、length/hash 与 ordinal，且 overlay 按矩阵必须 contribution-backed。canonical/tool_set 严禁 contribution_id/ordinal。缺失、重复或 selection kind 与 union tag 不匹配统一返回 `plan-order-integrity`；source/hash mismatch 返回 `source-mismatch`，request-only detail/contribution 不可用返回 `detail-unavailable`；required source 的 omission 直接拒绝 seal/dispatch。

`ref_type` 是本 change 在 plan、snapshot、SQLite assembly ref 和 projector 输入中的唯一序列化判别字段，闭合集合只有 `canonical_item | request_only`；不得持久化 `ref_kind` 或 `request_only` boolean 作为 ContextRef/selection discriminator，也不得把 `item_id`/`contribution_id`/overlay alias 当作第二个判别字段。统一身份字段是 `ref_id`：canonical ref 的 `ref_id` 必须等于 immutable item 的 `item_id`，普通 request-only ref 的 `ref_id` 是通常等于其 `contribution_id`、亦可为 producer 声明 source alias 的稳定逻辑 alias，overlay ref 的 `ref_id` 是其 overlay/source alias。ContextRef显式带`session_id`和`thread_id`；canonical ref的scope是`(session_id, thread_id, item_id)`，request-only draft ref的scope是`(session_id, thread_id, plan_id, ref_id)`。ContextRef不承担assembly binding：draft中`assembly_id`不存在/必须为NULL，只有sealed`ContextSelectionEntry.assembly_id`、`ref_manifest`和detail manifest才建立`(session_id, thread_id, assembly_id)`scope。request-only ref的`detail_ref`在draft中必须为NULL或仅表示待seal的source detail；Saver seal时才解析/物化为`{session_id, thread_id, assembly_id, detail_id}`，并逐字段写入selection/manifest。optional omission的selection ref仍必须保留tag/id，但可作为未解析完整manifest的typed identity stub；其source revision、length、hash、detail_ref可为空，已知值必须与manifest一致，且不得被restore/projector当作可读正文。旧模型若仍使用`ref_kind`/boolean，只能由legacy ingress adapter在边界处归一化，后续composer、storage、snapshot和projector不得继续消费别名。canonical ref必须且只能解析到同一owner thread已提交`item_catalog` item，但该完整解析要求只适用于`included=true`；optional`included=false`的canonical tag/id stub不声称item已可读。request-only ref在unsealed阶段只能解析到同一`(session_id, thread_id, plan_id)`registry；只有`included=true`的sealed entry才解析到该assembly的contribution/detail manifest，并由显式`contribution_id`定位contribution，`included=false` typed identity stub不解析assembly detail/contribution；同一scope内不能以另一类型重复注册。

工具集合使用独立的 `ToolSetRef`，不扩展 `ContextRef.ref_type`：`ContextRef` 的闭合集合仍只有 `canonical_item | request_only`，而 selection 的 `ref` 是一个 tagged union，可为一个 `ContextRef` 或一个 `ToolSetRef`。`ToolSetRef.ref_type=tool_set`仅在`selection_kind=tool_set`时合法，`ref_id`必须解析为同一`(session_id, thread_id, plan_id)`registry中的target-local`tool_set_snapshot_id`；unsealed registry的`assembly_id=NULL`，seal后的selection binding必须带当前`assembly_id`。ToolSetRef/ToolSetSnapshot manifest 必须保存非空 `source_revision`、`tool_set_schema`、`tool_set_schema_version`、`tool_policy_version`、逻辑 `content_length`、恰好一个 `content_hash`、`protection` 和 `availability`；hash/length 覆盖 `{ "tool_set_schema":"tool-set-ref", "tool_set_schema_version":"v1", "tools":<按稳定 tool_id 排序的 schema/config entries>, "tool_policy":<规范 policy>, "tool_policy_version":"v1" }` 的 RFC 8785 JCS UTF-8 bytes，统一使用 `sha256:jcs:v1` content_hash；敏感正文及其内部 hash 只向获准的 reader 提供，默认 history/API 投影脱敏，不生成替代 digest。每个 included tool-set selection entry 必须逐字段等于 `tool_set_manifest[]`，`base_delta_role=none`、不绑定 `contribution_ordinal`，但与其它 entry 共用 assembly 内唯一的 `plan_ordinal`；optional omitted tool-set entry 只保留 `ref_type=tool_set`/`ref_id`、plan ordinal、omission/loss、availability 和可得 identity，manifest 正文字段、detail_ref 与 contribution ordinal 可以为 null/未分配，已知 metadata 必须与 registry 一致且不投影 tools。缺失、重复、类型/selection_kind 不匹配或 manifest 不一致返回 `plan-order-integrity`，source/hash/length/schema/policy version 不一致返回 `source-mismatch`，不可用/无权限返回 `detail-unavailable`。ToolSetRef 只投影到 Provider 的 tools/tool-config；history 仅消费受 visibility 策略允许的 selection metadata/摘要，不生成 canonical message、Turn item 或 request-only 正文。

`ContextSelectionEntry.contribution_id` 是 request-only/overlay contribution 到 `contribution_manifest` 的唯一显式映射：included且contribution-backed时必须非空，且只能解析同一`(session_id, thread_id, plan_id)`registry的一个`contribution_id`；该manifest必须进一步提供正文或最终`detail_ref`、source revision、logical content length、hash token与assembly-bound`contribution_ordinal`。该 entry 的 `contribution_id` 是 manifest 读取的唯一显式键（included 普通 request-only 上 `ref_id` 与之同值属兼容事实）；restore、LangChain/native Provider/Web history必须按selection entry的`contribution_id`读取对应manifest/body，再校验entry与manifest，不得以detail_ref、hash或ordinal反推contribution，也不得把overlay alias当成contribution_id。canonical_history与tool_set entry（包括omitted）的`contribution_id`、`contribution_ordinal`、`detail_ref`必须为NULL；只有omitted request-only/overlay entry可保留已有`contribution_id`，且必须与同一manifest一致，不得新分配contribution_id/ordinal或触发正文/detail读取，没有既有映射则为NULL。

`ContextContribution` 固有就是 request-only contribution：其持久化字段 `request_only` 必须存在且恒为 `true`，只作为 contribution manifest 的不变量，不能用来判别 ContextRef union；缺失或为 `false` 必须拒绝。`ContextRef` 和 selection 仍禁止该 boolean，必须使用 `ref_type=request_only`。`ContextContribution.contribution_kind` 的闭合集合只有 `prompt | overlay_base | overlay_delta | notice`，不存在合法的 `tool_set` contribution。Provider tool definitions 只能通过同一 plan/assembly 的 ToolSetSnapshot/ToolSetRef manifest 绑定；`ContextContribution`、request-only body、`contribution_ordinal` 和普通 ContextRef 都不得代表 tool definitions。遇到 `contribution_kind=tool_set` 必须返回 `contribution-kind-unsupported`，不得转换为 ContextContribution、ToolSetRef 或 Provider tools；未发布旧迁移已退出支持，不保留旧 reader、report／quarantine 或输入例外。

`ContextRequestPlan` 的生命周期和 identity 也属于本结构合同：`create_context_plan`先在owner-thread namespace中创建唯一`plan_id`，状态为`unsealed`；未seal的plan可以登记`refs[]`、`tool_set_refs[]`/`contributions[]`，但必须保持`assembly_id=NULL`、`selection=[]`，因此不产生`ContextSelectionEntry`、`plan_ordinal`或可dispatch的assembly。draft registry的ContextRef约束为`UNIQUE(session_id, thread_id, plan_id, ref_type, ref_id)`；canonical ref可被多个plan查询复用，但request-only ref不得跨plan复用。draft ContextRef不解析assembly/detail physical path，新增detail只留在内存ledger或source typed ref。Saver只有在seal preflight通过ContextRef/ToolSetRef/contribution registry manifest的source、length、hash、visibility/protection和顺序校验后，才在同一提交边界分配thread-local`assembly_id`，生成selection/`plan_ordinal`与assembly-bound`contribution_ordinal`，将required request-only body写入detail store，并保存最终detail_ref与`ContextAssemblySnapshot`。seal失败不得留下可用assembly、selection或detail binding；原plan仍为`unsealed`，可以修正后重试。`(session_id, thread_id, plan_creation_idempotency_key)`负责plan创建幂等，`(session_id, thread_id, plan_id, seal_idempotency_key)`负责seal幂等；相同preimage返回原identity，不同preimage分别返回`plan-idempotency-conflict`或`assembly-idempotency-conflict`，不得把`plan_id`当成`assembly_id`。跨 owner fork 保留历史plan/ref/detail local ID和原封存scope，由目标本地snapshot binding核验；新目标请求创建当前owner的plan/assembly，不重算历史hash。成功后plan与snapshot保留两个identity，逐字段不可变且一对一；新的selection必须创建新的plan/assembly。未选择任何source的plan只有在已分配assembly后才允许以空`selection` seal，空selection仍属于该assembly scope。

`ContextRef.content_length` 的来源按 selection union 分流：`ref_type=canonical_item` 的 `canonical_history` 只能来自已提交 `item_catalog.payload_length`；`ref_type=request_only` 的 request-only/overlay selection 必须来自同一 assembly 的 sealed detail/contribution source manifest；`ToolSetRef.ref_type=tool_set` 的 tool_set selection 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest。三者都不能从 JSONL line offset/length、wire message 长度或当前文件猜测或替代。`ContextRequestPlan.refs[]`、`.tool_set_refs[]`/`.contributions[]`只是source registries，`selection[]`是唯一的顺序、inclusion和loss authority；selection entry只能在对应`(session_id, thread_id, assembly_id)`scope中存在。每个entry必须恰好解析到一个tagged-union source ref：非`tool_set` selection解析到一个`ContextRef`，`selection_kind=tool_set`解析到一个`ToolSetRef`，且同一ref/contribution/tool-set snapshot不得在一个assembly重复选择。所有entry的`ref_type`、`ref_id`、visibility、protection、availability、base/delta role和`source_overlay_epoch`必须逐字段等于对应registry/manifest；只有`included=true`时才强制`source_revision`、逻辑`content_length`和恰一个`content_hash`，且request-only才强制非空、同assembly的`detail_ref`，contribution-backed entry才强制`contribution_ordinal`。`included=false`仅允许optional omission，仍保留tagged source ref、`plan_ordinal`、`omission_reason`、`loss`、`availability`和可得source identity；source revision、length、hash token、detail_ref、contribution_ordinal可以为null/未分配，已知metadata必须逐字段等于manifest。缺项、重复、union类型/selection_kind不匹配或任意顺序/绑定不一致返回`plan-order-integrity`，canonical或tool-set source不一致返回`source-mismatch`，request-only detail不可用或不一致返回`detail-unavailable`。`selection_kind=tool_set`不得引用`item_catalog`、普通`ContextContribution`正文或ContextRef，ToolSetRef正文只来自tool schema/config manifest及其hash。`ContextAssemblySnapshot`持有seal时selection的不可变副本以及`ref_manifest[]`/`tool_set_manifest[]`；`assembly_item_refs`必须以`UNIQUE(session_id, thread_id, assembly_id, plan_ordinal)`、`UNIQUE(session_id, thread_id, assembly_id, ref_type, ref_id)`、`UNIQUE(session_id, thread_id, assembly_id, contribution_id)`和`UNIQUE(session_id, thread_id, assembly_id, contribution_ordinal)`保护一致性，omitted entry不分配detail/contribution ordinal。plan registry还必须以`UNIQUE(session_id, thread_id, plan_id, tool_set_snapshot_id)`防止ToolSetRef重复。`contribution_ordinal`是assembly binding，不是contribution跨assembly的全局属性；同一contribution在不同assembly重新绑定时取得新的ordinal，但同一sealed assembly内不可重写。三种projector和history只能消费该selection副本，不得重新从registry排序或prepend；omitted canonical/request-only/tool_set entry只保留metadata/loss，分别跳过正文、detail和工具定义，不从当前source回退或生成空值。

`ContextContribution.content_hash` 的正文 preimage 固定为 `{ "contribution_kind": <contribution_kind>, "body": <typed body> }` 的 `sha256:jcs:v1` RFC 8785 JCS bytes；draft body 可来自内存 inline typed value 或 typed source ref，sealed manifest 必须从已解析的 body/detail_ref 复核。preimage 不含 identity、ordinal、assembly、时间或 wire role。included ref/selection 统一使用内部 content_hash；删除 redacted_stable_digest、thread HMAC 与相关密钥生命周期。sensitive/protection 只控制访问和外部投影，默认 history/API 隐藏敏感正文及内部 hash，不生成替代 token；canonical ref 继续使用 immutable item content_hash。

#### Scenario: 自由 metadata 不得改变 contribution 决策

- **WHEN** 两份 contribution 的 typed selection/replacement/source binding 完全相同，仅一个合法 namespaced `extensions` envelope 的 `value` 中包含 `selection_only`、`replaceable_source`、`source_ordinal` 或 overlay alias 等旧控制 key
- **THEN** registry、ledger、seal 和三个 projector 得到相同的注册/选择/替换/顺序结果；顶层旧 `metadata` 控制字段直接 schema error，不能被当作兼容输入

#### Scenario: registry ordinal 在重启和替换后保持单一权威

- **WHEN** 同一 owner thread 登记多个 source，合法 replace 一个未封存 source slot，随后重启并在另一 assembly 选择它们
- **THEN** registry 唯一分配并恢复 `ContextContribution.source_ordinal`，replace 保留 slot 序号；ledger不再分配 fallback，sealed `plan_ordinal`/`contribution_ordinal` 仍分别由 selection/assembly 冻结，不读取扩展或对象遍历顺序

#### Scenario: 统一 selection 被三个读取面复用

- **WHEN** Saver 为同一 active view 生成 LangChain restore、native Provider request 和 Web history projection
- **THEN** 三者读取相同的 `selection[{plan_ordinal, ref}]`；`ContextRef` 负责 canonical/request-only/overlay source，`ToolSetRef` 负责 `selection_kind=tool_set`，并按相同的 `contribution_ordinal` 应用 request-only 与 base→delta overlay。LangChain/native Provider 只能把 ToolSetRef 编码到 tools/tool-config，history 只能保留受策略控制的 ToolSetRef metadata；只能在 wire 编码阶段合并角色，不得改变 selection 或把工具定义变成 message

#### Scenario: 重启后正文与 ordinal 一致

- **WHEN** 进程在 assembly seal 后重启，或 `included=true` source file 被覆盖、删除或替换
- **THEN** Saver 依据已提交 ref、source revision、length 和 content hash 恢复原正文与 ordinal；缺失/覆盖/错误 source 返回 `source-mismatch` 或 `detail-unavailable`，不发送错误正文，不静默 loss
- **AND** `included=false` 的 optional omission 只恢复 tagged ref、plan ordinal、omission/loss/availability 和可得 identity metadata，不尝试恢复正文或 ordinal，也不把 omission 当作 source mismatch

#### Scenario: contribution-backed request-only ref 唯一定位正文

- **WHEN** 一个 sealed plan 同时包含多个 request-only/overlay contribution，且每个 included selection entry 的 `ref_id` 是其稳定逻辑 alias（普通 request-only 通常等于 `contribution_id`，overlay 为 overlay alias）
- **THEN** 每个 entry 通过非空 `contribution_id` 唯一解析到同一 `(session_id, thread_id, plan_id)` 的 `contribution_manifest`，再由该 manifest 定位正文或最终 `detail_ref`、source revision、逻辑 length、hash token 与 `contribution_ordinal`
- **AND** restore、LangChain/native Provider 和 Web history 按 `plan_ordinal` 读取同一 entry/manifest mapping；缺失、重复、ref/contribution 不一致或 ordinal/body/hash 不匹配返回 `plan-order-integrity`、`source-mismatch` 或 `detail-unavailable`，不得按 `ContextRef.ref_id`、detail_ref、hash 或 ordinal 猜测另一条 contribution
- **AND** canonical_history/tool_set entry（包括 omitted）的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为空；只有 omitted request-only/overlay entry 即使保留已有 `contribution_id` 也只校验同一 manifest identity，不新分配 contribution_id/ordinal，不读取正文/detail；没有既有映射则为 NULL

## ADDED Requirements

### Requirement: 当前语义 Item 格式使用唯一版本闸门

系统 SHALL 同时核验SQLite的rollout_format_version与JSONL的format_version，仅接受格式3。格式1／2、未知、混合版本、manifest／envelope不一致或结构不完整均明确拒绝；不得根据角色／字段猜格式，不使用旧reader、不自动改写或回退为空历史。已退出的v1→v2转换、专用staging及历史物化分支 MUST 删除，不冻结旧writer。

#### Scenario: 旧格式保留原件并明确拒绝
- **WHEN** history、checkpoint、Provider或fork打开格式1／2的来源
- **THEN** 返回unsupported_rollout_format_version并说明实际与期望版本，原件不被覆盖、转换或清理，不引导用户运行已退出的命令


### Requirement: 语义 Item 拆分不依赖工具调用是否存在

系统 SHALL 在 canonical 写入时将 assistant 顶层具有独立语义来源的思考、文本和媒体内容块分别表达为 Item，并使每次工具调用对应唯一 tool_call Item；同一块的多个流式 delta 不产生多个 Item。同内容是否伴随工具调用 MUST NOT 改变这些内容的语义粒度。payload_kind=structured_content 仍可承载单项富内容属性，不得作为已识别 thinking／text／image 多语义输出的整包容器。

一次 tool_result、唯一 user_input root、runtime notice 和 compaction summary SHALL 保留原业务原子性，内部 rich 属性或附件不因递归拆块而变成多次业务行为。单个 reasoning carrier 的签名、summary、密文与保护元数据属于同一来源的完整表达；不同安全视图不产生不同 Item。未知块沿原显式 extension／opaque 合同处理，不丢弃、stringify 或默认冒充文本。

单个 tool_call Item SHALL 只含一次声明；沿已有 payload 使用 `tool_calls` 时数组必须恰含该调用，不允许多个调用继续包在同一 canonical Item 的 tool_call／structured_content／extension 中。参数本身的批处理数组不递归拆成调用；Provider 原 wire 多调用数组在输入拆分、消息组重建边界保留。调用 Item 的 completed 状态表示声明完整，不代表工具已运行或成功。

#### Scenario: 伴随工具前后粒度相同
- **WHEN** 两份输入拥有相同的thinking、text、image有序块，其中一份额外携带工具调用
- **THEN** 两份输入的三个内容块都各有独立 Item，带工具的输入额外产生调用 Item；不在无工具分支退回多 Part 复合 Item

#### Scenario: 同一块持续流式追加
- **WHEN** 一个文本来源接收上千次 delta 后完成或中断
- **THEN** 只有对应的语义 draft 和终态 Item，不按 token、chunk、Markdown 段落数量增加 Item

#### Scenario: 复合工具结果和用户附件
- **WHEN** 一次工具返回文本与图片，或用户一次提交文本与多个附件
- **THEN** 保留一次真实回执和唯一用户 root，原工具关联、附件引用与顺序不变，不伪造额外调用结果或 Turn root

#### Scenario: 受保护思考
- **WHEN** 单个真实 reasoning carrier 同时提供可读摘要、签名和加密载荷
- **THEN** 原完整保护信息可用于协议还原，前端只取得授权安全视图；摘要、签名和密文不被计为三次独立思考

### Requirement: 工具拆分保留原声明批次与真实执行关联

系统 SHALL 复用原 owner、ModelCall、message_group_id 和 ProjectionGroup 显式成员关系表达同批工具声明；单成员沿其原 Item 来源。组 ordinal 计入全部语义成员，调用的原相对顺序由其中 tool_call 成员确定，不另建 batch ID、调用顺序表或第二套 Part 身份。相同 ModelCall 但不同原消息不得仅凭 ModelCall 合批，Provider 重复使用 call ID 时必须保留其原 ModelCall／owner 范围。完整协议重建 SHALL 按原声明顺序还原数组，不能按结果到达次序重排。

原消息组、前端活动组与执行流 MUST 保持各自边界；同一活动组、Turn或执行流不构成同批声明证据。Provider请求中的parallel_tool_calls等选项只保留原协议语义，不表示实际执行重叠，不因拆分Item而变更调度或配置。

流式调用片段 SHALL 沿原adapter提供的Provider调用来源累计到各自draft，以协议明确的原声明顺序冻结组成员；片段到达次序不决定调用顺序。Provider索引只作原协议来源，不能直接用作持久Item身份或组ordinal。来源尚不足以关联时沿原draft合同处理，不猜ID、补占位Item或提前宣布声明完整；独立调用的参数完整性与partial／invalid终态不互相替代。

消息组封存 SHALL 只约束原canonical组发布事务，不成为新的工具启动或结果发布屏障。原调度器已允许启动的调用，不得因本次拆分额外等待其它声明完整、整组持久提交或其它回执；原本要求完整模型响应的调度规则亦不得因此改为提前执行。不新增批次执行事务或第二套流式调度入口。

同批关系与执行重叠 SHALL 独立表达。不同原消息组或ModelCall的调用，在原执行owner确认的同一可比较执行范围内可以实际重叠；这种重叠不得反向改变声明分组、Provider数组还原或Item顺序。跨目标、工作区或不可比较执行流不得仅按时间数值交叠推断并行。

实际执行 SHALL 沿原 invocation／attempt owner 记录与调用 Item 配对，保留当前每个scoped call一个attempt的约束；跨ModelCall的重试不覆盖早先调用及结果，新声明保留自己的Item，不伪装成原调用的第二attempt。同一声明的镜像与重放不复制调用Item。执行起止、排队和状态由原执行事实表达，不能从组 ordinal、声明数量、call completed、ToolCall 声明流时间或 canonical created_at 推断。缺执行证据不补造成功、串行、并行或 queued；不能确认调用或 attempt 时保持原未知／待关联语义，不按工具名或物理邻接猜测。

#### Scenario: 多工具声明往返
- **WHEN** 原模型消息包含思考、文本和 tool_calls=[A,B]，B先返回结果
- **THEN** 每次调用为一个独立 Item，原组记录全部成员和A先于B的声明关系；重建仍为[A,B]，两个回执以各自真实调用／attempt配对，不按完成顺序改写声明或原历史坐标

#### Scenario: 拆分保持调用数量和独立参数
- **WHEN** 一条完整模型消息含reasoning、text和两次同名工具调用A、B，各有真实调用ID及不同参数
- **THEN** 原组含四个语义成员和两个单调用Item，重建恰好恢复两次调用及各自参数；重复流片段、声明镜像或B的回执不增加调用数，不按工具名合并A、B，也不递归拆分参数中的数组

#### Scenario: canonical边界拒绝未拆分的多调用Item
- **WHEN** 调用方绕过原codec，试图提交一个含两次有效调用的completed tool_call Item
- **THEN** 原canonical写入校验明确拒绝且不提交该非法Item；reader和前端不再临时拆解修复，不保留复合调用的第二种合法表达

#### Scenario: 多调用流式片段交错到达
- **WHEN** 原协议明确调用顺序为[A,B]，B的参数片段先到，后续A、B片段交错，其中A到流结束仍不完整
- **THEN** adapter沿各自真实来源累计，保留A先于B的声明关系；B的完整声明不补全A，A按原partial／invalid合同收敛，不生成按到达顺序排列的持久Item、虚构ID或额外调用

#### Scenario: 同批声明在不同调度下运行
- **WHEN** 两份等价[A,B]声明分别被执行为A结束后B启动和A／B运行区间重叠
- **THEN** 两份声明组与Item粒度一致，区别仅来自原执行owner的attempt证据，canonical拆分不改变调度或将数组顺序编码成依赖链
- **AND** Provider重建均保持原[A,B]及调用参数；原调度配置保持不变，不因拆成两个Item新增A结果到达后才接受B的限制，也不强制同时执行

#### Scenario: 执行早于声明组封存
- **WHEN** 原调度器已允许B开始执行，但同消息的A仍在累计参数，消息组尚未封存
- **THEN** B沿原执行入口继续，不因组size／ordinal尚未冻结而新增等待；A不被补全或强行启动，B的执行证据沿原来源传递，最终声明顺序仍按原协议冻结。若原调度器要求完整模型响应，则仍等待该条件，不以本变更开放提前执行

#### Scenario: 展示分组不合并原声明消息
- **WHEN** 同一ModelCall中的两条原消息分别声明[A,B]和[C]，且UI将它们放入同一活动组
- **THEN** 仍保留两个原消息组并分别重建调用数组；不能因活动组或执行流相同而声明A、B、C同批，parallel_tool_calls选项也不能替代真实执行证据

#### Scenario: 不同声明组的执行重叠
- **WHEN** 两个原消息组分别声明A和B，原执行owner记录的同一可比较范围内真实有限执行区间发生重叠
- **THEN** 两个调用可被解释为执行重叠，但保留各自原声明组、Item与调用身份；协议重建仍为两条原消息，不合并tool_calls数组

#### Scenario: 调用返回后台任务引用
- **WHEN** 调用A启动后台任务并返回真实任务引用，后台任务未结束时调用B开始
- **THEN** A的调用结果和attempt终态按原调用事实保留；后台任务沿自己的owner继续存在，其运行时长与结果不回写为A的调用时长或结果。A、B的声明分组及调用区间不能证明两个后台任务是否并行

#### Scenario: 重试及重复Provider调用ID
- **WHEN** 重试／回退后的不同ModelCall使用同一Provider call ID，或原call被重复通知第二个attempt
- **THEN** 跨ModelCall按真实scope和attempt区分，早期失败不被晚到结果覆盖；同call第二attempt保持原明确拒绝，不为展示放宽运行约束或创建重复声明Item

### Requirement: final 输出引用原有语义 Item

Turn finalization SHALL 沿真实最终输出消息组选择已有完成 assistant_output Item，final_item_id 指向该组内最后一个最终输出成员；该组其它最终输出内容沿显式组关系按顺序展示。最终回答可以含多个文本／媒体 Item，但 MUST NOT 为展示另写聚合文本 canonical Item、第二份持久 final 成员集合或 Part 级 final 身份。不同 ModelCall、工具和 reasoning 不因位置或正文相同混入最终回答；原失败、中断、未知和 completed_empty 合同保持。

#### Scenario: 多块最终回答
- **WHEN** 明确 finalization 的消息含文本A、图片和文本B
- **THEN** 按三个原 Item 展示同一回答，final_item_id 指向该组真实末输出成员，各内容保留自己的 Item ID；刷新、历史和实时都不漏A／图片或重复B

#### Scenario: 终态引用已提交原输出
- **WHEN** 原模型结束事件的消息已经由 checkpoint writer 提交，包含文本／媒体输出和可选受保护思考
- **THEN** 按原 message_id 与 canonical manifest 确认有序成员，终态只引用原输出，JSONL 正文与原 checkpoint 不因 final 展示追加或重写；原签名、密文、来源与请求用量保持
- **AND** 缺原 message_id、原消息不存在、含工具声明或当前 view 排除目标输出时明确拒绝，不取最近文本、不新建替代消息；重复终态不复制正文

#### Scenario: 摘要候选不覆盖最终来源
- **WHEN** 模型切换后产生原最终输出，随后只为压缩生成摘要候选
- **THEN** 最终来源及原输出块仍属于实际输出消息，摘要候选不覆盖它们，也不成为 final；消息、ChatResult 与等价 generations 载体保持相同原消息身份

#### Scenario: 无 Turn 的原输出收敛与中断裁决
- **WHEN** 无 Turn execution 已提交其可见完成输出，随后完成提交与中断请求竞争
- **THEN** 沿原 execution owner 事务裁决，完成只收敛已有输出与控制终态，不另写正文或 Turn final 字段；中断先接受则完成不得覆盖它，完成先提交则外层不得再取消原 Job

#### Scenario: 中间文本不是最终答案
- **WHEN** 较长中间文本之后出现另一明确 final 消息组
- **THEN** 只将原 finalization 指定的输出组作为最终回答，不按最后一段、长度、角色或 Part ordinal 猜测

## REMOVED Requirements

### Requirement: 旧 message-line rollout 必须显式识别和迁移

**Reason**: 用户明确退出未发布格式1／2的转换支持，避免冻结旧实现及两套存储合同。

**Migration**: 既有原始数据保留；当前程序只接受格式3，不提供旧转换命令，不自动转换或清理真实数据。其它有序SQLite结构迁移仍归原owner。
