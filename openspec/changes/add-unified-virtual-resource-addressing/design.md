## Context

2026-10-05 用户确认术语收口，由主线程按 `tasks.md` 1.1/6.6 统一实施；机制称「统一资源寻址」，具体地址称「资源 URI」，身份与底层位置仍分别称 `ResourceIdentity`、真实路径。此次调整不改变寻址语法、稳定身份或授权行为，实际命名修改与唯一实现者的在途写入错开。

现状（全部取自仓库内受版本控制的事实源；关键取值已内联在下方要点中，不引用任何 `out/tests/temp/**` 临时产物）：

- `app/services/infrastructure/resource_platform/virtual_resources/` 定义了严格 URI grammar 与 typed resolver，但**解析/授权侧零生产调用**：`parse_resource_uri`、`VirtualResourceResolver`、`ResolvedResourceHandle`、`ResolutionContext` 只被测试引用。 **（修订注，`298ef599`+`f3bd8213`：本 change 3.2/3.3 已装配生产链路，「解析/授权侧零生产调用」已不成立，见下条）**
- 唯一生产使用点是 `app/agents/skill_runtime.py:541` 的 `skill_display_uri`——即大体系只被用来**打印**地址。 **（修订注，`298ef599` 落地）**：`app/agents/skill_runtime.py` 的 `_layer_scope_identity` 已按真实身份推导 `inline` 的 `scope_id`（`f3bd8213` 收紧 version charset），并经 `resolver.require_scope_binding` 校验；「只被用来打印地址」为**修订前现状**。
- `skill_runtime.py:52`（`boxteam://workspace/agents`）与 `skill_runtime.py:619`（`boxteam://workspace/{id}/resources/skills/catalog`）是绕过 owner 的裸拼接，实测被自家 grammar 以 `malformed_path` 拒绝；`boxteam://gateway/...` 等会话定位串同样被拒。
- `app/services/business/session_context_resource.py` 是**第二套并行**的 `boxteam://` 正则解析器（会话定位，支持 `#fragment`），与 URI grammar 互不可解析。
- 在途未归档 change `add-context-injection-lifecycle` 的 `tasks.md:47`（3.14）解析器本体已完成，`tasks.md:95`（6.6）与 `:111`（7.1）接线任务未做；其 `specs/context-injection-lifecycle/spec.md:331/333` 有 URI resolver requirement。

**契约版本与权威表状态**：本设计采用**契约修正 v2**（保留既有段序，`resources` 为固定保留段，`scope_id` 对所有 scope 必填），并已依据**已下发的权威表**定稿三类内容：scope 闭集、scope_id 语义、kind 闭集与拒绝码登记。权威表推翻 v2 两处初审：既有 scope 闭集其实只有 `workspace`/`gateway`/`builtin`/`memory`，`user`/`inline` 为新增；kind 既有只有 `agent-spec`/`skills`，`config` 与 `session` 为新增。

## Goals / Non-Goals

**Goals**

- 把「身份 / 地址 / 真实路径」三层分离落成**唯一 owner 与唯一实现**，并给出可机械检查的不变量。
- 给出一套统一 URI 语法（保留 `resources` 固定段序）与规范化单一实现、**定稿**的 scope 闭集、scope_id 语义表与 kind 闭集。
- 给出**分三套**集中登记的拒绝码（grammar 分段编码修订闭集 + resolve 7 + 联邦解析期）与其不混用约束；resolve 闭集包含 `unsupported_view`，专用于已识别的 view 与资源 kind 不兼容。
- 给出顶层 gateway 之间**星型解析**的边界契约，并把上界做成显式 policy 常量。
- 登记配置来源 real path 持久化这一**已存在违约**的迁移形态，并唯一规定来源 URI、逻辑 layer/precedence 与 carrier/snapshot 的边界。
- 明确与另外三个 change 的接口面，避免多份定义并存。

**Non-Goals**

- 不在本 change 内写生产代码；实施由 tasks 驱动。
- 不设计会话上下文 URI 的统一改造（由并行 change 承载），本 change 只承认其归属与 scope/kind 复用。
- 不设计 HTTP API 的多工作区路由细节（由「单后端多工作区挂载」change 承载），本 change 只要求 scope_id 身份在寻址层显式。
- **不为 `memory` 做任何设计**：它已确证不是 URI scope，且其 domain owner 与状态本体从未接入，故无 URI 替代 owner 的需求；解析器侧 MUST 物理移除既有两点式特例分支，并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**）。
- 不引入资源插件宿主、动态 provider 装配或可安装资源 API。

## Decisions

### D1：三层分离以「谁持久化 / 谁可跨边界」为切分线

- **ResourceIdentity**：持久化，无 revision，不依赖激活工作区 → 用于去重与 lineage。
- **URI**：持久化，允许悬空，禁 revision/hash → 软件内部与模型可见载荷的默认引用形式。
- **real path**：资源访问时仅在原生 owner 的最终 fs/sqlite 调用点及必要局部链路临时使用，不作为资源引用进入模型载荷、普通响应 DTO、解析结果或跨 Gateway 资源投影。原生目录选择/注册和普通文件原生路径是具名请求输入，不放宽输出/持久化边界。物理 workspace root 与用户显式选定的未注册目录根是两类具名控制面持久化例外，均只由原生 Gateway 持有；前者合同引用 `add-multi-workspace-backend-mounting`，后者见 D1b。原始诊断日志和 Browser 页面 URL 的两项具名例外仅按 D1c 与对应 spec requirements 生效，不改变资源引用的三层职责。

会话文件变更的修改对象保存 files URI，冷读索引和 reviewed map 使用与写入相同的引用校验。before/after/diff 快照由 session/edit 身份在存储 owner 内重推导，不保存等价相对 locator，也不在 DTO/事件重复输出；前端继续使用已有 diff_text 与 preview。移除旧字段及全部消费者，protobuf 保留已发布编号和名称，旧落盘字段明确失败而非双读迁移。

**理由**：审计发现的真实缺陷正是资源路径泄漏（skill 目录扫描、`skillPaths.ts` 解析物理路径、catalog URI 裸拼接、`ConfigSourceLayerRecord.source_path` 落库）。资源引用以身份和 URI 表达；原生 Gateway 根绑定是目标生命周期的控制面输入，不能作为 URI、资源记录或跨 Gateway 资源投影携带。

**备选**：把 revision 编进 URI（被否：revision 变化会让同一资源产生不同地址，破坏「地址稳定、revision 独立」）；把 real path 作为可选调试字段随资源记录持久化（被否：正是当前要消除的违约）。将 workspace root 复制进 managed-group 或联邦记录同样被否；只允许原生 Gateway 控制面持有具名根绑定，注册请求必须由 `add-multi-workspace-backend-mounting` 的唯一 root-binding requirement 授权后作为控制面输入送达原生 owner，中继不持久化或转为资源数据。

