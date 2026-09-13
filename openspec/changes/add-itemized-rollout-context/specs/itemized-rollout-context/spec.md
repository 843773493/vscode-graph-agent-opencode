## Purpose

为会话 rollout 建立独立于 LangChain 和具体 Provider 的不可变语义 item 层，使上下文、历史、恢复和 Provider 请求都能从同一份有序事实重建。

## ADDED Requirements

### Requirement: 产品 Session、durable Thread 与 LangGraph namespace 必须严格分层

Workspace navigation SQLite MUST是Session/Folder关系的唯一权威，MUST启用foreign key、node/locator唯一约束、schema version和启动完整性检查。切换旧树前 MUST保留可校验的旧索引/目录/SQLite一致性备份；新catalog的受控备份 MUST使用SQLite online backup或等价一致性快照并记录generation/checksum，不得直接复制活动WAL文件。catalog损坏或备份generation落后时 MUST进入维护/人工核对状态，保留未登记目录与原数据供审计，不得扫描日期桶重建父子关系、自动吸收Session、默认标active或把未知目录交给GC删除。

Session物理节点 MUST位于`${workspace_abs_path}/.boxteam/sessions/YYYY/MM/DD/{session_id}/`，日期从创建intent冻结的UTC`created_at`取得；叶名严格等于canonical ID，正常移动/重命名不得改变locator，不得以当前日期、显示名或导航父链猜路径。Workspace `navigation/session-catalog.sqlite` MUST以唯一node表保存Session/Folder的stable ID、kind、parent_node_id、显示名、状态及revision；Session行额外保存不可变created_at、受检相对locator及唯一main_thread_id，Folder不对应任何物理目录/manifest。SQLite父链 MUST拒绝缺失parent、跨workspace引用、自环/循环和deleting节点下新建/移动；最近祖先Session和breadcrumb只从该父链派生，`parent_session_id`若投影到DTO不得再作为`session.json`里的第二可变事实。fork/delegation/context source等业务lineage MUST与导航父链分离，移动Folder/Session不改变Session kind、来源、已封存context或ThreadRuntimeBinding。Gateway仅经Workspace受控catalog API同步索引，不得直接读取该业务SQLite或依据物理路径定位Session。

系统 SHALL以`workspace_id → session_id → SessionThread`表达产品运行层级。Workspace `.boxteam/navigation/session-catalog.sqlite`是Session/Folder导航节点、父子关系、Session locator和不可变`main_thread_id`的唯一权威；Session节点的`session-control.sqlite`保存thread catalog、collaboration ledger/fanout及child/migration publication journal，其唯一kind=main row MUST与workspace catalog pointer一致，不得维护可独立变动的第二main pointer。`session.json`仅保存不可变创建metadata，不保存可变父节点/当前显示名。canonical item、Turn、execution、model call、context view、assembly、source registration及ToolSet applied binding以`(session_id, thread_id)`为owner；Session control不得成为跨thread canonical context或第二ContextStore writer。

持久化 `session_id` 外形 MUST 匹配 `ses_[0-9a-f]{32}`，`thread_id` 外形 MUST 匹配 `thr_[0-9a-f]{32}`，并各自恰为36个ASCII byte；payload由UUIDv4生成，其第13个hex MUST为`4`、第17个hex MUST属于`8|9|a|b`。API、Proto、catalog、Session link/URI、typed ref、生产/测试IdentifierFactory和path resolver MUST 使用同一完整validator并拒绝斜杠、反斜杠、百分号编码、Unicode、`.`、`..`、非v4 bit profile及任何错误前缀、大小写或长度；不得清洗、截断、hash替代目录叶名或在正常runtime提供旧ID path alias。历史非规范ID只能经显式migration staging取得新的canonical target ID并保留source lineage/report。resolver在落盘前还 MUST 校验完整path预算。

LangGraph 的 `configurable.thread_id` MUST 等于 product `thread_id`。`checkpoint_ns` 仅用于该 product thread 内的 root graph/subgraph checkpoint namespace，MUST NOT 被当作、编码为或反向推断为 product thread identity。跨 owner 引用 MUST 使用 `(session_id, thread_id, entity_type, local_id)`，不得以裸 `session_id` 或 `checkpoint_ns` 访问其它 thread 的 item、checkpoint、source 或 active view。

Session创建 MUST先在workspace `.boxteam/navigation/session-catalog.sqlite`同库`SessionCreationRecord`以软件生成的idempotency key一次分配canonical session/main-thread ID、冻结UTC创建时间、`sessions/YYYY/MM/DD/{session_id}`最终locator、父node/generation、GraphBinding/capability preimage及ID/locator reservation；record未published时普通reader不可见。先在同文件系统`.boxteam/sessions/.staging/<operation_id>/`完成session metadata、唯一active fence、main storage/index/ContextStore和durability barrier，再原子rename到冻结日期目标；copy target所需`CopyAttachmentSettlementRecord`仍先置于staging control DB。最后重取topology exclusive、在workspace catalog同一SQLite事务CAS父node仍active/generation未漂移并发布node/locator/main pointer、终结record，该commit是唯一可见性提交点；不同preimage冲突，rename后/commit前orphan只按record定点恢复/隔离，不扫盘吸收。普通reader不得观察缺main、双main、非法初始fence或`thread_id=session_id` fallback，Gateway不参与业务事务。

`session-control.sqlite` MUST为每个Session保存唯一`SessionLifecycleFence(state=active|deleting|tombstoned, lifecycle_generation, deletion_record_id?)`。所有会产生新持久副作用的Session入口 MUST先取得workspace跨进程`NavigationTopologyGate` shared、再取得该Session的`SessionLifecycleGate` exclusive，在fresh SQLite catalog查询目标active/locator并确认local fence active generation，于至多一个session-control事务建立durable lease/等价record后立即释放两锁；已存在execution/runtime lease可覆盖其内部callback。最终可见性publication重取topology shared→Session gate并fresh验证catalog active及fencing token；catalog已deleting时新可见性发布必须取消，仅允许先前准入的冻结operation收敛，不得新派生root/wakeup/child。导航parent/name变更只写workspace SQLite且不产生context；lock顺序固定为topology→至多一个Session gate→至多一个SQLite写事务，禁止反向等待、持两个Session gate/两个DB写事务或持锁跨模型/网络等待。

`SessionOperationLease` MUST至少包含`lease_id`、`operation_kind=thread_creation|board_migration|collaboration_fanout|runtime_owner|execution|context_control|communication_source|communication_target|federated_call|remote_observation|attachment|fork_retention|session_catalog_mutation`、稳定`operation_identity`与preimage hash、捕获的`lifecycle_generation`、`holder_generation/fencing_token`、`state=active|settling|completed|cancelled|failed`、revision和可选`recovery_ref`；同generation/operation identity唯一且非终态可索引。专用record承担lease时 MUST提供同等字段/约束，并以显式`lease_state`或版本化全映射把自己的preparing/routing/published/aborted等状态归入上述非终态/terminal集合，不得按状态名称猜测。lease MUST NOT按墙钟自动到期。恢复owner只有在验证旧holder generation失效并CAS新token后才能继续原operation或进入settling；fence deleting后不得建新lease，旧lease只可完成/取消冻结operation且不得派生新root/wakeup/child。跨库主体先durable commit、lease后terminal；中间崩溃按稳定identity/recovery ref核对同一结果。删除方请求settling后仍必须等待原writer确认或幂等恢复核对，terminal前不能隔离节点；terminal后旧token callback必须失败。同一Session的catalog locator/lifecycle/归档等目标条目mutation MUST以`session_catalog_mutation`竞争gate，旁路写入fail closed。

`SessionLifecycleGate` MUST按`(workspace_id, canonical_session_id)`使用navigation控制根中不随Session日期node改变的跨进程OS shared/exclusive锁，打开前执行ID/path/no-symlink校验；`NavigationTopologyGate`另在workspace导航根提供跨进程shared/exclusive短锁。cold history/detail read在topology shared下取得fresh catalog locator和Session shared `SessionReadGuard`后释放topology锁，保持Session guard直到全部node/SQLite handles关闭并复核catalog/fence；已开始reader可完成，catalog deleting后新reader返回`session_deletion_pending`。删除owner逐Session关闭local fence前须等待read guard；锁对象生命周期内不得unlink/recreate，仅进程mutex或不能验证的锁语义必须fail closed。

普通单child创建 MUST在owner Session内以`thread_creation_idempotency_key`去重，delegated child还必须把`delegation_id`纳入preimage及唯一约束。在创建staging目录前，系统 MUST先按生命周期准入协议于`session-control.sqlite` create-or-get不进入thread catalog且承担operation lease的`ThreadCreationRecord(state=preparing)`，冻结request preimage hash、canonical child ID、最终relative locator、内部staging locator、GraphBinding/capability/seed/admission identity、预期artifact manifest/hash及owner Session lifecycle generation、thread catalog、collaboration/delegation precondition revision；同key不同preimage MUST冲突。child node、GraphBinding、capability、task seed/reference、初始ContextStore和持久execution admission intent再按记录完整staging并通过durability barrier，原子rename到记录指定且尚未被catalog引用的最终locator；最后重取topology shared/Session gate，由thread catalog、需要时的collaboration ledger及该record在单一SQLite事务中CAS验证workspace catalog active及fence仍为记录捕获的active generation、delegation/parent/member未取消且precondition未漂移，再插入可见catalog记录并推进为`published`。CAS失败 MUST不发布、不重基到当前状态，并只可在定点清理record列出的staging/final orphan后标记`aborted`；显式重试使用新operation。rename后、发布前崩溃的orphan MUST从预先存在的record定点校验、继续或清理，不得扫盘、猜测或吸收无记录目录；发布后、terminal response前恢复同一结果。发布后worker按creation/delegation identity幂等且重新通过生命周期准入create-or-get初始execution；不得留下可见空child、重复child或重复Job。手动child只有显式`initial_state=idle`时可无seed，且该值必须进入preimage。board migration批量child MUST改由在gate内先行建立的`BoardMigrationRecord`内逐target的`MigrationChildCreationEntry`承担等价lease/generation/creation manifest，且只有验证同一active generation的board最终事务可以整批发布；不得为同一target建立可由普通worker推进的独立`ThreadCreationRecord`。

Session删除（含其逻辑后代）与`recursive=true` Folder删除 MUST统一使用workspace `session-catalog.sqlite`的`NavigationSubtreeDeleteRecord`；不再打开独立`session-deletion-journal.sqlite`。删除持topology exclusive，以递归CTE冻结精确node/Session集合、各Session最后/隔离locator和parent/revision preimage；逐Session短时取得一个Session gate预检active/preparing `ForkRetentionClaim`，有blocker即不发布删除。随后同一workspace SQLite事务create-or-get幂等batch record并CAS全部target active，一次标记整棵子树deleting；此commit是逻辑可见性和新操作准入的线性化关闭点。同key同preimage恢复原batch，不同preimage冲突；任一目标preimage漂移明确abort且不部分标记。

#### Scenario: pinned retention 与整棵子树删除抢占准入

- **WHEN** pinned fork与包含source的Session/Folder递归删除并发
- **THEN** topology gate确定顺序：claim先建立时删除在catalog deleting提交前返回retention blocker且整棵子树仍active；删除先提交整树deleting时新claim被拒绝且无target副作用，不存在“journal已提交但局部fence未关、仍可补pin”的旧窗口

catalog批量deleting commit之后，即使部分Session local fence尚active，新的thread history/detail或mutation仍 MUST因fresh catalog检查返回`session_deletion_pending`，不能新建lease或回退active。删除owner不持topology gate、按冻结batch逐Session取得exclusive Session gate并CAS local fence到deleting、等待既有guard和所有旧generation lease/child/board/copy attachment/communication/pin收敛；旧writer仅能按原lease和删除record许可完成/取消冻结operation。只有目标零非终态lease、外部写入已停止且必要owner ref/tombstone durable，才可将该日期Session目录单独rename到batch冻结的`.boxteam/sessions/.deleting/<operation_id>/<session_id>/`并在workspace catalog记录进度。全部目标完成后一个workspace SQLite事务将整棵逻辑子树推进不可复用tombstone；中途崩溃保持全树deleting，恢复只按batch manifest定点继续，不扫盘、不向普通reader暴露部分成功。全程不得持gate等待reader、worker、网络或旧lease terminal。

#### Scenario: Session 默认入口只定位 main thread

- **WHEN** 产品 API 或 Web 未指定 thread 而请求一个 Session 的普通聊天、历史或执行
- **THEN** 服务从权威 Session catalog 解析 `main_thread_id` 并只访问该 thread；响应包含实际 `thread_id`，不得扫描或合并 child thread 的历史

#### Scenario: Session 创建在发布前后崩溃

- **WHEN**进程分别在同库create record提交后、staging/durability barrier前、日期目录rename后但catalog publish事务前、catalog publish后但响应前退出，并以同一session creation key恢复
- **THEN**正常reader只能看到不存在或恰好一个带完整main thread的Session；恢复通过journal和catalog返回同一session/main identity，未引用orphan仅定点继续/清理，不扫盘吸收、不产生第二main、丢失并发catalog更新、半目录或Gateway侧业务补写

#### Scenario: delegated child 创建被重试

- **WHEN**同一delegation在child staging、catalog发布或初始Job binding任一窗口重试
- **THEN**相同thread creation key/delegation preimage只解析一个child和一个初始execution；不同preimage冲突，正常reader看不到空child或staging locator

#### Scenario: child 准备期间父状态漂移不被覆盖

- **WHEN**`ThreadCreationRecord`提交后、catalog发布前，owner Session被删除、delegation被取消，或parent/member/catalog revision发生变化
- **THEN**最终发布CAS失败且不创建可见child或初始Job；恢复只清理record列出的staging/final orphan并标记`aborted`，不得按当前状态重基、扫描目录或恢复已取消delegation

#### Scenario: Session 删除等待内部操作记录收敛

- **WHEN**Session存在preparing child/board record、未终态communication或attachment pin时收到删除请求，并在任一清理阶段崩溃
- **THEN**workspace SQLite catalog保持引用`NavigationSubtreeDeleteRecord`的整棵子树`deleting`，新准入全被拒绝；恢复按batch冻结的每个Session locator打开control DB、收敛lease并分别隔离日期节点，直到全部目标完成才一次提交整树tombstone。未过期communication/observation窗口延后对应隔离节点GC，普通resolver始终不可见

#### Scenario: 新操作准入与删除只有一个线性化顺序

- **WHEN**child/board创建、collaboration mutation、用户或内部execution、source/ToolSet控制、communication acceptance或attachment pin与同一Session删除并发
- **THEN**操作的topology shared→Session gate准入与删除的topology exclusive→catalog deleting commit只有一个顺序：操作先行则已有可枚举lease由删除收敛，删除先行则操作不得产生thread、context、inbox/outbox、wakeup或附件副作用
- **AND**在workspace catalog已deleting而部分local fence仍active的崩溃窗口，普通业务reader与新mutation均返回`session_deletion_pending`；恢复继续原batch而不回退active

#### Scenario: 删除目标不因其它 Session 更新而丢失或卡死

- **WHEN**删除请求准备冻结目标树时，另一个Session或Folder先提交合法导航更新
- **THEN**删除事务从SQLite当前已提交revision重新验证精确目标集合，不覆盖无关节点；目标parent/revision已漂移则整个batch显式冲突并以新operation重试，不发布部分deleting或丢失其它更新

#### Scenario: 非规范或超长 ID 在接触文件系统前被拒绝

- **WHEN** API、Proto、Session link、catalog import或typed ref携带非36-byte canonical profile的Session/thread ID，ID含路径分隔、Unicode、百分号编码、`.`/`..`，或hex payload不满足UUIDv4 version/variant bit profile
- **THEN** 共享validator在catalog/path lookup和任何文件系统操作前返回明确的`invalid_session_id`或`invalid_thread_id`；不得清洗、截断、建立别名或创建部分目录

#### Scenario: main 与非 main thread 使用不同物理分桶

- **WHEN** Session 创建唯一 main thread，或创建 delegated/specialist/service durable thread
- **THEN** main thread的catalog locator固定为`threads/{main_thread_id}`，目录叶名等于真实thread ID；其它thread的locator固定为`threads/YYYY/MM/DD/{thread_id}`，日期来自不可变UTC `created_at`，不得额外增加hash shard
- **AND** 所有读取和写入均先通过 thread catalog/resolver 校验 locator；不得使用 `threads/main` 别名、显示名、目录扫描、当前日期或 `checkpoint_ns` 推断路径

#### Scenario: 活跃Session在逻辑文件夹间移动

- **WHEN** 一个Session在UTC创建日取得ID后被放入多层Folder、移动到另一Session之下或改名，期间main thread仍在执行
- **THEN** Session仍由SQLite locator解析到原`sessions/YYYY/MM/DD/{session_id}`日期节点，Folder不存在物理目录，main/child thread的rollout、sealed refs与运行中handle均不搬迁；breadcrumb与`parent_session_id`投影在单一SQLite事务后更新，fork/delegation lineage和Session kind不变

#### Scenario: 递归删除文件夹时中途崩溃

