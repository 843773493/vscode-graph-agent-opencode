## MODIFIED Requirements

### Requirement: Fork 支持三种独立模式

系统 SHALL 支持：

1. `context_fork`：复制 source active view 的有效 canonical item/Turn/checkpoint 范围；
2. `history_prefix_fork`：复制会话开始到指定 inclusive/before anchor 的有效 canonical prefix；
3. `full_rollout_copy`：复制 source 的全部 v2 canonical rollout、SQLite checkpoint/view/branch/control state 和 checkpoint channel state；source v1 原始 message-line 仅可作为一次性 migration/rollback audit 输入保留，不进入 target 正常 runtime。

三种模式都必须跨 session 使用 `GlobalEntityRef=(session_id, thread_id, entity_type, local_id)`，并且默认 target owner 为 target Session 的 main thread。系统在 target main thread 为复制范围内的 Turn、root/item、tool invocation/call/attempt、execution、model-call、assembly、view、branch、operation anchor、`accepted_ingress_id` 和 `acceptance_idempotency_key` 分配新的 target-thread-local identity，并以不可变 `fork_entity_mappings`（或等价 provenance）保存 source→target 一对一映射。target active branch/view、root/item offset 和 view-local logical ordinal 只能引用 target thread namespace；source id/sequence/offset 只能作为 lineage/audit 坐标。source overlay epoch、base/delta、canonical ambient item、assembly detail ref 也必须映射为 target-thread-local 引用。三种模式均不得共享 source JSONL/SQLite、canonical payload 或 detail path，或把 source 裸 ID 当作 target canonical identity。若 source 是 v1，fork 只能先由一次性 `legacy_import_v1_to_v2` migration staging 读取并生成 mapping/audit，再按 v2 copy 合同创建 target；正常 fork/history/provider/checkpoint/runtime 不得直接打开 v1。运行态 fork 的结果按模式区分：`context_fork`/`history_prefix_fork` preflight 拒绝并不创建 target；`full_rollout_copy` 可以创建完整历史 target，但把复制的未终态运行态标为不可运行的 `cancelled` 历史状态。

Thread-owned Node调试方案是额外的可移植业务资源，不属于canonical item、checkpoint channel或`full_rollout_copy`的历史方案revision。公开fork仅限同Workspace，按固定矩阵复制source选中thread在source capture时的当前方案正文：`context_fork`只复制当前活动方案（没有则不复制），`history_prefix_fork`不复制方案，因为历史anchor没有相应时点的调试方案快照，`full_rollout_copy`复制全部已保存方案的当前正文；migration-only`materialize_thread_copy`不自动复制调试方案。target方案使用target-local ID与source lineage，但不复制活动指针、进程、Inspector端口、动作审计或历史方案revision；target须显式激活方案。跨Workspace可移植方案export/import需要未来独立授权协议，本change的fork及Gateway federation grant不提供它。

#### Scenario: 默认有效上下文 fork

- **WHEN** 前端创建默认 `context_fork` 且请求没有携带 `turn_id`
- **THEN** 后端通过当前 active view 的 branch lineage 选择最近一个已经完成的 normal Turn，而不是按最大物理 item sequence、JSONL offset 或最新 checkpoint 选择运行中的 Turn
- **AND** target 只物化该已完成 Turn 的 inclusive 边界及其有效 canonical prefix，为所有复制实体建立 source→target mapping，并将源 checkpoint 的非 messages channel、channel versions、`versions_seen`、`pending_sends` 和 pending writes 物化到 target 自己的 SQLite 初始 checkpoint/view
- **AND** target active view 只打开 target-local item/offset，父会话删除不影响 detached target

#### Scenario: context/history prefix fork 拒绝运行中的 Turn

- **WHEN** `context_fork` 或 `history_prefix_fork` 请求明确携带一个尚未完成的 Turn，或者默认解析发现 source 存在 normal Turn 但没有任何已完成 Turn
- **THEN** backend 返回明确 fork 错误，`context_fork`/`history_prefix_fork` 不创建 target session、manifest、retention 或可用 target；不得通过自动取消运行态 Turn、复制 active execution 或复制未终态 assembly 来伪造 fork 成功。只有另行明确选择 `full_rollout_copy` 时，才适用完整历史副本中的 cancelled-historical 规则

#### Scenario: full rollout copy 保留取消态历史

- **WHEN** 用户明确选择 `full_rollout_copy`，且 source 含有未完成 Turn、active execution 或未终态 assembly
- **THEN** target 可以创建为独立的 v2 完整历史副本；对应 target Turn 标记为 `cancelled`、reason=`fork_source_runtime_not_copied`，相关 target execution/model-call/assembly 仅作不可运行历史记录，`final_item_id=NULL`，不得自动 resume 或伪造成功；source 的运行态不因复制而改变

#### Scenario: cancelled historical 不允许 resume

- **WHEN** 调用方对 `full_rollout_copy` 产生的 `cancelled` historical Turn 发起显式 resume
- **THEN** 系统返回 `turn_not_resumable`，不创建新的 execution/model-call，不改变该 Turn 状态；后续真实用户输入必须在 active view 创建新的 target Turn/root，并登记新的 view-local `logical_turn_ordinal`

#### Scenario: cancelled Turn 通过独立操作创建新 Turn

- **WHEN** 调用方明确选择 `replay_as_new_turn`，source 是普通 `cancelled` Turn 或 `full_rollout_copy` 产生的 cancelled historical Turn
- **THEN** active view 可以复制或引用 source history 作为上下文前缀，但系统必须另创建并登记新的 target-local Turn、新的 `user_input` root、acceptance、initial execution 和新的 view-local `logical_turn_ordinal`，并用 `replay_of_turn_id` 保存 lineage；原 Turn 继续保持 `cancelled`，不创建或修改原 execution，source Turn/root 不成为新 Turn 的 root
- **AND** 该操作不是原 Turn 的 `dispatch_replay` 或 `resume_turn`，不会在同一 API 语义中同时返回 `turn_not_resumable` 并创建新 Turn