### D1a：保留三种文件输入，在目标端统一解释

普通文件入口接受相对路径、绝对路径和资源 URI，引用公共 spec「普通文件入口必须保留原生路径输入并统一目标解析」。输入分类复用同一原语：目标平台原生绝对路径优先，只有输入开头的协议形式交给唯一 URI grammar；不能将原生路径中间的 `://` 当协议。POSIX 的 `./build://debug` 和对应绝对路径按原生目录处理，URI 输入的未知协议和非法地址仍明确拒绝、不回退。先固定目标 Gateway/机器与工作区或选定目录，再在目标端按原生平台解释路径，归入同一文件 owner；中继当前目录、source 文件系统与激活工作区切换都不能改变目标。工作区内路径可转换为已有 URI；工作区外路径沿既有能力和授权处理，不能偷偷改为工作区内目标或强制先注册。Windows 盘符与路径分隔符不能先被 source 的 POSIX 规则改写。

原生路径只作为请求输入及目标端临时访问值，内部持久引用、普通响应和跨 Gateway 资源投影继续采用 URI；没有现成 URI 模板的外部目标复用既有 owner/API，不增造 kind、第二解析器或第二操作链路。终端/shell 保留操作系统原生语义；Gateway 原生目录选择/注册是独立管理能力，其裁定不构成普通文件绝对路径禁令。

Windows 长路径复用 `native_io_path`，只在既有目录扫描、元数据、文本发布、归档和下载传输的原生 I/O 点扩展。文件 owner 仍以逻辑路径核对归属、私有目录及暂存目录，不能把扫描返回的扩展路径用于这些比较；普通响应、operation 回执、目录键与业务记录继续使用 URI。创建成功后的列表、读取、编辑和下载必须能访问同一个长路径目标，不能把传统路径长度限制误报成文件不存在。

文件导航库在原构造作用域登记SQLite关闭责任，仅在schema和owner校验全部成功后交给原文件owner。版本、缺表与归属错误使用稳定工作区身份；物理位置及损坏owner原值保留诊断。原生文件/SQLite错误保留类别、错误码和直接原因，不从错误正文猜测路径。初始化失败不发布owner、不静默重建库；修复原输入后同一服务沿原启动入口重试，schema版本与SQL资产保持原合同。

文件树的未注册目录浏览、编辑与快捷目录必须保留。迁移前由本寻址 owner 补齐这类目录的规范引用，不让消费方私建 URI 模板；目录选择本身不等于工作区注册，不得借用当前 workspace_id 冒充归属。接口/快捷方式持久化的真实路径清理必须与该能力同批迁齐，验收浏览→编辑→保存快捷目录→重开全链路，核工作区注册表没有新增或变更；不能将「先注册再访问」作为替代方案。

原 `_absolute_to_workspace_relative()` 曾经用 `path.lstrip("/")` 将外部绝对路径改投工作区，且混用了 DeepAgents 虚拟根；该转换及其直接、间接调用方已随 `d7f7b94d` 删除，用户输入由统一文件 owner 在目标平台解析。聊天 Markdown 只提取展示坐标并在 URL 编码前封装原目标，不解析工作区根、不解码原生路径；只有普通相对简写的真实 404 才保留既有唯一文件名查找，显式绝对路径或 URI 失败绝不改投同名文件，打开引用以内容 owner 返回的 URI 为准。验收从真实读写入口比较三种输入的实际对象，并用外部文件与工作区同名对照文件验证无误读/误写；Linux 跨 Gateway 验收见 `4af8b758`，Windows 实机仍须核目标端内容及 source/relay 不变，不能只测字符串 helper。

### D1b：未注册目录使用 Gateway files 根，控制面绑定与文件执行分离

固定模板为 `boxteam://{gateway_authority?}/gateway/{gateway_id}/resources/files/{directory_id}`，directory_id 由原生 Gateway 创建稳定 UUIDv7；文件 child 使用同一 grammar。不是工作区，不借用执行路由的 workspace_id，也不注册 `WorkspaceTarget`。工作区内路径优先归为已有 workspace files/root。

Gateway 在现有控制面 SQLite 中唯一保存目录 ID→原生根绑定，作为用户明确选定目录的持久控制面事实；这与工作区 mount root 是两类具名例外，不能扩为普通记录路径许可。路径归一/选择入口固定原生 Gateway/机器，依现有文件认证与目标权限解释三种输入，返回 URI/显示名/能力。远端 Gateway 经既有目标路由接受输入并在自身平台解释；source/relay 不解析、不保存根。

文件树、编辑器、快捷方式、目录版本与 operation/SSE 统一消费 URI；删除 filesystem: 展示伪协议、绝对 root_path 和业务库 real_path 等旧引用。后台 staging 只记相对 owner 的稳定 operation/step 身份，源/目的文件记 URI。保留当前精确目录 revision、条件编辑、部分/未知效果、分页快照、去重和同 ID 恢复合同，不用改预期来掩盖失败。

Gateway 用户 profile 的文件预览与目录展开引用同样只保存文件 owner 已返回的 files URI；冷读和更新共用 DTO 校验，旧路径明确失败且保留原件。设置请求及冷读错误只报告字段与类别，不把原生输入或磁盘旧位置复制到普通响应。

Gateway 只为目标端现有文件 owner 提供受认证的同机临时绑定，不执行 enumerate/read/write。首次请求与后台恢复都必须能按 URI 从原生控制面取得绑定；不能只依赖一次请求注入的缓存，也不能把根复制到业务库。实际执行继续复用 WorkspaceService/WorkspaceFileOperations；执行路由是服务归属，不改变资源归属。缺绑定明确失败，不回退当前工作区。实现同批迁齐所有直接/间接消费者后下线旧路径链，不留下 URI wrapper 与旧绝对路径两套执行。

### D1c：原始诊断日志与 Browser 本地页面 URL 的具名例外

2026-10-23 用户明确裁定，唯一寻址 owner 登记以下两项例外，消费者只引用对应 requirement：

1. Gateway 原始诊断日志管理入口：用户主动打开、受现有管理权限保护的原始服务日志与异常堆栈可保留真实文件路径。联邦诊断传递同类原始日志沿用同一管理授权边界，不将日志文字转成资源引用、普通错误正文或解析结果。
2. Browser 本地文件页面 URL：保留原生 `file:` 导航。用户明确打开的本地文件页面 URL 可保留在 Browser 地址栏、导航请求、相关错误、页面快照和恢复检查点，恢复时仍由冻结的目标机器按原生文件系统语义解释；中继与当前激活工作区不能改投目标。这里的恢复检查点许可只针对页面 URL，不包括 checkpoint 文件所在路径、下载/截图产物位置或其它 Browser 存储 locator。