- **WHEN** Folder包含多个Session和后代Folder，递归删除在已提交整树deleting、只隔离部分日期Session目录时退出
- **THEN** 普通reader看不到部分存活子树；恢复只根据同库batch record中的精确目标/locator继续收敛并一次提交全部tombstone，不扫描日期桶、不遗失未隔离Session，非递归删除非空Folder仍明确拒绝

#### Scenario: 旧物理导航树迁入日期桶

- **WHEN** 显式迁移读取旧`session-catalog-index.json`、Folder manifest及嵌套`children/`树
- **THEN** 迁移先冻结旧索引和完整ID/父链/locator映射，验证日期/ID/正文/SessionThread完整性，在不可见staging写入新SQLite和日期节点，再以单一可恢复切换点发布；失败保留旧数据供审计且正常新runtime不得双读旧JSON、扫盘补节点或给旧物理路径建立别名

#### Scenario: catalog 损坏或备份落后

- **WHEN** 启动完整性检查发现workspace catalog损坏，或恢复的备份generation低于已提交Session/Folder操作
- **THEN** 工作区业务准入停止并给出明确损坏/代际缺口报告；只在维护模式核对备份、operation record和Session manifest，未登记日期目录保留供审计，不能自动成为active node或被GC清理

#### Scenario: canonical attachment 只保存 workspace blob 引用

- **WHEN** user input 或其它 canonical item 使用上传附件
- **THEN** item 只保存稳定 `attachment_id`/variant、digest、length、MIME/protection 和 availability，不保存 `.boxteam/attachments` 物理 locator；workspace attachment catalog 验证该 reference 属于当前 session/thread/view 后才允许读取正文

#### Scenario: subgraph namespace 不能越过产品边界

- **WHEN** 同一 product thread 运行 root graph 和多个 LangGraph subgraph checkpoint namespace
- **THEN** 它们共享该 thread 的 owner node 但各自按 `checkpoint_ns` 保存 framework state；另一个 product thread 即使使用相同 namespace 字符串也不能读取前者状态

### Requirement: 会话目录编辑必须支持乐观投影、持久入队和失败回退

会话目录的Web客户端 SHALL在同步用户操作后立即显示带pending状态的Folder/Session导航投影；该投影只由已提交workspace catalog快照加本地有序命令重放得到，不得修改后端权威镜像、Gateway catalog、SessionThread ContextStore/CSM、ToolSet或物理locator。服务端 SHALL只以workspace `navigation/session-catalog.sqlite`已提交node为业务事实，任何模型/后台Session操作 MUST重新通过catalog/lifecycle gate准入，不得读取客户端pending树。普通同步目录mutation API不得保留第二写入通道。

后端 SHALL提供typed导航operation批量入队：单个短SQLite事务在认证路由下校验workspace/actor、规范化参数与`client_operation_id`/preimage，持久化`NavigationMutationRecord(state=queued)`、单调workspace queue_seq、依赖关系和新Folder ID reservation后返回202及完整receipt；202只表示durable acceptance，不得显示为成功。服务端`operation_id` MUST等于经验证的`client_operation_id`，`created_by_operation_id`只在同一认证scope解析；`(gateway_id,workspace_id,actor,client_operation_id)` MUST唯一；同key同preimage重试返回原receipt/terminal，同key异preimage冲突，terminal后保留足以阻止迟到重放的compact tombstone。前端 MAY本地使用`client_ref`显示未确认Folder；同批后端分配canonical ID并返回映射，跨批依赖 MUST在前批accepted后用`created_by_operation_id`解析，不能把临时ID作为持久node ID。Backend在每个workspace按queue_seq通过跨进程fencing owner顺序执行，进程重启继续原operation，不重排已接受依赖；请求批次原子入队、各operation按因果独立提交，前置失败的后继 MUST从queued直接进入terminal `dependency_failed`且无业务副作用；其它状态闭集为`queued|running|committed|rejected|cancelled`。

worker MUST在实际执行时按最新已提交SQLite和`NavigationTopologyGate`重新校验目标node revision、父节点active、无环/同名、Session deleting与权限；`base_catalog_revision`只供快照/事件对账，不得对无关node变更做全局CAS。相同node及受前序创建/移动/删除影响的祖先或目标父链编辑 MUST显式建立operation依赖；同node连续编辑以依赖和前序结果revision防止交错；其它客户端修改同node时冲突明确拒绝，不覆盖已提交外部修改。普通操作的catalog mutation、terminal record、catalog revision与导航事件outbox MUST同事务提交。递归删除 MUST复用`NavigationSubtreeDeleteRecord`：preflight失败则operation rejected且无deleting；整树deleting commit即导航逻辑committed，不得回滚active，物理排空的`settlement=pending|repair_required|settled`另行报告。入队不得持topology gate等待整树预检/网络/模型；暂时背压须明确可重试并保持客户端outbox，不静默丢命令。

客户端 MUST在用户操作时同步创建内存pending intent并立即投影；随后按client_sequence将未对账intent/依赖写入按稳定gateway/workspace/principal隔离的持久local outbox，本地持久化成功前不得派发Backend入队请求，持久化失败必须撤销该intent及依赖并显示错误。UI以confirmed snapshot+pending overlay显示最新意图；刷新、跨tab或SSE恢复时通过精确operation ID查询服务端状态并重放。服务端 MUST提供按ID状态查询和revision-pinned catalog snapshot/分页、带独立`navigation` channel序号和可恢复cursor的终态事件；分页cursor必须绑定catalog revision，后续页若revision已变化则显式`revision_changed`并重新读取，不得跨HTTP持长SQLite读事务或混页；事件与状态更新在同一catalog事务提交。乱序、重复、遗漏、cursor gone或旧Gateway export不能覆盖更新的confirmed revision，必须重读权威snapshot/受影响分支及breadcrumb。pending项在搜索、breadcrumb、分页和计数中只能作为显式本地投影，不能伪造后端cursor、total或跨端已提交结果。

确定`rejected|cancelled|dependency_failed`后，客户端 MUST查询权威snapshot并移除失败intent及传递依赖，再对最新confirmed基线仅在本地投影重放仍有效的独立pending intent，不得修改已accepted命令的payload、preimage或operation ID；不得用整树逆补丁覆盖后续成功操作或其它客户端变更。错误 MUST显示受影响node、失败原因和可重新发起的入口，保留仍存在的展开/选择状态；缺失node定位最近有效祖先。HTTP超时/断线/202丢失属于`unknown`而非失败，必须保留pending标识并按同一ID查询/重试；不能直接回退或换ID重发。local outbox只在terminal结果与对应catalog revision完成对账后清理，不得把未确认状态标记success或提供无法确认安全性的纯本地撤销。

#### Scenario: 快速连续移动与独立操作

- **WHEN** 用户在前一个HTTP请求未完成时连续移动多个Session/Folder，并对同一node连续改名或移动
- **THEN** Web立即按输入顺序投影全部pending变化且交互不等待每次后台执行；同node命令按dependency/queue_seq提交，独立命令可在前一命令失败后继续，后端每个命令只应用一次

#### Scenario: 本地outbox写入失败不派发命令

- **WHEN** Web已同步显示pending移动但对应IndexedDB写入失败，用户随后刷新页面
- **THEN** 前端在失败当次立即撤销该intent及依赖并显示明确本地存储错误，未向Backend派发该operation；刷新后从权威catalog加载原状态，不出现后端迟到提交或静默丢失一个已发送命令

#### Scenario: 新建文件夹后立即移动Session进去

- **WHEN** 新Folder尚未收到202而用户已将Session拖入它，随后批量入队或跨批接受回执
- **THEN** 前端立即使用client_ref投影；后端在acceptance时分配稳定Folder ID并以operation依赖解析后续move，创建失败使move成为dependency_failed且两者从投影回退，不能留下幽灵目录或把临时ID写入catalog

#### Scenario: 并发客户端修改与局部回退

- **WHEN** 客户端A有三条pending命令，其中第二条因客户端B已修改同node而在执行时冲突，第一条已committed且第三条独立
- **THEN** A先取得第二条terminal和最新catalog revision，撤销第二条及其依赖而保留第一条已提交事实，按新基线重放第三条；不能回到A最初整树快照、覆盖B的变更或展示虚假的全批成功

#### Scenario: 网络未知结果与刷新恢复

- **WHEN** Backend已durably接受或提交operation但202/事件响应丢失，浏览器随后刷新或工作区切换后重开
- **THEN** 本地outbox保留同一ID和preimage，先查询服务端状态再对账；未知时同key重试只得到原receipt/terminal，不重复执行，也不因断网直接回退后被迟到提交反向移动

#### Scenario: 事件乱序、cursor gap与Gateway旧快照

- **WHEN** navigation事件重复、乱序或cursor gone，且Gateway export/旧分页响应落后于已确认catalog revision
- **THEN** 客户端按operation ID/event_seq去重并获取一致的权威snapshot/相关分支和breadcrumb，旧响应不得覆盖新基线；本地pending继续单独投影且不污染服务端分页cursor/total

#### Scenario: 递归删除预检失败与提交后排空失败

- **WHEN** 一个pending递归删除因pinned retention在catalog deleting前被拒绝，另一个已提交整树deleting后物理隔离进程崩溃
- **THEN** 前者从pending投影恢复最新active子树并报具体blocker，pending隐藏期间已打开聊天仍按已提交catalog工作、不提前销毁owner；后者保持逻辑删除、显示settlement待恢复/修复而不假回滚，重启只按原`NavigationSubtreeDeleteRecord`继续且不允许新业务进入子树

#### Scenario: Backend在入队和执行之间重启

- **WHEN** 202已返回但worker尚未执行时Backend退出，或者事务已提交但terminal SSE尚未送达时退出
- **THEN** 新owner按持久queue_seq/operation record继续或返回原terminal，目录事实、事件和幂等记录各一次，客户端状态查询可补齐丢失事件而不重复应用

### Requirement: main thread 与 durable child thread 必须支持真实的多任务协作模型

系统 SHALL 将每个 Session 的 main thread 作为默认用户任务和长期用户上下文入口，并允许用户使用不同 Session 的 main thread分别承担 Git 管理、代码分析、实施或报告汇总等独立任务。大型任务需要分工时，main thread MUST 在同一 Session内创建 durable child thread；child thread MUST 复用与 main thread相同的消息、执行、checkpoint、历史和恢复合同，但使用显式、可审计的能力配置。当前 child thread MUST 禁用 Goal，Goal 的 Session 默认入口 MUST 只定位 main thread。

child thread MUST 拥有独立 Turn、item、history、active view和执行状态。系统不得为方便委派而把完整 main-thread history隐式复制到 child，也不得把 child原始历史合并回 main；委派输入只能包含明确选择的任务 seed、引用和能力信息，完成汇报必须保留 parent/child thread、delegation、source execution/item provenance。用户直接向 child thread发送的消息属于该 child的真实用户输入并创建 child-local Turn，但不得改变 main history或自动启用 Goal。

每个SessionThread MUST持久化独立`ThreadExecutionQueue`。需要新execution的可信用户acceptance、delegation admission和内部wakeup在owner事务中分配不可重排的thread-local`admission_ordinal`及幂等identity；真实用户acceptance立即持久化自己的Turn/root/initial execution和`logical_turn_ordinal`，busy thread上的新root/seed使用queued visibility。每个active execution MUST冻结`ExecutionContextFence(admission_ordinal, logical_turn_ordinal, admitted_view_revision, execution_id)`及默认Turn级`ResourceActivationSnapshot`；后续model-call plan只能包含fence之前已可见history及当前execution因果产生的assistant/tool事实，必须按`causal_admission_ordinal`排除更晚未admit的root、entry-scoped seed/notice和execution。fence不得让独立ambient source的新revision丢失：文件/Skill/team revision仍由ResourceRegistry异步发布，但默认`turn`边界只在execution取得active slot时冻结一次，当前Turn后续tool-loop不得漂移；只有resource kind显式配置`model_call`时，后续model-call preparation才可从Registry内存snapshot激活新revision。两条路径都不得在请求阶段读取源。当前execution在未来root之后物理append的item仍按Turn因果/plan ordinal排在未来Turn前，`item_sequence`不得作为model/history全局排序替代。

同一thread任意时刻至多一个active root execution，其余保持runnable/pending并严格按ordinal FIFO准入，workspace容量调度只能延迟、不能越序。前一execution终态后，owner MUST 在同一事务激活下一entry、推进其visibility并冻结新fence，完成后才允许before-model和seal；不得让queued root提前进入旧execution。取消/失败保留ordinal及terminal queue/Turn record，不得删除后重排；未admit取消的真实用户Turn保持可审计cancelled且不调用Provider。属于现有execution的tool result、provider callback或resume signal必须绑定原execution并由generation fence提交，不能伪装成新queue entry；显式interrupt/cancel是控制操作，不是高优先级插队。不同thread拥有独立active slot，sibling child和其它Session可以并行。

#### Scenario: 多个 Session main thread 承担不同长期任务

- **WHEN** 用户分别在 Git 管理、代码分析和实施 Session中持续对话
- **THEN** 每个 Session默认消息、Goal、历史和执行只进入自己的 main thread；任一 Session的上下文、队列或状态不得隐式成为另一个 Session的上下文或协作状态

#### Scenario: 大型任务创建可恢复 child thread

- **WHEN** 分析或实施 main thread为避免上下文膨胀而委派一个需要多轮、可恢复、可查看的子任务
- **THEN** 系统在同一 Session创建独立 durable child thread，只传递显式任务 seed和引用；child完成后向精确 parent thread提交带完整来源的汇报，而不复制其全部历史或创建 main-thread真实用户 Turn

#### Scenario: 用户直接向 child thread 对话

- **WHEN** 用户打开一个 durable child thread并直接发送消息
- **THEN** 系统在该 child thread中创建正常 user Turn并按同一执行合同运行；该 thread保持 Goal disabled，main thread默认历史和当前执行不被替换或合并

#### Scenario: active child 收到用户新消息

- **WHEN**delegated child已有active execution，用户从右侧侧边栏向同一child发送真实消息，同时sibling child也有可运行工作
- **THEN**新消息立即按acceptance合同持久化自己的Turn/root与pending execution并取得下一个admission ordinal，DOM可显示queued状态；前一execution的后续tool-loop request/history projection由原ExecutionContextFence排除该root，直到前一execution终态后才原子推进visibility并按FIFO启动。sibling child不共享active slot且可并行

#### Scenario: queued root 之后提交的旧 execution 结果仍保持因果顺序

- **WHEN**Turn B已queued并取得较早的物理item sequence，而active Turn A随后提交tool result和final item
- **THEN**A的model context与历史仍按A root→A tool/final完整排序，B只在A终态后进入下一execution；storage sequence只作append坐标，不得使B正文泄露给A或把A结果显示到B之后

#### Scenario: queue admission 崩溃恢复不重排

- **WHEN**进程在queue entry提交后、active slot claim后或execution启动后退出
- **THEN**恢复按同一admission幂等identity和ordinal找回pending/active execution，至多启动一次且不越过更早entry；历史callback只能回到原execution

### Requirement: durable Thread 与 resident runtime 必须分离

系统 SHALL 将 SessionThread 的 durable identity、catalog metadata、GraphBinding、canonical history、checkpoint/context view、CSM control state和 ToolSet applied binding与进程内 resident runtime分离。当前 durable child thread的默认 idle unload threshold MUST 为30分钟；当 thread没有 active、runnable或pending execution，没有未收敛 model/tool call或 mutation transaction，没有 runtime lease，并且连续达到该阈值无 execution、消息准入或 runtime callback活动时，系统 MUST 只卸载可重建运行资源，不得删除、归档、重命名或改写该 thread的持久事实。

普通 thread列表、状态、历史和详情读取 MUST 使用 cold read路径，不得仅因用户查看 child历史就初始化模型、工具、graph或可写 ContextStore runtime。新的用户消息、内部 wakeup或其它需要执行的操作 MUST 在准入时以原 `(session_id, thread_id)` 和GraphBinding延迟重建唯一 runtime owner；新runtime generation必须重新解析并逐字段验证原`graph_id、graph_revision、graph_schema_hash、capability_profile_hash`，不得因为进程缓存已清空或registry已有更新而改用latest graph。卸载前后已经提交的上下文字节、prefix epoch、tracked registration、diff基准和ToolSet binding必须保持一致。idle unload不得被解释为Skill/上下文到期，也不得生成 context item。

系统 MUST 从唯一 runtime manager 提供只读 `ThreadResidencySnapshot`，至少包含精确 `session_id/thread_id`、`residency=cold|loading|resident|unloading`、执行状态、`last_activity_at`、`idle_deadline_at` 和去敏 `blocking_reasons[]`。该 snapshot 是观测投影，不是 canonical/CSM/team 状态；查询它不得唤醒 runtime。idle 判定 MUST 使用可注入的单调 Clock port，生产值始终是30分钟，测试可以通过 fake clock 精确推进到阈值而不等待真实时间。

#### Scenario: child thread 空闲30分钟后卸载资源

- **WHEN** 一个durable child thread连续30分钟没有活动，且不存在执行、未收敛工具调用、mutation或runtime lease
- **THEN** 系统释放其可重建的进程内Agent资源并把thread标记为cold；其历史、checkpoint、GraphBinding、CSM/ToolSet状态和用户可查看性保持不变

#### Scenario: 活跃任务阻止空闲卸载

