## 1. 实施状态与回收扫描

当前已接入线上唯一 schema 4 目录 owner，迁齐上传/接受、原件/预览、文件与 Shell 准入、运行时代次及 30 天 GC；旧 blob writer 与即时 GC 已移除。下文逐批证据中的“尚未切换”是当时状态，以本段和最新任务勾选为准。已验收目录回收后冻结请求的冷重建，以及主窗口附件目录查看、派生文件编辑和刷新重开。同工作区/跨工作区 fork 的失败与删除结算已验收；Linux/Windows 本地产品流、真实 SSH 中继的目标端解析及6.2列出的生命周期故障组合已验收，旧代操作接管、挂起操作终止核实及完整关闭期限仍 TODO。Agent 团队、成员、委派和会话协作通信按用户裁定暂缓实现、专项验证与优化。

### 2026-10-26 停止派发/回执窗口硬退出与6.2收口

- 原停止链仅增加两处phase观察，使用原operation lease标识。正式消息和模型exec_command生成附件目录、PTY及父子进程；目录精确到期后，在停止派发前或真实回执返回后硬退出Backend。公开Gateway请求明确502；新Backend保留原待核实占用或核实真实终态，原显式管理入口停止后才回收目录，无自动重复派发。Linux/Windows各2项通过（199.33/249.77秒）；原live场景Linux回归1 passed/93.11秒。原lease身份、期限、locator及canonical字节保持。
- 按当前源码补齐6.2逐项审计：期限/29天重关联1项、真实运行时代次与旧代迟到释放/attach-GC 8项、main/child fork-GC及发布/失败/删除11项、物理删除前后独立进程硬退出2项，Linux/Windows分别全部通过。Windows首次组合运行因期限用例节点名使装具配置路径超过MAX_PATH而exit1，其余三组各8/11/2项通过；只缩短该函数名后期限用例独立1 passed/128.76秒。AST证明函数正文、fixture及断言完全一致，原失败保留，不称首次组合全绿。Linux对应1/8/11/2项为104.08/60.16/107.53/36.58秒；Windows后三组为83.41/161.76/46.97秒。
- 共享资源owner、恢复、锁、释放、命令、详情与资源API回归347 passed；Ruff/compileall通过。Pyright新测试无诊断，owner的4项原诊断经原HEAD指纹和逐项规则/位置映射核实，无新增，不称类型全绿。作者AST/目录职责自查无新增职责或目录；原owner仅988→992行。准确命令、源码指纹、原始输出/真实退出码、Windows快照与收据、类型对照和覆盖审计位于`out/tests/temp/attachment_stop_restart_20261026/artifacts/`。
- 6.2完成后为21/23；5.2与5.4继续未勾，不据本片声称不合作执行可强制终止、未知native启动已接管或整个operation/shutdown有硬期限。团队/协作和CIL第9节继续暂缓，原四份CIL规划排除实现提交。


2026-10-24 线上唯一切换验收：精确功能 tree `edbd840d49dda3cc77a1bd2b60e7bc258be5c318` 从 HEAD 完整导出，不带共享工作树的目录重构。20 项 residency/worker、10 项真实 fork、13 项目录入口、7 项真实 Linux PTY、8 项运行时/30 天 GC、26 项 ingest 恢复，以及 1 项旧产品迁移后启动与新上传均通过；catalog/维护/初始化共 102 项在组合运行中通过。组合运行中的另 2 项跨端入口因未经过 matrix watchdog 而在 fixture 准入失败，随后按正式 `integration-attachment-directory-entry` 重跑 13 项全部通过，未放宽断言。Ruff、compileall 与 Web 驱动语法检查通过。准确命令、完整源码指纹、原始输出和退出码在 `out/tests/temp/attachment_owner_commit_20261024/artifacts/`；此前三 Gateway 冻结传输/重启 3 项通过的限定源码和证据保留于 `out/tests/temp/attachment_owner_integrated_20261024/artifacts/runs/frozen-transfer-005/`。

Web 定向 `BOXTEAM_E2E_SECTION8_FOCUS=copy_missing_attachment` 通过真实 Gateway、三端页面初始化与 model_stream，1 passed/360.80 秒：必需附件缺字节拒绝且无目标发布，可选附件由真实压缩变为非依赖后 fork 为不可用，卡片与重开状态一致，副本继续执行且 Provider 不复活旧引用。原件从只读测试副本删除，仅作故障注入。首次隔离候选缺少 Web node_modules 链接而在 Vite 构建前失败，补齐已有依赖链接后上述专项与构建通过；两次原始结果均保留。未运行完整 default（含用户暂缓的协作场景）与 copy_attachments 的全部崩溃/删除竞态，不能将本项通过表述为完整 Web 验收。

本批移除旧 blob writer/query 与 Session 新引用生成，Web 专项消费者改用目录关联、冻结 manifest、原件只读和零关联 30 天期限；普通跨端 alias 读取补齐目标 Gateway 校验，可选附件缺原件沿原目标检查登记不可用，必需附件仍拒绝发布。完整 Web、Windows、冻结请求 GC 后重建及清理关闭期限尚未完成，不据局部通过关闭 3.4、4.1、4.2、5.2、5.4、6.1、6.2、6.4。

- [x] 1.1 登记目录级身份/关联、Shell URI、30 天回收、历史/fork、失效 owner 对账，并审查相关 change 的冲突与替代顺序。
- [x] 1.2 Thread residency 批量扫描隔离单项失败，保留原错误及 owner 身份，继续其它 Thread 并允许下一轮重试。
- [x] 1.3 唯一闲置 worker 显式记录扫描错误后继续原周期重试；验证取消和关闭仍收敛。
- [x] 1.4 完成定向回归、相关静态检查及 OpenSpec 严格验证，保留准确命令、源码指纹、原始输出和退出码。
- [x] 1.5 迁齐共享 operation、Terminal/Browser operation、Browser 管理入口及测试中的资源记录查询改名调用，移除旧名悬空引用且不增加兼容入口。
- [x] 1.6 单 Thread 闲置卸载扫描最多等待 30 秒，超时保留唯一在途任务并继续其它 Thread；覆盖 listener/scope 挂起、迟到成功/失败、重复扫描及新 admission 不绕过未结束的原任务，完成静态与规范验证。

## 2. 目录存储与接受链路

- [x] 2.1 全仓盘点上传、媒体读取、消息接受、历史、文件工具、Shell、fork、删除和测试的直接/间接调用，确定支持的旧库范围及原件保全/切换方案。（2026-10-24：调用链实施表、schema 3 → 4 显式离线迁移与原件/封存历史保护见 design；275 文件/3110 处检索原件及指纹在 `out/tests/temp/attachment_directory_inventory_20261024/artifacts/`。仅完成盘点/方案，2.2 后的产品实现和验收仍未完成。）
- [x] 2.2 在原附件 owner 实现每次独立上传的目录资源、稳定身份/locator、原件/预览/派生区和目录级关联，结构变更时提升 schema 并提供明确支持范围的迁移或拒绝行为。（2026-10-24：已准备备份内独立目录与 schema 4 基础 catalog，保全旧来源/终态 fork 原文；已补新 catalog 的旧 owner/fork alias 精确解析，并接入迁移发布核验（完整迁移 75 passed、附件定向 120 passed，1 项既存预览断言失败另有原 HEAD 复现；证据 `out/tests/temp/attachment_legacy_resolution_20261024/artifacts/`）；线上上传、读取、fork/ingest 与唯一切换仍 TODO，本项保持未完成。）（2026-10-24：迁移输入已改由固定 schema 3 产品完整归档/独立进程生成，复用 model_stream，经真实执行和两代 fork 保全来源；公开 schema 3 基线与 schema 4 候选各 86 passed，Ruff/compileall 通过，证据 `out/tests/temp/attachment_legacy_product_20261024/artifacts/`。线上唯一切换仍 TODO，本项未完成。）
  - 2026-10-24 先完成在线访问与离线维护的跨进程互斥：Backend 全 mount 在业务初始化前持共享锁至关闭排空，独立构造、上传 pin/ingest、catalog/历史投影遵守同一锁；目录根同级锁文件永不替换，关闭失败保留连接与占用并允许重试。新锁、目录访问、崩溃自动释放及失败路径随相关回归为119 passed；唯一失败的预览诊断旧断言在改动前 HEAD 完整 archive 也复现（`Errno 13` 与现有结构化 `errno=13` 不一致），未改断言。真实双 mount 启停、启动冲突/失败释放与上传拒绝集成共6个场景通过；维护时上传未创建 pin/ingest，原控制库逻辑内容不变。准确命令、tree/源码指纹、原始退出码与基线失败原件位于 `out/tests/temp/attachment_maintenance_20261024/artifacts/`。目录格式、schema 3 → 4 离线迁移、持久任务核对、一致备份和切换仍 TODO，本项未完成且未提升 schema；本轮未执行模型请求、团队/委派专项或 Windows 实机验收。
  - 同日接续完成 `migration prepare/verify` 离线备份阶段：独占锁内只读核对 schema 3、Session/Thread 操作、ingest/commit/fork claim、执行队列与外部占用；queued 保留，未知或活动任务拒绝。SQLite backup 纳入已提交 WAL，原件与历史按登记 locator 保全，复制前后及全批结束核源指纹；清单和所有内容 durable 后才发布 prepared，失败保留 preparing 且不覆盖。产品创建、上传和接受生成源数据，完整命令、队列/canonical hash/原件核验、WAL、九类损坏/活动输入、复制中新提交与中断、清单篡改和 locator 边界共24 passed/73.45秒；相关附件 owner 回归119 passed/1 deselected，跳过项为上条已复现的既存诊断断言；Ruff及compileall均exit0。证据位于 `out/tests/temp/attachment_backup_20261024/artifacts/`；首轮空WAL误判、第二轮核验生成sidecar、第三轮故障输入违反claim表约束的原始失败保留，后续分别修复并通过。目录格式/schema 4、恢复/切换、终态真实 fork 历史与 Windows 迁移验收仍 TODO，不能把 prepared 当作任务 2.2 完成；未执行团队/委派专项或真实模型请求。
  - 同日补齐离线外部 owner 准入：Terminal/Browser 先持久创建记录、后登记通用账本的真实产品窗口原先会误放行；新增回归在完整原 HEAD 候选以 DID NOT RAISE 复现。现复用领域记录原格式及终态闭集，终态与 verified 同时满足才允许备份，dispatching fork attach 明确拒绝；已核实记录与 Thread 关联逐字保存，不调用 recover 或改变来源。38 项备份集成通过/88.58秒（含两类产品创建登记失败、原24项及离线状态/回执/原文保全），Ruff、compileall与严格规范验证通过；原始命令、源码指纹、红测与收据在 `out/tests/temp/attachment_backup_owners_20261024/artifacts/`。离线已存终态记录使用局部数据夹具，不代替真实 PTY/Browser 停止协议验收；schema 4、目录转换、恢复/切换仍 TODO，2.2 保持未勾选。
  - 同日完成备份内目录物化：`convert/verify-directories --backup` 将每个旧 attachment_id 转为独立目录，原件与已有预览逐字复制、派生区留空；同 blob 多 owner 不使用硬链接。保留完整旧 owner/blob、UTC 创建日期、释放时刻与更晚的显式 retention；活动精确 owner 缺失、时间不明和正文版本冲突明确拒绝，tombstoned 不创建空目录。复制中断可核验并复用已完成文件后续跑，损坏成品不覆盖，校验与 durability 完成后才发布 directories_prepared。产品创建/上传/接受与真实预览生成的16项转换集成通过/34.27秒，原备份38项回归通过/88.32秒，Ruff与compileall通过；命令、完整候选源码指纹、raw和退出码在 `out/tests/temp/attachment_directories_convert_20261024/artifacts/`。释放/损坏状态为局部迁移夹具，不充当真实卸载或GC验收。旧库、备份data与canonical原文不变；schema 4、冻结alias/claim解析、初始化阶段任意崩溃恢复、线上切换和Windows实机仍TODO，2.2不勾选；本轮未执行团队/委派专项或真实模型请求。
  - 同日将目录 locator 生成/校验/路径预算归回原 `locator.py`，迁移不再自行拼日期/身份。目录使用冻结创建时刻的 UTC 日期，旧非 UUIDv7 附件 ID原样保留；校验真实日历日期、严格ASCII形态和完整路径预算，不修剪或重定向。新旧 locator 定向53项通过/3.19秒，转换集成17项通过/35.40秒，Ruff、compileall与严格规范校验通过；准确命令、候选源码、raw和失败归因在 `out/tests/temp/attachment_directory_locator_20261024/artifacts/`。首次单测失败是末尾对超长路径调用exists触发OS错误，路径预算拒绝已正确生效；改为比较父目录内容证明无写入，未修改生产行为或放宽拒绝。此批仍只供离线转换，不声称线上目录上传/切换或Windows实机完成。
  - 同日补齐目录转换初始化的进程退出恢复：manifest/journal 在完整计划 hash 定位的临时目录内写齐并 durable 后才原子发布转换根，正文复制仍沿原链路；重试只清理本计划的未发布元数据。完整 HEAD 加新增退出验收的候选红测为6 failed/1 passed，证明旧实现会暴露半初始化根或没有原子发布点。生产候选 `2d131f4eb37a5be0e0204c2229f5779b3f1fd3e9` 的备份/目录/catalog正式矩阵75 passed/232.17秒；仅加强测试的最终候选 `4a73d8b173af3ae445d9fd3496f6e23f7937f35a` 又通过全部7个真实子进程退出窗口/71.41秒，保全无关临时目录、原件和完整备份。Ruff及compileall通过；首轮隔离目录准入失败、首次Ruff失败及修复原件均保留。准确命令、tree/源码指纹、raw/退出码与验证复用依据在 `out/tests/temp/attachment_conversion_init_20261024/artifacts/`。不改变schema或已完成转换格式，已有缺失身份元数据的旧转换根仍明确拒绝；线上目录、唯一切换点和Windows实机仍TODO，2.2保持未完成，未追加团队/委派专项。
  - 2026-10-24 在线候选验收前置：测试工作区重建遇到只读原件目录时会在下个用例准备阶段失败，完整 HEAD 的 red-002 为2 failed/8 passed，两个权限反例均复现。统一清理入口现在只调整待删除测试副本的 owner 权限，不跟随外部 symlink，真实 I/O 错误仍原样抛出；green-002 的13项定向、Ruff及compileall通过。命令、完整 tree/源码指纹、raw与退出码在 `out/tests/temp/readonly_workspace_reset_20261024/artifacts/`。线上目录草案的上传/消息接受/冷读及两种图片尺寸预览共3项已在隔离候选通过，草案仍未切换或提交，不能据此关闭2.2/2.3；原件限制未放宽，团队与协作专项继续TODO。
  - 2026-10-24 完成离线目录根安装/撤销：原 owner 生成 schema 3 上传、接受和 queued 历史，install 先核完整源集合与 SQLite 逻辑内容，冻结原件/数据库/WAL 指纹；ready 重试在首次移出旧根前重跑完整 preflight。唯一迁移记录阻止未完成状态启动，旧根、备份和撤销副本保全，重复安装不覆盖新写入，已有新写入则拒绝撤销。安装两次 rename 与撤销两次 rename 均通过真实进程退出恢复，11 项集成通过/70.77秒；旧迁移75项先74通过，另1项被新增不可变触发器拦在损坏输入准备阶段，测试先断言拒绝再在独立副本移除触发器后注入损坏，正式matrix定向补跑通过/32.93秒，保留全部原断言。维护锁/locator及五种迁移状态缺库拒绝共38项通过；安装后的两个冷schema 4 owner均可读取旧URI、新原件URI及派生文件。Ruff与compileall通过，原始命令、源码指纹、退出码和失败归因在 `out/tests/temp/attachment_install_20261024/artifacts/`，冷读原件在 `out/tests/temp/attachment_online_entry_20261024/artifacts/runs/installed-cold-owner-002/`。此片仅提交离线安装和准入，线上schema 4注入、Shell全进程占用、GC与Windows/完整深历史安装恢复仍TODO；2.2不勾选，Agent团队/协作专项继续TODO。首次冷读漏装显式catalog owner与首次定向补跑缺正式matrix watchdog上下文的失败原件保留，均未作为业务验收。
- [x] 2.3 迁齐 ingest/pin、用户/内部消息接受及媒体预览，发布目录与原件 URI；排队输入在 runtime 首次建立前也有可核验保活，不新增附件专用工具。
  - 2026-10-24 修复 canonical 用户输入过滤时遗漏 `directory_uri`：新接受的附件保留目录坐标，旧历史缺字段不补造，原 hash 不重写。精确 HEAD 隔离副本的 red-002 为 1 failed/1 passed，真实复现目录身份经序列化及 Job 投影丢失；修复后 canonical/消息回归 653 passed，Ruff、compileall 通过。证据 `out/tests/temp/attachment_canonical_directory_20261024/artifacts/`。完整 schema 4 候选的真实执行/卸载/队列/GC 8 项及 HTTP/model_stream 硬退出三次重启 1 项通过，但线上目录 owner 尚未切换，2.2、2.3 与 5.1 仍未完成，Agent 团队/协作专项继续 TODO。

## 3. 统一 URI、文件 owner 与 Shell

- [x] 3.1 在唯一 URI owner 扩展 attachments kind、workspace 目录模板和 child 编解码/导航能力，迁齐 DTO、Proto/生成类型和普通文件消费者；新生产引用不再生成旧 Session 单文件模板。（2026-10-24：目录 grammar、导航 shape、原文件 root/child 单点与全部调用迁移、Web 文件树/Markdown 根拆分已完成；272 项 grammar/resolver、150 项文件入口/operation/download、13 项 Web、1 项真实 Gateway grammar 和1项真实浏览器编辑/双 owner 重启重开通过，tsc/build/Ruff/compileall/strict 通过。线上上传引用、DTO 与物理目录 owner 尚未切换，本项保持未完成。证据 `out/tests/temp/attachment_uri_contract_20261024/artifacts/`。）
  - 2026-10-24 接续迁齐 URI 消费合同：目录与旧 Session 附件身份统一归唯一 grammar，内部消息、fork 依赖、原 assembly 附件 capability 和使用查询不再各自解析旧模板；投影同步文件、目录和 derived 输出坐标，保留 canonical、用户正文与冻结预览，并拒绝来源/目标目录错配。该片未切换线上附件 schema 或生成新目录上传引用，任务 3.1 仍未完成。最终定向 874 项、旧附件 79 项、恢复 27 项均通过，另保留 1 项已在原 HEAD 复现的旧预览诊断断言未执行；Ruff 与 compileall 通过。此前组合运行/恢复整文件分别超过 120/90 秒保护期限；分组后恢复前十项 54.65 秒、其余十七项 59.52 秒全部通过，未修改或放宽断言。准确候选、命令、原始输出、退出码和超时原件见 `out/tests/temp/attachment_consumers_20261024/artifacts/`。Agent 团队与协作实现及专项验证继续 TODO。

  - 2026-10-24 目录引用协议已迁齐：AttachmentRef、TurnAttachmentDTO 与两份 Proto 新增可选 directory_uri，Python/TypeScript/Buf 绑定由原脚本生成；用户消息仅有 generated block 时也保留目录引用，Web 打开附件继续传递完整引用。保持旧历史没有目录字段的语义，不修改历史记录或切换线上存储。完整 HEAD 隔离候选的后端协议/映射/消息构造 23 项与 Web 媒体/附件 5 项通过，Ruff、真实 tsc/build、Proto format/lint/build 和相对真实 HEAD 的 breaking 检查通过。证据：`out/tests/temp/attachment_reference_protocol_20261024/artifacts/`，Proto 检查原件另见 `out/tests/temp/attachment_online_entry_20261024/artifacts/runs/protocol-contract-001/`。线上上传、文件 owner 和唯一迁移切换未完成，3.1 与 3.2 均保持未勾选。
- [x] 3.2 将附件目录导航/读取/派生区写入接入原文件 owner，保留原件限制、媒体读取、修订校验、私有存储和 containment 边界。
  - 2026-10-24 文件入口扩展已完成独立候选验证：唯一路径 owner 接受显式附件目录解析端口，浏览、文本 CAS、模型 create/edit/patch、集合 create/upload/copy/paste 与下载保留 URI 根并复用原执行链；实际写入前重核 derived 权限，原生/private files 输入不因附件开放而放行。MIME 与原件完整性沿原附件 reader，工作区权限和旧 Session 精确 capability 分开。完整目录候选通过13项上传/预览/fork/文件链集成；精确 HEAD 加本片的隔离候选通过262项文件/导航/文本传输/API/Proto回归，Ruff及compileall通过。证据分别位于 `out/tests/temp/attachment_online_entry_20261024/artifacts/`（最终 `file-owner-entry-003`）与 `out/tests/temp/attachment_file_owner_contract_20261024/artifacts/`。本片只提交公共文件链及注入合同，线上container尚未注入schema 4 owner，离线安装、操作lease/GC互斥与实际切换仍TODO，3.2保持未勾选；合并时保留共享工作树的逐步索引调整并迁齐遗漏索引，最终另有42项operation/recovery回归通过。Agent团队/协作专项继续TODO。首轮循环变量笔误和第二候选错误测试路径的失败原件保留，修正后通过，不以这些失败改动产品合同。
- [x] 3.3 Shell 结构化 workdir 在目标机器解析目录 URI并持有目录操作 lease；后台处理进程继续占用直到真实终态，保留原生相对/绝对路径与命令语义。
  - 2026-10-24 公共 Terminal 链已持久记录目录根 URI，在原 lease 账本绑定终端身份并仅在核实终态/冷恢复时结清；当前 schema 3 装配显式 None，schema 4 注入仍为代码 TODO。完整 schema 4 候选的三种 cwd 真实后台进程保活、准入/GC 竞争 4 passed，附件目录入口 13 passed，Terminal/Browser/API 回归 84 passed。仅证明候选与公共边界，不能把这些通过当作主线上目录切换或完整任务完成；证据 `out/tests/temp/attachment_shell_directory_20261024/artifacts/`。
  - 2026-10-24 已先行接入现有 files 目录 URI，删除工具层重复 cwd 解析/投影和额外 workspace_root 参数，全部创建入口由 Terminal owner 复用固定文件 owner。只将精确 `boxteam://` 前缀视作 URI，保留原生相对/绝对/工作区外、Shell 私有目录和 POSIX 含 `://` 目录名。完整 archive 红测为6单测失败，真实HTTP/PTY在第三种URI输入失败，证明旧实现误投本机相对目录。最终代码候选 `5f143640aa76378603c58d375ca27dfaa73e36d6` 的 unit-006 为63 passed/2.56秒，http-006 为1 passed，Ruff和compileall均exit0；覆盖中文/空格目录三种输入同文件、派生写入/原件不变、命令内URI字面量、两种冒号原生cwd、跨目标/非法URI拒绝且owner/lease/Node记录逐字不变、真实symlink环原路径仅在diagnostic/cause、verified删除与lease结清。http-002还覆盖工作区外绝对cwd及stdin完成（4 passed/45.26秒）；后续修改未改stdin链。独立只读审查提出的任意scheme误分类已修复并复核闭合。准确命令、源码指纹、raw/退出码和机械收据位于 `out/tests/temp/terminal_workdir_uri_20261024/artifacts/`。附件目录URI、后台全进程占用、Windows实机和跨机产品入口仍TODO，本项保持未勾选；002的错误引号断言及Ruff格式失败原件保留，003后已修正。
- [x] 3.4 模型附件 manifest 提供目录/原件 URI 及处理输出提示，迁齐 Web 附件查看和工作区资源管理；前端变更完成真实类型检查与 Web build。
  - 2026-10-24 主窗口右侧侧边栏的附件预览提供显式“添加附件目录快捷路径”，复用原会话快捷路径、目录查询、文件编辑和后端设置重读，不写入工作区默认值，也不因浏览建立永久关联。完整候选 `b39d1b04483480bec93a95e769c59d5aa376b1c2` 通过真实 Gateway 上传、模型派生写入、实际页面添加目录/编辑/保存/刷新重开，原件字节未变，新会话不继承此快捷路径；实际模型请求含目录、原件、输出 URI 和处理提示，无本机路径。验收截图发现 Gateway UI 设置校验仍拒绝 attachments URI，已迁齐原校验，真实 PUT 200 且设置错误列表为空。77 项 Gateway 设置回归、15 项文件树/附件卡回归、真实 tsc、Web build、Ruff 和 JS 语法检查通过；最终页面 1 passed/72.45 秒。准确命令、完整候选指纹、raw/退出码及截图位于 `out/tests/temp/attachment_web_directories_20261024/artifacts/` 与相应候选正式测试输出。首轮装具未注入执行进程回放、Bun/Playwright 请求上下文 URL 解析、缺少编辑激活点击及类型字段误用的失败原件均保留；004 虽通过原场景，但截图发现的 422 经005新增显式断言后修复，不能将004单独作为设置持久化证据。团队与协作专项未实施或验证，fork 竞态、Windows 和挂起 operation 继续按后续任务推进。

## 4. 历史与 fork

- [x] 4.1 使用既有 immutable asset/version 冻结模型实际使用的原件、预览与派生输入；目录变化/回收后仍可重建原请求 hash，普通查看明确展示不可用。
  - 2026-10-24 完整候选 `95192109d84d27d9bc1e513fcce4b6e7c91c88c5` 经真实 Gateway 上传 800×400 PNG、生成 512×256 预览、模型写入/读取派生文本及读取图片原件，冻结 5 次实际请求；经普通文件 CAS 修改派生文本、重启解绑、原 owner 外置时钟到期 GC 和再次重启，全部冷投影与真实 transport、封存 preimage/hash 一致，canonical 原文不变。原件入口返回 404、历史显示 unavailable，文件变更正文和 reviewed 仍可冷读。真实流程首次暴露会话变更校验漏接 attachments child，已在原校验入口迁齐，无第二链路；36 项变更记录/Proto/OpenAPI 回归通过。最终产品验收 1 passed/91.04 秒；准确命令、候选完整归档指纹、raw/退出码和审计原件在 `out/tests/temp/attachment_frozen_history_20261024/artifacts/`。002 的本地 authority 规范化断言及003的 import 排版失败保留原件，修正后按原合同核对目标身份；最终提交仅追加证据及 import 排版，不重复相同业务长测。Windows、完整 Web 附件目录管理、fork 失败/删除竞态与挂起 operation 仍属后续未完成项。
- [x] 4.2 迁齐统一目标检查与 fork journal/claim：同工作区共享目录关联、跨工作区冻结版本复制，保留原历史身份和 URI，失败/删除释放本次保留且不发布半个目标。
  - 2026-10-24 线上目录完整候选现通过10项真实消息接受/预览/恢复与同工作区及跨工作区 Session/child fork 场景。跨端 capture 后移走 live 来源，目标仍完成冻结复制、release、冷 owner 读取及不联网幂等重放；canonical 全字段和历史来源 URI 保持不变，无精确 alias 的 Thread 跨端读取被拒绝。候选修复了 capture 原件目录误设只读导致 release 的 PermissionError，以及来源 URI 未经已提交 alias 解析的缺口；该目录 owner 尚未纳入线上提交，迁移安装、文件/Shell 操作、失败/删除与完整恢复验收仍 TODO，4.2 不勾选。固定源码、错误原件、进程保护回执和最终10 passed位于 `out/tests/temp/attachment_online_entry_20261024/artifacts/`（最终 `history-protocol-002`，跨端专项 `remote-fork-007`）；fixture 准入、测试来源 Gateway 和读取入口误用的失败另保留原件，不作产品失败或通过证据。
  - 2026-10-24 完成目录迁移前的冻结传输边界：复制所需附件先进入原 capture 文件树，v3 marker 核对完整集合及 digest/length，网络统一复用 files/chunk；删除 live attachment_chunk、blob_id 请求字段及其调用方，Proto 保留编号和名称。目标导入只复制已核验的本地冻结文件，取消须等真实 writer 退出后释放 holder；v2 封存载荷/hash 保持原文读取，不回源补造文件。代码候选 `cdbe3b89d34d258e233dddbb2fcfcaa8c1285972` 的33项 fork 单测、16项协议测试及真实三 Gateway HTTP/SSH/PTY 的来源离线续写场景通过；该产品测试在 marker 发布后移走来源原件，目标仍正确复制、冷读和新输入执行。仅测试入口调整的候选 `69ca0959e2a4ccfe9bdb689aa4a12dfddc4263b1` 又通过2项产品生成附件历史的 Session/child 重启恢复。Ruff、compileall、真实 Web 类型检查/build、协议 lint/build/生成一致性及严格规范验证通过；相对真实提交 e6e6caed2 的 breaking 报告仅包含本次明确下线的 blob_id 删除。首轮 staging 父目录遗漏已修复；候选依赖链接缺失及脱离 matrix 的 fixture 准入失败原件保留。准确命令、完整候选指纹、raw/退出码在 `out/tests/temp/attachment_frozen_transfer_20261024/artifacts/`。目录 manifest、新目录 fork/恢复、线上切换与 Windows 验收仍 TODO，本项未勾选；未追加团队/委派专项实现或验证。

  - 2026-10-24 真实执行来源补验同工作区 fork 生命周期：完整候选 `b29c0ad1133e883e43da014006989d68371ebbde` 复用 model_stream，由产品消息/执行入口生成附件历史，验证新 Session/child 两类目标在来源删除后继续保留同一目录、历史身份与 canonical 全字段不变；最后目标删除后从真实零关联时刻重新起算 30 天，到期前不删、到期后原 GC 回收。两类目标在 owner reservation 后真实失败均沿原创建/claim 中止，未发布目标且不改变来源关联，失败 claim 不再无限保活。首轮 URI authority 与卸载关联的测试假设错误已按既有合同修正；随后 2 failed/2 passed 的原件定位到结算直接读取系统时间、绕过 owner 时钟。已将结算时间改为必填参数并迁齐全部 7 个直接调用，不新增回退。最终正式 matrix 4 passed/63.98 秒，31 项 fork/retention/GC 回归、Ruff 与 compileall 通过；命令、完整候选指纹、raw 和退出码位于 `out/tests/temp/attachment_fork_lifecycle_20261024/artifacts/`。跨工作区失败/删除和挂起操作仍未完整验收，4.2 与 6.2 保持未勾选，团队/协作专项未实施或验证。

  - 2026-10-24 目录 fork 失败/删除边界闭合：候选 `df2a2278d3132b60c2e1d06caa7c2deab9bf4cb6` 的正式生命周期 matrix 6 passed/68.50 秒，其中新 Session/child 的跨工作区复制在真实 import 完成、目标发布前失败，两端 capture/claim 沿原 owner 中止、来源 export 回执 confirmed，目标无节点/半个 Thread，导入目录零关联后保留 30 天再由原 GC 回收，原键重试无新网络副作用。来源由 model_stream 配合产品执行生成，跨端网络仅由既有 SourceForkOwnerTransport 序列化传输，未声称这 2 项是实际 TCP/SSH 验收。候选 `96737f99c3efa9beb30cce2794b58f96f8b9a0ea` 复用现有唯一 API 竞争用例，将手工执行历史改为产品执行来源；新 Session/child × fresh/replay × commit/delete 先行的 8 项正式 matrix 全部通过/86.80 秒。原 200/409/404、request_id、目标不复活、claim target_deleted 与来源字节/hash 断言均保留；按目录 manifest 的精确 Thread/目录身份和原件 digest/length 迁齐旧 blob 字段，并按既有工作区目录合同固定 reuse_reference，来源读取使用来源 URI 的 Gateway 身份。两轮旧字段/旧 action/错误 Gateway 测试假设的失败原件保留，不作为产品失败或通过；原真实 Gateway 冻结传输/重启收据继续有效。Ruff 与 compileall 通过，源码指纹、准确命令、raw 和退出码均在 `out/tests/temp/attachment_fork_lifecycle_20261024/artifacts/`。结合此前正常目录 fork、冻结传输及本节本地来源删除证据关闭 4.2，不外推 5.2/5.4/6.1/6.2/6.4，团队和协作专项继续 TODO。

## 5. owner 对账与延迟回收

- [x] 5.1 复用运行时代次和原任务/operation 账本核对失效关联，接入 unload/Thread/Session 删除的幂等 detach；保护冷 Thread、排队输入和新代关联，禁止仅靠 Thread ID 存在无限保活。
- [ ] 5.2 在原资源 owner 完成旧代操作接管和挂起操作取消/终止核实；未知结果保持可发现管理状态，不以超时假定进程已停止，不新增 lease 账本。
  - 2026-10-31 lease 冷恢复状态完整性：原 `ResourceLease.status` 收为四种既有状态，唯一加载入口整批校验后再核身份与引用；非法状态明确失败，不能从占用查询中静默消失，原文件保持不变。根校验错误保留行号、字段和规则，文本不回显损坏输入，原异常数据仍可诊断。旧实现六个状态反例全部失败；最终账本、错误出口、资源恢复/释放、MCP 与 Node Debug 回归204 passed/43.49秒，Ruff、两份改动源码的Pyright零诊断、compileall及本change strict通过。首次类型约束已拒绝状态但错误仍回显输入的六项失败原件保留，后按根校验配置修复。准确命令、源码指纹及原始输出见 `out/tests/temp/resource_lease_status_integrity_20261031/artifacts/verification.json`；不改格式或写链，不代完整进程重启、旧代身份接管和关闭期限，5.2/5.4仍未勾选；未修改或运行另一会话Web迁移和自动化，未重启共享debug。
  - 2026-10-31 lease 冷恢复身份完整性：唯一加载入口在发布内存前完整核对资源/lease ID 唯一性及每条 lease 的资源引用；重复行即使内容相同也明确失败，悬空引用覆盖 active、released、settled、reconcile_required，原文件逐字保留。原账本登记/占用方法生成有效来源后注入冲突，旧实现8个反例全部失败；最终账本、原错误出口、两类资源恢复、MCP 调用及 Node Debug 占用回归198 passed/43.66秒，Ruff、compileall及本change strict通过。Pyright生产与新增测试零诊断，原测试24条诊断前后逐条一致；作者AST自查仅修改原加载方法，不称独立审查。准确命令、源码指纹、原始输出及退出码见 `out/tests/temp/resource_ledger_restore_integrity_20261031/artifacts/verification.json`。未改变持久格式或写链；原生身份缺失的接管、同步lease写盘及完整关闭期限仍TODO，5.2/5.4保持未勾；未修改或运行另一会话Web迁移与自动化，未重启共享debug。
  - 2026-10-29 无Worker终端的原管理重试：完整持久原生身份允许非运行unknown记录进入原停止链，仍先核PID、进程组、系统会话与启动标记；拒绝时不发信号，保留unknown与原身份。原生产真实HTTP反例4 failed/2 passed，同Manager与冷Manager的kill返回false、delete拒绝；最终Node定向116 passed、真实HTTP/PTY退出与管理重试6 passed、原启动收尾6 passed。真实父子忽略TERM/HUP，首次停止EPERM及Worker错误只注入原边界，核原记录、独立停止和重复请求无新执行。静态与作者结构核对、准确命令、源码指纹、raw/退出码见`out/tests/temp/uri-global-closure/artifacts/verification-115.json`；不代Python lease、Gateway/Web、Windows或无PID接管。5.2/5.4仍未勾，Debug与他人前端改动保持。
  - 2026-10-28 POSIX停止快照切片：原停止入口冻结首次session/启动身份，SIGTERM与SIGKILL宽限后重查原session；根已回收不丢失后代，强杀后仍有存活成员返回still_running，复用原未知状态和目录占用处理。真实Linux信号中fork的反例在旧生产下返回terminated且遗留活进程；修复后5项真实进程检查通过/17.36秒。完整旧生产归档加确定性OS边界反例为4 failed/4 passed，最终Terminal Session/Manager/worker及Windows边界替身96 passed/4.86秒；真实PTY/HTTP回执、精确lease与目录GC矩阵7 passed/85.29秒。Ruff、Python语法及Node语法通过；准确命令、源码指纹、raw和退出码在`out/tests/temp/terminal-session-stop-proof/artifacts/`。首轮OS替身误拦模块加载的4项失败保留原件，修正为仅替换/proc读取后验证；不改变断言。仅Linux原生验收，未重跑Windows/Darwin实机、Gateway/Web或完整shutdown；native无外部身份、其它挂起操作接管及5.4仍TODO，本项保持未完成，团队/协作专项未实施。
  - 2026-10-24 Browser 关闭发布切片：context 已关闭后，检查点删除或最终终态写盘失败仍保持 unknown；deleted 重试不能凭内存终态跳过原清理和写盘，关闭与 unknown 写盘都失败保留两份异常及 cause。完整原 HEAD 加反例为 Bun 5 failed、真实 HTTP/Chromium 2 failed；最终 145 项 Session/Manager/锁回归及 9 项真实 HTTP/Chromium 故障/关闭场景通过（44.28秒），Ruff、compileall 与 JS 语法通过。新增真实场景经产品创建与 stop/delete，在原生检查点文件边界制造目录冲突，核对原页面只关闭一次、Node 与 Python unknown、原精确 lease 保留、故障修复后的外部 owner 显式重试真实落盘，再由 Python 核终态结清；不手填业务状态或通过替代 context 修复。准确命令、完整候选/源码指纹、原始反例与通过结果在 `out/tests/temp/browser_stop_receipts_20261024/artifacts/`。本轮仅 Linux，未重跑 Windows；普通管理入口的 unknown 接管、挂起队列和总期限继续 TODO，5.2/5.4/6.2 保持未完成，Agent 团队与协作专项继续 TODO。 交付复核同时保留主错误 code/statusCode；共享树既有按运行/节点隔离产物的改动暴露出模块服务日志读取及测试产物未初始化，首轮 7 failed/2 passed/2 errors、次轮 6 failed/3 passed 的原始结果保留。现由模块级 fixture 冻结服务日志位置并迁齐全部20个诊断消费者，每个 runtime 初始化本节点产物目录，未放宽错误/身份/终态断言。最终隔离候选与共享树各9项真实HTTP/Chromium通过，共享树42.81秒；各145项Bun、相关Ruff/编译/JS语法通过，OpenSpec strict 交付门另见同目录机器收据。未重跑其余52项Browser场景。
  - 2026-10-24 正式停止回执切片：Terminal 原停止链在进程树核实、Worker 退出确认和终态写盘全部成功后才清除原句柄、发布完成事件并返回；失败保留 unknown 与原执行，最终写盘失败后沿唯一停止链重试，两份清理/写盘异常保留 cause。共享 owner 在 stop/delete 结清前要求回执整数版本精确等于本次发布版本，旧版/未来版本拒绝且保留本次精确原 lease；外部快照拒绝布尔版本。完整原 HEAD 加反例的 Node 为 3 failed，真实 HTTP 为 8 failed；候选 Node 75 passed、owner/锁/HTTP 116 passed、真实 Terminal HTTP/PTY 14 passed/112.50秒，附件目录/三种 cwd/后台保活/GC 竞争 9 passed/87.69秒；Ruff、compileall、JS 语法与 OpenSpec strict 通过。准确命令、源码前后指纹、raw 与退出码在 `out/tests/temp/resource_stop_receipts_20261024/artifacts/`；首次 Browser 测试误写 Terminal 操作名的 4 项断言失败保留，现改为逐字段核对操作开始时的原 lease 身份，未放宽回执冲突。Node 注入仅证明句柄和顺序，HTTP 协议桩不证明真实外部进程停止；未重跑 Windows。native 无 PID 接管、unknown 显式管理、Browser 挂起队列与 operation 总期限仍 TODO，5.2/5.4/6.2 保持未完成；Agent 团队与协作专项继续 TODO。
  - 2026-10-24 Terminal 就绪前退出沿原 Worker 进程树终止链传回 PID/清理结果；只有停止证明、Worker 退出确认及终态落盘均成功才签发 failed 回执，保留原 Node 记录供丢失回执后的冷 owner 对账。普通 Worker 退出、无证明/native 异常、清理或落盘失败不结清占用，迟到启动消息不改写身份。完整原 HEAD 加新反例为 17 failed/30 passed；候选 Node 73 passed、双领域 owner/HTTP 96 passed、真实 Terminal HTTP/PTY 14 passed（114.17 秒，含六种启动退出/丢失回执组合），真实上传目录/Shell 启动失败/30 天 GC 1 passed（14.38 秒）；Ruff、compileall、JS 语法与 OpenSpec strict 通过。原始命令、完整快照/源码指纹、退出码和首次装具失败原件在 `out/tests/temp/terminal_startup_outcomes_20261024/artifacts/`。本片仅 Linux；未重跑 Windows，不将其当作跨平台进程树或真实后端硬重启的完整验收。未取得外部身份的 native 异常接管、旧代操作终止与 5.4 总期限继续 TODO，5.2/5.4 不勾选；Agent 团队与协作专项继续 TODO。
  - 2026-10-24 创建回执切片：Terminal/Browser 接纳前正式拒绝绑定本次身份与版本；Browser 原启动清理完成后返回正式失败快照。未知创建保留精确原 lease，存活观察、Turn/scope 收尾及冷恢复不能将其结清；正式未创建关闭 intent/关联及 cwd 占用，不把普通 HTTP 错误或任意 failed 快照当停止证明。Terminal 启动后的完整进程树终止核实和外部身份缺失的接管仍 TODO，5.2/5.4 保持未完成；Agent 团队与协作专项仍 TODO。准确命令、源码前后指纹及原始结果见 `out/tests/temp/resource_creation_receipts_20261024/artifacts/`，最终候选资源 owner/API/真实 HTTP 244 passed、客户端40 passed、真实 Browser故障/恢复40 passed（299.92秒）、Terminal HTTP/PTY 8 passed（92.49秒）；Node Terminal 41项通过。原 HEAD 加新反例16 failed，证明取消后原lease丢失及普通HTTP失败被误释放；Ruff/编译/strict通过。Browser/Terminal初次检查中的断言迁移与runner误用失败原件一并保留，不将其当产品通过。未重跑Windows，未声称全部未知创建/终止与生命周期组合完成。
  - 2026-10-24 已有资源的未知操作 lease 保留切片：工具、管理、卸载和孤儿回收均显式传递本次原 lease；回执丢失/取消由原 owner 与资源未知状态同批写入原账本，scope/Turn 收尾不再释放待核实占用，迟到 close 不覆盖已结清的 settled。明确失败回执、普通未知快照及同资源其它操作保持原收尾语义；冷恢复保留原 unknown 与错误原因，核实当前身份/版本的真实终态后才结清，不重发旧操作。完整旧 HEAD 加反例的真实 HTTP 取消为 4 failed/2.82 秒；最终候选的 owner/API/协议/HTTP 定向 290 passed/15.90 秒，真实 Terminal HTTP/PTY 8 passed/91.91 秒，真实 Browser 故障/恢复 40 passed/296.58 秒，Ruff/compileall 通过。命令、源码前后指纹、raw 与退出码在 `out/tests/temp/resource_unknown_leases_20261024/artifacts/`；初版范围过宽造成的失败及主动中断（exit 2）原件均保留，最终已完整跑通同一 Browser 选择，未放宽原状态、错误、重试或外部终态断言。未知创建的正式回执/lease 保留、旧代运行操作的接管/强制终止及 5.4 总期限仍 TODO，5.2 不勾选；本轮未重跑 Windows，也未追加 Agent 团队与协作专项实现或验证。
  - 2026-10-24 显式管理接管切片：unknown/reconcile_required 的 stop/delete 先核外部身份与当前版本，取得新的管理 operation lease 后复用唯一停止链；active lease、原未知 operation 身份及版本冲突仍拒绝。仅本次精确 verified 终态与 owner 落盘后结清旧占用，不自动重放旧工具动作，普通使用/关联/授权继续拒绝未知资源。Browser 检查点故障移除后由产品 API 完成关闭/删除，不再由测试先行关闭外部实例；硬退出后的存活 Terminal/Browser 由新管理命令停止。原 HEAD 加新反例为单元 7 failed、真实 Browser 2 failed（另有2个清理错误）；候选资源 owner/账本86 passed、HTTP取消/回执54 passed、Browser真实HTTP/Chromium9 passed/45.00秒、独立进程硬退出恢复8 passed/234.54秒，Ruff/compileall/strict通过。准确命令、源码前后指纹、原始输出与退出码在 `out/tests/temp/resource_explicit_takeover_20261024/artifacts/`。初轮硬退出装具仍靠已移除的产品环境变量选择服务而连接失败，已迁齐创建/恢复/孤儿及授权场景的显式 startup_mount；相关中断和旧动作预期失败保留原件。共享树最终复核与OS进程观察以该目录后续收据为准。未覆盖Windows、Gateway/主Web、未知native无PID、同进程active/持锁取消和Browser挂起队列；这些正确性边界及operation总期限继续TODO，5.2/5.4/6.2保持未勾；团队/委派/协作专项继续TODO。
