## MODIFIED Requirements

### Requirement: Rollout JSONL 只保存不可变消息

系统 MUST 按本 requirement 的 Item、payload 和 compatibility matrix 合同读写当前格式3的 rollout JSONL 与 SQLite catalog；不满足的组合在提交前明确拒绝。未发布格式1／2的离线转换已退出支持，删除候选推断、synthetic identity、report／quarantine、staging 和旧 reader，不把旧原件解释成当前数据。

`CanonicalItemRecord.status` 完整枚举固定为 `completed | partial | incomplete | cancelled | failed | unknown`，全部是可写入 JSONL 的终态；`completed` 表示声明 payload 已完整收敛，`partial` 表示截至中断/停止边界已持久化但尚未达到正常 semantic boundary 的完整快照，二者都不可在 JSONL 中互相改写。`draft`、`open`、`active`、`running` 和 `completed_empty` 只能存在于内存 draft 或 SQLite assembly/Turn/control state。ItemDraft 只能从 `draft` 转移到一个终态，JSONL 行提交后不得改变 status/payload/hash，修正、retry、resume 必须追加新 item 并建立 relation；无稳定 payload 的 draft 不得补造 `status=unknown` item，provider 空输出使用 metadata-only terminal convergence。item status 只描述单个 item payload 的事实，不等价于 execution/control outcome；后者统一使用 `ControlOutcome=completed|completed_empty|failed|interrupted|cancelled|execution_lost|unknown`，仅用于 ExecutionRecord、ModelCallRecord、assembly、storage commit 或控制记录。不得引入带 outcome 前缀的 unknown 状态别名；`tool_outcome=success|failure|cancelled|unknown` 只允许作为 tool_result typed payload marker。

`payload_kind` 完整枚举固定为 `text | structured_content | tool_call | tool_result | summary | attachment_ref | opaque | extension`。`opaque` 必须有非空 encoding/value、provider 或 wire type、schema version；`extension` 必须有非空字符串 `extension_schema` 与 `extension_version`，其中 schema 是稳定 namespaced identifier、version 是该 schema 的显式版本值，另有 value 和 protection/encoding metadata。未知 kind、未知 shape 或缺失 extension schema/version 必须进入 schema recovery error，不得静默按普通文本/空 payload/opaque 读取；不支持的已知 extension 只能保留为 immutable unsupported record，不能进入普通 context。

下表是 storage reader/writer 与 design/itemized spec 共用的唯一 semantic/payload/status compatibility matrix；未列出的组合在 JSONL durability barrier 前以 `item-schema-incompatible` 拒绝：

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

表外组合、其它 semantic kind、未知 payload kind、缺失 extension schema/version 或非 namespaced marker 均不得进入 JSONL；非法组合不写 catalog、不推进 offset，并返回 `item-schema-incompatible` 或 format/schema recovery error。execution、model call、assembly、storage commit 和控制记录的结果字段固定为 `outcome`，只能使用 `ControlOutcome=completed|completed_empty|failed|interrupted|cancelled|execution_lost|unknown`；`tool_outcome=success|failure|cancelled|unknown` 只允许在 `tool_result` payload 内按表使用，不能与 item `status` 混用。