空 rollout 没有运行中的 Turn，仍允许创建没有 checkpoint 的空 target session；这不属于从运行中 Turn fork。

#### Scenario: 历史前缀 fork

- **WHEN** 用户选择 `history_prefix_fork` 并提供 source item/content-part 的 inclusive 或 before anchor
- **THEN** target 独立拥有从会话开始到该 source anchor 的有效 prefix、对应 checkpoint 的非 messages channel 状态和 target-local anchor/offset；source anchor 只进入 lineage/mapping
- **AND** 如果 anchor 没有可恢复的 source checkpoint，系统返回明确错误，不使用空 channel 静默替代

#### Scenario: 完整 rollout fork

- **WHEN** 用户明确选择 `full_rollout_copy`
- **THEN** target 拥有 source 全部可迁移 canonical rollout、SQLite checkpoint/view/branch 状态和所有 checkpoint channel BLOB 的 target-local 独立副本，并为全部实体建立 mapping；若 source 含 v1 原始 message-line，该原件只能由一次性 `legacy_import_v1_to_v2` migration staging 保留为 audit/quarantine 输入，不挂载为 target reader、history、provider 或 checkpoint runtime 数据
- **AND** source 的 `fork_origins`、`retention_refs` 等 owner 关系表不直接带入 target；target 只由统一 writer 写入自己的 provenance、active view 和 namespace。若 source 是 v1，target 仍固定为 v2；source message id/sequence/offset 只写入 mapping/audit，不成为 target v2 identity/offset。source 中未终态 Turn、active execution 或未终态 assembly 在 target 中只保留不可运行的历史映射，并将 Turn 标为 `cancelled`、reason=`fork_source_runtime_not_copied`，不能自动 resume

#### Scenario: 调试方案复制矩阵不伪装历史回溯

- **WHEN** 同一source thread依次运行`context_fork`、锚定较早history的`history_prefix_fork`及`full_rollout_copy`，且capture时存在一个活动方案和其它已保存方案
- **THEN** 三种target分别只得到活动方案当前正文、无调试方案、以及全部已保存方案当前正文；每个被复制方案获得target-local ID/lineage但target均无活动方案指针、活进程/端口/动作审计，旧history anchor不会捏造历史方案状态

### Requirement: 跨 Session copy 必须冻结 source 后再写 target

`context_fork`、`history_prefix_fork`和`full_rollout_copy` MUST从解析source catalog开始，到全部source node/SQLite read handle关闭并durably提交不可变source snapshot manifest与已校验staging bytes为止，持有source Session的shared `SessionReadGuard`。manifest MUST绑定source lifecycle generation、thread/view/checkpoint上界、artifact清单、逐对象hash和所需detail capability；guard释放后copy只能消费该冻结manifest/bytes，不得重新打开source。source删除先取得exclusive gate并关闭fence时，copy MUST返回`source_session_deletion_pending|source_session_deleted`且不创建可用target；guard先行时删除等待snapshot capture完成，随后可继续删除而不等待target staging/publication。target creation/publication MUST在不持source guard时独立准入；实现 MUST NOT同时持有两个Session gate、一个Session gate与另一个Session的read guard或两个数据库写事务。同一Session内的rewind/replay/compaction沿用owner operation lease/view事务，不建立虚假的跨Session source guard。

当模式选择调试方案时，source debug owner MUST在同一read guard期间冻结已登记的方案manifest、当前revision和逐方案bytes/hash，读取前后验证manifest/revision不漂移，并把选择结果列入同一个`SourceCopySnapshot` artifact清单；不允许fork worker离开guard后重新打开source调试目录或以更新revision补齐。target debug owner MUST在不可见staging里冻结当前有效Workspace debug配置revision/hash，基于同一配置快照逐项校验方案入口、工作目录、全部断点路径与profile/adapter/runtime；发布前复核配置仍为冻结revision，若漂移则整个fork失败并以新operation重试，不在原operation里静默换配置。任一方案缺失、hash漂移或不兼容也使整个fork失败而不发布target，不跳过坏方案或默换默认profile。target-local方案ID/lineage映射及manifest作为fork materialization journal核验的artifact在target Session发布前完成；崩溃仅从冻结snapshot与journal幂等恢复/清理，不建立第二copy协调器或让普通reader看见半个方案。

对应的fork/Session creation journal MUST在capture前预分配唯一`source_snapshot_id`和正常resolver不可见的内部locator。capture MUST在source SQLite固定read snapshot内取得同一revision的view/checkpoint/control rows、`storage_commits`和各JSONL committed end offset，以可验证SQLite snapshot/online backup冻结数据库页，并只复制这些offset以内的JSONL与manifest引用且hash/length/capability校验通过的thread-local immutable detail；冻结revision后的并发append、view切换或terminal convergence不得混入。capture完成时 MUST先在预登记locator原子发布`SourceCopySnapshot(state=captured)`，绑定operation/preimage hash、source lifecycle generation、database snapshot hash、逐文件committed offset/length/hash、view/checkpoint revision、detail manifest hash和attachment claim manifest hash并完成目录durability barrier，释放guard后才把target operation CAS为`source_captured`。恢复只可定点接受完整且hash一致的captured snapshot；prepared/partial或冲突snapshot MUST使原operation abort并清理，不得扫盘、读取当前source补齐或换成更新revision。

