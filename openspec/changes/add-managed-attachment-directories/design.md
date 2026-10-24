## Context

见 proposal 的动机。当前附件正文位于工作区，blob 身份由 digest 推导，但逻辑附件及访问仍绑定 Session/Thread；fork 可为同一正文建立目标 owner 引用。启动与 Session 删除调用 `collect_garbage(retention=timedelta(0))`，候选按 blob 创建时间筛选，不是从最后一次 detach 开始计时。

Terminal/Browser 已共用资源关联和 operation lease 规则：正常 unload 立即删除 thread 临时资源，手动 detach 后才等待 30 分钟。孤儿扫描跳过仍有关联的记录；恢复只核外部资源状态，不能解决失效关联。Thread residency 只保存本进程真实 runtime；重启后的冷 Thread 不会凭空取得可卸载的 generation。

## Goals / Non-Goals

**Goals:** 目录是关联、Shell 工作目录和回收单位；异常退出不依赖 Agent 主动 detach；复用原 owner、URI、文件操作和冻结输入链路。

**Non-Goals:** 不新增云端存储、每文件引用计数、通用文件版本控制或附件专用工具。已完成的首个边界只修复 Thread 回收扫描的失败隔离、周期重试与有界等待，以及资源记录查询改名后遗漏的直接/间接调用；目录迁移继续按后续纵向切片实施，不能将盘点完成视为目录功能完成。Agent 团队、成员、委派及会话协作通信按用户裁定保留 TODO，不在此变更追加实现或专项验证。

## Decisions

### 1. 每次独立上传得到独立目录资源

附件 owner 分配稳定资源 ID，在 `.boxteam/attachments/YYYY/MM/DD/{attachment_id}/` 下保存目录，UTC 日期在创建 intent 冻结，唯一 catalog 保存受检 locator。原件位于 `original/`，预览位于 `preview/`，处理输出位于 `derived/`；相对文件路径继续使用统一 owner 的校验和编码规则，不按当前日期或用户显示名寻找目录。

相同内容的独立上传不合并可变目录。目录没有“整体不可变 digest”；原件、预览及已进入 canonical/assembly 的具体版本有自己的 digest/length。原件保持不变，Shell 处理使用新文件或子目录。尚未作为历史输入冻结的派生文件可按原文件 owner 能力修改，不为每次编辑建立另一套历史。

### 2. 统一寻址与 Shell 工作目录

统一 URI owner 登记 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/attachments/{attachment_id}` 为 directory 根，并登记其文件 child 规则；在该 owner 的 kind 闭集中增加 `attachments`，不新增 scheme、query 或真实路径字段。新上传消息使用该根下原件 URI，并提供目录 URI、文件名、MIME 和处理结果写入提示。既有 Session 产物 URI 只能作为原历史身份保留，按原 provenance/owner 授权解析，不能继续生成新旧两套附件地址。

Shell 的结构化 workdir 接受目录 URI，在 URI 指定目标的文件 owner 内取得短期本地 cwd 和操作 lease，再复用原 PTY 执行。URI 目标与 Shell 目标不一致时明确拒绝；不把远端 URI 当本机路径，不改写任意 command 字符串，不把真实 cwd写进普通业务记录。整个执行期间保留目录使用 lease；后台进程仍访问目录时，其真实进程 owner 延续占用，不能在工具提前返回时解除保活。

2026-10-24 的先行切片只接入既有 `files` 目录 URI：Terminal owner 依赖 container 已装配的 `WorkspaceFilePaths`，在创建记录/PTY 前解析精确 `boxteam://` 前缀的 URI；POSIX 原生目录名中的其它 `://` 不改变含义，全部原生 cwd 也在同一 Terminal owner 锚定目标工作区。工具层不再自行解析 cwd、接受另一份 workspace root 或重算公开位置，回执直接消费真实 PTY 的 `cwd_relative`。Shell 原生工作区外/私有目录行为保持，命令正文不做 URI 替换。目录解析中的系统异常和符号链接环完整留在诊断/cause，普通错误只包含资源 URI 和错误类型。附件目录 kind、全进程占用及其回收保护仍未实现，不能将这个切片算作任务 3.3 全部完成。

### 3. 目录关联与 30 天回收

用户上传经既有 ingest/消息接受边界自动关联发起 Thread；未发送或接受失败的上传继续由原 pin/ingest 恢复流程收敛。关联是一组幂等的精确 Thread 关系，不是每次读取递增的计数，不承担鉴权职责。工作区内按既有授权可读取的 Thread 不因缺少上传者身份而被拒绝；需要长期保活时显式 attach，短期文件/Shell 操作由 lease 保护。

