## Context

变更前附件正文位于工作区，blob 身份由 digest 推导，逻辑附件及访问绑定 Session/Thread；启动与 Session 删除按 blob 创建时间即时回收。当前线上仅支持 schema 4 独立目录；schema 3 通过下文离线迁移显式安装，新上传不再写 blob/owner-ref 表或生成旧 Session URI。原件、预览、派生区、运行时代次与最后一次零关联后的 30 天期限由同一目录 owner 管理。

Terminal/Browser 已共用资源关联和 operation lease 规则：正常 unload 立即删除 thread 临时资源，手动 detach 后才等待 30 分钟。孤儿扫描跳过仍有关联的记录；恢复只核外部资源状态，不能解决失效关联。Thread residency 只保存本进程真实 runtime；重启后的冷 Thread 不会凭空取得可卸载的 generation。

## Goals / Non-Goals

**Goals:** 目录是关联、Shell 工作目录和回收单位；异常退出不依赖 Agent 主动 detach；复用原 owner、URI、文件操作和冻结输入链路。

**Non-Goals:** 不新增云端存储、每文件引用计数、通用文件版本控制或附件专用工具。目录主链、原文件/Shell 注入及 Linux/Windows 本地和真实跨机器 URI 产品流已经完成；完整生命周期故障组合与挂起操作/关闭期限仍按后续纵向切片推进，不将局部通过视为整个变更完成。Agent 团队、成员、委派及会话协作通信按用户裁定保留 TODO，不在此变更追加实现或专项验证。

## Decisions

### 1. 每次独立上传得到独立目录资源

附件 owner 分配稳定资源 ID，在 `.boxteam/attachments/YYYY/MM/DD/{attachment_id}/` 下保存目录，UTC 日期在创建 intent 冻结，唯一 catalog 保存受检 locator。原件位于 `original/`，预览位于 `preview/`，处理输出位于 `derived/`；相对文件路径继续使用统一 owner 的校验和编码规则，不按当前日期或用户显示名寻找目录。

相同内容的独立上传不合并可变目录。目录没有“整体不可变 digest”；原件、预览及已进入 canonical/assembly 的具体版本有自己的 digest/length。原件保持不变，Shell 处理使用新文件或子目录。尚未作为历史输入冻结的派生文件可按原文件 owner 能力修改，不为每次编辑建立另一套历史。

### 2. 统一寻址与 Shell 工作目录

统一 URI owner 登记 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/attachments/{attachment_id}` 为 directory 根，并登记其文件 child 规则；在该 owner 的 kind 闭集中增加 `attachments`，不新增 scheme、query 或真实路径字段。新上传消息使用该根下原件 URI，并提供目录 URI、文件名、MIME 和处理结果写入提示。既有 Session 产物 URI 只能作为原历史身份保留，按原 provenance/owner 授权解析，不能继续生成新旧两套附件地址。

2026-10-24 目录寻址基础已接入唯一 grammar：附件根只接受 workspace scope 和稳定 `att_<32 hex>` 身份，保留旧 ID；child 复用 files/skills 的单次解码及规范编码。原 `file_uri_root`/`file_uri_child` 移入该 owner，迁齐全部生产和测试调用后移除原定义，不留转发。导航 shape 将根限定为 directory。Web 文件树父级与 Markdown 相对链接共用后端已校验 URI 的根拆分，不在客户端另建完整 grammar；附件相对链接停在其目录根，原生绝对路径仍交目标机器解释。协议的可选 directory_uri 已经由原生成脚本迁齐，线上上传、媒体读取与文件入口均注入同一个目录 owner；旧历史缺少目录字段时不补造或重写。证据见 `out/tests/temp/attachment_uri_contract_20261024/artifacts/`。扩大导航集成的旧删除诊断断言在原 HEAD 同样失败，原件保留；另一项因 fixture 重用目录失败，单独场景通过，未放宽任何断言。

Shell 的结构化 workdir 接受目录 URI，在 URI 指定目标的文件 owner 内取得短期本地 cwd 和操作 lease，再复用原 PTY 执行。URI 目标与 Shell 目标不一致时明确拒绝；不把远端 URI 当本机路径，不改写任意 command 字符串，不把真实 cwd写进普通业务记录。整个执行期间保留目录使用 lease；后台进程仍访问目录时，其真实进程 owner 延续占用，不能在工具提前返回时解除保活。

2026-10-24 的先行切片只接入既有 `files` 目录 URI：Terminal owner 依赖 container 已装配的 `WorkspaceFilePaths`，在创建记录/PTY 前解析精确 `boxteam://` 前缀的 URI；POSIX 原生目录名中的其它 `://` 不改变含义，全部原生 cwd 也在同一 Terminal owner 锚定目标工作区。工具层不再自行解析 cwd、接受另一份 workspace root 或重算公开位置，回执直接消费真实 PTY 的 `cwd_relative`。Shell 原生工作区外/私有目录行为保持，命令正文不做 URI 替换。目录解析中的系统异常和符号链接环完整留在诊断/cause，普通错误只包含资源 URI 和错误类型。该先行切片之后，附件目录 workdir 与进程持久占用也已接入；Windows 实机和远端产品入口验收仍由任务 6.1 负责。