workspace attachment blob MUST NOT复制进`SourceCopySnapshot`或Session/thread staging。source SQLite snapshot只冻结logical attachment/variant identity、digest、length、availability、source owner/item ref与attachment catalog revision，并 MUST以journal绑定的IdentifierFactory为每个source attachment item/ref预分配target-local item/attachment identity与mapping到source snapshot staging manifest，不能复用source identity或等待guard释放后再决定。关闭source SQLite read transaction后且释放source guard前，copy MUST以单个workspace attachment catalog事务为每个需要保持available的target-local attachment ref按`(copy_operation_id,source_attachment_ref,target_owner_ref)` create-or-get `ForkAttachmentClaim(state=preparing)`，绑定`source_snapshot_id`、target Session/thread/item、digest/length、受控blob locator和operation preimage，并原子验证source owner ref、blob未tombstone及正文digest/length；claim提交后阻止GC。全部claim成功后才将有序claim ID清单/hash写入captured marker；中途崩溃只可按精确copy operation ID查询并释放本operation claim，不按digest/locator扫描。source已unavailable且不参与可运行request的历史ref MAY显式映射unavailable，active view/required replay需要正文而claim失败时copy MUST失败。

target可见性发布前，worker MUST把全部required attachment claim推进`owner_reserved`并建立target owner ref；capability resolver还必须验证target Session/thread catalog active，因此reserved ref不提前授权未发布target。全部required claim reserved后才可发布target Session/board。存在required claim的公开copy target MUST在staging control DB预置绑定copy/preimage、target lifecycle generation、publication preimage及有序claim ID/hash的`CopyAttachmentSettlementRecord(state=preparing)`；board由`BoardMigrationRecord`保存等价字段。publication把对应record推进为非终态`published_pending_attachment_commit`；无required claim的公开copy不创建settlement record，board则直接进入终态`published`。随后finalizer单独取得target/coordinator gate并复核active generation/preimage，以不重叠的attachment catalog和session-control事务依次把claim推进`committed`，再把copy record推进`committed`或把board record推进`published`。

`CopyAttachmentSettlementRecord`状态闭集 MUST为非终态`preparing|published_pending_attachment_commit`和终态`committed|aborted|target_deleted`；`BoardMigrationRecord`状态闭集 MUST为非终态`preparing|published_pending_attachment_commit`和终态`published|aborted|coordinator_deleted`。未发布失败进入`aborted`；已经发布但target/coordinator删除抢先时，对`owner_reserved`必须精确释放claim/ref，对已`committed` claim必须先验证其属于record冻结的target owner，再按普通删除协议幂等且持久释放该owner ref；只有全部claim、owner ref与release outcome收敛后才进入`target_deleted|coordinator_deleted`，不得伪装成成功终态。恢复 MUST按带类型终态继续，不得以无类型terminal标志猜测publication或attachment是否成功。

target删除与finalizer竞争同一gate：finalizer先行时完成全部claim并把copy/board record分别推进成功终态`committed|published`后，删除按普通owner ref释放；删除先关闭fence时，排空流程按record中的精确claim ID释放`owner_reserved`的ref/claim，或验证已`committed` claim的精确target owner并按普通删除协议持久释放该owner ref，再把copy/board record分别推进终态`target_deleted|coordinator_deleted`。崩溃恢复只有在target仍active且publication preimage一致时才确认原owner ref用于finalization；target deleting/tombstoned时 MUST走删除settlement，不得重建owner、保留target owner ref、恢复active、写成功终态或报告target可用。未发布失败仍只按journal claim ID释放并进入`aborted`；不得扫描digest补claim、复用其它operation claim、复制blob bytes、留下GC空窗或在record非终态时隔离Session节点。

#### Scenario: full rollout copy 与 source 删除按 gate 次序裁决

- **WHEN** `full_rollout_copy`和source Session删除并发，且copy先取得source `SessionReadGuard`
- **THEN**删除在关闭source fence前等待capture完成；copy释放guard后仅用冻结manifest/bytes完成target-local物化，source随后可删除且target不回读source
- **WHEN**删除先取得exclusive gate并关闭source fence
- **THEN**copy明确失败且不发布target，不从隔离节点、删除journal或tombstone旁路读取source

#### Scenario: target publication 不与 source guard 重叠

- **WHEN**copy已经冻结source snapshot并准备创建或发布target Session/main thread
- **THEN**source `SessionReadGuard`已经释放，target gate/事务才可取得；锁顺序检查拒绝同时持有source与target Session gate/guard或两个数据库写事务

#### Scenario: source 并发 append 不产生混合快照

- **WHEN**capture已在source SQLite固定revision和JSONL committed offsets，随后source writer提交新item、terminal convergence或active view变化
- **THEN**本次`SourceCopySnapshot`只包含冻结revision及其committed offsets以内的数据库/JSONL/detail集合；target校验不得看见更新后的SQLite配旧JSONL、旧SQLite配新JSONL或部分checkpoint channel

#### Scenario: source capture 崩溃只能定点恢复

- **WHEN**进程在复制部分数据库/JSONL/detail后退出，或在captured marker durable后、target operation记录`source_captured`前退出
- **THEN**恢复仅检查journal预登记的`source_snapshot_id`/locator；前者abort并清理，后者验证完整manifest/hash后继续同一snapshot，不扫描其它目录、不回读当前source补齐或换revision

#### Scenario: fork attachment claim 防止 source 删除后的 blob GC

- **WHEN**source snapshot引用available attachment，copy在释放source guard前建立claim，随后source Session删除并释放自己的owner ref，而target尚未发布
- **THEN**workspace blob由该claim保留且不复制正文；target发布前claim转为owner_reserved并建立target owner ref，发布后target仍可按digest/length读取同一blob

#### Scenario: attachment claim 崩溃恢复不暴露未发布 target

- **WHEN**进程在claim preparing、owner_reserved或target publication后/claim committed前退出
- **THEN**正常resolver在target catalog发布前始终拒绝预留ref；发布后settlement record保持非终态，恢复只在target仍active且preimage一致时按fork journal中的claim ID继续，target deleting/tombstoned则由删除排空释放/确认且不得重建owner；全程不扫描digest、不遗留GC空窗或把其它fork的claim当成本次owner