按前文讨论的“与 Shell/Browser 相同的 Thread 释放通知”，Runtime unload 和逻辑 Thread/Session 删除都通知附件 owner detach；附件只进入宽限期，不随临时进程立即销毁。最后一个关联解除时持久化 `unattached_since` 和默认 30 天 deadline，再次 attach 清空 deadline。排队输入、进行中的上传、fork capture/publication 和文件处理由其既有 durable operation/claim 保活；不能因暂时没有 resident graph 将尚待执行的输入判为孤儿。

启动及低频周期检查只查询 catalog 到期记录，不频繁扫描磁盘。到期删除前与 attach、fork reservation、lease 获取在同一 owner 临界区/事务内复核，先提交不可再访问的删除状态，再按 locator 删除目录；重启按删除记录重试。有效操作结束后重新核验，不在 lease 到期时假定外部进程已停止。明确的未知目录不被自动吸收或删除。

### 4. 历史与 fork

历史关联、来源 provenance 和运行期保活职责分开：canonical 中出现 URI 本身不形成永久保活。附件脱离全部活动关联超过 30 天可删除原件；用户以后打开原会话或从该点 fork 时，资源明确 unavailable，不能自动复活空目录。这是本 change 对历史原件可用期限的显式产品修订；已封存的模型输入、预览、assembly/hash 和恢复依赖继续由原历史存储 owner 保留，不重新读取当前目录猜测过去输入。

同工作区 fork 默认通过统一目标检查取得共享目录关联，不复制可变目录来冒充同一资源；仅当该附件属于 fork 选中的历史依赖才保留它。跨工作区按冻结目录 manifest/revision 复制到独立目标资源，不保留跨端可变目录挂载。两者都沿用原 fork journal、publication 和 claim settlement；来源历史 ID/URI 不改写，目标写入使用目标 owner 解析的当前资源引用。目录修改与跨端 capture 必须核对冻结版本，不能复制混合时刻的文件集合。对原件的不可变约束和对派生输出的显式路径可见性不因共享而改变。

### 5. 异常退出必须由 owner 收敛

不为每个附件发送心跳；复用后端启动代次、Thread runtime generation、durable admission/operation 和真实任务终态识别持有者。进程仍存活时，由统一执行 finally、取消保护和卸载通知收敛；重启时对照权威 Thread/任务事实处理旧代关联，不能仅看数据库里还有 Thread ID 就无限保留，也不能把 cold Thread 或排队输入当作已失效。迟到的旧代 detach 不得移除新代 attach。

附件、Shell 和 Browser 分别由自己的 owner 核实清理；过期持有者仅说明可接管，不证明 PTY、浏览器或处理进程已经停止。挂起操作的超时触发原执行 owner 取消/终止并核实；未知结果继续可发现并阻断破坏性回收，显式展示待核实原因及管理入口，不按短 TTL 强删目录或把操作标成成功。

### 6. 本轮实施的失败隔离

`ThreadResidencyTracker.sweep()` 对每个 Thread 独立尝试清理，失败保留原 scope、generation 和 owner 记录，其它 Thread 仍完成。整批结束后向调用方抛出原错误（单项）或汇总错误（多项），附精确 owner 信息；不改变已有单错误类型或把失败算成成功卸载。`AgentExecutionService` 的唯一周期 worker 记录完整异常后按原 60 秒周期重试，取消和关闭正常传播。这里不解决失效关联或外部未知结果，也不绕过原 owner 核实规则。

闲置扫描通过 `asyncio.wait` 为单个真实卸载任务提供 30 秒等待预算，覆盖该任务中等待运行排空、领域通知和 scope close 的总等待。到期向本轮报告含精确 owner/generation 的 `TimeoutError`，继续扫描其它 Thread；不取消原任务，不解除关联或 lease，不标记 cold。原任务仍由 `runtime_unload` 持有，新 admission 等待同一任务，后续扫描不会为同一代再启动清理。迟到成功沿原关闭链落为 cold；迟到失败由现有后台任务回调完整记录，原状态保留给下一周期重试。这是事件循环能调度时的扫描等待预算，不是保证 30 秒内释放资源或整轮完成的期限。

