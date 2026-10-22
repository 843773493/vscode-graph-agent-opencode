## MODIFIED Requirements

### Requirement: Rollout JSONL 只保存不可变消息

系统 MUST 按本 requirement 的 candidate、item、payload 和 compatibility matrix 合同读写 rollout JSONL 与 SQLite catalog；任何未满足的组合都必须显式拒绝或进入规定的 migration quarantine。

`candidate_key` 与 `candidate_status` 属于 v1 migration report 的候选记录字段，不是 `CanonicalItemRecord.status`、`Turn.status` 或 `ControlOutcome`；candidate_status 闭合集合为 `accepted`、`legacy_missing_turn_id`、`legacy_turn_group_ambiguous`、`legacy_multiple_user_messages`、`legacy_orphan`、`legacy_unsupported_role` 和 `legacy_identity_conflict`。只有 `accepted`/`legacy_missing_turn_id` 才允许继续 identity 补全，其余状态必须拒绝或 quarantine，不能创建可运行 Turn。

v1 root candidate 的生成规则冻结为：`message_sequence` 只提供可审计的 user-root window 边界，不能仅凭非 user 记录的物理邻接猜测 Turn。每个 window 必须恰好包含一个 `role=user` 行；窗口内无非空 `turn_id` 时生成 `candidate_key=legacy-missing-turn:<legacy_message_hash>`、状态 `legacy_missing_turn_id`，窗口内恰有一个非空 `turn_id` 时生成 `candidate_key=legacy-turn:<turn_id>`、状态 `accepted`，并将缺失 ID 的可迁移成员归入该 ID。窗口内多个不同非空 ID 时标记 `legacy_turn_group_ambiguous` 并整体拒绝；同一 ID 跨多个 user window 时标记 `legacy_multiple_user_messages` 并整体拒绝；首个 user 前、没有 root 的记录标记 `legacy_orphan`，重复 source coordinate、message identity 冲突或无法确定窗口边界标记 `legacy_identity_conflict` 并拒绝。

window 内的 legacy role 处理固定为：`assistant`、`tool` 以及明确等价的旧 `function` 是可迁移的 Turn member，缺失 `turn_id` 时继承该 window 的唯一 candidate ID；`system` 和 `developer` 只能作为 `legacy_request_context` 保留 source coordinate、payload hash 和受保护 detail/lineage reference，不进入 Turn root/member、不能创建 Turn，也不得静默丢弃。`system_reminder` 只有在已有可信 internal/checkpoint metadata 时才作为 Turn 外的 `runtime_notice`（`turn_scope=pending_next_turn`、`turn_id=NULL`）；否则标记 `legacy_unsupported_role` 并整体拒绝 candidate。未知 role、其它 legacy role 或不能解释的 role/payload 组合一律标记 `legacy_unsupported_role`，原始行保留到 report/quarantine 并整体拒绝。若 system/developer/受信任 system_reminder 携带非空 `turn_id`，该 ID 必须与 window 唯一 candidate ID 相同，否则标记 `legacy_turn_group_ambiguous`；即使相同也不成为 Turn member。整个 rollout 没有任何 user window 时，所有这些行均标记 `legacy_orphan` 或 `legacy_unsupported_role` 并只进入 report/quarantine，不生成 root 或 synthetic acceptance。

只有 `accepted` 或 `legacy_missing_turn_id` candidate 才能进入 synthetic identity 补全；被拒绝 candidate 的每条 source 行必须在 migration report 中保留归属、拒绝原因和 source coordinate，禁止静默丢失。

#### Scenario: v1 window 中的非 user role 迁移

- **WHEN** 一个 user-root window 含有 assistant/tool/function、system/developer、可信 internal/checkpoint metadata 的 system_reminder，以及 unknown 或其它 legacy role
- **THEN** assistant/tool/function 按唯一 candidate 的缺失 ID 规则成为 Turn member；system/developer 逐行写入 migration report 的 `legacy_request_context`；可信 system_reminder 写为 `turn_id=NULL`、`turn_scope=pending_next_turn` 的 Turn 外 runtime_notice
- **AND** unknown、其它 role、无法解释的 role/payload，或缺少可信 metadata 的 system_reminder，使整个 candidate 进入 `legacy_unsupported_role` 并 quarantine；所有原始行保留 source coordinate、payload hash、归属和拒绝原因，不得静默丢失或按物理邻接改派

v2 `CanonicalItemRecord.status` 完整枚举固定为 `completed | partial | incomplete | cancelled | failed | unknown`，全部是可写入 JSONL 的终态；`completed` 表示声明 payload 已完整收敛，`partial` 表示截至中断/停止边界已持久化但尚未达到正常 semantic boundary 的完整快照，二者都不可在 JSONL 中互相改写。`draft`、`open`、`active`、`running` 和 `completed_empty` 只能存在于内存 draft 或 SQLite assembly/Turn/control state。ItemDraft 只能从 `draft` 转移到一个终态，JSONL 行提交后不得改变 status/payload/hash，修正、retry、resume 必须追加新 item 并建立 relation；无稳定 payload 的 draft 不得补造 `status=unknown` item，provider 空输出使用 metadata-only terminal convergence。item status 只描述单个 item payload 的事实，不等价于 execution/control outcome；后者统一使用 `ControlOutcome=completed|completed_empty|failed|interrupted|cancelled|execution_lost|unknown`，仅用于 ExecutionRecord、ModelCallRecord、assembly、storage commit 或控制记录。不得引入带 outcome 前缀的 unknown 状态别名；`tool_outcome=success|failure|cancelled|unknown` 只允许作为 tool_result typed payload marker。

v2 `payload_kind` 完整枚举固定为 `text | structured_content | tool_call | tool_result | summary | attachment_ref | opaque | extension`。`opaque` 必须有非空 encoding/value、provider 或 wire type、schema version；`extension` 必须有非空字符串 `extension_schema` 与 `extension_version`，其中 schema 是稳定 namespaced identifier、version 是该 schema 的显式版本值，另有 value 和 protection/encoding metadata。未知 kind、未知 shape 或缺失 extension schema/version 必须进入 schema recovery error，不得静默按普通文本/空 payload/opaque 读取；不支持的已知 extension 只能保留为 immutable unsupported record，不能进入普通 context。

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

系统 SHALL 为每个生产 rollout 保存 UTF-8 v2 `rollout.jsonl`。v2 item-line rollout 的每行是不可变 `CanonicalItemRecord`，是正常生产运行时唯一 canonical 事实源；v1 message-line 只能作为保留的旧原始 artifact，由显式一次性 `legacy_import_v1_to_v2` migration/import operation 读取，不能由正常 storage、history、provider、checkpoint 或 Turn runtime API 打开。SQLite manifest/database metadata 的 `rollout_format_version` 与每个 envelope 的 `format_version` 必须一致。v2 顶层 envelope 的必填核心字段必须存在且非 null：`format_version=2`、`record_type=item`、`item_sequence`、`item_id`、`semantic_kind`、`payload_kind`、`status`、`producer_ref`、`payload`、`content_hash`、`created_at` 和 `extensions`；`turn_id`、`turn_scope`、`message_group_id`、`projection_group`、`projection_identity`、`tool_result_evidence`、`source_manifest` 和 `wire_role` 是按语义可空/可省略的 typed 关联字段，旧 v2 顶层 `metadata` 不合法。`turn_scope=turn_root` 必须有 `turn_id` 并且只能指向该 Turn 唯一的 `user_input` root；普通 Turn item 必须是 `turn_member` 且有 `turn_id`；反向地，任何非空 `turn_id` 都必须配合 `turn_root` 或 `turn_member`，不得出现 `turn_id != NULL` 且 `turn_scope=NULL`；`ambient`/`pending_next_turn` 必须无 `turn_id` 且不属于普通 Turn member/root；持久化 pending runtime notice 必须使用 `semantic_kind=runtime_notice` 和 `turn_scope=pending_next_turn`，request-only notice 不生成 item。若 Turn 关联字段为空，reader 不得从物理顺序、wire role 或 message group 猜测 Turn。v1 envelope 仅供 migration reader 校验，字段为 `format_version=1`、`record_type=message`、`message_sequence`、`message_id`、`turn_id`、`role`、`message` 和 `metadata`。`assistant_text` 与 `final_response` 不得作为 canonical item kind。JSONL 不保存 checkpoint、branch、rewind、fork、compaction 等控制记录本身，但允许追加 `semantic_kind=compaction_summary` 的 canonical item；v1/v2 format dispatch 必须显式且不得混写。

v1 migration 的 root candidate 分组必须先按 source `message_sequence` 以每个 `role=user` 行切出 user-root window。窗口内无非空 `turn_id` 时生成一个 `legacy_missing_turn_id` candidate；恰有一个非空 ID 时使用该 ID 并仅吸收 assistant/tool/function 等可迁移成员的缺失 ID；system/developer 作为 `legacy_request_context` 留在报告/受保护 lineage，受信任的 system_reminder 映射为 Turn 外 runtime_notice，未知或其它 role 标记 `legacy_unsupported_role` 并整体拒绝。多个不同 ID 时整体拒绝。相同非空 ID 在一个窗口的可迁移非 user 行重复是正常成员；但同一 ID 跨多个 user window（即超过一个 user message）、重复 source coordinate、同一 message identity 对应不同 ID、或首个 user 前没有 root 的记录，都不得猜测为 Turn：分别整体拒绝相关组或保留为 `legacy_orphan`。只有单 root candidate 且 role/turn_id 校验通过才可补齐 synthetic ingress/execution identity。

#### Scenario: 一次性 migration 读取 v1 message-line

- **WHEN** 用户显式调用一次性 `legacy_import_v1_to_v2`，migration reader 打开 manifest 声明为 v1 的原始 rollout
- **THEN** 每行只在 migration staging/report 中按 legacy reader 读取，保留原始 `message_id`、`message_sequence`、`turn_id` 和 LangChain message；正常 storage/history/provider/checkpoint API 返回 `v1_migration_required`，不得把它伪装成已经完成 v2 migration

#### Scenario: v2 item-line 追加

- **WHEN** Agent 写入 user input、assistant output、reasoning、tool call、tool result 或 runtime notice
- **THEN** v2 rollout 按 item 顺序追加对应的 `CanonicalItemRecord`，不重复写入完整 LangChain messages 数组；历史 `assistant_text` 由 assistant output content part projection 生成

#### Scenario: v1/v2 不得混写

- **WHEN** manifest/envelope version 不一致，或同一个 JSONL 出现 v1 message 和 v2 item
- **THEN** reader 返回明确 format mismatch/recovery 错误，不混合建立 context view

### Requirement: 消息追加不得通过流式重复造成 O(n²)

系统 SHALL 在稳定的 v2 item 完成后追加一次对应 canonical record。流式 assistant output、reasoning content part 和逐步组装的工具参数在完成前不得每个 chunk 追加一份完整 item；未提交的流式内容可以在崩溃时丢失，但已提交 item 不得被覆盖。v1 message 只能在一次性 migration staging 中读取和转换，不得由生产 writer 追加或参与 dual writer。

#### Scenario: 流式 assistant output 完成

- **WHEN** 一个大型 assistant output 经历多个流式 delta 后完成
- **THEN** v2 rollout 只追加一个终态 `assistant_output` item 及其有序 content part，不为每个 delta 追加完整正文

#### Scenario: 流式过程中崩溃

- **WHEN** 进程在 assistant output item finalization 前崩溃
- **THEN** 系统可以丢弃未提交的 draft，但此前已提交的 item、Turn 和 SQLite 控制状态不得被删除

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