#### Scenario: target 删除与 attachment claim finalization 串行

- **WHEN**target已经发布但claim仍为owner_reserved，target Session删除与copy finalizer竞争同一gate
- **THEN**finalizer先行时在释放gate前提交全部claim并把copy/board record分别推进成功终态`committed|published`，删除随后按普通owner ref释放；删除先关闭fence时由排空流程释放reserved claim/ref，或验证committed claim归属后持久释放其target owner ref，再把copy/board record分别推进终态`target_deleted|coordinator_deleted`，copy恢复不得让已删除target重新可用或留下target owner ref

### Requirement: Fork 来源不形成默认运行时依赖

默认 fork SHALL 在 target SQLite 的 `fork_origins` 保存 source/target session、source checkpoint/view/branch、fork mode、mapping version、overlay/detail mapping 和 relationship，但运行时不得读取 source rollout、source SQLite 或 source detail path。一次 fork 只能产生一条新的 provenance/mapping 记录；`full_rollout_copy` 的文件复制步骤不得额外重复插入来源记录。`detached` fork 在 target 物化提交后不依赖 source，target 自己保存被复制的 overlay base/delta 和可用 detail；`pinned` fork 也必须使用 target-local active ref，只在 source SQLite 的 `retention_refs` 保留 source lineage/detail，供审计而不是供 target request 直接读取。source deletion 对 detached target 无影响；pinned source 删除必须等 retention release。required detail 无法复制时 fork 失败，optional detail 记录 unavailable；本 change 不提供单独的 unpin API。

`pinned` fork MUST在source capture前建立durable `ForkRetentionClaim`，不得等target提交后首次补写。target creation/fork journal先预分配target Session/operation/preimage；随后在不持target gate或事务时先取得workspace `NavigationTopologyGate` shared、再取得source exclusive `SessionLifecycleGate`，验证catalog/fence active，并在source `session-control.sqlite`同事务create-or-get绑定fork、source/target GlobalThreadAddress、target operation/preimage、source lifecycle generation与view/detail范围的`ForkRetentionClaim(state=preparing)`和operation lease/retention占位，之后按逆序立即释放两锁。source删除提交catalog deleting前 MUST检查preparing/active claim：删除先提交catalog deleting时fork不得建claim；claim先提交时删除返回`source_retained_by_fork|source_retention_operation_pending`且整棵子树catalog保持active。target `target_committed`后，worker另取source gate CAS同claim为`active`，再提交target `committed`。fork abort由target recovery record定点释放claim；target deletion先在target持久化`ForkRetentionReleaseRecord`，释放target gate后另取source gate释放精确claim，再回到target删除确认terminal；禁止同时持两端gate/事务。preparing claim无墙钟过期，关联record缺失/冲突时fail closed并要求recovery。

`ForkRetentionClaim`自身 MUST以`operation_kind=fork_retention`承担Session operation lease，并与retention占位原子提交；不得再建立一条无法按fork/preimage枚举或settle的平行lease。

本change的fork/copy API MUST限制在同一workspace、可由同一workspace Session catalog/lifecycle gate解析的source与target。Gateway federated link/grant只用于send/read/wait，MUST NOT授权远端fork、远端业务库读取或retention claim；未来跨Gateway copy需要独立export/import协议。

#### Scenario: 父 rollout 被删除

- **WHEN** detached target 已完成 target-local 物化，随后 source session 和 source rollout 被删除
- **THEN** target 仍能读取自己的 checkpoint、active view、Turn/item history、发送新输入并继续执行

#### Scenario: Pinned 子会话阻止父删除

- **WHEN** target 存在 active pinned retention reference
- **THEN** 删除 source 通过 source SQLite 的 active `retention_refs.owner_session_id` 被拒绝，并返回具体 target session

#### Scenario: Pinned retention claim 与 source 删除双顺序

- **WHEN**source删除先取得exclusive gate并关闭fence，随后pinned fork尝试建立claim
- **THEN**fork返回`source_session_deletion_pending|source_session_deleted`，不capture source、不创建可用target或retention占位
- **WHEN**pinned fork先提交preparing claim，随后source删除到达
- **THEN**删除在整树catalog deleting前返回`source_retention_operation_pending`或`source_retained_by_fork`，source所在整棵逻辑子树保持active供同一fork capture/恢复；不得先删source再尝试从target补claim

#### Scenario: Pinned 子会话被删除

- **WHEN** 用户删除一个作为 pinned fork target 的 session
- **THEN** 系统将 source SQLite 中对应 `reference_kind=fork`、`reference_id=fork_id` 的 retention reference 标记为 `released`，之后 source 可以继续删除或 pruning 未被其它关系保护的 view

#### Scenario: Pinned release 不同时持有两端 gate

- **WHEN**pinned target被删除或fork materialization abort，需要释放source claim
- **THEN**target先durably记录release/abort intent并释放自己的gate，再单独取得source gate释放精确claim，最后回到target确认terminal；任一崩溃点从两侧record幂等恢复，不同时持有source/target gate或数据库事务

#### Scenario: Fork 后 source overlay 与 detail 保持 target-local

- **WHEN** fork 复制的 history view 还引用 source overlay base/delta 或 sealed assembly detail
- **THEN** target 使用新的 target-local `source_overlay_epoch`、base/delta/item/detail identity 和 JSONL/物理 detail 路径；source epoch、source ref 和 source offset 只作为 lineage。后续 history view cutoff 不自动删除 target overlay，下一次 reconciliation 决定复用、追加或物化；target 不因 source 删除而从 source 重新读取

### Requirement: Fork 物化必须可恢复提交