系统 SHALL 为每个生产 rollout 保存 UTF-8 当前格式3的 `rollout.jsonl`。当前格式3 item-line rollout 的每行是不可变 `CanonicalItemRecord`，是正常生产运行时唯一 canonical 事实源；格式1／2的原件保留，storage、history、Provider、checkpoint、fork 和 Turn runtime 在写入前明确拒绝；当前程序不提供旧转换命令。SQLite manifest/database metadata 的 `rollout_format_version` 与每个 envelope 的 `format_version` 必须一致。当前格式3顶层 envelope 的必填核心字段必须存在且非 null：`format_version=3`、`record_type=item`、`item_sequence`、`item_id`、`semantic_kind`、`payload_kind`、`status`、`producer_ref`、`payload`、`content_hash`、`created_at` 和 `extensions`；`turn_id`、`turn_scope`、`message_group_id`、`projection_group`、`projection_identity`、`tool_result_evidence`、`source_manifest` 和 `wire_role` 是按语义可空/可省略的 typed 关联字段，旧 v2 顶层 `metadata` 不合法。`turn_scope=turn_root` 必须有 `turn_id` 并且只能指向该 Turn 唯一的 `user_input` root；普通 Turn item 必须是 `turn_member` 且有 `turn_id`；反向地，任何非空 `turn_id` 都必须配合 `turn_root` 或 `turn_member`，不得出现 `turn_id != NULL` 且 `turn_scope=NULL`；`ambient`/`pending_next_turn` 必须无 `turn_id` 且不属于普通 Turn member/root；持久化 pending runtime notice 必须使用 `semantic_kind=runtime_notice` 和 `turn_scope=pending_next_turn`，request-only notice 不生成 item。若 Turn 关联字段为空，reader 不得从物理顺序、wire role 或 message group 猜测 Turn。缺少版本字段不得默认推断为格式1，也不得即时转换。`assistant_text` 与 `final_response` 不得作为 canonical item kind。JSONL 不保存 checkpoint、branch、rewind、fork、compaction 等控制记录本身，但允许追加 `semantic_kind=compaction_summary` 的 canonical item；SQLite 与 JSONL 必须使用同一当前格式，不得混写或静默接受未知版本。

#### Scenario: 旧格式拒绝且保留原件

- **WHEN** 读取或初始化入口遇到旧版、未知版或缺失版本的 rollout
- **THEN** 明确报告格式错误，不调用旧 importer，不改写 canonical、catalog 或版本号，不清理原件

#### Scenario: 当前格式3 item-line 追加

- **WHEN** Agent 写入 user input、assistant output、reasoning、tool call、tool result 或 runtime notice
- **THEN** 当前格式3 rollout 按 Item 顺序追加对应的 `CanonicalItemRecord`，不重复写入完整 LangChain messages 数组；历史 `assistant_text` 由 assistant_output Item projection 生成

#### Scenario: 不同格式不得混写

- **WHEN** manifest/envelope version 不一致，或同一个 JSONL 混入其它版本的 message／item
- **THEN** reader 返回明确 format mismatch/recovery 错误，不混合建立 context view

#### Scenario: 控制语义不进入 JSONL

- **WHEN** 系统执行 checkpoint、rewind、compaction、replay 或 fork
- **THEN** 控制状态仅由 SQLite 相应 owner 保存，不在 JSONL 追加控制记录；compaction 生成的摘要仅以符合当前格式3 schema 的不可变 `compaction_summary` item 追加，不把控制事件伪装为消息

#### Scenario: v1 window 中的非 user role 迁移
- **WHEN** 请求将旧 v1 window 的非 user role 数据导入当前格式
- **THEN** 旧转换已退出支持，明确拒绝且保留原件，不凭 role 猜 root/Turn 或自动迁移。

#### Scenario: v1/v2 不得混写
- **WHEN** 在当前 rollout 追加旧格式1／2 envelope
- **THEN** 版本边界拒绝混写，原已提交内容和索引保持不变。

#### Scenario: v2 item-line 追加
- **WHEN** 调用方尝试追加旧格式2 item-line
- **THEN** 当前 writer 只接受格式3语义 Item，明确拒绝旧输入而不双写或兼容解码。

#### Scenario: 一次性 migration 读取 v1 message-line
- **WHEN** 调用方尝试使用已退出支持的 v1→v2 导入入口
- **THEN** 该入口及 staging/report 已删除，正常 reader 不提供替代转换；保留旧原件并报告不支持。

### Requirement: SQLite 是上下文和版本的权威数据库