当前 Terminal/Browser 的 HTTP 层分别设置 10/30 秒网络超时，它们不覆盖锁等待、线程池排队、多次请求总耗时或 scope 回调。共享 owner 锁若由挂起调用持有，其它 Thread 的实际释放仍可能等待该锁；同步回调阻塞事件循环时异步计时也无法抢占。本轮不修改 Session 删除、换代 admission 或 shutdown 的真实排空语义，不在仍有副作用时假报已完成。后续任务 5.4 必须统一 operation 截止时间、终止核实和迟到回执，隔离无关资源的锁等待，明确关闭/删除超时的失败返回和保留事实，并覆盖这些真实边界。

扩展资源 owner 回归发现，记录查询已更名为 `_require_owned_resource_record`，但共享 operation、Terminal/Browser operation 和 Browser 管理入口仍有旧 `_require` 调用，导致取得 lease 或读取记录时直接失败。本轮全量迁齐这些调用和对应测试，保留唯一查询方法，不加兼容入口，也不改变关联、lease 或删除策略。

## Conflict Review

本 change 是附件目录修订的后继 owner。以下旧 change 的已完成任务只证明旧基线；目录链路迁齐前保留原产品实现，不宣称这张表对应的迁移已完成。合入/归档顺序为先落现有基础 change，再应用本 change；不得在最后归档旧 change 时覆盖本修订。

| 冲突来源 | 处理决定 | 必须保留的约束 |
| --- | --- | --- |
| itemized design §8.3、spec 的“canonical attachment 只保存 workspace blob 引用”及单 blob fork 验收 | 新上传改用目录/文件资源身份，取消相同上传强制合并；原 blob/owner-ref 存储迁齐调用方后下线 | canonical 不保存物理 locator，历史原始身份和 frozen digest/length 不改写，fork claim/publication 原子性不降低 |
| virtual-resource-addressing 的 kind 闭集、单文件附件模板及共享 blob 场景 | 由同一 URI owner 登记上述 workspace attachments directory/child；旧 Session URI 只保历史解析，不生成新引用 | 不新增 URI 解析器、不泄漏真实路径、普通 files/root 仍不能直接遍历 `.boxteam` |
| unified-resource-file-tools 的“会话范围保存原件”“附件单文件只读”“进程执行另行定义” | 本 change 修改消息原件存储范围；目录原件只读、派生区通过原文件 owner 操作，结构化 Shell workdir补齐执行合同 | 512 像素预览、媒体投影、原件读取、文件工具权限及修订语义保持 |
| persistent-resources 的 thread unload 即时 delete、零关联 30 分钟、workspace 永不自动过期 | 这些规则继续适用于既有 Shell/Browser；附件复用机制但采用明确的零关联 30 天策略 | 显式 detach 不等于停止、有效 operation 阻止回收、未知停止结果不伪报成功 |
| deduplicate-execution-history-storage 的不可变内容复用及恢复/fork保护 | 可变目录不参与 digest 身份合并；已冻结历史输入仍按原 immutable asset/version 复用 | 历史 request_hash 可重建，GC 不删除 checkpoint/assembly 所需版本；不从当前目录补历史 |
| 当前附件 GC 的 `created_at < now-retention` 与 `retention=0` 调用 | 改为最后一次零关联后的 deadline；不能简单把参数改成 30 天 | 在 detach 第 30 天前不删，重启不重置 deadline，删除与 reattach 互斥 |
| 当前恢复只核运行状态、关联存活无对账，以及单项异常停止 sweep | 失效关联/代次对账属于后续纵向切片；本轮只完成 sweep 失败隔离与重试 | 不把日志或任务勾选当作异常退出后的真实资源释放证据 |
| persistent-resources 的 unload 必须核实终态，与扫描等待期限 | 30 秒只限制本轮等待；唯一卸载任务继续持有全部未结清事实，原 admission/Session 删除/关闭仍等真实清理 | 不把超时当停止证明，不重复删除，不提前释放 lease；共享锁/同步阻塞和关闭期限尚待 5.4 |

## Risks / Trade-offs