系统 SHALL 在 target SQLite 记录 `fork_materializations` journal，且 journal 明确保存 source/target namespace、mode、source anchor/checkpoint/view、mapping version、source snapshot/retention claim identity和materialization offsets。消息/item、checkpoint channel、pending state 和 target mapping 的中间 append 不得直接让 target session 变成可用状态。`context_fork`/`history_prefix_fork` 的运行态 preflight 拒绝在 target 创建前完成，失败不写目标取消 Turn；只有 `full_rollout_copy` 才在同一收敛事务中写入 target-local mapping、唯一 `fork_origins`、target active view 和复制运行态的 `cancelled` historical state，然后进入 `target_committed`。pinned fork必须在source capture前已有`ForkRetentionClaim(state=preparing)`；target `target_committed`后只把同一claim激活，成功后才进入`committed`。启动时遇到`prepared`必须按journal清理target半成品并释放对应preparing claim，遇到`target_committed`必须幂等激活既有claim/推进最终状态，不能首次创建另一个retention或根据JSONL source/target offset猜测fork是否成功。

若本次fork携带调试方案，`fork_materializations` MUST同时记录冻结的source调试artifact hash、选定方案ID/revision、target有效debug配置revision/hash与target-local方案映射/manifest hash；只有所有target方案文件完成durability barrier、配置revision发布前复核仍匹配并通过校验，才可提交target可见性。`prepared`恢复清理半成品调试文件，`target_committed`恢复只验证既有target artifact并推进同一journal；不得在提交后从source当前调试目录补复制或把失败方案静默剔除。

#### Scenario: Fork 物化中途崩溃

- **WHEN** 进程在 target item/消息、checkpoint 或 mapping 已部分追加但 journal 仍为 `prepared` 时退出
- **THEN** 下次打开 target rollout 将其恢复为干净的未物化状态，标记该 journal 为 `aborted`，且历史和 checkpoint 不返回半个 fork

#### Scenario: Fork 目标提交后进程退出

- **WHEN** target 已将 target-local finalization、运行态终止、mapping、active view 和 provenance 一起提交为 `target_committed`，但 source pinned retention 尚未完成时退出
- **THEN** 下次打开 target rollout按journal找回capture前已建立的preparing `ForkRetentionClaim`，验证两侧preimage后幂等激活并将journal标记为`committed`；不得首次补建或重复创建mapping、provenance、claim或retention

### Requirement: Compaction 创建 SQLite context view

系统 SHALL将compaction表达为SQLite中新的context view和控制事件，不创建新的JSONL segment或控制记录。摘要和保留的canonical item继续作为不可变item追加到rollout.jsonl，新的view通过item range/reference选择摘要和保留范围。实际compaction是四类合法prefix epoch边界之一；compaction事务提交view及`PendingPrefixEpochTransition(reason=compaction)`，但pending记录没有wire bytes且不是applied epoch。下一次真正model-call preparation可以精确复用source base/delta，也可以从activation coordinator冻结的ResourceRegistry内存snapshot取得tracked published revision并物化为完整source，然后把source decision、首个新epoch assembly seal与transition消费原子提交。snapshot不可用时保留compaction view/transition但不产生半assembly或dispatch，且不读取文件、网络或其它provider。所有第一条真实用户消息后的summary/source完整或增量内容必须保持独立user-role item。source/detail失效不能被解释为物化理由来修复旧sealed assembly；精确恢复仍显式失败。

#### Scenario: 压缩后继续运行

- **WHEN** compaction 成功提交并开始下一轮 Agent 执行
- **THEN** 新checkpoint引用包含摘要和必要尾部item的SQLite context view并登记pending compaction transition，后续item继续追加到同一个rollout.jsonl；下一次model-call preparation若精确复用source overlay或从冻结Registry activation snapshot物化tracked published完整user-role revision，都必须与首个新epochassembly及transition消费原子提交，不得修改旧item、请求期读源或用当前文件修复旧assembly

#### Scenario: 压缩后读取原始历史

- **WHEN** 调用方请求 compaction 之前仍未被 pruning 的历史
- **THEN** reader 通过 SQLite 旧 view 读取原始 item，且不把 compaction 控制事件作为模型请求 item 或用户消息

#### Scenario: compaction 前的 source overlay 保留审计 lineage

- **WHEN** `AGENTS.md` 或已应用的 skill source 在 compaction 前有 base A 和 A→B 增量
- **THEN** compaction的新epoch可以继续精确引用A与A→B，也可以按明确materialization policy从冻结Registry activation snapshot取得并引用B的完整revision；仅owner声明`root_eligible`且实际新epoch重建时可合并唯一system root，`tail_only`仍为独立user-role item。A与A→B的item/provenance保持可审计，不被物理覆盖或删除；source/detail失效作为旧assembly明确错误，不能触发静默重建或当前源读取

### Requirement: Rewind 在 SQLite 中创建新 branch 和 view

系统 SHALL在同一个rollout.jsonl上追加后续canonical item，并在SQLite中创建新的branch、context view和control event。rewind事务改变`history_view_revision`和canonical history可见尾部，恢复checkpoint-versioned tracking state并提交`PendingPrefixEpochTransition(reason=rewind)`；旧item保持不可变，旧后缀从新view中隐藏。pending记录没有wire bytes且不是applied epoch。目标checkpoint若保留tracked registration但最新已注入revision不在重建后的active view，下一次真正model-call preparation必须从activation coordinator冻结的ResourceRegistry内存snapshot取得published完整revision，并把恢复事实、新source overlay epoch、首个rewind epoch assembly与transition消费原子提交；合法新epoch只把owner声明`root_eligible`的受信有效状态合并唯一system root，`tail_only`仍独立user-role。snapshot不可用或seal失败时保留rewind view/transition但不产生半item或dispatch，且不得读当前源。snapshot/untracked不自动恢复。不得从overlay lineage把cutoff后的delta直接复活，不得创建semantic segment、parent segment或复制旧item正文来伪造新branch。