两项只豁免具名入口及列明字段，不为整个 Browser、Gateway diagnostics DTO、日志摘要、普通业务响应或其它持久记录提供路径许可，不新增 URI kind、解析器、文件操作链路或授权方式。普通文件的相对路径、绝对路径和资源 URI 继续按 D1a 在目标端统一解析并复用同一 owner。终端 cwd 的相对路径规范化合同保持。

验收区分正反两面：管理入口本地/联邦原始日志保留原文字且遵守既有管理授权；Browser 页面 URL 经原生导航、失败、快照、重启恢复仍指向目标端原文件；同时核普通错误/资源引用、checkpoint 文件位置、下载/截图位置不借此获得真实路径许可。规范登记本身不代替这些产品验收。

调试方案 fork 的文件阶段复用原 rollout 存储错误出口：在 staging 检查/写入、旧 staging 清理、发布和已发布正文验证的原生 I/O 边界保留异常类别、errno/winerror 与 cause；公开文本携带 `artifact=fork_debug` 和原阶段，真实位置与原异常仅留诊断。布局拒绝沿同一出口报告，不在 API 层扫描/脱敏字符串。原文件调用顺序、manifest/configuration hash、prepared→ready→published journal、来源身份和重放规则保持；公开 fork 失败仍按原创建 owner 中止清理，不增加另一个保存链或迁移。

fork source 的同一文件清单函数供捕获、冻结 marker 校验和附件冻结集合核对复用：先确认根为已有普通目录，os.walk的扫描错误立即抛出，不以默认忽略行为生成残缺清单。原生读取与元数据失败复用原storage错误出口（artifact=fork_source、stage=manifest），布局拒绝只公开原原因；位置与完整原异常保留诊断。排序、排除项、字节hash与文件集合不变，不新增清单owner或恢复reader。

同一 captured marker owner 在位置检查、发布、marker/manifest/control 读取、附件检查及清理的原生 I/O 边界复用原 storage 错误出口（artifact=fork_capture、stage 为原失败阶段），保留异常类别、errno/winerror 与直接 cause，真实位置只留诊断。发布遍历的扫描错误立即抛出，目录符号链接明确拒绝；原 fsync/rename 顺序、marker 字节和 hash 保持。preparing 且尚未发布完整 marker 的私有目录仍按原 Incomplete 合同中止；已发布完整 marker 即使 journal 尚未 CAS 也只按冻结 proof 恢复，不回读 live source。发布或清理失败不能伪报成功，不新增 marker、journal、writer 或恢复链。

fork typed detail 仍仅按同一冻结 SQLite 的声明与精确历史 owner 复制：文件元数据、目标目录创建、复制及原正文 hash 读取的原生失败复用 storage 出口（artifact=fork_source、stage=detail_copy），直接 cause 与真实位置留诊断；目录/文件布局拒绝只公开原原因。查询顺序、locator/owner 校验、原字节和 hash 保持；缺失正文继续交原资源 owner 判断是否必需，不在复制 helper 补造正文、改写历史或放宽发布检查。

隔离 SQLite worker 继续使用原独立解释器、固定读事务与 backup，不改变 stdin、成功 manifest 或30秒预算。父进程原生调用失败复用 storage 出口（artifact=fork_source、stage=sqlite_worker），保留原异常类别、errno/winerror和直接 cause；子进程非零退出仍是原 RuntimeError，公开退出码，原 command/stdout/stderr/status 保存在 CalledProcessError cause，完整 stderr 进入诊断，不声称跨进程保留子进程的原生异常对象。超时保留 TimeoutExpired 类别、原30秒与原异常 cause（stage=sqlite_timeout），普通文本不包含解释器或文件路径；原 subprocess.run 负责终止并等待 worker 后才清理私有目录。持 rollout 锁时源页/WAL/SHM均不变，session-control 在线 reader 仍允许 SQLite 更新 SHM 读标记而不改持久页/WAL；不增加另一条复制链。

同一私有目录的创建和清理在各自原生边界复用 storage 出口（stage=sqlite_private_create / sqlite_private_cleanup），原异常类别、errno/winerror与cause保持，位置只留诊断。原 TemporaryDirectory 仍唯一拥有临时目录，以显式 finally 调用原 cleanup；返回成功 manifest 前必须完成清理。非零退出在 finally 之前形成原 RuntimeError/CalledProcessError并记录stderr，使后续清理失败的原生cause/context与诊断保全两项故障；不能因清理异常丢掉worker失败证据。创建失败不启动worker，清理失败不伪报释放或成功，不增加补偿writer或第二清理链。

上层 rollout 来源捕获的原失败清理不再使用 rmtree(ignore_errors=True)：仍只删除同一 captured_root，清理成功后抛回原捕获错误，清理原生失败则复用原 storage 出口（artifact=fork_source、stage=cleanup），保留异常类别、errno/winerror与直接cause，真实位置只留诊断。此前的捕获错误通过原生异常context及诊断保留；不把删除失败当作已释放，不新增重试worker或清理链，后续请求仍沿原创建owner/journal协调中止和重试。

rollout 来源捕获的直接原生 I/O 复用同一 storage 错误出口（artifact=fork_source）：来源布局为 source_layout，目录创建与私有权限为 preparation，锁复制为 lock_copy，正文复制为 rollout_copy，SQLite 副本存在性为 sqlite_snapshot，JSONL 冻结为 jsonl_snapshot，control 文件读取为 control_snapshot，manifest 写入及目录 fsync 为 manifest_publish。原生异常保留类别、errno/winerror 与直接 cause，路径和完整原错留诊断；Windows ACL 原语已有的 RuntimeError 包装仅在该调用处投影，保留其原类别及启动/超时 cause，不扩大捕获范围或改动权限原语。已有 captured_root 的创建碰撞发生在当前请求取得目录所有权之前，明确报错并保留原目录与正文；后续失败仍沿原 owner 清理。仅给原生调用增加错误上下文，SQLite reader/worker、typed detail、复制顺序、锁、manifest 编码/hash、journal 和成功路径保持。

冻结 artifact 的来源导出继续复用原分页与分块接口，不创建清单缓存或另一套传输实现。原 os.walk 显式传播扫描错误，并拒绝原先会被 followlinks=False 静默跳过的符号链接目录；根与每层目录必须可读，不能把扫描失败解释为空清单或缺失文件。清单元数据与分块读取的原生错误复用 storage 出口（artifact=fork_transfer、stage=export_manifest / export_chunk），保留类别、errno/winerror 与直接 cause，真实位置和完整原错留诊断。原文件顺序、128文件分页、64KiB分块、offset/长度/hash、source/target身份、marker最后接收和有界并发保持，接收与原清理/journal链不改；故障解除后新请求仍按原冻结历史完成传输及冷恢复。