后续 Shell 公共边界已在原 Terminal owner 接入 `TerminalWorkingDirectories` 准入端口，持久记录只增加同工作区附件目录根 URI；目录使用继续写入唯一 `ExternalResourceLeaseLedger`，holder 使用持久终端身份而非本次工具/Turn。准入锁内发布占用后才启动 PTY；工具提前返回不结清，核实终态与冷恢复才结清，未知/缺失回执保留。线上 container 已注入 schema 4 目录 owner，不保留空准入或另一套文件/进程执行链。Linux 真实 PTY 已验证相对/绝对/目录 URI 三种 cwd、后台进程保活、准入与到期 GC 竞争及核实停止后的释放；未知停止结果继续由原 owner 保留，Windows 与远端产品验收仍 TODO。准确候选指纹、命令和原始结果在 `out/tests/temp/attachment_shell_directory_20261024/artifacts/`。

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

已有资源的工具、管理、卸载和孤儿回收操作丢失回执时，由原 owner 将本次精确 lease 与资源未知状态一同写入原账本。普通收尾及 Turn 清理只释放仍 active 的操作，不能释放待核实占用或覆盖 owner 已结清的 settled。同资源其它已完成操作、明确失败回执及单纯的未知资源快照不因此变成未知操作。冷恢复保留原未知结果与错误原因；只有核实资源身份、当前版本和真实终态后才结清，不能由存活快照自动重发旧副作用。创建链现在同样将本次精确原 lease 持久转为待核实；存活快照不再消除未知创建结果。Terminal/Browser 在有效且唯一的创建 intent 尚未接纳时返回 `creation_receipt`，绑定 resource/workspace/revision 和 `not_created`；owner 核对后将本次未创建 intent 关闭为不可见 tombstone、解除其关联并结清原占用，包括 Terminal 受管 cwd。身份冲突、普通 HTTP 失败、回执丢失或错误版本不能被当作未创建。Browser 只有在原 failStartup 链释放运行态并落盘成功后返回 `failed` 及完整失败快照；失败后的 stop/detach/delete 继续走原管理入口，不将任意 failed 状态加入全局停止集合。未收到正式回执的创建跨 scope/Turn 收尾与冷恢复继续保留未知及原 lease，真实终态仍由原 owner 核实后结清。Terminal 就绪前的 PTY 退出现复用原 Worker 的唯一进程树终止链，将已取得的 PID 和清理结果结构化传回；Linux forkpty 根已消失时仍按原 SID 核实后代。宿主只有在正式停止结果、Worker 退出确认和终态落盘全部成功后签发 `failed` 与 `terminated/verified` 快照；Node 保留原记录供回执丢失后的冷恢复核实，同步结清受管 cwd 占用。没有证明的 native 异常、Worker 异常或清理失败保留原未知记录与已取得的句柄，迟到 ready/启动失败消息不改写已结束的启动身份。创建途中未取得外部身份的接管，以及旧代运行操作的接管/终止与 5.4 总期限继续 TODO；Worker 退出与 HTTP 状态均不能替代停止证明。

### 6. 本轮实施的失败隔离

`ThreadResidencyTracker.sweep()` 对每个 Thread 独立尝试清理，失败保留原 scope、generation 和 owner 记录，其它 Thread 仍完成。整批结束后向调用方抛出原错误（单项）或汇总错误（多项），附精确 owner 信息；不改变已有单错误类型或把失败算成成功卸载。`AgentExecutionService` 的唯一周期 worker 记录完整异常后按原 60 秒周期重试，取消和关闭正常传播。这里不解决失效关联或外部未知结果，也不绕过原 owner 核实规则。