- [x] 5.3 实现最后一次零关联起算的默认 30 天持久 deadline、重新 attach 取消期限，以及启动/低频到期查询；删除与 attach/lease/fork互斥复核，并可在中途重启后继续。
- [ ] 5.4 统一清理 operation 的截止时间、取消/终止核实与迟到回执，隔离共享 owner 锁对无关资源的阻塞，处理同步阻塞回调/线程池排队；明确 admission、Session 删除和 shutdown 的有界等待及失败返回，保留未核实状态和原 locator，不重复副作用或以扫描超时假报释放。覆盖真实锁竞争、网络未回执、事件循环阻塞及重启恢复。
  - 2026-11-01：canonical 提交与原确认共用受保护 worker，覆盖重复取消、上下文、迟到写盘错误；21 项单测、14 项存储／重启集成及1项真实 Gateway 工具循环／Backend重启通过。证据 `out/tests/temp/canonical_commit_settlement_20261101/artifacts/verification.json`；共享 publication 锁及其它同步入口仍 TODO，本项未完成。
  - 2026-11-01 Node调试冷恢复：原claim读取、OS恢复判定／轮询、恢复审计和活跃停止的最后身份核实通过原受保护worker结算；信号在同一worker内重新核对PID与起始身份后派发，等待后复用／不可比／消失均不发送强制信号。原owner锁覆盖取消，不新增writer或账本。108项不同用例通过：相关单测95项（含3项新身份变化反例）、真实ASGI／Node／Inspector原生等待与重复取消6项、既有冷恢复身份／日志故障7项；最终新matrix6 passed／30.19秒，原历史及冷重读动作逐项保持。首轮集成的临时文件冲突来自旧fixture冷重开后仍保留旧monitor并写claim；现仅停止旧观察任务／连接，保留原Node、claim和manifest字节，既有业务断言未改。Ruff、compileall及strict通过，生产及两个定向测试Pyright零诊断，原fixture文件3项既存诊断按规则／完整消息／函数／AST与HEAD一致。准确命令、原始失败／输出、退出码、源码指纹及作者自查见`out/tests/temp/debug_cold_recovery_20261101/artifacts/verification.json`。固定90秒／4GiB与原集成闸门；准入使用既有替身，不代Session删除gate、Gateway/Web、独立Backend硬重启或Windows。其它同步读写、原生I/O抢占及整个operation／shutdown总期限继续TODO，5.2／5.4及21/23保持未完成。
  - 2026-11-01 Node调试删除准备的catalog读取移交原SessionStore，在同一受保护worker内结清只读打开、main/child枚举、关闭及全部owner校验；删除入口移除同步副本，原gate覆盖取消结算。空库与不支持schema明确拒绝且原字节不变，删除准备不再给空库初始化schema。旧实现产品反例因事件循环被原查询阻塞而失败；可写版本的空库反例明确复现原字节从空内容变为SQLite。最终91项不同检查通过：85项原归属／存储回归、2项新增schema拒绝及4项真实ASGI导航删除竞争；其中受只读变更影响的原5项drain与新增2项集中复验7通过，正式新matrix4通过／15.37秒。查询／连接关闭等待及worker取消均核原连接关闭一次、gate、fence、未隔离原件、原locator与同operation恢复；不伪造删除记录。Ruff、compileall和strict通过，Pyright由HEAD13项降为10项既存mixin宿主属性诊断，无新增；原始消息、规则与AST对照保留。准确命令、源码指纹、红绿原件、退出码与作者结构自查见`out/tests/temp/debug_drain_catalog_20261101/artifacts/verification.json`。新suite固定60秒／4GiB并沿原独占闸门；本片不代Gateway、真实外部调试进程、硬重启或Windows验收，后续claim/manifest同步访问、原生抢占及整个排空总期限仍TODO，5.2/5.4继续未勾，整体21/23，团队／协作继续延期。
  - 2026-10-31 operation 关闭的 lease 写盘结算：两类 owner 共用原关闭入口，原资源锁内将释放及成功后移除引用一并交给既有 shielded worker；账本仍沿唯一 writer 串行。关闭先捕获原取消，再取消并结算原批任务，迟到原生失败保留在原取消/期限的错误链中；重复取消不遗弃排队或写盘，不重建lease，已提交释放不重写。完整旧生产归档的两项反例均因同步写盘阻塞事件循环失败；最终14项新场景及原关闭回归26 passed，相关owner/恢复/准入/账本539 passed/24.67秒（范围重叠不累计），Ruff、两源码Pyright零诊断、compileall及strict通过。首轮取消消息丢失和第二轮成功释放后引用遗留的失败原件均保留；修复原业务步骤的worker边界，原测试及断言未改，新测试的取消后成功引用预期按原合同校正。126份采样源码前后一致，作者AST自查仅原_release/close变化。命令、指纹、原输出/退出码和归因见 `out/tests/temp/operation_release_io_20261031/artifacts/verification.json`；临时完整归档已清理。期限场景用单调时钟推进，不冒充真实30秒或外部停止；原生I/O抢占、其它同步writer/通知线程归属、完整operation/shutdown及真实HTTP/PTY/Chromium/Windows仍未验，5.2/5.4不勾选。另一会话持集成闸门期间未排队、未修改或运行其Web/Workbench自动化，未重启共享debug。
  - 2026-10-31 共享 lease 账本写入竞争：同一账本实例的七类 mutation 共用原读改写锁，查询继续读取最后持久成功的快照；不新增 writer、格式或账本。旧生产七项真实线程反例全部失败于共用临时文件冲突；完整候选归档的12项竞争检查通过，仅锁 `_commit` 的变异实现被五项状态反例拒绝，明确核到 unknown/scope 被覆盖、非法新占用及 settled 回退。最终账本、资源恢复/释放、MCP 和 Node Debug 回归216 passed/50.23秒，Ruff、两份源码 Pyright零诊断、compileall及 strict通过；作者AST核对去除锁后原业务方法完全不变。准确命令、完整候选tree、源码指纹、原输出/退出码见 `out/tests/temp/resource_ledger_writer_serialization_20261031/artifacts/verification.json`；临时完整归档已删除。同步写盘、通知线程归属、可取消I/O和完整operation/shutdown期限仍TODO，5.2/5.4保持未完成。未修改或运行另一会话Web/Workbench迁移及自动化，未排队集成或重启共享debug。
  - 2026-10-31 Terminal cwd 解析切片：唯一原解析交既有受保护worker，复制请求上下文，原Thread holder覆盖排队及执行；重复取消等原调用结算后传播，不提前发布intent/lease/PTY或迟到补做。相对、原生绝对、工作区/Gateway目录URI及工作区外路径保持目标语义。原完整HEAD归档的相对/绝对/工作区URI/工作区外四项反例均在事件循环阻塞处失败；最终cwd、owner、operation回执与共享worker回归138 passed/6.01秒，Ruff/compileall及本change strict通过。Pyright原owner一项ResourceTransport.create_terminal诊断在完整旧源码同样存在，最终测试无新增；后补owner TODO只改注释，已机械核对AST不变。非法Gateway/directory ID、Proto Struct PID数值类型及公共metadata字段的装具错误均保留原失败，产品合同和原期限未变。原有资源准入两项通过；新增场景三种输入的业务断言已完成，但四个运行PTY的Session批量清理触发原10秒HTTP超时，整项失败原件在integration-1793441210346478698.stdout.raw，尚不能称完整资源清理通过。此cwd场景改为每次验证后经原detach/delete入口核同一身份及verified终态，只同时保留peer和当前PTY；最终仅补新增cwd场景为1 passed/31.57秒，经ASGI资源API与真实Node/HTTP/PTY核对三种输入、peer详情、实际进程cwd和逐项verified删除，原有两项结果按相同功能代码复用，不称最终整suite重跑通过。两次matrix仅在等待另一会话的独占闸门时到期，不计业务失败或通过，未改锁或并发上限。四PTY批量清理保留代码TODO及原证据；准确命令、源码指纹、raw/退出码、类型对照与作者结构自查在`out/tests/temp/terminal_cwd_worker_20261031/artifacts/`。本片不证明原生I/O可抢占、挂起操作终止或整个operation/shutdown硬期限，5.2/5.4继续未勾；未修改或运行Web迁移及自动化，也未重启共享debug。
  - 2026-10-31：Backend维护锁栈关闭复用既有shielded worker，原观察预算覆盖线程池排队及原生释放；取消/到期保留唯一关闭owner和后续依赖，原错误与已消费回调语义保持。11项单测及7项双挂载/ASGI集成通过，包含实际维护互斥、诊断响应及同入口恢复；证据见 `out/tests/temp/managed_attachment_cleanup_20261031/artifacts/verification.json`。任意原生线程终止、独立进程重启和完整shutdown期限仍未验收，本项保持未完成。
  - 2026-10-30 fork运行资源关联等待切片：原attach入口一次30秒覆盖恢复、两次资源锁、Thread准入、回执提交及历史回执查询，不改变公开逐资源结算、原fork watchdog或幂等身份。到期/取消按未派发、dispatching/unknown与committed分别保留原事实，解锁后不后台补做，同key只在未派发时继续实际关联。完整原HEAD归档加相同反例10 failed/10 passed，最终相关单元280 passed/6.60秒；产品消息/Agent生成来源、ASGI公开fork和原owner锁竞争的两项集成通过/36.09秒，核原目标持续published、超时零关联副作用及同key唯一关联，模型复用model_stream。首轮四项锁场景因装具在首次恢复前持锁而尚未到预定观察点失败，修正为恢复后持锁，原件保留，不计产品红例。准确命令、源码指纹、原始输出、静态/规范结果及作者架构审查见`out/tests/temp/fork_live_attach_deadline_20261030/artifacts/verification.json`。外设传输为替身，不代真实PTY/Chromium、Gateway/Web、Windows或Backend重启；同步阻塞、不合作取消、native无身份接管和全shutdown仍TODO，5.2/5.4保持未勾，前端与团队/协作专项未实施。
  - 2026-10-29 首次冷恢复清单等待隔离：移除持有共享准备锁的网络等待，并发调用各自读取原清单，首个成功结果在原无await提交段冻结版本；迟到清单不覆盖、不重复推进，取消/读取失败保留原归属。旧实现的6项单元反例及2项真实Node/PTY或Chromium/HTTP反例均在健康peer被共享锁阻塞处失败；最终相关owner/lease回归419 passed/10.09秒，原cold-management矩阵4 passed/38.24秒，Ruff、compileall及三个修改Python文件的Pyright通过。新集成测试最初误将正常detach后的版本写死为2、误用PID字段名，已根据原释放链和现行os_pid合同修正，失败原件保留；产品版本及身份断言未放宽。准确命令、源码指纹、raw和实际退出码见`out/tests/temp/uri-global-closure/artifacts/verification-073.json`。仅证明事件循环正常调度时的初始化等待隔离和原管理停止回归；同步阻塞、不合作取消、native缺少外部身份及整个shutdown仍TODO，5.2/5.4保持未勾。Debug服务未重启，团队/协作专项未实施或验证。
  - 2026-10-25 Terminal创建/关闭切片：原容量队尾持有完整创建，关闭后新请求、队列和异步准备重核准入；迟到就绪仍由唯一原关闭任务持有。每次默认5秒观察超时保留原任务；全批资源与最终写盘真正排空后失败才允许显式重试，未知启动不签停止回执。普通信号失败保留HTTP管理入口，重复信号不重复副作用，致命错误保持退出1。已迁齐唯一cwd异步stat及调用方，保留原生绝对/相对路径合同。Linux定向62项、原辅助服务全套Node174/Bun383项通过；另加真实线程池排队/HTTP回归2项通过（与原cwd用例重叠，不累计）。真实HTTP/PTY及POSIX信号6项通过（59.11秒）；完整旧生产加反例5项单测和1项真实HTTP信号均失败。准确命令、完整归档tree、源码指纹、raw和真实退出码见`out/tests/temp/terminal_creation_shutdown_20261025/artifacts/`。Windows同生产快照定向63项（含真实线程池排队）通过；Linux原生cwd两项通过，Windows外部绝对cwd通过。原Windows三输入用例的POSIX分号被cmd.exe当作文件名，原始PTY输出明确归因于测试命令；统一为Node ESM读写负载后，该用例在Linux/Windows分别通过（27.91/15.42秒），原身份、内容、lease及URI拒绝断言保持。额外旧生产线程池反例也失败；Ruff/compileall、JS语法、结构审查和OpenSpec strict45/0通过。字符串括号静态修正前后AST相同，复用原行为结果；Windows SIGBREAK未验证，不将本批称为全部shutdown验收。TODO：未知native启动接管、完整进程退出总期限、其它同步存储/不合作取消仍按5.2/5.4/6.2验收，保持未勾；团队/成员/委派与协作专项继续暂缓。
  - 2026-10-25 文件worker关闭顺序切片：停止先关闭新准入，原受理锁内重核排队请求并等待已接纳准备；shield观察唯一原worker，取消等待不取消线程中的文件发布，不提前丢弃句柄。原准备/写入未结束时拒绝关闭SQLite，已受理未执行意图沿原ID在下次启动恢复。原生产实现加新增反例4项全部失败；修复后文件操作、事件与独立进程崩溃恢复47 passed/25.15秒。正式双挂载关闭matrix 7 passed/111.17秒，新增场景经原文件入口受理，在真实线程写入前暂停，取消直接stop等待后核Backend总预算、健康挂载关闭、原catalog/维护锁保留；释放屏障后同一Backend关闭owner收敛，冷开原SQLite核一次提交与事件。没有手填业务账本，未运行Gateway/主Web或Windows，不证明任意线程可强制终止、请求本身取消时全部同步准备的收敛或事件循环阻塞可抢占；5.2/5.4/6.2保持未勾。准确命令、前后源码指纹、反例/通过raw与实际退出码在`out/tests/temp/foundation_contract_cleanup_20261025/artifacts/`（file-stop-red-005、file-stop-green-006、shutdown-files-matrix-008）；Ruff/compileall及strict原件在同目录。团队/协作与CIL第9节新规划均未追加实施或专项验收。
  - 2026-10-25 Backend整批关闭等待切片：唯一Backend owner从原模块物理迁出，全部挂载按逆序发起且独立推进，内部仍由各Workspace原任务串行关闭，MCP同任务合同保持。workspaces/catalog/maintenance/model_stream共用单次30秒观察预算；到期/取消不取消原owner，保留未完成阶段及依赖，同入口重试不重发成功步骤，迟到原错和截止/成功竞争均明确处理。最终6项单测通过/5.84秒；产品上传和执行生成双挂载历史的6项关闭matrix通过/115.72秒，原挂载/维护锁/catalog/远端fork export生命周期matrix全部25项通过/119.23秒。完整3d2510631旧生产代码加相同单测6项失败；真实Backend等待反例失败，补强时钟任务错误传播后的runtime反例也失败，均在健康peer被串行挂载阻塞处或原取消错误处被区分。最初runtime场景全局跳时误伤健康peer，现先核实peer原owner关闭再推进runtime时域，并平移Backend截止以单独验runtime；Backend预算另有独立场景，不放宽身份、顺序或存储断言。原挂载Goal准备屏障超时在候选和完整旧基线都复现，raw显示LiteLLM的GitHub价目表请求超时；仅临时测试profile改用SDK内置价目表后全组通过，产品配置与模型stream链路不变。初轮无单项限时的旧取消反例由外部90秒保护终止；另一轮同suite并行反例主动以143停止，原件全部保留，未当成业务通过。准确命令、源码指纹、原始输出、实际退出码和机械核验在`out/tests/temp/backend_close_deadline_20261025/artifacts/`；结构由作者按AST自查，不称独立审查。共享合并后的最终回归与静态/strict以同目录后续收据为准。本切片只证明事件循环正常调度时的等待预算，不证明同步关闭可抢占、不合作任务或外部进程已停止，也不替代生产进程/Gateway/Web/Windows完整退出验收；5.2/5.4/6.2保持未勾，团队与协作继续TODO。

  - 2026-10-24 Manager 检查点失败收尾切片：采集及收尾失败按原资源聚合，继续其它 Browser；context 释放、检查点删除或终态保存失败时保留原非终态 status 与 unknown，原身份可重试，unknown 保存也失败保留双异常和 cause，收尾未核实不关闭共享池。原完整基线的三个反例均失败；最终两份单测 158 passed，正式 Chromium/HTTP/磁盘矩阵及原生 Node 各 6 场景通过，目录冲突真实触发采集/删除双失败并验证健康 Browser 冷恢复。首轮新增场景遗漏 checkpoint 父目录导致 ENOENT，修正装具为递归创建后通过，原件保留。准确命令、源码指纹、原始输出及退出码在 `out/tests/temp/browser_shutdown_cleanup_20261024/artifacts/`。仅证明 Manager 层，生产信号退出重试、完整 shutdown 期限与挂起操作终止继续 TODO，5.2/5.4/6.2 不勾选；未重跑 Windows、Gateway/主 Web 或团队协作专项。
  - 2026-10-24 Browser版本撤销及关闭共用原队列有界drain，删除Session独有计时等待；撤销先落盘新版本，默认5秒未排空明确返回独立408并保留原操作，同版本重试不重放旧动作。原HEAD准备/执行两个反例均失败；候选两份单测152 passed，真实Node/Chromium/Python客户端HTTP场景1 passed，以及原关闭矩阵4场景通过。首轮常量提取漏迁一处帧排空引用导致29项单测及关闭回归失败，迁齐全部引用后通过，原始失败未覆盖。准确命令、源码指纹、测量及原始输出在 `out/tests/temp/browser_revision_drain_20261024/artifacts/`；Python首次恢复gate、持久化阻塞、强制终止与整个operation期限仍TODO，5.2/5.4/6.2保持未勾；未追加团队或协作实现/专项验证。
  - 2026-10-24 Manager排空超时不再走lost/删除检查点/关闭共享池的破坏性收尾；继续检查其它资源后汇总原错误，保留原操作及恢复依据，迟到操作完成后同一入口重试关闭。原HEAD的准备/执行两个反例均失败，明确观察到delete_checkpoint及pool_shutdown；候选两份单测150 passed，真实HTTP/Chromium矩阵4场景通过，包括未改写默认预算的Manager 5秒超时、磁盘状态/检查点原样保留、原页面仍活和关闭重试后的冷读。新增其它冷资源继续检查的断言随最终单测补验。准确命令、指纹、原始输出和退出码在 `out/tests/temp/browser_shutdown_failure_20261024/artifacts/`；整个shutdown总期限、其它失败收尾、挂起动作终止及进程退出协调仍TODO，5.2/5.4/6.2不勾选，Agent团队与协作继续TODO。
  - 2026-10-24 Browser 关闭排空改用原队列完整已接纳集合，移除串行尾部及在途计数轮询；Manager 不再跳过仍可能准备唤醒的冷资源，空闲冷资源保留原检查点。原 HEAD 的准备阶段反例 2 failed/1 passed，最终候选两份单测 148 passed；真实 Chromium/HTTP/磁盘检查点的准备、执行、准备超时三个场景均通过，关闭后冷 Manager 重开保持最终编辑值。首轮业务断言全部通过但 HTTP 测试服务清理顺序失败，修正清理后正式矩阵退出 0，未放宽产品断言。准确命令、源码指纹、原始结果在 `out/tests/temp/browser_shutdown_prepare_20261024/artifacts/`。Session 超时仍保留原操作、连接及锁；Manager 失败收尾、挂起动作终止与整个 shutdown 总期限继续 TODO，本项及 5.2/6.2 保持未勾选；未重跑 Windows、Gateway或主 Web，Agent 团队与协作继续 TODO。

  - 2026-10-24 完成 5.4 的资源锁纵向切片：全部 Terminal/Browser owner、管理/工具/授权、fork attach、Thread/Session 清理和 operation 关闭改用原记录对应的同一把资源锁，恢复只保留首次对账 gate；相关集合按固定资源 ID 顺序取锁，仍整组预检 lease 后才 detach，查询在等锁后重检关联。没有新增持久事实或兼容锁入口。完整原 HEAD 归档加新增公开入口反例为 2 failed/10.96 秒，两类 owner 都在 A 的挂起停止阻塞 B 查询时超时；代码候选 `4daa604d6a52b5160d827f6af80e6566537fd694` 的 62 项 owner 回归通过/2.04 秒，保留原代次、未知回执、fork 幂等、冲突及顺序断言。真实 Linux Terminal HTTP/PTY 8 passed/80.09 秒，其中新增两目录场景在 A 已经外部停止但最终回执被屏障挂起时，B 仍独立查询、停止并 GC；A 的目录/lease 直至原回执返回才释放，原停止不重放。Browser 创建/工具、真实 Node 锁拒绝和取消队列重入共 3 passed/22.18 秒。相同候选在 `hyf_win` 的 Windows Job Object（600 秒/4 GiB）下 6 项两类 owner 竞争及 8 项真实 PTY 共 14 passed/54.95 秒；前后源码 tree、工作树 blob 与 Git EOL 规范化 hash 已核验。Ruff、编译、strict 和作者结构自查原件及共享改动合并依据在 `out/tests/temp/resource_lock_isolation_20261024/artifacts/`；保留现有记录查询改名/批量 lease 对账改动，不清共享暂存内容。本项不证明网络未回执的终止接管、同步阻塞/线程池、聚合查询或 Session 删除/shutdown 的总截止时间，也不是完整跨端 fork/重启验收；生产相应位置保留 TODO，5.2/5.4/6.1/6.2 继续未勾，团队和协作专项继续 TODO。


  - 2026-10-24 补齐 5.4 的 runtime 等待纵向切片：准入和单 Session runtime 排空分别共用一次调用的 30 秒单调预算，不为构建、换代或下一 Thread 重置；超时与取消保留原 build/unload task、scope/generation 和原始迟到错误，不把超时当释放，也不在尚未完成的 builder 上并发 close。原基线完整归档加最终时钟装具的 7 项反例全部失败；最终 7 项定向回归通过，连同既有 residency/worker 共 57 passed，3 项既有删除故障/恢复回归通过。真实产品上传、消息执行和同一子树删除 owner 的集成 1 passed/20.64 秒：超时后 catalog deleting、原 locator、active control fence、物理历史逐字保留；原卸载继续，回执结束后同一 key 完成删除且通知不重复。该测试复用现有 model_stream，仅对未使用的外部资源清单设空，不是实际 Gateway/PTY/Windows 或 shutdown 验收。矩阵已登记独立 integration suite。Ruff、编译、strict、完整候选差异、准确命令/指纹和原始结果在 `out/tests/temp/runtime_drain_deadline_20261024/artifacts/`。首次装具路径、清理 lease 顺序、未绑定运行 loop 的时钟、已有扫描装具误拦准入等待和 locator 字段笔误均保留失败原件；扫描断言仍严格核对原 30 秒，只将装具限定为原扫描调用。此前领域排空、gate/retention 等待及全局 shutdown 总期限、真实 operation 终止和阻塞回调仍 TODO，5.2/5.4/6.1/6.2 保持未勾；团队/协作专项继续 TODO。


  - 2026-10-24 完成 5.4 的 HTTP 传输取消切片：Terminal/Browser 原入口统一使用异步 HTTPX，物理删除 `_json_request_sync`，迁齐所有调用与测试；默认 10/30 秒和 Browser run 预算余量保持，同次网络传输增加总期限。取消关闭客户端 socket，网络失败仍为 503，原 HTTP 错误正文仅进诊断，Browser 408 恢复字段与 owner unknown/重启拒绝重放保持。完整原 HEAD 归档加真实 HTTP 反例为 4 failed/7.02秒，分别复现两类客户端取消后连接未关及默认线程池饱和阻塞请求；最终6项真实 HTTP 竞争/慢速响应验证和正式矩阵通过，259项相关单元/API/工具回归通过/14.10秒，真实 Terminal HTTP/PTY 8 passed/92.14秒，Browser故障/恢复32 passed/214.31秒。仅对已删除的同步私有入口移除重复模式，独有故障场景及原身份、错误归属、持久状态、路径/Browser file:合同断言保留。首轮 Browser 协议桩漏填 page_id 的失败原件保留，未修改产品协议或放宽断言。Ruff、编译、完整候选差异、准确命令/指纹与 raw/退出码在 `out/tests/temp/resource_http_cancellation_20261024/artifacts/`；本轮未重跑Windows。取消 socket不证明服务端操作已停止，真实operation总期限、终止接管、同步阻塞/DNS排队和Session删除/shutdown仍TODO，本项及5.2保持未勾；团队与协作专项继续TODO。

  - 2026-10-24 Manager 整批排空切片：先同步关闭本批所有运行资源准入，默认 5 秒单调排空截止只冻结一次，后续资源只用余量，原空集合仍能即时确认；超时按原身份汇总，不关闭共享池或重置原操作。原 HEAD 完整工作树的两个反例均失败，复现后项准入仍开放和 3×200ms 累计约 605ms；候选 154 项单元回归通过。正式 Chromium/HTTP/磁盘检查点矩阵 5 场景通过，新增双 Browser 在约 5.003 秒整批失败而非各等 5 秒，页面和原操作保留，放行后同 Manager 重试关闭、冷 Manager 重开保留双方最终表单值。静态、strict、准确命令、源码指纹和原始退出码见 `out/tests/temp/browser_shutdown_batch_20261024/artifacts/`。这是 Manager 层组合门：生产 backend 信号关闭仍在失败后退出，未接同 Manager 重试，已在唯一调用方登记 TODO；整个 shutdown、创建中的资源准入、检查点持久化/转换期限和强制终止继续 TODO，5.2/5.4/6.2 不勾选。未运行 Windows、Gateway/主 Web 或团队/协作专项。

  - 2026-10-25 Browser创建与关闭竞争切片：关闭先拒绝新创建，原排队/配额/首次导航/最终persist共用已有默认5秒排空预算；超时保留原创建与运行时，迟到结束后沿同一检查点链重试。启动失败删除独立清理链，复用Session.close保留failed/unknown与原cause，未核实创建不能关闭共享池。完整旧实现Git archive tree `96f4b38a3b6a8d02cf5140fbfe1146f8a316d104` 的五项新竞争用例均失败；候选172项Manager/Session/队列/检查点回归、9项原生Node状态故障、Python资源owner/客户端回归与Node语法检查通过。九个正式Chromium/HTTP/磁盘检查点场景全部通过，创建超时约5.00秒、创建与peer等待组合约5.05秒，核对原页面/上下文、精确未创建回执、原资源身份及同Manager关闭后冷恢复。
    - 首次扩大Bun误收Node专用mock测试，按原生Node入口补验；启动失败复用close后保留failed状态的遗漏已修复，原断言未放宽。首次真实组合场景误要求已完成创建的健康Browser仍打开，而原Manager本应独立生成其检查点；改为核健康Browser已保存、挂起peer仍保留，所有身份/内容/总预算断言保留。原失败的完整Playwright对象格式化触发受保护OOM，后续失败诊断仅输出标量，未提高4GiB上限。准确命令、完整源码前后指纹、raw/实际退出码及产品观察在 `out/tests/temp/browser_creation_shutdown_20261025/artifacts/`；本切片只闭合创建竞争，5.2/5.4/6.2继续未勾，生产信号退出、完整关闭期限/强制终止、Windows实机、Gateway/主Web与团队协作专项未覆盖。最终共享合并后172项Bun（含新断言）、43项Python、9项原生Node及9个正式Chromium/HTTP场景通过；创建超时与共享预算的精确耗时见shared-integration-002原件。主线程自查发现created阶段关闭准入窗口，原候选加反例2项均失败；关闭标记现同步覆盖全部已登记Session，并验证原交互入口拒绝新操作，移除迟到finalizer补标记及重复遍历。原共享计数循环与小fixture重构完整保留，未纳入本批候选提交；AST/差异自查和静态/strict结果见同目录收据，不称独立审查。

  - 2026-10-25 冷恢复单资源失败隔离：原owner不再在首项授权同步失败后跳过其它资源或由后续访问重启整批版本；完成其它同步和ledger后上抛原单错/聚合多错，失败资源保持unknown，取消仍直接传播。完整原生产代码加最终单测为4 failed/2 passed；候选Terminal/Browser及锁隔离80 passed，真实Node/Chromium/HTTP的原撤权场景与新增冷owner场景2 passed/26.14秒。新增场景经正式Session/资源创建和完整生命周期关闭，再冷建正式服务图，由原get_resource接收真实408；健康Browser可继续执行，版本不重复推进，迟到旧结果仍拒绝，最后沿原管理入口删除。冷图不启动周期worker，不声称进程硬重启或挂起期间全局锁隔离已验收。前几轮共享测试目录、工作区复制helper签名和后台worker先消费恢复错误的装具失败保留原件，未修改产品身份/错误合同；最终按节点复制完整fixture并使用明确的Browser/Terminal服务端口。Ruff、编译、AST及准确命令/指纹/raw/实际退出码见 `out/tests/temp/resource_recovery_failure_20261025/artifacts/`，共享合并后80项回归（2.48秒）及同一正式matrix的2场景（24.81秒）通过，Ruff/compileall与45项strict均退出0；主线程AST自查确认本批生产行为仅修改recover，原共享lease批量查询/命名整理完整保留，不称独立审查。5.2/5.4/6.2仍未勾选，完整恢复/关闭期限、Windows/Gateway/主Web与团队协作专项仍未覆盖。

## 6. 真实验收与旧链物理下线

- [x] 6.1 经产品入口上传、Shell 预处理、读取处理结果、fork 与重启恢复；覆盖 Linux/Windows 原生 cwd 和远端 URI 始终在目标机器解释。
  - 2026-10-24：新增正式 `integration-attachment-product-flow`，复用 model_stream，经真实 Gateway/Backend 上传、PTY 在附件 derived URI 下读取原件并写结果、read_file、公开 full_rollout_copy fork、硬退出重启和目标新执行冷读；未手填执行、canonical、关联或 fork 状态。Linux 1 passed/100.93秒，Windows `hyf_win` 相同源码 1 passed/101.15秒；来源 canonical 原字节不变，目标保留原 URI 与历史字节并追加新执行。Windows 红测发现两处持久 POSIX locator 被当成本机路径比较，以及冻结详情复制/安装/清理的长路径失败；现统一持久格式，仅在原 I/O 边界复用 native_io_path，迁齐 lineage、前缀和传输消费者，不改历史身份/hash。详情损坏拒绝/前缀回归19 passed，activation来源及冷恢复4 passed，Ruff/compileall通过。最初直接 pytest 的10项fork回归在正式matrix准入前报错，后按matrix验证通过，原失败保留。完整源码指纹、Windows快照、命令、原始输出和退出码见 `out/tests/temp/attachment_product_flow_20261024/artifacts/`。此轮仅证明两台机器各自的产品入口；经过中继的真正远端 URI、5.2/5.4及6.2故障组合仍TODO，6.1保持未勾选；团队/委派/协作专项未执行。
  - 2026-10-24 Backend 硬退出恢复补验：Windows 真实入口先复现 activation ID 含冒号的目录创建失败，修正生成端为结构化 JCS 身份元组的固定长度摘要；detail 物理 I/O 复用现有 Windows 扩展路径原语，不修改 typed ref、locator 或历史正文。模型调用恢复改为查询既有唯一 owner 索引取得原 snapshot ID，迁齐 Saver/Coordinator/存储及测试并移除旧命名推算查询，不重写历史 ID/hash、不升 schema。Linux 211 项相关回归与正式 matrix 的 4 项 fork 身份回归通过，真实 Gateway/Backend 三次硬退出恢复 1 passed；Windows 完整代码快照 `a6197d944f73be71ac31da668bd6cff90565e9ef` 下 23 passed/85.09 秒，覆盖真实上传/接受/排队执行、旧代与新代关联、冷恢复释放及重复重启无副作用。硬退出测试按原生平台核对准确退出码；Windows 同时终止并核实本测试后端进程树，保留三次实际 venv redirector/解释器进程证据。未 arm 的测试屏障不再掩盖原错误。Ruff/编译、源码指纹、命令、真实退出码、三次 Windows 失败与最终原件在 `out/tests/temp/attachment_windows_recovery_20261024/artifacts/`，远端产物另经统一 collect 保存。完整 Shell→读取→fork→重启组合、真正跨机 URI 和旧 Linux detail ID 的跨平台物理迁入仍 TODO，本项及 6.2 保持未勾；Agent 团队与协作专项继续 TODO。
  - 2026-10-24 原生 Shell 验收共用目标端 Python 处理程序，删除 Linux-only skip，保留 URI/绝对/相对 cwd × 本地/具 Gateway 身份六例及 admission/GC 竞争一例；显式测试配置不再依赖个人模型凭据。完整候选 `27f6cf1c959961e1068d2a738712d7db82fe3c1c` 在 Linux 真实 Terminal HTTP/PTY 全部 7 passed/65.27 秒，核实后台子进程在工具返回后仍存活、到期 GC 受保护、原 owner 停止后真实终态及 lease 结清。Windows `hyf-win` 已激活同一候选并由目标 Windows Job Object（600 秒/4 GiB）运行；Session staging 创建触发 WinError 206，尚未进入 Shell 断言，不能记为 Windows 通过。TODO：由配置明确的 Windows 短路径目标或启用长路径支持的环境重验，不在本切片改系统设置或缩减业务断言。初次同步误收本地依赖链接导致激活失败，已按激活前干净状态恢复目标，再排除依赖链接成功同步；两次配置装具失败和原始结果全部保留。源码指纹、命令、真实退出码及原始输出在 `out/tests/temp/attachment_windows_20261024/artifacts/`；6.1/6.2 仍未完成，不外推完整 Gateway/fork/重启和挂起操作合同。
  - 2026-10-24 Windows 原件保护与 Shell 实机补验：专用短路径目标越过原 Session staging 限制后，首次在 258 字符 rollout 主库启用 WAL 时失败；默认 SQLite VFS 报告 1040 字节，尚未覆盖实际原生路径限制。再次缩短专用 checkout 后进入上传，真实复现原件先设只读、再以可写句柄 fsync 的 PermissionError。现只在原 protect_original 入口先取得句柄，再设只读并同步同一文件；Windows 重试在未发布目录内重新取得句柄，不跳过失败后的 durability，上传、fork 导入与离线安装继续共用该入口。完整代码候选 `78dbfdaaa3e8bf2fb538fa100a3c39a2dddbf5f4` 在 `hyf_win` 的 Windows Job Object（600 秒/4 GiB）下通过 2 项真实文件保护/失败重试与 7 项真实 Terminal HTTP/PTY 验收，共 9 passed/46.32 秒；涵盖三种 cwd 输入、本地/具 Gateway 身份、后台进程跨工具返回保活、admission/GC 竞争、真实停止及 lease 结清。Linux 23 项保护/locator 单测、13 项上传/预览/本地及跨工作区 fork 集成、7 项 Shell 集成均通过，Ruff/编译及 strict 原件见本轮证据。准确命令、完整源码指纹、同步快照与两次实际失败在 `out/tests/temp/attachment_windows_short_20261024/artifacts/`。具 Gateway 身份不是跨机器网络验收；Windows 完整 fork/重启组合及 SQLite 长路径预算仍 TODO，6.1/6.2 不勾选，未改目标系统设置或原开发服务，团队和协作专项继续 TODO。
  - 2026-10-24 SQLite 原生路径边界：全应用文件型 SQLite 连接（含隔离 fork 复制脚本）统一经 `sqlite_file_uri`，保留原 mode/immutable/事务，按真实 native 路径 UTF-8 字节核主库与 sidecar 预算；捕获、marker、安装/恢复及消息流去重写入迁齐原 I/O 原语，不修改逻辑身份/hash、schema 或系统设置。原完整 HEAD 加最终长路径产品用例在首次消息以 `unable to open database file` 失败；Windows 最小复现确认默认 VFS 1040 字节不保证普通 Win32 长路径可打开，258/600 字符 ASCII 及 300 字符中文路径改用正确 URI 后完成 WAL/读回。最终 Windows 快照 `e5168fba214ecd89740e32d996237f54df009d6f`（候选 `87b432d`） 的 17 项 SQLite 单测和1项真实 Gateway→上传→PTY处理→read_file→公开fork→硬重启→目标冷读共18 passed/100.33秒，产品 rollout 主库精确258字符，来源 canonical 原字节及目标历史身份保持，目标新执行成功。Linux 同一完整流1 passed/101.62秒、详情/损坏拒绝19 passed、fork保留14 passed；此前未受后续快照 I/O 变更影响的核心568 passed/3 Windows skip、存储51 passed、离线迁移/恢复87 passed、消息流74 passed已保留。Ruff/编译通过；准确命令、前后源码指纹、原始失败/通过与退出码在 `out/tests/temp/sqlite_windows_path_budget_20261024/artifacts/`。配置缺失及产物打包命令过长的装具失败另保留原件；不作为产品结论。未声称 UNC 网络共享、真正中继跨机器 URI、旧 Linux 非法 Windows 文件名迁入或5.2/5.4/6.2组合已验收，本项继续未勾选；Agent团队与协作专项继续TODO。
  - 2026-10-24 真实跨机器补验：Linux Gateway（`dee803f02c24f09f65271f814be16229e910ed90`）通过既有 SSH 隧道、配对和规范工作区代理访问 `hyf_win` 的隔离 Windows Gateway（完整快照 `e5168fba214ecd89740e32d996237f54df009d6f`，tracked 工作树干净）。远端仍由原产品用例完成上传、PTY 预处理、read_file、公开 fork 与 Backend 硬退出重启；在其目标冷读后，Linux 经真实 HTTP 发起目标新执行，Windows 按附件 derived URI 运行原生 Shell 并生成/读取新派生文件。核对普通文件的相对路径、Windows 盘符绝对路径和资源 URI 返回同一目标正文及 URI，目录导航、request_id、原件及来源 canonical 字节不变，Linux 同名 decoy 未改；模型复用原 model_stream，不手填执行/fork/关联状态。最终协调命令及 Windows 测试退出码均为 0，准确命令、源码/协调器指纹、HTTP 收据、SSH 日志和原始输出在 `out/tests/temp/cross_machine_attachment_uri_20261024/artifacts/`（`runs/real-ssh-005/result.json`、`receipt005.json`、`windows-receipt005.json`）。此前 teardown 顺序、缺 runtime manifest、误将 Gateway 路由 ID 与后端 UUID 比较的装具失败保留原件，修正后未放宽产品断言；临时本地工作区由原入口创建独立身份，避免 fixture 固定 ID 混用。此项关闭目标端寻址产品门，不声称两端为相同版本，也不覆盖最新 Terminal 启动失败改动在 Windows 的专项回归、UNC 网络共享、跨平台非法文件名迁入或 5.2/5.4/6.2 故障组合。