#### Scenario: 只回退不继续

- **WHEN** 用户将 active head 回退到历史 checkpoint 或 item anchor，但暂不发送新消息
- **THEN** 系统切换到新的 branch/view，旧后缀仍可由旧 checkpoint/view 读取但不属于当前 view

#### Scenario: Rewind 后继续追加

- **WHEN** 用户 rewind 后执行 continue 或 resume
- **THEN** 新用户 item、tool call item 和模型输出 item 追加到同一个 rollout.jsonl，并加入 rewind 创建的新 view

#### Scenario: Rewind 跨越旧后缀

- **WHEN** 当前物理尾部已经包含后续 item，但用户回退到更早 item anchor
- **THEN** 新 view 只引用目标边界的有效范围和新 item 范围，不把物理尾部旧后缀加入当前上下文

#### Scenario: rewind 到 source delta 之前

- **WHEN** source overlay 为 A base 加 A→B delta，而 rewind 的 history view 不再包含保存该 delta 的历史 item
- **THEN** 下一次model-call preparation以rewind view作为canonical history，并从冻结Registry activation snapshot取得tracked published revision B，把恢复事实、新source overlay epoch、首个rewind prefix epoch assembly及transition消费原子提交；`root_eligible`可合并新root，`tail_only`为独立user-role item，旧A→B只保留审计lineage，snapshot/untracked不恢复，当前源不被读取

### Requirement: Fork anchor 具有明确包含语义

系统 SHALL 区分 `inclusive` 和 `before` 两种 item anchor。context view/reference 必须保存实际选择的逻辑 item 范围，Turn anchor 解析 MUST 使用 `TurnRecord.root_input_item_id` 作为用户交互起点，不得把 item 边界丢失或隐式扩大到物理尾部。

#### Scenario: Inclusive fork

- **WHEN** 用户从 B:3 创建 inclusive context fork
- **THEN** 子 view 包含截至 B:3 的有效 item，不包含 B:3 之后的旧后缀

#### Scenario: Before replay

- **WHEN** 用户编辑并重新执行 B:3
- **THEN** 新 view 只包含 B:3 之前的 item，再追加编辑后的 B:3 item 和新的后续 item

### Requirement: 用户操作使用 Turn 入口并解析到细粒度 item anchor

系统 SHALL 继续允许用户可见的 rewind、replay 以及从历史 Turn 发起的上下文操作使用稳定的 `turn_id` 和 `inclusive`/`before` 语义；backend resolver MUST 通过 `RolloutCheckpointSaver` 读取已提交的 active-lineage source view，并解析到 `TurnRecord.root_input_item_id` 对应的 canonical item 起点，再在需要时解析到 `content_part_id`/fragment anchor。业务层和 projector 不得直接扫描 RolloutStorage、AppendWriter 或内部 context reader。内部 compaction、fork 和恢复流程可以直接使用细粒度 durable item anchor；interrupt 首先使用当前 stream 的内存 cursor/ItemDraft，只有需要跨重启恢复或审计时才保存对应的 item/content-part reference。request-only context reference 与 pending runtime notice 不属于 view，不能直接作为 durable operation anchor。前端和普通调用方不需要传递 `view_id`、`checkpoint_id` 或物理 message 序号。

任何将被继续执行或编译为 Provider request 的目标 view MUST 在 tool protocol 上闭合。若解析后的 item/content-part anchor 位于 assistant tool-call group 与任一匹配 terminal tool result 之间，resolver MUST 返回 `tool-protocol-boundary-conflict`，保持原 view 且不得创建新 view、branch、checkpoint 或 prefix epoch；响应可以提供不含内容正文的前后最近安全 anchor，但不得自动移动用户选择、合成 result 或按物理序号猜测边界。read-only history 可以保留 partial/incomplete group 用于审计，但不得把它标记为可执行。

系统 SHALL 保留 item 级定位能力。自动 compaction、用户主动 compaction、重放和 durable interrupt finalization 均可以使用 canonical item/content-part 作为边界；不得因为 Turn 或 LangChain message projection 存在而把操作强制对齐到 message 末尾。实时 interrupt 可以只停留在内存 cursor，不能因此要求每个 raw chunk 都成为 SQLite anchor。所有 durable anchor MUST 明确记录 `inclusive`/`before`、source view/branch 和可恢复性；content-part anchor 至少需要稳定 part identity、ordinal、hash/prefix hash 和 recovery capability，任意字符/token offset 不得单独作为 durable anchor。

#### Scenario: Turn 操作解析到 tool call 之前

- **WHEN** 用户从包含 reasoning、assistant output 和 tool call 的 Turn 发起 before replay
- **THEN** resolver 将 Turn 入口解析为 tool call item 之前的精确 anchor，保留前置 item，不构造或修改 LangChain message 来表达边界

#### Scenario: content part 中断

- **WHEN** stream 在 assistant_output item 的某个 draft/content part 后被用户中断
- **THEN** finalization 保存 partial 状态；只有跨重启恢复或审计需要时才保存 item/content-part 停止 reference，恢复不会把该事实扩展为完整 assistant message

#### Scenario: item anchor 不属于 source view

- **WHEN** 调用方提交的 item/content-part anchor 不在目标 branch/view 的有效 lineage 中
- **THEN** 系统返回明确的 stale/unreachable anchor 错误，不按最大 message sequence 或最新 checkpoint 静默替换边界

#### Scenario: item anchor 切入 tool 调用与结果之间

- **WHEN** rewind、replay、compaction 或 fork 将一个 assistant tool-call group 与其任一匹配 terminal tool result 分隔到 active view 边界两侧
- **THEN** 系统返回 `tool-protocol-boundary-conflict`，不改变原 view、不创建目标，并返回可选择的安全 anchor metadata；只有只读历史可以展示该 partial group，执行投影不得消费它