- **WHEN** idle deadline到达时child thread仍有active/pending execution、未收敛model/tool call或持有runtime lease
- **THEN** 系统不得卸载该runtime；必须等待这些条件真实收敛并重新计算空闲窗口，不得伪造取消或完成状态

#### Scenario: 查看历史不恢复执行资源

- **WHEN** 用户只是在右侧侧边栏打开一个cold child thread的历史
- **THEN** 系统通过持久history projection返回内容并保持runtime cold，不创建模型client、工具实例、compiled graph、execution或新的context assembly

#### Scenario: cold child 收到新消息后延迟恢复

- **WHEN** 用户或精确内部wakeup向cold child thread发送新的可执行消息
- **THEN** 系统在execution admission阶段建立新runtime generation，按原thread identity和逐字段相同的GraphBinding恢复runtime，再提交或运行该消息；不得创建替代thread、改投main thread、静默选择latest graph或从当前文件覆盖旧上下文

#### Scenario: fake clock 精确验证产品30分钟阈值

- **WHEN** 测试使用同一 scheduler 的 fake Clock 把child idle时间从29分59秒推进到30分00秒
- **THEN** 29分59秒时residency仍为resident，到30分00秒且无blocker时才转为cold；测试不得改短产品阈值、sleep 30分钟或通过写库伪造状态

### Requirement: 跨 Session 协作必须只面向目标 main thread 且不共享协作状态

系统 SHALL 接受裸`session_id`、`boxteam://session/{session_id}`、`boxteam://workspace/{workspace_id}/session/{session_id}`或`boxteam://gateway/{gateway_id}/workspace/{workspace_id}/session/{session_id}`作为跨Session目标。Session link只是locator而非授权凭据。联邦部署 MUST支持中心Gateway hub通过SSH `-L`主动连接多个spoke，并在隧道内建立长期全双工WebSocket对等RPC channel；channel建立后hub与spoke均可主动发起request/response/event，无需反向SSH隧道或spoke直连。跨spoke路径最多为`B → A → C`，只允许一个hub transit且禁止继续多级转发。裸ID与未指定workspace的link只能在该有界hub拓扑内唯一解析，workspace-qualified link使用当前Gateway catalog identity，federated link使用稳定`gateway_id + workspace_id`。持久`connection_id`只作本地连接配置身份，瞬时channel instance/epoch/seq/ack和route locator均不得进入link、ResolvedSessionMainTarget、GlobalThreadAddress、outbox/inbox preimage或业务幂等key。target workspace MUST 从自己的权威catalog解析实际`main_thread_id`，hub MUST保留真实source gateway/Session/thread而不得伪装成业务source；零/多命中、未知gateway、未授权、不可达或main pointer无效时必须显式失败。

send首次解析并提交source outbox后 MUST 将稳定`GlobalThreadAddress`作为immutable target。后续网络重试、source恢复、WebSocket/SSH重连或hub重启只能刷新指向该地址的临时route lease，不得重新用裸ID discovery选择另一个同名Session，也不得因connection/channel/route revision变化生成新communication/preimage；稳定target不可达时保持原target并返回可重试路由错误。read/wait snapshot、selector和response envelope同样只能绑定稳定地址。hub丢失瞬时relay correlation后由source以原operation/communication重试并由target dedupe恢复，不得要求hub持久化消息正文或成为communication业务owner。

裸ID全拓扑解析 MUST 是有界exact-ID discovery：source查询自己的local workspace；spoke source通过唯一hub查询hub local workspace和其它active spoke，hub source直接查询自己的spoke。request必须携带完整visited gateway set、`max_transit_gateways=1`、`max_gateway_hops=2`和总deadline；hub可fan-out一次，spoke只查询本地且不得继续递归。workspace只做cold Session catalog lookup，不得加载thread runtime或扫描目录。只有在每个实际检查点按最新policy获准的候选可进入解析集；未授权存在与不存在必须统一为`target_not_resolvable`，多个已授权候选只返回不含locator的`target_ambiguous`和候选数并要求qualified URI。受认证、带catalog revision和短TTL的route hint只可优化路由；target workspace每次操作仍 MUST 重新验证Session与main pointer，hint不得成为业务事实、授权或第二份main catalog。

远端裸ID discovery MUST 使用channel-bound origin envelope和独立hub transit discovery grant。spoke B发起时hub A从channel registration确认真实origin并拒绝冒充，按最新transit policy向每个target spoke C签发绑定issuer A、origin B及source thread、audience C、预期operation、canonical session ID、visited path、request/nonce/deadline的短期grant。C验证受信hub、path和replay，并按自己的最新policy只查询本地registered workspace cold catalog；不得执行业务operation、读取history、解析child或继续递归。受认证response只能返回零、一个已授权qualified route hint，或不含locator的ambiguity count。聚合唯一candidate后 MUST 再执行完整operation授权；grant、response和hint都不是模型可见能力。

跨Gateway调用 MUST 使用channel-bound origin envelope和内部hub transit operation grant。spoke B发起时hub A从channel binding确认真实origin，按最新transit policy允许后向C签发绑定issuer A、origin B、transit path `[B,A,C]`、audience C、不可逆principal ref、单一operation、规范target、可选source全局thread地址、稳定operation invocation、request/nonce/expiry的grant。C不需要与B直接配对，但 MUST验证已登记hub A、grant完整性/audience/path/期限/replay，并按自己的最新policy授权“来自B、经A”的principal。C返回的受认证target response必须由A验证，再由A以绑定origin request、target response hash和path的relay envelope返回B；业务source始终为B。grant不得进入模型、URI、canonical item、outbox/inbox正文或普通日志；send receipt额外绑定communication/payload/acceptance。每个network attempt换request/nonce/grant但复用逻辑operation/communication，target dedupe为当前grant认证原acceptance而不重复注入。

hub和target MUST 在任何lookup或workspace forward前，分别将收到的origin/transit envelope以`(issuer_gateway_id, origin_gateway_id, audience_gateway_id, grant_kind, nonce)`写入Gateway control-plane的持久原子first-use replay registry并绑定grant/request/path hash。同nonce不同preimage或重复first-use MUST拒绝；结果未知的网络重试使用新request/nonce/grant，send业务幂等仍复用原communication。record保留到expiry、clock skew和transport replay margin之后，Gateway重启不得清空有效窗口；registry、credential或key不可验证时 MUST fail closed且零workspace副作用。hub只能持久化peer/connection registry、policy/replay registry及脱敏audit，不得保存替代双端outbox/inbox的业务状态。

Gateway federation权限的内置默认 MUST 对已认证、已登记在同一hub拓扑中的主体允许全部核心`discovery|send|read|wait|reply|transit`操作；限制规则为空且额外hardening默认关闭，用户不需要先配置allowlist。权限更新 MUST校验并原子发布带revision/hash的policy snapshot，无效候选明确失败且不部分生效，已有channel不得仅因policy变化重启。policy不得进入模型上下文、ToolSet、canonical item或sealed assembly；工具保持可见并在实际调用被拒时返回明确authorization错误。

身份认证、channel identity binding、grant/response完整性、audience/path、防重放、target解析和业务幂等 MUST始终启用。每次discovery fan-out、hub transit、target admission、read分页、wait状态/terminal披露、send retry及hub relay response返回都 MUST读取对应Gateway的最新policy revision。远端wait grant覆盖`effective_timeout + bounded_clock_skew`；cursor/selector不承载权限。运行中撤权阻止尚未durable acceptance的send并停止后续read/wait披露，返回`authorization_revoked`；已durable acceptance的send不回滚。重新允许后下一次实际操作立即生效，不改写历史结果或上下文。

跨Session当前只允许`send_message_to_session(target, content, kind="result", reply_to_communication_id?, delivery_policy="after_turn")`、`read_context(resource, ...)`和有界`wait_for_session(target, ...)`。send的`target`接受前述ID/URI，`content`必须非空，`kind`闭集为`question|reply|progress|result`，`delivery_policy`闭集为`after_turn|after_tool_result|after_interrupt`；输入 MUST NOT提供`communication_id`或`send_operation_id`，source outbox按软件持久化的稳定operation identity唯一分配communication，并返回resolved target、`communication_id`、durable delivery state、target acceptance ref和已知的`job_id/turn_id?`。模型可见schema还 MUST NOT提供`simulate_user`；只有受信外部UI/API ingress才能创建真实用户Turn。read的Session resource接受上述URI，软件 MAY 把裸ID规范化为`boxteam://session/...`。

`wait_for_session` MUST 接受至多一个`communication_id|job_id|turn_id`selector、`until=terminal|state_change`（默认terminal）和`timeout_seconds`（默认60、范围1–300秒）。有selector时只观察该对象，其中communication可在Job建立前从accepted等待到execution binding，不得因send→wait的排队窗口返回假idle；无selector时冻结准入时已有的active/runnable/pending identity集合，确实没有才返回idle，不订阅未来未知任务。结果状态闭集为`idle|pending|running|completed|failed|cancelled|timed_out`，timeout必须返回已观察identity/current state和可复用selector；未知selector返回`selector_not_found`而非idle。现有会持续订阅任意未来`AGENT_END`的`monitor_session_agent_end` MUST 被取代，不得与新wait并存为双语义。

`WaitForSessionResult` MUST 返回resolved target、顶层status、`observed[{selector_kind, selector_id, state, revision}]`、准入baseline revision和最新target revision；显式selector只返回该对象及communication到job/turn的binding。无selector空集合返回`idle`；非空集合 MUST 按`failed > cancelled > running > pending > completed`聚合且不得依赖数组顺序。`until=terminal`等待全部冻结对象终态后返回`failed|cancelled|completed`；`until=state_change`在任一冻结对象revision变化后按相同优先级返回当时状态及完整observed列表。`timed_out`只表示预算耗尽，observed仍保存真实当前状态；调用方不得从顶层status推断未返回对象。

wait MUST 由可注入的单调`Clock/DeadlineTimer`与目标状态订阅驱动，生产不得轮询数据库。测试 MAY 推进虚拟时间验证默认60秒和最大300秒，但 MUST NOT 缩短产品合同或改写communication、Job、Turn、residency及canonical时间戳。deadline与目标terminal/state-change并发时，系统 MUST 读取同一target owner的已提交revision：条件已满足则返回该状态，否则返回`timed_out`、该revision和可复用selector。

可恢复wait MUST 在RemoteObservationRecord保存版本化`DurableDeadline{timeout_seconds, admitted_at_utc, deadline_at_utc, monotonic_origin_id, monotonic_deadline}`。相同host boot/clock origin中的进程重启必须继续原monotonic deadline；origin变化时只能用可信UTC估算且剩余值不得超过原deadline或原timeout。无法证明仍有正剩余、检测到时钟回拨/超界或clock不可用时返回原baseline的`timed_out`或`deadline-clock-unavailable`，不得重新授予完整timeout。fake clock MUST 同时提供稳定origin与UTC映射，且不能和residency clock串扰。

`read_context` MUST 返回有界history/summary projection、revision/cursor和source refs，而非导入目标canonical item、Goal或CSM state。它作为模型工具时，返回projection只能由调用方thread的正常tool-call/result owner提交为一个带目标provenance的`tool_result` item，从而允许汇总Session生成周报；读取和等待不得修改或唤醒目标。send/read/wait结果 MUST 返回或审计实际解析的workspace、session和main thread身份，且重复投递必须遵守同一幂等目标。跨Session消息属于带source Session/thread和communication provenance的内部协作事件，不得伪造目标thread的真实用户Turn。

首个分页read MUST 分配稳定`observation_id/read_series_id`并签发版本化AEAD保护、opaque且可自验证的不可变`ReadContextSnapshot`/cursor envelope；密文内冻结该identity、resolved `GlobalThreadAddress`、active history view revision、可见item/Turn上界、projection/visibility policy hash、总读取预算、offset及到期时间。模型或客户端不得读取或篡改内部字段，解密/认证失败统一返回不含内部细节的`read-snapshot-invalid`。token不得写ContextStore/canonical history，target只可写独立受限的访问/operation control记录。后续页是新的source tool/API调用与新的`operation_invocation_id`，但 MUST由cursor恢复同一observation/read series并继续同一snapshot，即使目标新增消息、rewind或切换view也不得切到最新；cursor/snapshot不是capability，每页重新授权并校验target、principal、policy、projection、limit和预算。同snapshot参数冲突返回`read-snapshot-mismatch`，超过有界retention、认证key rotation或所需view/detail不可恢复返回`read-snapshot-expired`或明确loss，不得静默重开。空页、终页和source refs都携带相同snapshot revision；token固定最大上界，重放旧cursor只能重读相同范围，不能绕过产品总读取预算。

read/wait每个source tool/API调用 MUST 使用稳定`operation_invocation_id`：模型工具取source execution/tool invocation，受信UI/API取软件生成并持久提交的idempotency key；它是本次`source_call_id`，不得兼作跨多个分页调用的snapshot identity。source MUST由现有execution lease覆盖或先在source Session gate建立`federated_call` lease，再在网络前create-or-get `FederatedCallRecord`并冻结operation、稳定target、参数hash和page/selector语义；同identity不同preimage返回冲突。target MUST先在自己的Session gate验证active fence并建立`remote_observation` lease，才可在ContextStore/canonical history之外的Session-local有界operation store建立记录：read首调用以`(source_global_thread_address, first_source_call_id)`唯一create-or-get并分配`observation_id/read_series_id`，同call重试找回同一identity/snapshot；`RemoteObservationRecord`以observation identity保存冻结上界。后续页的新source call通过opaque cursor恢复该record，再以`(observation_id, page_ordinal)`唯一create-or-get绑定本次operation invocation与cursor hash的`RemoteObservationPageRecord`，且一个source call只能映射一个page、同ordinal不同preimage必须冲突。wait observation以`(source_global_thread_address, source_call_id)`唯一建立并冻结selector集合、baseline revision、effective deadline和subscription identity。grant/response同时绑定source call及适用的observation/page identity。source/target gate不得同时持有，网络attempt使用新grant但同一次source call MUST复用原operation identity。目标删除先行时不得创建snapshot/baseline/subscription；observation先行时删除把仍依赖node的调用收敛为明确target-deleted或原冻结结果并等待对应lease terminal。

source在target冻结observation后、持久化response或canonical tool result前退出时，恢复同一tool/API operation invocation MUST取得相同read page或wait baseline/terminal envelope，每个source call最多提交一个tool result；read下一页使用新的source call ID但继续cursor绑定的同一observation/read series，不得新开当前view或重新冻结上界。timeout后继续wait使用原selector、新operation identity和新observation。operation retention MUST覆盖本地execution恢复与远端snapshot/deadline窗口；过期返回`operation-retry-expired`而不静默建立新baseline。Session逻辑删除前 MUST terminalize全部非终态observation lease；catalog成为不可复用ID tombstone后，隔离节点及其中只读operation replay记录必须保留到全部未过期observation/communication恢复窗口结束，只能由匹配source address、source call、observation/page preimage且通过新授权的定点恢复路径访问，普通Session/history/runtime resolver不得打开。窗口结束后才可物理清理，之后原调用返回`operation-retry-expired`。operation store不得保存team/task/Goal、写入目标context或唤醒target runtime。

每次跨进程/服务器send MUST 使用两个本地持久事实而非伪造分布式事务：source thread node拥有`CommunicationOutboxRecord`，target main-thread node拥有`CommunicationInboxRecord`；Gateway只能逐跳授权、路由和保存Gateway级访问审计，不得成为communication业务状态owner或建立跨workspace共享数据库。两端记录都绑定source/target `GlobalThreadAddress=(gateway_id, workspace_id, session_id, thread_id)`、`communication_id`、immutable resolved target、payload hash、delivery policy和本端状态；outbox另绑定软件提供且模型不可见的`send_operation_id`并保存最新受认证target receipt，inbox保存target acceptance ref、ambient item/wakeup幂等键、`admission_id`、job/turn binding和terminal outcome。source outbox建立和target inbox acceptance MUST分别取得各自Session gate，并在各自`session-control.sqlite`建立覆盖本端communication的轻量`SessionOperationLease`；两个gate不得同时持有，网络与双端提交只组成可恢复saga。outbox只可`accepted → routing → target_accepted → execution_bound → terminal`，inbox只可`target_accepted → execution_bound → terminal`，任一侧可进入带原因的`failed|cancelled`；source只能用地址、communication、payload hash和acceptance ref匹配的受认证target receipt推进远端状态。

每次逻辑send MUST 在任何route前取得稳定`send_operation_id`：模型工具调用绑定source `(session_id, thread_id, execution_id, tool_invocation_id)`，受信UI/API入口绑定软件生成并持久提交的idempotency key；该字段不得由模型传入。source MUST先在lifecycle gate内验证active generation并建立communication lease，再在该lease覆盖的本地ContextStore事务中以`(source_global_thread_address, send_operation_id)`create-or-get outbox和唯一`communication_id`，并冻结resolved target、payload hash、kind、reply correlation及delivery policy；同operation不同preimage返回冲突，lease/outbox中间崩溃按稳定identity定点继续或终结。outbox未提交时 MUST NOT发起本地或远程route，因此target acceptance后、source receipt持久化前崩溃时，恢复同一tool/API invocation只能找回原outbox和communication ID。