目标接收的原生位置检查、目录准备/open、write、flush/fsync、close及文件/整份artifact目录发布分别在本机调用处复用原storage出口（artifact=fork_transfer；stage为receive_path / receive_open / receive_write / receive_flush / receive_close / receive_publish）。显式finally只关闭原有stream，保持原有成功顺序和清理责任；写入与关闭同时失败时保留两份原异常、cause/context和诊断。上下文不跨越来源transport的await，来源连接失败或取消不能误标为本机文件错误；原网络异常对象保持。所有接收者回收完成后才沿原import/capture owner清理，原marker最后接收、目录同步、hash/offset校验、四路并发及两端journal/身份保持，不新增接收器或补偿writer。

目标 rollout 暂存沿原逐文件复制和准备链路使用同一 storage 错误出口（artifact=fork_target；stage 为 staging / staging_copy / staging_cleanup / preparation_sync），保留原生类别、errno/winerror 与 cause，位置及原错只留诊断。目录创建仍在当前请求取得所有权后的清理范围之外，碰撞不得删除既有目录。复制的布局/hash拒绝不向普通错误输出内部 locator；失败清理若再失败保留两份原因。准备后的 bottom-up 文件/目录同步显式传播扫描错误，拒绝缺失根、符号链接和非普通文件，不能将漏同步当成功；保持原字节复制、SQL、hash、同步顺序及发布/journal owner，不另建安装链。

### D2：保留既有段序，scope_id 全必填

采用 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...}`，而非简化段序。

**理由**：简化段序（把 `scope_id` 变可选、去掉 `resources`）会让 `user`/`gateway`/`inline` 这些 scope **重新依赖隐含上下文**来补齐身份，与「根除当前工作区隐含前提」的目标直接矛盾；且会与仓库真实 grammar（既有 `resources/{kind}` 段序）分叉，制造第三套形态。保留段序 + scope_id 全必填，是「沿用真实 grammar 且把显式原则推广到所有 scope」。

**备选**：简化段序（被否，见上）；为每个 scope 定制不同段序（被否：多套语法正是本 change 要消除的）。

### D3：scope 闭集定稿为 workspace/user/gateway/inline，memory 移出

**理由**：权威表实测**当时的**既有闭集只有 `workspace`/`gateway`/`builtin`/`memory`；`user` 是用户要求新增。`memory` 零生产构造方、resolver 不比对 scope_id、`kind="memory"` 零构造、container 未装配，且两点式形态（无 `resources`、无 kind）会绕过固定段与 kind 校验——把它当 scope 等于在统一语法上开后门。故移出并显式标注「`memory` 不是 URI scope」，且因其 domain owner 与状态本体从未接入（无 URI 替代 owner 的需求），解析器侧 MUST 物理移除该两点式特例分支并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**：特例分支与 `memory_display_uri` 已删除，`_SCOPE_KEYWORDS`/`_DESCRIPTOR_KINDS` 均不再含 `memory`）。

**备选**：保留 `memory` 于闭集并为其定义 scope_id（被否：无任何事实支撑，会固化一个空洞）；把 `memory` 两点式当合法特例（被否：破坏「单一语法」，即本次要根除的双轨）。

### D4：scope_id 必须由真实身份推导，禁止硬编码

**理由**：现状 `skill_runtime.py:539` 把 `gateway` 与 `builtin` 的 scope_id 都硬编码为 `local`，二者逐字相同；resolver 期望的 `distribution_id` 全仓零生产赋值。让 `gateway`→真实 gateway_id、`inline`→真实 distribution_id，才能让 scope_id 真正承担身份，否则「必填」只是形式。`user`→`local` 依据 AGENTS.md 的单用户本地程序前提。`inline` 的来源已由 D4b 定稿为发行 manifest 的 `distribution` + `version`。 **（修订注，`298ef599` 落地）**：该 `else "local"` 已按 3.3 拆开，`inline` 改走 `app/core/distribution_identity.py` 的 `load_distribution_id`（`f3bd8213` 把 version charset 收紧为 `[A-Za-z0-9.-]`），`distribution_id` 不再是全仓零生产赋值。

**备选**：继续用 `local` 字面量（被否：scope_id 失去区分能力，且 `inline` 与 `gateway` 冲突）。

### D4b：inline 的 distribution_id 来源与编码定稿（用户裁定）

**决定**：`inline` scope 的 `distribution_id` 由发行包 runtime manifest（`packages/launcher/runtime-manifest.schema.json`）的 `distribution` + `version` 确定性推导；`version` 合法字符集为 `[A-Za-z0-9.-]`（semver 标识字符集，**不含 `_`**），编码规则为 `.`→`_` 的**单射**映射，再以 `-` 连接 `distribution`。**唯一实现**是 `app/core/distribution_identity.py` 的 `_VERSION_PATTERN` 与 `encode_version`（`version.replace(".", "_")`），已由 `f3bd8213` 落地，本节口径 MUST 与该实现保持一致。

**实测依据**：manifest 实测路径 `out/development-runtime/runtime-manifest.json`（由 `scripts/launch/dev.mjs:177-207` 写、`packages/launcher/src/gateway-supervisor.mjs:90` 以 `BOXTEAM_RUNTIME_MANIFEST` 传给 Python 侧），真实字段 `distribution: "source-development"`、`version: "0.0.2"`；`version` 取自根 `package.json`（`packaging/runtime/versions.mjs` 的 `BOXTEAM_VERSION`）。`distribution` 是 schema 枚举 `source-development`/`source-installed`/`npm`/`standalone`，全部落在 URI charset `[A-Za-z0-9_-]` 内。

**风险与真实反驳**：`version` 实测含点号（`0.0.2`），而 `_NAME_CHARSET`（`grammar.py:23`）不含 `.`。用真实 `parse_resource_uri` 实测，`boxteam://workspace/source-development-0.0.2/resources/skills/x/SKILL.md` 被 `invalid_character` 拒绝 —— 直接拼接不可行。改用 `.`→`_` 编码后 `source-development-0_0_2` 解析通过。该映射是**单射**（`_VERSION_PATTERN` 已排除 `_`，`_` 只可能来自原点号），但**不是可逆/解码**关系：生产 resolver 只做 `scope_id` 字符串比对（`resolver.py:139`），不提供解码函数。早先「先 `_`→`__`、再 `.`→`_`」的双步转义**不是单射**：`_` 本身就在其旧 charset `[A-Za-z0-9._-]` 内，与原点号同码，单符 `_` 与 `..` 会得到同一个编码 `__`。该双步转义已由 `f3bd8213` 删除，MUST NOT 恢复。

**为什么不用「去点法」**：`version.replace(".", "")` 会把 `0.0.2`/`0.02`/`00.2` 全部压成 `002`（实测碰撞、非单射），故否决。