- [x] 6.2 外置时钟/故障点覆盖 29 天重关联、30 天到期、处理进程保活、attach/GC/fork竞争、漏 detach 崩溃、旧代迟到释放和删除中途重启；核对真实目录和外部进程终态。
  - 2026-10-24 补齐目录 GC 硬退出窗口：来源由产品 Session/消息/真实执行入口和现有 model_stream 生成，未手填执行或关联状态；独立 GC 进程在已登记 deleting 后、物理删除前/后分别以真实 exit 91 硬退出，再由第二个独立进程以原 catalog/locator 恢复至 tombstone。核对恢复 exit 0、普通读取在 deleting/回收后均不可用、零关联期限与 locator 不变、重复 GC 无副作用、canonical 字节不变，且同日期桶下未登记的合法形状目录不被吸收或删除。完整候选 `be65ba9dc620eb1cb23a5e7acc0e760fa979f62d` 的正式 matrix 2 passed/56.44 秒，Ruff/compileall 通过；准确命令、全源码指纹、原始输出、两个故障点各自的 crash/resume 命令与真实退出码在 `out/tests/temp/attachment_gc_restart_20261024/artifacts/` 及候选正式镜像产物。首次脱离 matrix、未启动执行入口既有必需 owner，以及子进程缺少显式 catalog 生命周期 owner 的装具错误均保留原件，未改产品代码以适配测试；只沿用现有执行装配初始化，不追加团队功能或专项验证。本切片证明 GC 进程恢复，不替代完整 Backend/Windows/挂起操作组合门，6.2 继续保持未勾选。
  - 2026-10-25 补验 fork 与到期 GC 的真实跨进程竞争：通过原产品消息/执行入口及 model_stream 生成来源，在目录冻结后、claim 提交前后分别启动独立 GC 进程；新 Session/main 与新 child 两种目标共4项覆盖两个串行顺序。GC 先行时原 reservation 拒绝、creation/capture/claim 中止、同键重试不复活目录，保留原冻结审计而不删除或改写；fork claim 先行时 GC 不删除，目标发布后关联保活且原件字节正确。全部保留来源 canonical 原字节、原 locator 和期限；没有新增生产校验或第二套保留链。完整 HEAD 候选生命周期 matrix 10 passed/116.42秒，共享工作树同矩阵 10 passed/120.10秒，Ruff/compileall 与 strict 45 passed/0 failed；源码清单与准确命令、退出码、GC 子进程原始输出在 `out/tests/temp/attachment_fork_gc_race_20261025/artifacts/`。初轮同步屏障及“中止审计必须不存在”的装具假设错误保留原件，不记为产品反例。本切片未覆盖挂起操作终止或整个 Backend/Windows 故障组合，6.2 继续未勾；团队与协作专项继续 TODO。
- [x] 6.3 迁齐调用方后物理移除旧可变附件 blob 身份合并、owner-ref 私有访问、Session 新引用生成与即时 GC 生产链及死测试；保留历史冻结资产及必要来源解析，不双读或提供兼容 fallback。
- [x] 6.4 按 design 的替代顺序同步相关 owner 规范与验收项，测量目录增长、查询/回收及恢复耗时；短期性能长尾以原始测量证据登记 TODO，不降低正确性门槛。

  - 2026-10-24 已将 itemized proposal/design、rollout 附件 requirement 及 fork 验收中的旧 digest 合并/永久 live 保活规则替换为目录 owner 合同；文件工具 design/spec 同步附件目录和 derived 写入，工具输出保留 Session 单文件边界。目录 owner、URI owner 与 Shell/Browser 保留范围继续引用唯一规范，旧任务证据不重写为新行为。正式测量复用现有 model_stream，经产品消息/真实执行和 unload 生成 1/32 个目录，核对独立身份、原件字节、真实期限、冷 owner 恢复、精确到期 GC、重复 GC 和 canonical 原文不变。最终代码候选 `177a70abf336b19ebe21d59eddf2a2771d2202ac` 的 matrix 为 2 passed/31.48 秒，Ruff、compileall 通过；完整指纹、准确命令、raw/退出码与 retention-1/32.json 在 `out/tests/temp/attachment_retention_measurement_20261024/artifacts/`。冷 owner 恢复约 7.52/9.11 ms，到期查询约 0.087/1.396 ms，GC 约 7.07/131.14 ms；测量范围及 WAL/终态元数据优化 TODO 已登记 design，不承诺固定比例、线性增长或完整启动性能。首轮测试对 text payload 误用 dict.get 的装具失败原件保留，修正提取类型后通过，未改产品代码或放宽业务断言。5.2/5.4 的接管/挂起终止与关闭期限、Windows/跨端完整组合门仍未关闭；Agent 团队和协作专项继续 TODO。


### 2026-10-25 资源 lease 持久发布（5.2/5.4/6.2 部分收口）

- 原账本的登记、获取、释放、结清、Turn 批量释放、恢复对账和未知结果转换统一先构造下一份快照，经原 JSON/fsync/原子替换链成功后才发布内存；失败原样上抛，当前进程及冷读都保留原事实，同一操作可重试。移除先改共享对象再写盘及逐 lease 写盘的旧实现，不新增账本、writer、schema 或恢复格式。保留批量释放单次写盘及资源/lease 原顺序，状态事件仍仅在持久成功后发布。
- 完整 HEAD 基线新增16项 fsync/原子替换故障全部精确失败；候选账本34 passed（0.73秒）。Terminal/Browser/Session 相关回归115 passed、1 failed（5.86秒）；失败是后台任务 handle.status 在删除后仍为 pending，完整未修改基线同一用例也失败（3.16秒），相关测试仅使用假的资源 owner，不涉及本账本，保留原断言与原始输出，不扩修后台任务。
- 真实上传、Shell 工具、Terminal HTTP/PTY 和到期 GC 故障验收1 passed（19.11秒）：由产品入口创建真实目录及进程，停止时精确拒绝目录 lease 的原子替换，核真实进程已停、原占用在当前进程及冷账本仍保留，GC不删除；普通查询保留 unknown，用户以新管理身份核实原停止后持久结清，再允许 GC，底层 stop 只发送一次。没有手填目录关联、lease或终态，也未把失败改为成功。初两轮对普通查询与显式管理核实的调用/重放假设错误按原 owner 合同更正，失败原件保留，不记作产品缺陷。
- 候选 Ruff/compileall通过；准确命令、完整候选源码前后指纹、真实退出码、原始故障/通过与基线归因在 `out/tests/temp/resource_lease_publication_20261025/artifacts/`。本切片不证明挂起操作终止、全局关闭期限、Windows实机、Gateway/Web或全部故障组合；5.2/5.4/6.2保持未勾，团队与协作继续TODO。
- 共享合并后补验：账本与 Terminal/Browser owner 单测102 passed（2.39秒），完整真实 Shell 集成文件10 passed（90.72秒），相关 Ruff/compileall及 OpenSpec strict（45 passed/0 failed）全部退出0。单测/静态/规范轮完整指纹一致；Shell轮仅 Web `useTurnVirtualScroller.ts` 被并发修改，被测后端/Terminal链源码未变，保留完整前后指纹，不记作全树冻结。合并保留既有批量查询、同状态免写及整批单次写盘，移除原地修改/回滚路径；共享暂存条目未改动。候选与共享结果分别保留，基线既存后台任务失败仍未通过。


### 2026-10-25 冷恢复按资源隔离（5.4 部分收口）

- 原 owner 只核对一次清单并持久冻结授权版本，后续按原资源锁完成同步；查询、传输授权、管理/fork关联、工具调用与 Thread/Session 释放迁齐所选资源入口。工具准入、核实、锁与 lease 获取收敛到唯一 `_resource`，移除旧 `_use`、整批恢复锁和恢复布尔状态；`recovery.py` 仅保存本进程待同步的版本引用，不新增业务账本、持久格式或外部操作 writer。取消排空本次任务，保留未核实版本；后续重试复用该版本，不再次推进已完成资源。
- 完整原 HEAD 加新增故障回归在 Terminal/Browser 两项健康查询均超时（2 failed/4.93秒，源码前后一致）。候选资源回归118 passed/3.37秒、工具入口34 passed/1.71秒；共享合并保留原私有查询改名及批量 lease 查询，153 passed/4.29秒。覆盖挂起期间健康资源查询/过滤列表/创建/授权/工具/Thread释放、同资源等待者取消、整批取消、原版本重试、原错误身份及多错顺序；不通过删除未知记录或重发停止制造进度。
- 完整候选真实 Shell 文件11 passed/112.85秒：由产品上传/Shell 创建并提升两个真实 PTY，第一代服务按既有清理解除关联；第二代完整工作区服务图从原 catalog/owner/lease 文件装配，真实 steering 主动恢复时挂起第一终端的授权同步。第二终端仍能被新 Thread 关联、经工具读取、核实停止并释放目录供到期 GC；第一进程、原件和目录占用保持，解除故障后沿同一版本同步/核实停止并回收。未手填资源/关联/lease 状态；这是服务图冷重建和真实外部 PTY 边界，不冒称 Backend 硬退出或 Windows/Browser 实机组合。
- 首轮候选因作者混用共享树私有查询名而中止，修正为对应提交基线的唯一名称后通过；真实验收前两轮分别是故障注入晚于主动 steering，以及把增量输出误当完整缓冲，已按原源码合同纠正并保留失败原件，未放宽进程/占用/回收断言。准确命令、完整指纹、原始输出及退出码保存在 `out/tests/temp/resource_recovery_isolation_20261025/artifacts/`。首次清单、集合清理整体等待、挂起操作真正终止和全局关闭期限仍 TODO，5.2/5.4/6.2保持未勾；团队、成员、委派和协作不追加实现或专项验证。


### 2026-10-25 资源记录持久发布（5.2/5.4/6.2 部分收口）

- 原 ResourceRecordStore 改为隔离读取及唯一 save 发布，创建、管理/授权、Terminal/Browser 工具、冷恢复、fork 回执与离线备份消费者迁齐；移除公开可变 records 和 persist。等资源锁后重新读记录，失败的 scope、关联、版本和终态不混入 unknown；终态落盘后才结清 lease，unknown 写盘前先保护原占用，后续普通存活观察不得清除原账本的待核实事实。沿用原 schema 1，不新增格式、账本或兼容 writer。
- 完整原 HEAD 的24项序列化/fsync/替换故障全部失败且源码前后一致；最终候选资源与真实 HTTP 回归208 passed，共享资源/工具/HTTP232 passed。完整真实 Shell/PTY文件在候选12 passed/120.99秒、共享12 passed/119.10秒；新场景由产品上传及 Shell 创建真实处理进程，核实进程已停但 Python owner 终态发布失败时，原目录和精确占用继续保留，管理核实持久成功后才允许到期GC。离线记录准入/字节保全12 passed/24.12秒。
- 扩大 fork 检查发现旧跨 Gateway action 断言与手工历史缺少冻结队列输入，完整未修改基线分别复现；不修改生产合同以适配。来源删除、冷恢复和 HTTP 完整验收迁用现有产品执行 fixture/model_stream，保留原身份、版本、关联和回执断言；共享正式 live fork 矩阵22 passed/181.57秒，退出0。该轮仅无关的事件流单测被并发修改，被测源码未变，不记作全树冻结；真实 Chromium/HTTP 关闭故障及重试2 passed/15.88秒。旧跨 Gateway action 断言仍属未通过项；早期未进入业务的环境错误或被中止轮次不记作通过。
- 准确命令、完整源码指纹、原始失败/通过和真实退出码保存在 `out/tests/temp/resource_record_publication_20261025/artifacts/`。本切片不证明挂起操作终止、全局关闭期限、Windows及Browser完整故障组合；5.2/5.4/6.2继续未勾，团队、成员、委派与协作继续TODO。全量JSON记录写入成本及既有owner文件膨胀不在本轮追加优化。


### 2026-10-25 单次 operation 关闭隔离（5.4 部分收口）

- 关闭沿每个原资源锁独立释放，原账本持久成功后才遗忘本次引用；移除整批锁和先 pop 后释放，单错原样上抛、多错保留顺序聚合，取消后沿原 operation/lease 重试，不新增 writer 或占用账本。
- 完整原 HEAD 的4项故障/忙锁用例失败且源码前后一致；候选资源、工具及真实 HTTP 取消回归239 passed/8.67秒，覆盖单错/多错、冷账本、忙锁隔离、取消等待和原占用重试。共享资源/工具/真实 HTTP 取消回归240 passed/9.84秒，完整源码前后一致；两处 Ruff/compileall 与 OpenSpec strict 均退出0。准确命令、源码指纹、原始输出和退出码在 `out/tests/temp/resource_operation_close_20261025/artifacts/`。
- 本切片不证明外部进程终止、同步阻塞隔离或整个 operation/shutdown 的总期限，5.2/5.4/6.2保持未勾；团队、成员、委派与协作继续TODO，不追加实现与专项验证。

### 2026-10-25 Browser 原执行终态核实（5.2/5.4 部分收口）

- 工具超时后的原脚本和页面reset由同一队列持有；排空在原单次预算内继续核对后来登记的执行句柄。stop/delete释放context后仍核实原执行结束，默认5秒未排空返回408并持久保持unknown；同身份/版本重试不重复关闭context，核实后才发布终态。
- 完整原生产代码加最终关闭单测4项均失败；新增“排空先开始、原执行后登记”的2项反例在修正前均错误返回true。最终候选相关单测190 passed，正式`integration-browser-owner-revision-drain`全部4 passed/42.29秒，真实Manager关闭9场景通过；新场景由原创建/执行/停止/删除HTTP入口驱动真实Node/Chromium，受控HTTP仅暂停原fetch，核对磁盘unknown、context释放、peer可用和原页面只关闭一次。
- Ruff、compileall和Bun语法解析通过，运行前后源码指纹一致；准确命令、原始输出、实际退出码、结构自查及失败归因在`out/tests/temp/browser_close_executions_20261025/artifacts/`。最初4项失败来自单测装具漏CDP句柄，单列保留，不记产品反例。结构自查未发现本批职责越界，既存大文件未扩大重构，不称独立审查。
- 尚不支持强制终止不合作脚本；Python业务owner停止前的版本等待已由下述停止准备切片补齐；整个operation/shutdown期限、生产信号入口失败后退出协调、Windows和Gateway/主Web本批未验。5.2/5.4/6.2继续未勾，团队与协作继续TODO。
- 共享合并后同批单测190 passed、正式Python/Node/Chromium集成4 passed，Ruff及45项OpenSpec strict通过；共享index完整保留。机器收据`verification001.json`核对原始输出hash、前后源码、实际退出码与进程组清空，仅作为运行证据，不替代上述业务断言。

### 2026-10-25 Browser 管理停止的准备顺序（5.2/5.4 部分收口）

- stop/delete推进版本显式标记停止准备：先撤权、关闭新准入并持久化，不提前等待依赖context关闭的原脚本；真正关闭与终态核实仍走原Session.close和唯一队列。普通恢复/关联/授权继续原排空语义，Python传输及其全部直接/间接消费者迁齐必填意图，内部HTTP拒绝缺少boolean的旧请求，不新增兼容或第二停止链。
- 完整未修改基线经真实Session/Python业务owner→Node/Chromium的stop/delete均在撤权排空阶段失败，修正后两项通过；完整Browser集成6 passed/54.65秒，共享6 passed/52.67秒。候选Browser单测171 passed、原生Node Terminal 22 passed、资源live-fork正式矩阵22 passed/190.26秒、真实PTY冷恢复/GC场景1 passed/27.37秒，共享资源/传输/工具/API单测199 passed。Ruff、compileall、Bun解析及45项OpenSpec strict通过；源码前后指纹一致。
- Python回归中的16项既存unknown动作/transition断言在完整未修改基线同样失败，未放宽断言；误用Bun执行node:test的mock限制已按原生Node运行器核实通过。首轮静态检查因运行保护禁止读取Git index而未进入检查，随后冻结文件清单再通过。原始命令、输出、退出码、指纹与合并前件位于`out/tests/temp/browser_management_stop_20261025/artifacts/`，自查无本批职责越界，既存大文件不扩范围拆分。任意脚本强制终止、首次冷恢复gate、整个operation/shutdown期限及生产信号失败后退出协调仍TODO；5.2/5.4/6.2保持未勾，团队与协作继续TODO。

- 2026-10-25 API合同补验：按既有“未知资源由新管理操作核实停止”场景迁齐5个旧测试（16个参数化失败），未知资源在身份/版本可核对时提供stop候选；外部身份缺失仍无动作。增加旧版本拒绝、传输授权拒绝、失败的新管理请求保留两笔精确待核实lease，以及故障解除后经同资源新管理请求核当前版本/终态再结清的API断言。普通Browser操作重试仍409，原生错误和路由、原身份与请求次数断言保留。候选完整文件62 passed/4.40秒、Ruff通过；基线16失败原件沿用前述`browser_management_stop_20261025`，本批原件在`out/tests/temp/resource_unknown_api_contract_20261025/artifacts/`。此为原合同的测试迁移，无产品行为或格式变更，不扩张团队/协作范围。

- 同批协议消费者补齐：完整未修改的`505665b43`在Browser/Terminal协议错误文件另复现84项失败（83项旧动作预期、1项已删除私有恢复标志）；现改为真实API观察unknown与版本不匹配时无动作/拒绝stop、普通查询不重发撤权，新冷恢复同步版本后才允许显式stop。原错误cause、HTTP状态、诊断正文、身份与次数断言保留。最终共享四个相关API文件151 passed/12.04秒，Ruff与候选compileall通过；合并保留既有专用fixture名称。候选早期fixture名称错误、误以为普通查询会自动重发撤权的断言，以及合并尚未写入时重复执行旧测试的失败原件均保留，不计通过。5.2/5.4/6.2仍未勾，团队与协作不追加实现或专项验证。


### 2026-10-25 Browser 生产信号关闭重试（5.4 部分收口）

- 原 backend 信号入口不再在 Manager 排空/核实收尾失败后关闭 HTTP 并丢失重试入口；完整原异常留在日志，原进程、Manager、执行与恢复依据保留，关闭准入不回退。信号监听允许再次调用同一关闭链，在途重复信号不重发；成功后才停止监听并按原信号退出。未捕获异常/rejection 仍按致命规则退出 1，关闭过程中到达的致命错误也先记录并升级退出事实，不被在途去重忽略。
- 完整 HEAD 候选正式矩阵 6 passed/45.75 秒，共享工作树同矩阵 6 passed/45.41 秒；真实 Linux SIGTERM/SIGINT/SIGHUP、Node、Chromium 与 HTTP 屏障覆盖默认 5 秒排空失败后的原页面/操作保留、拒绝新动作/创建、重复信号、操作放行后关闭与真实冷进程恢复最终表单值。另有独立致命异常、rejection 及信号关闭中途致命异常三项回归；致命路径的 HTTP 排空仍等待原请求，测试显式放行，不把它声称为有界 shutdown。原 Manager 回归 42 passed，Ruff、JS 解析、compileall 通过。
- 完整旧 HEAD 归档仅增加相同测试与 suite，最终 4 failed/2 passed：三项信号场景分别暴露管理 HTTP 失联或重复信号退出，关闭中途致命错误原先被去重吞掉；两项独立致命退出为正例。首轮装具曾阻塞原串行队尾并混用场景日志，后续致命路径误设总退出期限、日志同步过窄，以及一次候选/反例并发端口冲突均保留原件，不作为产品反例，最终改为真实 HTTP 可用性和实际退出事实判断。
- 准确命令、完整源码前后指纹、真实退出码、原始输出、按场景保留的服务日志/结果与结构自查在 `out/tests/temp/browser_signal_retry_20261025/artifacts/` 及对应正式产物。候选最终源码完整冻结；共享产品运行期间有未加载的 Web/独立测试文件变化，完整冻结拒绝保留在 `verification002-rejected.json`，仅出具明确范围的 `verification003-limited.json`，不冒称共享全树冻结。未覆盖 Windows SIGBREAK、上层启动器强杀、Gateway/主Web、任意脚本强制终止或整个 operation/shutdown 期限；5.2/5.4/6.2 继续未勾，团队、成员、委派与协作继续 TODO。


### 2026-10-25 批量 runtime 关闭隔离（5.4 部分收口）

- Session/服务关闭分别等待本批原 state；首个 Thread 的构建、lease、listener 或 scope 挂起不再阻止其它 Thread 开始排空。重入复用唯一原 build/unload；单 Session 全批共用原 30 秒截止。取消排空本次等待者，原任务保持；原异常按输入 owner 顺序汇总，失败沿同入口重试，不增加另一 owner 或自动重发。
- 候选定向回归 68 passed/17.90 秒，完整旧 HEAD 加新增单测 11 failed/0.52 秒；正式产品矩阵 5 passed/71.86 秒，通过产品上传、消息与真实图执行生成两个 Session，核对 peer scope closed/tracker cold、附件 detach/deadline、原 canonical bytes、locator 和 checkpoint。模型复用原 model_stream；Terminal/Browser 清单仅隔离本场景未使用的外部服务，不能代替外部进程或完整后端退出验收。
- 完整旧 HEAD 的同一产品场景两项反例均因 peer 无法开始关闭而超时失败（2 failed/49.55 秒），不是健康接口或 helper 反例；45 项 OpenSpec strict 全部通过。Ruff 与 compileall 通过；首轮产品测试错把 scope 回调发生当作 tracker 已发布 cold，第二轮缺少读取 checkpoint 所必需的显式 owner，均只修正测试同步/调用，未放宽业务断言；失败原件保留。准确命令、源码前后指纹和实际退出码在 `out/tests/temp/runtime_shutdown_isolation_20261025/artifacts/`。
- 全局 shutdown 总期限、存储关闭顺序、同步阻塞和强制终止仍 TODO，5.2/5.4/6.2 保持未勾；团队、成员、委派与协作不追加实现或专项验证。

- 合并后保留共享既有 state 类型拆分，定向单测 68 passed/16.97 秒、同一正式产品矩阵 5 passed/63.26 秒，Ruff 及 45 项 OpenSpec strict 通过。主线程 AST/目录自查未发现本批职责越界；候选既存大文件和测试目录规模已复核，不称独立代理审查。共享暂存内容保持原样。


### 2026-10-25 Backend 关闭依赖与原入口重试（5.4 部分收口）

- 原两层退出栈改为唯一具名关闭链，失败保留原步骤、后续存储、locator 与维护锁，独立挂载继续排空；全部完成后才关闭 catalog。专属 Workspace task 保持 MCP AnyIO scope 的创建/关闭身份，并发等待复用原执行，禁止新生命周期覆盖未完成关闭。catalog 真实关闭成功后才标记完成；回滚失败仍立即隔离业务访问且保留首错，物理关闭可继续重试。
- 全局 runtime 关闭共用原 30 秒单调预算，构建/lease/listener/scope 挂起仍保留唯一原任务，不假报释放。候选产品 5 passed/111.62 秒，完整相关生命周期及远端 fork 清理回归 25 passed/124.69 秒，catalog/runtime/装具单测 346 passed/11.57 秒，Ruff/compileall 通过。完整旧生产的产品反例 4 failed/1 deselected，runtime 超时四项单测反例 4 failed；均保留完整旧生产归档，不拼接旧源码。
- 早期配置文件准备错误、默认模型 fallback 导致未规划调用、旧错误聚合/失败后关库断言，以及任务创建入口与 writer 屏障互等的测试准备均有原始失败；最终使用工作区覆盖关闭 fallback、观察真实任务接纳，并保留身份、错误对象、原期限与完整关闭顺序断言。fork 首轮直接 pytest 因缺正式 matrix watchdog 未进入业务，后续经正式矩阵三项通过，不以装具失败冒称产品反例。证据在 `out/tests/temp/workspace_shutdown_order_20261025/artifacts/`；共享树新产品 5 passed/100.55 秒、相关生命周期与远端 fork 回归 25 passed/120.59 秒、单测 346 passed/10.19 秒，Ruff/compileall 通过；45 项 OpenSpec strict 全部通过，`verification001.json` 机械核验前后源码、profile、原始日志与实际退出码。未运行 Gateway/主 Web、Windows 或真实生产进程退出专项，本批不宣称覆盖这些入口。
- 结构自查：main 的生命周期主体物理迁入同层单一模块，旧入口/全部调用方迁齐，不加 shim；新模块约 445 行，职责集中于 Workspace/Backend 启动、依赖关闭及重试，无新增源码目录。整个 Backend 总期限、同步阻塞、线程池与外部终止核实仍 TODO，5.2/5.4/6.2 保持未勾；团队、成员、委派及协作不追加实现或专项验证。


### 2026-10-25 冷恢复单次总等待预算（5.4 部分收口）

- 原ResourceRecovery在入口只取得一次30秒截止，覆盖首次清单、准备锁、资源锁及授权同步；到期取消并排空本调用的协作式等待，不取消其它调用持有的原同步。未开始同步不伪造unknown；已发送未核实的同步保留原lease、unknown和冻结版本，重试不推进已完成资源或重放旧业务副作用；原transport超时和外部取消仍传播。
- 固定HEAD完整候选的资源/锁回归138项通过；新增场景进一步通过产品operation制造丢失回执，10项通过，核对精确原lease不被到期/重试释放。真实Node/Chromium/HTTP矩阵8项通过，覆盖清单和版本回执暂停后推进原30秒截止、页面/版本保留及同版本重试。测试只推进截止事件，没有缩短生产合同。旧生产完整archive与相同新增测试用于反例；最终源码、原始输出、退出码和核验记录保留在 `out/tests/temp/resource_recovery_deadline_20261025/artifacts/`，不以包装进程成功代替命令结果。
- 首次真实矩阵在fixture准备阶段被既有端口容量拦截（261个文件超过250）；原端口单测同样复现。独立提交 `b3f8b0f05` 将容量扩为350，保留100端口块与E2E不重叠，补核TCP上限；8项端口单测通过后重跑真实矩阵。未修改环境配置或放宽业务断言。
- 该边界仅证明事件循环正常调度、传输可取消时的恢复等待期限；不代表真实网络永久挂起、独立Backend硬重启、同步阻塞/线程池、不合作任务终止、Gateway/主Web或整个operation/shutdown验收。5.2/5.4/6.2保持未勾；团队、成员、委派和协作继续TODO。

最终共享工作区复核：资源/锁单测138 passed/4.47秒，真实Browser矩阵8 passed/61.21秒；Ruff、compileall及OpenSpec strict 45/45通过。相同新测试在完整旧生产archive为单测8 failed/2 passed、真实矩阵2 failed/6 passed。测试期间另有开发启动脚本及其JS单测三处并行改动，均不在本场景执行/导入链；逐项源码指纹保留原差异，依赖审计见 `nondependency-audit001.json`，没有将全范围源码说成未变，也未覆盖这些改动。全部运行命令、原始日志、实际退出码与进程回收核验见 `verification001.json`。


### 2026-10-25 聚合资源列表网络等待隔离（5.4 部分收口）

- Terminal/Browser的聚合列表只发一次外部清单请求，网络等待期间不占资源锁，收到后逐资源短暂加锁并重检过滤及已核实删除。仍应返回的完整记录已变化则显式冲突，不让旧清单覆盖已提交命令或同版本外部关闭观察；取消不发布未收到的观察。保持原记录/lease链、排序及整批释放preflight，不新增持久状态、每资源HTTP或兼容实现。
- 固定HEAD候选相关单测/API回归215 passed/9.05秒，新真实Node/Chromium/HTTP矩阵2 passed/15.98秒；正式Session/资源入口生成状态，慢清单回包期间peer查询与run工具返回42，随后正式stop或Node直接关闭加owner观察提交终态，旧回包冲突且owner/lease字节不回退，刷新只取一次清单。复用fixture按当前测试文件隔离工作区/产物。旧版本完整archive叠相同测试，单测8 failed/2 passed、真实矩阵2 failed，均在独立查询/命令被列表所持锁阻挡处复现。
- 首轮候选使用不存在的私有查询名，修正为固定HEAD唯一原方法；真实矩阵首轮收尾未先detach而被既有删除规则拒绝，后续改用正式detach/delete，没有放宽产品断言。扩展回归首次命令误写不存在的测试路径，在收集前退出4；Ruff首次仅import排版失败，修正后通过。所有失败原件保留，不作为业务通过证据。准确命令、候选/共享源码指纹、raw/真实退出码及进程回收在 `out/tests/temp/resource_list_isolation_20261025/artifacts/`。
- 作者AST/目录自查无本片新增职责或import边；既有约900行owner及超过20文件的单测目录仍为后续结构审查风险，不启动代理专项。逐资源观察持久化仍使用完整JSON写盘，批量写入与同步fsync成本保留TODO，未声称性能提升。列表恢复/锁等待与整组释放总预算、同步阻塞、外部终止和完整shutdown继续TODO，5.2/5.4/6.2保持未勾；团队、成员、委派和协作不新增实现或专项验证。

共享最终复核：相关单测/API 215 passed/8.92秒，新真实Browser矩阵2 passed/14.55秒，既有恢复矩阵8 passed/61.93秒；Ruff、compileall和OpenSpec strict 45/45通过。共享已有私有查询改名在三方合并保留，旧HEAD候选继续使用其唯一原名，无shim。共享全树diff检查另报协作模块既存EOF空行，本片候选diff检查通过；该无关模块按用户裁定不修改。全部候选、反例与共享运行的源码前后、日志、退出码及进程回收由verification收据核验，共享暂存指纹始终不变。


### 2026-10-25 临时孤儿回收独立推进与整批截止（5.4 部分收口）

- 原Terminal/Browser孤儿回收改为按资源独立恢复、取锁和复核/删除，单项冷恢复授权、资源锁及删除回执挂起不阻止peer完成。入口一次30秒覆盖准备、恢复、锁、撤权与删除，不逐阶段重置；取消排空本次等待，未开始不伪造unknown，已派发未核实保留unknown和精确原lease，已成功的终态不回滚。原错误按资源顺序汇总；下轮不自动重发未知删除，显式管理沿原owner核实。
- 候选233项相关单测/API回归通过，随后新增冷恢复授权截止两个场景，最终235项通过；覆盖准备/恢复/锁/撤权/删除预算、取消、底层超时归属、真实detach产生的到期资源与原lease结算。真实Node/Chromium/HTTP矩阵1 passed/11.24秒，经正式Session、创建、detach和回收生成关键状态；peer在首项撤权回执等待中完成真实删除，首项删除已落盘但Python回包暂停，推进原截止后unknown/lease保留，显式核实同版本终态且删除调用始终各一次。模型不参与此资源边界，不构造执行/恢复业务事实。
- 完整原HEAD archive叠相同新增测试及suite的反例为18项单测失败，新增两个冷恢复截止场景也失败；真实矩阵1 failed/16.33秒，停在peer不能开始删除的屏障。候选Ruff、compileall与OpenSpec strict通过，原始命令、完整源码指纹、raw/退出码和进程回收核验在 `out/tests/temp/resource_orphan_sweep_20261025/artifacts/`。作者AST/目录自查未发现本片职责越界；既有约900行owner及超20文件单测目录仍是结构风险，不称独立审查。
- 本片只证明事件循环正常调度、等待可取消时的原孤儿回收边界；同步写盘/回调阻塞、不合作操作及真实外部终止、整组释放/Session删除/生产shutdown和完整重启故障组合继续TODO。原30分钟临时孤儿与附件30天期限保持，5.2/5.4/6.2仍不勾，团队、成员、委派及协作不追加实现或专项验证。

真实验收最终收口到跨辅助服务目录 `tests/integration/workspace_services/test_resource_orphan_sweep.py`，suite为 `integration-resource-orphan-sweep`（120秒/4GiB），同一场景参数化覆盖真实Terminal PTY和Browser Chromium；未提交的Browser专用文件已物理迁走，fixture不复制。固定候选2 passed/23.27秒；最终共享235项单测/API回归及两类真实资源均通过，准确原始耗时见各run。相同最终场景在完整旧生产为2 failed/33.53秒，均阻在peer回收屏障。测试路径扩展曾误改既有profile，原始运行未改写：两版准确字节和所有recorded hash已核对并保全于profile-versions001.json；此后使用独立002 profile，源码/日志/退出码及进程回收核验见verification001–004。该测试未代替独立进程硬重启或不合作任务终止验收。


### 2026-10-25 整组关联释放检查与独立收尾（5.4部分收口）

- 原Thread/Session资源drain在全量原锁内重读/核全部lease，检查与任务接管间不让出事件循环，保持冲突不部分detach。未知终态观察只持自身锁；检查通过后各资源独立完成并立即解锁。入口一次30秒覆盖准备/恢复/观察/集合锁/撤权/删除；取消排空本调用，未知保留精确原lease，不重发删除。物理删除仅有调用的旧集合锁helper，无兼容链或新持久格式。
- 准确命令、固定候选/完整旧生产反例、原始日志、源码指纹与退出码在`out/tests/temp/resource_release_batch_20261025/artifacts/`。候选初轮44项中42通过，Browser operation误用Terminal专有方法的2项准备错误已改为原Browser perform；初次扩大命令误写不存在的API文件，未进入业务。Ruff初轮max建议及import排序均修复，原失败保留。随后候选279项资源/API和2项真实PTY/Chromium/HTTP通过；最终含未知观察隔离的48项及共享验收结果按原件核验。
- 真实场景使用正式Session/创建/operation及原Session资源drain，不伪造Thread generation；Thread释放由同owner参数化单测覆盖。整个Session删除/runtime卸载、同步阻塞、不合作任务与完整重启组合仍TODO，5.2/5.4/6.2保持未勾；团队/成员/委派/协作不追加实现与专项验证。


### 2026-10-25 29天重关联与进程重启期限（6.2验收）

- Backend装配将已有UTC时钟注入原附件owner，默认仍是系统UTC，无测试开关或第二计时实现。真实Gateway/Backend/model_stream经正式消息与图执行生成关联，SIGKILL后由启动owner处理漏detach；第29天同URI再次执行重置期限，新代零关联重新起算30天，第58天重启不延长，第59天精确回收，普通文件入口转404，canonical原文保持且再重启不复活目录。
- 固定候选新场景1 passed/123.73秒，原runtime生命周期8 passed/67.35秒、GC前后真实进程退出2 passed/43.54秒。源码、准确命令、raw和退出码在`out/tests/temp/attachment_retention_restart_20261025/artifacts/`；Ruff首轮仅有未用import/排版，原失败保留。处理进程、fork竞争与硬重启故障组合继续据各正式suite核验，不能用单项通过代替全部6.2。
- 该片不追加团队/成员/委派/协作实现或专项验证；整个operation终止核实、同步阻塞、Session删除和shutdown总预算仍按5.2/5.4保留TODO。