source outbox和target inbox的dedupe scope都是`(source_global_thread_address, communication_id)`并各自在本地事务中强制；同key不同payload/target MUST返回冲突。target MUST先在自己的lifecycle gate内验证catalog/main pointer与active generation并建立target communication lease，再在该lease覆盖的同一ContextStore owner事务中提交inbox acceptance、唯一ambient item、wakeup idempotency和稳定`admission_id`；删除先关闭fence时不得建立lease/inbox，acceptance先行时删除必须发现并收敛该lease。JobService MUST 以`admission_id + admission preimage hash`create-or-get execution并由仍有效的target lease覆盖或重新准入，进程在Job创建后、inbox binding前退出时只能找回原Job/Turn并补写binding，不能在deleting generation晚到绑定。不得在event未提交时返回acceptance或在重启后创建重复Job。send只有收到target durable acceptance receipt后才返回成功；网络结果未知时返回可重试unknown outcome并要求复用原ID或原send operation。target在acceptance与execution binding之间重启后 MUST 从持久inbox/lease继续，wait通过target查询或受认证receipt沿同一communication恢复。系统不得引入跨workspace SQLite、两阶段提交或Gateway业务账本。

communication retention MUST NOT以短TTL破坏幂等。pending/running记录及reply/wait/audit所需因果字段完整保留；terminal正文/detail可以按明确policy回收，但拥有者Session删除前 MUST 保留最小tombstone，包含双端地址、send operation/communication/acceptance/admission identity、payload/preimage hash、correlation、terminal state和receipt验证字段。Session删除 MUST 先把本地未终态outbox/inbox收敛为带`source_deleted|target_deleted`原因的failed/cancelled并提交catalog deletion tombstone；canonical Session ID不得复用，迟到route不得改投其它Session或main thread。

target workspace MUST 由单一`InboxAdmissionWorker`恢复未绑定communication，而不得依赖read/wait触发。worker在acceptance提交事件和backend startup时从持久状态索引取得`target_accepted`且未`execution_bound`的inbox，以精确target main-thread address、wakeup idempotency key和`admission_id`取得或rehydrate owner并幂等create-or-get admission；成功时原子提交原job/turn binding，永久失败时提交failed outcome及受认证receipt。worker MUST NOT扫Session/thread目录、依赖内存future或建立第二ContextStore writer；并发worker必须以claim/lease或等价数据库约束保证单次有效admission。wait只观察已提交状态，不能因被调用才启动目标工作。

#### Scenario: hub 主动连接仍允许 spoke 反向发起操作

- **WHEN**中心Gateway A分别通过SSH `-L`和WebSocket建立到B、C的channel，B随后在A主动建立的channel上向C的qualified Session URI发起send/read/wait
- **THEN**请求按`B → A → C`路由、响应按`C → A → B`返回，C看到的source仍是B的真实thread；系统不要求B到A反向SSH隧道、B与C直连或hub持久化消息正文

#### Scenario: 默认权限直接开放核心联邦功能

- **WHEN**A、B、C已经认证并登记在同一hub拓扑，且用户没有配置federation规则或启用额外hardening
- **THEN**discovery、send、read、wait、reply和唯一hub transit默认允许，不能因为缺少allowlist而失败；身份、完整性、path、防重放和幂等校验仍然强制执行

#### Scenario: 权限热更新只在操作边界生效

- **WHEN**channel保持连接时管理员原子发布新policy revision，先撤销再恢复B经A访问C的read/wait权限
- **THEN**撤权后的下一页、状态披露或新调用明确返回`authorization_revoked`且不产生目标副作用，恢复后的下一次调用立即允许；channel、ToolSet、canonical history和已提交wire prefix保持不变，已durable acceptance的send不回滚

#### Scenario: backend startup 自动恢复未绑定 inbox

- **WHEN**target在durable acceptance后、execution binding前退出，随后backend启动且source没有先调用wait/read
- **THEN**InboxAdmissionWorker从状态索引恢复该记录并用原wakeup key只admit一次；wait稍后只观察既有binding/终态，不承担唤醒职责

消息`kind`闭集 MUST 为`question|reply|progress|result`。`kind=reply`必须提供`reply_to_communication_id`，且target inbox必须证明被回复communication的source/target与本次方向相反；其它kind携带该字段必须被拒绝。reply correlation只能建立消息因果关系，不得成为跨Session共享task/team状态。

跨Session transport或通信账本 MUST NOT 保存或同步team member、task assignment、role、Goal或其它共享协作状态。既有跨Session team/member/task状态 MUST 迁移为单个Session内部以child thread为成员的协作状态；另一个Session不能被attach为该board的持久成员。跨Session需要协同时只能通过上述显式操作和独立Session自身的状态推进。

迁移旧跨Session team成员时，系统 MUST 对每个legacy member显式选择“在coordinator Session内物化为target-local durable child并建立source Session lineage/mapping”或“冻结/脱离membership并保留原Session独立”。物化 MUST 对已终态或显式quiesce的source main-thread checkpoint/view使用migration-only `materialize_thread_copy`，复用target-local mapping/校验引擎但只在`BoardMigrationRecord`冻结的不可见staging中创建goal-disabled target child和完整target-local identity；该原语 MUST NOT创建新Session/main、写catalog/ledger、自行发布或暴露可执行owner。公开`context_fork`、`history_prefix_fork`和`full_rollout_copy`仍只创建新的target Session及其main thread，不得被board migration改写语义。source Session及其用户历史保持不变。active execution、runtime、lease或未收敛mutation不得复制；无法quiesce时只能选择freeze/detach或使整个migration失败，不能创建缺失上下文的空child。

`materialize_thread_copy` MUST把coordinator准入、source snapshot capture和coordinator最终publication分成不重叠的锁阶段。migration先在coordinator Session的exclusive gate内建立承担lease的`BoardMigrationRecord`后立即释放；再按record冻结顺序逐个source Session取得shared `SessionReadGuard`，一次最多持有一个，从catalog解析到关闭node/SQLite handle期间冻结绑定source lifecycle generation、view/checkpoint上界、artifact/detail清单与hash的不可变snapshot manifest和staging bytes，随后立即释放。source删除先关闭fence时整批migration MUST保持未发布并定点abort；guard先行时删除只等待该source capture，guard释放后不得等待child staging或board publication。最终publication另行取得coordinator gate并CAS原record/fence/board/catalog/member/task preimage。系统 MUST NOT同时持有coordinator gate、source guard、另一Session gate或两个数据库写事务，也不得在guard释放后重新打开source。

每个`MigrationChildCreationEntry` MUST在capture前预分配唯一`source_snapshot_id`和内部locator。capture在source SQLite固定read snapshot中冻结同一revision的view/checkpoint/control rows、`storage_commits`与各JSONL committed end offset，使用可验证SQLite snapshot/online backup复制数据库，并只读取offset以内JSONL及manifest点名且hash/length/capability验证通过的thread-local immutable detail。完成时先在该locator原子发布绑定operation/preimage、source lifecycle generation、database hash、逐文件offset/length/hash、view/checkpoint revision、detail manifest hash和attachment claim manifest hash的`SourceCopySnapshot(state=captured)`并durably flush；释放guard后才CAS entry为`source_captured`。冻结后的并发source append/view变化不进入该child。partial/prepared capture只能使原operation abort并定点清理；完整marker可在不回读source时恢复，禁止扫盘、补齐或同operation改用新revision。

board migration中的workspace attachment正文 MUST保持单一content-addressed blob而不进入child staging。source snapshot冻结logical attachment/variant、digest/length/availability、source owner/item ref和catalog revision，并以record绑定的IdentifierFactory预分配target child-local item/attachment identity与mapping。关闭source SQLite read transaction后、仍持source guard时，每个entry在单个attachment catalog事务中按copy operation/source ref/target owner ref create-or-get `ForkAttachmentClaim(preparing)`，验证source owner与blob正文并阻止GC；全部成功后才把有序claim ID清单/hash写入captured marker，中途崩溃按精确copy operation ID释放本operation claim。board发布前全部required claim必须成为`owner_reserved`并建立指向coordinator Session/target child/item的owner ref；resolver仅在child catalog active后允许读取。有required claim时`BoardMigrationRecord` MUST绑定有序claim ID/hash并在board可见性发布时进入非终态`published_pending_attachment_commit`；无required claim时可见性事务直接进入终态`published`。finalizer单独取得coordinator gate、复核active generation与publication preimage后，以不重叠的attachment catalog和session-control事务依次提交claim并把record推进`published`。coordinator删除先关闭fence时，删除排空按record中的精确claim ID释放reserved claim/ref，或验证committed claim属于冻结的child owner后按普通删除协议幂等、持久释放该owner ref，再把record推进终态`coordinator_deleted`；不得重建owner、保留child owner ref、伪装成成功`published`或隔离仍有非终态settlement record的节点。未发布失败按entry claim ID释放并进入`aborted`。source unavailable的非required历史ref可保留unavailable；required claim失败使整批board不发布。不得复制blob、复用source identity、扫描digest补claim或部分激活child owner。

#### Scenario: migration copy 与公开 full rollout copy 不混用

- **WHEN**legacy board迁移在既有coordinator Session中物化child，同时调用方也可使用公开`full_rollout_copy`
- **THEN**board路径只能由`BoardMigrationRecord`调用`materialize_thread_copy`生成未发布child，最终由coordinator本地事务发布；公开copy仍创建独立target Session/main，二者不共享target creation key、发布事务、catalog语义或可见性结果

#### Scenario: board migration capture 与 source 删除双顺序

- **WHEN**migration先取得某个legacy member source的shared `SessionReadGuard`
- **THEN**该source删除等待snapshot capture完成；guard释放后删除可继续，migration仅从冻结manifest/bytes准备child且不再访问source
- **WHEN**source删除先关闭fence
- **THEN**该member capture返回`source_session_deletion_pending|source_session_deleted`，整批board不发布并按record定点abort/清理，不能留下空child或部分mapping

#### Scenario: board migration 一次只持一个 Session 锁

- **WHEN**migration包含多个source member并最终发布到coordinator
- **THEN**coordinator record准入、每个source snapshot capture和coordinator publication是顺序且不重叠的阶段；任意时刻最多持有一个Session gate/guard和一个数据库事务

#### Scenario: migration capture 使用一致 committed 上界

- **WHEN**source在固定read snapshot后继续append item、提交terminal convergence或切换active view
- **THEN**child只从该`SourceCopySnapshot`冻结的SQLite revision、JSONL committed offsets和detail manifest生成；不得组合不同时刻的SQLite、JSONL、checkpoint channel或detail

#### Scenario: migration capture 中断不改用当前 source

- **WHEN**某个member的capture只写入partial staging便崩溃，或完整marker已durable但entry尚未CAS为`source_captured`
- **THEN**恢复只按该entry预登记locator定点abort/清理partial，或验证并继续完整snapshot；不得扫描其它目录、从当前source补齐或在同operation静默升级revision

#### Scenario: board attachment owner 与 child 一起保持不可见或完整可用

- **WHEN**migration child引用source attachment，claim已owner_reserved但board最终事务尚未发布，或board已发布但claim尚未标记committed便崩溃
- **THEN**前者正常resolver不能读取reserved ref且恢复会整批继续/释放；后者保持`BoardMigrationRecord`非终态，只有coordinator仍active且publication preimage一致才按同一claim确认target owner，所有已发布child attachment均可用，不出现部分member附件、重复blob或source删除后的悬空ref

#### Scenario: board attachment finalization 与 coordinator 删除串行

- **WHEN**board已经发布但claim仍为owner_reserved，coordinator Session删除与attachment finalizer竞争同一gate
- **THEN**finalizer先行时在释放gate前提交全部claim并把board record推进成功终态`published`，删除随后按普通owner ref释放；删除先关闭fence时由排空流程释放reserved claim/ref，或验证committed claim归属后持久释放其child owner ref，再把board record推进终态`coordinator_deleted`，copy recovery不得重建owner ref、保留child owner ref或让已删除child重新可见

board migration MUST使用不可见staging和单一coordinator可见性提交点，而非跨thread/workspace分布式事务。在创建任何child staging目录前，系统 MUST先于coordinator `session-control.sqlite` create-or-get不改变thread catalog/collaboration ledger的`BoardMigrationRecord(state=preparing)`，冻结operation/preimage、coordinator Session lifecycle、旧board/catalog revision，并为每个target内嵌唯一`MigrationChildCreationEntry`，包含child ID、最终relative locator、内部staging locator、GraphBinding/capability、source checkpoint/view、lineage/mapping和预期artifact manifest/hash；同operation不同preimage MUST冲突。该entry是batch child唯一creation journal，普通ThreadCreationWorker MUST NOT枚举、发布或启动它，且不得为同一target创建独立`ThreadCreationRecord`。随后才准备、验证并durably flush所有记录内的child artifact、lineage、权限和hash，把全部child原子rename到记录冻结且尚未被catalog引用的最终locator；恢复只能枚举该record中的有限target。全部rename完成后，在同一`session-control.sqlite`事务中CAS验证coordinator仍active、旧board/catalog revision及member/task preimage未漂移，再更新thread catalog、collaboration ledger全部locator/member/task mapping；无required attachment claim时record直接推进为终态`published`，否则推进为非终态`published_pending_attachment_commit`并在claim结算后转为`published`。该事务是唯一可见性提交点，后续结算不得改变board成员集合。CAS失败 MUST不覆盖并发修改、不重基或部分发布，并只可定点清理record列出的全部staging/final orphan后标记`aborted`；重新迁移使用新operation/preimage。任一rename失败不得发布；rename后、发布前崩溃的不可见orphan因preparing record已先存在而可定点恢复/清理，不得扫盘吸收；发布后、terminal response前恢复同一结果。发布前正常reader只见旧board，发布后只见完整新board。不得原地复用source identity、部分发布一个board或让正常runtime继续读写旧跨Session状态；任一member无法决定、授权、复制或验证时整个board保持未发布并产生审计报告。

#### Scenario: batch child 只有一个 publication journal

- **WHEN**board migration为多个target child建立staging并发生普通ThreadCreationWorker并发扫描
- **THEN**每个target只存在所属`BoardMigrationRecord`内的creation entry，普通worker无法认领或单独发布；只有board最终事务可同时发布全部child/member/task mapping

#### Scenario: 实施 Session 通知 Git 管理 Session

- **WHEN** 实施Session根据用户指令向Git管理Session的ID或link发送完成消息
- **THEN** Gateway将消息路由到Git管理Session所在workspace，由目标workspace解析并投递其main thread；消息保留来源和幂等信息，但两个Session不建立共享team/task/Goal状态

#### Scenario: Gateway 重连不改变已冻结目标

- **WHEN**source outbox已冻结远端main地址，随后WebSocket channel epoch、SSH连接或remote route revision变化并重试原send operation
- **THEN**Gateway只刷新指向同一gateway/workspace/session/thread的临时route lease，持久connection config ID、communication和preimage保持不变；若原稳定target不可达则显式失败，不重新用裸ID选择其它同名Session

#### Scenario: 汇总 Session 跨工作区读取和等待

- **WHEN** 报告汇总Session获得多个可达workspace或server上的Session link，并读取其main-thread结果或等待当前任务完成
- **THEN** 每个操作独立解析目标main thread并返回带source ref的授权投影或被捕获execution的状态；返回值可作为汇总thread的单一tool result供模型生成报告，但目标canonical history/Goal/CSM state不被复制或改写，目标runtime不因read/wait被加载

#### Scenario: 裸ID discovery 与 operation 授权分两阶段

- **WHEN** 汇总Session只提供远端裸Session ID，source尚不知道目标workspace
- **THEN**唯一hub先验证source channel/origin，再凭hub transit discovery grant让各target spoke只执行本地受限exact lookup并返回受认证候选结果；唯一target确定后才使用绑定完整target、origin和`B → A → C`路径的operation grant。discovery grant不能直接read/send/wait或继续递归，重复nonce在hub/target重启后仍被拒绝且无workspace副作用

#### Scenario: read 分页期间目标继续变化

- **WHEN**汇总Session读取第一页后，目标main新增Turn、rewind或切换active view，再使用原cursor读取后续页
- **THEN**第二页使用新的source call/operation invocation，但opaque cursor恢复首请求的同一observation/read series；所有页面仍来自冻结的ReadContextSnapshot revision和上界，新变化不混入。权限撤销、参数篡改或snapshot过期分别显式失败，不以cursor绕过授权或静默切换到新snapshot

#### Scenario: read 或 wait 回执前 source 退出

- **WHEN** target已按source call与observation identity冻结read page或无selector wait baseline，而source在持久化远端response/tool result前退出并恢复同一invocation
- **THEN** 新grant只恢复原RemoteObservationRecord/PageRecord及同一结果，该source call至多提交一个tool result；目标新增Turn/Job不得改变原snapshot或selector集合，record过期时明确失败而不重开

#### Scenario: send 后立即 wait 不丢失排队任务

- **WHEN** `send_message_to_session`返回communication回执后目标Job尚处于accepted/queued，调用方立即以该`communication_id`调用`wait_for_session`
- **THEN** wait继续跟随该communication的delivery与execution binding并等待精确Job/Turn收敛，不返回idle、不改成订阅其它未来任务

#### Scenario: wait 超时后可继续观察同一工作