**拆分唯一性**：`distribution` 枚举自身含 `-`（如 `source-development`），故还原 MUST 用枚举最长前缀匹配，MUST NOT 用「首个 `-`」裸切；枚举内无一取值是「另一取值 + `-`」的前缀，故 `(distribution, version)` 拆分唯一。`version` 含 `[A-Za-z0-9.-]` 之外的字符（含 `_`、`+`）时 fail-closed，MUST NOT 静默丢弃。

**为什么 MUST NOT 放宽 charset**：charset 与拒绝码是已登记 grammar 的一部分；为解决一个编码问题而放宽 `.`，会引入 `.`/`..` 段混淆与规范化歧义，并与「不新增转义后门」冲突。故选编码、不放宽语法。

**为什么 MUST NOT 用目录名/安装路径**：同一发行包在不同机器、不同路径安装时路径必不同；用它会让跨 gateway 寻址从根上不成立。manifest 的 `distribution`+`version` 是发行包自带的稳定身份。

**缺失 fail-closed**：`distribution`/`version` 缺失即显式失败，不设 `local` 默认（AGENTS.md「永不返回虚假的默认值」）；开发态 `source-development` 亦走同一 manifest，不单开分支。

**备选**：放宽 charset 允许 `.`（被否，见上）；用目录名或环境变量（被否：不稳定）；缺字段时回退 `local`（被否：虚假默认值）。

### D4c：gateway 的 gateway_id 来源与注入 owner 定稿（用户裁定）

**决定**：`gateway` scope 的 `scope_id` 取值来源为 `${BOXTEAM_HOME}/gateway/identity.json` 中由 `app/gateway/credentials.py:138` 的 `load_or_create_gateway_id` 生成的随机不透明 id（形如 `gateway_<32hex>`）；MUST NOT 用 host:port 或监听端口。注入 owner 为 **Gateway 侧**：Gateway 代理 `/api/v1/*` 时附加 Gateway 身份头（按既有 `X-BoxTeam-*` 约定命名为 `X-BoxTeam-Gateway-Id`），workspace 后端从请求上下文读入。因同一 workspace 后端可被不同 Gateway 挂载，gateway 身份 MUST 按请求注入并读取，MUST NOT 用进程级单例或「当前激活」态；缺失或非法时 fail-closed。MUST NOT 改写或复用 `X-Request-ID` 的语义与职责。

**实测依据**：`load_or_create_gateway_id` 生成 `f"gateway_{secrets.token_hex(16)}"` 并写入 `identity.json`；`app/gateway/` 与 `app/gateway/remote_gateway.py` 已在该路径读写（原 `main.py` 行号随 `45f9ada2` 拆分失效：现分布于 `lifespan.py`/`routes/**`）。仓库既有 `X-BoxTeam-*` 头为 `X-BoxTeam-Federation-Token`/`X-BoxTeam-Workspace-Id`（`app/gateway/auxiliary_proxy.py:91-92`、`app/gateway/registry.py:1837-1838`），此前无 gateway 身份头。

**前端口径冲突（登记影响项）**：`src/clients/web/src/state/session/sessionCatalogOutbox.ts:32` 的 `CatalogOutboxPartition.gatewayId` 注释逐字为「稳定 Gateway 身份：本地 Gateway 用其监听端口，远程 Gateway 用其 gateway_id。」，与本决定「MUST NOT 用监听端口」冲突；统一口径归 Gateway 侧（网关身份由 Gateway 按请求注入、取值按 spec requirement 推导），该注释 MUST 在实施期清理。

**备选**：用 host:port 或监听端口（被否：不稳定且跨机不可比）；复用 `X-Request-ID` 承载（被否：违反「任何一层不得补造第二个请求 ID」）。

### D5：authority 缺省 == 本机 gateway == 显式本机 gateway_id

**理由**：既有形态是「authority 缺省」的特例，大量在途规划依赖「不带 authority 即本机」。让显式本机 gateway_id 与缺省**同解**，可避免「本机 vs 显式本机」被当成两个地址而破坏规范化。

**备选**：二者含义不同（被否：产生两个等价地址，持久化记录因写法不同失去可比性）。

### D6：星型解析在 gateway 层收敛，边界只传逻辑事实，上界是 policy 常量

链路：本地 parse（fail-closed）→ 本机按 workspace registry 解析 / 对端按联邦关系转发（hub 直连直接 spoke；spoke 经唯一 hub 一次有界 transit）→ 不可达/未共享/未找到一律 fail-closed。

**理由**：只有实际持有资源的进程能做权威解析；转发方不得猜测路径。把 `visited set`/`max_transit_gateways`/`max_gateway_hops`/总 deadline 做成显式 policy 常量，是因为拓扑会变，解析器不应因拓扑调整而重写。星型（最多一次中继）与仓库既有联邦拓扑一致。

**不变量**：`locator 是输入，不是输出`。

普通文件原生路径可按 D1a 作为固定目标的具名请求输入转送；中继不解析、不持久化，也不得将其返回为资源引用。资源 URI 及目标授权仍使用本唯一 owner，不能因支持原生路径另建跨 Gateway 执行链。

**备选**：转发方缓存并本地解析对端资源（被否：第二份事实源）；对端返回 real path 由本机访问（被否：跨边界传资源 locator，且远端路径在本机无意义）；把上界写死在解析器里（被否：拓扑变化即改代码）。经目标 Gateway 按 `add-multi-workspace-backend-mounting` 唯一 root-binding requirement 授权的 workspace registration 是独立控制面输入，可把用户提供的 root 交给原生 Gateway owner；relay 只转送，不授权、不持久化该输入，它不进入资源解析或跨 Gateway projection。

### D7：拒绝码分三套集中登记，不可混用

**理由与裁定**：规范包含三个独立拒绝码闭集——`ResourceUriGrammarError.reason_code` 的分段编码修订闭集（现行17码快照、修订18码，见唯一spec登记）、resolve 解析/授权期闭集 7 个（原有 6 个加 `unsupported_view`）与**联邦解析期第三套**（`app/gateway/federation/errors.py` 的 `FederationError.code`，归属 `app/gateway/federation/`）。`unsupported_view` 专用于已识别的 view 与资源 kind 不兼容。当前生产 resolver 已登记七码并维持闭集构造校验；任务 1.3 还包含其它阶段登记的整体验收。三套分别对应「字符串→ParsedResourceUri 的语法期」「已解析后的解析/授权期」与「跨 gateway 联邦解析期」，混用会让阶段职责错位。集中登记可机械检查「无自造同义码」。

**备选**：合并为一套码（被否：语义阶段不同，合并会丢掉「哪一期拒绝」的信息）；每个模块自带枚举（被否：正是要消除的多份定义）。