- Linux 六组生命周期正式套件合计34 passed；最终装具修改后runtime 8 passed/71.03秒、GC 2 passed、startup/navigation 10 passed/31.36秒，Shell新增PID证据1 passed。共享树新重启场景1 passed/114.82秒，最终runtime 8 passed/65.00秒、startup/navigation 10 passed/25.54秒，相关Ruff及compileall通过。完整旧生产反例在第二次零关联起点断言失败：旧实现未消费外置UTC时钟。
- Windows `hyf_win` 已核对快照：`c51e5584feddc42a9375cdf3fa7b0c764f2d8914` 的新增重启场景通过；所在组合命令后续因runtime装具缺少个人Provider凭据而退出1，不能算整组通过。Shell组合10 passed/1 skipped后在另一用例的PTY PID解析失败；修正为目标程序落盘ASCII PID并保留输出标记后，原持久化故障场景与真实硬重启恢复2 passed/113.06秒。测试配置使用显式离线凭据，fork装具的读写明确UTF-8，独立图装具显式关闭附件与workspace存储；所有原业务断言保留。
- 后续快照`b714b06d162c72ce598827e9775f28e410694b7d`暴露未收口边界：runtime与GC各首项通过，但下一项重建工作区仍受原控制库/存储句柄占用；fork为2 passed、1 failed、8 errors，包含真实child fork失败回滚对长路径直接rmtree的WinError 3，以及缓存ThreadCreationService持有的session-control.sqlite阻止重建。TODO：在原Thread创建工厂/生命周期owner明确控制库关闭，并迁齐child创建/失败清理的原生长路径I/O，再复验Windows故障组合；不扫描/强关未知连接，不改历史/身份，也不把装具失败当成产品反例。6.2保持未完成，5.2/5.4继续独立推进。

### 2026-10-25 Windows child fork回滚长路径（6.2部分收口，停止边界）

- 原Thread创建owner的三处目录删除统一复用`native_io_path`，保留冻结locator、完整归属核验和原fsync顺序。产品回滚断言提取为共用helper，新增单独的child回滚入口；原完整失败场景的来源删除、30天GC及全部断言仍保留，没有因Windows句柄占用而删减验收。
- Windows快照`dd2ca4fc8d53f169e527e7cbe781f2cbe421fdae`的正式定向suite为1 passed/24.15秒，核对child/capture/claim均aborted、staging/final不存在且原附件关联不变。Linux原完整fork生命周期11 passed/118.28秒，定向staging/abort回归10 passed；共享树定向产品入口1 passed/19.99秒及相同10项回归通过。准确命令、完整源码指纹、原始输出及退出码位于`out/tests/temp/attachment_child_rollback_20261025/artifacts/`。
- 首次Windows完整场景已越过全部回滚断言，后续来源Session隔离rename因控制连接占用返回WinError 5，原始失败保留；此前WinError 3反例保留于`attachment_retention_restart_20261025`。TODO：控制连接生命周期及Windows完整故障组合。5.2/5.4/6.2继续未勾选，整体20/23；按用户要求完成此修复提交后停止，不扩展团队、成员、委派或协作实现/专项验证。


### 2026-10-25 Thread 控制连接生命周期（恢复推进，6.2部分收口）

- 删除 factory 的长期 LRU service/连接缓存，改为显式操作作用域；协议与所有调用（含嵌入子进程代码）迁齐，测试查询不跨物理 Session 删除，异常/取消/构造失败关闭同一原连接。工厂保留原 gate 与当前作用域 publication 接线，不新增持久状态。
- 工厂9项及绑定/创建故障共44 passed，Linux正式附件fork生命周期11 passed，取消/冷恢复8 passed；8项中6项为构造checkpoint的局部恢复，2项真实执行生成来源。Windows快照 `3aacd348f29229dea876b25a6c3086722586e4b0` 为9 passed/1 failed/1 error，已越过原来源删除WinError 5，余项分别是附件capture普通mkdir的WinError 206与目标装具catalog未关闭的WinError 32，仍须修复复验。证据在 `out/tests/temp/thread_control_lifetime_20261026/artifacts/`。
- Linux扩展snapshot竞争初轮退出143；加首错诊断后确认原手填来源缺冻结队列输入，完整HEAD archive同样失败。保留生产拒绝及原断言，在旧装具登记TODO；新连接suite只登记本片取消/恢复边界。API代表用例另在完整HEAD复现既存workspace identity不一致；扩大Ruff的4项import错误均在未修改文件，本片全部changed Python检查通过。原失败原件保留，不算通过。
- AST核对无未迁移factory调用（含可解析嵌入脚本），除工厂新测试外原测试assert数量保持；结构自查未发现本片新增领域或导入边，既有超大fork测试文件及超20文件目录的维护风险保留。团队、成员、委派、协作只机械迁移连接用法，不追加功能或专项验证。5.2/5.4/6.2仍未完成，整体20/23，继续原goal。

### 2026-10-25 Windows 完整附件 fork 生命周期（6.2 部分收口）

- 附件 capture 的清单、复制、保护/删除与 Thread 创建的 staging、复验、rename、发布/回滚统一在原 I/O 边界复用 `native_io_path`；普通输入路径和冻结 locator/hash 不变。远端装具显式关闭附件及工作区活动连接后再关闭 registry，原全部断言保留。
- Windows 快照 `e3f5e5ffa1718f6cb860c5b1a779110a25d6d76f` 完整生命周期 11 passed/170.83秒，Linux 相同产品源码 11 passed/108.85秒；新增长路径冷恢复 Windows 2 passed/0.95秒，Linux 通用 Thread 创建 77 passed/4.42秒（11项团队相关未运行），目录回归 Linux/Windows 各24 passed。前批 Windows runtime 8 passed/98.51秒、GC重启2 passed/62.59秒的依赖未改变，复用其原始证据，不冒称本轮重跑。
- Ruff/compileall 通过，AST核对原断言数量保持；首轮新测试插入位置错误、跨端 child staging 的 WinError206 及 watchdog 父进程核对失败均保留原件。准确命令、候选指纹、原始退出码与范围在 `out/tests/temp/attachment_capture_native_io_20261025/artifacts/`。watchdog 由下述切片补齐；完整旧代操作接管、同步阻塞和关闭故障组合仍未完成，5.2/5.4/6.2保持未勾，整体20/23。

### 2026-10-25 Windows fork watchdog 原生进程树保护

- 原父链校验只额外接受经解释器、参数与直接归属核对的一层 venv launcher，保留 matrix 祖先及启动标记检查；控制 reader 读原 FileIO，避免错误退出时的 BufferedReader daemon 锁崩溃。正式 `integration-fork-watchdog-process-tree`（60秒/4GiB）覆盖到期、错误控制消息和正常结清，检查本例进程树真实停止、无关进程存活及原始错误；不把基础设施测试计作产品 fork 验收，生产单次 fork 60秒保持。
- Linux 三场景3 passed/2.83秒；Windows 最终快照 `3cbac8c10675dfca91921916f46e478adad6869f` 合并运行13项单测和3项真实进程场景为16 passed/1 skipped/4.06秒，跳过项为 Linux `/proc` 专项。Linux 最终单测14 passed/0.27秒。原 Windows 反例两场景均复现 BufferedReader 锁崩溃，到期场景另复现父链拒绝；原始失败、准确命令、源码前后指纹及退出码在 `out/tests/temp/fork_watchdog_windows_20261025/artifacts/`。
- Windows 补跑曾因 POSIX 单测未显式选择平台而挂起，外置90秒保护退出后按精确PID/启动时间/命令回收该次测试树；另一模拟macOS用例补齐其平台常量。保留两次原失败，不修改生产行为或放宽断言。Ruff/编译与作者结构自查另在同目录留证。5.2/5.4/6.2继续未勾，整体20/23；团队、成员、委派与协作不追加实现或专项验证。

### 2026-10-25 子树删除单次总预算与导航回执（5.4部分收口）

- 原删除owner在同key锁前取得一次30秒，跨mark、全部Session的drain及finish共用；到期保留原journal/locator与原runtime任务，底层超时和取消保持身份。新增明确超时类型供原导航executor区分：mark前拒绝并写`delete_wait_timeout`，mark后保持committed/repair_required；不令worker失败，不增加另一条删除链。
- Linux相关单测118 passed/42.47秒（含原删除及导航回归），真实API集成8 passed/22.22秒，正式执行矩阵7 passed/86.55秒、运行器退出0。覆盖7类等待、跨阶段剩余预算、原错误身份、上传/执行生成的原历史/关联/runtime保全、peer删除及同key恢复；原runtime超时通过单独延长测试外层预算来保留精确旧断言，默认产品预算仍30秒。Windows快照`0a9371d5e08ecf69f871333416a0d1cdcf38363f`经原Job Object保护运行，原生锁/删除回归与API共69 passed/40.82秒，未将其称为完整Windows执行或关闭验收。
- 完整旧HEAD归档仅叠本片验收测试，8项期限检查失败、2项原错误身份检查通过，证明旧链缺少总预算；扩大到旧产品场景的45秒外置检查退出124且无测试结果，不作为业务反例。首轮新装具重复绑定回调被原规则拒绝，修正为一次绑定；首轮产品suite在7 passed/85.85秒后仍因启动/退出开销触及90秒，真实退出3/TIMEOUT保留，依据新增场景测量调整suite至120秒后通过，未放宽产品期限。
- 准确命令、源码前后指纹、原始输出与退出码位于`out/tests/temp/session_delete_budget_20261025/artifacts/`；Ruff/compileall与作者AST/目录自查通过，无新增源码目录或执行链。此时Job准备窗口尚未覆盖，已由下述切片补齐；同步阻塞/线程池、不合作取消及外部终止继续TODO。5.2/5.4/6.2仍未勾，整体20/23，团队协作继续暂缓。

### 2026-10-25 Job准备窗口与物理删除共用截止（5.4部分收口）

- 导航删除入口冻结一次30秒绝对截止，Job调度锁/已接纳准备窗口与原子树删除owner顺序消费同一值；直接进入子树owner仍由原入口取得默认截止，过期输入在同步mark/journal之前拒绝。原Condition物理替换为同锁检查计数、锁外等待的Event，避免取消时重新取调度锁而挂住；失败只同步归还本调用准入标记，原准备task、计数、Job队列、catalog与原journal仍由原owner持有。协议与全部直接调用、fake及测试迁齐，无第二删除链。
- Linux Job单测116 passed/39.07秒，core/导航单测119 passed/43.12秒，API集成10 passed/26.33秒，真实执行矩阵7 passed/87.87秒（runner退出0）。API覆盖准备等待到期且dispatch lock仍被持有，以及准备消耗20秒后Session gate只剩原预算、重启沿原journal结清；保持所有身份和回执断言。完整旧HEAD仅叠新API测试，两项均因期限后等不到持久回执而失败。初轮合并单测在60秒退出124，无完整结论，随后按上述两组独立完成。
- Windows快照`0e9af1a516f7787214ec157c81c41b731c001c12`经原Job Object的90秒/4GiB保护，准备窗口、core期限和API共30 passed/32.44秒。同步后宿主唯一Python变化为未选中旧fixture的TODO注释，AST核对与快照完全一致；不因此重跑相同场景。扩展Linux准入回归为24 passed/2 failed；两个失败均在删除前因旧fixture的catalog/消息owner工作区身份不同而拒绝，在完整旧HEAD原样复现2 failed/4.68秒。保留身份校验与原断言，fixture内登记TODO，不把它们计为通过或扩大产品修复。
- Pyright 1.1.414对5个产品文件报告12项错误；同版本检查完整旧HEAD所得文件、规则与完整消息逐项相同，仅行号变化，无本片新增诊断，不声称类型检查全绿。Ruff/compileall、全仓调用AST与作者结构自查另留原件；无新增源码目录，既有超大文件/超20文件目录仍是维护风险。准确命令、源码前后指纹、原始输出、退出码和失败归因在`out/tests/temp/delete_preparation_budget_20261025/artifacts/`；ASGI不能代替真实网络/Gateway验收，Windows未重跑全部执行/关闭组合。同步阻塞、线程池、不合作取消及外部终止仍TODO，5.2/5.4/6.2仍未勾，整体20/23；团队、成员、委派及协作不追加实现或专项验证。

### 2026-10-25 资源operation收尾总预算（5.4部分收口）

- Terminal/Browser沿唯一`ResourceOperations.close()`全批共用一次30秒协作式等待预算，原资源锁、账本与released语义保持。到期排空本次锁等待，精确未释放引用保留给同operation显式重试；之前返回的账本释放错误与预算错误按原顺序保留，底层TimeoutError和调用方取消不误归为预算。解锁不自动补做释放，不改变外部进程/页面终态；同步写盘与不合作取消另留代码TODO。
- Linux新旧资源owner、锁隔离和账本回归168 passed/4.38秒；正式`integration-resource-release`4 passed/35.17秒，保留原两项整组释放并增加两类真实资源的收尾期限。产品Session/创建/operation与真实Node/PTY或Chromium/HTTP生成占用后，仅持原资源锁并推进本进程单调时间，核健康占用先释放、原lease冷读仍active、原进程/页面身份及版本不变和原operation显式重试。完整`94e4ecb52`归档只叠相同最终测试，单测4 failed/2 passed/4.87秒，产品2 failed/2 passed/48.02秒；新增期限场景均在截止后得不到产品返回，原底层超时身份与原产品场景仍通过，不将测试自己的观察超时当作产品响应。
- Windows快照`d80699e1b6bcaa1f29bd3e90ea6a28dadc47bbdc`经原90秒/4GiB Job Object保护，新期限、锁隔离和账本共100 passed/8.48秒；未重跑Windows真实PTY/Chromium。生产文件Pyright 1.1.414零诊断，Ruff/compileall、作者AST/目录审查和OpenSpec strict另留原始证据。准确命令、源码前后指纹、真实退出码及旧版反例在`out/tests/temp/resource_operation_close_budget_20261025/artifacts/`；现有suite仍120秒/4GiB，不扩大产品期限。全业务operation执行期限、同步阻塞、旧代接管及外部终止继续TODO，5.2/5.4/6.2仍未勾，整体20/23，团队、成员、委派及协作继续暂缓。

### 2026-10-25 删除准入验收的工作区身份与观察连接

- 修复前述旧fixture身份TODO：沿统一空工作区准备入口取得真实workspace_id，再传入catalog装具；catalog、消息与后台owner使用同一身份，不放宽生产身份校验。测试观察canonical终态后显式关闭SQLite连接，避免Windows目录隔离被观察者自身阻挡。生产代码无改动，原512/513节点规模、准入、顺序、终态及失败断言保持。
- Linux扩展三文件26 passed/31.17秒；最终观察连接修正后补跑两个相关场景2 passed/6.35秒。Windows首次扩展22 passed/4 failed：一个是测试观察连接占用，另外三个是未修改的Node调试配置原生长路径写入失败。最终快照`60e479026a11c93ee97f347817dbb37f5e1a0aae`的删除准入全文件6 passed/44.28秒；另外三个产品失败留待独立切片，不计为通过。
- Ruff、compileall及作者AST自查通过，准确命令、源码指纹、raw与退出码在`out/tests/temp/delete_admission_fixture_20261025/artifacts/`。Windows沿原90秒/4GiB Job Object保护；ASGI及进程内验收不代替真实网络、Gateway或完整关闭组合。5.2/5.4/6.2仍未勾，团队与协作继续暂缓。

### 2026-10-25 单次资源管理命令总等待预算（5.4部分收口）

- 原`WorkspaceResourceOwner.command`入口一次30秒覆盖冷恢复、Thread准入、资源锁、撤权及回执；未派发不补造unknown/管理lease，已提交的冷恢复事实保留，派发未核实仍沿原异常链保留unknown和精确占用，底层TimeoutError及调用方取消保持归属。32个其它方法AST不变，去除本次预算上下文后原command AST一致；不新增命令执行链、账本或schema。同步阻塞与不合作取消保留代码TODO，5.2/5.4/6.2仍未勾，整体20/23。
- Linux新定向16 passed/0.63秒；完整`428a50855`归档的相同单测8 failed/8 passed/9.34秒，旧实现的派发前总截止及跨恢复/命令预算场景均不能返回产品期限错误，原取消和底层超时场景保持。相关owner/API回归289 passed/10.25秒，包含上述16项，不相加。Windows快照`da3c38e9e789c4a0df1465c48ae24fe0a54f78ab`同16项通过/1.49秒，后续仅集成装具调整，生产与单测未变；既有开发服务未重启。
- 正式`integration-resource-release`完整8 passed/69.21秒，原4项与新增4项共用原120秒/4GiB保护。正式Session和ASGI资源创建/管理入口生成真实Node/PTY或Chromium/HTTP状态，核资源锁等待到期不改原身份/版本、peer继续可用、停止已完成但回执未返回时HTTP503与原request_id、冷账本精确lease保留及新管理请求只核终态不重发停止。总预算跨冷恢复的20+11秒由单测推进单调时钟验证；真实服务场景只推进本次原截止，不称真实等待30秒、Gateway/Web/Windows真实辅助服务或任意执行的强制终止验收。
- 首轮单测fixture在事件循环启动前取时钟产生14 errors，改为异步fixture后通过；首轮产品组合在失败后达120秒，缩小首项得到原锁收尾失败。原故障注入未限定调用方且推进全局时钟，现仅在具名命令的真实调用边界持锁/暂停回执并推进其截止，未改生产来适配测试，失败原件均保留。Ruff、最终compileall及OpenSpec strict 45/0通过；Pyright 1.1.414的4项诊断在完整旧生产逐条复现，仅行号不同，无新增诊断，不称类型检查全绿。准确命令、源码前后指纹、原始输出/实际退出码、完整旧生产及作者自查在`out/tests/temp/resource_command_deadline_20261025/artifacts/`；团队协作与CIL第9节继续暂缓。

### 2026-10-25 Windows 终止工具异步等待（5.4 部分收口）

- 原进程树终止owner移除`spawnSync`，复用已有`execFileAsync`并迁齐唯一Windows调用点及系统边界替身；保留参数、1500毫秒工具上限与后续退出核实。启动、退出码、信号、stderr与原cause保留；超时杀掉工具后即使根已消失仍报告失败，不将工具回执视为停止证明。
- Linux相关Node回归70 passed/4.72秒，包含迁齐原10项及新增超时/并发HTTP的13项Windows边界测试；Linux真实session/僵尸/过期身份4 passed/5.24秒。Windows快照`fc3961e8fce6177af32f91d3379fcb7d60ea3f64`同13项通过/4.78秒，原生受控父子进程经正式终止入口1 passed/0.58秒，分别核根与子进程退出。边界替身在回执未放行时完成真实loopback HTTP请求，不冒称真实taskkill并发验收；原生父子进程不代替完整PTY/Gateway/Web或所有后代场景。
- Node静态语法、diff检查与OpenSpec strict 45/0通过；每次运行均有进程外期限/4GiB保护及前后源码指纹，原始命令、输出、实际退出码和作者自查在`out/tests/temp/terminal_async_taskkill_20261025/artifacts/`。Windows既有开发服务前后均保持可用，未重启。全operation/shutdown期限、其它同步存储及不合作任务终止仍TODO，5.2/5.4/6.2保持未勾、整体20/23；团队与CIL第9节继续暂缓，原4份CIL规划排除本提交。

### 2026-10-25 单次领域列表总等待预算（5.4 部分收口）

- 原共享owner的`list_resources`入口一次30秒覆盖准备、所选资源恢复、唯一外部清单及逐资源锁，保持原过滤、完整记录冲突校验与持久观察链；到期仅排空本次等待，已提交观察保留，不补造unknown/lease，不后台重试。原传输TimeoutError与调用方取消保持身份。作者AST核对32个其它方法不变，去除预算上下文后原列表函数体一致；无新存储、schema或执行链。
- Linux最终列表32 passed/1.17秒；相关十文件先319 passed/2 failed，后两项是新增前项保留装具在冷恢复前持第二资源锁，尚未到列表合并，现改为原外部清单取得后占锁，最终列表全文件32项通过。其它319项源码未变，复用原回归，不将两个计数相加。完整旧生产`270a0d7c2`归档中10个有效期限反例失败，另外两项相同装具准备失败明确排除；最终装具仅改变该函数，十个旧期限反例AST仍与最终一致。另一次误写API测试路径导致零用例收集的失败原件保留，改为既有两个API文件后才取得上述回归。
- 正式`integration-resource-release`完整10 passed/87.09秒，继续原120秒/4GiB保护；包含原8项及新增Terminal/Browser列表2项。正式Session和ASGI创建/列表生成真实Node/PTY或Chromium/HTTP，暂停已取得的清单回执并推进原截止，核HTTP503/request_id、peer可读、记录/lease逐字保留、原进程/页面身份和版本不变及正式重试成功；不代替真实30秒等待、独立进程重启、Gateway/Web或跨领域聚合API总预算。Windows快照`350fdf93b6e3cdedeb9655273e1430c153ece89d`同32项通过/2.79秒，原开发服务未重启。
- Ruff、compileall及OpenSpec strict原件见`out/tests/temp/resource_list_deadline_20261025/artifacts/`，全部运行有外置保护、源码前后指纹、原始输出和真实退出码。Pyright 1.1.414按项目解释器报告4项旧诊断，与完整旧生产的规则/消息逐项一致；首次未指定解释器的额外Python版本诊断单列保留，不声称类型全绿。跨领域聚合API、同步存储、不合作取消和全operation/shutdown仍TODO，5.2/5.4/6.2继续未勾、整体20/23；团队及CIL第9节仍暂缓，原4份CIL规划排除提交。


### 2026-10-25 公共资源列表跨领域共用总预算（5.4 部分收口）

- 原Workspace资源HTTP列表在Terminal/Browser串行调用外共用一次30秒协作式预算，保留顺序、过滤、领域owner、DTO及错误映射。到期排空当前等待，已提交前项观察、原资源和精确lease保留；底层TimeoutError及调用方取消保持归属，不返回部分清单或后台补发。AST核对另外9个函数及全部签名不变，去除预算上下文后原列表函数AST一致，无新owner、schema或聚合状态。
- Linux三个API单测文件74 passed/4.82秒；正式integration-resource-release完整11 passed/91.64秒，仍用原120秒/4GiB保护。新增场景沿正式Session、ASGI资源创建/列表生成真实Node/PTY/Chromium/HTTP状态，推进同一HTTP截止累计20+11秒，核503/request_id、前项记录和共享lease原字节、等待已排空、peer可读、原外部身份/版本及同入口重试顺序。完整旧生产80eed06be只叠相同最终新测试，单测3 failed/4 deselected，产品1 failed/10 deselected；旧API在测试观察窗口内未返回，不能将该观察超时当产品503。
- Windows快照be56bed9216788a3004977a3cbe79d4ac554c27d经原90秒/4GiB Job Object保护，同74项通过/6.99秒。初轮65 passed/9 failed均发生在旧测试默认GBK读取UTF-8账本，明确两处取样编码后Linux/Windows复验通过，全部原断言保持，生产存储编码未变。既有开发服务未重启；不把Windows API单测或Linux ASGI/真实辅助服务称为Gateway/Web、Windows原生PTY或真实等待30秒验收。
- Ruff、compileall、diff检查及OpenSpec strict 45/0通过，作者AST/目录自查未发现本片新增结构问题；Windows末次status仍为Web/Gateway可用。准确命令、源码前后指纹、原始输出/退出码及完整旧生产归档保留在out/tests/temp/resource_aggregate_deadline_20261025/artifacts/；纯临时解包树已封存业务原始输出后删除。Session资源展示投影、同步存储/序列化、不合作取消及全operation/shutdown继续TODO，5.2/5.4/6.2仍未勾，整体20/23；团队及CIL第9节继续暂缓，原4份CIL规划排除提交。


### 2026-10-25 资源详情的单次总等待预算（5.4 部分收口）

- 原共享owner的get_resource从入口取得一次30秒，冷恢复、原资源锁和外部清单共用；原快照校验、观察、持久化及投影保持。到期排空本次等待，已提交冷恢复及原资源/占用保留；详情读取自身不补造unknown或新lease，不释放其它调用的锁，不后台补做。原传输TimeoutError与调用方取消保持归属。另32个方法AST不变，去除预算上下文及期限错误出口后原详情函数体一致，无新owner、schema或存储链。
- Linux相关11文件334 passed/10.86秒，包含新16项，不与其单独运行16 passed相加。新增真实双辅助服务suite四场景4 passed/33.28秒，原integration-resource-release完整11 passed/92.79秒；两个suite分别沿90秒和原120秒/4GiB保护。新场景经正式Session、ASGI创建/详情及原工具operation生成真实Node/PTY或Chromium/HTTP和active lease，核锁/回执到期HTTP503与request_id、peer可读、原记录/lease字节、冷账本精确占用、外部身份/版本及原入口重试；只推进本次截止，不称真实等待30秒或Gateway/Web验收。跨真实冷恢复与查询的20+11秒共享预算由单测验证。
- 完整旧生产149fa551e只叠相同最终测试，单测8 failed/8 passed（3.80秒），产品4 failed（40.15秒）；旧详情在测试观察窗口内未返回，不能把该观察TimeoutError当产品期限错误或503。Windows快照f455c0ca1fee639d7ca8ab100f6c1c5c38a061ec经原90秒/4GiB Job Object保护，新16项及原两份API共83 passed/7.70秒；远端tracked干净，4份源码按LF归一后与宿主一致，原服务未重启，不声称Windows真实PTY/Chromium已重跑。
- Ruff/compileall通过；首轮仅新集成文件import格式不符合Ruff，调整排版后AST不变，复用原产品行为结果。Pyright 1.1.414的4项旧诊断与完整旧生产的规则、消息和对应代码逐项相同，不称类型检查全绿。准确命令、源码前后指纹、原始输出/真实退出码、作者结构自查与完整旧生产压缩归档在out/tests/temp/resource_detail_deadline_20261025/artifacts/；纯临时解包树已在封存业务产物及核对全部源码后删除。同步存储/线程池、不合作取消、完整operation/shutdown与旧代接管仍TODO，5.2/5.4/6.2保持未勾，整体20/23；团队与CIL第9节仍暂缓，原4份CIL规划排除提交。

### 2026-10-25 Browser 失败回执的原占用保护（5.2 部分收口）

- 工具和管理动作的成功/正式失败回执复用同一核验、提交与投影链，严格核本次整数版本、资源/工作区身份及核实状态；失败处理中的校验、记录、账本或投影异常不再绕过unknown而普通释放精确原lease。处理异常保留原Browser错误cause；未知页面转换继续返回原HTTP错误并保留待核实占用，完整已核实失败保持原异常及正常释放语义。没有新账本、状态表或重放链。
- Linux新旧owner/API回归337 passed/11.10秒（含新52项）；正式新suite四场景4 passed/23.46秒，原版本撤销/关闭8 passed/61.55秒，原真实HTTP取消/回执54 passed/5.24秒。新场景由正式Session、ASGI创建/管理与原工具operation生成真实Chromium/HTTP动作，先产生页面副作用再失败，仅注入回执版本或首次记录提交故障；核原页面/版本、冷账本精确占用、普通查询不结清，以及原管理停止verified后才settle。完整旧HEAD归档仅叠相同最终新测试/矩阵，单测28 failed/26 passed、产品4 failed；旧失败包括接受错误版本、未知占用已普通释放与缺失处理错误cause，原正式未知转换HTTP合同仍通过。
- Windows快照`f0e7a1569194f80fa116697cb09125422b5e2bcf`沿原90秒/4GiB Job Object保护，新52项、原Browser owner和API共129 passed/12.37秒；tracked干净，4份相关Python归一LF后与本地相同，原Web/Gateway保持可用，未重启。初轮旧owner测试默认GBK读取UTF-8账本失败，现只显式指定原UTF-8格式，原断言逐字保持；Linux单项补验通过。未重跑Windows真实Chromium、Gateway/主Web或独立Backend重启。
- 首轮扩大回归引用不存在的API文件，随后更正命令；第二轮发现本次核验把原未知页面转换500改成409，已修复产品错误归属且原两项API断言保持。全部失败原件、准确命令、前后源码指纹、真实退出码、Ruff/compileall、生产文件Pyright 1.1.414零诊断及作者AST/目录自查位于`out/tests/temp/browser_operation_receipts_20261025/artifacts/`。完整旧代操作接管、同步阻塞、不合作取消和全operation/shutdown仍TODO，5.2/5.4/6.2继续未勾、整体20/23；团队及CIL第9节继续暂缓，原4份CIL规划排除本提交。


### 2026-10-25 Terminal 普通操作与工作区完成的回执核验（5.2 部分收口）

- 原Terminal工具查询、读写、后台和steering调用，与workspace命令完成查询共用原资源锁内的唯一回执链；严格核本次整数版本、资源/工作区及verified，再提交记录、同步账本和投影。错误或未知回执不再成功或普通释放本次精确lease；真实终态完成查询会提交停止事实并结清受管cwd占用，保留workspace记录与关联。没有新账本、重放链或schema。
- Linux新旧owner/API/工具/steering/恢复回归336 passed/8.38秒，含新64项；最终正式新suite 7 passed/59.00秒，原附件Shell suite 12 passed/114.50秒。正式Session、上传、创建及原工具operation生成真实Node/PTY/HTTP，读、真实写入与完成的stale/unknown回执均保留冷账本精确占用和到期目录，原管理停止verified后才settle/GC。独立终态场景暂停steering轮询，由完成查询独占提交已真实停止的PTY事实；不代替Gateway/Web或独立Backend重启。
- Windows原生新suite 7 passed/69.50秒，继续90秒/4GiB保护；新旧owner/API单测122 passed/11.57秒。最终产品快照`ac49a9901b1570c7bc753cb302fde2755fe5d541`的四份Python归一LF指纹与Linux逐项一致、tracked clean；原Web/Gateway前后可用，未重启。首轮文件锁路径261字符而在准备阶段7 errors，去掉新文件名中重复terminal前缀后进入业务；第二轮2 failed/5 passed并触发套件期限，写入探针的换行未提交Windows控制台命令。现改为与产品一致的回车输入，原业务断言保持，未改系统配置或放宽期限。旧owner测试只明确UTF-8读取账本，原断言不变。
- 完整旧HEAD `e1d28ed604a2e9e4437cc9e6469d98f25424a089`归档仅叠相同最终新测试/矩阵：新单测27 failed/37 passed，真实产品7 failed/58.07秒，分别复现错误回执未拒绝、原占用普通释放及外部已停止却返回running。4427份原文件与archive一致，最终新测试逐字核对；首轮PID类型、同名suite两树端口争用和错误matrix参数的设施失败原件单独保留，不充作产品反例。
- Ruff和compileall通过；Pyright 1.1.414的5项Terminal专用传输方法诊断在完整旧生产规则/消息逐项相同，不称类型全绿。作者AST自查确认另外9个方法及全部原签名不变，新目录含四节AGENTS；准确命令、源码前后指纹、原始输出/真实退出码、Windows收据与完整旧生产归档见`out/tests/temp/terminal_operation_receipts_20261025/artifacts/verification-056.json`。完整旧代接管、同步阻塞、不合作取消和全operation/shutdown仍TODO，5.2/5.4/6.2继续未勾、整体20/23；团队及CIL第9节继续暂缓，原4份CIL规划排除本提交。


### 2026-10-25 传输授权的单次总等待预算（5.4 部分收口）

- 原`transport_grant`入口共用一次30秒协作式预算，覆盖终态预检锁、冷恢复、Thread准入、签发锁、运行观察及真实授权回执。保留原终态零网络拒绝、`_grant`的版本/关联/占用和唯一签发链；派发前到期无新lease/unknown，派发未核实保留unknown及精确原占用，已提交恢复不回滚，放行回执不后台补签。底层TimeoutError与调用方取消保持原归属，AST去掉预算后原函数体一致，模块其余节点/签名/import均不变；无新writer、schema或token账本。
- Linux相关owner/恢复/锁/列表/释放/命令/详情及两类API回归347 passed/10.99秒，包含新30项，不与单独30 passed相加。新测试覆盖两个锁、恢复、Thread准入、运行快照及签发回执的到期/取消、peer可用、冷账本精确lease及新管理停止settle；冷恢复同步先消耗20秒、签发等待再消耗11秒，原冻结版本不重推、旧授权不重发。
- 正式新suite Linux 4 passed/26.79秒，Windows 4 passed/51.71秒，均沿90秒/4GiB保护。正式Session/ASGI创建与授权生成真实Node/PTY或Chromium/HTTP，持原锁或暂停已取得的真实签发回执，只推进本次截止，核503/request_id、peer授权、外部原身份/版本、精确占用冷读、存活查询不结清及原管理停止核实后settle/拒绝新授权；派发前到期沿原入口重试。Windows新旧锁/授权/API回归157 passed/16.98秒，快照`dcca03c8c522f1bdf9b33bef066b4ce13842ceec`的三份Python及matrix归一LF指纹与Linux一致、tracked clean，四份具名产品收据已取回；原Web/Gateway前后可用，未重启。诊断产物不保存临时grant token。
- 完整旧HEAD `8548eefbfe073e72039814ef99536a2d7ef87781`归档仅叠相同最终新测试/矩阵：新单测14 failed/16 passed，正式产品4 failed/28.91秒，因原授权调用无法按入口期限返回；已派发场景在测试取消后保留unknown并使原scope清理拒绝，首因超时上下文原样保留。4430份原文件及最终测试逐字核对，4份原PTY状态均exited且PID已不存在，封存原始产品产物后删除纯临时解包树。首轮在归档尚未解完时提前启动的两项对照未找到文件，未执行测试；等待原准备handle完成后顺序重跑，失败原件保留。
- Ruff/compileall通过；Pyright 1.1.414的4项原诊断与完整旧生产的规则/消息/位置逐项相同，不称类型全绿。新测试按生产目录下沉，含四节AGENTS，原owner的976→988行已有大文件由作者AST/职责审查记录本片边界，无新跨域职责。准确命令、前后源码指纹、原始输出/真实退出码、旧生产归档、Windows收据及作者自查见`out/tests/temp/resource_grant_deadline_20261025/artifacts/verification-030.json`。真实30秒等待、Gateway/Web、独立Backend重启、同步阻塞、不合作取消及完整operation/shutdown继续TODO；5.2/5.4/6.2仍未勾、整体20/23，团队及CIL第9节继续暂缓，原4份CIL规划排除提交。

### 2026-10-26 Backend 硬重启与处理进程目录保活（6.2 部分收口）

- 新增正式 `integration-attachment-process-retention-restart`（240秒/4GiB），复用原 Gateway/Backend、model_stream 和 PTY 装具，经公开消息及真实 exec_command 启动父子进程；四次 Backend 原生硬退出覆盖漏 detach、精确30天到期仍由原进程占用保活、公开管理停止核实父子进程终态后回收，以及再次重启幂等。关联、期限、lease、目录和 canonical 均只读核对，不手填历史、不由测试杀处理进程或结算占用。Linux 1 passed/93.37秒，Windows 1 passed/110.89秒，原身份、locator、期限与历史字节保持。
- Windows 首轮新用例名导致装具产物路径超限，缩短名称后进入产品；随后首次模型请求的“连接错误”经同路径原始探针确认是回放产物临时文件越过 MAX_PATH。唯一原子写入入口复用现有 `native_io_path`，不改变协议、模型响应或逻辑身份；同路径探针由失败转为成功。新增正文258/320字符边界回归，Linux/Windows 的 model_stream 单测均59 passed（1.55/2.44秒）；Windows 最终快照 `355d3999a66c45b26a7d2a814c03890967fc3ae7`。两次准备/请求失败、初轮新单测遗漏必填 finish_reason 及补齐后的原始结果均保留，未放宽业务断言。
- Ruff/compileall通过；Pyright 1.1.414的新测试无诊断，回放模块3项原诊断与完整旧HEAD归档逐项一致，4434份原文件经archive校验，不称类型全绿。准确命令、源码指纹、原始输出/退出码、Windows收据、类型对照与作者结构审查位于 `out/tests/temp/attachment_process_retention_restart_20261026/artifacts/`。本片不证明未知回执挂起后硬重启、旧代操作接管、同步阻塞或不合作取消的终止；5.2/5.4/6.2保持未勾、整体20/23。团队及CIL第9节继续暂缓，四份CIL规划排除实现提交。

### 2026-10-26 工具资源准入等待预算（5.4 部分收口）

- 原`ResourceOperations._resource`统一Terminal/Browser单次方法的冷恢复、资源锁和lease校验，入口一次30秒协作式预算；未派发到期不造unknown/新占用，原异常及取消保留归属，已提交恢复及已有lease保留。准入后退出预算并继续持原锁覆盖真实工具执行，不称整个operation已有总期限。
- Linux共享owner定向回归122 passed（含新26项），真实PTY/Chromium正式组合2 passed/19.16秒，原Terminal/Browser工具回执7+4项通过。Windows最终快照`0a43ea16413909a3fb734390df946125bdb46bc1`的Terminal同用例1 passed/16.67秒；Browser创建被现有内存压力保护拒绝，实机32GiB内存load85%、可用4806770688字节，默认critical阈值0.85。TODO：资源压力解除后补Windows Browser验收，不改阈值或称Windows全绿。各测试均有进程外期限和4GiB保护。
- 完整旧HEAD归档叠最终新单测，10项超时行为失败、另2项缺少预算观察点失败、14通过；4435份归档原文件逐字核对。首轮红测在装具缺观察点时失败，已改为进入行为断言；真实组合早期对全部锁等待者的断言错误，后续又误取恢复gather为锁等待者，最终屏障置于真实恢复之后并核本次精确future取消，保留其它任务等待者，未改生产行为或放宽取消断言。原始失败均保留。
- Ruff、compileall和本片三个Python文件的Pyright通过；作者AST/目录自查无结构finding，不称独审或全仓类型全绿。准确命令、源码指纹、原始输出/退出码、Windows收据及失败归因在`out/tests/temp/resource_operation_admission_20261026/artifacts/`。5.2/5.4保持未完成，整体21/23；同步阻塞、不合作取消、Thread准入和整个operation/shutdown仍TODO，团队与CIL第9节继续暂缓，原4份CIL规划排除实现提交。

### 2026-10-26 Thread 短准入与资源操作隔离（5.4 部分收口）

- 原 `admit_thread` 在共用 namespace 的 Session shared OS holder 内，以一次30秒预算取得短 Session 写锁并核定 workspace/Thread，进入资源操作前释放 Session 锁；holder 保持到原 operation 收尾。Session 删除链只登记一次共用 holder，沿既有 lease/drain 清理，无第二份账本。同 Session 的独立资源可并行，逻辑删除提交后等待原操作退出再清理资源与隔离目录。
- Linux 相关回归399 passed，最终新单测20 passed，真实 PTY/Chromium 组合2 passed，原资源释放组合11 passed；Windows 新单测20 passed、真实 Terminal 组合1 passed。停止窗口 Backend 硬退出回归 Linux/Windows 各2 passed，核新增 holder 不遗留崩溃锁。Windows Browser 因85%内存压力前置条件未验证，未调整保护阈值。Windows 快照 `53a8e85a9b13af7edeeabc1149382816e45e95ed` 的五份 LF 源码与本地一致。
- 完整旧 HEAD 归档叠相同最终新单测，12 failed/8 passed，包含旧 Session 长期持锁阻塞 peer 的行为反例。Ruff、compileall、45项 OpenSpec strict 通过；两个新测试最终 Pyright 零诊断，生产7项既存诊断与旧 HEAD 逐项一致，不称全仓类型全绿。首次误用未安装的 `uv run pyright` 未启动检查，改用既有固定版本 `uvx` 后完成。准确命令、源码指纹、raw、退出码与限定证据见 `out/tests/temp/resource_thread_admission_20261026/artifacts/verification-031.json`；纯临时旧树与 tar 已删除。
- 本片不代表 Gateway/Web 完整验收或整个 operation/shutdown 总期限；同步阻塞、不合作取消及旧代接管仍 TODO，5.2/5.4保持未完成、整体21/23。四份 CIL 规划未改且排除本提交；随后按用户最新优先级转入 CIL 第9节，团队协作继续暂缓。

### 2026-10-28 Terminal原生创建后、就绪前失败收尾（5.2部分收口）