SQLite 中的 `Turn.status` 必须使用闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`；`open`/`active` 非终态，其他值是 terminal outcome。合法转移固定为：

| 当前 `Turn.status` | 允许的下一状态 | 条件 |
|---|---|---|
| `open` | `active`, `cancelled`, `failed`, `unknown` | acceptance 已提交后由执行启动或明确控制结果收敛 |
| `active` | `completed`, `completed_empty`, `interrupted`, `cancelled`, `failed`, `unknown` | terminal convergence 一次提交 |
| `interrupted` | `active` | 仅显式 resume；必须新建 `execution_id` |
| `unknown` | `active` | 仅 reason=`execution_lost` 的显式 resume；必须新建 `execution_id` |
| `completed` | 无 | terminal；`final_item_id` 非空 |
| `completed_empty` | 无 | terminal；`final_item_id=NULL` |
| `cancelled` | 无 | terminal；对原 Turn 的 `resume_turn` 或绑定原 `turn_id` 的 `dispatch_replay` 返回 `turn_not_resumable`；新执行只能调用独立的 `replay_as_new_turn` |
| `failed` | 无 | terminal；只能由新的真实用户输入创建新 Turn |

`completed_empty` 是唯一无 canonical output 的正常终态名称。`cancelled` 是吸收态；普通 `cancelled` 与被快照归属策略标记不可恢复的 fork imported history 永久不可恢复，`resume_turn` 或绑定原 `turn_id` 的 `dispatch_replay` 不创建 execution/model-call、不改变 status，并返回 `turn_not_resumable`。`history_replay` 只能在相应 owner namespace 的 history view 中生成 projection；若调用方需要重新执行，必须明确调用独立的 `replay_as_new_turn`，由该新 Turn 创建操作在 active view 分配 target-local Turn、新的 root/acceptance/initial execution 和新的 `logical_turn_ordinal`，source history 可以作为上下文前缀，source Turn/root 不能成为新 Turn 的 root，并用 `replay_of_turn_id` 保存 lineage；它不是原 cancelled Turn 的 dispatch replay，不能在同一 API 语义中既返回错误又创建新 Turn。状态、execution/control outcome 和 final item 指针的变化必须由同一 SQLite terminal convergence 事务提交。

`accepted_ingress_id` 与 `acceptance_idempotency_key` 必须分别建立 `(session_id, thread_id, accepted_ingress_id)` 和 `(session_id, thread_id, acceptance_idempotency_key)` 唯一约束，并各自一对一指向 owner thread 中的 accepted Turn。相同 key、ingress、payload hash 和 source branch 的重试返回原 Turn/root/initial execution；key 与 ingress/payload/branch 任一冲突，或 ingress 被不同 key 重用，必须返回明确 acceptance idempotency conflict，不创建或修改 Turn。跨 session fork 或跨 thread materialization 生成的 copied 值也必须占用 target SessionThread 的本地唯一空间并标记其 identity origin，不能使用 source 裸值绕过约束。

系统 SHALL 使用单个 `index.sqlite` 保存冻结的当前格式3 `rollout_format_version` 和 SQLite schema version、canonical item catalog、权威 Turn/Execution/ModelCall lineage、工具投影、reasoning 投影、checkpoint、branch、context view、assembly outcome、fork 来源、retention、`storage_commits` 和提交状态；未发布 v1/v2 canonical 离线转换及专用 staging/report 已退出支持，旧 source coordinate/offset 不作为当前运行时输入。`TurnRecord` 至少保存 `turn_id`、owner-thread-global `turn_ordinal`、`accepted_ingress_id`、`acceptance_idempotency_key`、`root_input_item_id`、`initial_execution_id`、`last_execution_id?`、`final_item_id?`、status 和 origin `source_branch_id`。SQLite 不得被定义为可以仅从 JSONL 重建完整控制状态的缓存。

本轮字段重构须把用于查找、排序、唯一性、恢复和安全判定的核心控制信息放入带 CHECK/FK/UNIQUE 约束的明确 SQLite 列或 typed projection/manifest，字段 owner 与 JSONL typed core/registry binding 对齐：例如 `projection_group` 的 ordinal/size/form、已确认工具结果身份、source revision、`ContextContribution.selection_role`/`replacement_policy`/typed source binding，以及只由 registry 分配的 `(session_id, thread_id, source_ordinal)`。普通 `extensions_json` 仅保存 namespaced/versioned opaque annotation，不建隐藏决策索引，不从它恢复核心缺失列；`payload_kind=extension` 的正文仍以 canonical JSONL payload 为事实源。SQLite 的 item catalog 与投影只复制/索引已提交 JSONL item 的必要核心字段，不能成为可与 JSONL 分歧的第二 payload 事实源；registry/assembly/control 的自身权威列继续留在 SQLite。schema writer、reader、fork快照、checkpoint codec、history/provider projector必须共用同一领域类型/序列化合同，拒绝旧实验 v2 `metadata_json` 决策路径。现有实验 v2 字段形态不要求兼容迁移或双写；正常打开不匹配新 schema 的数据要报告明确 schema-incompatible，保留原件供显式重建，不得以空扩展或默认值修补。当前 rollout SQLite schema 为31，canonical 格式为3；版本分别由原 owner 登记。普通启动不自动迁移、清理或吸收未验收开发数据，测试数据只能按任务授权重建。

本轮不提供旧实验 v2 schema1/2/3 的兼容升级入口或旧 `metadata_json` artifact reader；这些开发数据与其升级专属接口、fixture 和测试不属于支持合同。现行 bootstrap/schema journal、事务执行与完整性校验仍归原 storage owner。未发布的 v1/v2 canonical 离线转换已退出支持，不冻结旧 writer／reader。其它已登记有序 SQLite 结构迁移仍由原 owner 显式执行，不因本次退出而删除，不授权改写真实 Home。

#### Scenario: SQLite schema 迁移

- **WHEN** 软件升级需要增加 item catalog、Turn root 或 assembly 表结构
- **THEN** 对本 capability 支持的已声明源版本，系统通过 `schema_migrations` 在事务中执行有序 migration，并更新 `database_meta.schema_version` 与 rollout format metadata；本轮明确不支持的旧实验 v2 通用 metadata 字段形态不得被自动转换或兼容读取

#### Scenario: 旧实验 v2 开发数据明确拒绝

- **WHEN** 正常启动、读取、fork 或恢复遇到旧实验 v2 schema1/2/3、旧 metadata 字段或缺少当前 typed owner 核心列的数据
- **THEN** 系统明确报告 schema-incompatible，不执行旧升级 parser、不猜补 owner/来源事实、不修改原 SQLite/JSONL/detail；开发者可按授权删除后由现行 writer 重建，普通请求不得隐式清库

#### Scenario: 扩展值不能修补缺失的核心索引

- **WHEN** item catalog 缺少投影归组或结果确认等必需 typed 字段，而 `extensions_json` 存在同名值
- **THEN** seal/read/recovery 报 schema 或 index integrity error，不从扩展推断核心事实、不推进 committed offset，也不改写 JSONL；只有结构合法的未知扩展可供审计原样保留

#### Scenario: SQLite 损坏

- **WHEN** `PRAGMA integrity_check` 或读取关键 item/Turn/view/assembly 控制表失败
- **THEN** 系统进入 `recovery_required` 并要求从 SQLite 备份恢复，不得仅扫描 JSONL 伪造 branch、checkpoint、Turn root 或 context view

#### Scenario: 核心字段集中定义

- **WHEN** 业务代码需要读写 item catalog、Turn、checkpoint、view、branch、assembly 或 storage commit
- **THEN** 操作通过统一数据库层和集中 schema/领域类型执行，必需核心字段及约束由其权威 owner 定义，不在业务模块散落另一套表名、SQL 参数或自由扩展字段决策

### Requirement: SQLite 能按 offset 快速定位消息

系统 SHALL 在当前格式3的 `item_catalog` 保存物理 sequence、稳定 identity、可选 `turn_id`、`semantic_kind`/`payload_kind`、status、JSONL offset/length、logical `payload_length`、`source_revision`、content hash 和 commit_id，并为 `(turn_id, item_sequence)`、Turn root、view logical ordinal、`wire_role`/`semantic_kind` 和 checkpoint view 建立索引。原 Item catalog／projection 与 view membership MUST 提供每个 `expandable_activity` 逻辑 Item 的 item_id、物理 sequence/offset reference、producer/tool relation、created time 和相邻计数成员 elapsed；不另建 turn_item_projection、Part 轮廓或第二索引；首项elapsed基于Turn acceptance/root，Turn duration基于terminal时间。`item_count`必须由同revision投影成员计数，不能由`last_item_sequence - first_item_sequence`、JSONL首尾区间、物理邻接或DOM容器数量推导；聚合tool summary与重复carrier不能建立额外成员，显式Turn-member compaction summary必须建立一个成员，时间缺失/倒退或revision不一致必须显式失败。busy thread的queued root可与旧execution后续item物理交错，因此Turn成员不要求形成连续sequence。旧 message-line 坐标不进入正常 history/Provider/checkpoint/runtime，不保留 staging/report 或兼容 fallback。当前 `messages` 的 checkpoint 消息组 locator 继续由原 writer 维护，不与旧 message-line artifact 混同。`payload_length` 是按 payload schema 编码的正文 bytes，不是 JSONL line length；`canonical_history` 的 `ContextRef.content_length` 只能来自该字段并经 payload/hash 复核，request-only/overlay 必须来自同一 assembly 的 sealed detail/contribution source manifest，`tool_set` 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest；三者都不能使用 JSONL offset/length 或 wire message 长度替代。读取单个 Turn 或窗口时不得从 JSONL 文件头线性扫描到目标。

#### Scenario: 读取中间 Turn

- **WHEN** 调用方请求游标附近的 Turn
- **THEN** 系统先通过`context_view_turns`、`TurnRecord.root_input_item_id`和该Turn的显式逻辑membership/projection找到有序成员，再只读取这些成员命中的item offset/length；不得把first/last sequence之间的其它Turn或ambient item吸收进来

#### Scenario: 快速找到 Turn 开头

- **WHEN** 调用方需要加载或解析一个 `turn_id`
- **THEN** 系统直接读取 `TurnRecord.root_input_item_id`/sequence，不根据第一个 wire role、首个物理 item 或 `MIN(item_sequence)` 猜测 Turn root

#### Scenario: 读取大型工具结果摘要

- **WHEN** 调用方只请求工具名称和状态
- **THEN** 系统从 SQLite tool projection 返回摘要，不打开大型 tool result JSONL 正文

### Requirement: Context plan selection 和 contribution ordinal 必须持久化

selection source 的逻辑唯一性必须由持久化约束表达：draft ContextRef registry 使用 `UNIQUE(session_id, thread_id, plan_id, ref_type, ref_id)`，其中 canonical item 可跨 plan 查询复用、request-only ref 不得跨 plan 复用；同一 sealed assembly 的 ContextRef selection 使用 `UNIQUE(session_id, thread_id, assembly_id, ref_type, ref_id)`，ToolSetRef 使用独立的 tagged identity 约束，plan registry 另有 `UNIQUE(session_id, thread_id, plan_id, tool_set_snapshot_id)`。`ContextRef` 本身不携带 assembly identity；`assembly_id` 只由 sealed selection/manifest 绑定，request-only 的最终 `detail_ref` 必须解析到同一 `(session_id, thread_id, assembly_id)`。`tool_set` selection 的 `ref_id` 只能解析到该 plan 的 ToolSetSnapshot，不能与 ContextRef 或 contribution 共用同一 typed identity。

`ContextRequestPlan` 与 `ContextAssemblySnapshot` 的 identity/lifecycle 必须一并持久化：创建 plan 时生成 thread-local `plan_id`，初始为 `plan_state=unsealed`、`assembly_id=NULL`、`selection=[]`；未 seal 的 plan 可以保存 registry，但不产生 assembly ref、`ContextSelectionEntry` 或 `plan_ordinal`。Saver 仅在 seal preflight 通过后于同一提交边界分配 thread-local `assembly_id`，生成 selection/ordinal 并持久化 snapshot；成功 seal 后 snapshot 的 `plan_id` 与 `assembly_id` 一对一且不可变，失败则不留下可 dispatch 的 assembly。plan 创建幂等使用 `(session_id, thread_id, plan_creation_idempotency_key)`，seal 幂等使用 `(session_id, thread_id, plan_id, seal_idempotency_key)`，两者冲突分别报告 `plan-idempotency-conflict`/`assembly-idempotency-conflict`。`ContextSelectionEntry` 只能存在于已分配的 assembly scope；无 source 的空 selection 可以 seal，但必须带该 assembly identity。

`ContextAssemblySnapshot` MUST 持久化 Saver 冻结的 `selection` 列表。每个 selection entry 都有唯一的 `plan_ordinal`、`selection_kind`、序列化的 tagged-union `ref`、visibility、protection、availability、`included`，以及 omission 时的 `omission_reason`/`loss[]`；只有 `included=true` 的 entry 才强制 source revision、逻辑 `content_length` 和恰一个 `content_hash`。`canonical_history|request_only|overlay_base|overlay_delta` 必须绑定一个 ContextRef，`tool_set` 必须绑定一个 ToolSetRef。ContextRef 的 `session_id`/`thread_id`/`ref_id` 在 draft 只按 `(session_id,thread_id,item_id)` 或 `(session_id,thread_id,plan_id,ref_id)` registry 解析，不携带 assembly binding；只有 sealed entry/`ref_manifest` 绑定 `assembly_id`，只有 included request-only entry 才能拥有解析为同一 `(session_id,thread_id,assembly_id)` 的最终 `detail_ref`。每个 included 且被 selection 绑定的 `ContextContribution` 都有 `source_revision`、`content_length`、同一 hash-token 规则及 assembly binding 内独立、单调、不可重写的 `contribution_ordinal`，unsealed registry contribution 和 omitted entry 不预分配该 ordinal，overlay 还必须保存 base/delta role 与 source overlay epoch；ToolSetRef 不绑定 contribution ordinal。SQLite `assembly_item_refs` 至少保存 `plan_id`、`assembly_id`、`plan_ordinal`、ref identity、上述 integrity manifest 和 contribution/tool-set binding，并对 `UNIQUE(session_id, thread_id, assembly_id, plan_ordinal)`、`UNIQUE(session_id, thread_id, assembly_id, contribution_id)` 和 `UNIQUE(session_id, thread_id, assembly_id, contribution_ordinal)` 建立唯一约束。每条 included selection 的 source revision/length/hash token、visibility/protection/availability、base/delta role 和 overlay epoch 必须逐字段等于对应 ContextRef/ToolSetRef/contribution/detail manifest；omitted entry 的已知 identity metadata 若存在也必须一致，但 null/未分配的 detail、正文 hash/length 和 contribution ordinal 不得被补造。缺项、重复 ref/contribution/tool-set snapshot、union 类型与 selection_kind 不匹配或顺序/绑定不一致报告 `plan-order-integrity`，canonical/tool-set source mismatch 报告 `source-mismatch`，request-only detail mismatch/unavailable 报告 `detail-unavailable`。`selection_kind=tool_set` 的正文只能来自同一 plan/assembly 的 ToolSetSnapshot schema/config manifest 及其 hash，不从 item catalog、canonical ContextRef 或普通 contribution 借用。restore、LangChain、native Provider 和 Web history 只能消费这份 selection；Provider 将 included ToolSetRef 投影到 tools/tool-config，history 只保留策略允许的 metadata，不得按 `created_at`、`contribution_id`、物理邻接或 dict insertion order 排序，也不得把 request-only refs 或 tool definitions 无条件 prepend 到 canonical messages；optional omitted canonical/request-only/tool_set entry 只保留 omission/loss metadata，分别跳过正文、detail 和工具定义，不从当前 source/registry 回退。selection/ordinal 冲突必须报告 plan-order-integrity error。

矩阵明细见 `itemized-rollout-context` 的 requirement「selection_kind 与 ref_type 必须使用唯一兼容矩阵」；本节不再复述。存储仍 SHALL 在 source lookup 之前按该权威矩阵校验，矩阵外组合返回 `plan-order-integrity`；omitted entry 不解析 source 时正文字段和 contribution/detail binding 可 NULL/未分配，但已知 role/identity 必须与已提交 manifest 一致，restore/history 对 omitted canonical/request-only/tool_set 分别跳过正文、detail、工具定义，overlay omission 不应用 base/delta，required omission 拒绝 seal/dispatch。

`assembly_item_refs.contribution_id` 是 selection 到 contribution manifest 的唯一映射列：included 且 contribution-backed 的 request-only/overlay entry 必须保存非空 `contribution_id`，它必须在同一 `(session_id, thread_id, plan_id)` registry 唯一解析一个 `ContextContribution`；字段归属见权威矩阵，本节不复述。`ContextRef.ref_id` 是 canonical `item_id` 或 request-only 的稳定逻辑 alias（普通 request-only 通常等于其 `contribution_id`，亦可为 producer 声明 source alias；overlay 为 overlay alias），但该列仍是唯一显式映射：restore、history 和两类 request projector 必须通过该列读取 manifest/body，禁止通过 detail_ref、hash 或 ordinal 反推 contribution，也不得把 overlay alias 当成 contribution_id。该列与 `UNIQUE(session_id, thread_id, assembly_id, contribution_id)`（NULL 不参与重复约束）共同防止多条 selection 隐式指向同一 contribution。

普通 `ContextContribution` 的持久化 `request_only` 字段必须存在且恒为 `true`，它是 contribution 本体语义，不是 `ContextRef`/selection 的 discriminator；后者只能使用 `ref_type=request_only`。其 `contribution_kind` 的闭合集合只有 `prompt | overlay_base | overlay_delta | notice`，其 `content_hash` 必须校验 `sha256:jcs:v1(JCS({"contribution_kind": <kind>, "body": <typed body> }))`；`tool_set` 不是 contribution kind，Provider tool definitions 只能由 ToolSetSnapshot/ToolSetRef manifest 提供。输入 `contribution_kind=tool_set` 必须返回 `contribution-kind-unsupported`，不得将其写入 contribution registry、转换为 ToolSetRef 或用于 Provider tools；不保留旧 migration reader／report／quarantine 例外。所有 included 正文统一保存并校验内部 content_hash；敏感正文及内部 hash 从默认外部投影隐藏，不使用 thread HMAC、redacted_stable_digest 或密钥。`content_length` 必须对应该 typed body 的 canonical encoded bytes，不能由 ref 字符串、JSONL line length 或 wire message 长度替代。`contribution_ordinal` 是 assembly binding：同一 contribution 进入另一 assembly 时重新绑定新的 ordinal，重启只能从已提交 `assembly_item_refs` 恢复，不能从 `created_at`/ID 重新排序。

#### Scenario: sealed selection 与 contribution ordinal 可恢复

- **WHEN** Saver 为一个已通过 preflight 的 plan 持久化 canonical/request-only/overlay/tool-set selection
- **THEN** storage 将其 `plan_ordinal`、适用的 `contribution_id`/`contribution_ordinal`、manifest 和 assembly identity 原子写入并可在重启后恢复；不适用字段保持 NULL