闲置扫描通过 `asyncio.wait` 为单个真实卸载任务提供 30 秒等待预算，覆盖该任务中等待运行排空、领域通知和 scope close 的总等待。到期向本轮报告含精确 owner/generation 的 `TimeoutError`，继续扫描其它 Thread；不取消原任务，不解除关联或 lease，不标记 cold。原任务仍由 `runtime_unload` 持有，新 admission 等待同一任务，后续扫描不会为同一代再启动清理。迟到成功沿原关闭链落为 cold；迟到失败由现有后台任务回调完整记录，原状态保留给下一周期重试。这是事件循环能调度时的扫描等待预算，不是保证 30 秒内释放资源或整轮完成的期限。

Terminal/Browser 的 HTTP 层现统一使用可取消的异步 HTTPX，默认 10/30 秒及 Browser run 的原预算余量保持；同次网络传输共用单调总期限，不再把完整请求交给默认线程池。传输取消会关闭客户端连接，仍不证明服务端已经停止；该期限不覆盖资源锁等待、多次请求的 operation 总耗时或 scope 回调。共享 owner 已迁为每条资源记录一把进程内锁；创建、管理、传输授权、工具调用、lease 关闭、fork attach、Thread/Session 清理和孤儿回收均复用该资源的同一把锁，不再跨单资源外部请求持有 owner 全局锁。恢复入口只保留首次对账的 single-flight gate；锁与原 owner 记录一一对应，不形成新的持久账本。列表先按原关联/来源元信息选目标，等锁后重检过滤；多资源清理按资源 ID 固定顺序取得整组锁，仍在任何 detach 前核对完整 lease 集合。包含挂起资源的聚合查询或整组清理仍会等待该资源，同步回调阻塞事件循环时异步计时也无法抢占。任务 5.4 后续继续统一 operation 总截止时间、终止核实和迟到回执，明确 Session 删除、换代 admission 与 shutdown 的有界等待、失败返回和保留事实；本次锁隔离不代表这些合同已完成。


runtime 准入及单 Session 的 runtime 排空现各有一次调用共用的 30 秒单调总等待预算，覆盖等待旧代卸载、构建与多个 Thread 的顺序排空，后续阶段不重置预算。等待超时或调用方取消只结束本次等待，原 build/unload task 仍由原 state 持有，迟到回执沿原链记录；后续重试复用同一任务，不能先建下一代或关闭尚在构建的 scope。Session 删除沿原异常传播停留在 deleting，保留 locator、active 控制 fence 和物理历史，待原任务完成后按同一删除记录继续。该预算只覆盖 tracker 的 runtime 阶段，不覆盖此前领域资源排空、Session gate/retention 等待或整个删除 operation。TODO：真实外部操作的总截止时间与终止核实、同步阻塞和线程池排队、全局 shutdown 与存储关闭顺序仍未完成；不能在全局 shutdown 等待超时后先关闭 catalog，再允许原任务继续写入。

扩展资源 owner 回归发现，记录查询已更名为 `_require_owned_resource_record`，但共享 operation、Terminal/Browser operation 和 Browser 管理入口仍有旧 `_require` 调用，导致取得 lease 或读取记录时直接失败。本轮全量迁齐这些调用和对应测试，保留唯一查询方法，不加兼容入口，也不改变关联、lease 或删除策略。


2026-10-24 传输取消切片已迁齐两个客户端及全部直接/间接测试调用，物理删除同步私有 HTTP 入口，不保留同步生产链。原资源 owner 的 unknown 与恢复拒绝重放规则不变；HTTP 状态错误保留原始诊断与 cause，网络失败继续映射为服务不可用，Browser 408 的 code/retryable/recovery/timeout 字段保持。共享单元 HTTP 装具只替换 HTTPX 的网络 transport，不替换编解码；真实辅助服务验收仍经真实 socket。四项原基线反例分别证明取消后 socket 未关、默认线程池被占满时请求不能推进。最终候选的六项真实 HTTP 测试核对取消断连、unknown 跨重启、拒绝重放、独立资源传输及慢速分段响应不能延长期限；原业务协议回归 259 passed，真实 Terminal HTTP/PTY 8 passed，Browser 故障/恢复 32 passed，正式新矩阵 6 passed。Ruff、编译、命令、候选源码指纹和原始结果在 `out/tests/temp/resource_http_cancellation_20261024/artifacts/`。本轮仅 Linux；未重跑 Windows，不将关闭 socket 等同服务端进程终止，也不声称整个 operation、同步阻塞回调、DNS 执行器等待、Session 删除或 shutdown 已有完整期限。上述接管/终止与关闭顺序继续 TODO，5.2/5.4 不勾选；Agent 团队与协作专项继续 TODO。