`accepted_ingress_id` 与 `acceptance_idempotency_key` 必须分别建立 `(session_id, thread_id, accepted_ingress_id)` 和 `(session_id, thread_id, acceptance_idempotency_key)` 唯一约束，并各自一对一指向 owner thread 中的 accepted Turn。相同 key、ingress、payload hash 和 source branch 的重试返回原 Turn/root/initial execution；key 与 ingress/payload/branch 任一冲突，或 ingress 被不同 key 重用，必须返回明确 acceptance idempotency conflict，不创建或修改 Turn。跨 session fork/migration 或跨 thread materialization 生成的 copied/synthetic 值也必须占用 target SessionThread 的本地唯一空间并标记其 identity origin，不能使用 source 裸值绕过约束。

系统 SHALL 使用单个 `index.sqlite` 保存冻结的 v2 `rollout_format_version`/schema version、canonical item catalog、权威 Turn/Execution/ModelCall lineage、工具投影、reasoning 投影、checkpoint、branch、context view、assembly outcome、fork 来源、retention、`storage_commits` 和提交状态；一次性 migration staging/report 可以另存 v1 source coordinate/offset，但该坐标不提供正常 v1 读取 API。`TurnRecord` 至少保存 `turn_id`、owner-thread-global `turn_ordinal`、`accepted_ingress_id`、`acceptance_idempotency_key`、`root_input_item_id`、`initial_execution_id`、`last_execution_id?`、`final_item_id?`、status 和 origin `source_branch_id`。SQLite 不得被定义为可以仅从 JSONL 重建完整控制状态的缓存。

本轮字段重构须把用于查找、排序、唯一性、恢复和安全判定的核心控制信息放入带 CHECK/FK/UNIQUE 约束的明确 SQLite 列或 typed projection/manifest，字段 owner 与 JSONL typed core/registry binding 对齐：例如 `projection_group` 的 ordinal/size/form、已确认工具结果身份、source revision、`ContextContribution.selection_role`/`replacement_policy`/typed source binding，以及只由 registry 分配的 `(session_id, thread_id, source_ordinal)`。普通 `extensions_json` 仅保存 namespaced/versioned opaque annotation，不建隐藏决策索引，不从它恢复核心缺失列；`payload_kind=extension` 的正文仍以 canonical JSONL payload 为事实源。SQLite 的 item catalog 与投影只复制/索引已提交 JSONL item 的必要核心字段，不能成为可与 JSONL 分歧的第二 payload 事实源；registry/assembly/control 的自身权威列继续留在 SQLite。schema writer、reader、fork快照、checkpoint codec、history/provider projector必须共用同一领域类型/序列化合同，拒绝旧实验 v2 `metadata_json` 决策路径。现有实验 v2 字段形态不要求兼容迁移或双写；正常打开不匹配新 schema 的数据要报告明确 schema-incompatible，保留原件供显式重建，不得以空扩展或默认值修补。既有 v1 一次性导入规则独立保留。本 change 将 rollout SQLite schema 升至 v18；旧 v17 开发库明确 schema-incompatible，不自动迁移、清理或吸收，测试数据只能按任务授权重建。

本轮不提供旧实验 v2 schema1/2/3 的兼容升级入口或旧 `metadata_json` artifact reader；这些开发数据与其升级专属接口、fixture 和测试不属于支持合同。现行 bootstrap/schema journal、事务执行与完整性校验仍归原 storage owner。独立 `legacy_import_v1_to_v2` 只处理 v1 message-line，并直接生成现行 typed v2，不得复用或重新引入上述旧实验升级链。

#### Scenario: SQLite schema 迁移

- **WHEN** 软件升级需要增加 item catalog、Turn root 或 assembly 表结构
- **THEN** 对本变更支持的已声明源版本，系统通过 `schema_migrations` 在事务中执行有序 migration，并更新 `database_meta.schema_version` 与 rollout format metadata；本轮明确不支持的旧实验 v2 通用 metadata 字段形态不得被自动转换或兼容读取

#### Scenario: 旧实验 v2 开发数据明确拒绝

- **WHEN** 正常启动、读取、fork 或恢复遇到旧实验 v2 schema1/2/3、旧 metadata 字段或缺少当前 typed owner 核心列的数据
- **THEN** 系统明确报告 schema-incompatible，不执行旧升级 parser、不猜补 owner/来源事实、不修改原 SQLite/JSONL/detail；开发者可按授权删除后由现行 writer 重建，普通请求不得隐式清库

#### Scenario: 扩展值不能修补缺失的核心索引

- **WHEN** item catalog 缺少投影归组或结果确认等必需 typed 字段，而 `extensions_json` 存在同名值
- **THEN** seal/read/recovery 报 schema 或 index integrity error，不从扩展推断核心事实、不推进 committed offset，也不改写 JSONL；只有结构合法的未知扩展可供审计原样保留

#### Scenario: SQLite 损坏

- **WHEN** `PRAGMA integrity_check` 或读取关键 item/Turn/view/assembly 控制表失败
- **THEN** 系统进入 `recovery_required` 并要求从 SQLite 备份恢复，不得仅扫描 JSONL 伪造 branch、checkpoint、Turn root 或 context view

### Requirement: JSONL 与 SQLite 提交边界一致

`storage_commits` 的字段和组合固定为：`commit_kind`=`acceptance|execution_admission|assembly_sealed|item_convergence|terminal_convergence`，`commit_mode`=`item_bearing|metadata_only`。一个逻辑 SQLite 事务只写一条 storage commit；一个 item-bearing commit 可以包含多个 JSONL item，所有对应 item catalog 行引用同一 `commit_id`。`acceptance` 必须是 item-bearing，`assembly_sealed` 必须是 metadata-only，`item_convergence` 必须是 item-bearing，`terminal_convergence` 可以是 item-bearing 或 metadata-only；同一 terminal outcome 不得拆成两条不同 mode 的 terminal commit。真实用户首次 acceptance commit metadata MUST 在 root append 前随同一 `acceptance` item-bearing commit 冻结有序已处理 head-internal notice IDs、execution/admission/source cut 和 selection hash；相同 key 重放返回该原 metadata，不能从当前 catalog 重选。notice 元数据不属于 user payload/admission payload hash。`execution_admission` 只用于 internal execution 的首次准入：notice 形态使用 item-bearing 并原子保存真实 execution 关联与首个 runtime notice；初始 task-seed/reference 形态使用 metadata-only，原子保存冻结的 request identity/preimage 和 admission，零 item、JSONL offset 不变。两形态均不得创建用户 Turn、accepted ingress 或伪造 terminal outcome，`outcome` MUST 为 NULL；真实用户 initial execution 仍属于原 acceptance 事务，不额外写第二条 execution_admission commit。`(session_id, thread_id, commit_kind, subject_id, idempotency_key)` 是唯一幂等范围，重放时 payload、outcome、mode、record count 或 offset span 不一致必须失败。
系统 SHALL 将提交分为 sealed-before-dispatch 与 terminal convergence 两个阶段，并在 `storage_commits` 中记录 `commit_id`、`commit_kind`、subject identity、idempotency key、`jsonl_offset_before`、`jsonl_offset_after`、`jsonl_record_count` 和 outcome。`database_meta.committed_jsonl_offset` 是每个 SessionThread rollout 的单一权威 committed boundary；`storage_commits.jsonl_offset_after` 只是同一边界的不可脱离副本。每条已提交 commit 必须满足 `jsonl_offset_before` 等于事务开始时的 database meta offset、`jsonl_offset_after >= jsonl_offset_before`，并在同一 SQLite 事务成功后满足 `database_meta.committed_jsonl_offset == jsonl_offset_after`；下一条 commit 的 before 必须等于上一条 commit 的 after，metadata-only 的 before/after 相等。插入 storage commit、更新 database meta offset、item index/view/checkpoint/control outcome 必须在同一个 SQLite 事务内完成，item-bearing commit 之前必须完成 JSONL durability barrier；reader 只能使用 database meta 这一个权威 boundary，并用 storage commit 副本作一致性校验。sealed 阶段必须先持久化不可变 `ContextAssemblySnapshot`、required detail references、plan/request hash 和 `dispatch_state=ready`，失败时不得发起 Provider request；terminal 阶段才将 canonical item、assembly terminal outcome、Turn/execution/model-call outcome、`final_item_id`、control、view、checkpoint、全部 `checkpoint_channels` 行和 database meta 收敛到 SQLite。item-bearing terminal commit 必须先将 canonical item 或 legacy message 写入并 fsync 到该thread的`rollout.jsonl`；provider 空输出、失败或 execution lost 没有 JSONL item 时，必须使用 metadata-only/control commit，before/after offset 相等且 record count 为零。`put_writes` 可以针对已提交 checkpoint 使用独立 SQLite 事务追加 pending writes，但不得绕过上述 boundary。SQLite 提交的 JSONL offset 必须已经可靠写入文件；启动时若 database meta、storage commit chain、文件长度或 item index 不一致，必须停止并报告 commit-boundary conflict，不能自行选择另一 offset。

#### Scenario: 未提交 JSONL 尾部

- **WHEN** 进程在 JSONL 写入后、SQLite 提交前崩溃
- **THEN** 启动恢复根据 `database_meta.committed_jsonl_offset` 忽略或截断未提交尾部，并保留此前已提交的 item、Turn、assembly 和 checkpoint 状态

#### Scenario: 空输出或失败的 terminal metadata-only commit

- **WHEN** Provider 没有返回 canonical item，或执行以 failure/interrupted/execution_lost 结束
- **THEN** 系统仍以 `commit_kind=terminal_convergence`、`commit_mode=metadata_only` 原子保存 assembly、model-call、execution 和 Turn outcome，不推进 JSONL offset，不创建空 item；重复相同 commit idempotency key 只返回原结果

#### Scenario: 初始 seed/reference 内部准入不伪造 item

- **WHEN** 唯一 internal admission owner 接受没有 runtime notice 的初始 task-seed/reference execution
- **THEN** 同一 SQLite 事务以 `execution_admission`/`metadata_only` 保存真实 admission、execution 与冻结 request preimage，零 JSONL item且 offset 不变，不创建用户 Turn、accepted ingress 或 terminal outcome；同 subject/key 相同 preimage 返回原结果，不同 preimage 明确冲突，重启读取同一事实

#### Scenario: 内部 notice 与准入同事务

- **WHEN** 唯一 internal admission owner 接受携带首个 runtime notice 的 internal execution
- **THEN** 系统在单一 `execution_admission`/`item_bearing` commit 中保存真实 admission/execution 与首个 notice，遵守 JSONL durability barrier，不创建用户 Turn 或 terminal outcome；原 acceptance、assembly_sealed、item_convergence、terminal_convergence 的组合约束保持不变

#### Scenario: sealed 提交与终态提交分离

- **WHEN** sealed-before-dispatch 已提交但进程在 dispatch 前或 terminal convergence 前退出
- **THEN** 恢复保留原 sealed plan；`dispatch_state=ready` 表示尚未确认发出，可用相同 assembly/model-call identity 继续或显式终止；已开始 dispatch 且缺少已提交完整模型 carrier 与同 owner checkpoint 完成证明的 call 必须以 `terminal_convergence`/`metadata_only` 的 `unknown/execution_lost` 收敛，不得假设成功。只有满足 itemized identity requirement 的全部非终态 pending `send/read/wait` 证明条件时，才能续接原 checkpoint；不得仅凭 `unknown/dispatched` 字段推断模型完成或重新发出原 Provider request

#### Scenario: SQLite 提交后恢复

- **WHEN** SQLite 事务已经提交但进程随后崩溃
- **THEN** 系统根据已 fsync 的 JSONL 和 SQLite 提交记录恢复该批 item/message，不得回退已提交的 Turn `final_item_id` 或 assembly outcome

### Requirement: SQLite 能按 offset 快速定位消息