- **WHEN**`wait_for_session`在默认60秒或显式1–300秒预算内未达到请求的terminal/state-change条件
- **THEN**返回`timed_out`及resolved target、观察identity、current state和原selector；后续调用复用该selector继续等待，不创建新通信或订阅无关Job

#### Scenario: wait 期间进程或主机时钟域变化

- **WHEN** wait observation已保存deadline后backend进程重启，或host boot/clock origin发生变化
- **THEN** 相同origin继续原monotonic剩余预算；新origin只使用可信UTC且绝不延长原deadline，无法安全恢复时明确超时或返回`deadline-clock-unavailable`，不重新计算完整60/300秒

#### Scenario: fake deadline timer 不篡改产品等待语义

- **WHEN**验收通过注入的单调fake timer把默认等待推进到60秒或最大等待推进到300秒，同时目标状态可能在deadline边界提交
- **THEN**系统不真实sleep、不缩短配置、不轮询数据库，并按target已提交revision裁决terminal/state-change或`timed_out`；虚拟时间不得改变其它业务时间戳或runtime residency

#### Scenario: target acceptance 后重启不会重复投递

- **WHEN**target已durable接受communication但尚未绑定execution时进程重启，source随后以同一ID重试send并wait
- **THEN**target inbox只保留一次ambient event，恢复原target acceptance ref并最终补齐同一job/turn binding；不同payload或target复用该ID明确冲突

#### Scenario: source 在 target acceptance 后丢失 receipt

- **WHEN**target已提交acceptance，但source在验证并持久化receipt前退出，随后同一tool/API invocation被恢复
- **THEN**source按原`send_operation_id`找回已提交outbox和communication ID，以新grant重查原acceptance；target只返回当前grant认证的原acceptance receipt，不创建第二个inbox、ambient item、Job或Turn

#### Scenario: Job 已创建但 inbox binding 尚未提交

- **WHEN**target进程在JobService已create-or-get execution之后、inbox写入job/turn binding之前退出
- **THEN**InboxAdmissionWorker按原`admission_id`恢复同一Job/Turn并补写binding，不创建第二个execution

#### Scenario: terminal communication 回收后仍保持幂等

- **WHEN**terminal communication正文已按retention回收，但拥有者Session仍存在，随后收到相同ID的迟到重试或冲突payload
- **THEN**最小tombstone分别返回原terminal identity/outcome或幂等冲突，不重新投递；删除Session后其canonical ID也不得被新Session复用

#### Scenario: reply correlation 不能伪造来源

- **WHEN**`kind=reply`缺少`reply_to_communication_id`、引用不存在/未授权的communication、或原communication方向与本次回复不相反
- **THEN**target在创建outbox/inbox或ambient item前拒绝请求；非reply kind携带该字段同样拒绝，不通过文本或Session ID猜测因果关系

#### Scenario: Agent 跨 Session 消息不能伪造用户

- **WHEN** 模型调用`send_message_to_session`
- **THEN** 工具schema不包含`simulate_user`，目标仅接收有provenance的ambient/pending event；任何试图传入该字段的请求都被schema拒绝

#### Scenario: 裸 Session ID 解析有歧义

- **WHEN** 同一裸`session_id`在Gateway catalog中没有唯一、已授权且可达的目标
- **THEN** 操作只在有界exact-ID discovery内返回`target_ambiguous`和已授权候选数，并要求使用可唯一定位的Session link或workspace信息；不得披露候选locator、区分未授权存在与不存在、任选目标、递归查询peer或扫描服务器

#### Scenario: federated link 经过真实 Gateway hop

- **WHEN**验收以三个隔离Gateway进程配置`B ⇄ A ⇄ C`loopback hub-and-spoke channel，并由B调用C上带`gateway_id`的Session URI
- **THEN**请求经过B origin channel、A唯一transit和C target授权后才到目标workspace，响应按原路返回且source仍为B；反向SSH、B/C直连、进程内resolver stub、直接请求远端workspace backend或共享Gateway home都不能作为通过证据

#### Scenario: 默认允许不能关闭联邦协议校验

- **WHEN**即使权限默认允许，federated请求仍使用错误origin/audience/path、过期或重放grant、失效channel identity/peer credential，或者命中用户运行时启用的明确deny rule
- **THEN**hub或target Gateway在转发workspace前明确拒绝并记录脱敏decision audit；目标inbox、canonical history、runtime和outbox远端状态都不推进，模型与普通日志看不到grant或credential

#### Scenario: 跨 Session 状态管理被拒绝

- **WHEN** 调用方尝试把其它Session作为team member、向跨Session协议写入member/task/role/Goal状态，或让wait创建持久共享任务板
- **THEN** 系统拒绝该操作并指向同Session child-thread协作或显式send/read/wait入口，不通过message metadata静默恢复旧状态机

#### Scenario: legacy team board 迁移不产生混合成员

- **WHEN** 旧board含有以另一Session表示的member，且迁移选择将其物化为coordinator Session的child thread
- **THEN** 系统从已终态/quiesce的source main checkpoint/view在不可见staging中full-copy出goal-disabled child并创建target-local identity和source lineage，只在全部member验证完成后通过coordinator catalog/board单一事务发布全部member/task引用；原Session及历史仍独立，runtime中不存在空child、部分board或既指向Session又指向child的成员

#### Scenario: legacy team migration 在发布边界崩溃可恢复

- **WHEN**migration在若干child staging artifact完成后、coordinator可见性提交前崩溃，或在该提交成功后、staging清理前崩溃
- **THEN**恢复分别只暴露完整旧board或完整新board，并按migration journal继续/清理；正常reader永远看不到staging child、混合member或跨库半提交状态

#### Scenario: legacy board 在 staging 期间发生并发修改

- **WHEN**`BoardMigrationRecord`冻结旧board/catalog revision后，coordinator被删除或任一member/task/board revision在最终发布前变化
- **THEN**发布CAS失败并保留并发修改，旧board继续完整可见；系统只按record清理全部不可见target并标记`aborted`，不得把staging结果重基到新board或发布部分child

### Requirement: durable GraphBinding 可验证重建，不持久化进程对象

每个 `SessionThread` SHALL 持久化 `GraphBinding(graph_id, graph_revision, graph_schema_hash, capability_profile_hash)`。重启恢复 MUST 通过受注册的 graph factory 解析完全相同的 binding 并校验 revision/hash；系统不得序列化或恢复 Python `CompiledStateGraph`，也不得在 binding 缺失或不匹配时静默选用最新 graph。进程缓存若复用 graph topology，缓存对象不得捕获 Session、thread、工具实例、provider request 或可变 execution state；这些值 MUST 在每次 model invocation 通过显式 `ThreadRuntimeBinding` 注入。

#### Scenario: 缺失精确 graph revision

- **WHEN** checkpoint/SessionThread 引用的 `graph_id + graph_revision` 未注册或 schema hash 不匹配
- **THEN** 恢复返回 `graph_binding_unavailable` 并保持已提交 rollout/context 不变，不创建替代 graph 或新的 checkpoint

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

`accepted_ingress_id` 与 `acceptance_idempotency_key` 各自在 `(session_id, thread_id, accepted_ingress_id)` 与 `(session_id, thread_id, acceptance_idempotency_key)` 范围内唯一，并各自一对一指向一个 accepted Turn。相同 key、相同 ingress、相同 payload hash 和相同 source branch 的重试返回原 Turn/root/initial execution；相同 key 但 ingress、payload 或 branch 不同，或相同 ingress 但 key/payload 不同，必须返回明确 acceptance idempotency conflict，不创建第二个 Turn/root，也不修改原记录。检查与创建必须在同一 SQLite 事务内完成。

系统 SHALL 在接受真实用户输入时创建或恢复一个 `TurnRecord`，并为新 Turn 保存唯一的 `root_input_item_id` 和对应的物理 item sequence。权威 `TurnRecord` 至少包含 `turn_id`、thread-global 且不可重排的 `turn_ordinal`、`accepted_ingress_id`、thread 内唯一的 `acceptance_idempotency_key`、`root_input_item_id`、`root_input_item_sequence`、`initial_execution_id`、`last_execution_id?`、`final_item_id?`、`status` 和不可变的 origin `source_branch_id`。`root_input_item_id` MUST 指向 `semantic_kind=user_input` 且 `producer_ref.producer_kind=user` 的 canonical item；Turn 的 identity 和起点不得从 `wire_role`、LangChain message 类型、首个物理 item、`message_group_id` 或 Provider model call 推断。一个 Turn 可以包含多个 execution/model-call，并允许在没有新用户输入时 resume 原 Turn。

`acceptance_idempotency_key` 在 `(session_id, thread_id, acceptance_idempotency_key)` 范围内唯一。同一 key 重复提交相同 ingress payload hash 时，系统 MUST 返回原 `turn_id`、root item 和 `initial_execution_id`；同一 key 对应不同 payload 时 MUST 返回幂等冲突，不得创建第二个 Turn/root。acceptance-time 的 Turn、root item 和首次 execution 必须作为同一可重试提交边界可见。

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

系统 SHALL 将 `turn_id` 作为owner thread内逻辑用户交互的全局不可变local identity，将`turn_ordinal`作为首次acceptance分配的thread-global ordinal，将`source_branch_id`作为首次接受该Turn的origin branch。在同一`(session_id, thread_id)`内，派生branch/view只能复制对既有Turn/item的引用，不得复制或重新编号`TurnRecord`；`resume_turn`仅按Turn.status转移表复用同一Turn并创建新的execution/model-call lineage；`history_replay`是唯一可以在同一owner thread的历史view中复用source Turn/root的回放操作，且不创建execution；`replay_as_new_turn`必须创建新的Turn/root/acceptance/initial execution并以`replay_of_turn_id`关联，不能把该操作解释为原Turn的`dispatch_replay`。跨session fork按后文`GlobalEntityRef`和target-local mapping合同建立新的target Turn/item identity。fork后新接受的用户输入或显式`replay_as_new_turn`才在target main thread创建新的`turn_id`、`turn_ordinal`和origin`source_branch_id`；`context_view_turns.logical_turn_ordinal`是view-local顺序，必须与thread-global`turn_ordinal`分开存储和解释。

#### Scenario: history_replay 复用历史 Turn

- **WHEN** 从已有 Turn 的历史 anchor 在同一 owner thread 内创建 history view 并执行 `history_replay`
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

上述owner-thread identity复用不适用于跨session fork。跨session引用 MUST 使用`GlobalEntityRef=(session_id, thread_id, entity_type, local_id)`；`(session_id, thread_id)`是实体正文、控制状态和索引的owner namespace，裸`local_id`只在其owner thread内唯一。`context_fork`、`history_prefix_fork`和`full_rollout_copy`都必须在target Session的main thread创建新的target-local Turn、root item、item sequence/JSONL offset、tool invocation/call/attempt、execution、model-call、assembly、view、branch和操作anchor，并在不可变的`fork_entity_mappings`/等价provenance中保存source ref到target ref的一对一映射；source ref/offset只能作为lineage/audit坐标，不能被target reader当作canonical identity或直接打开。source overlay epoch/base/delta、canonical ambient item和assembly detail也必须建立target-local mapping；target reader不能读取source detail path。三种模式分别复制source active view的有效范围、指定inclusive/before anchor的有效prefix、以及全部canonical/legacy rollout和SQLite control/channel state；三者都创建新的target active branch/view，target使用target-local logical ordinal和committed offset。target rollout一律为v2；v1 source的message identity/sequence/offset仅保留为`legacy_source_ref`，不成为target v2 identity。

跨 session fork 的 source/target retention 与 lineage 独立：`fork_origins` 保存两侧 session、source checkpoint/view/branch、mode、mapping version、overlay/detail mapping 和 relationship；detached fork 在物化提交后不依赖 source，pinned fork 只为审计保留 source retention ref，target active plan 仍只读 target-local copy。source 未终态 Turn/active execution/未终态 assembly 在 `context_fork`/`history_prefix_fork` 中导致 preflight 拒绝且不创建 target；`full_rollout_copy` 则允许完整历史 target，但对应 target Turn 标记为 `cancelled`、reason=`fork_source_runtime_not_copied`，不可运行。复制后的 target Turn 只有在 Turn.status 转移表允许时才可复用 target `turn_id` 做显式 `resume_turn` 并创建新的 target execution/model-call/assembly；`history_replay` 只在相应 owner namespace 的 history view 中复用已有 Turn/root 且不创建 execution，跨 session fork 本身不以它复用 source 裸 identity；显式 `replay_as_new_turn` 才创建新的 target Turn/root/acceptance/initial execution，并在 target active view 登记新的 `logical_turn_ordinal`，可以复制或引用已映射 source history 作为上下文前缀，source Turn/root 不成为新 Turn 的 root，并以 `replay_of_turn_id` 关联，且不属于原 Turn 的 `dispatch_replay`；普通或 historical `cancelled` Turn 的 `resume_turn`/`dispatch_replay` 均返回 `turn_not_resumable`。target 新用户输入创建新的 target Turn/root/initial execution，且 target `turn_ordinal` 大于已复制 Turn 的最大值。required detail 无法复制时 fork 失败，optional detail 显式为 unavailable。

跨session fork还必须映射Turn的acceptance identity：source`accepted_ingress_id`和`acceptance_idempotency_key`通过`fork_entity_mappings`映射为target main thread新的target-local值，分别满足`(target_session_id, target_thread_id, target_accepted_ingress_id)`与`(target_session_id, target_thread_id, target_acceptance_idempotency_key)`唯一。复制值必须标记`identity_origin=fork_copied`，source值只在source`GlobalEntityRef`/lineage中保留；target普通ingress不得复用这些copied key，target新输入必须由target ingress重新生成新值。对copied Turn的显式`resume_turn`仅在Turn.status转移表允许时复用target Turn和copied acceptance关联，并创建新的target execution/model-call；`history_replay`不创建execution，显式`replay_as_new_turn`才创建新的target Turn/root/acceptance/initial execution并以`replay_of_turn_id`关联，且不属于原Turn的`dispatch_replay`；普通或historical`cancelled` Turn的`resume_turn`/`dispatch_replay`均返回`turn_not_resumable`。重复fork使用同一fork idempotency key返回原mapping，source/target acceptance mapping不得重复或覆盖。

本change所有跨Session fork/copy MUST限制在同一workspace。`pinned`模式必须在source capture前由target creation/fork journal预分配target operation，再单独取得workspace topology shared及source `SessionLifecycleGate`，于source `session-control.sqlite`提交绑定fork、两侧GlobalThreadAddress、target operation/preimage、source generation及view/detail范围的`ForkRetentionClaim(state=preparing)`和retention占位。source删除先提交catalog deleting则fork零可见副作用失败；claim先提交则删除在整树catalog deleting前返回`source_retention_operation_pending|source_retained_by_fork`。target提交后只能CAS激活同一claim，不能首次补写。abort/target删除以target durable release intent驱动，释放target gate后单独取得source gate释放claim，再确认target terminal；禁止同时持两端gate/事务，preparing claim不得按墙钟自动清理。Gateway federated grant不授权fork、远端业务库读取或retention mutation。

`ForkRetentionClaim`自身 MUST按`operation_kind=fork_retention`提供完整lease identity/preimage/generation/fencing/state/recovery字段并与retention占位原子提交，不得另建平行lease。

#### Scenario: pinned fork 不在 target commit 后首次建 pin

- **WHEN**pinned fork完成source capture与target materialization
- **THEN**source中已经存在capture前提交且与本次target operation/preimage一致的preparing claim；target提交后只能激活它，缺失或冲突时fail closed且不得宣布fork成功

#### Scenario: federated Session link 不能执行 fork

- **WHEN**调用方用Gateway-local或federated Session link请求`context_fork`、`history_prefix_fork`或`full_rollout_copy`到另一workspace/server
- **THEN**系统明确返回unsupported/authorization错误，不把read grant升级为copy能力、不打开远端SQLite或创建retention；send/read/wait合同不受影响

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

本要求中的 `storage_commits` 合同固定为：`commit_kind` 只能是 `acceptance`、`assembly_sealed`、`item_convergence` 或 `terminal_convergence`，另有正交的 `commit_mode`=`item_bearing|metadata_only`。一个逻辑 SQLite 事务只能对应一条 storage commit，item-bearing commit 可以覆盖多个 JSONL item；`acceptance` 必须为 item-bearing，`assembly_sealed` 必须为 metadata-only，`item_convergence` 必须为 item-bearing，`terminal_convergence` 可为 item-bearing 或 metadata-only。同一 terminal outcome 不能同时写 item-bearing 和 metadata-only 两条 commit；同一 `(session_id, thread_id, commit_kind, subject_id, idempotency_key)` 重放时 payload、outcome、mode、record count 或 offset span 不同必须报幂等冲突。