## Conflict Review

本 change 是附件目录修订的后继 owner。以下旧 change 的既有验收只证明对应基线；目录主链已在本变更迁齐，尚未完成的 Web/平台和关闭期限验收继续保留。合入/归档顺序为先落现有基础 change，再应用本 change；不得在最后归档旧 change 时覆盖本修订。

| 冲突来源 | 处理决定 | 必须保留的约束 |
| --- | --- | --- |
| itemized 的旧内容寻址附件布局及单 blob fork 验收 | proposal/design、rollout 存储 requirement 与 fork 验收已同步目录/文件资源身份，取消相同上传强制合并；原 blob/owner-ref writer 已下线 | canonical 不保存物理 locator，历史原始身份和 frozen digest/length 不改写，fork claim/publication 原子性不降低 |
| virtual-resource-addressing 的 kind 闭集、单文件附件模板及共享 blob 场景 | 由同一 URI owner 登记上述 workspace attachments directory/child；旧 Session URI 只保历史解析，不生成新引用 | 不新增 URI 解析器、不泄漏真实路径、普通 files/root 仍不能直接遍历 `.boxteam` |
| unified-resource-file-tools 的旧 Session 单文件产物边界 | 附件目录/child、原件只读与 derived 导航/写入已同步其 design 和文件 requirement，工具结果仍为 Session 单文件；Shell workdir 继续由本 change 定义 | 512 像素预览、媒体投影、原件读取、文件工具权限及修订语义保持 |
| persistent-resources 的 thread unload 即时 delete、零关联 30 分钟、workspace 永不自动过期 | 这些规则继续适用于既有 Shell/Browser；附件复用机制但采用明确的零关联 30 天策略 | 显式 detach 不等于停止、有效 operation 阻止回收、未知停止结果不伪报成功 |
| deduplicate-execution-history-storage 的不可变内容复用及恢复/fork保护 | 可变目录不参与 digest 身份合并；已冻结历史输入仍按原 immutable asset/version 复用 | 历史 request_hash 可重建，GC 不删除 checkpoint/assembly 所需版本；不从当前目录补历史 |
| 当前附件 GC 的 `created_at < now-retention` 与 `retention=0` 调用 | 改为最后一次零关联后的 deadline；不能简单把参数改成 30 天 | 在 detach 第 30 天前不删，重启不重置 deadline，删除与 reattach 互斥 |
| 当前恢复只核运行状态、关联存活无对账，以及单项异常停止 sweep | 已接入 tracker 实例/generation token、权威 pin/queue 核对与旧代解绑；外部未知结果的取消/终止核实仍 TODO | 不把日志或任务勾选当作异常退出后的真实资源释放证据 |
| persistent-resources 的 unload 必须核实终态，与扫描等待期限 | 30 秒只限制本轮等待；唯一卸载任务继续持有全部未结清事实，runtime admission/单 Session runtime 排空等待最多 30 秒，真实清理仍由同一任务核实；其它删除阶段和全局关闭仍待收口 | 不把超时当停止证明，不重复删除，不提前释放 lease；共享锁/同步阻塞和关闭期限尚待 5.4 |

## Risks / Trade-offs