系统 SHALL 在正常 v2 `item_catalog` 保存物理 sequence、稳定 identity、可选 `turn_id`、`semantic_kind`/`payload_kind`、status、JSONL offset/length、logical `payload_length`、`source_revision`、content hash 和 commit_id，并为 `(turn_id, item_sequence)`、Turn root、view logical ordinal、`wire_role`/`semantic_kind` 和 checkpoint view 建立索引。另一个后端权威`turn_item_projection` MUST以`(session_id, thread_id, turn_id, logical_item_ordinal)`唯一保存每个`expandable_activity`逻辑Item的item/part identity、物理sequence/offset reference、producer/tool relation、created time和相邻计数成员elapsed；首项elapsed基于Turn acceptance/root，Turn duration基于terminal时间。`item_count`必须由同revision投影成员计数，不能由`last_item_sequence - first_item_sequence`、JSONL首尾区间、物理邻接或DOM容器数量推导；聚合tool summary与重复carrier不能建立额外成员，显式Turn-member compaction summary必须建立一个成员，时间缺失/倒退或revision不一致必须显式失败。busy thread的queued root可与旧execution后续item物理交错，因此Turn成员不要求形成连续sequence。v1 `messages` 坐标只能由一次性 `legacy_import_v1_to_v2` migration staging/report 使用，不能成为正常 history/provider/checkpoint/runtime 的读取表或 fallback。`payload_length` 是按 payload schema 编码的正文 bytes，不是 JSONL line length；`canonical_history` 的 `ContextRef.content_length` 只能来自该字段并经 payload/hash 复核，request-only/overlay 必须来自同一 assembly 的 sealed detail/contribution source manifest，`tool_set` 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest；三者都不能使用 JSONL offset/length 或 wire message 长度替代。读取单个 Turn 或窗口时不得从 JSONL 文件头线性扫描到目标。

#### Scenario: 读取中间 Turn

- **WHEN** 调用方请求游标附近的 Turn
- **THEN** 系统先通过`context_view_turns`、`TurnRecord.root_input_item_id`和该Turn的显式逻辑membership/projection找到有序成员，再只读取这些成员命中的item offset/length；不得把first/last sequence之间的其它Turn或ambient item吸收进来

#### Scenario: 快速找到 Turn 开头

- **WHEN** 调用方需要加载或解析一个 `turn_id`
- **THEN** 系统直接读取 `TurnRecord.root_input_item_id`/sequence，不根据第一个 wire role、首个物理 item 或 `MIN(item_sequence)` 猜测 Turn root

#### Scenario: 读取大型工具结果摘要

- **WHEN** 调用方只请求工具名称和状态
- **THEN** 系统从 SQLite tool projection 返回摘要，不打开大型 tool result JSONL 正文

### Requirement: Context view 通过 SQLite 范围表达

系统 SHALL 使用不可变 `context_views` 和持久逻辑membership segment/reference表达有效 canonical history context，不得复制完整messages/items正文数组。每个segment必须按`logical_item_ordinal`解析为显式item refs或对父view已验证segment的切片；只有底层物理sequence连续、区间内每个已提交item都属于该segment且无queued Turn/ambient/其它execution排除项时，才可压缩为JSONL range，并必须保存成员数、端点identity和membership hash。resolver必须验证hash和成员，不得把首尾sequence/offset之间的所有行视为当然可见。`context_view_turns`必须保存view-local唯一的`logical_turn_ordinal`、`turn_id`、`root_input_item_id`和fork lineage；它不改变`TurnRecord.turn_ordinal`、`source_branch_id`或root identity。request-only reference和source overlay reference不属于普通canonical history view membership；assembly在解析view后必须独立执行context reconciliation。同prefix epoch只允许精确复用overlay并尾部追加；rewind重建由新epoch从冻结的Registry activation snapshot恢复tracked published完整revision，不从被隐藏lineage重新注入cutoff后delta，也不读取当前源。pending runtime notice 不成为普通 Turn root 或普通 Turn 分页成员。internal admission 的 notice（若有）及自身 response items 的 display_only membership 和私有执行恢复，统一消费 checkpoint-history-loading requirement「内部 execution history 由持久 admission 独立加载」；初始 seed/reference 没有 notice，也必须保持自身输出的明确成员关系与 checkpoint 协议闭合。公开 internal canonical lane 的资格与分页则只消费该 capability 的「同一历史游标有界推进 Turn 与内部 execution」，没有唯一安全 display notice 的 seed/reference 不产生公开展示项。Provider canonical history 不吸收这些展示成员，当前 execution 私有输入与自身输出由 admission 执行请求恢复路径保持协议闭合。`context_view_jumps` SHALL支持长view链的快速跳转和cycle检查。

#### Scenario: queued Turn 与旧 execution 物理交错

- **WHEN**Turn B的queued user root先取得较大的physical sequence，Turn A随后追加tool result和final item，使A/B成员在JSONL中交错
- **THEN**A、B的`turn_item_projection`和view membership仍分别按逻辑ordinal返回正确成员、计数、计时与执行顺序；任何range优化都不得把B root计入A或让B在A terminal前进入旧execution context

#### Scenario: rewind 到历史前缀

- **WHEN** 用户将 active head rewind 到历史 checkpoint 或 canonical item anchor
- **THEN** 系统创建新 branch 和新 view，引用目标 view 的有效前缀并隐藏旧后缀，不修改旧 item/message

#### Scenario: pending runtime notice 不拆分 Turn

- **WHEN** 被中断 execution 产生 pending runtime notice，随后用户提交新的 user input
- **THEN** 新 view 可以通过 ambient/assembly reference 使用该 notice，但 Turn 分页和 Turn root 从新的 `root_input_item_id` 开始

### Requirement: final response、tool summary 和 reasoning 由 SQLite 投影定位

系统 SHALL 在 SQLite 的 Turn、v2 item projections、tool relations 和 reasoning projections 中保存 final response pointer、工具摘要、reasoning 摘要和 encrypted reasoning 元数据。v2 的最终响应必须由 `TurnRecord.final_item_id` 指向 canonical `assistant_output` item；`assistant_text` 与 `final_response` 是 projection。provider phase 标记不能替代 v2 final item；v1 的 heuristic 只允许在一次性 migration staging 中生成迁移报告提示，不能作为正常 history/provider/checkpoint fallback。
`Turn.status=completed` 时 `final_item_id` 必须非空；`completed_empty`、`interrupted`、`cancelled`、`failed`、`unknown` 以及未成功 finalization 时必须为空。`cancelled` 只表示明确停止的不可运行历史状态，不得投影为成功 final response。

#### Scenario: 中间 assistant output 与最终响应并存

- **WHEN** 一个 Turn 包含中间 `assistant_output`、tool_call 和最终 `assistant_output`
- **THEN** `turn_finalize.final_item_id` 指向的 canonical item 才作为 `final_response`，其它可见文本只属于 `assistant_text` projection

#### Scenario: 没有 finalization

- **WHEN** Turn 只有 partial、failed 或 unknown assistant output，没有成功的 `turn_finalize`
- **THEN** `final_item_id` 保持为空，历史不得把最后一条 assistant item 猜测为 `final_response`

#### Scenario: encrypted reasoning 默认读取

- **WHEN** assistant output 包含 provider encrypted reasoning
- **THEN** Web 默认只返回 SQLite 中的安全摘要和存在标记，不返回或解密 encrypted payload

### Requirement: canonical carrier 与工具调用顺序必须可无损恢复

v2 `content_part` 的 canonical 正文固定存于父 item JSONL envelope 的 payload（多 part 使用 payload schema 的 `parts` 字段或同等明确字段），不存于 SQLite 或 `ContextPlanDetailStore`。part 的 `content_part_id`、semantic kind、ordinal 和 value 属于 payload，因此受父 item `content_hash` 覆盖。可选的 SQLite `item_parts` 只能是派生稀疏索引，保存 item/part identity、ordinal、JSON Pointer 或 part ordinal locator、可选的父 JSONL line offset/length、父 line hash 和 part hash/prefix hash；offset 是 immutable UTF-8 JSONL 行内的加速坐标，读取必须校验父 line/content hash，冲突返回 integrity error，缺少索引可解析命中的 item。大正文仍按 item offset/length 从 JSONL 有界读取。

一次性 migration staging SHALL 原样保存 v1 assistant `AIMessage.content` carrier 及其数组顺序；v2 SHALL 将对应的 normalized content part 和 provider item identity 归一化到 canonical `assistant_output`/`reasoning` item，并由 LangChain projector 在需要时恢复有序 `AIMessage`。正常 v2 provider/checkpoint 路径不得读取 v1 carrier。`AIMessage.tool_calls` 的执行语义在 v2 由独立 `tool_call` item 表达；`ToolMessage` 由 `tool_result` item 和 `tool_call_id` 投影生成。v2 的 `tool_invocation_id`、`tool_call_id`、`tool_attempt_id` 在 session 内唯一；一个 logical invocation 可以有多个 retry/resume call attempt，一个 call attempt 至多一个 tool attempt，一个 tool attempt 至多一个已提交 result，旧 attempt 通过 `retry_of`/`resumes` 保持不可变。只有 active lineage 中成功 completed 且未 superseded 的 result 可以作为 replay input。SQLite SHALL 使用稳定 item/content-part identity 定位内容；v1 source coordinate 只能存在于 migration mapping/audit，不创建覆盖 canonical 顺序的第二套全局 part 序号。

#### Scenario: 一个 assistant 同时包含多种 content carrier 和工具调用

- **WHEN** provider 返回 reasoning、summary、thinking、redacted thinking、text 和两个 tool call
- **THEN** v2 canonical history 保留对应 item/content-part 的来源顺序和 tool identity；LangChain full projection 可以生成合法的一个或多个 `AIMessage` 与关联 `ToolMessage`，但不把 `assistant_text` 当作 canonical item

#### Scenario: 工具卡片合并不改变 canonical 顺序

- **WHEN** 一个 tool_call item 与后续 tool_result item 通过相同 `tool_call_id` 关联
- **THEN** Web detail 可以把它们投影为一个工具卡片，但 canonical item 顺序和 LangChain 执行投影仍保留独立的调用/结果关系

### Requirement: 大型正文只保存在 JSONL

系统 SHALL 将大型 canonical assistant output、工具参数、工具结果和 encrypted reasoning 正文完整保存在对应 thread rollout JSONL。SQLite 只能保存类型、状态、长度、哈希、有界投影和 offset/length；`ContextPlanDetailStore` MUST 位于由 session catalog与thread catalog/resolver定位的真实thread node下的精确相对路径 `rollout/context-plan-details/<assembly_id>/<detail_id>`，不得写入全局 `${BOXTEAM_HOME}`、workspace attachment blob store或按显示名定位。该物理 detail store 只服务已分配 assembly 的 sealed manifest；unsealed plan 不得创建或解析 assembly detail path。`detail_id` 是 assembly 内 target-local 的不可变物理叶名；sealed `detail_ref` 是不暴露物理路径、规范化为 `{session_id, thread_id, assembly_id, detail_id}` 的逻辑 typed reference，由 resolver 唯一映射到该路径，调用方不得自行拼接路径。它只允许保存 request-only prompt、middleware 输入和 assembly 诊断详情，必须绑定同一 `session_id`/`thread_id`/`assembly_id` 并受 visibility/protection、retention/GC 和显式 detail capability 控制；credential/token/secret 的存储归来源凭据 owner，attachment 正文归原 blob owner；detail owner 只保存其合同允许的单份精确上下文正文，sensitive 只约束访问和默认外部脱敏。detail store 不得保存可替代 canonical item正文或attachment blob的第二副本。

#### Scenario: 默认工具摘要

- **WHEN** 调用方只请求 tool_summary
- **THEN** 系统只访问 SQLite tool projection，不解析大型工具参数和结果正文

#### Scenario: 显式读取大型正文