系统 SHALL 将存储提交区分为 sealed-before-dispatch 与 terminal convergence 两个阶段，并在 SQLite `storage_commits` 中记录 `commit_id`、`commit_kind`、subject identity、idempotency key、`jsonl_offset_before`、`jsonl_offset_after`、`jsonl_record_count` 和 outcome。`database_meta.committed_jsonl_offset` 是每个 SessionThread rollout 的单一权威 committed boundary；`storage_commits.jsonl_offset_after` 是同一边界的不可脱离副本，不是 reader 可择一使用的第二权威。每条已提交 commit 必须满足 `jsonl_offset_before` 等于该事务开始时的 database meta offset、`jsonl_offset_after >= jsonl_offset_before`，且事务成功后 `database_meta.committed_jsonl_offset == jsonl_offset_after`；下一条 commit 的 before 必须等于上一条已提交 commit 的 after。插入 storage commit、更新 database meta offset、item index/view/checkpoint/control outcome 必须在同一个 SQLite 事务内完成；item-bearing commit 之前必须完成 JSONL durability barrier。provider 空输出、失败和 execution lost 等没有 JSONL item 的 terminal convergence MUST 使用 metadata-only/control commit 原子记录 assembly、execution/model-call outcome 和 Turn 状态，metadata-only 的 before/after offset 相等且 record count 为零。启动恢复必须同时校验 database meta、storage commit chain、JSONL 文件大小和 item index；缺失、等值不成立、回退、越界或链断裂时必须停止并报告 commit-boundary conflict，不得由 reader 自行选择一个 offset 继续。

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

middleware MUST NOT 原地修改既有 canonical item，也不得把对 `ModelRequest.messages`、`system_message` 或 `tools` 的修改作为 canonical history 写回。middleware 只能返回一种或多种结构化结果：请求级 prompt/context overlay、ToolSet及其assembly policy snapshot、context view transform，或需要持久化的 canonical item append intent。Gateway federation operation policy不属于middleware contribution。只有最后的 LangChain/Provider adapter 可以将这些结果编译为目标请求对象。

#### Scenario: 临时 system prompt 注入

- **WHEN** workspace instructions、skill、memory 或运行时身份只对本次 model call 生效
- **THEN** middleware 返回带来源、版本、顺序和 hash 的 prompt contribution，ContextRequestPlan 使用它生成 system/developer input，但不会生成普通历史 message 或修改既有 canonical item

#### Scenario: 持久化提醒注入

- **WHEN** 中断提醒、compaction summary 或其它事实必须进入后续上下文
- **THEN** middleware 返回 append intent，由 writer 追加新的 canonical item 并由 active view 选择它，不能覆盖旧 item 或旧 prompt contribution

### Requirement: Canonical item、request-only context 和 wire role 必须分离

系统 SHALL 将 context plan 中的引用区分为 canonical item reference 和 request-only reference。静态 system prompt、动态 skill 说明、workspace/environment snapshot、memory injection 以及 tool definition 默认 MUST 以 request-only contribution 或 tool-set snapshot 参与本次请求；只有显式声明为后续上下文事实时，才允许追加对应的 canonical item。Provider wire role 只是编码投影，不得改变引用的生命周期、来源或关系。

已被某次 sealed request 使用的 source revision 可以作为 request-only overlay 的稳定 base；后续 source revision 不得静默替换该 base。需要跨 checkpoint 延续的 source diff 必须通过结构化 `PersistItemIntent` 追加为 ambient `runtime_notice` item，或明确标记为只对下一次 request 有效的 request-only delta；二者都必须带 source revision、base/delta 关系和 hash，不得退化为无 provenance 的普通 message。

#### Scenario: 首次 root 的多个上下文合并到一个 system wire item

- **WHEN** 第一条真实用户消息尚未出现，首次 assembly 把当时已启用的静态 system prompt、skill metadata/activation 和环境状态选为 initial-root contribution
- **THEN** ContextRequestPlan 保留各自独立的 request-only reference、来源和顺序，root compiler可以将它们编译为唯一的system/developer/instructions wire item，但历史不会把合并结果视为一条canonical message；真实用户消息后的完整source、delta、runtime notice或恢复item不适用该合并规则

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

被 `selection` 绑定且 `included=true` 的每个 `ContextContribution` MUST 在对应 `assembly_id` scope 内取得独立、稳定、不可重写的 `contribution_ordinal`；unsealed plan 中仅登记为 registry 的 contribution 和 `included=false` optional omission 不带该 binding。`ContextRequestPlan.selection` MUST 是 Saver 冻结的有序列表，每个 canonical/request-only/overlay/tool-set source ref 取得唯一 `plan_ordinal`，并记录 `selection_kind`、source overlay/base/delta role、visibility/protection/availability、omission/loss 及可得 source identity；只有 `included=true` 才强制 source revision、逻辑 content length 和恰一个 source hash token。request-only included entry 才强制 sealed detail_ref，tool_set entry 使用独立 ToolSetRef manifest，canonical entry 不带 detail_ref。`assembly_id` 是该次 plan/selection 的唯一持久范围。`ContextAssemblySnapshot`/SQLite `assembly_item_refs` 必须持久化这些字段及 content hash；omitted entry 的 detail、正文 hash/length 和 contribution ordinal 可以 null/未分配，已知 metadata 必须一致。重启后按 ordinal 恢复，不能按 `created_at`、`contribution_id`、物理邻接或 projector 本地规则猜顺序。LangChain、native Provider 和 Web history 必须消费同一 selection；只有 initial root 中启用的 contribution 可以合并到唯一 system/developer/instructions item，第一条真实用户消息后的 request-only/source/runtime/compaction contribution 必须保持独立 user-role item且不得与相邻 user item合并；Anthropic 中途 system role仅保留TODO。ToolSetRef只能进入Provider tools/tool-config，二者都不得被projector无条件prepend到canonical messages。optional omitted entry只保留omission/loss metadata并跳过正文/工具定义，required omission必须拒绝seal/dispatch。顺序或ordinal不一致必须返回plan-order-integrity error。

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
| `canonical_history` | `ContextRef.ref_type=canonical_item` | 仅解析同一owner thread的`item_catalog`；source revision、logical length、恰一个 hash token 必填；`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 必须 NULL，`base_delta_role=none` | 仍保留 `canonical_item` tag/id、plan ordinal、omission/loss/availability 和可得 identity；history/restore 跳过 canonical message，不读当前 item |
| `request_only` | `ContextRef.ref_type=request_only` | `detail_ref` 必须同 assembly；若 contribution-backed，`contribution_id`/`contribution_ordinal` 必填且唯一指向同一 plan 的 contribution manifest；`base_delta_role=none` | 保留 `request_only` tag/id、plan ordinal、omission/loss/availability；detail、正文完整性字段和 contribution binding 可空，不回退当前 middleware/source |
| `overlay_base` | `ContextRef.ref_type=request_only` | 必须 contribution-backed，非空 `contribution_id`/`contribution_ordinal`/detail/source revision/length/hash；`base_delta_role=base`、`source_overlay_epoch` 必填，并绑定完整 base | 保留 request-only tag/id、plan ordinal、`base_delta_role=base` 及可得 epoch/identity；不应用 delta/base，不以当前 source 替代 |
| `overlay_delta` | `ContextRef.ref_type=request_only` | 与 overlay_base 相同，`base_delta_role=delta`、`source_overlay_epoch` 必填，另校验 `from_revision`/`to_revision`/diff algorithm/version/`diff_hash` chain | 保留 request-only tag/id、plan ordinal、`base_delta_role=delta` 及可得 epoch/identity；不应用 delta、不重建 overlay |
| `tool_set` | `ToolSetRef.ref_type=tool_set` | 仅解析同 plan/assembly ToolSetSnapshot manifest；`base_delta_role=none`，`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 必须 NULL | 仍保留 `tool_set` tag/id、plan ordinal、omission/loss/availability 和可得 identity；history/restore 不生成工具定义，Provider 不回退 registry 或空 tools |

`ContextRef.ref_id` 仍是 canonical `item_id` 或 request-only `plan_item_id`，不是 contribution identity；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref` 与 source manifest 定位正文/detail及其 source revision、length/hash，不要求 contribution identity；included 且 contribution-backed 的 request-only/overlay 才必须用非空 `contribution_id` + `contribution_ordinal` 唯一定位 `contribution_manifest` 的正文/detail、source revision、length/hash 与 ordinal，且 overlay 按矩阵必须 contribution-backed。canonical/tool_set 严禁 contribution_id/ordinal。缺失、重复或 selection kind 与 union tag 不匹配统一返回 `plan-order-integrity`；source/hash mismatch 返回 `source-mismatch`，request-only detail/contribution 不可用返回 `detail-unavailable`；required source 的 omission 直接拒绝 seal/dispatch。

`ref_type` 是本 change 在 plan、snapshot、SQLite assembly ref 和 projector 输入中的唯一序列化判别字段，闭合集合只有 `canonical_item | request_only`；不得持久化 `ref_kind` 或 `request_only` boolean 作为 ContextRef/selection discriminator，也不得把 `item_id`/`plan_item_id` 当作第二个判别字段。统一身份字段是 `ref_id`：canonical ref 的 `ref_id` 必须等于 immutable item 的 `item_id`，request-only ref 的 `ref_id` 必须等于 plan 内 target-local 的 `plan_item_id`。ContextRef显式带`session_id`和`thread_id`；canonical ref的scope是`(session_id, thread_id, item_id)`，request-only draft ref的scope是`(session_id, thread_id, plan_id, ref_id)`。ContextRef不承担assembly binding：draft中`assembly_id`不存在/必须为NULL，只有sealed`ContextSelectionEntry.assembly_id`、`ref_manifest`和detail manifest才建立`(session_id, thread_id, assembly_id)`scope。request-only ref的`detail_ref`在draft中必须为NULL或仅表示待seal的source detail；Saver seal时才解析/物化为`{session_id, thread_id, assembly_id, detail_id}`，并逐字段写入selection/manifest。optional omission的selection ref仍必须保留tag/id，但可作为未解析完整manifest的typed identity stub；其source revision、length、hash、detail_ref可为空，已知值必须与manifest一致，且不得被restore/projector当作可读正文。旧模型若仍使用`ref_kind`/boolean，只能由legacy ingress adapter在边界处归一化，后续composer、storage、snapshot和projector不得继续消费别名。canonical ref必须且只能解析到同一owner thread已提交`item_catalog` item，但该完整解析要求只适用于`included=true`；optional`included=false`的canonical tag/id stub不声称item已可读。request-only ref在unsealed阶段只能解析到同一`(session_id, thread_id, plan_id)`registry；只有`included=true`的sealed entry才解析到该assembly的contribution/detail manifest，并由显式`contribution_id`定位contribution，`included=false` typed identity stub不解析assembly detail/contribution；同一scope内不能以另一类型重复注册。

工具集合使用独立的 `ToolSetRef`，不扩展 `ContextRef.ref_type`：`ContextRef` 的闭合集合仍只有 `canonical_item | request_only`，而 selection 的 `ref` 是一个 tagged union，可为一个 `ContextRef` 或一个 `ToolSetRef`。`ToolSetRef.ref_type=tool_set`仅在`selection_kind=tool_set`时合法，`ref_id`必须解析为同一`(session_id, thread_id, plan_id)`registry中的target-local`tool_set_snapshot_id`；unsealed registry的`assembly_id=NULL`，seal后的selection binding必须带当前`assembly_id`。ToolSetRef/ToolSetSnapshot manifest 必须保存非空 `source_revision`、`tool_set_schema`、`tool_set_schema_version`、`tool_policy_version`、逻辑 `content_length`、恰好一个 `content_hash`/`redacted_stable_digest`、`protection` 和 `availability`；hash/length 覆盖 `{ "tool_set_schema":"tool-set-ref", "tool_set_schema_version":"v1", "tools":<按稳定 tool_id 排序的 schema/config entries>, "tool_policy":<规范 policy>, "tool_policy_version":"v1" }` 的 RFC 8785 JCS UTF-8 bytes，普通 hash 使用 `sha256:jcs:v1`，受保护正文由 protected manifest 保存内部 hash 并向普通 reader 暴露 stable digest。每个 included tool-set selection entry 必须逐字段等于 `tool_set_manifest[]`，`base_delta_role=none`、不绑定 `contribution_ordinal`，但与其它 entry 共用 assembly 内唯一的 `plan_ordinal`；optional omitted tool-set entry 只保留 `ref_type=tool_set`/`ref_id`、plan ordinal、omission/loss、availability 和可得 identity，manifest 正文字段、detail_ref 与 contribution ordinal 可以为 null/未分配，已知 metadata 必须与 registry 一致且不投影 tools。缺失、重复、类型/selection_kind 不匹配或 manifest 不一致返回 `plan-order-integrity`，source/hash/length/schema/policy version 不一致返回 `source-mismatch`，不可用/无权限返回 `detail-unavailable`。ToolSetRef 只投影到 Provider 的 tools/tool-config；history 仅消费受 visibility 策略允许的 selection metadata/摘要，不生成 canonical message、Turn item 或 request-only 正文。

`ContextSelectionEntry.contribution_id` 是 request-only/overlay contribution 到 `contribution_manifest` 的唯一显式映射：included且contribution-backed时必须非空，且只能解析同一`(session_id, thread_id, plan_id)`registry的一个`contribution_id`；该manifest必须进一步提供正文或最终`detail_ref`、source revision、logical content length、hash token与assembly-bound`contribution_ordinal`。`ContextRef.ref_id`仍是request-only的`plan_item_id`，不等于也不替代`contribution_id`；restore、LangChain/native Provider/Web history必须按selection entry的`contribution_id`读取对应manifest/body，再校验entry与manifest，不得按ref_id、detail_ref、hash或ordinal搜索/猜测。canonical_history与tool_set entry（包括omitted）的`contribution_id`、`contribution_ordinal`、`detail_ref`必须为NULL；只有omitted request-only/overlay entry可保留已有`contribution_id`，且必须与同一manifest一致，不得新分配contribution_id/ordinal或触发正文/detail读取，没有既有映射则为NULL。

`ContextContribution` 固有就是 request-only contribution：其持久化字段 `request_only` 必须存在且恒为 `true`，只作为 contribution manifest 的不变量，不能用来判别 ContextRef union；缺失或为 `false` 必须拒绝。`ContextRef` 和 selection 仍禁止该 boolean，必须使用 `ref_type=request_only`。`ContextContribution.contribution_kind` 的闭合集合只有 `prompt | overlay_base | overlay_delta | notice`，不存在合法的 `tool_set` contribution。Provider tool definitions 只能通过同一 plan/assembly 的 ToolSetSnapshot/ToolSetRef manifest 绑定；`ContextContribution`、request-only body、`contribution_ordinal` 和普通 ContextRef 都不得代表 tool definitions。v2 遇到 `contribution_kind=tool_set` 必须返回 `contribution-kind-unsupported`；只有一次性 `legacy_import_v1_to_v2` migration reader 可以原样保留到 migration report/quarantine，但不得转换为 ContextContribution、ToolSetRef 或 Provider tools，也不得被正常 runtime 调用。

`ContextRequestPlan` 的生命周期和 identity 也属于本结构合同：`create_context_plan`先在owner-thread namespace中创建唯一`plan_id`，状态为`unsealed`；未seal的plan可以登记`refs[]`、`tool_set_refs[]`/`contributions[]`，但必须保持`assembly_id=NULL`、`selection=[]`，因此不产生`ContextSelectionEntry`、`plan_ordinal`或可dispatch的assembly。draft registry的ContextRef约束为`UNIQUE(session_id, thread_id, plan_id, ref_type, ref_id)`；canonical ref可被多个plan查询复用，但request-only ref不得跨plan复用。draft ContextRef不解析assembly/detail physical path，新增detail只留在内存ledger或source typed ref。Saver只有在seal preflight通过ContextRef/ToolSetRef/contribution registry manifest的source、length、hash、visibility/protection和顺序校验后，才在同一提交边界分配thread-local`assembly_id`，生成selection/`plan_ordinal`与assembly-bound`contribution_ordinal`，将required request-only body写入detail store，并保存最终detail_ref与`ContextAssemblySnapshot`。seal失败不得留下可用assembly、selection或detail binding；原plan仍为`unsealed`，可以修正后重试。`(session_id, thread_id, plan_creation_idempotency_key)`负责plan创建幂等，`(session_id, thread_id, plan_id, seal_idempotency_key)`负责seal幂等；相同preimage返回原identity，不同preimage分别返回`plan-idempotency-conflict`或`assembly-idempotency-conflict`，不得把`plan_id`当成`assembly_id`。跨session fork不得复用source的plan/ref/detail identity，必须在target main thread创建target-local plan/assembly/ref/detail，并仅在fork lineage/audit保存source GlobalEntityRef。成功后plan与snapshot保留两个identity，逐字段不可变且一对一；新的selection必须创建新的plan/assembly。未选择任何source的plan只有在已分配assembly后才允许以空`selection` seal，空selection仍属于该assembly scope。

`ContextRef.content_length` 的来源按 selection union 分流：`ref_type=canonical_item` 的 `canonical_history` 只能来自已提交 `item_catalog.payload_length`；`ref_type=request_only` 的 request-only/overlay selection 必须来自同一 assembly 的 sealed detail/contribution source manifest；`ToolSetRef.ref_type=tool_set` 的 tool_set selection 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest。三者都不能从 JSONL line offset/length、wire message 长度或当前文件猜测或替代。`ContextRequestPlan.refs[]`、`.tool_set_refs[]`/`.contributions[]`只是source registries，`selection[]`是唯一的顺序、inclusion和loss authority；selection entry只能在对应`(session_id, thread_id, assembly_id)`scope中存在。每个entry必须恰好解析到一个tagged-union source ref：非`tool_set` selection解析到一个`ContextRef`，`selection_kind=tool_set`解析到一个`ToolSetRef`，且同一ref/contribution/tool-set snapshot不得在一个assembly重复选择。所有entry的`ref_type`、`ref_id`、visibility、protection、availability、base/delta role和`source_overlay_epoch`必须逐字段等于对应registry/manifest；只有`included=true`时才强制`source_revision`、逻辑`content_length`和恰一个`content_hash`/`redacted_stable_digest`，且request-only才强制非空、同assembly的`detail_ref`，contribution-backed entry才强制`contribution_ordinal`。`included=false`仅允许optional omission，仍保留tagged source ref、`plan_ordinal`、`omission_reason`、`loss`、`availability`和可得source identity；source revision、length、hash token、detail_ref、contribution_ordinal可以为null/未分配，已知metadata必须逐字段等于manifest。缺项、重复、union类型/selection_kind不匹配或任意顺序/绑定不一致返回`plan-order-integrity`，canonical或tool-set source不一致返回`source-mismatch`，request-only detail不可用或不一致返回`detail-unavailable`。`selection_kind=tool_set`不得引用`item_catalog`、普通`ContextContribution`正文或ContextRef，ToolSetRef正文只来自tool schema/config manifest及其hash。`ContextAssemblySnapshot`持有seal时selection的不可变副本以及`ref_manifest[]`/`tool_set_manifest[]`；`assembly_item_refs`必须以`UNIQUE(session_id, thread_id, assembly_id, plan_ordinal)`、`UNIQUE(session_id, thread_id, assembly_id, ref_type, ref_id)`、`UNIQUE(session_id, thread_id, assembly_id, contribution_id)`和`UNIQUE(session_id, thread_id, assembly_id, contribution_ordinal)`保护一致性，omitted entry不分配detail/contribution ordinal。plan registry还必须以`UNIQUE(session_id, thread_id, plan_id, tool_set_snapshot_id)`防止ToolSetRef重复。`contribution_ordinal`是assembly binding，不是contribution跨assembly的全局属性；同一contribution在不同assembly重新绑定时取得新的ordinal，但同一sealed assembly内不可重写。三种projector和history只能消费该selection副本，不得重新从registry排序或prepend；omitted canonical/request-only/tool_set entry只保留metadata/loss，分别跳过正文、detail和工具定义，不从当前source回退或生成空值。

`ContextContribution.content_hash` 的正文 preimage 固定为 `{ "contribution_kind": <contribution_kind>, "body": <typed body> }` 的 `sha256:jcs:v1` RFC 8785 JCS bytes；`body` 在 draft 可以来自内存 ledger 的 inline typed value 或 typed source ref，sealed manifest 必须从已解析的 body/detail_ref 复核。preimage 不包含 contribution identity、ordinal、assembly、时间或 wire role。敏感/受保护正文不能暴露普通 hash 时，ref 使用恰一个 owner-thread-scoped `redacted_stable_digest=hmac-sha256:thread:v1:<64位小写hex>`，protected manifest 仍保存内部 content hash 并完成校验；canonical item ref 不使用该替代 token，始终使用 immutable item `content_hash`。

#### Scenario: 统一 selection 被三个读取面复用

- **WHEN** Saver 为同一 active view 生成 LangChain restore、native Provider request 和 Web history projection
- **THEN** 三者读取相同的 `selection[{plan_ordinal, ref}]`；`ContextRef` 负责 canonical/request-only/overlay source，`ToolSetRef` 负责 `selection_kind=tool_set`，并按相同的 `contribution_ordinal` 应用 request-only 与 base→delta overlay。LangChain/native Provider 只能把 ToolSetRef 编码到 tools/tool-config，history 只能保留受策略控制的 ToolSetRef metadata；只能在 wire 编码阶段合并角色，不得改变 selection 或把工具定义变成 message

#### Scenario: 重启后正文与 ordinal 一致

- **WHEN** 进程在 assembly seal 后重启，或 `included=true` source file 被覆盖、删除或替换
- **THEN** Saver 依据已提交 ref、source revision、length 和 content hash 恢复原正文与 ordinal；缺失/覆盖/错误 source 返回 `source-mismatch` 或 `detail-unavailable`，不发送错误正文，不静默 loss
- **AND** `included=false` 的 optional omission 只恢复 tagged ref、plan ordinal、omission/loss/availability 和可得 identity metadata，不尝试恢复正文或 ordinal，也不把 omission 当作 source mismatch

#### Scenario: contribution-backed request-only ref 唯一定位正文

- **WHEN** 一个 sealed plan 同时包含多个 request-only/overlay contribution，且每个 included selection entry 的 `ref_id` 都是独立的 `plan_item_id`
- **THEN** 每个 entry 通过非空 `contribution_id` 唯一解析到同一 `(session_id, thread_id, plan_id)` 的 `contribution_manifest`，再由该 manifest 定位正文或最终 `detail_ref`、source revision、逻辑 length、hash token 与 `contribution_ordinal`
- **AND** restore、LangChain/native Provider 和 Web history 按 `plan_ordinal` 读取同一 entry/manifest mapping；缺失、重复、ref/contribution 不一致或 ordinal/body/hash 不匹配返回 `plan-order-integrity`、`source-mismatch` 或 `detail-unavailable`，不得按 `ContextRef.ref_id`、detail_ref、hash 或 ordinal 猜测另一条 contribution
- **AND** canonical_history/tool_set entry（包括 omitted）的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为空；只有 omitted request-only/overlay entry 即使保留已有 `contribution_id` 也只校验同一 manifest identity，不新分配 contribution_id/ordinal，不读取正文/detail；没有既有映射则为 NULL

### Requirement: selection_kind 与 ref_type 必须使用唯一兼容矩阵

系统 SHALL 在任何 source lookup、detail 解析或 projector/restore 之前按下表校验 `ContextSelectionEntry.selection_kind` 与 tagged-union `ref`；`included=true` 和 `included=false` 都必须满足同一行的 tag/type 关系。矩阵外组合必须返回 `plan-order-integrity`，不得根据 payload、wire role、`ref_id` 或当前 registry 猜测另一种生命周期：

| `selection_kind` | 唯一合法 ref | `included=true` 合同 | `included=false` optional 合同及读取行为 |
|---|---|---|---|
| `canonical_history` | `ContextRef.ref_type=canonical_item` | 只能解析同一owner thread的`item_catalog` item；source revision、logical content length、恰一个 hash token 必填；`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 为 NULL，`base_delta_role=none` | 仍保留 `canonical_item` tag/id、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 item identity；正文不解析、不生成 canonical message |
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