- [历史原件在无关联 30 天后不可用] → 明确到期/已回收状态；冻结模型输入与运行恢复依赖单独保护，不静默延长或承诺无限期附件历史。
- [共享目录的派生结果互相覆盖] → 原件不变，模型提示使用新的文件/子目录；文件 owner 保留修订冲突合同，处理结果被用于模型输入时冻结具体版本。
- [Shell 可写目录绕过逐文件观察] → 执行全程占用目录，capture/读取在原 owner 核实文件版本和 manifest；不依赖模型报告写入完成。
- [GC 与 attach/fork/重启竞争] → 复用 owner 事务、lease 和 journal，删除前复核，提交删除状态后拒绝新 attach；删除失败保留可重试事实。
- [慢清理拖长周期或占住共享 owner 锁] → 单 Thread 扫描等待预算 30 秒，同代任务去重；多个慢任务仍会累计等待，涉及同一挂起资源的整组释放仍可能受锁阻断；无关单资源和其它 Thread 的资源集合已隔离。后续 5.4 处理终止核实、整组等待与关闭期限；同步阻塞无法靠 asyncio 超时抢占。不擅自改 30 分钟/30 天期限。

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
3. 已提供显式 `python -m app.services.infrastructure.attachment_blob_catalog.migration prepare --workspace <root> --output <new-dir>` 与 `verify --backup <dir>`。只接受完整附件 schema 3，表、约束、索引和触发器由线上初始化共用的 SQL 顺序生成内存参照；缺失/改写不自动修复。通过 SQLite backup API 保存附件 catalog 与登记 Session/control/rollout 的已提交 WAL 内容；备份快照采用 immutable 只读核验，不在其中生成 WAL/SHM。依权威 catalog 和 journal 枚举并保存原件、派生预览及历史 capture，同时逐字保全已通过准入的 Terminal/Browser owner 记录与关联；终态仍残留的冻结 capture 全部逐字复制，不再次 SQLite backup 改变其字节；materialized/aborted 后已由原 owner 清理的临时 capture 不重建。Linux 执行明细中的合法冒号文件名原样保留，受检 locator 继续拒绝盘符逃逸和符号链接；清单保存 source schema、稳定身份、相对 locator、digest/length。原件先核受检 locator、普通文件及字节 hash，不遍历日期桶吸收资源。源文件和非空 WAL 在复制前后与全批结束复核，只将“缺 WAL”和“空 WAL”统一为无已提交页；任一步变化或失败保留 preparing 产物和原数据，不能为迁移先触发旧 retention=0 GC 或删原件。内容、清单及目录全部 durable 后，唯一 journal 才发布 prepared；verify 拒绝未完成或 hash 不一致的备份。此阶段不转换目录、不提升 schema，不提供覆盖源数据的恢复操作，不能把 prepared 当成迁移成功。
4. 一个既有 `attachment_id` 迁为一个目录资源并保留其稳定 ID；同一 Thread 同内容的多次上传已在旧库合并，不能凭空推断原先的上传次数。不同既有逻辑 ID 不根据 digest 再合并。共享 blob 的已登记原件分别复制入各目录 `original/content`，不使用可写硬链接；已有预览逐字迁入 `preview/512.webp`，处理区 `derived/` 留空。用户显示名只保存在元数据，不参与物理定位。available 的原件缺失/损坏必须拒绝；已 tombstoned 的资源只保留不可用事实，不能创建空目录冒充恢复成功。已提供 `convert --backup <dir>` 与 `verify-directories --backup <dir>`：仅在已核验备份的 `converted/` 准备上述目录，日期冻结为旧 owner 创建时间的 UTC 日期；完整旧 owner/blob 行及来源备份指纹进入确定转换清单。活动关联要求权威 Session 和精确 Thread 存在，cold 不判失效；复制中断保留 preparing，可核对并复用已完成文件后继续，不覆盖损坏成品。所有输入/成品核验并 durable 后才发布 directories_prepared。目录阶段不切换线上 locator。另提供 `prepare-catalog --backup <dir>` 与 `verify-catalog --backup <dir>`，在 `converted/catalog.sqlite` 准备 schema 4 基础 catalog：目录、原件/已有预览版本、精确关联、完整旧 owner/blob 来源绑定及终态 fork claim/mapping 的原始 snapshot_json。SQL 唯一定义在 `managed_directory_schema.sql`，旧来源绑定和 fork 原文由不可变触发器保护；journal 绑定源备份、目录清单和 DDL 指纹。写库或 rename 后中断沿同一 journal 续跑，已发布目标绝不覆盖，核验包含完整结构及来源逐行比对。此目标尚无线上 ingest/新 fork 写入，离线安装入口见第 7 条；线上 owner 仍只支持 schema 3，不能据此启动新存储或宣称任务 2.2 已完成。
   转换初始化先在完整计划 SHA256 定位的 `.converted-{hash}.preparing` 内原子写入 manifest 与 preparing journal，二者及目录 durable 后才将整个目录 rename 为 `converted/` 并同步父目录，随后才开始正文复制。初始化期间进程退出可重建同计划的未发布元数据；其它计划的临时目录、源备份和已发布正文不被清理。已存在但缺失元数据的旧转换根仍明确拒绝，不靠扫盘补造归属。此调整不改变 schema 或已完成转换的格式。