- 原worker在native创建返回时交付唯一PID，ready和正式启动失败证明必须匹配原身份；非法、重复及晚到消息不能覆盖身份。Session先保留unknown，再复用原进程树停止/worker退出/终态落盘链；只有实际返回停止成功才签failed回执，失败保留原句柄，created/unknown可经原管理kill重试。未取得PID的created明确拒绝伪报终止；无第二套停止实现、writer或schema。
- 最终Node定向97 passed/3.43秒；Linux新增真实HTTP/PTY父子进程2 passed/13.71秒，原信号关闭6 passed/60.64秒、进程树5 passed/13.70秒，均有进程外超时及4GiB保护。新用例只注入就绪交付错误和首次停止故障，父子忽略SIGTERM，独立核强杀/worker退出、原恢复记录、peer及同管理入口重试。完整旧HEAD e1adea8ae的4699份文件逐字核对后仅叠相同最终产品测试/故障边界/matrix，两项均因原os_pid丢失失败；原业务输出封存在out/tests/temp/terminal-startup-cleanup/artifacts/old-product-output，纯临时旧树与tar已删除。
- Ruff、新测试Pyright零诊断及Node语法检查通过。初轮新工作负载字符串误多加号导致SyntaxError，修正后真实场景通过；初轮Ruff未使用导入也已修复，原失败输出保留。准确命令、前后源码指纹、原始输出/退出码、作者结构自查及限定证据见out/tests/temp/terminal-startup-cleanup/artifacts/verification-001.json；不称Gateway/Web、Windows或独立Backend重启验收。native尚未交付PID、同步阻塞、不合作取消及整个shutdown期限继续TODO，5.2/5.4仍未勾，整体21/23；团队协作继续暂缓。

### 2026-10-28 冷恢复取消后的显式管理停止（5.2部分收口）

- 原stop/delete只准备清单与冻结版本，在原资源锁内先核版本、active lease及delete关联限制，再与普通恢复共用唯一锁内同步主体；停止意图不先等待依赖context关闭的旧执行。同步后重读原记录，真实停止、终态持久化和原lease结算仍沿原链；终态对账后重新读取当前lease，避免已settle的旧列表阻断后续delete。过期版本和有关联的delete不消耗pending或派发撤权，无新账本、schema、任务或停止实现。
- 最终共享owner/账本/等待预算定向413 passed；真实Session/ASGI、冷owner及Node/Chromium/HTTP组合10 passed/90.42秒，新增stop/delete由正式matrix独立补验2 passed/24.07秒（与10项重叠，不累计）。原真实PTY的unknown/read与管理停止1 passed，核原目录及精确lease结算。新matrix固定60秒/4GiB，原Browser suite的90秒及产品期限未改变。
- 原生产对新增停止/过期版本用例12项均失败，真实Browser的stop返回408而非200；完整HEAD `05bac2c1b`归档4706份文件逐字核对。扩大回归中的32项旧I/O断言也在该完整HEAD复现，现按已生效URI合同核公开安全错误和原生cause身份/顺序，保留全部状态及占用断言。作者差异审查发现的旧lease列表窗口另以2项失败反例确认后修复，原始失败、首次测试计数误用及筛选未启动均保留，未把它们计作通过。
- Ruff、compileall及OpenSpec strict 47/0通过；触达五文件Pyright基线96项、候选95项，按文件/规则/完整消息核新增0、减少1，新测试无诊断，不称类型全绿。AST核原恢复主体除显式停止意图外保持相同，作者结构自查、准确命令、源码指纹、raw/退出码及限定证据见 `out/tests/temp/resource-cold-management/artifacts/verification-001.json`。不代表Gateway/Web、Windows、Backend硬重启、未知native身份或任意脚本强制终止；同步阻塞、不合作取消及整个shutdown期限继续TODO，5.2/5.4保持未勾，团队协作继续暂缓。

### 2026-10-28 Catalog关闭的线程等待与准入（5.4部分收口）

- Backend复用原同步I/O原语执行唯一registry关闭链，线程池排队前同步撤销装配准入；服务图、catalog和registry作用域入口先拒绝已关闭状态，原锁内仍复核。线程排队、事务锁等待继续由同一关闭owner持有；观察预算到期或等待者取消不提前归还维护锁、不重复关闭，原执行结束后才继续后续依赖。未新增账本、schema或关闭实现。
- 原生产的新单元反例2项失败（排队装具另有5秒屏障超时，已改为15秒且保留原件）；真实catalog事务反例1项失败。仅异步关闭、未修准入的中间候选又复现工作区请求等待5秒，修复后经真实双挂载、产品Session、SQLite事务锁及ASGI请求核验即时拒绝、诊断可读、原owner/locator/维护互斥保留与迟到完成。相关单元/catalog回归14 passed，完整关闭matrix 8 passed/168.04秒（新场景与前14项重叠1项），启动失败3 passed，合计24个不同用例；模型沿用model_stream，未手填执行或恢复事实。
- Ruff、compileall及OpenSpec strict 51/0通过；Pyright原HEAD完整归档24项、最终候选24项，按文件/最窄AST owner/规则/完整消息比较无新增，不称类型全绿。原有集成用例AST保持，作者结构自查不等于独审。准确命令、源码指纹、原始输出/退出码及限定证据见 `out/tests/temp/uri-global-closure/artifacts/verification-026.json`。本片不证明真实等待30秒、Gateway/Web、Windows、独立Backend硬重启、原生线程强制终止或整个shutdown期限；其它同步关闭、未交付PID及不合作执行继续TODO，5.2/5.4仍未勾、整体21/23，团队协作继续暂缓。

### 2026-10-28 文件存储关闭的事务等待（5.4部分收口）

- 文件worker排空后，原Workspace任务复用`run_sync_shielded`等待唯一同步关闭，事务锁竞争不再阻塞事件循环；原步骤结束前保留catalog、locator与维护锁，不重复关闭或移动MCP scope。真实双挂载、产品Session及SQLite事务反例在原实现1 failed，候选1 passed；完整相关关闭回归17 passed/171.60秒，核对peer关闭、ASGI诊断/request_id、观察到期及原事务释放后的同owner收尾。观察期限由测试外置推进，不冒充真实等待30秒。
- Ruff、compileall与OpenSpec strict通过。完整旧HEAD归档4730个blob逐字核对，生产Pyright 4→4、测试17→17，无新增，不称类型全绿；原有测试函数AST保持。首次归档比对因Git中文路径转义而失败，改用NUL分隔核对后确认源码一致，未修改候选。准确命令、前后指纹、原始输出/退出码及作者结构自查见`out/tests/temp/uri-global-closure/artifacts/verification-028.json`。未验Gateway/Web、Windows、独立Backend硬重启、任意线程终止或整个shutdown硬期限；其它同步关闭、旧代接管与不合作取消继续TODO，5.2/5.4保持未勾，整体21/23，团队协作继续暂缓。

### 2026-10-28 已接受Job与FIFO发布边界（5.4部分收口）

- 原Job先保持accepted，新准入与冷恢复均在同一调度锁内恢复原FIFO后才发布queued；取消和排空先写canonical终态，只有queued删除已有条目。accepted同样刷新快照，关闭仅发一次取消事件；写盘失败保留原Job和准入供原入口重试。移除永远无法返回false的队列删除检查及其死错误构造器，严格FIFO缺项仍明确报错，不增加兼容回退或镜像插入链。
- 真实Session/持久任务API生成准入，再于原canonical outcome读取处注入装载失败；最终取消、Backend关闭、终态写入失败后原owner重试与正常模型恢复，加原队列故障组合共23 passed/175.62秒，核原Job/admission/hash、无重复执行、取消事件与快照。Job目录单元116 passed/39.45秒，相关删除3 passed/6.04秒。单元端口按原持久phase装载，修复恢复装具多一层workspace及每项残留问题，所有原业务断言和测试函数AST保持；完整旧HEAD归档4732个blob逐字核对，最终反例在旧生产取消/关闭2 failed，旧装具setup错误也独立复现。
- Ruff、compileall及OpenSpec strict 51/0通过；Pyright旧HEAD40、候选40，按文件/最窄AST owner/规则/完整消息无新增，新测试零诊断，不称类型全绿。首次OSError注入、测试上下文遗漏及160秒外层保护终止均保留原件且不计通过，最终320秒验证预算未改产品期限。命令、前后指纹、raw/退出码及作者结构自查见`out/tests/temp/uri-global-closure/artifacts/verification-032.json`；完整临时源码已删除，正式测试产物保留。不代表Gateway/Web、Windows、独立Backend硬重启、准备尚未提交的关闭竞争、同步阻塞可抢占或整个shutdown硬期限；5.2/5.4仍未勾，整体21/23，团队协作继续暂缓。

### 2026-10-29 消息准备与Backend关闭竞争（5.4部分收口）

- 真实发送停在上传前或canonical线程提交前时，旧关闭入口未等待准备便关闭附件catalog并返回成功，放行后原发送触发已关闭存储错误；两项有效反例与原始500保留。Workspace现在撤销新准入后，通过原计数排空准备，再取得Job取消快照与收尾。Session删除迁入同一等待主体，移除重复循环；无新账本、schema、兼容入口或执行链。
- 最终新产品6 passed/49.03秒：真实双挂载、Session/消息HTTP入口与真实上传/canonical提交，分别覆盖上传前、canonical提交前、已通过Job准入但尚未登记三个窗口，以及关闭观察到期/取消；核原owner与依赖保留、peer独立关闭、新发送503、原输入/附件持久化与迟到Job取消终态。模型端复用model_stream。Job与Backend生命周期单元127 passed/41.46秒，含新3项多窗口、单等待取消及原准备成功/异常/取消；原任务体前取消writer回归2 passed/23.51秒，核落盘失败及原入口重试。矩阵新增独立120秒/4GiB suite `integration-shutdown-message-preparation`，真实CLI列表校验通过，命令与已通过产品运行一致，未无理由重跑同一业务。
- 扩大旧关闭回归首项在触发关闭前超过原8秒ready期限，后续5项被原生命周期未关闭阻断；原件保留，不计业务通过。该前置长尾已有代码TODO，未扩大性能优化或放宽期限；从新进程定向运行不依赖该失败状态的上述writer两项通过。首次新测试导入错误、扩展装具将0.1秒观察预算误留至最终收尾所致失败也独立保留，修正装具后才取得最终6项结果。
- Ruff、compileall及OpenSpec strict 51/0通过。完整旧HEAD归档4736个blob核对一致，Pyright旧12/最终12，按文件/最窄AST owner/规则/完整消息无新增，新测试零诊断；不称类型全绿。准确命令、源码前后指纹、raw/实际退出码、类型与作者结构自查见`out/tests/temp/uri-global-closure/artifacts/verification-037.json`；临时完整源码和tar已删除，正式测试及验证产物保留。未验证Gateway/Web、Windows、独立Backend硬重启、同步阻塞可抢占、任意线程终止或整个shutdown硬期限；5.2/5.4仍未勾，整体21/23，团队协作与MCP显式持有继续暂缓。

### 2026-10-29 重复取消后的准备计数归还（5.4部分收口）

- 有效反例1项确认：其它Job持调度锁时，原准备的finally等待该锁，再次取消后任务结束但计数仍为1，后续删除/关闭永久等待。归还现只在所属事件循环同步更新原计数和事件，移除该异步锁等待；登记与排空检查仍在原锁内，没有新清理task、shield层或账本，也不提前结束原线程writer的受保护收尾。
- 最终Job/Backend单元128 passed/43.04秒，真实准备关闭产品6 passed/51.19秒，原任务体前取消writer回归2 passed/23.79秒；Ruff、compileall、OpenSpec strict 51/0通过。生产AST只移除finally的锁等待，其余语句与所有旧测试函数AST逐字语义保持；Pyright复用与当前HEAD五文件SHA吻合的上轮完整证据，旧12/当前12、无新增，新测试零诊断，不称类型全绿。
- 命令、前后源码指纹、raw/实际退出码与作者结构自查见`out/tests/temp/uri-global-closure/artifacts/verification-038.json`，原反例保留。仅验证原asyncio调用归还，不代表Gateway/Web、Windows、独立进程取消、同步阻塞可抢占或任意线程终止；5.2/5.4仍未勾、整体21/23，旧8秒准备长尾与团队协作/MCP显式持有继续TODO。

### 2026-10-29 附件catalog关闭的同步等待（5.4部分收口）

- 物理关闭复用`run_sync_shielded`及原Workspace任务，排队前同步撤销准入，新访问在连接锁前拒绝且锁内复核；原连接关闭、失败重试与维护锁归还仍为同一链。观察到期或取消保留原执行及依赖，迟到完成才继续收尾，无新账本、schema或兼容入口。
- 真实Session/消息HTTP与model_stream生成附件和canonical；连接读取/关闭的真实SQLite锁竞争、线程池排队分别覆盖超时与取消，4 passed/71.94秒，核同owner、peer关闭、诊断request_id、维护锁、原件及新catalog冷读。相关单元17 passed/5.07秒，扩大附件/关闭回归80 passed、1 failed/74.71秒；失败的source-point revision断言在完整原HEAD归档原样复现，未改断言。4739个归档blob逐字核对，同一最终新测试在旧生产关闭阶段失败。首次产品装具的已关闭catalog冷读、线程池误阻peer及Ruff失败均保留原件，修正后取得上述结果。
- Ruff/compileall、矩阵CLI列表与OpenSpec strict 51/0通过；生产Pyright 8→8，按文件/最窄AST owner/规则/完整消息无新增，新测试零诊断，不称类型全绿。命令、源码指纹、raw/退出码与作者AST自查见`out/tests/temp/uri-global-closure/artifacts/verification-046.json`；纯临时归档源码删除，正式输出另行保全。未验证Gateway/Web、Windows、独立Backend重启、真实等待30秒、任意线程终止或整个shutdown硬期限；5.2/5.4仍未勾、整体21/23，团队协作与MCP显式持有继续TODO。

- 2026-10-29 接续迁齐上述 source-point 测试：按 itemized「Fork item 定位与前缀捕获保持一致」，前缀接受早于或晚于当前值的观察 revision，响应仍逐字段等于真实 view/锚点/附件投影；完整复制继续拒绝 revision 冲突。未改生产逻辑或附件身份断言。定向1项及原完整附件/关闭组合81项通过，Ruff与差异检查通过；离线 doctor/diagnose 的原 URI/只读/失败合同另验27项通过。命令、源码指纹、raw及退出码见`out/tests/temp/uri-global-closure/artifacts/verification-047.json`。本片修正旧合同测试，不代替5.2/5.4的剩余验收。

- 2026-10-29 Browser脚本错误出口回归补充5.2／5.4反例证据：真实`await new Promise(() => undefined)`在工具超时后仍由原队列持有，本地file页面验收的detach在owner版本排空处返回408，另一工具超时测试在context已关闭后的原执行排空处返回408。当前与完整HEAD归档串行执行均复现，不能把页面reset／后续工具可用当作原脚本已停止，也不能删弱清理断言以计通过。准确命令、源码指纹、两版原始输出和已核实的失败阶段见`out/tests/temp/uri-global-closure/artifacts/verification-064.json`；保留现有TODO，不在URI模块加载修复中扩展终止机制。


### 2026-10-29 固定旧产品输入与当前元数据版本衔接

- 固定旧产品仍完整归档自 `cb90835523b9ff6d91e3f8dfaf3491855d55191d`，由真实产品入口生成附件、执行和两代 fork。只在各测试工作区副本中调用既有 catalog v10→11 离线转换和 rollout 24→25→26 的登记 SQL 升级；Session/Thread 从权威 catalog/control owner 取得，不扫描重建身份。原始旧库及生成工作区保留，转换原库字节与副本 canonical JSONL 升级前后逐字校验；不改生产 preflight、schema 或迁移支持范围。
- 原四文件直接 pytest 回归 86 passed、1 failed：失败发生于旧产品 fork 输入生成的 watchdog 启动前置条件，尚未进入当前迁移。改用现有正式 `integration-attachment-offline-migration` matrix 套件，87 passed/275.43秒、exit0，未改 watchdog、期限、身份/hash 或 fork 断言。已安装 catalog 的产品 owner 启动、历史读取和新上传也通过，移除该场景已无消费者的 `WORKSPACE_ROOT` 准备。
- Ruff/compileall通过；现有 Pyright 1.1.414 与完整 HEAD 归档基线均9项诊断，按文件、最窄 AST owner、规则、完整消息及出错表达式完全一致，新升级函数无诊断，不称类型全绿。首次 uvx 下载超时未进入类型分析，保留原件且不重试。作者结构自查仅新增167行 fixture 内的一个元数据升级 helper，无新目录或生产实现；同目录旧聚合测试的组织债务不在此扩展。命令、源码指纹、raw、真实退出码、基线比较和限制见 `out/tests/temp/uri-global-closure/artifacts/verification-066.json`。本片无 Web/Windows/真实 Provider 新验收，不勾5.2/5.4或多工作区4.3/8.2。

### 2026-10-30 辅助服务 HTTP 收尾期限（5.4部分收口）

- Terminal／Browser 删除两份无限等待的 HTTP 关闭函数，共用默认5000毫秒收尾；到期关闭原连接，明确诊断并退出1，正常SIGTERM仍为143。普通Manager未核实继续保留原进程与管理重试；致命错误沿原退出1合同，不把断网当作资源停止证明。
- 原生产在两个未完成正文场景均超过8秒，2 failed／2 passed；候选真实Node／HTTP／POSIX信号4 passed／12.99秒，两原信号回归各6 passed／46.08与58.55秒。三组串行走原独占闸门及matrix外部期限／4GiB保护；正文场景等正式100 Continue后才发部分正文，核精确诊断、原退出码与空持久资源集合。
- Ruff、新测试Pyright零诊断、Node语法、规范strict与作者AST结构自查通过，HTTP／WebSocket／Manager其余接线保持。首次fixture缺参及端口冲突均未进入业务，原件保留。准确命令、指纹、raw／退出码、四场景正式产物与限定证据见`out/tests/temp/service_http_shutdown_20261030/artifacts/verification.json`；无临时源码或二进制产物。未验Windows、Gateway/Web、整个shutdown、同步阻塞或未知原生进程接管，5.2／5.4仍未勾，整体21/23；团队协作与MCP显式持有继续TODO。

### 2026-10-30 冷恢复同步失败后的显式管理重试（5.2部分收口）

- 原普通同步异常会丢弃冻结版本；发送前失败时，两端版本不同导致新管理停止持续拒绝。原恢复记录现同时保留版本与仅管理重试标记：普通查询仍可发现unknown且不重发同步，显式stop/delete在原锁及准入后重试同一版本；重复失败不造管理lease、不派发停止，同步成功后才移除pending。取消、健康资源、旧占用及唯一停止/结算链保持，无新持久状态、schema或账本。
- 原生产8个有效反例失败，最终相关单测410 passed/11.33秒。新增真实Node/PTY或Chromium/HTTP及正式Session/ASGI创建、授权、冷owner/管理场景4 passed/56.62秒，原冷管理4 passed/37.72秒；均走独占闸门和外部期限/4GiB。原Browser撤权suite首轮4 passed/4 fixture setup errors，健康探针收到原请求行且随后端口占用，未进入失败场景主体；确认原端口可重新绑定后，仅重跑受阻4项，4 passed/34.47秒，未改配置、生产或断言。
- Ruff/compileall、生产和新增两文件Pyright零诊断、规范strict及作者AST结构自查通过。准确命令、指纹、原始输出、退出码与限定证据见`out/tests/temp/resource_cold_sync_retry_20261030/artifacts/verification.json`。初版单测因已detach资源没有Thread关联而拒绝，未当业务红例；后改用正式workspace授权入口生成未知占用。无临时源码/截图/二进制，正式运行输出保留。未验Backend进程硬重启、Gateway/Web、Windows、同步阻塞、不合作取消或整个shutdown期限；5.2/5.4仍未勾，整体21/23，团队协作及MCP显式持有继续TODO。

### 2026-10-30 页面超时恢复与未结束 Node 脚本的验收边界（5.2／5.4部分）

- 旧页面恢复及file URL用例用Node内永不结束的Promise触发超时，却要求后续detach成功；页面reset无法终止该Promise，保留unknown属于原合同。三处输入改为真实`page.evaluate`等待，由关闭原Page结束执行；reset失败用例通过正式stop先关闭context并核verified终态，再detach/delete。原53条断言全部保留，新增4条管理停止／清理断言；没有修改生产实现、超时、失败码或停止证明。
- 三项真实工具／HTTP／Chromium场景3 passed/28.76秒，覆盖页面reset后短操作及显式清理、reset原生失败与日志路径边界、file页面URL的导航错误／checkpoint／恢复例外。独立原脚本产品回归4 passed/35.44秒：真实fetch在超时后继续等待，stop/delete到期保留unknown和原记录、peer可读，放行原执行后同入口重试；另验证等待context关闭的脚本先关闭再排空。未以页面用例替代这些脚本场景。
- 队列及原脚本关闭Bun单测6项、Node原生日志失败单测9项通过，共22个不同用例；首次混用Bun runner的9项`node:test`因`t.mock`未实现而失败，改用该文件所需的Node runner后9项通过，原件保留。Ruff通过，Pyright与完整HEAD归档基线均148条，按最窄AST owner／规则／消息／出错表达式比较零新增；作者AST复核确认只变三个测试函数，原断言未减少，不称类型全绿或独立审查。
- 准确命令、源码前后指纹、原始输出、实际退出码、工具选择失败、作者复核及限定证据见`out/tests/temp/browser_timeout_scope_20261030/artifacts/verification.json`。不新增生产终止机制；任意不合作Node脚本、同步阻塞、整个shutdown期限、Gateway/Web和Windows继续未验证。5.2／5.4及整体21/23保持未完成，团队协作与MCP持有延期边界不变。


### 2026-10-30 整批关联释放保全已完成项错误（5.4 部分收口）

- 原Thread/Session释放在其它回执等待到期或被调用者取消时，会丢掉已完成项返回的失败。现删除任务内把异常变成返回值的分支，在同一批任务排空后统一按资源顺序收集；原单错保持对象，多错与批次超时聚合。调用者取消保留原`CancelledError`与消息，已发生的其它失败由其cause聚合保留；原preflight、锁、unknown、lease和只核终态的重试链保持。没有新owner、后台重放或兼容入口。
- 修改前8项反例全部失败；最终相关六文件201 passed，其中8项新场景随后因补强存在性/静态类型断言定向重验全部通过，不重复相加。独立`integration-resource-release-errors`沿正式Session/ASGI创建及真实PTY/Chromium/HTTP验证2 passed/24.65秒，矩阵总28.50秒，固定60秒/4GiB和原集成独占闸门。核原错误身份、超时cause、精确lease冷读、健康终态及同入口核实不重发删除。
- Ruff/compileall通过；三个当前文件Pyright有45条既有诊断，按所属符号、rule、原message及源片段与基线逐项匹配，无新增；新集成文件零诊断。原owner1049行及原单测目录33个直接Python文件仍为既有P2结构负担，本次未新建源码目录或引入通用抽象。准确命令、源码指纹、原始输出、退出码、AST和类型基线核对见`out/tests/temp/resource_release_errors_20261030/artifacts/`。
- 首次完整原释放矩阵120秒超时，原件保留；新增场景最初5秒观察窗早于真实PTY约8.5秒删除回执，依据终态时间修正为15秒观察并独立短suite，产品30秒合同不变。原矩阵定点还复现管理lock-terminal在10秒等不到旧`recover((rid,))`钩子；此前冷管理已改用原recovery准备和锁内停止同步，装具迁齐留待下一独立边界，不以新2项通过宣称原矩阵或5.4全门完成。首轮取消消息经gather丢失及新增import/类型准备问题均保留失败原件，最终未放宽取消消息、业务状态、lease或错误顺序断言。
- 本次不代表Gateway/Web、Windows、Backend硬重启、同步阻塞、不合作终止或整个Session删除/shutdown验收；5.2/5.4继续未勾选，Agent团队与协作继续TODO。


### 2026-10-30 冷管理准入装具迁齐（5.4 验证补齐）

- 原`integration-resource-release`的管理lock场景仍以`recover((rid,))`作为持锁注入点；现行stop/delete只调用`recover(())`准备冷清单，再在原资源锁内做停止同步。因此旧装具在资源命令实际已完成时仍等待10秒未触发的事件。仅将该fixture的tuple判定迁齐为`()`并说明边界；AST核对全文件92条既有断言、产品期限、身份、版本、原lease、终态及不重发行为均保持，不改产品实现或加入旧恢复fallback。
- 首轮复验遇47960端口`EADDRINUSE`，11项均在fixture准备阶段报错，未进入业务；随后逐一核三个端口可绑定、无监听，保留状态原件后复验。最终原固定120秒/4GiB矩阵11 passed/110.76秒，总114.62秒，原集成独占闸门继续生效。它与上一独立suite的2项真实故障组合分别证明不同场景，不能替代整个5.2/5.4、Gateway/Web、Windows、Backend重启或不合作终止。
- Ruff/编译/本change strict通过；原文件Pyright46条既有诊断逐项保持，无新增。准确命令、前后源码指纹、原始输出/退出码、端口状态、92条断言AST及类型基线比较位于`out/tests/temp/resource_command_fixture_20261030/artifacts/`，旧钩子失败原件复用`resource_release_errors_20261030/artifacts/product-existing-diagnose-001.*`。性能TODO：本机本次总114.62秒已接近原120秒门槛，后续其它主体完成后再按相同工作负载测量并优化，不擅自增大测试期限或调整业务期限。5.2/5.4保持未勾，团队与协作继续TODO。

### 2026-10-30 operation取消保全已完成释放错误（5.4部分收口）

- 原`ResourceOperations.close()`在部分lease释放失败、另一项仍等待锁时被调用方取消，会丢掉此前失败。现复用原gather排空及按资源顺序取错的分支，保持原CancelledError和消息，已发生的释放错误聚合为其cause；没有先前错误时原样取消。只修改共享close，原30秒预算、账本、锁、持久成功后移除引用及同operation显式重试链保持，无后台补做或第二套释放实现。
- 修改前4个有效反例失败、2个无错误取消场景通过；最终相关单元312 passed/9.16秒。新增6项在补齐类型收窄及逆序完成断言后定向复验全部通过，属于上述子集，不重复相加。正式Session/ASGI资源创建和原operation生成真实PTY/Chromium/HTTP与占用，原`integration-resource-release-errors`矩阵4 passed/52.06秒，总55.97秒，固定60秒/4GiB；等待其它会话Web测试释放独占名额后才启动，未绕过原闸门、改Web自动化或重启共享debug。核原错误身份、取消消息、精确lease冷读、健康释放、外部身份/版本不变、解锁不补做与原入口重试。
- Ruff、compileall和本change strict通过；生产及新增场景Pyright零诊断，原单测11条诊断均定位于本次AST未改的函数，不称类型全绿。首次新增代码的5条类型收窄诊断已修正，原件保留；所有原测试函数及断言AST保持。准确命令、源码指纹、原始输出、退出码、正式产品收据和作者结构自查见`out/tests/temp/resource_operation_cancel_20261030/artifacts/verification.json`。性能TODO：本次矩阵总55.97秒接近原60秒门槛，保留测量，待主体完成后再优化，不增大期限。本片不代表Gateway/Web、Windows、Backend硬重启、同步阻塞、不合作终止或完整shutdown验收；5.2/5.4与整体21/23保持未完成，团队协作及MCP显式持有继续TODO。

### 2026-10-30 文件监听关闭保留原任务（5.4部分收口）

- 移除关闭/最后订阅退出时提前清空登记的两处实现，统一等原任务终结后移除；原任务只取消一次，取消观察者、重复shutdown及同key新订阅不会跳过或再次取消原清理。其它root不跨等待持共享锁，关闭后拒绝新订阅；初始化取消沿原release清理本次队列，失败身份与顺序保留，无第二关闭owner或持久账本。
- 修改前6项反例失败；最终六文件共67个不同单元用例通过，其中10项新增场景。首次66 passed/1 setup error来自该测试目录遗留schema10库（当前要求11），保留旧工作区后仅补跑该1项通过，不改schema或断言。真实双挂载lifespan/原生watchfiles验收1 passed/10.62秒，矩阵总17.26秒；暂停位置在真实原生退出后、原任务最终完成前，核peer关闭、原30秒观察到期、取消消息、原任务/scope/registry/维护锁保留及同owner继续排空。首次矩阵在另一会话Web测试占用名额时排队60秒TIMEOUT；确认持锁进程退出后重跑通过，未绕独占闸门或扩大期限。
- Ruff、Pyright零诊断、compileall及本change strict通过；作者AST复核确认路径解析、事件分发、普通监听错误帧等原方法保持，非独立审查。准确命令、前后源码指纹、原始输出、退出码及旧库归档位置见`out/tests/temp/watch_shutdown_20261030/artifacts/verification.json`。未改Web实现/自动化或重启共享debug，不声称独立进程重启、Windows、任意原生线程终止或完整shutdown验收；5.2/5.4及21/23保持未完成，团队协作与MCP显式持有继续TODO。
- 后续将故障点从监听任务外层移入原`awatch`迭代器的finally，发现取消后的清理异常原先只发错误帧后正常返回。4项shutdown/最后订阅、单错/多错反例均失败；现将该原错上抛给同一drain，失败任务和错误身份保持，普通运行时错误帧回归不变。最终69项单元及真实双挂载1项回归通过（10.34秒，矩阵16.79秒），原4条断言全部保留，Python净减4行。受控迭代器故障不代表真实Rust清理故障；真实生命周期回归仍只证明原生退出后的收尾。首轮矩阵等待Web名额60秒TIMEOUT、初轮Ruff的过期noqa拒绝均保留；持锁进程退出后按原期限补跑通过。静态与strict、指纹、raw及作者复核见`out/tests/temp/watch_native_cleanup_20261030/artifacts/verification.json`，5.2/5.4保持未勾。

### 2026-10-30 删除恢复目录的同步回收（5.4 部分收口）

- 原同步实现的删除/父目录同步暂停场景有2项有效反例；现将完整单节点校验、retention、lease/creation复核、删除和barrier迁入同一worker，原Session gate保持至线程池排队及原I/O结算。取消不提前释放gate，不新增schema、账本或兼容入口。
- 74项单元通过，覆盖原删除回归、排队/原生等待、取消、期限和异常身份/同入口重试；2项产品ASGI集成通过（10.07秒，矩阵16.71秒），经创建/导航删除生成journal及隔离目录，验证暂停期间HTTP查询、request_id、peer节点、原gate与目录保全，放行后取消归属及不重复删除。首轮集成误用仅含版本的snapshot端点，改用roots及typed节点身份断言；未调整生产API或放宽断言。
- Ruff、Pyright零诊断、compileall及本change strict通过；准确命令、源码指纹、原始输出、退出码和作者差异复核见`out/tests/temp/session_replay_gc_io_20261030/artifacts/verification.json`。catalog清单同步读取、任意原生I/O终止、独立进程重启、Windows、Gateway/Web和完整shutdown仍未验；5.2/5.4及21/23保持未完成。未改Web实现/自动化或重启共享debug，团队协作和MCP显式持有继续TODO。

### 2026-10-30 Session 删除物理隔离的同步等待（5.4 部分收口）

- 原同步rename/目录屏障各有1项有效反例。现将mkdir、rename及三道目录barrier迁入同一worker；原错误映射、操作顺序及两处异步phase观察保持。恢复分支也持原Session gate至原worker结算，取消/到期不提前释放原gate或同key锁，不补记尚未核实的进度、不重复drain/rename。
- 101项相关单元通过，含27项首次/恢复、全部磁盘阶段、完成/取消/截止和线程池排队场景；15项产品ASGI/生命周期集成通过（44.77秒，矩阵51.25秒），经真实导航入队、删除、worker停止和重新启动核原journal、未提交进度、原件/fence、HTTP查询及同操作恢复，并保留原删除回执和目录扫描竞态回归。worker重启不等于Backend进程重启。
- Ruff、Pyright零诊断、compileall、本change strict及作者AST复核通过；准确命令、指纹、原始输出、退出码及限定证据见`out/tests/temp/session_isolation_io_20261030/artifacts/verification.json`。初次新增测试locator类型收窄及import格式错误已修，原件保留。目录/控制库预检及catalog事务仍同步，任意原生I/O终止、独立进程重启、Windows、Gateway/Web与完整shutdown继续未验；5.2/5.4及整体21/23保持未完成。未改Web实现/自动化或重启共享debug，团队协作及MCP显式持有继续TODO。

### 2026-10-30 隔离预检与fence的同步等待（5.4 部分收口）

- 源目录检查、真实SQLite写锁竞争及预检线程池排队的原实现共3项有效反例。现将隔离内的目录检查、恢复目标核验和原fence CAS合并到同一worker；迁齐唯一调用方后删除旧CAS入口。原验证/错误、CAS事务及连接关闭顺序、异步phase与物理屏障保持，原Session gate/同key锁覆盖排队与I/O结算。取消或到期不执行后续rename、不假报未推进fence或删除成功。
- 113项相关单元通过；原物理隔离排队装具改在真实fence观察点占用线程池，保留原身份、进度、取消及barrier断言。17项产品ASGI/生命周期集成通过（49.22秒，矩阵55.51秒），新增真实SQLite竞争期间导航查询、worker停止及同journal重启恢复，原删除回执/扫描竞态回归仍通过；关键删除记录由产品入队生成。
- Ruff、Pyright零诊断、compileall、规范strict及作者AST复核通过；准确命令、指纹、raw/退出码和限定证据见`out/tests/temp/session_isolation_preflight_20261030/artifacts/verification.json`。前序目标存在性查询、Thread创建收敛的控制库访问及catalog事务仍同步；整个shutdown硬期限、任意原生I/O终止、Backend进程重启、Windows和Gateway/Web仍未验。5.2/5.4与整体21/23保持未完成，团队协作及MCP显式持有继续TODO；未改Web实现/自动化或重启共享debug。

### 2026-10-30 删除收敛控制库的打开与关闭（5.4 部分收口）

- 原实现的真实SQLite写锁等待和同步关闭故障点各复现1项阻塞反例；现由原worker原语完成检查/打开/关闭，打开结果交付前已登记关闭。取消与到期结算原连接后传播，同key锁不提前释放；原异步收敛、后序holder/Session gate和删除进度顺序保持，不把此阶段误写成已持Session gate。
- 128项相关单元通过，含12项排队/原生等待、完成/取消/到期和3项原错误/关闭/重试场景。原预检3项排队装具迁到真实排空回调后，全部原断言保持；首轮122通过/3失败及静态修正原件均保留。4项产品ASGI集成通过（15.00秒，矩阵21.66秒），由创建/导航入队生成journal，核暂停时HTTP/request_id、原件/fence、worker停止前原连接结算及同操作恢复；关闭故障点不冒称SQLite原生关闭锁竞争，worker重启不代Backend进程重启。
- Ruff、Pyright零诊断、compileall、strict及作者AST复核的命令、指纹、raw和退出码见`out/tests/temp/session_delete_control_io_20261030/artifacts/verification.json`。收敛内部SQL/文件校验、前序目标读取和catalog事务仍同步；原生终止、完整shutdown、Backend进程重启、Windows与Gateway/Web仍未验。5.2/5.4及21/23保持未完成；未改Web实现/自动化或重启共享debug。

### 2026-10-30 删除接管未发布Thread的同步等待（5.4 部分收口）

- 原清单读取和staging删除各有1项有效阻塞反例。现迁齐原清单/记录读取、最后复核/中止及唯一目录清理的五处调用；目录全部核验后才删除/同步，异步fork中止回调仍留原事件循环。取消/到期结算原worker与holder/gate，按实际提交保留preparing或aborted，不重造创建身份、账本或进度。
- 237项单元通过，含21项原生读取/核验/删除/屏障、真实SQLite中止锁竞争、线程池排队及完成/取消/到期。首轮235通过/2失败是清理排队观察早于前序读取交付，装具现确认真实清理队列后取消，原断言保持。4项导航产品ASGI集成通过（15.37秒，矩阵21.93秒），未发布记录和staging经原创建服务生成；原fork取消/中止恢复套件8项通过（78.67秒，矩阵86.28秒），核原capture、附件claim及不重建/不重捕获。
- Ruff、compileall、strict通过；Pyright仍为6项既有诊断，已在完整HEAD归档逐项复现，规则/消息/源语句相同，本轮零新增，不宣称全绿。作者AST复核保留其余31个符号、五处调用、全部核验/删除语句与原控制库测试断言；结构审查登记既有1492行模块和core目录49个直接Python文件，本次不新增领域或顶层owner。准确命令、指纹、raw/退出码及审查见`out/tests/temp/thread_creation_cleanup_io_20261030/artifacts/verification.json`。原创建准备、前序目标读取/catalog事务、Backend进程重启、Windows、Gateway/Web与完整shutdown仍未验；5.2/5.4及21/23不勾，未改Web实现/自动化或重启共享debug。


### 2026-10-30 资源记录与诊断输出的双重失败

- 原记录读取、写入及格式拒绝统一调用现有 `report_failure`，移除直接写日志分支；保留原公开说明、异常类别、errno/winerror 与直接 cause。日志故障只增加类别，位置与完整次错保存在原异常 notes，不改变唯一 save 的持久发布、原资源或 lease。
- 原实现的两类资源替换＋日志双错反例均失败；最终共享资源、原账本错误与执行失败单测166项通过。正式 Session、ASGI 资源创建 API、真实 PTY/Chromium/HTTP 的4项正常日志/日志失败组合通过，原身份、字节、占用及显式重试断言全部保留；未手填资源状态。两次90秒矩阵在共享Web验收占用闸门时排队超时，随后取得名额的运行被既有端口容量400拦在装具；原HEAD已有404个集成文件。独立提交 `ba02bde06` 将容量扩至450，8项原端口测试及静态检查通过，范围邻接不重叠且不超过65535。最终资源运行沿原独占闸门等待87.84秒，取得名额后37.52秒；进程外保护覆盖排队、90秒执行及4GiB数据段，未并行运行产品测试。
- Ruff、compileall及严格规范验证通过。Pyright保留2项既有诊断，完整HEAD归档实跑与最终源码逐项核对规则、消息和源语句，零新增，不称类型全绿。作者结构/AST复核确认三个领域记录、唯一save及全部既有断言未变，不新增目录或持久事实。准确命令、源码指纹、原始失败/通过与退出码在 `out/tests/temp/resource_record_diagnostics_20261030/artifacts/`，独立端口证据在 `out/tests/temp/integration_port_capacity_20261030/artifacts/`；临时归档已清理。未运行Gateway/Web、Windows或Backend硬重启，本片不补足5.2/5.4的终止与完整期限验收，两项继续未勾；Web/Workbench及其自动化由另一会话推进。


### 2026-10-30 删除恢复的前序目标探测（5.4 部分收口）

- 两项原实现反例证实 `exists/is_symlink` 前序探测阻塞事件循环；现同一次 worker 保留短路语义与原异常，不新增探测 owner 或恢复链。前序仅持同 key 锁与外层预算，Session gate、资源 holder 及 retention/drain 仍在后序；取消/到期结算原生等待或线程池队列后传播，不补做排空、fence、rename或进度。真实隔离后恢复不重复管理动作，断链 symlink 不覆盖原件。
- 170项相关单元通过，含21项新增探测、排队、完成/取消/到期、原错和断链目标场景。首次168通过/2失败来自旧控制库打开装具提前占用线程池，现移到真实 retention 回调后，原28条断言AST全部保持。原17项产品ASGI/生命周期场景通过（49.72秒），新增两项产品导航入队、原 worker 停止及同 journal 恢复通过（10.01秒）；两组分别注册，保持各60秒/4GiB与集成独占闸门，未删旧场景或放宽业务期限。直接运行新增两项的10.16秒结果仅作先期验证，不重复计数。
- Ruff、Pyright零诊断、compileall与strict通过；作者AST自查确认生产仅改 `_drain`，其余17个方法/函数及控制库原断言保持，不代表非作者独立审查。首次import/闭包绑定静态失败及误传重复 `--suite` 参数的零执行失败原件均保留。准确命令、源码指纹、原始输出、退出码和自查位于 `out/tests/temp/session_isolation_probe_20261030/artifacts/`。
- 原创建准备、catalog同步事务、任意原生I/O终止、整个shutdown硬期限、Backend独立进程重启、Windows和Gateway/Web仍未完成本轮验证；5.2/5.4及整体21/23保持未完成。未修改Web/Workbench实现或其自动化，未重启共享debug；团队协作及MCP显式持有继续TODO。