联邦外层闭集在 `errors.py` 构造时校验；目标 owner 的业务拒绝通过唯一 spec 登记的外层 owner-rejected 合同携带原归属，既有业务消费者还原内层 code。未知 wire 码在接收边界作为 malformed frame 保因并关闭。2026-10-10 源码及四个真实 relay 消费者已随 `82f518e8` 集成，细节以 spec 的唯一登记为准，不在这里再列第二套码。

### D8：identity 与 URI 的职责边界以「可解析性」划分

identity 不可解析、不做寻址；URI 可解析、不承担身份。同一逻辑名跨 scope 是两个 identity；跨来源覆盖是独立 concern。

**理由**：审计里 `SemanticResourceDescriptor` 双类同名与 `contracts.py` 的 TODO 表明，之前把「身份」与「地址/owner scope」耦合，导致字段语义漂移。以「可解析性」切分最清晰。

**备选**：让 URI 兼任 identity 用于去重（被否：URI 允许悬空，悬空后无法去重）。

### D8a：内部 catalog 无真实地址时显式无 URI

MCP/Skill catalog 聚合是 Registry/activation 内部语义资源，当前没有 owner 公布的可访问模板，统一采用必填 `display_uri=null`；真实 Skill 条目与 AGENTS 地址不变。字段、校验、完整性、持久化和恢复只引用 spec requirement「内部语义聚合资源不得伪造 URI」。一次迁齐 descriptor/snapshot→Registry→provenance/serde→activation schema/read/write/fork/detail→必要协议/投影/fixture，不为当前强制字符串 carrier 增兼容层或伪造 kind。该合同已定，源码实施与动态验收仍待。

### D8b：kind 闭集在唯一 owner 登记（owner 裁定，消除待登记）

**决定**：kind 闭集定稿为 `agent-spec` | `skills` | `config` | `session` | `files` | `workspace` | `folder`，各模板以公共 spec 为准。`files` 承载 workspace scope 的源码根及 D1b 的 gateway scope 原生目录根；`workspace`/`folder` 承载已登记的领域导航。新增实现见第8、9组及2.7，原四值历史证据保留其验证边界。会话上下文资源（会话定位）**自身**的 kind 取 `session`，由本 change 在此登记；`config` 承载配置来源文件本身。消费方不再登记同义 kind。

**理由**：闭集一旦闭合，未登记 kind 会被拒绝；若把「会话上下文资源的 kind」留成待登记，并行 change `migrate-session-context-resource-uri` 的设计就无法落地。所有权在本 change，故必须现在登记而非挂成 open question。会话上下文资源的语义本体是「定位一个 Session」，既有 kind 无一覆盖，故取其语义名 `session`。

**含义**：会话上下文资源的规范形态回到标准资源形态 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/session/{...canonical path segments}`（如 `.../resources/session/{session_id}`），与 `boxteam://session/{session_id}#...` 旧自有语法彻底分道。

**备选**：让会话上下文资源复用 `agent-spec` 或 `skills`（被否：语义完全不匹配，等于用错误词汇掩盖新资源种类）；把该 kind 留成待登记（被否：会阻塞并行 change 落地，且违背本 change 的 kind 所有权）。

### D9：配置来源 URI 使用 config kind 且只标识可寻址来源文件

**决定**：配置来源 URI 使用既有 `config` kind，只指向可寻址的来源文件；`layer` 与 `precedence` 保留为 URI 外的兄弟字段。`inline` 是发行包内稳定文件来源，按 owner requirement 使用 URI；没有稳定、可寻址来源文件的 `runtime_override` 可不带 URI，并按资源身份与地址分离的 owner requirement 保持其来源身份。SQLite 仅是内部 carrier，不产生新的资源 kind、地址形式或公开 carrier 字段。

**理由**：URI 表达来源文件位置，逻辑来源层和存储 carrier 各有职责；为非文件来源造地址会把身份、位置和存储实现重新混在一起。

**备选**：为 runtime override 或 SQLite carrier 新造 kind / URI（被否：它们不是新增资源类型或来源文件）；把 layer 塞进 URI（被否：URI 只承载位置，layer 是并列语义）。

### D10：配置来源迁移直接复用 config 侧既有平级属性模式

`ConfigSourceLayerRecord`（`config/state.py:475`）的 `source_path`/`backup_path` 与 `ConfigSourceJournalRecord.source_path`（`:632`）是已存在的 real path 持久化违约。迁移形态 = 把 `path` 换成 `uri`，其余 sibling 字段原样保留，与 `ConfigSource`（`config_sources.py:16`）平级结构对齐。

**理由**：用户明确要求「直接复用 config 侧已跑通的模式，不要另发明一套」。

**备选**：为 URI 单开一个嵌套结构（被否：与 config 侧既有模式分叉，制造第二套表示）。

### D11：正名 builtin 到 inline 且 layer bundled 到 inline，经影响评估后定为安全改名

**理由**：同一概念三个名字（layer `bundled`、scope `builtin`、config layer `inline`）——正是本仓库要求根除的。两处都改名后 shim 退化为恒等映射可删。

**影响评估**：`layer` 参与 entry identity 与 catalog payload；scope/layer 必须在实际 producer 和消费者一起收敛，不构造旧开发数据迁移。历史“activation writer 生产未调用”的调查已被真实 execution/Runner/Factory/Saver 接线取代，不能继续当准入门；当前新写入与恢复按 D8a 及 spec 的单一合同处理。真依赖 URI scope 的 grammar/resolver 与无关工具 origin/主题等 `builtin` 同名继续分开核对。

**备选**：只改 scope 名不改 layer 名（被否：留下 layer `bundled` 与 config layer `inline` 的同概念异名，且 shim 无法退化为恒等）。

### D12：与其它 change 的接口以「术语冻结 + 单向引用」实现

本 change 是术语、scope、scope_id 语义、语法、kind 闭集、拒绝码与配置来源 layer/precedence 语义的唯一 owner；另外四个 change 引用。

**理由**：用户要求各方不得各自重新定义；物理删除在途 change 的 URI 定义不可行（其 requirement 已被引用），故采用「引用 + 声明从属」。

**备选**：把 URI 语法从在途 change 的 spec 中物理删除并全迁到本 change（被否：破坏未归档 change 的 requirement 连续性）。

### D13：config layer 表示逻辑来源，carrier 与快照保持分离

**决定**：config `layer` 只表示逻辑配置来源，闭集与既有 precedence 由 spec requirement「配置 layer 必须表示逻辑来源且与载体和快照分离」唯一登记。读侧将 runtime override 从旧 `sqlite` 值归入逻辑来源 `runtime_override`，保留原 precedence；SQLite 继续作为内部 carrier，不暴露 `sqlite` layer 或新增公开 storage/carrier 字段。`active_snapshot` 与 `pending_snapshot` 是快照状态，不是配置来源，必须位于 `sources[]` 之外。各配置数据 owner 对自己的 `source_key` 映射各自保有唯一权威表；所有读路径一致使用对应映射。