- [历史原件在无关联 30 天后不可用] → 明确到期/已回收状态；冻结模型输入与运行恢复依赖单独保护，不静默延长或承诺无限期附件历史。
- [共享目录的派生结果互相覆盖] → 原件不变，模型提示使用新的文件/子目录；文件 owner 保留修订冲突合同，处理结果被用于模型输入时冻结具体版本。
- [Shell 可写目录绕过逐文件观察] → 执行全程占用目录，capture/读取在原 owner 核实文件版本和 manifest；不依赖模型报告写入完成。
- [GC 与 attach/fork/重启竞争] → 复用 owner 事务、lease 和 journal，删除前复核，提交删除状态后拒绝新 attach；删除失败保留可重试事实。
- [慢清理拖长周期或占住共享 owner 锁] → 单 Thread 扫描等待预算 30 秒，同代任务去重；多个慢任务仍会累计等待，同 owner 的真实释放仍可能受锁阻断。后续 5.4 处理终止核实、锁粒度与关闭期限；同步阻塞无法靠 asyncio 超时抢占。不擅自改 30 分钟/30 天期限。

## Migration Plan

### 调用方与切换范围（任务 2.1）

2026-10-24 在 `4aa3e5a0f` 及当前共享工作树盘点。首轮精确符号检索覆盖 102 个文件、612 处；补充大小写不敏感的附件、Session 产物和 Shell 工作目录入口检索，覆盖 275 个源码/协议/测试文件、3110 处。该数字包括生成文件和测试引用，不代表 275 个独立 owner。原始检索、逐文件 SHA256 和行号保留在 `out/tests/temp/attachment_directory_inventory_20261024/artifacts/full-source-manifest-001.json` 及同目录 `full-references-001.jsonl`；后续实现按最终候选重新核对，不能将共享工作树的盘点结果当成已提交源码验证。

| 链路 | 现有权威及直接/间接消费者 | 单次迁齐与旧实现移除边界 |
| --- | --- | --- |
| 身份、落盘、SQL | `attachment_blob_catalog/{catalog,schema,records,blob_identity,locator,ingest,blobs,queries,store}.py` 与 `sql/`；container/Depends 注入 | facade 实际位于 `catalog.py`；schema 3 的 digest claim、owner 派生身份及 blob GC 由目录 owner 替代。SQL 已抽离，勿重复整理；同时修订本目录 AGENTS 的旧 facade/身份限制，不新增第二个生产 store。 |
| 上传、用户及内部接受 | `message_service.py` → `persist_inline`/`AttachmentContentService` → `UserContentBuilder` → saver acceptance；`recovery.py` 与 `session_control_operation_lease` | 原 pin/fence/holder/消息接受结算协议保留；新资源 ID 绑定 ingest 幂等键而非 digest。同一次重试复用一个目录，独立上传另分配目录。accept、内部 admission 和恢复都核对精确附件及消息，不用 resident runtime 判断是否仍需保活。 |
| 排队与执行输入 | Job 的 pending controller/runtime state、`pending_request_store.py`、execution step runner、checkpoint runtime/async API | 控制队列只保引用，正文仍由 canonical owner 提供；已接受但尚未执行的附件必须由 durable acceptance/operation 保活，不能因 pin 已终态或 runtime 尚未建立被 detach/GC。 |
| 媒体与协议 | `attachment_content_service.py`、`api/messages.py` content/thumbnail；`internal_v2/attachment.py`、message/pending/turn DTO、`message.proto`/`public.proto` 及 Python/TS/Buf 生成物 | 原件与 512 像素预览改在同一目录，视频抽帧、MIME、诊断和不可用状态保留；新增目录引用须迁齐序列化、校验和生成产物，不能只改 Python 类型。 |
| Web 输入与查看 | Composer/tray、attachments state、发送/重发 hooks、session API、timeline/turn 投影、`MessageAttachments`、`WorkspaceAttachmentPreview`、`useMessageMediaSources` 与 media utilities | 输入、发送、展示及下载都消费后端目录/文件引用；不由前端拼本机位置。迁齐 OpenAPI/生成类型，完成真实类型检查及 Web build。 |
| URI、文件与导航 | `core/resource_uri.py`、virtual resolver、resource navigation、bootstrap/container 的 artifact reader；`workspace_filesystem_tools.py` 和 `workspace_files/paths.py` | 唯一 grammar 增加 attachments directory/child，原文件操作链获得目录 owner 端口；普通 files/root 仍拒绝 `.boxteam`。新消息不生成 Session 附件 URI，旧 URI 只解析到新 owner 的历史绑定。 |
| 模型读取与冻结 | `agent_factory.py::read_artifact`、`context_dispatch.py::read_tool_call_attachment`、item schema/runtime notice、canonical LangChain/content/provider 映射、`item_resource_references` | 区分工作区访问授权、Thread 保活和历史 provenance；不能照搬原 Session/Thread 私有引用拒绝。typed 附件和 generated blocks 迁齐，用户原文及已封存 canonical/assembly bytes/hash 不变；实际用于请求的文件版本由既有 immutable asset owner 冻结。 |
| Shell 与资源关联 | `tools/terminal.py` → TerminalOperations/ResourceOwner → PTY；workspace resource records/operation leases 与 Thread residency 通知 | 结构化 cwd 经目标 owner 解析，命令原文不替换；前台、提前返回及后台子进程沿原进程 owner 保持目录占用，真实终态后才解除。原生路径输入保留，远端 URI 不在中继机器解释。 |
| fork、转移与历史 | attachment `fork_store/fork_import/fork_claims`；checkpoint `fork_source/fork_copy/fork_owner`、`source_capture/source_snapshot`；Session fork service/transfer DTO、copy attachment settlement、attachment projection | 原 capture/preimage/snapshot hash 含完整旧附件快照，不能原地改写。新同工作区 fork 共享目录关联，跨工作区冻结 manifest/revision 复制；旧历史身份通过新 owner 的确定映射解析，claim/publication/失败及删除结算全迁齐，不能只换普通读取。 |
| 删除、恢复与回收 | `main.py` 启动恢复与 retention=0 GC；container Session 删除；fork lineage Thread 删除；`recovery.py` 与 `queries.py` | 同批替换创建时间 GC 和即时调用；精确 detach 后从最后零关联时刻起算 30 天，操作/排队/fork 仍受保护。删除登记、物理移除及重启重试共用目录 owner，不扫描目录吸收未知资源。 |
| 验收及发行资产 | attachment catalog/identity/recovery/content 单测、消息寿命/拓扑/预览故障集成、read/navigation 工具、fork modes/publication/transfer/unavailable、Web 附件组件、vision E2E、`test_sqlite_release_assets.py` | 随各纵向切片迁齐旧身份/URI/GC 断言；执行、fork、恢复关键状态经产品入口生成。仅查询/投影局部测试可手工构造历史，发行包仍必须携带实际加载的 SQL。 |