### 2026-10-30 删除与回收的 catalog 等待（5.4 部分收口）

- 删除冻结/mark、拒绝中止、进度、finish/回执读取和回收清单统一复用原 shielded worker；导航 hook 仍在原 SQLite 事务内。同 key 锁及对应 topology gate 等原调用实际结算后释放，取消不遗弃提交，也不继续下一阶段；重试沿原 journal、冻结 locator 和已完成进度，不增加 writer、schema 或恢复链。
- 完整 HEAD 归档中的旧生产实现被两项阻塞反例区分。最终相关单元222项通过（36.64秒）；新增产品导航入队、mark/finish 等待期间停止 worker、同 journal 恢复及回收取消3项通过（12.75秒）；相邻删除/故障结算/启动扫描17项通过、2项目标探测由先前独立套件覆盖。全部有进程外超时与4GiB保护，集成沿跨 worktree 独占闸门串行；相邻批排队137.06秒，取得名额后47.80秒，未超60秒执行预算，不把排队算作业务耗时。
- 原线程池排队装具迁到各自实际阶段，finish 时钟屏障等原进度提交后才推进；既有85条断言的AST全部保持。早期装具/静态失败及221通过/1失败原件保留，不算最终通过。Ruff通过；Pyright原4项既有locator类型诊断在完整HEAD归档逐项复现，新增生产/场景零诊断，不称类型全绿。作者AST自查保留其它15个生产方法，不称独立审查。
- 准确命令、源码指纹、原始输出、实际退出码、类型基线和结构自查见 `out/tests/temp/deletion_catalog_io_20261030/artifacts/verification.json` 及同目录原件。全仓同步catalog消费者及其共享连接锁、创建准备、原生I/O终止、完整shutdown硬期限、Backend独立进程重启、Windows和Gateway/Web仍未验；5.2/5.4及整体21/23保持未完成。未修改Web/Workbench及其自动化，未重启共享debug，团队协作及MCP显式持有继续TODO。

### 2026-10-30 Child冻结后的目录准备与恢复（5.4 部分收口）

- 目录探测、staging准备/原子写/同步、rename/同步、final恢复核验与发布后复验共用原shielded worker；各步骤结算后才交还原key锁与创建holder，取消/到期不继续下一步，不提前关闭原控制库，也不倒删已发布child。准备/核验仍在Session/topology gate外，发布事务及业务回调仍在原事件循环/gate内；无新writer、schema或目录恢复实现。
- 完整旧生产tree加相同新增用例的两个反例均失败，复现事件循环被staging或发布后核验阻塞。最终创建链112项单元通过（9.23秒，含24项原生等待/线程池排队×完成/取消/到期×准备/rename/恢复/已发布核验），相邻创建收敛与Session gate 52项通过（5.00秒）；原创建测试正文逐字不变。
- 正式`integration-fork-child-preparation-io`最终6项通过（86.88秒）：产品消息与真实Agent/model_stream生成来源历史，经fork服务创建child；核目录等待取消时原holder保护、冻结record不变、同key恢复及唯一child/零初始执行，来源和目标canonical原字节一致；同时复跑原三类发布确认/附件结算失败恢复。初轮新增3项已通过（48.80秒），不重复计数；最终批补验测试收尾的显式Awaitable类型，保持原60秒fork期限、180秒矩阵预算、4GiB保护和跨worktree集成独占闸门，未与另一会话的Web验收并跑。
- Ruff、compileall及本change strict通过。Pyright仍为6项原诊断，在完整HEAD归档按规则、完整消息、最窄AST方法和源语句逐项相同，新增代码零诊断，不称全绿；初版两项测试收尾类型错误及import格式错误已修正，原件保留。作者AST复核只有create/最终确认两个方法改变、增加一个目录探测函数，原31个符号、物理写入/rename/核验函数及既有测试保持；原1517行模块和core直接49个Python文件的架构债务不据此关闭。
- 准确命令、前后源码指纹、原始输出、实际退出码、类型对照和作者自查见 `out/tests/temp/child_creation_io_20261030/artifacts/verification.json` 及同目录原件。准入/发布的同步catalog/control、任意原生I/O终止、完整shutdown硬期限、Backend硬重启、Windows及Gateway/Web未补验；5.2/5.4及整体21/23仍未完成。Web迁移/自动化、共享debug、团队协作及MCP显式持有延期边界保持。

### 2026-10-30 Child准入的同步核对与冻结（5.4 部分收口）

- 准入catalog读取、owner/控制库绑定/main指针核对、路径预算及record/manifest冻结复用同一次shielded worker，原topology shared、Session exclusive、key锁和创建holder保持到真实结算，包括线程池排队。取消或外层到期后不进入prepared观察、目录准备或发布；已冻结record与清单供同key恢复，不新建事务、writer、schema或恢复链。发布事务及原业务回调仍在事件循环/原gate内。
- 203项相关单测通过：创建及准备142项（13.47秒，含新增30项catalog/绑定/main/record/manifest × 完成/取消/到期 × 原生等待/排队），相邻创建收敛、Session gate与owner工厂61项（9.59秒）。完整旧生产归档加相同测试的catalog/SQLite两个反例均失败，复现事件循环被阻塞及真实SQLITE_BUSY，不以helper替代产品状态。
- 最终原`integration-fork-child-preparation-io`共8项通过（107.52秒）：新增两项由产品消息/Agent/model_stream生成历史，实际fork在SQLite写锁或manifest冻结后取消；核原gate/holder、原child/locator/清单、唯一目标、零初始执行及来源/目标canonical字节不变。同批保留原三项目录等待及三项发布确认/附件结算恢复。新用例并入既有文件、移除临时新suite，fork目录直接Python文件保持20；迁移前首次8项通过（117.25秒），不重复计数。保留原60秒fork期限、180秒/4GiB矩阵及集成独占闸门。
- Ruff、compileall通过；Pyright仍为原6项生产诊断，完整归档逐项核对规则、完整消息、原owner及源语句一致，新准入闭包只是原gate正文的完整AST迁移，新增测试零诊断。作者AST核对原gate上下文和全部其它模块AST不变；原1522行模块及core直接49文件的结构债务未解决，不据此关闭7.5-F。首轮import格式检查失败已修复并保留原件。
- 准确命令、源码指纹、原始输出、实际退出码、类型对照和作者复核在`out/tests/temp/child_admission_io_20261030/artifacts/verification.json`及同目录原件。发布同步I/O、任意原生线程终止、完整shutdown硬期限、Backend硬重启、Windows及Gateway/Web仍TODO；5.2/5.4和整体21/23保持未完成。未触碰另一会话的Web迁移/自动化、共享debug、团队协作及MCP显式持有延期范围。

### 2026-10-30 Child发布、失败收尾与终态核验（5.4 部分收口）

- 原gate内owner核对与发布CAS复用同一次shielded worker；冲突后重读、gate外失败复核/中止、初始execution intent和返回前published核验均结算各自原调用。fork物化前capture查询、物化后record重读/失败中止迁齐同一链；原业务回调与异步阶段观察保持原事件循环/gate及顺序，不新建事务、writer、schema、恢复链或第二确认链。实际已发布/aborted事实不因取消回退，同key重试补齐原确认而不重发CAS。
- 251项相关单测通过：创建/目录准备/准入/本轮发布190项（20.86秒，含新增48项发布catalog、CAS前后、冲突重读、失败复核/中止、intent及终态核验 × 完成/取消/到期 × 原生I/O/线程池排队）；相邻创建收敛、Session gate与工厂61项（9.44秒）。完整旧生产归档加相同新增测试的发布写锁和终态核验两个反例均失败，复现事件循环阻塞及SQLITE_BUSY。首轮import格式问题已修正，测试全AST保持相同，复用原190项结果，不为格式重复长跑。
- 正式原`integration-fork-child-publication`7项通过（109.47秒）：产品消息/真实Agent/model_stream生成带附件来源，原三类确认失败恢复保持；新增真实SQLite写锁、CAS提交后、已提交冲突重读及返回前核验的四种取消。核原holder/gate、唯一published child、零初始execution、冻结record及来源/目标canonical原字节，未确认时settlement保持preparing，原确认完成后保持published_pending_attachment_commit，同key重试完成committed且CAS仅一次。实际确认回调线程核对仍为事件循环。保留原60秒fork期限、180秒/4GiB矩阵与集成独占闸门，未改suite或Web自动化。
- Ruff、compileall通过；Pyright原6项生产诊断与完整HEAD归档的规则/完整消息/最窄AST owner/源语句逐项一致，新增测试零诊断，不称全绿。作者AST核对：逆转worker调度并还原publish闭包后，整个模块AST与原版完全相同；原gate、事务、异常/确认/观察顺序及全部其它符号保持。单元与fork目录直接Python文件分别10/20，没有新增源码目录；1525行模块与core直接49文件的既有结构债务未在本片解决，不关闭7.5-F。
- 命令、源码指纹、原始输出/实际退出码、完整归档反例、类型对照和作者审查见`out/tests/temp/child_publication_io_20261030/artifacts/verification.json`及同目录原件。业务确认回调的同步I/O、任意原生线程终止、完整shutdown硬期限、Backend硬重启、Windows及Gateway/Web继续TODO；5.2/5.4与整体21/23保持未完成。Web迁移/自动化、共享debug、团队协作及MCP显式持有延期边界未扩展。

### 2026-10-30 Fork 发布确认的异步合同（5.4 部分收口）

- Session main/child 的首次发布及已发布重入统一 await 原 on_published；Session service、fork owner 及测试消费者全量迁齐，不保留同步兼容入口。确认回调入口仍在原事件循环/gate，原 proof 核对、catalog 查询及附件 settlement 写入放入同一次既有 shielded worker；原锁覆盖线程池排队及真实结算，取消不继续下一阶段、不回退已发布事实，不另建 writer、事务、schema 或确认账本。
- 168项定向单测通过（12.54秒）：Session/child 创建、发布错误身份/同key恢复及owner工厂。新增正式 integration-fork-confirmation-io 共6项通过（92.22秒），由产品消息/Agent/model_stream生成带附件历史，核新Session与child在确认写入前后及线程池排队取消时的原gate、回调/worker线程、published目标、冻结来源与目标canonical原字节、原claim与preparing→pending→committed结算。同key恢复禁止重新物化，目标身份与记录不变。
- 原 integration-fork-child-publication 7项回归通过（101.73秒），保留三类确认/观察失败的原异常及四种真实SQLite写锁/发布后/冲突重读/终态核验取消。两suite串行走集成独占闸门，各保留180秒/4GiB与原60秒fork watchdog；未改Web suite、共享debug或其它会话暂存。
- Ruff、compileall、OpenSpec strict与diff检查通过。扩大到五个生产模块及受影响测试的Pyright为81项既有生产诊断；完整HEAD归档逐项核对规则、完整消息、最窄AST owner与源语句一致，本轮无新增、测试零诊断，不称类型全绿。作者AST核验确认正文与旧版相同，其余模块逆转异步确认合同后整模块AST相同，原gate/异常/观察顺序保持。已有大模块和core目录结构债务未解决，不关闭相关架构任务。
- 完整候选源码归档仅还原同步确认后，main反例在事件循环阻塞、原任务已提前终结的断言处失败；child在等待故障点阶段超时，原件保留，不单独作精确归因。首次反例误用直接pytest，被正式watchdog装具拒绝，已改用原matrix，不算有效反例；首轮测试executor恢复类型及import格式问题已修复，保留原始失败。准确命令、候选/归档指纹、原始输出/退出码、类型对照及作者复核在 out/tests/temp/fork_confirmation_io_20261030/artifacts/verification.json 与同目录原件。两个临时源码归档已删除，正式产物与反例输出保留。Session其余同步I/O、任意原生线程终止、独立Backend重启、Windows、Gateway/Web及完整shutdown硬期限继续TODO；5.2/5.4与整体21/23不提前勾选。

### 2026-10-30 Session 创建原同步阶段的结算（5.4 部分收口）

- 原创建 owner 的记录冻结、目录准备/核验、发布前核对/rename、发布 CAS、已发布结果及隔离中止统一复用既有 shielded worker，各自原 key 锁及相应 gate 保持到真实 I/O 结算。取消/外层期限不遗弃提交、不继续下一阶段；原 preparing/published/aborted 身份、locator、原异常和异步确认顺序保持，不增 writer、事务、schema 或恢复链。
- 211项相关单测通过：创建133项（7.96秒，含新增66项，覆盖11阶段×完成/取消/到期×原生等待/线程池排队）；相邻Session service、child发布/准入及gate78项（9.72秒）。真实SQLite写锁覆盖冻结及发布前等待。完整旧生产归档加相同测试的staging/rename两个反例在事件循环等待故障点时超时，区分原同步阻塞；首次反例错误地提前读取新worker符号，已修测试作用域，不算有效反例。
- 新正式 `integration-session-creation-io` 六场景均覆盖通过：产品消息/Agent/model_stream生成带附件来源，经ASGI fork API核冻结、staging、rename、CAS前后及quarantine取消，独立工作区API仍响应，原key/gate等原调用结算，同请求只恢复原目标或保留aborted及隔离目录，canonical原字节不变。首五项在完整批次通过；最后一项修正HTTP错误合同后定向1项通过（21.57秒），前五项完整AST不变的复用证据在 `product-result-reuse.json`。原 `integration-fork-confirmation-io` 六项回归通过（83.09秒）；集成串行走原独占闸门、180秒/4GiB及正式60秒fork watchdog，没有与Web验收并跑。
- 首轮产品检查只输出FAILED后被watchdog终止（143），不算通过；修复新测试提前关闭阻塞连接的问题后，健康探测因原fixture明确隔离持久任务调度而返回500，改用真实工作区信息API核事件循环与request_id。quarantine的HTTP重试按既有500及request_id断言，原服务入口仍核精确aborted错误，不改变产品错误合同。全部初轮失败原件保留。Ruff、compileall通过；Pyright仅1项既有winerror属性诊断，完整HEAD归档按规则、完整消息、最窄AST owner及源语句相同，新增测试零诊断，不称类型全绿。作者逆转worker调度及局部函数抽取后整模块AST相同；985行模块、core直接49个Python文件的既有结构债务未关闭。
- 准确命令、前后源码指纹、原始输出/退出码、完整基线来源、类型对照及作者复核见 `out/tests/temp/session_creation_io_20261030/artifacts/verification.json` 和同目录原件；临时完整源码归档已删除，其正式测试输出保留。同步辅助查询的其它异步消费者、Session/fork其它存储访问与共享连接锁、任意原生线程终止、独立Backend重启、Windows、Gateway/Web及完整shutdown硬期限继续TODO；5.2/5.4与整体21/23保持未完成。未修改Web/Workbench及其自动化，未重启共享debug，团队协作及MCP显式持有继续延期。

### 2026-10-30 Session与资源Thread准入同步读取（5.4 部分收口）

- Session读写active核验、单Session catalog/目录读取及Terminal/Browser共享main/Thread定位复用原shielded worker。DTO投影仍在事件循环，原gate/holder覆盖线程池排队及真实结算；取消/外层期限不提前解锁、不继续准入或迟到创建。原锁序、工作区/目录/Thread判断与异常不变，不增协议、同步兼容入口、writer、schema或lease账本。
- 两批定向单测94项（32.54秒）和332项（11.24秒）通过，其中44项重复，合计382个不同用例；新增24项覆盖main/显式Thread×完成/取消/到期×原生等待/线程池排队×两类owner。Session级联删除原用例在候选和完整HEAD归档均失败：helper仅等committed就停止worker，尚未等physical settlement；保留两份原件且未改产品或放宽断言，其余删除场景本批未复跑，定向批明确排除4项。
- 原`integration-resource-thread-admission`全4项通过（42.98秒），两类真实PTY/Chromium/HTTP各验证8个同步阶段：active、Session读取/目录、main、显式Thread、读准入、共享catalog连接锁及线程池排队。核事件循环及独立HTTP响应、原gate/holder、原记录/lease字节、无迟到创建、显式重试及原导航删除完整流程。新增测试初版误将Terminal原生提示符路径列入普通响应不变量，按既有Shell合同改核公开身份/版本与错误，未改产品行为；初版闭包lint及单测故障点误触30秒预算捕获也已修正，失败原件保留。
- `integration-fork-confirmation-io`6项回归通过（89.62秒），真实消息/Agent/model_stream生成带附件来源，原Session/child发布确认、原gate与canonical保全继续通过。集成串行走原独占闸门，资源suite维持90秒/4GiB、fork维持180秒/4GiB及原60秒watchdog；未改测试runner或Web自动化。
- Ruff通过；Pyright23项生产诊断与完整HEAD基线的规则、完整消息、最窄owner及源语句逐项相同，测试零诊断，不称类型全绿。作者逆转worker调度与两个局部闭包抽取后，两份完整生产模块AST与原版相同；同步get的生产消费者未发现跨await持有同catalog原生事务锁。906/1054行模块及既有目录结构债务未在本片解决，不关闭架构总门。
- 准确命令、前后源码指纹、原始输出/实际退出码、完整基线及反例来源、类型对照和作者复核见`out/tests/temp/resource_admission_io_20261030/artifacts/verification.json`及同目录原件。共享连接锁仍会阻塞依赖该存储的读取，worker不能抢占原生I/O；其它同步存储、旧代接管、不合作取消、独立Backend重启、Windows、Gateway/Web及完整shutdown硬期限继续TODO。5.2/5.4与整体21/23保持未完成；未修改Web/Workbench及其自动化或重启共享debug，团队协作及MCP显式持有继续延期。

### 2026-10-30 导航删除装具等待物理结算（5.4 验证装具修复）

- direct catalog 装具继续持有原 worker，逻辑终态与物理 settlement 共用一次30秒观察预算；只有原回执不再 pending 才正常停止 worker，失败或取消仍清理本装具持有的 worker。产品 durable acceptance、逻辑终态及删除恢复合同未变，不增加产品等待接口。两处后端集成测试迁至现有装具模块，移除已删除模块的悬空导入。
- 新真实 Session 删除屏障用例在旧装具上于“提前完成并停止 worker”断言失败；修复后原目录确实移除、原 operation/queue_seq/target 不变、receipt revision 前进、导航事件字节行不变。后端两文件40项集成通过，沿原独占闸门且保留180秒/4GiB保护。另修复纯Folder单测的等待竞态：屏障内精确核 committed/pending 和 deleting 恢复行，放行后保留 settled、节点移除及无重复事件的强断言，并显式停止自己的 worker；原HEAD单独运行该用例通过，不把它称为稳定失败反例。
- 四文件单测最终99项通过，包含前片排除的Session删除；与集成合计139个不同用例。Ruff与本change strict通过；Pyright保留5条与完整HEAD基线的规则、完整消息及最窄语句AST一致的既有诊断，新增零条并消除两处悬空导入，不称类型全绿。本片证据与准确命令、源码指纹、原始输出及实际退出码保存在`out/tests/temp/catalog_intent_settlement_20261030/artifacts/verification.json`及同目录原件；临时完整HEAD源码副本在比对后删除。本片只恢复验证装具的证明能力，不代表旧代接管、任意原生I/O终止、Backend重启、Windows、Gateway/Web或完整shutdown硬期限完成；5.2/5.4与整体21/23保持未完成。未修改另一会话的Web迁移及自动化，未重启共享debug；团队协作和MCP显式持有继续延期。


### 2026-10-30 Browser Manager关闭的唯一执行与整次观察（5.4部分收口）

- 关闭由唯一原Promise持有创建排空、各资源检查点/失败收尾、共享运行时与末次写盘；每次调用默认30秒观察，阶段不重置，到期保留原执行、句柄、检查点与准入关闭。资源独立推进，正文捕获仍走原串行队列；整批实际结算后才允许失败重试，原错按资源顺序汇总并记录迟到失败。运行时关闭成功后，最后写盘失败只补未完成步骤，成功重入无重复副作用。
- 68项单测、10个真实Chromium/HTTP/磁盘检查点/冷Manager场景及6项POSIX生产信号关闭测试通过；完整候选归档还原旧Manager后，两项反例分别检出重叠检查点与末次写盘失败后的重复共享池关闭。默认30秒timer及跨阶段复用在单测中受控推进，真实context释放场景使用显式2秒观察，不冒称已真实等待默认30秒。JS解析、AST作者自查和strict通过；既有集成函数及其原断言未改。
- 准确命令、源码指纹、原始输出和实际退出码见`out/tests/temp/browser_shutdown_owner_20261030/artifacts/verification.json`。首轮临时runner导入路径错误未进入产品；首次新场景误把primitive操作封套当字符串，仅修正取值后保留原表单值断言，失败原件保留。未运行Gateway/主Web、Windows、Backend硬重启或任意脚本强制终止；共享检查点捕获仍串行，同步阻塞及生产进程退出总期限仍TODO，5.2/5.4保持未完成（21/23）。未修改另一会话Web/Workbench迁移文件或暂存，不重启共享debug。

### 2026-10-30 批量冷恢复中断保留此前失败（5.4部分收口）

- 原ResourceRecovery排空本次任务后，按资源选择顺序保留已完成的原异常；预算到期与本次TimeoutError共同聚合，调用者取消保持原CancelledError/消息并以聚合cause保留此前失败。冻结版本、仅管理重试标记和原lease账本保持，成功项不回滚，后续普通恢复不重发已失败同步。
- 424项相关单测通过（12.86秒），覆盖两类owner、多错逆序完成、取消/到期、原对象与pending分类；完整旧生产归档加相同新单测4项均在预期行为处失败。4项真实PTY/Chromium/HTTP集成通过（69.37秒），经正式Session、ASGI创建/授权及冷owner生成关键状态，核原未知lease冷读、外部身份/版本、健康项可读、不重发同步和显式停止后结算；串行使用原集成闸门，外部180秒/4GiB保护。Ruff、三文件Pyright零诊断及本change strict通过。
- 准确命令、前后源码指纹、原始输出/实际退出码和作者AST自查见`out/tests/temp/resource_recovery_errors_20261030/artifacts/verification.json`。首轮import格式与fixture可空类型已修；误用Python入口的Pyright未执行，保留原件并改用现有Bun入口。只推进本次30秒截止，不代表真实等候30秒、独立Backend重启、Gateway/Web、Windows、任意脚本终止或完整shutdown期限；5.2/5.4与整体21/23保持未完成。未修改另一会话Web/Workbench、自动化或暂存，未重启共享debug。

### 2026-10-30 孤儿回收中断保留此前失败（5.4部分收口）

- 原sweep_orphans排空本次任务后按资源选择顺序保留已完成的原异常；到期追加带原cause的预算TimeoutError，取消保持原CancelledError/消息并以聚合cause保留此前失败。普通失败的原ExceptionGroup、成功结果顺序、未知记录/版本、精确lease与显式管理链保持，无新恢复账本或删除writer。
- 388项相关单测通过（14.95秒，含24项定向子集），覆盖两类owner、多错逆序完成和到期/取消；完整旧生产归档加相同新单测的4个反例均在预期错误保留断言处失败。4项真实PTY/Chromium/HTTP集成通过（160.97秒，包含等待另一会话Web测试的独占名额；外层164.48秒），正式Session及ASGI创建/detach生成状态，只对本次回收推进时钟与截止，核原未知lease冷读、外部deleted/verified/版本、健康项可读及显式delete核实不重发。未绕过闸门或重启共享debug；外部180秒/4GiB保护保持。
- Ruff、compileall和strict通过；Pyright保留4条与完整HEAD的规则、完整消息、位置及最窄语句AST相同的既有诊断，改动函数和新增测试零诊断，不称类型全绿。首轮ContextVar默认值的静态告警通过提前冻结不可变datetime修正，未改业务断言。命令、指纹、原始输出/实际退出码、类型对照和作者AST自查见`out/tests/temp/resource_orphan_errors_20261030/artifacts/verification.json`；临时完整源码归档删除，正式产物保留。未验证真实等候30秒、独立Backend重启、Gateway/Web、Windows、不合作操作终止或完整shutdown期限；5.2/5.4与整体21/23保持未完成，另一会话Web/Workbench、自动化和暂存保持。

### 2026-10-30 活动记录库关闭不阻塞事件循环（5.4部分收口）

- 原Workspace的`activity.close`仅改为复用既有`run_sync_shielded`，线程池排队和原生句柄释放由同一关闭任务持有；Backend观察到期/取消不丢弃原步骤、catalog或维护锁，原失败沿同入口重试，MCP/AnyIO仍由原任务关闭。配置source owner的空关闭保持；未新增关闭writer或改变SQLite释放语义。
- 31项相关单测通过（6.30秒），5项双挂载集成通过（133.34秒，含等待另一会话Web测试名额）；排队/原生释放与取消/到期四组合及原生错误重试均核对原任务、步骤、ASGI诊断、request_id、维护锁、冷读活动与Session locator。完整旧生产归档加同样测试的2个反例均失败（35.58秒）；最终完整独立候选同样5项通过（32.26秒），不依赖其他会话未提交改动、不重复计数。释放屏障位于真实原生解锁调用前，不声称OS已实际发生解锁阻塞。候选首次只在独占闸门等待，236秒后主动中断自己的等待（exit2、no tests ran），按600秒闸门上限重设外层保护后完成；未绕过闸门、终止另一会话测试或重启共享debug。
- Ruff、compileall与strict通过；Pyright的4条既有诊断在完整旧生产上规则、完整消息、位置及对应表达式AST完全一致，新增测试零诊断，不称类型全绿。首轮导入和事件循环私有属性类型已修正，测试恢复原池的反射访问附具名Ruff说明，未改变可执行AST或断言。原始命令、源码指纹、退出码、完整候选patch和作者AST核对见`out/tests/temp/activity_shutdown_20261030/artifacts/verification.json`；正式输出保留，临时源码清理。未验证真实等待默认30秒、独立Backend重启、Gateway/Web、Windows、任意线程终止或完整shutdown硬期限；5.2/5.4及整体21/23保持未完成，另一会话Web/Workbench、自动化和暂存保持。


### 2026-10-31 Fork冻结文件传输的原生等待与失败结算（5.4部分收口）

- 来源导出和目标接收的原生文件I/O复用既有shielded worker；原key锁覆盖结算，打开后尚未交付的句柄先登记，写入者排空后才删除临时快照。保留分块、文件并发、marker、历史字节和hash；取消维持preparing，同key恢复复用完整创建身份与唯一目标。失败清理在线程中恢复原异常上下文，清理取消显式保留此前失败，不新增writer或账本。
- 17项单测、完整独立候选的10个新ASGI产品场景及13个原传输故障回归通过（唯一40项）；另一次定向10项不重复累加。完整旧生产归档加相同新测试的2个反例，均在原同步I/O占用事件循环处失败。新增清理次错反例先检出上下文丢失，修复后保留原生cause与原传输异常。正式matrix、原60秒fork watchdog、主仓独占闸门和4GiB/进程外超时保持；最终新场景151.26秒、原回归202.34秒，旧反例65.94秒，准确命令、指纹和实际退出码见`out/tests/temp/fork_transfer_workers_20261030/artifacts/verification.json`。
- Ruff、compileall和strict通过；Pyright的38条既有诊断已与完整HEAD逐项核对（含旧单测2条），本片无新增类型诊断，不称类型全绿。首轮正式runner前置缺失与一次主动中止自己的闸门等待均未进入测试体；测试误判取消为aborted、误限接收路径及watchdog外层取消观察已按原合同修正，原件保留。传输替身不是实际HTTP网络；原生调用边界屏障不证明OS真实阻塞或线程可终止。其它catalog同步读取、旧代接管、独立Backend重启、Gateway/Web、Windows及完整shutdown硬期限继续TODO，5.2/5.4与21/23保持未完成。未改另一会话Web/Workbench、自动化、共享暂存或重启debug。


### 2026-10-31 Workspace 周期任务关闭保留原错误（5.4 部分收口）

- 原周期任务结束即记录任务身份和完整原异常；关闭先取消全部周期任务再排空，按原登记顺序聚合非取消失败。原错误明确交回调用方，本次保留后续依赖与 Backend 维护锁；已结算任务从原集合移除，同一生命周期 owner 重试只继续后续步骤，不重放周期工作。普通取消与正常完成不报错，旧单任务吞错入口已删除，未引入新存储、账本或兼容层。
- 15 项定向单测及 6 项真实双挂载生命周期集成通过（集成 42.60 秒；主仓共享闸门另排队 310.75 秒）。新反例在完整旧生产归档中精确失败于 Backend 关闭未抛异常；故障仅注入原活动 DELETE 边界，Session 和活动由正式入口生成，核对原错误身份、依赖保留、同一 owner 重试及冷读原记录。首次两份反例及候选因装具读取未配置的个人 Provider 凭据而失败，不能算行为反例；复用现有 maintenance_case 隔离配置后上述验证通过，原始失败保留。
- 扩大正式 `integration-mounted-workspace-lifespan` 为 25 passed／4 failed（187.64 秒）；四个 Goal 场景均在原 `ready.wait()` 的 8 秒期限、进入本次关闭修改之前失败。当前完整 HEAD 基线代表项也在同一语句超时（22.36 秒），另外三项未逐项跑新基线；原测试已有 `29c7ec96` 性能 TODO，本轮不修改期限或展开该长尾优化。结合定向验证，共 46 个不同用例通过，不称扩大套件全绿。
- Ruff、Python 编译与本 change strict 通过；Pyright 原有 4 项 JobServiceProtocol 诊断与完整 HEAD 基线的规则、完整消息和最窄语句 AST 一致，新增零项，不称类型全绿。作者 AST 复核确认除周期任务排空和完成回调外的生产逻辑保持，原模块 424→440 行，无新增源码目录。
- 命令、源码指纹、原始输出和实际退出码见 `out/tests/temp/periodic_shutdown_failures_20261031/artifacts/verification.json` 及同目录原件。本片不证明同步阻塞可抢占、任意旧操作可终止、生产进程总期限、独立 Backend 重启、Windows 或 Gateway/Web 总验收；5.2／5.4 与整体 21/23 保持未完成。未修改另一会话的 Web/Workbench、自动化、暂存或共享 debug；团队协作与 MCP 显式持有继续延期。

### 2026-10-31 PTY 自发退出保留未确认的 Worker（5.2／5.4 部分收口）

- Session 收到 PTY 退出观察后，等待原 Worker 关闭确认，期间保留 unknown 和原句柄；原 kill/delete 可接管同一执行，迟到退出处理不覆盖管理结果。Worker 重叠关闭复用唯一在途任务，不重复 IPC／信号，失败结算后才允许重试；已确认退出才能移除句柄，正常退出确认后恢复原 verified。不增加停止算法、持久结构或公开 API。
- 完整旧生产归档的真实 HTTP/PTY 反例精确失败于“Worker 仍存活但快照的 pty_worker_pid 已为空”。初版候选 8 项真实集成通过（53.53 秒）；随后补齐并发管理与唯一关闭任务，最终 124 项 Session／Worker／Manager／shutdown 单测通过。原诊断单测的旧 null 预期按真实反例修正为原句柄及 unknown，保留全部原日志、因果关系与发布顺序断言，不用装具补造退出确认。
- 最终 9 项集成已补验：完整 `git archive 936c09c99` 经主仓共享闸门取得名额，在原 60 秒／4GiB 保护下全部通过（55.57 秒，外层 58.03 秒），源码前后指纹一致。覆盖正常自发退出及并发管理时保留原 Worker、唯一关闭和真实终态；证据见 `out/tests/temp/terminal_exit_worker_final_20261031/artifacts/`。此前等待满 600 秒的失败原件仍保留，pytest 当时未启动；较早定向排队只因源码修订取消自己的等待。未中断或绕过另一会话测试，初版 8 项不重复累加。
- Ruff、Python 编译、Pyright 和 Node 语法检查通过；规范严格验证见同目录原件。准确命令、各阶段指纹、原始输出、退出码及作者差异复核见 `out/tests/temp/terminal_exit_worker_20261031/artifacts/verification.json`。无 PID 的 native 接管、冷重启后的未知 Worker、Windows 及完整 shutdown 硬期限继续 TODO，5.2／5.4 和整体 21/23 未关闭。未修改 Web/Workbench、其自动化或共享 debug。

### 2026-10-31 Linux 执行根退出后的原 SID 清理（5.2 部分收口）

- 原 Worker 错误及停止失败生成 exited/unknown 后，真实 PTY 根进程自行退出，原 SID 的后代仍在执行；旧实现显式 kill 返回500，未进入后代停止链。Linux 现仅在原 PID、进程组和 SID 符合原 PTY 根身份时，将根缺失继续交给既有进程树清理；仍存在但身份不符或身份不完整时继续拒绝，不创建 Worker 或第二停止实现。
- 完整 HEAD 归档叠加本片改动，124 项 Session／Manager／进程生命周期单测通过；4 项真实 HTTP／PTY 场景通过（23.37 秒），覆盖同 Manager／冷 Manager × kill／delete，核原后代停止、持久终态、原身份及重复请求无新执行。复用装具仅将输出归属改为当前测试文件，不手填状态或替换身份／停止算法。旧生产反例精确失败于500且后代仍执行。
- Ruff、两文件 Pyright 零诊断、Python 编译、Node 语法及本 change strict 通过。准确命令、三个改动源码的前后指纹、原始输出和退出码见 `out/tests/temp/terminal_root_recovery_20261031/artifacts/verification.json`。集成先沿主仓闸门等待221.25秒，取得名额后沿原90秒／4GiB保护执行；未绕过另一会话的测试。
- 本片仅证明 Linux 已持久完整身份的原 SID 后代清理，不代无 PID 创建接管、未知 Worker 发现、Python lease、Windows、Gateway/Web 或完整 shutdown 期限；5.2／5.4 及整体21/23仍未完成。另一会话的 Web/Workbench、自动化、共享暂存和 debug 服务保持。

### 2026-10-31 Terminal／Browser 资源记录写入的异步结算（5.4 部分收口）

- 全部资源记录 writer 迁到唯一异步 `ResourceRecordStore.save`；文件写锁内复用原 shielded worker 完成候选合并、校验、JSON、fsync 和 replace，成功后才整体发布。原资源锁覆盖线程池排队和真实结算；新建 intent 发布前先登记原锁，冷恢复准备以唯一锁完成观察、原 lease 对账、版本冻结及 pending 发布。已落盘但外部同步未核实的版本复用原 pending，仅由显式 stop/delete 重试；fork 取消以原持久 committed 回执判断提交，unknown 收尾保留原失败。不增加 writer、schema、恢复账本或同步兼容入口。
- 656 项受影响单测通过（23.66 秒），其中新增 16 项覆盖两类 owner、并发合并、取消和重复取消、线程池排队、初始 intent、冷恢复准备、fork dispatching/committed 及失败收尾。完整旧生产归档加公开 owner 并发场景，精确因事件循环同步阻塞失败；未用直接 await 新接口的不兼容测试充当反例。
- 最终 4 项真实资源集成通过（55.48 秒，`run-e66c3374d582`），经正式 Session／ASGI 创建真实 PTY／Chromium，覆盖两类资源 × 完成／取消，核 peer 409 仍响应、request_id、worker 线程、原资源锁与 lease、外部身份／版本及同 owner 显式停止结算。共享 ASGI 装具补装正式 HTTPException handler，故障屏障用原 TraceMiddleware 的 request_id 区分管理请求和后台观察；原身份、版本和 lease 断言保持。既有单测以原锁释放确认 owner 提交，不把传输替身返回当作提交完成。
- Ruff、Python 编译及新增单测／ASGI 测试类型检查通过；扩大类型检查与完整 HEAD 基线按规则、完整消息和最窄语句 AST 比对，178 项均在原 188 项内，新增零项；共享装具的原 46 项诊断逐项相同，不称全量类型全绿。初版装具缺 request_id、故障屏障误拦后台观察、180 秒合并回归预算不足以及独占名额等待失败的原件保留，不放宽业务期限或绕过另一会话测试。
- 最后追加的记录错误／冷恢复／释放 25 项合并回归，按已测单项耗时将临时外层预算从 180 秒调整为 420 秒，但再次等待主仓独占名额满 600 秒，pytest 未启动；后续 fork／离线备份回归也未启动。前次 180 秒运行只保留已完成 11 项及退出 124 的原件，不计作全组通过。当前另一会话 Web 产品测试仍持闸门，停止同条件重复排队，以上两组保持未验证；`queue-regression-final-exit.json` 记录实际退出和待补命令，不用本批局部结果关闭完整验收。
- 准确命令、前后源码指纹、原始输出、实际退出码、类型对照与作者 AST 自查保存在 `out/tests/temp/resource_record_async_20261031/artifacts/`。同步冷加载、lease 写盘、不可抢占原生 I/O、旧代完整接管、独立 Backend 重启、Windows、Gateway/Web 及完整 shutdown 仍 TODO；初始 intent 写盘后取消仅证明保留 creating／原身份／原锁且无 lease 或外部创建，不证明该 intent 的完整接管。5.2／5.4 与整体 21/23 保持未完成。未修改另一会话的 Web/Workbench、自动化或暂存，不重启共享 debug；团队协作及 MCP 显式持有继续延期。

### 2026-10-31 首条 intent 取消后关闭尚未派发的创建（5.2 部分收口）