5. 新 owner 保存旧 Session URI、精确旧 owner/blob/version 和历史别名的确定绑定。生产解析旧 URI 后直接使用新目录/冻结版本，不回读旧表或旧正文位置；旧数据仅作迁移输入及人工备份。canonical item、assembly、资源索引中的原始历史 URI 和已封存 capture/snapshot/preimage 保持逐字不变。终态 fork mapping/claim 保留冻结原文及来源身份，由新 owner 校验该原文并解析迁入版本；不能改写旧 `relative_locator` 来重算 hash，也不能从 digest 猜测跨 Thread 授权。旧 v2 附件载荷由 `ForkAttachmentSnapshot.to_payload()` 显式冻结字段，claim、captured marker 和 source hash 共用同一入口；不再由 dataclass 全字段反射决定历史合同。目录快照须登记新版本，不能为旧 dataclass 添加默认字段后重新计算 v2。跨域正文先进入原 capture 的冻结文件树，由 `source-copy-snapshot:v3` marker 核对完整文件集合、digest/length，再经现有 files/chunk 传输；删除 live `attachment_chunk` 和对应 blob_id 传输字段，公开 Proto 保留其编号与名称。目标导入只读已收到的本地快照，不再二次联网读取 live owner。旧 v2 封存载荷/hash 继续原样读取，缺少冻结文件时明确失败，不回源补造；升级前用旧版本完成或撤销未终结跨域 fork，与离线迁移的非终态拒绝边界一致。新 fork 按目录 manifest 合同写入，不继续产生旧 blob 快照。
   旧引用解析现在由原附件 owner 的 `legacy_references.py` 消费新 catalog 中的不可变来源和终态 fork 载荷；离线 prepare/verify 在逐行对照后实际解析全部 owner/alias，并校验 committed claim、精确目标 owner 与原件版本。普通旧引用只认本地工作区和精确 Session/Thread，跨来源必须有该消费 Thread 的冻结 alias，拒绝歧义或按 digest 猜测。历史 snapshot 的 locator 属于来源机器，不与目标位置比较，也不拿来读目标。该切片不改变 schema、封存 JSON/hash 或线上入口，目录的当前可用性与关联是否 released 分开；线上上传/lease/fork 新版本和安装切换仍 TODO。验证证据位于 `out/tests/temp/attachment_legacy_resolution_20261024/artifacts/`：产品入口执行及两代 fork 后移离来源/备份正文，仍可从新目录解析并核字节；覆盖 8 类来源/版本/claim/URI/alias 损坏与歧义反例，以及来源 locator 与目标不同的正例。完整离线迁移 suite 75 passed，附件定向 120 passed；另 1 条预览诊断文字断言在原 HEAD 同样失败，保留原始结果，未修改断言或生产预览行为。Ruff、compileall 和 OpenSpec strict 通过；线上整体目录功能仍未完成。

6. 旧 active 引用按其精确 Session/Thread 与权威任务状态建立关联/必要占用；不能把全部冷 Thread 当失效，也不能仅因 Thread ID 存在而永久保活。旧 released 引用保留原释放 `updated_at`，某目录已零关联时用其可核验释放时刻起算 30 天，不能改按创建日或每次重启重置；原显式 `retention_until` 更晚时继续保护至该时刻，不缩短既有保护。未知释放时刻明确拒绝该记录并保全。迁移期间只登记到期事实，不夹带物理 GC；删除终态及保留中的 fork/恢复依赖独立核对。
7. 在隔离准备区完成新 catalog、目录及映射校验，通过原 owner 的持久迁移记录冻结源备份指纹、schema 和目标集合；全部 durable 后才提交唯一切换点。启动只接受已完成的 schema 4，未完成迁移明确失败并由原迁移入口继续/撤销；不从源/目标双读猜测成功。中断前后的恢复、重复执行和回退必须验证。切换成功也不自动删除 schema 3 备份；应用有新写入后的回退不得直接覆盖新数据。旧生产表/方法与全部调用方在切换批次移除，目录维护互斥及迁移记录不成为第二套业务状态。

   已提供 `install --workspace <root> --backup <dir>` 和 `rollback --workspace <root> --backup <dir>`。唯一 `.boxteam/attachment-migration.json` 冻结备份、目标 catalog、原源集合和文件/WAL 指纹；SQLite 内容用完整 schema/行核对，避免将 backup API 的页头变化当作内容变化。ready 恢复且尚未首次移出原目录时，再跑完整 preflight 核对集合，不只复验旧清单中的文件。先将旧根保全为 `.attachments-{backup_id}.schema3`，再发布已 durable 的准备目录，最后提交 installed；中间状态均阻止业务准入。撤销保全新目录副本，原目录恢复并 durable 后才发布 aborted。重复 install 不覆盖安装后的新写入；存在新写入时 rollback 明确拒绝。离线安装与当前线上 schema 4 owner 已接通；实际启动后可读取旧历史并生成新目录，Shell 全程占用与目录 GC 共用原 lease 账本。Windows 与完整深历史验收继续保留。