本合同中的 detail store 物理路径冻结为 resolved thread node 下的 `rollout/context-plan-details/<assembly_id>/<detail_id>`。`detail_id` 是 assembly 内 target-local 的不可变物理叶名；`detail_ref` 是规范化为 `{session_id, thread_id, assembly_id, detail_id}` 的逻辑 typed reference，由 session与thread catalog/path resolver唯一解析，不是物理路径别名。所有跨 session fork或跨thread materialization都必须生成 target-local detail_id/detail_ref并保留source ref到target ref的lineage；source path不得被target reader直接使用。任何父级symlink、realpath containment越界、敏感detail普通plaintext、required detail缺失或source/content hash不匹配都必须在seal/dispatch前显式失败。

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

- **WHEN** `sensitive=true` 的 detail 试图写入普通文件，或从 resolved SessionThread node 到 assembly/detail target 的任一父组件是 symlink、realpath 不在该 workspace/session/thread containment 内
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

同一`prefix_epoch`内的replay、branch/view读取、checkpoint restore和普通source edit MUST完整继承上一sealed assembly的wire bytes，并且只能在尾部追加新的canonical/source item。只有首次组装、实际compaction、rewind重建和ToolSet hard rebase可以登记并在下一份成功sealed assembly中应用不继承旧字节前缀的新epoch；`overlay_materialized`也只能在这四类边界内发生。rewind/compaction可先提交view和`PendingPrefixEpochTransition`，但pending记录没有wire bytes且不是applied epoch。rewind目标checkpoint若仍保留tracked registration但最新已注入revision已被移出active view，下一次真正model-call preparation必须从activation coordinator冻结的ResourceRegistry内存snapshot取得当前published revision，并把完整user-role revision、首个新epoch assembly与transition消费原子提交；snapshot不可用时保留view/transition但不产生半item或dispatch，且不得读文件、网络或其它provider。snapshot/untracked不自动恢复。普通source edit只追加delta。source base/detail不可恢复、source删除或source detail读取授权变化、revision/hash mismatch、Provider/projector profile不兼容或required detail缺失必须返回invalid/mismatch并阻止dispatch，不能用当前source静默重建旧request或以显式refresh暗中改写前缀。Gateway federation operation policy不属于source读取授权，也不参加该reconciliation。

每次会改变运行时上下文的操作都必须比较 history view 与 source overlay 两类状态，并形成 reconciliation outcome：`history_view_changed`、`overlay_reused`、`delta_appended`、`overlay_materialized` 或 `overlay_invalid`，同时记录 `prefix_epoch_reason`。只有合法新 prefix epoch 内的 `overlay_materialized` 才能改变 source base。source 不可读、source detail读取授权改变、revision/hash mismatch 或 detail 缺失时，系统必须显式返回 source-mismatch/detail-unavailable，不能用当前 source 静默重建旧 request。

#### Scenario: `AGENTS.md` 变化不破坏稳定缓存前缀

- **WHEN** request A 已使用并 seal 了 `AGENTS.md` 的 revision A，随后文件变为 revision B
- **THEN** 下一次普通 request 继续引用 A 作为 base，并追加带 A→B provenance 的 delta；不得直接把 system base 替换为 B，也不得把未标注来源的 `HumanMessage` 当作 delta

#### Scenario: rewind 到 diff 之前恢复 tracked source 的当前完整 revision

- **WHEN** source overlay 已有 `A(base) + A→B(delta)`，用户 rewind 到不包含最新已注入 revision 的较早 history view，且目标 checkpoint仍保留tracked registration
- **THEN** rewind先提交view/pending transition；下一次真正model-call preparation从冻结的Registry activation snapshot取得published revision B，并把对应独立user-role item、新source overlay epoch、首个rewind prefix epoch assembly与transition消费原子提交；不得读取当前文件、越过rewind cutoff重新注入旧A→B delta，snapshot/untracked也不得自动恢复

#### Scenario: 非 rewind 的只读 history view 不物化 source base

- **WHEN** history projection、history-prefix read或checkpoint restore只读取既有canonical history选择，且未执行compaction、rewind重建或ToolSet hard rebase
- **THEN** 系统不得新建prefix epoch或物化source；若需要继续dispatch，只能精确复用已提交source overlay与wire prefix

#### Scenario: source overlay 失效必须失败关闭

- **WHEN** source base/detail缺失、source不可读、revision/hash不匹配或Provider/projector profile与既有prefix不兼容，且当前操作不在四个合法新epoch边界内
- **THEN** reconciliation返回`overlay_invalid`或明确的source/detail/stable-prefix错误并阻止dispatch；不得读取当前文件后返回`overlay_materialized`

#### Scenario: 多次变化保留有序 delta 链

- **WHEN** 在物化前 source 依次从 A 变为 B、再变为 C
- **THEN** plan 保留 A base、A→B 和 B→C 两个有序 delta，使用每个 delta 的稳定幂等键；重启后可以按引用恢复到 C，而不要求重新读取当前文件

#### Scenario: source overlay 只在合法新 prefix epoch 物化

- **WHEN** 首次组装、实际compaction、rewind重建或ToolSet hard rebase已建立合法新prefix epoch，并且该边界的reconciliation决定以activation snapshot中已发布的tracked revision C替代可见base+delta链
- **THEN** 系统创建以C为完整base的新`source_overlay_epoch`并把post-user内容保留为独立user-role item；旧A base与增量只作为不可变审计记录，不在新active plan中重复应用。该规则不允许修复或伪造任何旧sealed assembly

#### Scenario: source 未进入 request 时不制造 diff

- **WHEN** workspace skill 的 `SKILL.md` 在当前 request 中从未被加载或使用，随后只发生 body 变化
- **THEN** 系统不为该未使用 source 额外注入 delta；只有已进入 request 的 skill metadata/body 才建立 base/delta lineage

#### Scenario: source 详情不可用时显式失败

- **WHEN** overlay 需要的 source revision/detail 被删除、越权或 hash 校验失败
- **THEN** 普通历史仍可读取已提交 canonical item 和安全 provenance，但 exact replay/依赖该 overlay 的新 assembly 返回明确的 source-mismatch 或 detail-unavailable，不静默使用当前 source

### Requirement: 历史 view 变化与运行时 source 变化必须独立重协调

系统 SHALL 将一次运行时上下文表示为相互独立的 `history_view_revision`、`source_overlay_epoch`、source revision set、`ToolSetSnapshot`/Provider-projector assembly policy snapshot、`prefix_epoch`/reason 和 assembly identity。rewind、replay、fork、checkpoint restore、compaction、source edit、skill/environment/memory 变化、tool schema/可见性/执行policy变化以及附件上下文可用性变化等会影响有效上下文的操作，必须在下一次 Provider dispatch 前通过统一的 `ContextReconciliation` 比较上一份已提交 snapshot 与当前状态，并生成明确 outcome：`history_view_changed`、`overlay_reused`、`delta_appended`、`overlay_materialized` 或 `overlay_invalid`。只有首次组装、实际compaction、rewind重建和ToolSet hard rebase可以改变prefix epoch；其它变化必须保留旧wire bytes并尾部追加或显式失败。source/ToolSet/Provider-projector assembly policy变化不得伪装成普通history message或隐式创建Turn。Gateway federation operation policy明确排除在该表示与reconciliation之外：它不修改ToolSet、assembly或prefix，只在discovery/send/read/wait/transit等实际操作边界读取最新revision。reconciliation未完成或返回invalid时不得发起Provider request。

#### Scenario: rewind 创建显式重建 epoch

- **WHEN** rewind 隐藏canonical history尾部并重建active view
- **THEN** rewind事务推进`history_view_revision`并登记pending rewind transition；下一次model-call preparation才从冻结的Registry activation snapshot取得published revision并原子seal首个`epoch_reason=rewind`assembly，保留tracked registration但最新source revision已移出view时在同一事务追加对应完整user-role revision，snapshot/untracked不恢复且不读当前源

#### Scenario: 被 rewind 隐藏的 delta 不跨 cutoff 复活

- **WHEN** rewind 后A→B ambient delta不再属于active view，而registration仍为tracked
- **THEN** reconciliation用冻结的Registry activation snapshot中published revision的新user-role item恢复有效source状态，旧A→B只保留lineage/audit且不重新进入active plan；若registration为snapshot/untracked则不恢复，不得读取当前文件补造revision

#### Scenario: 非文本上下文变化使用同一重协调边界

- **WHEN** tool schema、工具可见性、环境状态、memory、附件权限或 provider/projector capability 发生变化
- **THEN** 普通source变化只追加独立user-role delta；有效ToolSet变化在safe boundary执行hard rebase；其它会破坏已提交wire prefix的policy/profile变化显式失败。assembly记录变化原因和hash，不把这些变化追加为真实用户/assistant history，也不在非法边界物化overlay

#### Scenario: 重协调失败阻止请求

- **WHEN** history view 已切换但 source revision、ToolSet assembly policy 或所需 detail 无法与上一份 snapshot 对齐
- **THEN** 系统返回 `overlay_invalid` 或对应 mismatch/detail-unavailable，保留旧 assembly 和 canonical history，不使用未重协调的混合上下文发起 Provider request

### Requirement: Context plan 与 provider request hash 必须可重放和比较

`CanonicalItemRecord.content_hash` 的值格式固定为 `sha256:jcs:v1:<64位小写hex>`；其输入恰好是 `{ "payload_kind": <payload_kind>, "payload": <payload> }` 的 RFC 8785 JCS 无空白 UTF-8 字节，SHA-256 后用小写 hexadecimal 编码。文本不 trim 或换行归一化，对象 key 由 JCS 排序，数组保留 payload 语义顺序，二进制由 payload schema 先编码为 typed base64url 等值。item identity/sequence、semantic kind、status、producer、metadata、时间、关系、wire role 和 detail path 不进入该 hash；多 part payload 的 part identity/ordinal/value 在 payload 中并因此被覆盖。`item_catalog.content_hash` 必须与 JSONL envelope 一致，校验失败不得继续读取。

`plan_hash` 的 canonical preimage schema 固定为 `context-plan-hash:v2`。除 format/schema version、active view revision、按 `plan_ordinal` 排序的 canonical/request-only/overlay selection、每个 ContextRef 的 source/version/hash/length、贡献顺序和 selection/visibility policy 外，必须显式包含由 `ContextRequestPlan.tool_set_refs[]` 选出的 `tool_set_refs[]`。每个 ToolSetRef 条目必须包含 `tool_set_snapshot_id`、`source_revision`、`content_length`、恰一个 `content_hash` 或 `redacted_stable_digest`、`tool_set_schema`、`tool_set_schema_version`、`tool_policy_version` 及同一 `tool_set_manifest[]` 绑定的规范 `tool_policy`；不得用脱离 manifest 的 logical tool contract hash 替代这些字段。`tool_set_refs[]` registry portion 按 `tool_set_snapshot_id` 排序，`selection[]` 仍按 `plan_ordinal` 保留语义顺序，manifest 内 tools 按稳定 `tool_id` 排序；未被 selection 选中的 registry entry 不进入该 plan hash。canonical item ref 缺少 content hash 或被选 ToolSetRef 缺少 manifest hash token 时均不可 seal，因此 plan hash 不能只覆盖脱离 item/tool 来源的 message 文本。