- 原调用在首条记录 worker 结算后仍能证明尚未进入外部创建回调时，持原资源锁与 Thread 准入受保护地关闭 intent，保存 deleted/verified、解除关联后才同步原账本 stopped 投影；只登记原资源身份，不补造 operation lease 或外部回执。重复取消不遗弃收尾，记录／账本写入失败保留原取消与原生错误；成功后的列表、详情和冷重建不再遗留活动 creating。原登记、lease 获取及已派发创建／失败回执处理 AST 保持不变。此证据补齐上一节仅验证保留 creating 的本地未派发边界；原调用已随 Backend 退出的旧 intent 不能凭清单缺失或缺 lease 推断未创建，代码和 5.2 继续 TODO。
- 662 项相关单测通过（29.14 秒，含 22 项写盘／取消定向子集），覆盖两类 owner、重复取消发生于第二次收尾写盘、记录及账本失败的 errno／原取消 context／原生 cause、持久顺序和冷读取。完整旧生产归档加相同公开 owner 场景，两类资源均精确因保留 creating 而失败，不以新私有 helper 的不存在充当反例。
- 新增 2 项真实 Session／ASGI／Node 场景通过（23.97 秒，`run-c71aeb5b8bef`）：先创建健康 PTY／Chromium，经原生写入屏障取消另一创建，核原锁、尚无登记／占用、终态、request_id、真实清单只有健康资源、原生身份／版本不变、记录与账本冷读，以及原健康资源仍可正式停止。原 4 项管理写盘场景在 `run-1106a97fb585` 通过，源码指纹与原函数／imports／fixture AST 复核后复用；不称单次 6 项全绿。首轮新增场景错误调用要求身份已登记的 lease 查询，改为精确核 `get(id) is None`，产品代码与其余断言未变，原失败保留。
- Ruff、Python 编译、单测与 ASGI 测试类型检查通过；生产保留 4 条与完整 HEAD 基线按规则、完整消息及最窄语句 AST 相同的既有诊断，新增零条，不称全量类型全绿。作者自查确认只有原首条写盘异常收尾及同 owner 新 helper 改变；既有 owner 超过 800 行的结构债务不据本片关闭。
- 上一边界追加的 25 项记录错误／冷恢复／释放回归在取得原共享闸门后运行，7 项通过，随后 Browser 损坏创建回执场景的业务结果文件、deleted/verified 与 lease 结清已完成，但后续收尾挂起，最终由原 420 秒进程外保护以 124 终止；不再以合并运行预算不足解释，也不计作整组通过。相同单项在完整旧生产和候选分别通过（15.17／13.35 秒），未复现且未取得挂起任务栈，gdb 被 ptrace 权限拒绝；不能据此判断为旧问题或本片回归。最小探针另确认 `run_sync_shielded` 成功 worker 后的收尾等待可能吞取消，留后续独立切片修复，不把它直接认定为上述挂起根因。
- 拆开的冷恢复／释放为 12 passed、3 装具错误（141.87 秒），Terminal 原始日志明确 EADDRINUSE，后两项也在启动前因占用被拒绝，不终止未知进程。fork 正式期限 suite 两项通过（38.55 秒）；正式诊断 suite 四项通过、四项在 fixture 读取已删除的 `workspace_owner.logger` 时出错（108.21 秒），不算整组通过。离线备份记录的 14 项定向验证通过（48.53 秒）。初次临时 pytest 调用被 fork 正式 watchdog 前置条件拒绝，旧产品备份输入也因归档未加载仓库 `.env` 失败；原件保留，修正为正式 matrix 与 `uv --env-file .env` 读取既有测试环境，不改配置或保存凭据内容。
- 本片命令、前后源码指纹、原始输出／实际退出码、旧生产反例、复用依据与类型对照保存在 `out/tests/temp/resource_unstarted_intent_20261031/artifacts/`。旧代接管、取消传播与收尾挂起、上述未通过验收、同步 lease I/O、原生线程中断、Backend 硬重启、Windows、Gateway/Web 和完整 shutdown 仍未验完；5.2／5.4 及整体 21/23 不勾选。未修改另一会话的 Web/Workbench、自动化或暂存，不重启共享 debug。

### 2026-10-31 同步 worker 已完成后的取消传播（5.4 部分收口）

- 确定性复现表明原 `run_sync_shielded` 在成功 worker 之后仍创建并等待 cleanup task，首次外部取消可能被用于既有取消收尾的 shield 吞掉：任务接受取消却返回42，或继续等待后续工作。现只在原等待收到取消时沿既有原语等待同一 worker 并重抛；正常结果／原生失败不再建立收尾 task 或额外 await。签名、同步操作、原锁和原生异常身份保持，`run_cleanup_shielded` AST 不变，不新建 worker、writer 或停止链。
- 606 项相关单测通过（49.94 秒，含新增5项），覆盖结果交付时取消、继续等待、原生执行期间重复取消、原锁持有、原生错误与原取消 context、参数及结果。完整旧生产归档加相同公开原语用例，两项反例均精确失败于已接受取消被吞，不以新私有接口缺失充当红测。Ruff、两文件 Pyright 零诊断和编译检查通过。
- 6 项正式 Session／ASGI／Node／真实 PTY 与 Chromium 记录写入集成在同一运行中全部通过（72.51 秒），覆盖管理写入完成／取消及未派发 intent 的两类 owner。原资源身份、版本、锁、精确 lease、冷读、列表／详情与同入口停止断言保持；集成沿原主仓闸门取得名额，不更改另一会话 Web／Workbench、其自动化或共享 debug。
- 新源码下再次运行原25项回归并加载只读任务快照，5项通过后在 Browser 授权账本失败场景的收尾挂起。多轮快照一致定位 `WorkspaceLifespan._close → TerminalSteeringService.shutdown → await task`，原 `terminal-steering-monitor` 已有取消计数1却仍在 `_run` 的轮询 sleep；这证明本片通用 worker 修复尚未解决全部关闭链。保留原快照后仅对本会话 timeout 监督器发送 SIGTERM，实际退出143，非360秒预算耗尽，不计整组通过；监控停止准入另作纵向切片，不用本次观察倒推前次无栈挂起的精确根因。
- 准确命令、完整归档及基线指纹、原始输出、实际退出码、只读插件身份和作者AST复核见 `out/tests/temp/shielded_worker_cancel_20261031/artifacts/`。Windows、Gateway/Web、旧代接管、不可抢占原生I/O及完整 shutdown 仍未验完，5.2／5.4和21/23保持；未解决的logger装具与端口错误亦不以本片通过项覆盖。

### 2026-10-31 Terminal steering 关闭后停止下一轮扫描（5.4 部分收口）

- 关闭先登记停止请求，再取消原监控一次并等待；保留原任务句柄到结算，重叠关闭等待同一任务。原扫描的受保护收尾即使消耗取消，返回后仍直接退出，不再进入轮询 sleep。显式重新 start 复位停止请求；单次扫描、完成通知、claim 和候选筛选 AST 保持不变。
- 13 项定向单测通过，完整旧生产归档加相同最终测试的两个反例均因关闭仍等待原监控而失败。25 项资源记录错误／冷恢复／释放回归全部通过（275.08 秒），真实 PTY 完成及目录回收单项通过（21.18 秒）；均先取得主仓共享闸门，沿原进程外超时及 4GiB 保护执行。本次通过补齐该组回归，不倒推此前无栈挂起的精确根因。
- Ruff、编译和 OpenSpec strict 通过；Pyright 保留一条与完整旧生产规则、完整消息及语句 AST 一致的既有 operator 诊断，新增测试零诊断。初版 fixture 的 Awaitable 返回类型已修正并集中重跑，原件保留。准确命令、源码指纹、退出码、类型对照、只读快照插件和作者审查见 `out/tests/temp/terminal_monitor_stop_20261031/artifacts/verification.json`。
- 关闭观察者取消、不合作扫描终止及原监控失败传播仍有代码 TODO，旧代接管、完整 shutdown 期限、Windows 与 Gateway/Web 未验完，5.2／5.4 与整体 21/23 不勾选。未修改另一会话的 Web/Workbench、自动化或暂存，不重启共享 Debug。

### 2026-10-31 Fork 诊断装具迁齐现有日志 owner

- 删除装具对已不存在的 `workspace_owner.logger` 的引用及唯一无用 import，继续将故障 handler 装到现有 fork service 与统一 failures logger。生产正文、真实日志写入故障、原错误身份／次数／注释、unknown lease、已提交关联和同 key 恢复断言均保持，不为测试恢复已删除的生产接口。
- 正式 `integration-fork-live-diagnostics` 全部 8 项通过（111.92 秒），保留原 180 秒／4GiB、fork watchdog 及主仓共享闸门。Ruff、编译、该文件 Pyright 零诊断及 OpenSpec strict 通过；准确命令、完整独立归档指纹、原始输出与退出码见 `out/tests/temp/fork_diagnostic_logger_20261031/artifacts/verification.json`。首条类型检查命令因 uv 环境无 pyright 而未启动检查器，改用既有 `bun x pyright`，未更改依赖。此前四项 fixture 错误已由本次补验收口，5.2／5.4 和 21/23 仍未关闭；未改另一会话 Web/Workbench、自动化、暂存或共享 Debug。

### 2026-10-31 Terminal 关闭观察取消与原失败交付（5.4 部分收口）

- 原 shutdown 只观察唯一监控任务，观察者取消直接交回并保留句柄，不再向扫描转发取消或吞掉观察取消。原任务结算后按身份移除句柄，非取消失败保持原异常身份交回 Workspace／Backend，后续依赖与维护锁保留；同一关闭入口重试继续后续阶段，不重跑失败监控。只修改原 shutdown，净减 6 行，扫描、通知、claim、筛选及原失败日志回调 AST 保持。
- 17 项单测、2 项新增双挂载 Backend 集成和 2 项既有回归通过。新集成由正式入口创建 Session／活动，核原异常身份、peer 完成、原 owner、关闭阶段、依赖／维护锁保留、重试及冷读；外设清单使用明确传输替身，真实目录读取产生原生错误，不据此声称新故障已完成真实 HTTP／PTY 验收。完整旧生产归档叠加相同测试，4 项单测和 1 项集成反例分别精确失败于吞错及观察取消不及时结束。既有回归包含真实 PTY 完成／目录释放；全部测试保留进程外期限、4GiB 与主仓共享闸门。
- Ruff、编译及 OpenSpec strict 通过；Pyright 原有 1 条 operator 诊断与完整基线的规则、完整消息和语句 AST 相同，新增测试零诊断。首轮集成 1 通过／1 装具失败，原因是 shield 转发已取消任务时丢失取消消息，改为先观察原任务结束再直接取得结果，保留原消息和 3 秒期限；最终 2 项重跑通过。首次旧生产集成因主仓名额等待满 600 秒而未启动，后续真实反例已补验，不能将排队失败当行为红测；没有中断其他会话或本片已取得名额的回归。命令、源码指纹、原始输出、实际退出码及差异复核见 `out/tests/temp/terminal_shutdown_observers_20261031/artifacts/verification.json`。不合作扫描终止、完整 shutdown 硬期限、旧代接管、Windows 及 Gateway/Web 仍未验完，5.2／5.4 与 21/23 保持；未修改另一会话 Web/Workbench、自动化、共享暂存或 Debug。

### 2026-10-31 Terminal 启动失败后的迟到身份与正式停止证明（5.2 部分收口）

- 原就绪等待失败或超时后，同一仍在收尾的 Worker 首次合法 spawned PID 仍由原句柄保留；已有身份、成功就绪或已退出 Worker 不被迟到创建替换。Worker 沿原进程树停止链返回绑定 PID 的 stopped 结果，发送结算后退出。Session 等原 Worker 退出，再读取迟到身份与正式清理结果；先持久化核实终态再签原创建失败回执，无证明仍保留 created/unknown、原 PID 和无失败回执。不另建模块、状态 writer、公开 DTO、schema 或账本。
- 167 项相关 Node 单测通过（6.90 秒）；最终产品行为和两份单测与该次运行字节一致，仅补一行期限 TODO，已机械核对去除此注释后的 SHA-256。完整旧生产归档叠加相同首批测试，5 项行为反例精确失败；最终真实 HTTP 反例进入业务体，明确失败于原产品查询 os_pid 为 null 而真实 PTY 已创建，不以新增 IPC 类型缺失充当反例。
- 最终 2 项迟到身份／丢证明的真实 HTTP/PTY 集成通过（14.57 秒），完整 8 项正式文件在原 60 秒／4GiB 保护内全部通过（47.10 秒）。核原 unknown/noPID 磁盘记录、真实父子与 Worker 退出、原版本、公开快照／持久记录及精确失败回执；丢证明即使真实进程已停止也不结清。所有集成先取得主仓共享闸门，旧反例和候选串行执行。
- 初版强杀正向场景的失败已查明：实机 /proc 约 6381 项，三轮扫描加 1.5 秒宽限使正式证明晚于宿主 3 秒强杀期限，保留 unknown 符合合同；正向迟到证明场景改用配合 TERM 退出的真实父子，原忽略 TERM 的强杀及停止失败重试场景和断言保持。扫描成本、统一总期限及缺证明后的管理接管保留代码 TODO 与原始时间线，不扩超时、不修改停止算法，也不据此宣称性能问题已解决。一次临时诊断 preload 提前注册 message listener 消费了 start，修正为只观察原 emit 后取得真实时间线，最终已撤回全部临时观察代码。
- 首轮完整集成出现 6 项完成／2 项失败后因 60 秒退出124；同轮旧版本因端口占用在 setup 拒绝，不能算行为红测。最终候选首次排队满600秒未启动，随后共享名额释放才重试；原失败、工具阶段和真实退出码均保留。最终 8 项通过补齐该文件验收，不倒推此前没有停止证明即意味着原生进程已停止。
- Ruff、编译、Python 集成文件 Pyright 零诊断、Node 语法及 OpenSpec strict 通过；作者 AST 自查只改变原 start、Worker shutdown 和 Session failStartup，既有超过800行 Session 的结构债务仍保留，未冒充独立审查。准确命令、源码指纹、原始输出／退出码、反例、时间线、差异与复用依据见 `out/tests/temp/terminal_late_startup_20261031/artifacts/verification.json`。native 始终无身份、Backend 已退出、丢回执后接管、Windows、Gateway/Web 与完整 shutdown 仍未验完，5.2／5.4 和21/23保持；没有修改另一会话的 Web/Workbench、自动化、共享暂存或 Debug。

### 2026-10-31 Terminal 原生创建身份与丢证明后的管理恢复（5.2 部分收口）

- Worker 原有一次元数据读取经同一 IPC 交付原 PID 的进程组、系统 session 和启动标记，读取与原25毫秒最小观察均结束后才 ready；没有元数据明确交付 null。宿主只收原 PID 匹配的首次身份，错误、迟到、重复或已退出 Worker 不替换原标记。Session 正常启动、失败收尾与停止失败的 unknown 落盘共用同一身份捕获，移除成功启动后重新采样当前进程作为创建身份的旧读取，不新增产品模块、状态 writer、账本、schema 或公开字段。
- Worker 关闭本身失败时也保存新到身份；只有原身份字段发生变化才追加既有 writer 的保存。保存再次失败按发生顺序保留创建、关闭、存储原错，保留原句柄与 unknown，不将身份到达当作停止证明，也不重放清理。原生无身份、Python operation/目录 lease、整个关闭硬期限仍未收口。
- 最终相关 Node 单测176 passed（8.99秒），含原 cwd/HTTP线程池、记录/重试、未知管理及新增身份/失败窗口；完整旧生产叠加相同最终测试的7项反例均因原身份未保存、遗漏写盘或原错链缺失精确失败。旧生产真实 HTTP/PTY 反例进入业务体，核到原 unknown 记录的进程组为 null、与实际原生身份不符；未用新增私有接口缺失、setup 或端口错误冒充反例。
- 最终真实 HTTP/PTY 集成：迟到身份、丢停止证明、同Manager/精确Node硬重启、kill/delete和重复请求4 passed（12.64秒）；原启动失败/强杀/日志/cwd完整8 passed（47.38秒）；原根退出仍沿SID核实真实后代及冷管理4 passed（23.86秒）。三组均沿原主仓共享闸门串行，分别保留60秒/4GiB保护；完整状态经真实创建生成，重启不补写记录或身份，核原 native标记、owner版本、unknown/终态、持久字节及无新执行。首轮在追加失败窗口保存之前也有173项单测和相同三组通过，修改后已集中补跑，数字不重复累计。
- Ruff、编译、两Python集成文件 Pyright零诊断、对应Node语法及 OpenSpec strict通过。初版新测试的局部final变量有一条可能未绑定诊断，改为沿真实已查询快照初始化，未改变2次管理请求、身份或终态断言，最终检查通过；原输出保留。作者 AST 自查只涉及原构造/start、Worker handleStart、Session start/停止失败/身份捕获/启动失败，原其余方法保持；既有Session超过800行结构债务未据此关闭，未冒充独立审查。
- 命令、完整固定HEAD归档、源码指纹、原始输出/实际退出码、7项单测反例、真实HTTP反例、结构与差异复核见 `out/tests/temp/terminal_identity_recovery_20261031/artifacts/verification.json`。native始终不交付身份、Workspace Backend退出后的Python lease/目录占用接管、完整operation/shutdown硬期限、Windows与Gateway/Web仍未验完，5.2／5.4和21/23保持。没有修改另一会话的Web/Workbench、自动化测试或共享Debug；只提交本片文件，其他暂存保持。

### 2026-10-31 启动未知操作跨 Backend 硬重启的占用验收（5.2 部分）

- 正式 Gateway/Backend 消息入口及 model_stream 生成附件和 canonical 历史，原管理创建启动真实 PTY 父子；沿原 SHELL 配置复用启动故障装具，只丢停止证明。两次 Backend 硬重启、到期和普通查询均保留 created/unknown、原 operation 与目录 lease；原管理停止核实后才结清，后续重启回收目录，原身份、历史字节和执行次数保持。新集成 1 passed（90.16 秒），原启动失败完整 8 passed（46.73 秒）；主仓共享闸门串行，保留进程外 180/60 秒及 4GiB。
- Ruff、Pyright 零诊断及 Node 语法通过。固定 HEAD 完整归档、源码指纹、原输出和退出码见 `out/tests/temp/terminal_startup_backend_recovery_20261031/artifacts/verification.json`。没有新产品实现或第二套故障/持久化链；native 始终无身份、原调用未派发即消失、不合作操作总期限、Windows 和 Web 仍未验完，5.2/5.4 与 21/23 保持。未修改另一会话 Web/Workbench、其自动化、共享暂存或 Debug。

### 2026-10-31 Terminal 原生创建前发布恢复意图（5.2 部分）

- 原 Manager 在 Session.start 前沿唯一 writer 发布 created/unknown，原就绪及身份到达后再核实；写入失败只有仍在场的原调用证明未派发，才持久关闭同一身份并签 not_created。收尾再次失败保留 created/unknown、按发生顺序的两份原错及无回执，不新增 schema、账本或恢复链。
- 180 项 Node 单测、真实 HTTP/PTY 硬退出恢复 1 项、原信号/致命错误 6 项及原启动失败完整 8 项通过（9.27/12.07/57.57/47.51 秒）。完整旧生产叠加同一最终测试的 4 项单测反例及 1 项真实 HTTP 反例明确失败；真实反例在已创建 PTY 后核到空恢复列表，未执行后续硬退出步骤。原信号装具将方法重入计数改为真实 Worker shutdown IPC 计数，保持每个原 PID 一次、共两次及保留 PTY 的原断言；旧生产复现原装具失败，改正观测后同一旧生产 SIGTERM 通过。
- Ruff、Python 编译/类型、Node 语法、作者 AST 自查及 OpenSpec strict 通过；命令、源码指纹、原输出/退出码、装具归因和取消排队说明见 `out/tests/temp/terminal_creation_intent_20261031/artifacts/verification.json`。集成沿原主仓共享闸门串行运行，保留进程外期限与 4GiB；未修改另一会话 Web/Workbench、自动化、暂存或共享 Debug。无原生身份接管、原 Backend 调用消失、完整 operation/shutdown 硬期限及 Windows 仍未验完，5.2/5.4 与 21/23 保持。

### 2026-10-31 Terminal 未核实删除的历史保留（5.2 部分）

- 原裁剪入口只将 deleted/verified 纳入既有256条配额；unknown/reconcile_required 保留同一身份、版本和恢复正文。原近期历史装具补齐真实 Session 已有的核实字段，保留原数量与淘汰顺序断言；不增加 writer、schema、模块或第二套裁剪链。
- 182 项相关 Node 单测通过（9.82 秒），完整旧生产叠加最终测试的两个反例均失败于冷启动丢失待核实记录。新增验证使用历史记录测试原保存、冷启动、裁剪及身份冲突拒绝，只证明该筛选/恢复链，不冒充真实进程生成状态或完整接管验收。Node 语法、作者 AST 自查及 OpenSpec strict 通过，准确命令、源码指纹和原输出/退出码见 `out/tests/temp/terminal_unknown_retention_20261031/artifacts/verification.json`。未改 Web/Workbench、自动化、共享暂存或 Debug，5.2/5.4 与21/23继续未完成。

### 2026-10-31 Browser 原生创建前发布恢复意图（5.2 部分）

- 原Manager在Session.start前沿唯一writer发布created/unknown，首次导航完成后才核实。未派发的意图保存失败复用原failStartup/close，持久保存failed/verified后才签not_created；收尾失败保留unknown、原错误链和无回执。created的close/delete明确409，不用空context伪报终态。创建测试迁入独立文件，原Manager测试1016→775行；既有迁移测试正文经AST核对，仅原收尾写入故障调整为允许新的首条意图保存，原错误/cause/无回执断言保持。
- 官方Node/Bun分流runner分别305/469项通过（整组12.39秒）；真实HTTP/Chromium硬退出恢复1项、原启动诊断2项及原关闭/超时/冷恢复脚本10场景通过（4.04/6.28/32.87秒）。完整旧生产叠相同最终测试，5项单测与1项真实HTTP反例精确失败；后者在真实context已创建时核到空恢复列表，未进入后续SIGKILL步骤。集成均沿主仓闸门串行并保留原进程外期限与4GiB。
- 首轮误用Bun直接运行混合目录，触发现有node:test的t.mock不支持，另有新测试把尚未出现的started_at误写为null；修正新预期并改用已有分流runner后全部通过，不修改既有Node测试。Ruff、Python编译/Pyright零诊断、Node语法、作者AST自查及OpenSpec strict通过；准确命令、固定完整归档与源码指纹、原输出/退出码及失败归因见 `out/tests/temp/browser_creation_intent_20261031/artifacts/verification.json`。原context身份的旧代接管、其它冷unknown窗口、Python lease、完整operation/shutdown期限、Windows与Gateway/主Web仍未验完，5.2/5.4与21/23保持。未修改另一会话Web/Workbench、自动化、共享暂存或Debug。

### 2026-10-31 附件工作目录准入的原生访问结算（5.4 部分）

- 来源识别和目录 holder 内的可用性／locator／原生目录检查统一复用原 shielded worker，保留 Gateway／request 上下文及原判断；holder 覆盖线程池排队、执行及重复取消结算。取消不继续发布目录占用或创建 PTY，但此阶段已有原 Terminal intent 和 operation lease，仍由原创建失败链处理；不把前一 cwd 解析切片的“零 intent”证明扩大到本阶段。不新增解析链、writer、schema 或账本。
- 214 项相关单测通过（31.15 秒，含新增 11 项），覆盖识别／可用性／目录检查、成功／原失败／重复取消及两处线程池排队。完整旧生产归档叠加相同新增测试，来源识别反例精确失败于同步阻塞事件循环。3 项原准入真实集成通过；最终独立附件场景 2 项通过（36.65 秒），从正式消息上传生成目录，经 ASGI 资源入口和真实 Node HTTP／PTY 验 peer 可读、原 intent／lease／holder、真实 cwd、持久目录占用及正式 detach/delete 结清。原准入文件逐字节恢复，不增加既有 90 秒 suite 预算；新增文件由 integration-python 发现。
- 初版附件集成因测试查询尚未登记的资源而失败；按原账本合同改验未登记身份后通过，不修改产品语义或原身份断言。一次补验仅等待另一会话共享闸门，主动终止本会话监督器并保留退出 143，未进入测试体；最终先等待同一闸门释放，再由原 fixture 准入串行执行，仍有进程外 180 秒／4GiB 保护。拆分装具时一次末尾空行保护断言拒绝覆盖，后续静态检查因文件尚未生成退出，未进入测试；修正装具后最终检查通过，失败原件保留。
- Ruff、compileall 和 OpenSpec strict 通过；Pyright 的 2 条生产诊断与完整旧生产逐项一致，无新增类型诊断，不称类型全绿。准确命令、源码指纹、原始输出、实际退出码、类型对照及作者 AST 自查见 `out/tests/temp/attachment_cwd_admission_20261031/artifacts/verification.json`；完整旧源码临时副本已清理，其测试产物和指纹保留。同步 lease 写盘、原生 I/O 抢占、旧代完整接管、整个 operation／shutdown 期限及 Windows／Gateway／主 Web 未验，5.2／5.4 和整体 21/23 保持未完成；未修改另一会话的 Web／Workbench、自动化或共享暂存，不重启共享 debug。


### 2026-10-31 Browser 创建期间拒绝管理关闭不得污染交互准入（5.2 部分）

- created 状态的 close/delete 在设置 closingRequested 前保持原 409/browser_creation_unverified；拒绝不改变磁盘、原创建 Promise 或后续交互准入。只移动原拒绝分支，不新增恢复链、状态或账本；无原 context 身份的旧代接管继续 TODO。
- 相关 30 项单测通过；新增断言在原生产实现上精确红测（16 通过、1 失败）。2 项真实 Node HTTP/Chromium 集成通过（4.49 秒），沿主仓独占闸门串行：暂扣真实 context 回执后分别拒绝 close/delete，释放原回执后同一 ID/版本完成创建、正式 navigate 新增页面、正式关闭并释放 context；既有硬退出/冷启动场景继续保留原意图及未知身份保护。外部 900 秒期限包含排队，各进程保留 4GiB 数据段保护。
- Ruff、Python 编译、定向 Pyright 零诊断、Node 语法、作者差异自查及 OpenSpec strict 通过；准确命令、源码指纹、原始输出及实际退出码见 `out/tests/temp/browser_creation_rejection_20261031/artifacts/verification.json`。这不代表 Python owner/lease、无身份接管、完整 operation/shutdown 期限、Windows 或 Gateway/主 Web 验收，5.2/5.4 与整体 21/23 保持未完成。未修改另一会话 Web/Workbench、自动化或共享暂存，未重启共享 debug。


### 2026-10-31 Terminal 完成监控的关闭观察期限与原错传播（5.4 部分）

- 原 shutdown 通过既有 asyncio.wait 每次最多观察同一任务30秒，到期保留原句柄、关闭准入及后续Workspace依赖；重入不再取消原扫描或重启监控。原轮询中的可重试错误在关闭期间直接交回原异常，正常轮询继续沿原周期重试；不新增状态、账本或调度链。
- 17项相关单测通过；原无限等待反例红测失败，补齐预算后超时/连接错误被消耗的两项反例仍精确失败，补齐原错传播后全部通过。4项真实双Workspace生命周期集成通过（59.76秒），默认期限场景实际等待30秒，核首次关闭取消、重入不新增取消、原任务/Session locator/存储/维护锁保留、迟到原生错误及同一生命周期owner重试完成；关键Session与活动记录由产品入口生成，外设回执仅提供屏障与故障。
- 初次集成在新测试的取消总数断言失败（3通过、1失败）：列表与恢复的两个原30秒预算也会取消同一监控。按源码和现场证据将“首次关闭仅一次取消”的精确断言移至其期限前，再核到期后重入不增加取消，未放宽资源身份、原错或截止断言。一次先前排队运行因补齐已发现的关闭期吞错而仅终止本次pytest，退出143且未进入测试体；未停止其它会话测试。原件均保留。
- Ruff、compileall、OpenSpec strict与作者差异/AST自查通过；Pyright基线和最终各1条既有诊断，规则、完整消息、最窄AST owner和表达式一致，无新增，不称类型全绿。准确命令、源码指纹、原始输出、实际退出码、失败归因及类型对照见 `out/tests/temp/terminal_monitor_deadline_20261031/artifacts/verification.json`。
- 集成沿原跨worktree独占闸门串行，进程外900秒包括排队且保留4GiB保护。未验证原生I/O抢占、无身份接管、整个operation/shutdown硬期限、Terminal进程终止、Windows或Gateway/主Web；5.2/5.4与整体21/23继续保持未完成。未修改另一会话Web/Workbench、自动化或共享暂存，未重启共享debug。

### 2026-10-31 Agent驻留扫描关闭的原任务持有（5.4部分）

- 唯一关闭任务持有原扫描与tracker排空，每次调用最多观察30秒；取消观察或到期不丢句柄、不再次取消扫描、不重开准入。扫描结算后才清缓存，关闭期原错不按周期重试消耗，迟到失败先交付后再允许显式重试剩余关闭。原实现的两项红测准确失败于扫描句柄提前清空；最终28项相关单测通过，覆盖观察取消、迟到原错和扫描/scope等待到期。单元到期场景缩短常量，只证明等待机制，不冒充默认30秒实测。
- 首次11项集成因独占名额等待600秒全部fixture失败，未进入测试体；更早本片排队进程exit143也不计业务结果。实际名额释放后补验为10 passed／1 failed（155.24秒）：新增3项双Workspace场景均通过，真实默认30秒、观察取消、迟到原错、Backend归属、Session locator/存储/维护锁保留及同owner重试已有实测。唯一失败首错是原lifespan装具替换配置启动，导致编译前Skill source未激活，后续scope断言因未编译再次失败；相关7份源码与驻留修复前HEAD逐字相同，此为静态归因，未冒称旧生产动态复现。
- 仅在该lifespan用例恢复真实配置启动/关闭，让产品入口激活Skill source；不手工预激活、不改产品、不放宽冷启动/释放顺序断言。补跑此项1 passed／13.80秒，其余10项按未变化源码复用，不称最终11项整批重跑。两次业务运行各2451份采样源码保持；沿原跨worktree闸门串行，未绕锁或停止另一会话进程。
- Ruff、compileall及OpenSpec strict通过；原生产4条Pyright诊断与基线规则、完整消息和语句AST一致，两份原测试零诊断；本次修正的runner测试78条既存诊断前后同规则/消息/语句AST，无新增，不称类型全绿。准确命令、源码指纹、原始输出/退出码及差异自查见`out/tests/temp/residency_shutdown_owner_20261031/artifacts/verification.json`。原生I/O抢占、外部停止、完整shutdown硬期限、Windows及Gateway/主Web继续TODO，5.2/5.4与21/23不勾选；未修改Web/Workbench、其自动化、共享暂存或debug。

### 2026-10-31 启动停止证明丢失后的 Python 目录占用（5.2部分验收）

- 正式消息上传、Python Terminal 创建及真实 Node HTTP/PTY 生成目录和两份原占用；只在既有 IPC 故障边界丢弃停止证明。原 Node 与精确 Node 硬重启各一例，核 created/unknown、原生创建身份、原 operation 待核实及目录 lease 有效、独立进程观察与到期 GC 拒绝；不以进程已退出作为停止证明。
- 原 Python 管理停止核实并持久发布终态后，冷读唯一账本确认两份精确 lease 结清，目录才被回收；原工作负载、来源身份及恢复记录保持，随后 detach/delete 完成原资源清理。2 passed／302.65秒（包含共享名额排队）、exit0；首轮120秒全部耗在排队、exit124，未进入测试体，不计业务失败或通过。两轮均保留4GiB进程外保护，不绕闸门、不停止另一会话进程。
- 最终测试Ruff、Pyright零诊断及compileall通过，1217份采样源码在第二轮前后逐字一致。准确命令、原始输出、源码指纹与作者自查在`out/tests/temp/startup_directory_recovery_20261031/artifacts/verification.json`；通用integration-python自动发现新文件，未增加其它suite预算。没有修改产品实现、Web/Workbench、其自动化或共享debug；Python Backend硬重启、无原生身份接管、Windows、Gateway/Web与完整operation/shutdown期限仍未验证，5.2/5.4及21/23保持未完成。

### 2026-10-31 工具operation准入写盘的原任务结算（5.4部分）

- 原lease获取与成功后的引用登记交给同一shielded worker；排队时取消/关闭不开始新写入，已开始的写入持原锁结算。关闭覆盖尚无lease引用的原任务，取消保留原消息、迟到存储错误及已提交占用，关闭后不再派发工具。
- 定向82项、相关owner回归569项通过，回归34份采样源码前后一致。完整旧生产归档的两项反例复现事件循环阻塞；仅移除关闭对在途准入的跟踪，两项变异反例均失败。Ruff、compileall、两文件Pyright及OpenSpec strict通过；准确命令、原始输出、退出码、指纹及作者AST自查见`out/tests/temp/operation_admission_io_20261031/artifacts/verification.json`。
- 使用真实owner记录和lease、替身传输，不证明真实PTY/Chromium终态；其它同步writer、通知的事件循环归属、原生I/O抢占及完整operation/shutdown期限继续TODO。5.2/5.4与21/23保持未完成；未修改另一会话Web/Workbench、其自动化、共享暂存或debug。

### 2026-10-31 工具失败的账本及owner记录收尾（5.4部分）

- 原unknown账本写入改用shielded worker，与后续原owner记录save共用一次取消保护；原资源锁和holder覆盖排队与结算，后续重复取消不割裂两步、不替换原工具错误或取消。不新增writer、账本或状态格式。
- 新增20项、连同记录收尾42项及相关owner589项通过，回归35份源码前后一致。完整旧生产归档的两项反例复现同步阻塞；移除整段收尾保护的两项变异反例精确失败于原错误被新取消替换。Ruff、compileall、strict通过；生产原4条Pyright诊断的规则、完整消息、文件归属和最窄AST表达式逐项相同，新增测试零诊断。首轮20项均因新测试误用冷store构造参数失败，修正装具后通过，未修改产品合同或放宽原错误/lease断言。
- 准确命令、原始输出、退出码、指纹与作者AST自查见`out/tests/temp/operation_unknown_io_20261031/artifacts/verification.json`。传输使用替身，账本自身写盘失败只验原错、原件及显式重试，不证明旧代接管；真实PTY/Chromium终态、其它同步writer与完整关闭期限继续TODO。5.2/5.4及21/23未完成，另一会话Web/Workbench、其自动化、共享暂存和debug未动。

### 2026-10-31 owner状态发布的异步账本结算（5.4部分）

- 唯一`_sync_ledger`改为异步入口，14处生产调用全部直接等待；先保存原owner记录，再等待原账本worker结算后投影。资源锁覆盖线程池排队与写盘，取消和重复取消保留原消息及迟到存储错误。Terminal目录lease终态通知仍在原事件循环线程，不将其它writer或通知迁移冒充完成。
- 新增40项，定向146项及相关回归989项通过，47份采样源码在回归前后相同；随后仅捕获原pending map以修复闭包类型收窄，并补跑52项冷恢复/投影测试。首轮回归6项取消顺序失败已修复：外部同步成功后，原账本发布和移除pending共用受保护提交，取消不再导致重复同步；原发布失败保留同版本供显式重试。
- 另一首轮失败为既存API测试与规范不符；完整旧生产复现同一400/409差异，仅调整显式stop重试原冻结版本的断言，并加强完整错误正文和精确请求次数检查，不改产品行为。完整旧生产6项反例复现同步阻塞；仅删除两领域工具调用的await，2项最终变异反例均失败。初次变异参数选择错误退出4，未进入测试体，不计有效反例。
- Ruff、compileall及OpenSpec strict通过；最终Pyright与原生产各21条既存诊断，文件、规则、完整消息、词法owner及最窄AST表达式逐项相同，新测试零诊断，不称类型全绿。准确命令、原始输出、退出码、指纹、类型对照及作者结构自查见`out/tests/temp/ledger_projection_io_20261031/artifacts/verification.json`。
- TODO(5.2)：`out/tests/temp/unknown_write_cleanup_window_20261031/artifacts/probe-final.json`已实证unknown首步账本写失败后的释放窗口：恢复存储后，普通operation.close仍把两领域原active lease改为released，而外部记录仍running；通用Turn收尾也释放active，仅改工具finally不足。探针使用原owner/账本及替身传输，不证明真实进程或Backend硬重启。仍需在原账本和owner链解决派发未核实的持久保护，不能新增第二账本或把写失败当作unknown已持久。
- 真实PTY/Chromium、旧代接管、其它同步writer、原生I/O抢占及完整operation/shutdown期限继续TODO，5.2/5.4及21/23保持未完成；未修改另一会话Web/Workbench、其自动化或共享debug。

### 2026-10-31 普通工具派发的原lease持久保护（5.2/5.4部分）

- Terminal普通调用、workspace完成查询与Browser动作（工具/既有HTTP入口）共用原派发保护；同一JSON账本的原lease先持久进入dispatching，正式回执校验、owner记录和账本投影提交后恢复active。unknown首步账本写失败仍保留原dispatching，operation和Turn收尾不再释放；普通查询、冷owner重建与显式停止沿原身份核实，不增加账本、记录副本、生成结构或数据库迁移。
- 准备写失败不派发；排队取消/关闭不开始新写入，已开始的原写入持锁结算。原调用证明尚未进入外部回调时才撤回准备，撤回失败保留占用与原取消。准备写入期间close开始后重核原operation准入，拒绝迟到外部调用；收尾不补造unknown或外部回执。
- 新增37项；定向241项、补齐准入装具后的271项及最终相关回归1051项通过，采样源码前后一致。首轮56项失败中52项是Browser回调装具仍断言active、4项是新线程池装具在原错误产生前取消，已分别按派发阶段和原错误事件修正；随后8项缺失replace导入、扩大回归4项准入writer计数失败也已修复。准入与回执故障点各自限定原阶段，保留精确写入次数、原错误、身份及期限断言，不把新增派发/确认写入混算为获取原lease。
- 完整旧生产归档8项反例准确复现普通收尾将active改为released；完整候选归档仅放开Turn对dispatching的释放，8项变异反例失败；仅删除准备写盘后的operation准入重核，2项变异反例失败。Ruff、compileall与strict通过；最终Pyright和旧生产各19条既存诊断，规则、完整消息、文件、词法owner和最窄AST表达式逐项一致，新测试零诊断，不称类型全绿。
- 同一资源/operation顺序100次读取的本机替身传输测量：Terminal中位1.66→3.96ms，Browser 2.35→6.11ms；每领域原owner仍写100次，账本新增200次快照写入，分别194500/196500字节。TODO(性能)：测量大账本重写和调度成本后再优化原writer，保留派发前及回执后两个持久边界；此小工作负载不代表真实HTTP/PTY/Chromium或全链路性能。
- 原始输出、准确命令、源码指纹、退出码、反例/变异补丁、测量及作者结构自查见`out/tests/temp/operation_dispatch_receipt_20261031/artifacts/verification.json`。传输和Session定位使用既有替身，仅证明原owner/账本及协议入口单测；真实进程、Backend硬重启、Windows、创建/版本管理/传输授权的首步写失败保护及完整operation/shutdown仍TODO。5.2/5.4与本变更21/23保持未完成，未修改另一会话Web/Workbench、其自动化、共享暂存或debug。

### 2026-10-31 创建派发与首条intent的原operation保护（5.2/5.4部分）

- 两领域创建迁入唯一共享`creation.py`，移除原owner三个私有方法及逐层重复身份参数，复用同一operation、原lease获取和dispatching派发链。首条intent写盘前登记原锁及operation在途集合，close冻结并等待原锁；取消/关闭不迟到登记、取得lease或外部创建，已开始写盘必须结算。原调用证明未进入回调后才提交deleted/verified、stopped并结清已取得的精确lease。cwd解析后拒绝迟到创建，未声称close已等待整个cwd阶段。
- 新增61项；相关回归1112 passed／59.88秒，采样源码前后稳定。随后仅修正新增测试的闭包类型收窄，并将派发状态断言移至外部创建和普通收尾之后，让反例直接证明真实释放窗口；修改后18项派发测试全部通过，其余回归按未变化源码复用，不称最终1112项整批重跑。
- 完整旧生产归档叠加最终测试，8项反例核到回执丢失且unknown首步写失败后原active被普通close改成released；另外2项准确核到首条intent仍写盘时close提前返回。完整候选归档仅恢复creating清单隐式核实的两项变异反例失败于普通查询返回verified，最终生产只允许原创建入口明确消费创建回执完成首次核实。
- 首轮12项失败中8项复现普通查询误核实创建结果，另4项为新增装具误判原记录writer的迟到错误链、查询尚未登记的资源；按原合同修正装具，保留精确错误、取消消息、原lease/Thread/holder/operation/Turn身份及调用次数。另一次初版红测在外部回调前先核派发状态，只证明缺少保护，未冒称已证明释放窗口；最终红测将检查移至实际创建和失败写盘之后，原件全部保留。
- Ruff、compileall与OpenSpec strict通过；本轮Pyright范围原/新生产各12条既存诊断，按文件、规则、完整消息、词法owner和最窄AST逐项一致，新测试零诊断，不称类型全绿。原始命令、源码指纹、退出码、类型对照和作者结构自查见`out/tests/temp/resource_creation_dispatch_20261031/artifacts/verification.json`。使用原owner/账本和替身传输，不证明真实PTY/Chromium、Backend硬退出、旧代完整接管、Windows或完整operation/shutdown期限；版本管理与传输授权首步写失败保护继续TODO。5.2/5.4及整体21/23保持未完成，未修改另一会话Web/Workbench、其自动化、共享暂存或debug。