离线迁移验收的 schema 3 输入固定来自 `cb90835523b9ff6d91e3f8dfaf3491855d55191d`：测试用完整 Git archive 导出该版本，独立 Python 进程复用其产品 fixture 和 `model_stream`，经真实上传、接受、执行及两代 fork 生成状态。当前产品不再承担生成旧结构的职责，也不保留旧线上 owner 或手写执行/fork 状态。旧进程及 owner 关闭后才复制工作区供当前迁移入口使用；保留源 commit/tree、命令、退出码、原始输出及来源回执。旧源码与生成工作区分别隔离，测试工作区沿用正式镜像输出路径，避免嵌套归档将 fork 临时 SQLite 路径拉长；旧 fork watchdog 的产物仍留在其认可的源码输出根。缺少固定历史 Git 对象时明确失败，不回退到当前源码或伪造旧表。团队服务仅沿用该旧版本执行入口既有的初始化依赖，团队功能与专项验证继续 TODO。

2026-10-24 此输入切片在精确公开 HEAD 候选（仍 schema 3）和完整 schema 4 候选各通过 86 项离线迁移/安装检查，覆盖共享旧 blob、已有预览、排队输入、两代真实 fork、冻结来源 hash、安装/撤销及进程退出窗口；Ruff 和 compileall 通过。准确源码指纹、失败归因及最终原始输出位于 `out/tests/temp/attachment_legacy_product_20261024/artifacts/`。这一批只证明旧产品输入解耦；随后线上 owner、原调用方及唯一切换已迁齐，证据与未完成验收按 tasks 最新记录。

### 实施顺序与验证门

Windows 恢复补验要求 activation 新 ID 同时满足原生文件名单段与有界长度：Coordinator 对 snapshot kind、精确 Session/Thread、execution 和可空 model-call 的结构化 JCS 元组生成固定摘要，不用冒号拼接运行身份。原 typed detail locator 不变，物理 owner 使用既有 `native_io_path` 访问 Windows 长路径。已存 snapshot 身份仍由原唯一 catalog 返回；model-call 恢复按其既有 owner 唯一索引查出原 ID，并验证 execution parent，不能用新命名规则重新冻结历史。这不改变数据库结构或重写历史正文/hash。TODO：旧 Linux 数据中的非法 Windows 文件名在跨平台物理迁入时仍需明确方案与产品验收；本切片不隐式清洗、改写或增加路径别名。

完整附件流程进一步要求持久 detail locator 始终使用 POSIX 字符串；fork source 捕获和目标资源检查与原 manifest 一致，不得用 str(Path) 随宿主平台改变合同。冻结详情从捕获、lineage/前缀读取、artifact 传输、目标安装到发布屏障及失败/恢复清理的原生 I/O 都复用 native_io_path，逻辑 locator、来源身份和原 hash 保持不变。Linux/Windows 的真实 Gateway→上传→PTY处理→读取→公开fork→硬重启→目标冷读已通过；准确源码、原始失败/通过和未覆盖范围见 tasks 6.1，不能据此声称中继远端寻址或全部故障组合已验收。

SQLite 文件连接现统一经过 `sqlite_file_uri` 编码原生 I/O 路径，保留各 owner 原有只读、immutable、WAL 与事务语义；预算按实际交给 VFS 的 UTF-8 路径及 journal/WAL/SHM 后缀核对。Windows 默认 VFS 的 1040 字节缓冲区不等于普通 Win32 路径可用长度，不再靠缩短工作区规避。冻结 SQLite 复制仍在隔离解释器保持原读事务，父进程仅编码 URI，经结构化 stdin 传递，不打开或关闭 live 数据库文件。快照捕获、marker 校验、安装/恢复和消息流去重文件在原生 I/O 边界共用已有扩展路径/原子写；逻辑 locator、来源身份、正文和 hash 不变，不升 schema、不改系统设置。258 字符主库（sidecar 跨 MAX_PATH）、600 字符 ASCII 与含中文路径实机 WAL/重开/只读复制通过，258 字符工作区完整附件产品流也已通过；精确范围见 tasks 6.1 的 `sqlite_windows_path_budget_20261024` 证据，真正中继跨机器 URI 及完整生命周期故障组合继续 TODO。