#### Scenario: system reminder 位于新 Turn 之前

- **WHEN** source view 中最后一个执行因中断产生 pending runtime notice，随后用户提交新的普通输入
- **THEN** resolver 将新输入的 `root_input_item_id` 作为新 Turn 起点；pending notice 只按其 `notice_for`/assembly 关系参与请求，不因其 wire role 或物理位置取代新 Turn root

## ADDED Requirements

### Requirement: rewind、compaction、fork 与 delegated thread 的边界不可混同

rewind、compaction、history replay 与 checkpoint branch MUST 只重建其 selected `SessionThread` 的 active view、source state、ToolSet applied binding 和 graph checkpoint；不得影响同一 Session 的 sibling thread。`context_fork`、`history_prefix_fork` 和 `full_rollout_copy` 仍然创建新的 target Session 与其新的 main thread，并以 thread-qualified `GlobalEntityRef` 保存 source/target mapping。新 delegated execution 则在 parent Session 创建 child SessionThread，不得用 context fork 或新的 product Session 伪装。

#### Scenario: child thread rewind 不影响 main thread

- **WHEN** delegated child thread 从某个 item anchor rewind
- **THEN** 只有该 child thread 新建或切换 active view；main thread 的 canonical prefix、assembly、ToolSet binding、checkpoint 和 Turn ordinal 保持不变

### Requirement: Checkpoint 区分 canonical view 与 request-only assembly

checkpoint SHALL 将可恢复的 canonical context view、LangChain message projection 和某次请求的 `ContextAssemblySnapshot` 分开引用。checkpoint 可以记录当次请求应用过的 request-only prompt contribution 或独立 `ToolSetRef` 的 assembly reference、版本和 hash，但不得把合并后的 system/developer wire message、ToolSetSnapshot 或 tool definition 当作 canonical history 写回 view，也不得用当前 middleware 配置伪造旧 assembly。

checkpoint 在 restore/replay 时必须把已选 ToolSetRef manifest identity（snapshot id、source revision、length、hash token、schema/policy version 和绑定 policy）作为 `context-plan-hash:v2` 的一部分逐字段校验；工具 schema/config 或 policy 变化只能得到新的 plan/hash，不能从当前 registry 静默重建旧工具集合。`ContextContribution.contribution_kind=tool_set` 非法，旧值只能进入 legacy quarantine，不能成为 checkpoint 中的 Provider tool definition。

#### Scenario: checkpoint 恢复动态 system context

- **WHEN** 某次执行使用了 skill 说明或 workspace/environment prompt，但这些内容没有被提升为 canonical item
- **THEN** checkpoint 恢复 canonical view 时不新增历史 message；如需重放原请求，系统通过保存的 assembly/request-only reference 判断是否可以精确重建或明确报告 source mismatch

#### Scenario: checkpoint 恢复持久化系统通知

- **WHEN** 中断提醒或 compaction notice 已通过 append intent 成为 canonical system notice
- **THEN** checkpoint 的 view 可以引用该 canonical `runtime_notice` item；若它位于第一条真实用户消息之后，当次请求只能按plan ordinal投影为独立user wire item，并与canonical identity保持可追踪但不互相替代，不得提升为中途system

### Requirement: Turn root、execution 和 finalization 在分支操作中保持稳定