旧开发中间数据不要求兼容：升级实现可以删除并重建不兼容的中间数据，不得为其增加双读、回填或兼容映射。该决定不改变 URI kind、语法或已有地址形态。

**理由**：`layer` 描述配置为何生效，carrier 描述状态存在哪里，snapshot 描述一次完整配置状态；把这些角色压进同一个 layer 既误报来源，也让 active/pending 状态伪装成可寻址的来源。

### D14：导航类型、根模板与路径编码分开

本次按 spec「导航形态与操作能力必须由资源 owner 公布」定稿 directory/file/domain_object。skills/{name} 是技能包根，其后是文件 child path；files/root 是 workspace 源码树根 URI，原生 Gateway 通过唯一 workspace-root binding 在本地取得实际根，URI、解析结果与普通 DTO 均不返回物理根。该 binding 的注册与授权（含本地 managed token、远端 `managed_workspace_admin`、目标 owner 判权）、冷启动和唯一持久化 owner 由 `add-multi-workspace-backend-mounting` 定义，本 change 不建立第二套授权规则。config/agent-spec既有地址为单文件，Session/assembly为领域对象，Session附件/工具结果由原owner公布单文件引用，不开放私有存储树。文件工具消费单独change，避免把目录工具、context激活和业务资源ID合成一种万能对象。

身份段继续用既有严格profile，包括D4b distribution_id编码；该段不支持escape。文件段使用spec的单次UTF-8解码和规范重编码，保留原Unicode、大小写和字面百分号，不将filename套身份charset。中央grammar接收wire URI、构造器接收未编码segments；所有工具共用，不能各自unquote或把glob/grep pattern当路径。文件shape、child规则和operation由实际owner发布；资源根模板固定登记，不引入插件注册表；物理 workspace root 只取自具名 Gateway binding，不由 URI 编码、扫描磁盘或猜测。

### D15：轻量导航与会话产物模板

普通列目录/搜索/读取只需要可解析URI和内容，不要求子文件都持久注册ResourceIdentity/快照；显式追踪与历史恢复仍走来源owner。文件hash按需要计算，分页不依赖逐文件快照。本owner登记session/{session_id}/attachments/{attachment_id}和session/{session_id}/tool-results/{result_id}单文件模板；该处各段均是业务身份，不用文件编码放宽。attachments现有二进制接口仍归原owner。跨端普通文件读取/patch在目标端执行，联邦寻址不扩权，执行错误/部分或未知效果归文件操作合同。

工作区文件附件复用 `WorkspaceFilePaths` 解析相对路径、绝对路径及 files URI，在 `MessageService.create` 返回 DTO 前统一引用、默认文件名和 MIME；Job、SSE 与 canonical 消费同一对象。`UserContentBuilder` 的直接入口也复用同一归一方法，删除附件自带的原生路径解析器及旧相对路径输出。Session 附件仍调用 blob owner 的发布校验与预览端口，不复制正文、不改写既有历史。用户正文保持原字节；视频抽帧以临时目录为 cwd、仅传相对文件名，文件 I/O 错误在最终访问边界使用 URI 和 errno，原始异常保留为 cause。

### Fork容器模板与资源目标检查

统一owner登记session集合根/Session/Thread三种domain_object模板；使用既有session kind，不新增thread或workspace URI kind，不将领域对象转成文件树。source Thread精确归属，target用容器URI和创建意图；view/revision/anchor仍是兄弟字段。登记后必须同批迁构造器、parser、scope路由和消费者，当前只支持非空Session尾段的实现不构成对新模板已交付的证据。

统一入口复用解析和domain owner路由，得到目标相关的操作决定，而非新建通用资源管理层。ResourceIdentity保持不透明；现有retention_scope、关联、revision/hash/length、availability和owner operations用于判断。需要retain/export/import能力时在对应owner合同登记，不把read/activate/observe自动升级为复制授权。kind固定的不可变性质由owner提供，缺少真实能力必须明确失败，不通过自由metadata键或新增fork布尔字段决策。

同工作区附件正文已经去重共享，优化重点是避免多余identity remap/复制清单及保留正确目标生命周期。源/目标实际存储域由owner核实，URI authority不同不必然代表存储不同；hash相同也不证明可访问。目标决定和回执复用现有fork journal，通用入口不保存第二份权威resource状态。

## Risks / Trade-offs

- **[契约 v1 曾下发且与真实 grammar 不一致]** → 本 change 全部产物已切到 v2 并按权威表定稿，proposal/design 显式记录变更与理由。
- **[权威表推翻 v2 两处初审]** → 已在 proposal/spec/design 逐处改正（`user`/`inline` 为新增、kind `config` 为新增），避免把初审当定稿。
- **[scope_id 由字面量 local 改为真实身份推导会改变既有字符串]** → `gateway`/`inline` 现共用 `local`，改动产生不同 URI 字符串；因这些字符串只进内存 registry 与响应、不落盘，属契约级调整而非数据迁移（见 D11）。
- **[破坏性语法改动打断在途实现]** → 迁移计划显式列出旧形态（含 `skill_runtime.py:52/:619` 裸拼接、`:538` 的 shim、layer 名）一并收敛，不留别名或双读；tasks 把「删旧解析实现」与「修消费方」合并为单一步骤，避免悬挂中间态。
- **[跨 gateway 解析引入新失败模式]** → 拒绝码分三套闭合且 fail-closed；对「未授权存在」与「不存在」返回同一结果，避免 locator 泄露；上界为 policy 常量，便于审计。
- **[distribution_id 曾是空洞]** → 权威表证明其全仓零生产赋值；来源已由本 change 定稿为发行 manifest 的 `distribution` + `version`（编码规则见 spec 对应 requirement），空洞从「来源未定」降为「尚未实现装配」，实施期按 D4b 接线。 **（修订注，`298ef599`+`f3bd8213` 落地）**：`load_distribution_id` 已接入 `skill_runtime._layer_scope_identity`，本项由「尚未实现装配」变为已装配。
- **[memory 语义不明可能诱使猜测]** → 已确证它不是 URI scope，列入 Non-Goals；因其 domain owner 与状态本体从未接入（无 URI 替代 owner 的需求），解析器侧 MUST 物理移除两点式特例分支并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**）。

## Migration Plan