- **WHEN** 调用方明确请求大型 tool_call 或 tool_result
- **THEN** 系统通过 SQLite offset 读取 JSONL，并在超过预算时返回明确的 truncated 状态

## ADDED Requirements

### Requirement: rollout storage 必须以 SessionThread 为物理与事务 owner

系统 SHALL使用`(session_id, thread_id)`解析一个唯一的rollout JSONL、SQLite index、checkpoint control state、detail store和ContextStore transaction node。Session node的`session-control.sqlite`内thread catalog表是thread位置、main pointer与GraphBinding的权威索引，并与collaboration ledger/fanout及publication journal共享Session级事务；`session.json`不得保存可变main pointer/catalog。storage resolver MUST先验证catalog再定位thread node，不能扫描磁盘吸收目录，也不能把session root、其它thread node或`checkpoint_ns`当作回退路径。每个thread的`committed_jsonl_offset`、item sequence、Turn ordinal、source revision、active view和ToolSet applied revision都相互独立；Session control数据库不得保存这些thread-local canonical事实。

storage resolver MUST 在读取catalog或构造路径前用共享canonical validator验证恰为36-byte ASCII的`ses_[0-9a-f]{32}`与`thr_[0-9a-f]{32}`外形，并按 `migrate-identifiers-to-uuidv7` 的 `uuidv7-identifier-profile` 校验payload位 profile；落盘前还须验证完整path预算。任何超长、Unicode、分隔符、`.`/`..`、百分号编码、不符合该 profile 的 payload、错误前缀或大小写必须显式失败且不产生目录；resolver不得清洗、截断或以hash leaf/旧ID alias绕开该约束。

本 Requirement SHALL 取代本 change 中此前所有 session-root `rollout/` 物理路径和不含 `thread_id` 的 operational detail/plan/assembly locator；旧字段只能作为一次性 migration lineage 读取，正常 runtime不得继续生成。

主thread的相对locator MUST是`threads/{main_thread_id}`；非主durable thread的相对locator MUST是`threads/YYYY/MM/DD/{thread_id}`，其中日期来自thread不可变UTC `created_at`，不得额外增加hash shard。两类目录叶名都必须严格等于`thread_id`，且locator一经提交不可因日期变化、重启、显示名或thread kind变化而重写。可预测的主路径也不得绕过catalog/resolver。

聚合入口 MUST NOT 通过继续追加 Mixin 承载新职责：`RolloutStorage`（`app/services/infrastructure/rollout_context/storage/service.py`）与 `RolloutCheckpointSaver`（`app/services/infrastructure/rollout_context/checkpoint/saver.py`）等聚合入口 MUST 收敛为 `design.md` 1.1/1.1.1 冻结边界下的薄 facade，新增职责 MUST 落到对应聚焦模块（storage/checkpoint/assembly/operations/mapping 等），聚合入口作为类定义直接基类的 Mixin 集合 MUST NOT 净增长；已存在的大文件拆分义务只增不减。

#### Scenario: 两个 thread 不能共享 offset 或上下文事实

- **WHEN** 同一 Session 的 main thread 与 delegated child thread 都提交 item 或 checkpoint
- **THEN** 各自只推进自己的 JSONL/SQLite transaction；任何一方失败、rewind、compaction 或 ToolSet rebase 均不得改变另一方的 offset、active view、source state 或 sealed assembly

### Requirement: rollout 定位必须收敛为 thread-qualified 的单一落点

系统 SHALL 把上一条「(session_id, thread_id) 为物理 owner」落到唯一一组可机械核对的落点上，禁止 session 级 `rollout/` 与 thread 级落点双轨并存：

- rollout 物理节点、`index.sqlite`、`rollout.jsonl` 与 thread 级 control state MUST 位于 `(session_id, thread_id)` 解析出的 thread node 下，相对 session node 的 locator 对 main thread 恰为 `threads/{main_thread_id}`、对非 main durable thread 恰为 `threads/YYYY/MM/DD/{thread_id}`；`RolloutStorage` 的 `root()`/`index_path()`/`jsonl_path()` MUST 接受 `(session_id, thread_id)` 并解析到 `<thread_node>/rollout`，MUST NOT 继续解析到 `<session_node>/rollout`，也 MUST NOT 把裸 `session_id` 或 `checkpoint_ns` 当作 thread identity 使用。
- 每个 thread MUST 拥有各自的 `index.sqlite` 与 `rollout.jsonl`；`database_meta` 的单行 `singleton_id=1` 语义只在该 thread 自己的 DB 内成立。不同 thread MUST NOT 共享同一 rollout DB、`committed_jsonl_offset`、`last_item_sequence`、`source_overlay_epoch` 或 `last_control_sequence`；线程级隔离 MUST 由「每 thread 独立数据库文件」实现，MUST NOT 退化为在单一 Session 级 DB 内用 `thread_id` 列区分同一份 control 状态的第二套形态；同一 Session 的 main/child 之间任一 control 状态的变化都不得被另一方观察到。
- detail store 与 context source control 的 rollout 根 MUST 来自唯一 thread node，MUST NOT 来自 session node；detail_relative_path 只解析原单份正文，不保留 protected_detail_relative_path、ContextDetailKeyStore、redaction key 或第二密文落点。
- Session 级物理存储（message stream、trace、LLM request log、pending request）MUST 继续由 Session node 作为唯一物理 owner 并按 session 定位，MUST NOT 移动、拆分或复制为 thread node 下的独立物理存储。message stream 与 trace 为暴露和校验实际 thread 身份所需的 API/SSE/cursor/cache/index 逻辑引用 MUST 携带 `thread_id`；这类逻辑身份隔离不改变其 Session 物理路径和 owner。Trace 的 Job 归属复用已提交创建/启动索引事实，不新增可变归属表；缺失、冲突或跨 Session 的归属明确失败，不能默认分配给 main。thread 化物理范围以本条落点清单为界。

#### Scenario: main 与 child thread 的 rollout 落点互不重叠

- **WHEN** 同一 Session 的 main thread 与一个 delegated child thread 各自首次写入 rollout
- **THEN** 两者分别解析到 `threads/{main_thread_id}/rollout` 与 `threads/YYYY/MM/DD/{thread_id}/rollout`，持有各自的 `index.sqlite`/`rollout.jsonl` 与单行 `database_meta`；任一方推进 offset 或 reset 均不改变另一方的 `committed_jsonl_offset`、item sequence 或 control sequence

#### Scenario: session-only 定位不得冒充 thread 定位

- **WHEN** 调用方只提供 session_id 而要求 rollout/index/JSONL 落点，或提供 canonical `thr_` thread id / 裸 `main` 别名充当 session_id
- **THEN** resolver 显式失败并指明缺少或错误的 owner 维度，不得回退到 session 级 `rollout/`、不得以 session node 冒充 main thread node、也不得把 `checkpoint_ns` 升级为 owner

#### Scenario: 单份 detail 归入 thread node

- **WHEN** 为某个 assembly 写入或读取 request-only detail
- **THEN** 唯一正文位于该 thread 的 thread node 下的 `rollout/context-plan-details/<assembly_id>/<detail_id>`，同一 Session 的另一 thread 不能经 session node 读取或写入这些 detail；不创建本地密钥或第二密文落点

### Requirement: 附件正文必须使用 workspace 级内容寻址 blob store

系统 SHALL将附件正文直接保存到`${workspace_abs_path}/.boxteam/attachments/YYYY/MM/DD/{blob-id}`，不得在日期与blob叶名之间增加Session、thread或digest shard。`blob-id` MUST匹配68-byte ASCII `blb_[0-9a-f]{64}`，payload MUST等于精确正文bytes的SHA-256小写hex，并与`digest=sha256:<同一payload>`及length逐字节一致；不得使用原始文件名、扩展名、MIME、Session或thread参与身份或路径。日期 MUST是该digest首次成功取得catalog唯一claim的UTC日期。`${workspace_abs_path}/.boxteam/attachments/catalog.sqlite` MUST是attachment identity、digest、受校验相对locator、length、MIME/protection、variant lineage、session/thread/item refs、retention、tombstone与GC的唯一权威；reader不得扫描日期目录定位blob或把调用方字符串拼成路径。

Session/thread所属上传 MUST在写入workspace ingest或任何staging正文前取得按session ID互斥的workspace `SessionLifecycleGate`，在gate内验证Session catalog/thread及`SessionLifecycleFence`仍为同一active generation，并于owner Session的`session-control.sqlite` create-or-get承担operation lease的durable `AttachmentOperationPin`，冻结generation、ingest operation、精确session/thread和preimage；同operation不同preimage MUST冲突。随后以软件生成的ingest idempotency key于attachment catalog create-or-get `AttachmentIngestRecord(state=preparing)`，冻结该pin identity、workspace/preimage、限制和受控内部staging locator。正文只能写入该record指定的staging，完成有界写入、hash/length计算与durability barrier后，record在catalog事务中推进为`hashed`并以digest唯一约束create-or-get `BlobCommitClaim`；胜出claim冻结blob ID、首次UTC日期、最终relative locator和预期hash/length。胜出者原子rename到尚未发布的最终locator并复验正文；发布blob availability、逻辑attachment/variant、owner reference及terminal record/claim前 MUST再次取得同一gate，在持锁期间验证原pin有效、fence仍为pin捕获的active generation且thread未失效并提交attachment catalog事务后才释放gate。canonical `attachment_ref`只能在这些事实已发布后提交。pin只能在owner reference和canonical使用事实durable，或失败清理terminal后释放。并发相同digest的其它ingest MUST复用该claim的首次日期与唯一locator；正文尚未落盘时，可用自身已校验的同内容staging完成原claim，不得提前清除staging后因缺少胜出正文而失败。活winner身份不得推测接管，发布后的正文缺失或损坏仍必须明确拒绝。

fork 跨域附件导入必须用按 copy operation 分片的跨进程 holder 覆盖全部 staging 写入与网络接收，并与短时正文提交锁分离。操作终结不等于旧 writer 已停止：中止与 GC 只能非阻塞取得该 holder 后，重查该 operation 的全部附件 claim 已为 committed/aborted/target_deleted，再按保留 claim 中的冻结 blob 和原 operation 派生 staging locator 并删除；不得依赖已移除的 alias mappings、扫描目录或按 blob 全局删除其它操作的文件。活 writer 保留自身 finally 清理；进程退出后或物理删除中断，由后续 GC/startup 按同一定位规则幂等重试，错误必须暴露。

pin与workspace ingest MUST以稳定operation identity形成显式跨库saga，不得假设两个SQLite原子提交。在附件saga中，`SessionLifecycleGate`串行化pin准入、owner-reference提交和Session进入`deleting`三个短临界区，不得跨正文写入或模型执行持有；它同时遵守Session其它副作用准入的统一gate/fence合同。gate必须在workspace内跨进程互斥、进程退出自动释放且不可用时fail closed。统一锁序为先取得一个gate，再打开至多一个catalog或SQLite写事务；禁止持有任一存储事务后等待gate、同时持有两个Session gate或两个数据库事务。恢复仍只读持久record。恢复只枚举非终态pin、ingest和claim记录冻结的有限locator；pin后、ingest前崩溃可由owner恢复/删除流程终结，rename后但catalog发布前的正文保持不可见并由原claim继续或清理。Session先进入`deleting`时必须拒绝新pin和尚未提交的owner reference；若owner reference已在更早的持锁临界区提交，删除owner必须先阻断canonical使用、释放该reference并收敛已有pin，之后才能隔离Session节点。不得留下指向删除owner的reference。无record文件不得被reader、恢复或GC扫盘吸收。同一blob ID对应不同digest、length或bytes时 MUST返回`blob-identity-conflict`且不得覆盖。