系统 SHALL 使用带算法标识的 `sha256:jcs:v1` 对 plan/request preimage 做规范化 hash：对象 key 递归排序，数值按 JCS 规范化，语义数组按 item/content-part/tool/ref ordinal 保序，集合型字段按稳定 identity 排序，缺省字段省略，语义 null 才编码 null。`plan_hash` MUST 只覆盖 provider-neutral 的 format/schema version、active view revision、ordered canonical/request-only refs、source/version/hash/length、贡献顺序、selection/visibility policy 以及完整 ToolSetRef manifest identity（`tool_set_snapshot_id`、source revision、length、hash token、schema/policy version 与绑定的 `tool_policy`）；不得包含 assembly/execution/model-call identity、时间、wire role、provider/model capability、provider request ID 或 provider-specific loss。`request_hash` MUST 覆盖 projector id/version、provider/model profile、规范化 wire request、由同一 ToolSetRef manifest 投影出的 tool schema/config、attachment refs 和 loss/redaction markers；不得包含 provider request ID、retry/attempt identity、时间戳、认证或 transport headers。敏感正文 MUST 在 preimage 中以 redaction class、长度和 owner-thread-scoped stable digest 表达，不能把 secret 或 detail store 原文放入 hash。

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

系统 SHALL 为每个已提交 canonical item 建立最小 catalog 定位记录，至少包含 item identity、物理顺序、`semantic_kind`/`payload_kind`、status、Turn/group 关联、JSONL offset/length、content hash、commit identity 和基本 visibility。request-only reference 不得进入 canonical item catalog 或占用 canonical item sequence；如需精确重放，只能在有界的 assembly/plan detail store 中保留。更重的来源关系、文本/summary projection、tool/reasoning detail 和 middleware assembly metadata MUST 按 item 的用途按需建立；仅当 item 或其 content part 被声明为持久化操作边界时才建立 durable rewind/compaction/fork anchor。未被历史、上下文或操作使用的 canonical item 可以只有最小 catalog 和受保护 reference，不得因此影响 rollout 恢复完整性。Context view membership 的逻辑顺序 MUST 与 rollout 物理 `item_sequence` 分开表达，Turn root MUST 通过 `root_input_item_id` 定位而不是扫描 semantic/wire role。Turn内可展开逻辑Item还 MUST通过`(turn_id, logical_item_ordinal)`显式索引item/part identity和相邻elapsed；`first_item_sequence`/`last_item_sequence`只能作为诊断存储坐标，不能代表连续membership或用于计算`item_count`。

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

系统 SHALL 通过 SQLite context view、branch lineage 和 item range/reference 决定一次执行或历史请求可见的 canonical history item 集合。compaction、rewind、replay 和 fork MUST 通过新 view 或引用表达 history 选择变化，不得修改既有 item、创建依赖物理 segment 的第二份正文，或按最大物理序号猜测 active context。request-only reference 和 source overlay reference 不属于普通 history view membership；ContextRequestPlan 必须在 history view 解析完成后独立执行 reconciliation。普通同epoch执行只能复用已提交overlay并尾部追加；rewind若保留tracked registration但隐藏最新revision，则按rewind新epoch合同从冻结的Registry activation snapshot追加published完整revision，不能跨cutoff复活旧delta或读当前源。`pending_next_turn` runtime notice如果已经持久化为canonical item，也必须保持pending/ambient scope，不能作为normal Turn member或Turn root。

#### Scenario: Rewind 隐藏旧后缀

- **WHEN** 用户 rewind 到较早 item 后继续执行
- **THEN** 新 active view 只包含目标边界以前的可见 item 和新追加 item，旧 JSONL 后缀仍可由旧 view 读取但不进入当前请求

#### Scenario: Rewind 不跨 cutoff 复活旧 source delta

- **WHEN** rewind隐藏历史尾部中的A→B source delta item，且目标checkpoint保留该source的tracked registration
- **THEN** 新history view不包含该delta，下一次ContextRequestPlan引用冻结的Registry activation snapshot中published完整revision并追加新source overlay epoch；旧A→B只保留审计lineage，snapshot/untracked不自动恢复，当前源不被读取

#### Scenario: Compaction 位于 Turn 中间的安全边界

- **WHEN** compaction anchor 指向一个 Turn 中间两个已经协议闭合的逻辑 item group 之间
- **THEN** context view 可以以该 item 边界截断并保留摘要/必要尾部，不被强制对齐到 Turn 末尾

#### Scenario: Compaction 或 rewind 不切断 tool 协议组

- **WHEN** compaction、rewind、replay 或 fork 的 anchor 位于 assistant tool-call group 之后、任一匹配 terminal tool result 之前，且结果将成为可执行 active view
- **THEN** resolver 返回 `tool-protocol-boundary-conflict`，保持原 active view 且不创建目标 view、branch 或 epoch，并只返回不含正文的前后最近安全 anchor；系统不得自动移动边界、合成结果、留下 orphan call/result，或把只读 partial history 投影用于 dispatch

#### Scenario: pending runtime notice 被下一次请求消费

- **WHEN** 被打断 execution 产生一个尚未绑定新用户 Turn 的 `pending_next_turn` runtime notice，随后用户提交普通输入
- **THEN** 新 Turn 以该用户 input 的 `root_input_item_id` 开始；ContextRequestPlan 可以通过 ambient canonical reference 或 request-only projection 使用 pending notice，但它不进入 Turn 分页、root 或普通 Turn item range

#### Scenario: pending runtime notice 尚未被消费

- **WHEN** session 只有 pending runtime notice，尚未出现新的用户 input，也没有 resume execution
- **THEN** notice 保持 pending，不被 active context view 当作普通历史 Turn，不被历史 reader 伪造为用户消息；过期或丢失时返回明确的 pending/detail 状态

### Requirement: Canonical item 必须可编译为多种请求投影

系统 SHALL 从 active context view 和 request-only contribution 先形成有序、可审计的 context request plan，再按目标能力编译为 LangChain message 或 Provider 原生 item/request。编译过程 MUST 保留 canonical/request-only reference、`semantic_kind`/`payload_kind`、可表达的顺序、tool-call/result 关联、附件和 reasoning 保护状态。只有同一prefix epoch最顶层且位于第一条真实用户消息之前的initial root contributions可以合并为唯一system/developer/instructions item；之后的runtime notice、compaction summary及所有source full/delta/恢复 contribution必须按plan ordinal成为独立user item，不得提升、前插或按相邻role合并。无法表达时必须显式reject，不得静默改role或拼接文本；Anthropic部分官方模型的mid-context system能力仅保留关闭TODO。`assistant_text` 只能作为 `assistant_output` payload/content-part 的 projection，不能在 plan 中作为 canonical item kind。

#### Scenario: 编译为 LangChain 执行消息

- **WHEN** 当前 Agent 选择 LangChain adapter 执行请求
- **THEN** adapter 生成临时的 `list[BaseMessage]`，可将同一 `message_group_id` 下的 item 聚合为一个 `AIMessage`，并把 tool calls 放入 `AIMessage.tool_calls`、结果放入关联的 `ToolMessage`

#### Scenario: 编译为原生 Responses 请求

- **WHEN** Provider adapter 能直接接受 item 化请求
- **THEN** 系统可以从 context request plan 直接生成 Provider item/request，不要求先构造 LangChain message，也不把 LangChain 对象作为 canonical history

#### Scenario: request-only prompt 的 wire 投影

- **WHEN** ContextRequestPlan 包含静态 system prompt、skill contribution 和 workspace/environment contribution
- **THEN** 只有initial root中的启用贡献可编译进唯一system/developer/instructions item；第一条真实用户消息后的Skill/workspace/environment full、delta或恢复内容保持独立user item并保留reference/source identity/request-only生命周期，不能从wire role反向创建历史item或为适配Provider合并相邻user item

### Requirement: Context plan 必须绑定精确资源激活快照和安全来源

每个`ContextRequestPlan`和`ContextAssemblySnapshot` SHALL引用一个由上下文生命周期owner冻结的`ResourceActivationSnapshotRef`，并为每个实际选择的管理型资源保存有序`ResourceProvenanceRef`。activation snapshot MUST记录`activation_snapshot_id`、`snapshot_kind=turn|model_call`、model-call snapshot的`parent_turn_snapshot_id`、activation policy revision/hash、Registry generation、owner SessionThread、Turn、按kind要求的model-call identity、`bindings_hash`和`activation_provenance_hash`；resource provenance MUST记录内部resource identity、模型安全display URI、resource kind/scope/facet、已发布语义revision、effective boundary、captured Registry generation、语义payload content length、恰一个content hash或redacted stable digest、typed snapshot/detail reference、availability、activation ordinal，以及受保护typed `source_lineage_ref`和覆盖source_id/revision向量与derivation版本的`source_lineage_digest`。该lineage不得包含provider locator/credential，不得被用于当前源重读；原始来源revision与语义resource revision不得混用。

Turn取得active execution slot时 MUST冻结整个Turn不可变的activation policy与`TurnResourceSnapshot`，其中固定turn-bound binding。若没有model-call-bound kind，该Turn全部model call复用同一snapshot；若存在override，每次安全model-call preparation MUST建立parent-linked`ModelCallResourceSnapshot`并绑定该model call，逐字节复用parent中的turn-bound binding，只重新冻结model-call-bound binding。同一assembly可以包含两类binding，必须由每个ResourceProvenanceRef的effective boundary区分，不能用单个assembly boundary覆盖。ResourceRegistry或activation配置后续变化不得改写已经sealed的plan、hash、selection或Provider bytes，policy变化只在下一个Turn生效。

`bindings_hash` MUST只覆盖按activation ordinal排列的resource identity/kind/scope/facet/revision/integrity token/availability/display URI等实际选择语义，provider-neutral`plan_hash`只使用该bindings hash及其它实际context selection。独立`activation_provenance_hash` MUST覆盖snapshot kind、`parent_kind + parent_bindings_hash`关系描述、activation policy revision/hash、Registry generation及各binding effective boundary/captured generation；具体`parent_turn_snapshot_id`由typed relation/FK及turn-bound binding逐字节复用校验保护。`activation_snapshot_id`、Turn/model-call identity和captured_at只作typed relation，不进入内容hash。policy/effective boundary/generation不得进入bindings/plan hash；相同wire选择不能因为无关resource event、新Turn identity、policy发布或重启generation而改变hash。display URI只作provenance，不得替代resource identity、capability、dedupe或幂等key；URI不包含revision。恢复、replay、history、fork和Provider projection MUST使用已封存resource identity/revision/hash/snapshot ref，不得把display URI解析为当前资源，不得访问当前文件、network endpoint或ResourceRegistry补造历史。owner、parent/policy、ordinal、URI/resource binding、integrity或required snapshot任一缺失/冲突时必须显式失败。

模型可见或普通history projection只能返回策略允许的display URI、resource kind/scope/facet、revision安全标识、availability和typed provenance ref；绝对路径、provider locator、network endpoint、credential、内部handle和受保护正文 MUST NOT进入JSONL、普通history、模型请求日志或工具结果。Skill source URI属于resource/context envelope而不是Skill metadata。正常v2 runtime不得从旧`.boxteam/.../SKILL.md`、`read_file_path`、middleware location字符串或当前catalog反推resource provenance，也不得双写兼容字段。

额外的`source_lineage_digest/ref` MUST只进入`activation_provenance_hash`和受保护manifest完整性校验，不得进入`bindings_hash`、`plan_hash`、wire bytes或普通history字段；raw来源变化但被选择的语义facet未变时，既有binding的语义revision、lineage ref与plan hash均保持不变。required lineage缺失、digest不符或derivation版本无法验证时 MUST fail closed，不得从当前来源或Registry回填。

#### Scenario: 默认Turn跨多个model call复用资源快照

- **WHEN** Turn取得active slot时冻结resource revision A，第一次call后Registry发布revision B且该Turn继续第二次call
- **THEN** 默认turn配置下两个plan引用同一activation snapshot和A；B只影响后续Turn，旧plan hash和Provider bytes不变化

#### Scenario: 来源变化但选中语义facet不变

- **WHEN** `SKILL.md`只修改不参与metadata或activation的字段，或多层配置某来源修改后有效值未变化
- **THEN**来源revision可推进，但被选择的语义resource revision及既有source lineage ref不变；下一个assembly的bindings/plan hash及同epoch旧wire前缀不因此变化，history仍按原sealed ref恢复而不重读来源

#### Scenario: 来源lineage损坏

- **WHEN** required resource binding的source lineage manifest缺失、digest不符或derivation版本冲突
- **THEN** seal/restore/dispatch显式失败，不能按当前ResourceRegistry、display URI、文件或endpoint重建一个看似相同的历史binding

#### Scenario: model_call边界产生新快照

- **WHEN** resource kind配置为model_call且Registry在两次安全call之间发布revision B
- **THEN** 第二次call保存引用原TurnResourceSnapshot的ModelCallResourceSnapshot并可以为该kind选择B；turn-bound binding逐字节复用，第一次assembly继续绑定A，两个snapshot和resource provenance的lineage可独立恢复

#### Scenario: 混合边界和policy revision可恢复

- **WHEN**同一assembly包含`agent_spec=turn`与`tracked_skill_activation=model_call`，且当前Turn执行期间activation配置又发布新revision
- **THEN**assembly的snapshot保存原policy revision/hash和parent Turn snapshot；agent spec binding标记turn并复用parent，tracked Skill binding标记model_call并记录自己的captured generation。配置新revision不改变当前Turn，restore可逐字段验证而不猜测单一boundary

#### Scenario: 历史资源位置已经改变

- **WHEN** sealed assembly引用的资源后来移动物理路径、改变provider locator或更新同一display URI的revision
- **THEN** restore从原snapshot/detail ref验证并读取原字节；required detail不可用时返回明确错误，不解析URI、当前Registry或当前文件替代

#### Scenario: 旧路径型来源字段进入正常v2运行时

- **WHEN** 正常v2 plan、assembly或history记录只携带`.boxteam` Skill路径、`read_file_path`或middleware location而缺少typed resource provenance
- **THEN** 系统返回schema/provenance错误并停止seal/restore/dispatch，不创建兼容ResourceProvenanceRef、不双写也不回退猜测

#### Scenario: 模型只能看到安全虚拟来源

- **WHEN** Skill activation来自Gateway全局资源并进入history或工具结果
- **THEN** projection可以返回`boxteam://gateway/{gateway_id}/resources/skills/{skill_name}/SKILL.md`及安全revision标识，但不得返回`${BOXTEAM_HOME}`路径、provider handle、credential或完整受保护正文

### Requirement: Tool 因果关系和 reasoning 保护状态不可丢失

ContextRequestPlan MUST先完成tool协议收敛：assistant同一group声明的全部tool call都存在同一causal execution内匹配的terminal result之前，期间已提交的Skill/team/runtime source只能作为pending事实，不得进入可dispatch selection或插在call/result之间。收敛后的plan顺序 MUST为assistant tool-call group、全部paired result、再到期间pending source；物理`item_sequence`、JSONL offset或提交时间只作存储坐标，不能替代协议因果。

系统 SHALL 为 model-declared tool call、实际 tool execution 和 tool result 保存独立的 item identity，并使用 `tool_invocation_id`、`tool_call_id` 和 `tool_attempt_id` 表达逻辑调用、实际 call attempt 与执行 attempt。一个 logical invocation 可以有多个按序 call attempt；每个 call attempt 至多启动一个 tool attempt；每个 tool attempt 至多有一个已提交 `tool_result` item，缺少 result 只能表示未完成或 unknown。三种identity在`(session_id, thread_id)`owner内唯一，tool attempt的执行幂等键必须绑定`tool_attempt_id`和输入payload hash；同key相同payload重试只返回原outcome，不同payload必须报冲突。retry/resume必须创建新的call/attempt identity，并以`retry_of`/`resumes`关系连接旧identity，旧item不可变。只有当前active view/lineage中`status=completed`且工具outcome成功的result，才可通过`replay_input`关系作为自动replay输入；superseded、failed、partial或unknown result默认不可作为replay输入。reasoning的可读文本、provider summary、encrypted/opaque payload和本地展示策略 MUST 保持可区分；受保护payload默认不得进入用户历史响应或普通assistant文本。

#### Scenario: tool call 与 result 成对恢复

- **WHEN** 一个 Turn 包含 tool call、工具执行和 tool result
- **THEN** 恢复与 Provider request projection 能通过 invocation/call/attempt 关联还原配对，且重复恢复不会创建第二个可执行 tool call；只有成功 completed result 才会自动作为 replay input

#### Scenario: skill_load activation 不打断工具配对

- **WHEN**模型并行声明多个tool call，其中`skill_load`先产生activation source而其它调用或本调用的tool result尚未全部terminal
- **THEN**activation保持pending；下一次可dispatch request先包含完整assistant call group和全部paired result，再包含独立user-role activation，任何物理append交错都不得改变该顺序

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