分支操作必须使用统一的 `Turn.status` 闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`。合法转移为 `open -> active|cancelled|failed|unknown`、`active -> completed|completed_empty|interrupted|cancelled|failed|unknown`；只有显式 `resume_turn` 才允许 `interrupted -> active`，以及 reason=`execution_lost` 的 `unknown -> active`，且必须创建新的 execution。其它 terminal outcome 不可转移。`completed_empty` 是唯一的无 canonical output 正常终态名称；普通 `cancelled` 与 `full_rollout_copy` 对未复制 source runtime 写入的 `cancelled` historical 都是吸收态，不能执行原 Turn 的 `resume_turn` 或 `dispatch_replay`，必须返回 `turn_not_resumable`；新执行只能通过独立的 `replay_as_new_turn` 创建新的 Turn。

Turn.status 转移表冻结为：

| 当前状态 | 允许的下一状态 | 条件 |
|---|---|---|
| `open` | `active`, `cancelled`, `failed`, `unknown` | acceptance 已提交后由执行启动或明确控制结果收敛 |
| `active` | `completed`, `completed_empty`, `interrupted`, `cancelled`, `failed`, `unknown` | terminal convergence 一次提交 |
| `interrupted` | `active` | 仅显式 resume，必须新建 `execution_id` |
| `unknown` | `active` | 仅 reason=`execution_lost` 的显式 resume，必须新建 `execution_id` |
| `completed`, `completed_empty`, `cancelled`, `failed` | 无 | terminal；对原 Turn 的 `resume_turn` 或绑定原 `turn_id` 的 `dispatch_replay` 不发生状态转移；`cancelled` 均返回 `turn_not_resumable`，新执行只能由独立 `replay_as_new_turn` 创建新 Turn |

回放操作必须分离：`history_replay` 只生成 projection，不创建 execution；`resume_turn` 只按上表复用允许恢复的原 Turn；`dispatch_replay` 表示把 Provider dispatch 绑定到原 `turn_id`，对普通 `cancelled` 和 `full_rollout_copy` 的 cancelled historical 都返回 `turn_not_resumable`；`replay_as_new_turn` 才是显式的新 Turn 创建操作，创建新的 Turn/root/acceptance/initial execution 并以 `replay_of_turn_id` 保存 lineage。`replay_as_new_turn` 后续可以进行 Provider dispatch，但不是原 Turn 的 `dispatch_replay`，同一 API 语义不得同时返回错误并创建新 Turn。`full_rollout_copy` 的 cancelled historical 不允许任何同 Turn resume 或 dispatch replay。

checkpoint、branch 和 view SHALL 保存 Turn 的 `turn_id`、owner-thread-global 且不可重排的 `turn_ordinal`、不可变 origin `source_branch_id`、`accepted_ingress_id`、`acceptance_idempotency_key`、`root_input_item_id`、execution/model-call lineage 和 `final_item_id` 引用。在同一 `(session_id, thread_id)` owner namespace 内，派生 branch/view 只能复制既有 Turn/item 的引用，不得复制或重新编号 TurnRecord；无新用户输入的 continue/resume 只有在 Turn.status 转移表允许时才继续原 Turn并创建新的 execution/model-call identity。`history_replay` 是唯一可以复用 source Turn/root 的历史 projection，不创建 execution；显式 `replay_as_new_turn` 必须创建新的 Turn/root/acceptance/initial execution，并在 active view 登记新的 `logical_turn_ordinal`，source history 可以作为前缀复制或引用但 source Turn/root 不能成为新 Turn 的 root，并用 `replay_of_turn_id` 关联。跨 session fork 不复用这些裸 local identity，而按本 delta 的 GlobalEntityRef/target-local mapping 合同建立新的 target Turn/item/acceptance identity，并仅在 lineage 中保存 source identity；target 两类 acceptance identity 各自必须满足 target-thread-local 唯一性。fork 后接受新的真实输入 MUST 在 target main thread 创建新的 thread-local Turn、递增的 target-thread `turn_ordinal`、新的 root 和新的 origin branch。retry/resume 不得复用已经提交的 output item identity；没有成功 finalization 时不得以最后一个 assistant item 替代 `final_item_id`。`context_view_turns.logical_turn_ordinal` 是 view-local 唯一顺序，必须和 owner-thread-global `turn_ordinal` 分开；上述新 Turn 操作不能被解释为绑定原 Turn 的 `dispatch_replay`。

#### Scenario: resume 不创建伪造 Turn

- **WHEN** 用户从被中断的 execution 执行 continue/resume，且没有提交新的用户输入
- **THEN** 新 branch/view 可以继续原 Turn 的 root，并记录新的 execution/model-call lineage，不创建第二个 `TurnRecord`、`root_input_item_id` 或全局 `turn_ordinal`

#### Scenario: 新用户输入创建新 root

- **WHEN** pending runtime notice 之后提交一条普通用户输入
- **THEN** 系统创建新的 `turn_id`、全局递增的 `turn_ordinal`、新的 root input item 和新的 origin `source_branch_id`；pending notice 只能通过 ambient/assembly relation 参与请求，不改变新 Turn 的 root

#### Scenario: 同一 owner thread 内 history_replay 复用 Turn 但不隐式执行

- **WHEN** 从已有 Turn 的 inclusive 或 before anchor 在同一 `(session_id, thread_id)` 内创建 branch/view
- **THEN** 新 history view 复用原 `turn_id`、`turn_ordinal`、`source_branch_id` 和 `root_input_item_id`，并为该 view 登记自己的 `logical_turn_ordinal`；复制或加载 view 不产生新的 execution，`history_replay` 也不产生 execution，只有符合 Turn.status 转移表的显式 `resume_turn` 才新增 execution/model-call lineage

#### Scenario: 同一 owner thread 内 replay_as_new_turn 创建新 active Turn

- **WHEN** 从已有 Turn 的 inclusive 或 before anchor 在同一 `(session_id, thread_id)` 内明确选择 `replay_as_new_turn`
- **THEN** 新 active view 可以复制或引用 source history 作为上下文前缀，但必须登记新的 target-local `turn_id`、新的 `user_input` root、acceptance、initial execution 和新的 view-local `logical_turn_ordinal`；source Turn/root 仅作为前缀或 lineage，不能成为新 Turn 的 root
- **AND** 新 Turn 的 Provider dispatch 绑定新 `turn_id`，该操作不是 source Turn 的 `history_replay`、`resume_turn` 或 `dispatch_replay`

#### Scenario: 跨 session fork 新建 target identity

- **WHEN** 从 source session 的 Turn/anchor 通过 `context_fork`、`history_prefix_fork` 或 `full_rollout_copy` 创建 target session
- **THEN** target 使用 target-main-thread-local `turn_id`、`turn_ordinal`、`source_branch_id`、`accepted_ingress_id`、`acceptance_idempotency_key`、root/item/assembly refs 和 offset，并在 fork mapping 中保存 source GlobalEntityRef；两类 acceptance identity 分别占用 target-thread-local 唯一空间，source 值只保留为 lineage/audit 坐标；复制本身不隐式创建可运行 execution，只有符合 Turn.status 转移表的显式 `resume_turn` 才复用 target Turn 并创建新的 target execution/model-call lineage；`history_replay` 只在其 owner namespace 已存在的 history view 中复用该 namespace 的 Turn 引用且不创建 execution；显式 `replay_as_new_turn` 必须在 target active view 创建新的 target Turn/root/acceptance/initial execution 和新的 `logical_turn_ordinal`，可以复制或引用已映射 source history 作为前缀，source Turn/root 不能成为新 Turn 的 root，并以 `replay_of_turn_id` 关联；该操作不属于原 Turn 的 `dispatch_replay`；普通或 `cancelled` historical Turn 的 `resume_turn`/`dispatch_replay` 均返回 `turn_not_resumable`

#### Scenario: root lookup 与 view 顺序分离

- **WHEN** 同一 Turn 出现在两个不同 fork lineage 的 view 中
- **THEN** resolver 先按 view lineage 定位 `context_view_turns`，再解析同一个 `root_input_item_id`；每个 view 可有不同 `logical_turn_ordinal`，不得按 `MIN(item_sequence)`、wire role 或 view 第一条 item 创建第二个 root