相同 digest 的后续上传 MUST 复用既有 blob 和首次 locator，不得按上传日期复制。删除 Session/thread/item 时只移除相应 reference；存在 canonical item、active execution、sealed assembly、checkpoint/operation pin 或其它有效 owner reference 时不得删除正文。满足零引用与 retention 后，GC MUST 先原子提交 tombstone/availability，再删除物理 blob；失败可幂等重试。canonical `attachment_ref` 与外部 API不得包含物理 locator，访问必须同时校验 workspace、session、thread、item/view membership、hash/length 和 capability。

上传 MUST 先冻结消息 ID，并将其绑定到原 pin 的恢复引用和 preimage。正文发布不得提前完成 pin；用户 acceptance 或内部 notice admission 提交后，canonical owner MUST 核验原始接受提交及同一 item 的附件使用索引，再结算该消息的 pin。恢复 MUST 先排空消息准备 holder 并取得 Session gate；未接受消息的引用只有在同 Session 所有已发布 Thread 均无 canonical 使用且无 fork 保活时才可定点释放，禁止释放整个 Thread/Session。fork 准备期仍有 claim/mapping 保活时 MUST 保留原 pin，直至发布或撤销后重新核验。fork 发布、中止或目标 Thread 删除后，必须在释放事务和 Session gate 后触发同一定点恢复，不依赖下次进程启动；活上传 holder 仍不得被抢占。缺少消息身份、正文/索引或接受提交不一致 MUST 明确失败；不建立第二份消息正文或附件使用事实。

正文提交 MUST 复用固定分片跨进程 holder 锁串行化同一 blob 的 claim、rename 与校验，普通上传释放正文锁后才取得 Session gate 发布。`hashed` 非终态 ingest MUST 在 catalog GC 事务中保活正文；fork 导入沿原 preparing/owner_reserved claim 保活，网络接收不占正文提交锁。GC MUST 非阻塞取得同一正文锁并在事务内重查操作、owner 与 retention 依赖，不得在 Session gate 内等待正文锁。未发布且无依赖的 claim MUST 先持久推进 aborted，再按冻结 locator 校验并删除正文；删除中断仍由同一 claim 重试，不扫描目录。新上传或授权 fork 导入可以重新领取该 digest claim，保留首次日期与 locator，禁止重开旧 ingest 或 pin。已 tombstone 的正文经新操作校验并发布后可恢复 available，且必须清除旧 tombstone 时间；已 available 正文的缺失或损坏不得由重传静默修复。

#### Scenario: 同正文胜者在 rename 前崩溃
- **WHEN** 第一个真实上传进程已提交唯一 claim 但尚未 rename，第二个进程上传相同正文，随后第一进程退出
- **THEN** OS 释放正文锁后第二上传使用自己的已校验 staging 完成原 locator 并正常接受消息；恢复只终止失败消息的 ingest/pin，不改写活 winner 身份或另一 owner 的引用

#### Scenario: 未发布正文清理中断与重新上传
- **WHEN** 上传在 rename 后、availability 发布前退出，原 holder 已停止，清理在 claim aborted 提交后中断
- **THEN** 下一次 GC 仍只按该 claim 重试精确正文删除；新操作重新上传时保留首次日期和 locator，旧尝试保持终态，其他仍有 ingest 或 fork 依赖的正文不能被回收

#### Scenario: 正文已发布但消息尚未接受时崩溃

- **WHEN** 上传已发布 owner reference，但消息尚未通过用户 acceptance 或内部 notice admission，随后进程退出
- **THEN** pin 仍非终态；恢复只释放该失败消息无其他 canonical 使用及 fork 保活的精确引用，再终结原 pin，不改变同 Thread 已接受消息或共享 blob 的其他 owner

#### Scenario: 消息已接受但 pin 尚未结算时崩溃

- **WHEN** canonical 接受事务已提交，进程在 pin active 或 settling 状态退出
- **THEN** 恢复验证原消息、接受提交和附件使用索引后结算原 pin，保留附件读取；旧 fencing token 不能再次回调，索引损坏时不得视为消息未接受而释放引用

#### Scenario: 相同正文跨日期并发上传

- **WHEN**两个ingest在UTC日期边界两侧完成相同正文的hash并竞争digest claim
- **THEN**catalog只允许一个claim冻结首次日期和唯一locator；另一个ingest清理自身record中的staging并复用胜出blob，两个逻辑attachment可有独立owner reference但不得产生第二份正文

#### Scenario: blob rename 后、catalog 发布前崩溃

- **WHEN**进程已把record中的staging rename到claim冻结的最终locator，但尚未发布blob availability、attachment和owner reference
- **THEN**普通reader与canonical writer仍看不到该blob；恢复只按非终态claim校验并继续或清理，禁止扫盘吸收、提交悬空`attachment_ref`或改写其它digest的正文

#### Scenario: 上传与 Session 删除竞态

- **WHEN**附件operation已取得Session-local pin但尚未发布workspace owner reference，并与目标Session进入`deleting`竞争同一`SessionLifecycleGate`
- **THEN**若删除先取得gate则owner-reference提交被拒绝；若上传先在gate内提交reference，则删除随后按pin阻断canonical使用、释放reference并收敛ingest。只有pin、ingest/claim、reference和staging均terminal后Session节点才可隔离，恢复不得向tombstone Session补挂附件

#### Scenario: 相同附件跨 Session 复用 blob 但不共享权限

- **WHEN** 两个 Session 上传相同内容并得到相同 digest
- **THEN** attachment catalog 复用一个物理 blob，但为两个逻辑 attachment/owner reference分别校验权限；删除一个 Session 只释放其 reference，另一个 Session 的读取和 retention 不受影响

### Requirement: Sealed ContextAssemblySnapshot 与 canonical commit 具有明确边界

系统 SHALL 在 Provider dispatch 前通过 `RolloutCheckpointSaver` 持久化 sealed `ContextAssemblySnapshot`；其 plan、canonical/request-only refs、source/hash、tool-set snapshot、`hash_algorithm`、`plan_hash` 和 `request_hash` 不可变。采用缓存保持型 source overlay 时，snapshot 还必须保存 `history_view_revision`、`source_overlay_epoch`、base source revision/reference、按序 delta references、target/materialized revision、materialization reason 和 overlay hash；这些引用必须能在重启后恢复 base→delta 链，而不能依赖重新读取当前文件。`plan_hash` 使用固定 `context-plan-hash:v2` 的 provider-neutral JCS preimage，`request_hash` 使用具体 projector/provider profile 的规范化 request serialization；provider request ID、时间、认证、transport headers 和 retry identity 不得进入 hash。assembly 的 completed、completed_empty、failed、interrupted、unknown outcome 与 canonical item/Turn finalization 必须遵守 JSONL fsync 后 SQLite terminal convergence 的提交边界；detail store 缺失不得让 reader 伪造成功或阻塞已提交 canonical history 的恢复。业务层和 projector 只能消费 Saver 提供的已提交 plan/snapshot，不得直接扫描 RolloutStorage、AppendWriter 或内部 context reader。

本存储合同采用一个显式的 selection source union：`ContextRef.ref_type` 仍只允许 `canonical_item | request_only`，`ContextSelectionEntry.ref` 在非工具选择时恰为一个 `ContextRef`；`selection_kind=tool_set` 时恰为一个独立 `ToolSetRef`，不能把 `tool_set` 加入 `ContextRef` 的枚举。`ToolSetRef` 必须序列化为 `ref_type=tool_set`、`ref_id=tool_set_snapshot_id`、`plan_id`、seal 后非空的 `assembly_id`、非空 `source_revision`、`tool_set_schema`、`tool_set_schema_version`、`tool_policy_version`、逻辑 `content_length`、恰一个 `content_hash`、`protection=public|redacted|protected` 和 `availability=available|unavailable|forbidden|expired`；其 hash/length 覆盖 `{ "tool_set_schema":"tool-set-ref", "tool_set_schema_version":"v1", "tools":<按稳定 tool_id 排序的 schema/config entries>, "tool_policy":<规范 policy>, "tool_policy_version":"v1" }` 的 `sha256:jcs:v1` RFC 8785 JCS UTF-8 bytes，protected registry 保留内部 hash。manifest 的 `tool_policy` 和各版本字段必须与 ToolSetRef 逐字段绑定，不能由 projector 临时补入。unsealed plan 的 tool-set registry 允许 `assembly_id=NULL`，但不得生成 selection/`plan_ordinal`；seal 后 `ToolSetRef` 必须解析到同一 `(session_id, thread_id, plan_id)` 的 `tool_set_snapshot_id`，并以 `assembly_id` 绑定到 `assembly_item_refs`。工具选择与其它 source 共用 assembly 内唯一 `plan_ordinal`，但不占 item catalog、Turn item sequence 或 `contribution_ordinal`。