目录创建、上传接受、原件/预览、统一文件读写、Shell 占用、运行时代次与低频 GC 已纵向切换，blobs.py/queries.py 旧生产实现物理移除；schema.py、旧 SQL 和旧快照仅用于显式迁移或封存历史读取。后续优先补齐冻结历史/完整 Web/平台验收，再处理挂起操作终止、共享锁与关闭期限；每个批次迁齐调用方并验证后提交，未满足全部合同的任务保持未勾选。

迁移本身须覆盖从产品生成的 schema 3 历史、相同内容重复上传、共享旧 blob 的多个 owner、已释放/已回收资源、终态深历史 fork、排队输入、非终态明确拒绝，以及切换前/切换点后崩溃与重复执行。核对原件字节、URI 身份、owner/claim、历史请求 hash 和备份；目录回收不删除历史 immutable asset。Agent 团队/委派专项验收继续 TODO；附件链实际必需的用户/内部消息接受和通用 fork 仍验证。

2026-10-24 真实 SSH 产品验收补齐任务 6.1：Windows 原用例经产品上传、PTY 处理、read_file、公开 fork、Backend 硬退出重启及目标冷读后，Linux Gateway 再经原配对/SSH 隧道和 canonical workspace HTTP 代理提交目标执行。附件 derived URI 仅由 Windows owner 解析，原生 Shell 读取原件并写新结果，read_file 与普通文件 API 读回相同内容；相对路径、Windows 盘符绝对路径及资源 URI 同指目标机器的普通文件，响应位置仍为 URI，Linux 同名 decoy 不变，来源 canonical 与原件保持。协调器不写执行、fork、恢复或关联事实，模型继续复用原 model_stream。准确双端源码、原始失败与通过、SSH/HTTP 和 Windows 终态收据见 tasks 6.1 的 `cross_machine_attachment_uri_20261024` 证据；Linux 为 `dee803f02`、Windows 为已验证的 `e5168fba`，不能表述为相同源码全平台通过。Windows 最近 Terminal 启动失败专项回归、UNC、旧 Linux 非法 Windows 文件名迁入及 5.2/5.4/6.2 完整故障组合继续保留，Agent 团队和协作不新增实现或专项验证。

### 目录增长与回收测量（任务 6.4）

`integration-attachment-retention-measurements` 通过产品消息和真实 Agent 执行生成 1/32 个独立同内容上传，每个原件 3968 字节，随后沿真实 unload 产生零关联期限；定点冷 owner 恢复、截止前/到期查询与 GC 均消费原记录，不手填 pin、关联、期限或执行。正式原件和测试源码指纹见 tasks 6.4 证据。该场景度量 owner 成本，模型使用现有 model_stream，不代表网络/SSH、Windows 或完整 Backend 启动耗时。

本次 Linux 样本为：1/32 个目录的 live 原件分别 3968/126976 字节，SQLite 分配页合计 143360/196608 字节；冷 owner 恢复约 7.52/9.11 ms，到期查询约 0.087/1.396 ms，GC 约 7.07/131.14 ms。GC 后全部已登记 live 目录消失，canonical bytes 不变，catalog 保留原 tombstone、文件版本、关联与 ingest 行。WAL 在发布后为 98912/2640952 字节，GC 后为 111272/3036472 字节；这反映本次未主动 checkpoint 的日志占用，不能与数据库页重复相加后当成有效正文体积，也不能由两个小样本承诺全链路线性增长。

TODO（性能）：32 个目录的 124 KiB 原件对应约 2.90 MiB GC 后 WAL，终态记录仍保留；后续按同工作负载扩大目录数/正文规模并测 checkpoint 后文件占用、恢复和尾延迟，再评估 WAL checkpoint 与无引用终态元数据整理。清理前仍须证明历史/fork/恢复依赖安全，不自动缩短保留期限、不删除仍有依赖的 tombstone/版本，也不为小样本提前引入后台任务或新索引。挂起 operation 接管、共享 owner 锁和关闭期限继续是 5.2/5.4 未完成的正确性边界，本测量不能替代它们。