1. 本 change 落地寻址 capability：术语表、scope 闭集与 scope_id 表、kind 闭集、**分三套**拒绝码集中登记处（spec 层）。
2. 实现统一语法与规范化单一实现，由 `app/core/resource_uri.py` 承载唯一纯 grammar 并物理下线 `virtual_resources/grammar.py` 的旧实现；同一步内修正 `skill_runtime.py:52` 与 `:619` 的裸拼接、删除 `:538` 的 `bundled`→`builtin` shim、并把 layer 名同步正名为 `inline`（不暴露中间态）。
3. 把 `distribution_id` 接到已定稿来源（发行 manifest 的 `distribution` + `version`，编码规则见 D4b；已由 `298ef599`+`f3bd8213` 装配），并把 `gateway` 的 `gateway_id` 接到已定稿来源（`identity.json` 的 `load_or_create_gateway_id`，由 Gateway 侧按请求经 `X-BoxTeam-Gateway-Id` 注入，见 D4c）；落实 `gateway`/`inline` 的 scope_id 由真实身份推导。 **（修订注（2026-10-01 第四轮复核更正）：`distribution_id` 部分已由 `298ef599`+`f3bd8213` 落地；`gateway_id` 请求级注入已落地——`64ba30c8`/`53befbfc`/`9881a3b2`）**
4. 接入解析链（本机分支），使 Skill/配置/状态链路改用 URI 解析，而非仅打印。
5. 按 D10 把配置来源 real path 迁移为 URI 兄弟字段（`config/state.py` + `config_sources.py` + `api/config.py` 对齐）；layer、precedence、URI 是否存在、内部 carrier 与 snapshot 边界均按 D13 及其 spec owner requirement 实施。
6. 接入 gateway 层星型解析、policy 常量上界与集中登记的拒绝码。
7. **迁移面**：来源控制与activation按各自唯一owner落地「必要字段 + 新写入 + 切读路径」，不扫描或改写旧开发数据。此前157 live + 44 dev/temp库中boxteam URI为零是历史基线取证，不能当作实施后仍零落盘的前提；新增URI持久化、附件与工具结果旧scheme消费迁移分别按其owner合同验收。
8. 与其它 change 对表：会话上下文 URI 与多工作区复用本 change 的 scope/scope_id/kind 归属；`memory` 在各方均按「非 URI scope」处理，解析器侧特例分支已物理移除（提交 32bc6256）并以 `unknown_scope` 类拒绝码 fail-closed 拒绝。
9. **回滚策略**：本 change 为规划产物；实施若需回滚，回滚到「旧语法 + 单一打印用途」状态，但必须在同一原子步骤内恢复所有消费方，不得停留半接入态。

## Open Questions

以下是契约层仍需 owner 拍板的点，**不自行放宽**：

1. **gateway authority 与既有 connection_id/gateway_id 的对应（已定稿，不再是 open question）**：v2 要求承载**稳定 gateway_id**；用户已裁定来源为 `${BOXTEAM_HOME}/gateway/identity.json` 的 `load_or_create_gateway_id`（`gateway_<32hex>`），注入 owner 为 Gateway 侧按请求注入，见 D4c 与 spec 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」。剩余仅为实施期装配，不影响契约定稿。
2. **distribution_id 的真实来源（已定稿，不再是 open question）**：用户裁定采用发行 manifest（`packages/launcher/runtime-manifest.schema.json`）的 `distribution` + `version`；编码规则与缺失 fail-closed 见 D4b 与 spec 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」。剩余仅为实施期装配（当前零生产赋值），不影响契约定稿。 **（修订注：已由 `298ef599` 落地装配、`f3bd8213` 收紧编码，不再是待办）**
3. **悬空 URI 的保留期与 GC 归属**：spec 只要求「悬空是合法值」，未定义留存策略；可能与 Session 删除 tombstone 的恢复窗口相关。
4. **ResourceIdentity 的编码形态**：只要求「不透明、稳定、revision-free」，未限 length/charset 上界；是否由本 change 一并闭合需确认。

## 契约的单一定义点（供四方 change 引用，不复制）

契约的**逐字权威文本**存在于本 change 的规划产物，二者互为唯一来源：

- `openspec/changes/add-unified-virtual-resource-addressing/specs/virtual-resource-addressing/spec.md`：定义三层分离、scope 闭集与必填 scope_id（含 `gateway` scope 的 scope_id 由 Gateway 身份文件按请求注入推导、`inline` scope 由 manifest 推导）、URI 语法、kind 闭集、gateway authority、星型解析与 policy 常量、**分三套**拒绝码、identity 独立、默认寻址政策与「新写字段」迁移面，以及配置来源 URI、逻辑 layer/precedence、carrier/snapshot 分离与多工作区显式 scope_id；`builtin`→`inline` 正名归并于相关 scope/layer 定义。
- `openspec/changes/add-unified-virtual-resource-addressing/tasks.md` 任务 `1.1`：术语表登记处；任务 `1.2`：scope 闭集与 scope_id 唯一表落点；任务 `1.3`：**分三套拒绝码集中登记处**（新增/更新拒绝码闭合集的唯一落点）。

实施阶段的机械约束（写入任务，不在本 change 执行）：

1. 拒绝码唯一处：新增码只在任务 `1.3` 指定的登记处出现一次，且 MUST 明确归属 grammar 或 resolve 闭集；其它模块与其它 change 通过引用使用。
2. 术语唯一处：`资源身份 / ResourceIdentity`、`资源 URI`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution` 只在任务 `1.1` 登记；其余 change 只引用。

### 四方接口现状（审计，供用户核对）

- `migrate-session-context-resource-uri`（并行）已声明「不拥有 URI 语法本体与拒绝码登记」。
- `add-multi-workspace-backend-mounting`（并行）引用了 `资源身份` / `gateway authority` / `real path` / `星型解析` 术语，与契约一致。
- `add-context-injection-lifecycle`（在途未归档）已加「归属声明 / 指向」，其残留旧词汇与 `memory` 形态已就地收口。
- `add-workspace-persistent-resource-management`（第四个 change）持久化领域 owner 的工作区资源记录，持有稳定 `resource_id`（不存 real path）；已加「引用本 change 的 identity + URI 持久化引用政策、不复制定义」声明。


fork 目标目录发布、恢复及最终 rollback 清理在原目标排他锁内复用既有 storage 错误出口（artifact=fork_target，stage 为 publication / publication_recovery / publication_finalize）。原生类别、errno/winerror与直接 cause 保持；安装失败后回滚再次失败时，两份原异常仍按原 cause/context 保留。真实位置及完整原错只留诊断，普通错误和创建记录不包含目标路径或内部 locator。原 target→rollback、stage→target、各次目录同步、失败还原和最终删除顺序逐项保持；恢复只消费同一 materialization 的既有目录与 journal，不增加安装/清理链或 schema。故障解除后仍沿原恢复或产品新请求读取同一历史 checkpoint、metadata、pending writes、assembly owner 与 request_hash。