### 2026-10-31 传输授权的原lease派发保护（5.2/5.4部分）

- 两领域原授权入口从运行快照读取前复用同一dispatching保护，删除快照、签发和拒绝校验的重复unknown收尾，不新增身份、账本、schema或签发链。短暂写盘失败时管理finally不误释放；持续失败也不再尝试释放并用次错覆盖原失败。已核实预检冲突和正式未签发拒绝先提交观察及同一lease回执，再返回既有ResourceConflict/cause；无效拒绝与投影错误仍沿原unknown链保护占用。
- 新增64项；定向130项及最终相关回归1176项通过（61.26秒），采样源码前后稳定。覆盖单次/持续原生replace故障、原错误/取消消息、原lease/Thread/holder/operation身份、同owner与冷重建后显式停止、正常签发、预检冲突、正式拒绝/六类无效快照、准备写入取消/失败及线程池排队不迟到签发。既有期限测试只将外部回调阶段的原lease断言由active改为dispatching，其余身份、期限、冷读和终态断言保持。
- 完整旧生产归档叠加最终测试的8项反例，准确复现正式签发回执丢失且unknown首步写盘短暂失败后原lease已released。初版持续故障红测先失败于finally释放的次错替换原异常，仅证明错误链窗口；补齐单次故障后直接核到误释放，原件均保留。完整候选仅把正式拒绝冲突放回unknown保护体内的变异，核正式拒绝不得误标unknown。
- Ruff、compileall与OpenSpec strict通过。Pyright本轮选定生产及既有期限测试原/新各38条既存诊断，按文件、规则、完整消息、词法owner和最窄AST逐项一致；新增三份测试零诊断，不称类型全绿。准确命令、指纹、原始输出、退出码、类型对照、反例及作者结构自查见`out/tests/temp/resource_grant_dispatch_20261031/artifacts/verification.json`。传输使用既有替身，不代表真实HTTP/PTY/Chromium、Backend硬退出或Windows验收；同步管理lease获取/释放、版本管理首步失败保护、完整旧代接管与整个operation/shutdown期限仍TODO。5.2/5.4及整体21/23保持未完成；另一会话Web/Workbench、自动化、共享暂存与debug未动。

### 2026-10-31 管理版本与停止的原lease派发保护（5.2/5.4部分）

- promote/attach/detach、fork关联、stop/delete及Thread/Session释放统一复用原dispatching保护，删除逐层重复unknown收尾；停止首条状态保存也受保护。原版本已发布或外部已更新但回执未知时，即使unknown账本/owner写盘持续失败，管理finally与Turn也不误释放原占用；同owner及冷恢复由新管理身份沿原链核实。
- fork提交后取消由共享派发链读取原committed回执，复用原完整资源/holder/operation/Thread身份校验，再沿唯一账本一次提交直接释放；不经过active中间写入，确认失败保留原取消及cause、原lease与提交版本。同key冷恢复不重复关联。正式停止只settle此前未知占用，本次连续工具恢复同一active lease直到原operation收尾。
- 新增115项管理链和4项原账本故障用例；最终相关后端回归1409 passed／77.48秒，采样源码前后稳定。既有期限/原生fork写盘/停止/回收测试仅迁移派发阶段active→dispatching；原线程池排队测试明确在记录writer入队时占池，继续核原锁、取消、版本和终态。初轮扩大回归1398 passed、11 failed均据原输出完成此阶段迁移，未放宽期限、身份或终态断言。
- 完整旧生产归档4项反例重现失败收尾后管理finally再次尝试释放并覆盖原失败；完整候选定点变异在身份校验前恢复active，2项反例检出原lease被误释放。Ruff、compileall、OpenSpec strict通过；选定生产及受影响测试Pyright原/新各36条既存诊断，按文件、规则、完整消息、词法owner及最窄AST逐项一致，新管理测试零诊断。准确命令、指纹、原始输出/退出码、类型对照、变异补丁和作者AST自查见`out/tests/temp/resource_management_dispatch_20261031/artifacts/verification.json`。
- 本轮使用原owner/账本和既有传输替身，不证明真实HTTP/PTY/Chromium、Backend硬退出或Windows行为；同步管理lease获取/释放、完整旧代接管、原生I/O抢占及整个operation/shutdown期限仍TODO。5.2/5.4及整体21/23保持未完成；未修改Web/Workbench、其自动化或共享debug，不纳入另一会话的改动及暂存。

### 2026-10-31 管理lease获取/释放的原worker结算（5.4部分）

- 原管理lease入口统一改为异步上下文，五组命令/授权/Browser动作/Thread和Session释放/回收调用方迁齐，删除同步入口。原资源锁及适用Thread holder覆盖worker排队、原生获取和释放；排队取消不迟到取得或派发，已开始获取由原调用结算后释放精确lease。连续工具传入的原lease保持自身operation收尾，不重取、不代释放。业务错误收尾屏蔽后续取消，正常释放取消保留首次消息及迟到写盘cause；释放失败保留原active占用，不重发业务动作补做。
- 新增52项：40项跨Terminal/Browser命令、授权及Browser动作的获取/释放原生replace屏障，覆盖重复取消、迟到原错、精确身份、原锁和独立Session删除holder；4项冲突后释放覆盖后续取消/写盘失败不覆盖原ResourceConflict；8项单线程池排队覆盖原30秒预算/取消、原件零变更及零迟到lease/派发。定向52项及相关377项通过，最终完整相关后端回归1461 passed／80.26秒；采样源码前后稳定。
- 完整旧生产归档2项在原同步写盘屏障中触发2秒事件循环观察超时，原生屏障自带5秒释放上限；完整候选只删除排队取消检查，2项反例检出迟到新增lease导致原账本字节变化。Ruff、compileall与OpenSpec strict通过；Pyright本轮生产/新测试范围原新各6条既存诊断，按文件、规则、完整消息、词法owner与最窄AST逐项一致，新测试零诊断。准确命令、指纹、原始输出/退出码、变异补丁、类型对照与作者AST审查见`out/tests/temp/management_lease_io_20261031/artifacts/verification.json`。
- 原JSON账本仍串行发布，不承诺无关写请求可绕过原生慢盘；原生I/O不可抢占，其它同步writer/通知归属、完整旧代接管及整个operation/shutdown硬期限继续TODO。验证使用原owner/账本和既有传输替身，不证明真实HTTP/PTY/Chromium、Backend硬退出或Windows。5.2/5.4及整体21/23保持未完成；未修改另一会话Web/Workbench、其自动化或共享debug，未纳入其改动及暂存。

### 2026-10-31 终态账本与原循环通知（5.4部分）

- `settle`／`reconcile` 唯一异步入口迁齐 Terminal、Browser、创建、冷恢复、MCP 和 Node 调试；原 writer 串行落盘后按提交顺序在原循环通知。取消等待原写入及通知，保留原锁、首次取消和迟到错误，不新增账本或通知重试链。
- 新增19项。扩大后端回归1855 passed、1 failed，原失败为旧Node调试测试漏await；修复后该项与全部新增用例共20 passed，其余通过结果按未变化生产AST复用。作者新增停止测试曾误将工作区管理stop当作Thread holder持有期，已按原入口改为核真实资源锁；未改变产品准入。补await时一处缩进导致收集失败，修正后编译及定向通过；原输出全部保留。
- 完整旧生产归档的两个反例在同步写盘中触发2秒循环观察超时（原生屏障5秒上限）；Ruff、compileall、strict通过，最终Pyright原新各54条既存诊断按文件、规则、完整消息、词法owner和最窄AST一致，新测试零诊断。证据见`out/tests/temp/lease_terminal_notify_20261031/artifacts/verification.json`。本轮为原owner／持久账本及既有传输替身验证，不代真实HTTP、PTY、Chromium、Backend硬退出或Windows验收；5.2／5.4及21/23保持未完成，Web／Workbench、其自动化和共享debug未动。

### 2026-10-31 Terminal本地准备与外部派发分界（5.2／5.4部分）

- 受管cwd准入、目录URI保存和目录lease获取移至共享创建链的外部派发前；原资源锁、目录holder及Thread准入覆盖原writer的worker结算。准备失败或取消沿原未派发收尾，关闭后不迟到创建；真正派发丢回执仍保留unknown及目录占用，不新增账本或恢复入口。
- 新增16项，相关后端回归910 passed／40.77秒，采样源码前后一致。完整旧生产归档叠加最终测试的四个准备故障均复现未结清intent，正常与丢回执两个控制通过；Ruff、compileall通过。Pyright原新各一条既存ResourceTransport属性诊断，文件、规则、完整消息和原Attribute表达式一致，新测试零诊断。原始输出、命令、指纹、类型对照及作者结构审查见`out/tests/temp/terminal_pre_dispatch_20261031/artifacts/verification.json`。
- 本轮使用原owner、文件writer及lease账本和替身目录准入／外部transport，不证明真实PTY、GC、HTTP、Backend硬退出或Windows；完整旧代接管、原生I/O抢占和整个operation/shutdown期限仍TODO，5.2／5.4及21/23保持未完成。未修改Web／Workbench、其自动化或共享debug。

### 2026-11-01 冷恢复准备的原生账本结算（5.2／5.4部分）

- 资源登记与committed fork占用结算复用既有shielded worker及原账本writer锁，准备锁覆盖完整写盘、对账和版本冻结。仅实际取消后排空同一准备任务，保留首次取消及迟到写盘cause；正常结果和原失败直接传播，同key不重放关联，不新增账本或恢复入口。
- 新增16项，定向24项及相关后端回归1062项全部通过；63份采样源码在检查前后保持一致。完整旧生产归档的四项反例均复现事件循环被同步写盘阻塞；保留worker修复而恢复旧finally的归档变异为4 failed／4 passed，失败均为原取消被迟到写盘错误替换。Ruff、compileall、Pyright及OpenSpec strict通过，准确命令、指纹、原始输出、实际退出码和作者结构审查见`out/tests/temp/resource_cold_preparation_20261101/artifacts/verification.json`。
- 原owner与账本真实落盘，外部transport为替身；不代真实PTY／Chromium、HTTP、独立Backend硬重启或Windows验收。其它同步访问、原生I/O抢占、完整旧代接管与整个operation/shutdown期限继续TODO，5.2／5.4未勾，整体仍21/23。未修改或运行Web／Workbench及其自动化，未重启共享debug。

### 2026-11-01 operation退出的原取消与异常归属（5.4部分）

- Terminal／Browser owner迁齐共享operations异步上下文，移除重复finally。正常退出直接等待原close；主体已有错误或取消才屏蔽后续取消排空，保留主体原对象及迟到释放错误cause，原资源锁和Thread holder覆盖结算。同operation显式重试原lease，不改变预算、账本或外部动作。
- 新增16项全部通过，相关后端回归1078 passed／67.61秒。完整旧生产归档叠加最终测试为8 failed／8 passed，分别检出正常收尾吞首次取消及释放写盘错误替换主体原错。新增装具先前误认锁返回值、额外持有无关operation holder的失败原件保留；最终隔离装具而不放宽holder、身份或异常断言。
- Ruff、compileall及OpenSpec strict通过；Pyright原新同一条既存ResourceTransport属性诊断按文件、规则、完整消息、词法owner及最窄AST一致，新测试零诊断。准确命令、源码指纹、原输出／退出码和作者结构审查见`out/tests/temp/resource_operation_exit_20261101/artifacts/verification.json`。使用原owner、真实账本和OS holder，外部transport为替身，不替代真实HTTP／PTY／Chromium或Windows验收；完整旧代接管、原生I/O抢占及整个operation／shutdown期限仍TODO，5.2／5.4与21/23保持未完成。未改另一会话的Web迁移、回归driver、共享暂存或debug。

### 2026-11-01 真实 Workbench 资源补验（5.2／5.4适用证据）

- 复用唯一正式 `integration-web-persistent-resources`，两参数2 passed／176.36秒，最终退出0，Gateway清理完成并释放原集成名额。真实Gateway、PTY、Chromium验证工作区保留资源在安全重启后仍可操作、跨Thread关联／解除、revision冲突后重读、删除Session后继续使用，以及工作区Browser创建预览和删除；模型使用显式model_stream回放。
- 2289份采样源码前后无变化；正式`run-0aa95245e35e`的`workbench-build.json`及稳定dist保留，可供迁移会话复核。三个阶段均无pageerror，但不称控制台／网络全净：删除冲突409为显式注入，Session查询404发生在提交删除后，另一次Terminal列表409发生在停止／删除阶段，原驱动未保存其响应正文，具体原因仍未核实。
- 准确命令、原始输出、源码指纹、三个阶段回执和清理证据见`out/tests/temp/resource_web_acceptance_20261101/artifacts/verification.json`。不替代首次取消、未知执行终止、整个operation／shutdown期限或Windows验收，5.2／5.4及21/23保持未完成；未修改正式driver、公共fixture、另一会话的迁移实现或共享debug。

### 2026-11-01 资源HTTP错误采集与限定重验

- 唯一`persistent_resource_lifecycle.mjs`补采错误HTTP正文、request_id、请求工作区、当前精确Thread及phase/step，排空采集任务；未归因错误使验收失败，Terminal列表409不获状态码豁免。显式Browser409核原请求版本、owner及正文；Session404仅消费响应发生时冻结的committed/settled删除证明，不能借未来回执追认。原guest401探测仅按同一request_id分类，不扩大到其它401；原业务断言、期限和前端generation隔离保持。
- 仅运行包含after-unload的unload-restart参数，正式`run-c5189c859147`为1 passed／1 deselected／133.84秒，最终exit0、Gateway清理完成并释放名额。唯一捕获的Browser409核expected=6／actual=7及头体同request_id；旧Terminal409本次未复现。旧request_id仅有access记录、没有正文，原因继续未确定，不按stop/delete时序归因，不改产品或盲重试。
- 2289份采样源码运行中无变化；最终仅补强诊断分类与采集收尾。当前唯一函数消费同一真实HTTP原件、拒绝7类错误反例，并通过先404后删除、未提交删除、冻结证明副本、精确guest探测与其它401拒绝的短检查，语法检查通过。时序和guest分支使用明确诊断HTTP替身，不称产品分支验收，未因此重复业务组。正式构建、原始失败限制和自检源码保留；准确命令、指纹、patch及原输出见`out/tests/temp/resource_http_error_audit_20261101/artifacts/verification.json`，5.2／5.4及21/23继续未完成。

### 2026-11-01 GC 与终态 fork 暂存的原 worker 结算（5.4 部分）

- 到期候选和终态 fork 操作查询复用受保护 worker；同一目录的 deleting、物理删除、原账本 stopped 和 tombstone 在原目录 holder 内依次结算。物理删除后归还工作区文件写锁，账本等待不阻塞无关文件；账本失败保留 deleting、原 locator 与期限，下一次原 GC 幂等重试。终态 fork 的快照、原生探测与删除在原 import holder 内结算，取消和迟到失败不遗弃原访问，不扫描未知目录。
- 28 项新增 worker／取消／失败／原 holder 单测与相关附件回归共155 passed；最终目标平台异常断言补验1 passed。正式产品4项覆盖真实启动失败后的目录释放、GC原账本写盘失败重试及两项既有停止故障收尾；独立进程GC删除前／后硬退出2 passed，main／child fork、GC竞争及发布／失败／删除11 passed。首次Shell组合11 passed／2 failed仅因旧测试仍断言原生错误直接公开；按已生效的结构化错误合同迁齐类型、errno、原cause身份和路径边界后，4项定向通过，其余9项沿相同源码证据复用，不放宽终态或身份断言。
- 实现前的4项新测试均失败于同步访问阻塞；原记录不足以证明完整旧归档对照，不据此声称完成归档变异验证。Ruff、compileall、OpenSpec strict通过；Pyright原新各17条既存诊断，逐项一致，新测试零诊断。准确命令、源码指纹、原始输出、实际退出码及结构审查见`out/tests/temp/attachment_gc_workers_20261101/artifacts/verification.json`。既有Session集成目录有144个直接Python文件，本轮未新增其直接文件；新单测置于原附件专用子目录。未修改Web迁移、公共fixture或共享debug；此证据不代Windows、原生I/O抢占、旧代无身份接管或整个operation／shutdown期限，5.2／5.4及21/23保持未完成。

### 2026-11-01 创建回执前 Python owner 硬退出（5.2 部分）

- 新增独立真实进程验收：正式 Session／acceptance 和原资源创建入口生成 intent 与 dispatching lease，在 transport 派发前或真实 Node 已创建而 Python 尚未消费回执时强杀原 Python 进程。冷 owner 从原持久记录恢复，核来源、关联、精确 lease、外部 PID／页面身份和无自动重放；有实例时仅新管理 operation 可停止并结清，随后原 Session 清理完成真实删除。无实例时空清单／404仍保持 creating／unknown 和原占用，代码明确保留无身份接管 TODO。
- 两领域×两个窗口共4 passed／89.39秒，外层180秒和4GiB数据段保护，沿原集成独占闸门；采样源码前后无变化。Ruff、compileall、Pyright零诊断，限定独立复审核准worktree／HEAD／指纹后无finding。首轮误将缺失实例的unknown期望成reconcile_required、次轮将Proto Struct整数PID限定为Python int的装具失败均保留，按现有合同修正，不改产品或放宽身份、原占用和真实终态断言；初次调用未安装的Python pyright入口失败后使用既有Bun入口。
- 准确命令、源码指纹、原始输出和实际退出码见`out/tests/temp/resource_creation_restart_20261101/artifacts/verification.json`；正式工作区／进程日志与强杀、恢复原件在`out/tests/integration/workspace_services/test_resource_creation_restart/`。本轮只新增验收及合同登记，未修改产品、公共fixture、Web迁移或共享服务；不代表完整Backend／Gateway／Web重启、Windows、无外部身份接管或整个operation／shutdown期限。5.2／5.4及21/23继续未完成。

### 2026-11-01 尚无原生登记的创建取消证明（5.2 部分）

- creating／unknown 通过原 stop 入口取得新管理 operation，由原生 owner 在同一 Session 记录及 writer 发布 creation_cancelled，再签绑定资源／工作区／整数版本的 not_created。未发布候选只显示 unknown，已有原生登记只返回 present 交原停止链；排队及迟到的同 ID 创建被拒绝，冷重启和普通终端历史裁剪不消除取消事实。Python 通过原 writer 关闭 intent、解除关联、同步账本并结清精确旧占用，不新增数据库结构、文件或 lease 账本。
- 原资源回归1118 passed／2 failed，失败仅为旧测试仍预期无管理动作；迁齐精确 stop 合同并增加真实 owner 冷恢复管理取消后，该文件22 passed。新取消专测12 passed；原生 Terminal 31 passed（Node node:test）、Browser 19 passed（Bun）。四项独立进程强杀／冷恢复／真实 HTTP、PTY／Chromium补验4 passed／91.78秒，包含取消后真实迟到创建被拒绝。原生测试装配曾因屏障释放顺序挂起及误用 Bun 执行 node:test 失败，原输出保留；修正 runner／屏障后验证通过，未修改既有行为断言。
- Ruff、Python编译、Node ESM语法及OpenSpec strict通过；Pyright原新各13条诊断按相对文件、规则、完整消息、所属定义及最窄AST一致，新取消及本次触达的生命周期测试零诊断。准确命令、源码指纹、原始输出、实际退出码、复审及结构审查见`out/tests/temp/resource_creation_takeover_20261101/artifacts/verification.json`。真实重启补验期间只修改不参与该集成的单测，生产及集成源码未变化；不是全仓冻结。
- 已登记但缺少 PID／context 的旧创建仍不能从空句柄推出停止；完整旧代接管、原生I/O抢占及整个operation／shutdown期限继续TODO，5.2／5.4和21/23保持未完成。本证据不替代完整Backend／Gateway／Web或Windows验收；未修改另一会话的前端、公共fixture、长回归driver或共享debug，仅复用现有独占闸门。

### 2026-11-01 Browser弹出页注册的原执行排空（5.4部分）

- context事件接纳的注册进入既有operation执行集合；释放context后，迟到CDP、设备设置和激活不得重新发布页面，旧context事件不能进入新context。Manager仅停止新准入、原context仍在时，已接纳注册仍可完成供检查点捕获，不新增队列或持久账本。
- 新增5项注册／关闭单测，相关Browser回归192 passed；实现前4项反例均失败，未作为完整归档变异证据。真实Python客户端、Node/HTTP及Chromium新场景与既有创建／两种关闭场景共4 passed：原关闭先408并持久保留unknown，独立页面可操作，原回执释放后显式重试才核实终态，页面不得复活。首次创建超时未定位，观察追踪下注册26毫秒且场景通过，未调大请求期限；保留启动慢的测量TODO及原件。扩大回归另两项曾因端口占用未进入业务，原同bind核空后仅补跑这两项通过，未杀未知进程。
- Ruff、Pyright零诊断、Python编译及Node语法通过；本轮首次类型收窄前5条诊断和误用不存在的pyright script失败均保留。限定独审无finding，源码指纹、原始输出、准确命令与退出码见`out/tests/temp/browser_page_registration_20261101/artifacts/verification.json`。所有测试有外部超时与4GiB保护，集成沿原独占闸门；不替代Python业务owner、完整Backend／Gateway／Web重启或Windows验收，5.2／5.4和21/23继续未完成。未修改Web迁移、其公共fixture／回归driver或共享debug。

### 2026-11-01 Browser下载保存与原结果写盘排空（5.4部分）

- 页面下载复用原operation执行集合，保存及成功/失败结果写盘全程由同一句柄结算；activeDownloads不再被当作关闭完成证明。已释放或替换页面的迟到下载事件不启动保存；原context关闭后等待未结算时仍408/unknown，原执行完成后显式重试才核实终态，不新增队列或账本。
- 新增5项单测，修改前4项反例失败、正常对照通过，未称完整归档变异；修复后相关Browser回归217 passed，Ruff/Pyright零诊断、编译/Node语法通过。真实Node/HTTP/Chromium下载与弹出页关闭2 passed/21.46秒：原StateStore实际保存正文后仅暂扣回执，核磁盘unknown、真实字节及原下载尚未发布、peer可操作，放行后原身份记录与同资源终态一致。2835份采样源码前后无变化，外部超时/4GiB及原集成闸门保持。
- 准确命令、原始输出、退出码、指纹、正式产物及作者结构审查见`out/tests/temp/browser_download_drain_20261101/artifacts/verification.json`。不代原生保存强制抢占、下载网络永久挂起、整个operation/shutdown期限、Python业务owner、Gateway/Web或Windows；5.2/5.4及21/23保持未完成。前端、公共fixture、Section8长回归driver、共享Debug及他人暂存未改。

### 2026-11-01 页面关闭事件的完整执行排空（5.4 部分收口）

- 原close事件的替代页激活、最后页状态保存和失败记录写盘现纳入原operationQueue执行集合；没有新队列或账本。Manager先等原事件再采集检查点；stop/delete未排空仍408/unknown，原执行结算后显式重试才verified，既有身份复核和closing准入规则不变。
- 修改前4项反例失败、正常对照通过（非完整归档变异）；修改后222项相关单测通过。新场景经正式创建、新标签及run入口生成真实Chromium页面，原指针reset先完成，仅暂扣回执；peer真实关闭原活动页，核关闭已发生但激活尚未结算、stop408与磁盘unknown、peer可用、释放原回执后同身份/版本重试verified，以及原迟到激活错误已持久保存。与下载、弹出页补验合计3 passed/28.36秒，源码2837份采样前后无变化。
- Ruff、Pyright零诊断、compileall、Node语法及OpenSpec严格检查通过。准确命令、原始输出/退出码、源码指纹和作者结构审查在`out/tests/temp/browser_page_close_20261101/artifacts/`，正式场景产物沿测试镜像目录保留。未修改前端迁移、公共fixture、长回归驱动或共享debug；没有新增独立审查。
- 此片不证明不合作执行可强制终止、Python业务owner接管、Gateway/Web、硬重启、Windows或整个operation/shutdown期限。5.2、5.4仍未完成，整体21/23。

### 2026-11-01 Node Debug 进程占用发布与终态顺序（5.4 部分）

- 资源登记与获取沿同一受保护 worker 结算，原 owner 锁和运行时锁覆盖取消及真实进程终态；删除两个无其它调用方的同步辅助方法，迁齐异步回调。修改前两个事件循环阻塞反例失败；首版异步实现的两个进程退出反例也失败，随后复用原 state_lock 修正顺序，未新增状态账本。
- 正式矩阵 10 passed／60.38秒，覆盖真实 API／Node／Inspector、两处账本发布、取消与迟到写盘失败、同 owner stop 等待、冷 service 重读及同方案重启；退出信号调用改用标准库后定向 2 passed／10.42秒。相关回归 66 passed／50.11秒；Ruff／编译通过，两份生产执行模块与新增集成 Pyright 零诊断，另修正握手等待列表的两项既有类型错误。所有测试有外部期限／4GiB，集成沿原独占闸门。
- 准确命令、原始输出、退出码、源码指纹及作者复审见 `out/tests/temp/debug_lease_admission_20261101/artifacts/verification.json`。不代替 Gateway／Web、独立进程重启、Windows、线程池永久排队或整个 operation／shutdown 期限；5.2／5.4 及 21/23 仍未完成。未修改前端、共享 debug、公共 fixture 或长回归驱动。

### 2026-11-01 MCP 连接登记与关闭的原 task 结算（5.4 部分）

- 启动、重连及配置替换共用原 session task 的异步登记／关闭回调，删除三个重复同步登记；原 writer 的 worker 结算后才通知就绪或完成关闭，取消保留原取消和迟到失败，未发布候选的控制流异常仍收尾。运行中断线后的终态写盘次错立即由原 owner 报告；没有新增账本、连接重放或另一套 SDK 生命周期。
- 修改前两个原生发布屏障反例均失败；正式独立矩阵最终17 passed／26.99秒（此前16项25.96秒通过），补充控制流与最终生命周期12 passed／6.45秒，其中新增1项真实连接控制流和1项存储次错单测。既有真实stdio／HTTP协议回归28 passed／32.38秒。扩大整组110秒及单测85秒运行均外部超时；单测原输出174个明确PASS，剩余17项独立29.88秒通过，不将两次整组退出124表述为全量成功。
- 首轮取消cause环已修正；后补控制流场景曾错误计算共享正式工作区中所有历史连接，现按本次精确owner实例核两条原身份及stopped，不放宽生命周期断言。Ruff／编译通过，最终四文件Pyright零错误零警告；准确命令、指纹、原始输出、退出码与作者复审见`out/tests/temp/mcp_connection_publication_20261101/artifacts/verification.json`。全部测试有进程外期限／4GiB，集成沿原独占闸门；不代Gateway／Web、独立Backend硬重启、Windows、其它同步账本访问或整个operation／shutdown期限。5.2／5.4及21/23保持未完成；未修改公共fixture、前端迁移、长回归驱动或共享debug。

### 2026-11-01 Node Debug claim 的原写入结算（5.4 部分）

- 启动、身份登记、运行及停止／冷恢复统一等待原 claim writer 的受保护 worker；沿原 tracker 保护排队与写盘，再按真实提交更新投影，失败不遗留提交 blocker。原 owner 锁、Session 准入及 runtime state_lock 覆盖相应结算，冷恢复互斥不阻塞活跃进程的停止中快照；不新增持久账本或恢复链。
- 真实 ASGI／Node／Inspector 的三阶段四结果矩阵12 passed／79.63秒，真实进程退出2 passed／9.73秒，既有 lease 场景10 passed／60.46秒；定向新单测7 passed／0.99秒，相关回归96 passed／64.71秒，共127个不同用例。新测试覆盖原提交、重复取消、迟到失败、实际线程池排队、闲置卸载及原进程终态。首轮屏障误挂 Path.replace，修正为原 os.replace；并发回归观察点从异步返回改为真实存储提交，原身份／顺序断言保留。失败原件未覆盖，不把中断运行或重复用例相加。
- Ruff／编译／OpenSpec严格校验通过，五个生产文件及两个新测试 Pyright 零诊断；原始输出、命令、退出码、源码指纹及作者结构审查见 `out/tests/temp/claim_io_20261101/artifacts/verification.json`。测试均有进程外超时／4GiB保护，集成沿原独占闸门；不代 Gateway／Web、独立Backend重启、Windows、同步读取、原生I/O抢占或整个operation／shutdown期限。5.2／5.4及21/23保持未完成；未改共享debug、前端迁移、公共fixture或长回归驱动。


### 2026-11-01 同目录 fork 释放后的到期回收补验

- 已修复本地同目录 fork 因不存在的独立导出回执而无限保活；prepared/reserved和跨工作区export的原保护保留。真实上传/工具/full copy/副本执行/目录GC/来源删除/进程重启及冻结请求恢复共用 itemized 7.5-C／8.3 本轮收据，原件见 `out/tests/temp/attachment_fork_gc_20261101/artifacts/verification.json`。不据此关闭5.2或5.4，整体仍21/23。

### 2026-11-01 GC 候选的单次行快照与删除收尾失败（5.4 部分）

- 删除先查ID再逐目录重查的读取，候选、单目录读取和deleting回执共用原locator校验。1／32个独立真实上传的候选查询从2／33次SELECT降为1次，不改变删除前事务复核、30天期限或原holder；没有schema变化或新SQLite语法依赖。未开始删除的目录与缺失目录均明确拒绝并回滚，缺失目录不再误抛AttributeError；重复tombstone收尾保留原时间。
- 修改前新增4项中3项失败、正常对照通过；修复后相关单元回归113 passed/22.46秒。现有正式产品矩阵 `integration-attachment-retention-measurements` 2 passed/35.59秒，经真实消息执行／卸载形成1／32目录、冷owner恢复和到期GC，核真实原件、独立身份、原期限、tombstone、重复GC及canonical字节不变。全部测试有进程外期限／4GiB，集成沿原独占闸门；不承诺端到端速度比例。
- Ruff、两个修改源码文件的真实Pyright零诊断、compileall和本change严格检查通过，消除catalog此前三项Optional诊断。准确命令、源码指纹、原始输出／退出码、查询量反例与作者审查见 `out/tests/temp/gc_catalog_snapshot_20261101/artifacts/verification.json`。本片不证明无原生身份的旧代接管、原生I/O抢占、整个operation/shutdown期限或Windows，5.2／5.4及21/23继续未完成；未改前端、公共fixture、长回归驱动或共享debug。

### 2026-11-02 Browser 管理动作的单次准入预算（5.4 部分）

- `BrowserResourceOwner.perform`在原入口一次取得30秒准入预算，覆盖冷恢复、Thread准入、原资源锁、身份／版本／关联核验和原管理lease获取；合并原私有分支，不保留第二入口。成功后退出预算，原holder、锁和lease继续覆盖同一回执链；HTTP／Session恢复消费者及`run.timeoutMs`的既有执行期限保持。到期不派发迟到动作，不补造unknown；已提交恢复、底层TimeoutError及原取消保持归属。原生worker须结算，仍不承诺整个动作可强制终止。
- 两项新用例在旧产品复现无界等待；修复后相关主回归92 passed，类型标注补强后14项新增场景和15项原Browser owner回归合计29 passed（与前批有重叠）。覆盖恢复／准入／锁等待、取消、跨阶段同预算、冷恢复版本保留、无Thread的workspace入口、动作超过准入预算但保持原锁／lease，以及底层超时身份。正式Session／ASGI动作API及真实Node／Chromium／HTTP两场景分别通过，核503／request_id、peer操作、原记录／lease字节、页面身份、原脚本表单副作用及同入口重试。矩阵首跑为1 passed／1 failed，失败是新测试先持资源锁再等待同锁冷恢复造成的自锁；调整测试持锁时序后锁场景定向1 passed，不重复未变的run场景，不称首轮矩阵全绿。独立suite `integration-browser-operation-admission`固定90秒／4GiB，沿原集成闸门。
- 修改的三份Python文件Ruff、真实Pyright零诊断；命令、指纹、原始输出／实际退出码及失败归因保留在 `out/tests/temp/browser_management_admission_20261102/artifacts/verification.json`。不代表真实等待30秒、Gateway／Web、Windows、无原生身份旧代接管或完整operation／shutdown门，5.2／5.4和21/23继续未完成；未改前端或公共fixture。

### 2026-11-02 活动裁剪的存储等待与关闭保留（5.4 部分）

- 启动和每小时活动裁剪复用原`prune`与`run_sync_shielded`，SQLite DELETE／线程池排队不再占住事件循环；原周期任务持有worker直到结算，关闭观察到期或取消不提前关闭数据库、释放维护锁或重复裁剪。启动仍须取得原结果才继续；没有新增存储、任务账本、期限或兼容入口。
- 真实SQLite写锁红测复现事件循环阻塞；首批第二项因前项红测清理未重入而受污染，修正测试清理后，独立启动红测以原线程身份及8秒原生屏障再次复现。修复后独立矩阵`integration-activity-prune` 5 passed／36.49秒，覆盖两种等待、两种观察中断及启动回执，核诊断request_id、原owner／locator／维护锁、只裁剪一次及冷读保留窗口；固定90秒／4GiB，沿原集成闸门。33项相关单测和3项原存储关闭集成分别通过。扩展的15项关闭回归在120秒保护下退出124，仅输出7个通过标记，整组未完成；不重复已完成部分，另选未执行的上述3项通过，不将整组记绿。
- Ruff／compileall及OpenSpec strict 54/54通过。真实Pyright的4项JobServiceProtocol方法缺失与完整HEAD归档一致，无新增诊断；初次`uv run pyright`未找到命令，随后沿既有`bun x pyright --pythonpath .venv/bin/python`入口核验。命令、源码指纹、原始输出、退出码与失败归因见 `out/tests/temp/activity_prune_20261102/artifacts/verification.json`。其它同步活动读写、原生I/O抢占、执行关闭长尾及整个进程退出期限仍TODO，5.2／5.4和21/23保持未完成；未改前端、公共fixture或共享debug。

### 2026-11-02 Node 调试停止与删除排空的原生状态访问（5.4 部分）

- running／停止阶段沿原 claim 读取器进入受保护 worker，原 state_lock 覆盖排队及结算；删除停止进程后，原 manifest 投影写入及最终 active claim 核验同样由 worker 完成。原 Session 删除 gate 与 owner 锁保持，全部结清才从原 runtime 集合移除该 owner，避免物理隔离后服务关闭再次定位已删除 Session；失败及取消保留原 runtime 供同一 operation 恢复，没有增加 writer、账本或恢复链。
- 真实 Node／Inspector 和 ASGI 导航删除的完整九场景通过（47.07秒），覆盖停止 claim 读取、manifest 写盘及最终 claim 核验的等待／取消／失败恢复，核 peer 可用、gate／fence、locator、原进程身份、同 operation 结算、隔离原件及后续服务关闭。受影响的 claim 发布和 catalog 排空18项回归通过（91.72秒）；本次归属单测14项通过（2.77秒）。首轮新 matrix 的 final 屏障误拦驻留 source 同步回调，修正故障点识别后完整九项通过；原24项组合回归曾被外部期限终止（exit124），不计作整组通过，后续只补本片受影响的18项。
- 修改的两个生产文件及新集成测试 Pyright 零诊断，Ruff／compileall及 OpenSpec strict 54/54通过。纳入旧归属单测文件的检查仍有48项类型诊断；在相同当前生产依赖下检查 HEAD 的原测试，逐字归一结果相同，无新增，不称全部类型检查为绿。准确命令、源码指纹、原始输出、实际退出码及类型对照见 `out/tests/temp/debug_delete_io_20261102/artifacts/verification.json`。
- 驻留 source 同步观察仍可能读盘，已在原回调保留 TODO；其它同步调试入口、原生 I/O 抢占、不合作取消及完整 operation／shutdown 期限仍未完成。此片不代 Gateway／Web、独立 Backend 硬重启或 Windows 验收，5.2／5.4与21/23保持未完成；未改前端、公共fixture或共享debug。


### 2026-11-02 Browser 原创建执行的管理取消（5.2 部分）

- 原 Node 仍持有创建 Promise 时，匹配当前版本的 cancel-creation 关闭准入、沿原资源转换释放 context，并等待迟到 context 及原 failStartup/close 的终态写盘。重复取消共用原执行，已取得 context 只返回 present；原创建结束后清除在途引用。释放首错不被随后成功的失败收尾吞掉，无原执行的冷记录继续 unknown，不新增启动链或账本。
- 最终相关 Browser 单测189项及真实 Node/HTTP/Chromium 创建全文件4项通过；真实场景覆盖 context 交付前、页面注册后两处回执等待、管理版本推进、原创建与取消共同结算、磁盘 failed/verified、context 仅关闭一次及 peer 继续执行。原 Python owner 消费者59项回归通过，其生产依赖未变，复用原结果。完整 HEAD archive 加全部候选补丁后，仅移除 Manager 新取消分支的两项负向变异均在“立即回执而未进入关闭”处失败，未超时。
- Ruff、真实 Pyright 零诊断、Node 语法和本 change 严格检查通过；误用不存在的 uv pyright 命令与首轮测试 run 参数写错的失败原件保留。准确命令、源码指纹、退出码、正式产物和作者结构审查见 `out/tests/temp/browser_startup_cancel_20261102/artifacts/verification.json`。全部测试有进程外期限/4GiB，集成沿原独占闸门；不代 Python lease、Backend 硬重启、Gateway/Web、Windows、无原执行接管或整个 operation/shutdown 期限。5.2/5.4及21/23仍未完成。

### 2026-11-02 Python 冷恢复保留原生创建阶段（5.2 部分）

- 唯一快照消费入口将原生created映射为领域creating，原creating在未verified的运行观察下保持原创建阶段。Python失去创建回执后，显式管理stop仍先取消原创建，再沿原停止链核实；已完成启动的verified快照及未知结果规则保持，不增加字段、schema、writer或第二账本。
- 新增Terminal/Browser×created/running四个反例在修复前全部失败；最终共享创建、两类owner、冷恢复与回执诊断224项单测通过。真实Node/HTTP/Chromium两处启动屏障下强杀Python，再以独立进程冷恢复，两项通过，核原Thread、冻结版本、精确lease结算、原context只关闭一次及拒绝重放创建。既有四个原生创建场景在native-002中通过，但该组新场景失败，整组不记绿；修正新测试对原intent默认reconcile_required的误判后，新两项在native-004通过。另补启动已完成仅回执丢失的Terminal/Browser两项真实硬退出回归通过，沿原stop/delete而非cancel-creation。
- 首次补跑被既有450文件端口容量拦在setup；独立提交`836d75046`删除固定层级配额，两层共用567个完整100端口块，当前518文件互不重叠，13项端口与46项装具回归及静态检查通过。其它原失败包括测试误判unknown/verified、一次瞬时EADDRINUSE和新增测试的类型/导入问题，原件保留；未修改宿主端口配置或杀未知进程。
- 三份修改Python的Ruff、compileall及真实Pyright零诊断通过；严格OpenSpec、准确保护命令、源码指纹、原始输出、退出码和作者结构核对见`out/tests/temp/browser_creation_recovery_20261102/artifacts/verification.json`。所有测试有进程外期限/4GiB，集成沿原独占闸门；本片是Python owner子进程硬退出，不代完整Backend/Gateway/Web/Node硬重启或Windows。无原context身份旧代接管、原生I/O抢占和整个operation/shutdown期限继续TODO，5.2/5.4与21/23保持未完成。