optional omission 的 selection `ref` 仍使用 `ref_type=tool_set`/`ref_id=tool_set_snapshot_id` 的 tagged identity stub，不要求在不可用时把完整 ToolSetRef manifest 字段补入 entry；它只保留 `plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 identity。只有 `included=true` 的 ToolSetRef 才必须完成 manifest 全字段校验并进入 hash preimage；omitted tool-set 不进入 Provider tools，也不以当前 registry 或空 tools 替代。

`plan_hash` 的 provider-neutral preimage 必须显式含有选自 `ContextRequestPlan.tool_set_refs[]` 的 `tool_set_refs[]`，按 `tool_set_snapshot_id` 做 registry 稳定排序，并在同一 preimage 中保留按 `plan_ordinal` 排序的 selection。每个 included 条目必须含 snapshot id、source revision、manifest `content_length`、恰一个 `content_hash`、`tool_set_schema`/`tool_set_schema_version`、`tool_policy_version` 和同一 manifest 的规范 `tool_policy`；manifest 内 tools 按稳定 `tool_id` 排序。工具 schema/config、policy、schema version 或 source revision 的变化必须改变 `plan_hash`，不得只比较笼统 logical tool contract hash。seal/restore/replay 必须先校验 included ToolSetRef 与 manifest，再比较 `plan_hash`；不可解析、不可用或逐字段不一致分别报告 `source-mismatch`/`detail-unavailable`，preimage 不一致报告 `plan-hash-mismatch`，均不得 fallback 到当前 registry 或空 tools。相同 manifest 在不同 provider 间保持相同 `plan_hash`，provider-specific tool encoding 只可导致不同 `request_hash`；同 projector 的 request hash mismatch 禁止 exact replay。

`ToolSetRef`的上述schema/config entries仅允许Provider可见直接工具和始终存在、名称/schema/description固定的`invoke_extension_tool`信封。扩展目标目录的name/schema/policy、MCP连接generation不得写入ToolSetRef，也不得因内层目录revision变化产生新的ToolSetRef。每次model-call assembly另存typed `ExtensionCatalogBindingRef`：精确owner/thread、Execution/ModelCall activation snapshot、catalog semantic revision/hash、对应MCP指引source revision、effective boundary、稳定target/schema identity、server generation验证ref、policy revision与受保护catalog snapshot/detail ref。该ref与同一次seal的指引选择原子绑定，不能只更新一侧；物理endpoint、credential与瞬时连接实例ID不得作为业务identity或模型可见字段。connection generation lease在实际调用期间由MCP owner持有，冷历史仅保留验证ref而不恢复连接。

ExtensionCatalogBindingRef使用独立的`extension_dispatch_binding_hash`（版本化RFC8785语义preimage）校验catalog revision、target/schema集合及绑定的指引revision，不混进既有`context-plan-hash:v2`或仅描述Provider wire bytes的`request_hash`。内层目录变化可产生新的assembly binding和独立hash，但只要Provider可见ToolSetRef和已提交wire context前缀未变化，就不能制造`toolset_changed` epoch；若指引delta确实进入plan，其selection自然改变既有plan hash。restore/retry必须同时校验plan/request hash及独立dispatch binding hash，缺失报`extension-catalog-unavailable`并拒绝dispatch/继续调用，不使用当前目录、当前同名target或空目录回退。fork生成target-local operational ref并保留source lineage/已sealed历史，不复制活连接句柄。request-only binding不计入Turn item统计；指引若实际作为canonical ambient item提交，则只按其唯一canonical identity计数规则处理，不从binding或DOM推断。

#### Scenario: 扩展目录变化只改变独立dispatch binding

- **WHEN** 两份相邻assembly的Provider可见直接工具/信封及已提交wire前缀相同，但后一份按激活边界选中了新的MCP目录
- **THEN** 两份ToolSetRef manifest和prefix epoch保持相同，后一份另存新的ExtensionCatalogBindingRef与`extension_dispatch_binding_hash`；若MCP指引产生delta，它只追加为独立user-role source item；restore分别校验当时的binding，不按当前目录回填

#### Scenario: assembly 写入失败

- **WHEN** sealed assembly 或必要 provenance metadata 无法持久化
- **THEN** 系统不发起 Provider request，并返回明确的 assembly persistence error

#### Scenario: Provider 调用后进程退出

- **WHEN** Provider 已收到 request，但没有已提交完整 canonical carrier 与精确 pending-tools checkpoint 的同 owner 完成证明
- **THEN** 重启将 model call/assembly 标记为 `unknown`/`execution_lost`，未提交 JSONL item 不对 reader 可见

#### Scenario: 跨 provider hash 可比

- **WHEN** 同一个 committed plan 使用相同的 active selection 和 ToolSetRef manifest 被不同 provider projector 编译
- **THEN** assembly 保持相同的 `plan_hash`，其 preimage 包含完整 `tool_set_refs[]` manifest identity；各自生成 projector/provider-specific `request_hash`，同一 projector 的 hash mismatch 必须阻止 exact replay

#### Scenario: 工具 manifest 变化阻止旧 plan replay

- **WHEN** tool schema/config、tool policy、manifest schema version 或工具 registry source revision 变化
- **THEN** 必须创建新的 ToolSetRef/plan 并得到新的 `plan_hash`；旧 plan restore/replay 返回 `plan-hash-mismatch` 或 `source-mismatch`，不得用当前 registry、空 tools 或旧 logical tool contract hash 继续 dispatch

#### Scenario: tool_set contribution 不代表工具定义

- **WHEN** storage 收到 `ContextContribution.contribution_kind=tool_set`
- **THEN** 返回 `contribution-kind-unsupported`，或在 legacy ingress 中仅写入 quarantine/report；不得产生 ToolSetSnapshot、ToolSetRef 或 Provider tool definitions

#### Scenario: storage hash 使用 RFC 8785

- **WHEN** storage 为 item content、ContextRequestPlan、request 或 migration identity 生成 `sha256:jcs:v1`
- **THEN** 使用真正 RFC 8785 JCS 的 UTF-16 code-unit key ordering、ECMAScript/IEEE-754 有限数字规范化和无空白 UTF-8 bytes；`json.dumps(sort_keys=True)`、语言默认 key ordering、NaN/Infinity 或 code-point-only ordering 不能作为实现
- **AND** content/plan/idempotency preimage schema/version 与跨语言 golden vectors 固定，provider request ID、时间、认证、transport header 和 retry identity 排除

#### Scenario: required detail 是 dispatch 硬门槛

- **WHEN** `required_detail=true` 但 detail 为 NULL、缺失、不可读、保护级别不满足，或 source revision/content hash/length 不匹配
- **THEN** sealed-before-dispatch 提交失败并返回 `detail-unavailable`、`source-mismatch` 或 security error，不产生 ready assembly，Provider request 不发出；只有 optional detail 才能带显式 loss seal

#### Scenario: optional omission 不占用正文完整性绑定

- **WHEN** optional request-only detail、canonical item 或 ToolSetRef 在 seal 时不可用、不可读、过期或完整性校验失败
- **THEN** storage 可以提交 `included=false` 的 sealed selection entry，并保留 tagged ref、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 source identity；`detail_ref`、`content_length`、hash token、`contribution_ordinal` 不分配或写为 NULL，已知 metadata 必须与对应 manifest 一致
- **AND** restore/history 保留 omission metadata 但不读取当前 source、canonical 正文或 detail；Provider 不生成对应的 message/tool definition/空 tools；required source 的同一情况必须拒绝 seal/dispatch

#### Scenario: snapshot 恢复校验正文和顺序

- **WHEN** 重启后从 `assembly_item_refs` 恢复 canonical/request-only refs 与 source overlay
- **THEN** Saver 对 `included=true` 的 ref/contribution 重新校验 `source_revision`、逻辑 `content_length` 和恰一个 `content_hash`，request-only 再校验同 assembly 的 `detail_ref`，contribution-backed entry 再校验 `contribution_ordinal`，并按持久的 `selection.plan_ordinal` 恢复顺序；`included=false` 的 optional omission 只校验 tagged ref、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和已保存的 source identity（若有则必须与 manifest 一致），不分配/读取 detail、正文 hash/length 或 contribution ordinal。覆盖、缺失或错误 source 返回 `source-mismatch`/`detail-unavailable`，不得静默 loss 或发送错误正文

### Requirement: resource activation snapshot 与虚拟来源必须随 assembly 持久化

itemized storage SHALL 只持久化 ResourceActivationCoordinator 已冻结的 `ResourceActivationSnapshotRef`、逐资源 `ResourceProvenanceRef` 及其 assembly selection binding，不拥有 ResourceRegistry、monitor task、loader 或 activation policy。`ResourceActivationSnapshotRef` 至少包含 `activation_snapshot_id`、`snapshot_kind=execution|model_call`、model-call snapshot必需的`parent_execution_snapshot_id`、`activation_policy_revision`、`activation_policy_hash`、完整规范化`activation_policy={default_boundary, overrides}`、`registry_generation`、`owner_session_id`、`owner_thread_id`、必填`execution_id`、按admission kind可空的`turn_id`、按kind可空的`model_call_id`、`captured_at`、`bindings_hash`和`activation_provenance_hash`；`ResourceProvenanceRef` 至少包含稳定`resource_id`、模型可见`display_uri`、`resource_kind`、`owner_scope`、`facet`、`revision`、`effective_boundary=turn|model_call`、`captured_registry_generation`、`content_length`、恰一个`content_hash`、`snapshot_ref|detail_ref`、`availability`和snapshot内唯一`activation_ordinal`。两类记录必须以 `(session_id, thread_id)` 为owner，并通过外键或等价完整性约束绑定精确plan/assembly。`bindings_hash`只纳入按activation ordinal排列的实际resource选择语义，`plan_hash`使用bindings hash；独立`activation_provenance_hash`纳入snapshot kind、`parent_kind + parent_bindings_hash`关系描述、policy revision/hash、Registry generation及binding effective boundary/captured generation。具体`parent_execution_snapshot_id`另由外键和turn-bound binding逐字节复用校验保护；运行identity/captured_at不进入内容hash，policy/generation不进入bindings/plan hash。resource identity、revision、facet、availability、length或hash变化必须产生新plan hash，而仅effective boundary/policy/generation变化只改变provenance hash，不得造成相同wire选择的plan hash抖动。

execution/Turn 的合法性、默认冻结复用与 internal NULL 语义只引用本 change 的「Context plan 必须绑定精确资源激活快照和安全来源」合同；storage 以 owner/execution FK及可空Turn relation验证 root/model-call parent，唯一持久冻结记录保证同用户Turn的原policy/binding可恢复，不另立第二冻结owner。旧实验activation开发数据按用户授权清除，fresh库显式创建当前schema；正常运行遇到缺失/陈旧marker明确拒绝，不隐式升级/清库，也不新增旧activation版本读写或迁移兼容。独立v1显式importer不受本裁定撤销。

SQLite SHALL 保存 activation snapshot catalog、resource binding manifest、assembly binding 和按 owner/execution/非空turn/model-call/revision 的定点索引；资源正文仍按 protection policy 保存到 `ContextPlanDetailStore` 或受保护 snapshot body store，不能复制进 SQLite catalog。`display_uri` 只能使用经 统一资源寻址 校验的 `boxteam://` URI，且不得含物理绝对路径、provider locator、网络 credential、memory key、query、fragment 或 userinfo；内部 `provider_locator` 只能留在 ResourceRegistry/provider 私有存储，禁止进入 rollout JSONL、SQLite history manifest、trace、hash preimage或客户端响应。虚拟 URI 是安全展示和解析入口，不是资源 identity、权限 token、dedupe key 或幂等键；所有 lookup 仍以稳定 `resource_id + revision` 和 owner/capability 为准。

seal 必须使用 activation coordinator 已冻结且仍完整的内存 snapshot，在 `assembly_sealed` metadata-only commit 中原子写入 resource snapshot/ref manifest 与 selection；Saver、storage、history 和 projector 均不得在 seal、restore、replay、rewind、compaction 或 fork 时重新读取当前文件、网络资源、内存源，或根据 `display_uri` 解析“最新版”内容。execution snapshot固定policy和turn-bound binding；存在model-call-bound kind时，后续model call绑定parent-linked新snapshot，只替换对应binding并复用parent其余binding。任何已sealed assembly及其前缀字节保持不变，policy热更新只影响后续新用户Turn或独立internal execution，同Turn retry/resume复用原冻结事实。跨 owner fork 保留历史 activation snapshot/ref local ID和原封存事实，由目标本地 snapshot binding 校验；新的非历史 materialization 仍创建目标当前身份。不得在线读取 source owner 或按 URI 重解最新版。

#### Scenario: 默认 Turn 内资源变化不改写后续 model call

- **WHEN** 一个 Turn 已冻结只含turn-bound binding的ExecutionResourceSnapshot，随后 ResourceRegistry 发布 Skill、AGENTS 或团队状态的新 revision，且该 Turn 继续执行工具循环并产生后续 model call
- **THEN** 该 Turn 的每个 assembly 都引用原 `activation_snapshot_id`、resource revisions 和相同 bindings hash；新 revision 只影响下一个取得 active execution slot 的 Turn，任何已提交 prefix、selection、plan hash 和 provider request bytes 都不被改写

#### Scenario: model_call 边界从内存快照激活新 revision

- **WHEN** workspace 配置显式选择 `model_call`，Registry 已在两个 model call 之间发布新 semantic snapshot
- **THEN** activation coordinator 为后一个 model call 冻结引用原execution snapshot的新`activation_snapshot_id`，逐字节复用turn-bound binding并只更新model-call-bound binding，再由Saver seal；该路径不执行`stat`、目录扫描、文件读取或网络拉取，旧assembly继续解析旧snapshot

#### Scenario: policy 热更新不改变已开始 Turn

- **WHEN** execution snapshot已保存policy revision P1，Turn内配置发布P2并改变某resource kind的effective boundary
- **THEN** 该Turn后续snapshot/assembly仍绑定P1，P2只由下一个Turn使用；storage不得重写parent、binding boundary、plan hash或已提交Provider bytes

#### Scenario: 重启和历史恢复不读取当前资源

- **WHEN** sealed assembly 引用的 Skill 文件已移动、删除或更新，或相同 `display_uri` 当前解析为另一 provider locator
- **THEN** restore/history 只按已持久化的 `resource_id + revision + snapshot_ref|detail_ref` 校验并恢复原内容或明确报告 `detail-unavailable`；不得读取当前 URI、用新 revision 替换旧正文或通过物理路径猜测来源

#### Scenario: 私有 locator 不得进入持久历史

- **WHEN** provider 使用绝对文件路径、认证网络 URL 或内存 key 取得资源
- **THEN** storage 只保存稳定 resource identity、安全 `boxteam://` display URI、revision、hash/length、availability 和受保护正文引用；对 SQLite、JSONL、trace 和客户端响应的结构检查均不得发现原 locator 或 credential