### 支持的旧库及原件保全

1. 当前 `AttachmentBlobCatalog._initialize()` 只支持空库与 `user_version=3`，版本 1/2 无现有升级入口。目录结构落地时将附件 schema 提升为 4，仅提供当前完整 schema 3 的显式离线迁移；新空库直接创建 4。版本 1/2、未知版本、缺表、损坏或受检 locator/内容不一致均明确拒绝并保留原数据，不隐式扩大支持范围。任务 2.1 只定范围，不提升 schema。
2. 迁移前停止目标工作区后端及相关写入/外部任务。Session/topology/operation 锁不是整个工作区的停机证明。原附件 owner 的 `maintenance.py` 已引入跨进程共享/独占维护锁：Backend 在任何业务初始化前取得所有 mount 的共享锁，至全部业务 writer 与 catalog 关闭后归还；独立服务图构造也遵守此入口，目录访问由 catalog 惰性持有同一共享锁，连接关闭失败保留连接与锁供重试。锁文件固定在附件根同级，不能随目录/catalog 切换替换或删除；冲突立即明确失败，不探测端口、检查 PID 文件或按锁文件存在推断停机。`migration prepare` 已在取得独占后核对 Session 创建/删除、operation lease、Thread 创建、ingest、正文 commit claim、fork capture/claim/export release 和外部资源账本；非终态先由原恢复入口收敛，仍未收敛或未知终态则拒绝，不伪造终态或丢弃占用。完整 queued 输入保留，claimed/started 或没有 queued 输入支撑的 unknown execution 拒绝；通用账本中的外部资源必须 stopped、占用 released/settled 且仍有对应资源记录；Terminal/Browser 的领域 owner 记录也独立按原格式核对，要求其原终态闭集与 verified 同时成立，fork attach 不得仍 dispatching。不能用通用账本缺少资源来忽略领域记录先落盘的创建窗口，也不在离线预检中调用 recover 或伪造终态。维护锁不能单独证明遗留外部进程已经停止，版本转换、恢复和切换仍 TODO。
3. 已提供显式 `python -m app.services.infrastructure.attachment_blob_catalog.migration prepare --workspace <root> --output <new-dir>` 与 `verify --backup <dir>`。只接受完整附件 schema 3，表、约束、索引和触发器由线上初始化共用的 SQL 顺序生成内存参照；缺失/改写不自动修复。通过 SQLite backup API 保存附件 catalog 与登记 Session/control/rollout 的已提交 WAL 内容；备份快照采用 immutable 只读核验，不在其中生成 WAL/SHM。依权威 catalog 和 journal 枚举并保存原件、派生预览及历史 capture，同时逐字保全已通过准入的 Terminal/Browser owner 记录与关联；冻结 capture 全部逐字复制，不再次 SQLite backup 改变其字节；清单保存 source schema、稳定身份、相对 locator、digest/length。原件先核受检 locator、普通文件及字节 hash，不遍历日期桶吸收资源。源文件和非空 WAL 在复制前后与全批结束复核，只将“缺 WAL”和“空 WAL”统一为无已提交页；任一步变化或失败保留 preparing 产物和原数据，不能为迁移先触发旧 retention=0 GC 或删原件。内容、清单及目录全部 durable 后，唯一 journal 才发布 prepared；verify 拒绝未完成或 hash 不一致的备份。此阶段不转换目录、不提升 schema，不提供覆盖源数据的恢复操作，不能把 prepared 当成迁移成功。
4. 一个既有 `attachment_id` 迁为一个目录资源并保留其稳定 ID；同一 Thread 同内容的多次上传已在旧库合并，不能凭空推断原先的上传次数。不同既有逻辑 ID 不根据 digest 再合并。共享 blob 的已登记原件分别复制入各目录 `original/`，不使用可写硬链接；可核验的预览可迁入 `preview/`。原件文件名按唯一 owner 的 child 校验，用户显示名不参与物理定位。available 的原件缺失/损坏必须拒绝；已 tombstoned 的资源只保留不可用事实，不能创建空目录冒充恢复成功。
5. 新 owner 保存旧 Session URI、精确旧 owner/blob/version 和历史别名的确定绑定。生产解析旧 URI 后直接使用新目录/冻结版本，不回读旧表或旧正文位置；旧数据仅作迁移输入及人工备份。canonical item、assembly、资源索引中的原始历史 URI 和已封存 capture/snapshot/preimage 保持逐字不变。终态 fork mapping/claim 保留冻结原文及来源身份，由新 owner 校验该原文并解析迁入版本；不能改写旧 `relative_locator` 来重算 hash，也不能从 digest 猜测跨 Thread 授权。新 fork 按目录 manifest 合同写入，不继续产生旧 blob 快照。
6. 旧 active 引用按其精确 Session/Thread 与权威任务状态建立关联/必要占用；不能把全部冷 Thread 当失效，也不能仅因 Thread ID 存在而永久保活。旧 released 引用保留原释放 `updated_at`，某目录已零关联时用其可核验释放时刻起算 30 天，不能改按创建日或每次重启重置；未知释放时刻明确拒绝该记录并保全。迁移期间只登记到期事实，不夹带物理 GC；删除终态及保留中的 fork/恢复依赖独立核对。
7. 在隔离准备区完成新 catalog、目录及映射校验，通过原 owner 的持久迁移记录冻结源备份指纹、schema 和目标集合；全部 durable 后才提交唯一切换点。启动只接受已完成的 schema 4，未完成迁移明确失败并由原迁移入口继续/撤销；不从源/目标双读猜测成功。中断前后的恢复、重复执行和回退必须验证。切换成功也不自动删除 schema 3 备份；应用有新写入后的回退不得直接覆盖新数据。旧生产表/方法与全部调用方在切换批次移除，目录维护互斥及迁移记录不成为第二套业务状态。

### 实施顺序与验证门

已完成的扫描小修不改变附件格式、URI 或资源删除行为。下一实施批次以目录创建/上传接受/原件与预览读取为完整入口，同时迁齐统一 URI、冻结输入及对应 fork/历史解析所需端口；不能先暴露一个无消费者的新 catalog。接着补齐统一文件写入、Shell 全进程占用、代次对账和低频 GC；每个批次迁齐声明、写入和消费者后集中验证，任务未满足全部合同则保持未勾选。

迁移本身须覆盖从产品生成的 schema 3 历史、相同内容重复上传、共享旧 blob 的多个 owner、已释放/已回收资源、终态深历史 fork、排队输入、非终态明确拒绝，以及切换前/切换点后崩溃与重复执行。核对原件字节、URI 身份、owner/claim、历史请求 hash 和备份；目录回收不删除历史 immutable asset。Agent 团队/委派专项验收继续 TODO；附件链实际必需的用户/内部消息接受和通用 fork 仍验证。