#### Scenario: 旧路径 provenance 被拒绝

- **WHEN** writer、migration 以外的 runtime 或 projector 尝试写入 `/.boxteam/skills/...`、`/.boxteam/bundled-skills/...`、绝对 Skill 路径、`read_file_path` 或其它 path-based provenance 字段
- **THEN** schema validation 明确拒绝该记录且不 seal assembly；系统不提供字段别名、兼容 adapter、fallback lookup 或双写

### Requirement: Context plan selection 和 contribution ordinal 必须持久化

selection source 的逻辑唯一性必须由持久化约束表达：draft ContextRef registry 使用 `UNIQUE(session_id, thread_id, plan_id, ref_type, ref_id)`，其中 canonical item 可跨 plan 查询复用、request-only ref 不得跨 plan 复用；同一 sealed assembly 的 ContextRef selection 使用 `UNIQUE(session_id, thread_id, assembly_id, ref_type, ref_id)`，ToolSetRef 使用独立的 tagged identity 约束，plan registry 另有 `UNIQUE(session_id, thread_id, plan_id, tool_set_snapshot_id)`。`ContextRef` 本身不携带 assembly identity；`assembly_id` 只由 sealed selection/manifest 绑定，request-only 的最终 `detail_ref` 必须解析到同一 `(session_id, thread_id, assembly_id)`。`tool_set` selection 的 `ref_id` 只能解析到该 plan 的 ToolSetSnapshot，不能与 ContextRef 或 contribution 共用同一 typed identity。

`ContextRequestPlan` 与 `ContextAssemblySnapshot` 的 identity/lifecycle 必须一并持久化：创建 plan 时生成 thread-local `plan_id`，初始为 `plan_state=unsealed`、`assembly_id=NULL`、`selection=[]`；未 seal 的 plan 可以保存 registry，但不产生 assembly ref、`ContextSelectionEntry` 或 `plan_ordinal`。Saver 仅在 seal preflight 通过后于同一提交边界分配 thread-local `assembly_id`，生成 selection/ordinal 并持久化 snapshot；成功 seal 后 snapshot 的 `plan_id` 与 `assembly_id` 一对一且不可变，失败则不留下可 dispatch 的 assembly。plan 创建幂等使用 `(session_id, thread_id, plan_creation_idempotency_key)`，seal 幂等使用 `(session_id, thread_id, plan_id, seal_idempotency_key)`，两者冲突分别报告 `plan-idempotency-conflict`/`assembly-idempotency-conflict`。`ContextSelectionEntry` 只能存在于已分配的 assembly scope；无 source 的空 selection 可以 seal，但必须带该 assembly identity。

`ContextAssemblySnapshot` MUST 持久化 Saver 冻结的 `selection` 列表。每个 selection entry 都有唯一的 `plan_ordinal`、`selection_kind`、序列化的 tagged-union `ref`、visibility、protection、availability、`included`，以及 omission 时的 `omission_reason`/`loss[]`；只有 `included=true` 的 entry 才强制 source revision、逻辑 `content_length` 和恰一个 `content_hash`。`canonical_history|request_only|overlay_base|overlay_delta` 必须绑定一个 ContextRef，`tool_set` 必须绑定一个 ToolSetRef。ContextRef 的 `session_id`/`thread_id`/`ref_id` 在 draft 只按 `(session_id,thread_id,item_id)` 或 `(session_id,thread_id,plan_id,ref_id)` registry 解析，不携带 assembly binding；只有 sealed entry/`ref_manifest` 绑定 `assembly_id`，只有 included request-only entry 才能拥有解析为同一 `(session_id,thread_id,assembly_id)` 的最终 `detail_ref`。每个 included 且被 selection 绑定的 `ContextContribution` 都有 `source_revision`、`content_length`、同一 hash-token 规则及 assembly binding 内独立、单调、不可重写的 `contribution_ordinal`，unsealed registry contribution 和 omitted entry 不预分配该 ordinal，overlay 还必须保存 base/delta role 与 source overlay epoch；ToolSetRef 不绑定 contribution ordinal。SQLite `assembly_item_refs` 至少保存 `plan_id`、`assembly_id`、`plan_ordinal`、ref identity、上述 integrity manifest 和 contribution/tool-set binding，并对 `UNIQUE(session_id, thread_id, assembly_id, plan_ordinal)`、`UNIQUE(session_id, thread_id, assembly_id, contribution_id)` 和 `UNIQUE(session_id, thread_id, assembly_id, contribution_ordinal)` 建立唯一约束。每条 included selection 的 source revision/length/hash token、visibility/protection/availability、base/delta role 和 overlay epoch 必须逐字段等于对应 ContextRef/ToolSetRef/contribution/detail manifest；omitted entry 的已知 identity metadata 若存在也必须一致，但 null/未分配的 detail、正文 hash/length 和 contribution ordinal 不得被补造。缺项、重复 ref/contribution/tool-set snapshot、union 类型与 selection_kind 不匹配或顺序/绑定不一致报告 `plan-order-integrity`，canonical/tool-set source mismatch 报告 `source-mismatch`，request-only detail mismatch/unavailable 报告 `detail-unavailable`。`selection_kind=tool_set` 的正文只能来自同一 plan/assembly 的 ToolSetSnapshot schema/config manifest 及其 hash，不从 item catalog、canonical ContextRef 或普通 contribution 借用。restore、LangChain、native Provider 和 Web history 只能消费这份 selection；Provider 将 included ToolSetRef 投影到 tools/tool-config，history 只保留策略允许的 metadata，不得按 `created_at`、`contribution_id`、物理邻接或 dict insertion order 排序，也不得把 request-only refs 或 tool definitions 无条件 prepend 到 canonical messages；optional omitted canonical/request-only/tool_set entry 只保留 omission/loss metadata，分别跳过正文、detail 和工具定义，不从当前 source/registry 回退。selection/ordinal 冲突必须报告 plan-order-integrity error。

矩阵明细见 `add-itemized-rollout-context` 的 requirement「selection_kind 与 ref_type 必须使用唯一兼容矩阵」；本节不再复述。存储仍 SHALL 在 source lookup 之前按该权威矩阵校验，矩阵外组合返回 `plan-order-integrity`；omitted entry 不解析 source 时正文字段和 contribution/detail binding 可 NULL/未分配，但已知 role/identity 必须与已提交 manifest 一致，restore/history 对 omitted canonical/request-only/tool_set 分别跳过正文、detail、工具定义，overlay omission 不应用 base/delta，required omission 拒绝 seal/dispatch。

`assembly_item_refs.contribution_id` 是 selection 到 contribution manifest 的唯一映射列：included 且 contribution-backed 的 request-only/overlay entry 必须保存非空 `contribution_id`，它必须在同一 `(session_id, thread_id, plan_id)` registry 唯一解析一个 `ContextContribution`；字段归属见权威矩阵，本节不复述。`ContextRef.ref_id` 是 canonical `item_id` 或 request-only 的稳定逻辑 alias（普通 request-only 通常等于其 `contribution_id`，亦可为 producer 声明 source alias；overlay 为 overlay alias），但该列仍是唯一显式映射：restore、history 和两类 request projector 必须通过该列读取 manifest/body，禁止通过 detail_ref、hash 或 ordinal 反推 contribution，也不得把 overlay alias 当成 contribution_id。该列与 `UNIQUE(session_id, thread_id, assembly_id, contribution_id)`（NULL 不参与重复约束）共同防止多条 selection 隐式指向同一 contribution。

普通 `ContextContribution` 的持久化 `request_only` 字段必须存在且恒为 `true`，它是 contribution 本体语义，不是 `ContextRef`/selection 的 discriminator；后者只能使用 `ref_type=request_only`。其 `contribution_kind` 的闭合集合只有 `prompt | overlay_base | overlay_delta | notice`，其 `content_hash` 必须校验 `sha256:jcs:v1(JCS({"contribution_kind": <kind>, "body": <typed body> }))`；`tool_set` 不是 contribution kind，Provider tool definitions 只能由 ToolSetSnapshot/ToolSetRef manifest 提供。v2 输入 `contribution_kind=tool_set` 必须返回 `contribution-kind-unsupported`；只有一次性 `legacy_import_v1_to_v2` migration reader 可以保留原始记录到 migration report/quarantine，且不得将其写入 contribution registry、转换为 ToolSetRef 或用于 Provider tools，正常 runtime 不开启 legacy adapter。所有 included 正文统一保存并校验内部 content_hash；敏感正文及内部 hash 从默认外部投影隐藏，不使用 thread HMAC、redacted_stable_digest 或密钥。`content_length` 必须对应该 typed body 的 canonical encoded bytes，不能由 ref 字符串、JSONL line length 或 wire message 长度替代。`contribution_ordinal` 是 assembly binding：同一 contribution 进入另一 assembly 时重新绑定新的 ordinal，重启只能从已提交 `assembly_item_refs` 恢复，不能从 `created_at`/ID 重新排序。

#### Scenario: sealed selection 与 contribution ordinal 可恢复

- **WHEN** Saver 为一个已通过 preflight 的 plan 持久化 canonical/request-only/overlay/tool-set selection
- **THEN** storage 将其 `plan_ordinal`、适用的 `contribution_id`/`contribution_ordinal`、manifest 和 assembly identity 原子写入并可在重启后恢复；不适用字段保持 NULL

### Requirement: selection_kind 与 ref_type 的兼容矩阵必须唯一登记且被存储消费

本 requirement MUST NOT 登记第二份 `selection_kind` 与 tagged-union `ref` 兼容矩阵；唯一权威登记是 `add-itemized-rollout-context` 的 `specs/itemized-rollout-context/spec.md` 的 requirement「selection_kind 与 ref_type 必须使用唯一兼容矩阵」。存储 SHALL 在 source lookup、detail 解析和 restore/projector 之前按该权威矩阵校验 `ContextSelectionEntry.selection_kind` 与 tagged-union `ref`；included 与 omitted entry 都必须满足同一 tag/type 关系。矩阵外组合必须返回 `plan-order-integrity`，不能由存储根据 payload、wire role、`ref_id` 或 registry 猜测生命周期。

`ContextRef.ref_id` 仍是 canonical `item_id` 或 request-only 的稳定逻辑 alias（普通 request-only 通常等于其 `contribution_id`，亦可为 producer 声明 source alias；overlay 为 overlay alias）；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref`/source manifest 读取正文并校验 source revision、length/hash；included 且 contribution-backed 的 request-only/overlay 才必须用非空 `assembly_item_refs.contribution_id` + `contribution_ordinal` 唯一读取同一 `(session_id, thread_id, plan_id)` 的 contribution manifest/body，再校验 detail、source revision、length/hash 与 ordinal，overlay 本身必须 contribution-backed。canonical/tool_set entry（包括 omitted）的 contribution_id/ordinal/detail_ref 必须 NULL；只有 omitted request-only/overlay entry 可保留已有且与同一 manifest 一致的 contribution_id，不得新分配 contribution_id/ordinal 或读取正文/detail，没有既有映射则为 NULL。omitted entry 仍必须保留矩阵规定的 tag/type，正文和 contribution/detail binding 可 NULL/未分配，已知 metadata 必须与 manifest 一致；required omission/detail failure 拒绝 seal/dispatch。restore、history 与 Provider/LangChain projector 对 omitted entry 只保留 omission/loss，并分别跳过正文、detail、overlay 应用或 tools。

#### Scenario: 重启保持 base 到 delta 顺序

- **WHEN** 一个 assembly 含 canonical history、request-only contribution 和 A base→B delta，进程在 dispatch 前后重启
- **THEN** 恢复的 `plan_ordinal`/`contribution_ordinal` 与原 snapshot 完全一致，三种 projector 产生相同的 source selection/order；history view 改变只重新选择 plan，不重写既有 ordinal

#### Scenario: detail 路径隔离与默认正文脱敏

- **WHEN** detail store 写入或读取敏感 detail，或请求路径的任一父组件是 symlink/realpath 越界
- **THEN** detail owner 只保存一份精确正文并校验 hash/length/source/typed owner，默认 history/API 脱敏；root/write/read 都拒绝父级 symlink 和 containment 越界，availability/loss 显式写入 assembly，不生成独立密文或密钥

### Requirement: 缓存保持型 overlay 的引用和物化必须可恢复

存储 SHALL将缓存保持型source overlay的base、delta、supersedes/materializes relation、`source_overlay_epoch`和物化结果作为可校验的assembly/overlay引用保存，并与`history_view_revision`、`prefix_epoch`/reason及唯一`PendingPrefixEpochTransition`/applied owner分开。同一prefix epoch内的replay、fork source read、checkpoint restore或普通source edit必须复用旧wire bytes并只追加；首次组装、实际compaction、rewind重建和ToolSet hard rebase是source/root重建边界，`runtime_notice_consumed` 则只在必须从 wire refs 移除已处理 internal notice 时复用同一 transition owner 并在首个 assembly 成功 seal 时应用。后者不触发 overlay materialization、source restore 或 fork lineage；相关 restore/fork/copy consumers 必须显式按 reason 过滤。acceptance freeze、notice 求差与 assembly seal 通过同一 owner 和现有 storage commit 协调，不新增第二 transition/applied ledger。rewind不得把cutoff后的delta从lineage直接复活：tracked registration从冻结Registry activation snapshot取得的published完整revision、首个新epochassembly和source transition消费必须同事务提交，snapshot/untracked不恢复且请求路径不读源。notice consumption 与 rewind/compaction 同时待应用时同一 transition owner须保留两类原因事实并原子推进；seal 前失败不消费 notice，冷恢复复用 acceptance 冻结集合。已物化的新完整source revision只能在合法source/root重建边界的context view/assembly中成为active；旧base/delta的JSONL item或request-only detail reference保持不可变并可按retention policy查询，不得物理覆盖。source diff如果需要跨checkpoint继续生效，必须有对应的canonical ambient`runtime_notice` item catalog/关系；request-only base/delta只能由sealed assembly/detail reference恢复。

overlay 的 `base_ref` 和每个 `delta_ref` 若 `included=true`，必须在 `assembly_item_refs` 中保存完整 manifest：`source_revision`、`content_length`、`content_hash`、`source_overlay_epoch`、`base_delta_role`、`contribution_id`、`contribution_ordinal`、`plan_ordinal`；`contribution_id` 必须唯一解析同一 plan 的 `ContextContribution`，不得从 base/delta ref 或 ordinal 猜测；delta 还保存 `from_revision`、`to_revision`、diff algorithm/version 和 `diff_hash`。若 optional overlay entry 为 `included=false`，`assembly_item_refs` 只保存 tagged ref、`plan_ordinal`、omission/loss 和可得 identity，不写 detail、正文 length/hash 或 `contribution_ordinal`，该 delta 不得被恢复为可应用正文。`content_length` 不得复用 JSONL line length，`diff_hash` 不得替代 included delta source body hash。恢复只允许使用该 manifest 与已提交 detail/canonical source；included entry 的任何字段缺失、chain 不相接或正文校验失败都必须返回 `source-mismatch`/`detail-unavailable`，omitted entry 只恢复 omission/loss metadata，不能仅凭 ref 字符串重建。

#### Scenario: 重启恢复 base 与增量

- **WHEN** 进程在 source A 已 sealed、A→B delta 已追加但尚未 compaction 时退出
- **THEN** Saver 通过已提交 snapshot、item catalog 和 relation 恢复 A base 与 A→B delta 的顺序，不读取当前 source 猜测 B，也不把 delta 当作普通 Turn member

#### Scenario: overlay 物化后 active view 不重复应用旧 overlay

- **WHEN** 首次组装、实际compaction、rewind重建或ToolSet hard rebase的合法新epoch判定必须将tracked source B物化为新的完整revision
- **THEN** 新active view/assembly只引用B的完整revision和新的`source_overlay_epoch`，旧A/A→B引用仍可审计但不再被projector发送；若合法边界选择复用，则精确引用A/A→B。其它source/cache/detail失效只返回错误，不得触发物化

#### Scenario: overlay 提交失败保持旧视图

- **WHEN** delta item、assembly metadata 或新的 materialized view 在提交前失败
- **THEN** 原 active view 和上一 sealed assembly 保持可读，新 overlay 不以半成品状态可见，恢复不得根据 JSONL 尾部自行启用它

#### Scenario: rewind 后恢复 tracked 完整 revision

- **WHEN** rewind使保存A→B delta的历史item不再属于active history view，且目标checkpoint仍保留tracked registration
- **THEN** 下一次model-call preparation从冻结Registry activation snapshot取得published完整revision B，并把它与首个新history-view/prefix-epoch assembly和pending transition消费原子提交；旧A→B不跨cutoff重新注入，snapshot/untracked也不自动恢复，失败不产生半assembly且不读取当前源

### Requirement: Fork 存储归属与历史执行事实必须分离

存储 SHALL 保留复制历史的 local ID、原正文、时间、owner scope 与 plan/request/content hash，以目标 catalog/资源根和冻结的 namespace binding 确定当前读取归属。完整 rollout 按一致磁盘快照保留原字节和 committed offset；view 前缀按所选成员及必需正文依赖读取，布局变化只重建必要目标 offset 索引，不重写记录。新请求属于目标当前身份，历史请求仍属于当时执行事实。该合同不得通过跳过 typed owner、revision、hash、length 校验实现。

fork 的范围、历史 local ID 保留、当前存储归属与历史执行 scope 分离、目标 URI、资源处理及不可恢复历史策略 MUST 具名消费 `checkpoint-context-branching/spec.md` 的 fork requirements；本 capability 不另设逐实体 remap、全量 hash 重算或仅 main-thread 的复制合同。

正常 fork 只接受 v2；v1 source 明确返回 `v1_migration_required`，先经显式一次性 importer 完成转换，不把 legacy reader 挂到目标。新目标 main/child 的当前执行状态为空，导入历史不可 resume；后续新输入或 replay_as_new_turn 新建目标 Turn/root/acceptance/execution。历史 local acceptance key 只用于历史核验，不接受其作为新目标请求准入。

#### Scenario: 磁盘快照不改写正文和业务 hash

- **WHEN** 完整复制 source thread 的 JSONL、SQLite 和已封存 detail/request snapshot
- **THEN** 历史原字节、local ID 和业务 hash 保持；读取按目标本地归属 binding 校验，不逐项分配新 ID、改写 sealed scope 或重新计算全部请求

#### Scenario: view 前缀的索引落在目标文件

- **WHEN** view_prefix_fork 只物化所选前缀与 view 外必要依赖
- **THEN** 原记录字节和 local ID 保持，目标索引记录自身文件的正确 offset；源 offset 不成为源文件读取入口，依赖不自动增加可见成员

#### Scenario: 导入历史与当前运行状态分离

- **WHEN** target 读取带原 active/interrupted 状态的 imported history，随后请求 resume 或提交新输入
- **THEN** 历史状态如实保留，resume 返回 turn_not_resumable；新输入正常创建新目标执行，不恢复来源 pending tool、Job 或准入

### Requirement: 集成验收复用统一模型协议且保留真实传输证明

普通业务集成和 Web 测试 MUST 复用现有 `model_stream` 的响应资产、协议编码及请求匹配，迁移完整调用链后删除各自重复的 SSE 实现。专门验证真实 HTTP、SDK 或同步/异步调用的测试 MAY 保留本地 HTTP 服务，但 MUST 共享相同资产和编码，MUST NOT 因异步 transport 回放已通过而删除同步或真实网络验收。

查询、历史投影和展示的局部测试 MAY 手工准备数据，但 MUST 限定其证明范围。执行、fork、恢复的完整验收 MUST 经相应产品入口产生关键业务状态；故障注入可以中断真实 durable 边界，MUST NOT 直接写入本应由产品生成的准入、封存、复制发布或恢复结果。验收记录 MUST 明确产品入口、模型替身与真实 HTTP/SDK 边界。

#### Scenario: 普通业务场景不维护独立 SSE 模型

- **WHEN** 普通业务集成或 Web 测试需要确定性模型响应
- **THEN** 使用现有 `model_stream` 资产、协议编码与请求匹配；同一响应不在测试文件中再实现一套 SSE 拼装

#### Scenario: 专用网络与同步调用验证保持独立证明

- **WHEN** 测试针对真实 HTTP、SDK 或同步/异步调用边界
- **THEN** 可以保留本地 HTTP 服务并复用共享响应定义，分别记录所经过的传输和调用方式；异步回放通过不能代替同步或网络通过

#### Scenario: 完整业务验收由产品创建关键状态

- **WHEN** 执行、fork 或恢复的验收声明完整链路通过
- **THEN** 准入、封存、复制发布与恢复等关键状态由相应产品入口生成；直接准备历史的测试仅证明其声明的局部查询或投影行为

### Requirement: Runtime notice 的生成结果 payload 闭合且完整参与内容哈希

持久化的 `generated_session_result` MUST 作为 `semantic_kind=runtime_notice`、`payload_kind=structured_content` 的 canonical item 写入既有 v2 envelope。其闭合 `RuntimeNoticePayload` 只包含既有私有模型输入 `prompt` 与 `display_part={kind: generated_session_result, text, provenance}`；`prompt` 可以包含完成 report-back 所需的 branch result，`display_part` 只包含 `kind`、安全固定文案 `text` 和 typed `provenance`，后者只允许可得的 `source_session_id`、`source_thread_id`、`delegation_id`、`source_execution_id`、`source_item_id`。branch 正文 MUST NOT 出现在 `display_part.text`、`provenance`、metadata、extensions 或 history/Web 展示字段。payload 不得扩展任意 key，不得把 display 内容复制到顶层、`metadata` 或 `extensions`，也不得保存 run/generator/control ID。

canonical item 的 `content_hash` MUST 继续按既有 `{payload_kind, payload}` JCS/SHA-256 preimage 计算，并完整覆盖私有 prompt（包括其中的 branch result）、display part 与 typed provenance。`ExecutionAdmission` 的 admission `payload_hash` 独立覆盖 `admit_internal_execution` request/preimage；`InitialExecutionIntent.binding_preimage_hash` 则覆盖该 intent 冻结的 admission key、owner Session、thread、creation key、initial state、execution binding ID 与 job ID；三者作用域不同、不得互相替代。JSONL canonical payload 保持完整且不可变；Provider request 与 checkpoint message codec 只能从它投影既有 `prompt`，不能把 display part 作为模型输入。schema 不闭合、provenance 字段越界或 hash 不匹配时，写入/读取必须显式失败，不能丢弃 display part 或退化成普通文本。

#### Scenario: display 文案和 prompt 共同受 canonical hash 保护

- **WHEN** `generated_session_result` 被追加到 v2 rollout，或恢复时重新校验其 canonical item
- **THEN** hash preimage 同时包含 `payload_kind=structured_content`、原有 `prompt`、安全 `display_part` 和 typed provenance；任何一部分变化都导致 hash 校验失败
- **AND** JSONL envelope 不新增顶层 display、metadata 或兼容副本字段

#### Scenario: checkpoint 与 Provider 投影不把 display 当模型内容

- **WHEN** Provider request 或 checkpoint message codec 从该 canonical runtime notice 构造模型消息
- **THEN** 只投影 `RuntimeNoticePayload.prompt`，不把 `display_part.text`、provenance ID 或控制字段加入模型消息
