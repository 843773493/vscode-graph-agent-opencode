## 1. 本轮边界：规范与回收扫描小修

任务 1.* 的勾选沿用已有工作树的实施进度，相关产品代码尚未全部纳入本次规划提交。本次任务 2.1 只核调用方和迁移方案并验证规范，不代替这些代码的候选验证或发布验收。

- [x] 1.1 登记目录级身份/关联、Shell URI、30 天回收、历史/fork、失效 owner 对账，并审查相关 change 的冲突与替代顺序。
- [x] 1.2 Thread residency 批量扫描隔离单项失败，保留原错误及 owner 身份，继续其它 Thread 并允许下一轮重试。
- [x] 1.3 唯一闲置 worker 显式记录扫描错误后继续原周期重试；验证取消和关闭仍收敛。
- [x] 1.4 完成定向回归、相关静态检查及 OpenSpec 严格验证，保留准确命令、源码指纹、原始输出和退出码。
- [x] 1.5 迁齐共享 operation、Terminal/Browser operation、Browser 管理入口及测试中的资源记录查询改名调用，移除旧名悬空引用且不增加兼容入口。
- [x] 1.6 单 Thread 闲置卸载扫描最多等待 30 秒，超时保留唯一在途任务并继续其它 Thread；覆盖 listener/scope 挂起、迟到成功/失败、重复扫描及新 admission 不绕过未结束的原任务，完成静态与规范验证。

## 2. 目录存储与接受链路（后续实施）

- [x] 2.1 全仓盘点上传、媒体读取、消息接受、历史、文件工具、Shell、fork、删除和测试的直接/间接调用，确定支持的旧库范围及原件保全/切换方案。（2026-10-24：调用链实施表、schema 3 → 4 显式离线迁移与原件/封存历史保护见 design；275 文件/3110 处检索原件及指纹在 `out/tests/temp/attachment_directory_inventory_20261024/artifacts/`。仅完成盘点/方案，2.2 后的产品实现和验收仍未完成。）
- [ ] 2.2 在原附件 owner 实现每次独立上传的目录资源、稳定身份/locator、原件/预览/派生区和目录级关联，结构变更时提升 schema 并提供明确支持范围的迁移或拒绝行为。（2026-10-24：已准备备份内独立目录与 schema 4 基础 catalog，保全旧来源/终态 fork 原文；已补新 catalog 的旧 owner/fork alias 精确解析，并接入迁移发布核验（完整迁移 75 passed、附件定向 120 passed，1 项既存预览断言失败另有原 HEAD 复现；证据 `out/tests/temp/attachment_legacy_resolution_20261024/artifacts/`）；线上上传、读取、fork/ingest 与唯一切换仍 TODO，本项保持未完成。）
  - 2026-10-24 先完成在线访问与离线维护的跨进程互斥：Backend 全 mount 在业务初始化前持共享锁至关闭排空，独立构造、上传 pin/ingest、catalog/历史投影遵守同一锁；目录根同级锁文件永不替换，关闭失败保留连接与占用并允许重试。新锁、目录访问、崩溃自动释放及失败路径随相关回归为119 passed；唯一失败的预览诊断旧断言在改动前 HEAD 完整 archive 也复现（`Errno 13` 与现有结构化 `errno=13` 不一致），未改断言。真实双 mount 启停、启动冲突/失败释放与上传拒绝集成共6个场景通过；维护时上传未创建 pin/ingest，原控制库逻辑内容不变。准确命令、tree/源码指纹、原始退出码与基线失败原件位于 `out/tests/temp/attachment_maintenance_20261024/artifacts/`。目录格式、schema 3 → 4 离线迁移、持久任务核对、一致备份和切换仍 TODO，本项未完成且未提升 schema；本轮未执行模型请求、团队/委派专项或 Windows 实机验收。
  - 同日接续完成 `migration prepare/verify` 离线备份阶段：独占锁内只读核对 schema 3、Session/Thread 操作、ingest/commit/fork claim、执行队列与外部占用；queued 保留，未知或活动任务拒绝。SQLite backup 纳入已提交 WAL，原件与历史按登记 locator 保全，复制前后及全批结束核源指纹；清单和所有内容 durable 后才发布 prepared，失败保留 preparing 且不覆盖。产品创建、上传和接受生成源数据，完整命令、队列/canonical hash/原件核验、WAL、九类损坏/活动输入、复制中新提交与中断、清单篡改和 locator 边界共24 passed/73.45秒；相关附件 owner 回归119 passed/1 deselected，跳过项为上条已复现的既存诊断断言；Ruff及compileall均exit0。证据位于 `out/tests/temp/attachment_backup_20261024/artifacts/`；首轮空WAL误判、第二轮核验生成sidecar、第三轮故障输入违反claim表约束的原始失败保留，后续分别修复并通过。目录格式/schema 4、恢复/切换、终态真实 fork 历史与 Windows 迁移验收仍 TODO，不能把 prepared 当作任务 2.2 完成；未执行团队/委派专项或真实模型请求。
  - 同日补齐离线外部 owner 准入：Terminal/Browser 先持久创建记录、后登记通用账本的真实产品窗口原先会误放行；新增回归在完整原 HEAD 候选以 DID NOT RAISE 复现。现复用领域记录原格式及终态闭集，终态与 verified 同时满足才允许备份，dispatching fork attach 明确拒绝；已核实记录与 Thread 关联逐字保存，不调用 recover 或改变来源。38 项备份集成通过/88.58秒（含两类产品创建登记失败、原24项及离线状态/回执/原文保全），Ruff、compileall与严格规范验证通过；原始命令、源码指纹、红测与收据在 `out/tests/temp/attachment_backup_owners_20261024/artifacts/`。离线已存终态记录使用局部数据夹具，不代替真实 PTY/Browser 停止协议验收；schema 4、目录转换、恢复/切换仍 TODO，2.2 保持未勾选。
  - 同日完成备份内目录物化：`convert/verify-directories --backup` 将每个旧 attachment_id 转为独立目录，原件与已有预览逐字复制、派生区留空；同 blob 多 owner 不使用硬链接。保留完整旧 owner/blob、UTC 创建日期、释放时刻与更晚的显式 retention；活动精确 owner 缺失、时间不明和正文版本冲突明确拒绝，tombstoned 不创建空目录。复制中断可核验并复用已完成文件后续跑，损坏成品不覆盖，校验与 durability 完成后才发布 directories_prepared。产品创建/上传/接受与真实预览生成的16项转换集成通过/34.27秒，原备份38项回归通过/88.32秒，Ruff与compileall通过；命令、完整候选源码指纹、raw和退出码在 `out/tests/temp/attachment_directories_convert_20261024/artifacts/`。释放/损坏状态为局部迁移夹具，不充当真实卸载或GC验收。旧库、备份data与canonical原文不变；schema 4、冻结alias/claim解析、初始化阶段任意崩溃恢复、线上切换和Windows实机仍TODO，2.2不勾选；本轮未执行团队/委派专项或真实模型请求。
  - 同日将目录 locator 生成/校验/路径预算归回原 `locator.py`，迁移不再自行拼日期/身份。目录使用冻结创建时刻的 UTC 日期，旧非 UUIDv7 附件 ID原样保留；校验真实日历日期、严格ASCII形态和完整路径预算，不修剪或重定向。新旧 locator 定向53项通过/3.19秒，转换集成17项通过/35.40秒，Ruff、compileall与严格规范校验通过；准确命令、候选源码、raw和失败归因在 `out/tests/temp/attachment_directory_locator_20261024/artifacts/`。首次单测失败是末尾对超长路径调用exists触发OS错误，路径预算拒绝已正确生效；改为比较父目录内容证明无写入，未修改生产行为或放宽拒绝。此批仍只供离线转换，不声称线上目录上传/切换或Windows实机完成。
  - 同日补齐目录转换初始化的进程退出恢复：manifest/journal 在完整计划 hash 定位的临时目录内写齐并 durable 后才原子发布转换根，正文复制仍沿原链路；重试只清理本计划的未发布元数据。完整 HEAD 加新增退出验收的候选红测为6 failed/1 passed，证明旧实现会暴露半初始化根或没有原子发布点。生产候选 `2d131f4eb37a5be0e0204c2229f5779b3f1fd3e9` 的备份/目录/catalog正式矩阵75 passed/232.17秒；仅加强测试的最终候选 `4a73d8b173af3ae445d9fd3496f6e23f7937f35a` 又通过全部7个真实子进程退出窗口/71.41秒，保全无关临时目录、原件和完整备份。Ruff及compileall通过；首轮隔离目录准入失败、首次Ruff失败及修复原件均保留。准确命令、tree/源码指纹、raw/退出码与验证复用依据在 `out/tests/temp/attachment_conversion_init_20261024/artifacts/`。不改变schema或已完成转换格式，已有缺失身份元数据的旧转换根仍明确拒绝；线上目录、唯一切换点和Windows实机仍TODO，2.2保持未完成，未追加团队/委派专项。
- [ ] 2.3 迁齐 ingest/pin、用户/内部消息接受及媒体预览，发布目录与原件 URI；排队输入在 runtime 首次建立前也有可核验保活，不新增附件专用工具。

## 3. 统一 URI、文件 owner 与 Shell（后续实施）

- [ ] 3.1 在唯一 URI owner 扩展 attachments kind、workspace 目录模板和 child 编解码/导航能力，迁齐 DTO、Proto/生成类型和普通文件消费者；新生产引用不再生成旧 Session 单文件模板。（2026-10-24：目录 grammar、导航 shape、原文件 root/child 单点与全部调用迁移、Web 文件树/Markdown 根拆分已完成；272 项 grammar/resolver、150 项文件入口/operation/download、13 项 Web、1 项真实 Gateway grammar 和1项真实浏览器编辑/双 owner 重启重开通过，tsc/build/Ruff/compileall/strict 通过。线上上传引用、DTO 与物理目录 owner 尚未切换，本项保持未完成。证据 `out/tests/temp/attachment_uri_contract_20261024/artifacts/`。）
  - 2026-10-24 接续迁齐 URI 消费合同：目录与旧 Session 附件身份统一归唯一 grammar，内部消息、fork 依赖、原 assembly 附件 capability 和使用查询不再各自解析旧模板；投影同步文件、目录和 derived 输出坐标，保留 canonical、用户正文与冻结预览，并拒绝来源/目标目录错配。该片未切换线上附件 schema 或生成新目录上传引用，任务 3.1 仍未完成。最终定向 874 项、旧附件 79 项、恢复 27 项均通过，另保留 1 项已在原 HEAD 复现的旧预览诊断断言未执行；Ruff 与 compileall 通过。此前组合运行/恢复整文件分别超过 120/90 秒保护期限；分组后恢复前十项 54.65 秒、其余十七项 59.52 秒全部通过，未修改或放宽断言。准确候选、命令、原始输出、退出码和超时原件见 `out/tests/temp/attachment_consumers_20261024/artifacts/`。Agent 团队与协作实现及专项验证继续 TODO。

- [ ] 3.2 将附件目录导航/读取/派生区写入接入原文件 owner，保留原件限制、媒体读取、修订校验、私有存储和 containment 边界。
- [ ] 3.3 Shell 结构化 workdir 在目标机器解析目录 URI并持有目录操作 lease；后台处理进程继续占用直到真实终态，保留原生相对/绝对路径与命令语义。
  - 2026-10-24 已先行接入现有 files 目录 URI，删除工具层重复 cwd 解析/投影和额外 workspace_root 参数，全部创建入口由 Terminal owner 复用固定文件 owner。只将精确 `boxteam://` 前缀视作 URI，保留原生相对/绝对/工作区外、Shell 私有目录和 POSIX 含 `://` 目录名。完整 archive 红测为6单测失败，真实HTTP/PTY在第三种URI输入失败，证明旧实现误投本机相对目录。最终代码候选 `5f143640aa76378603c58d375ca27dfaa73e36d6` 的 unit-006 为63 passed/2.56秒，http-006 为1 passed，Ruff和compileall均exit0；覆盖中文/空格目录三种输入同文件、派生写入/原件不变、命令内URI字面量、两种冒号原生cwd、跨目标/非法URI拒绝且owner/lease/Node记录逐字不变、真实symlink环原路径仅在diagnostic/cause、verified删除与lease结清。http-002还覆盖工作区外绝对cwd及stdin完成（4 passed/45.26秒）；后续修改未改stdin链。独立只读审查提出的任意scheme误分类已修复并复核闭合。准确命令、源码指纹、raw/退出码和机械收据位于 `out/tests/temp/terminal_workdir_uri_20261024/artifacts/`。附件目录URI、后台全进程占用、Windows实机和跨机产品入口仍TODO，本项保持未勾选；002的错误引号断言及Ruff格式失败原件保留，003后已修正。
- [ ] 3.4 模型附件 manifest 提供目录/原件 URI 及处理输出提示，迁齐 Web 附件查看和工作区资源管理；前端变更完成真实类型检查与 Web build。

## 4. 历史与 fork（后续实施）

- [ ] 4.1 使用既有 immutable asset/version 冻结模型实际使用的原件、预览与派生输入；目录变化/回收后仍可重建原请求 hash，普通查看明确展示不可用。
- [ ] 4.2 迁齐统一目标检查与 fork journal/claim：同工作区共享目录关联、跨工作区冻结版本复制，保留原历史身份和 URI，失败/删除释放本次保留且不发布半个目标。
  - 2026-10-24 完成目录迁移前的冻结传输边界：复制所需附件先进入原 capture 文件树，v3 marker 核对完整集合及 digest/length，网络统一复用 files/chunk；删除 live attachment_chunk、blob_id 请求字段及其调用方，Proto 保留编号和名称。目标导入只复制已核验的本地冻结文件，取消须等真实 writer 退出后释放 holder；v2 封存载荷/hash 保持原文读取，不回源补造文件。代码候选 `cdbe3b89d34d258e233dddbb2fcfcaa8c1285972` 的33项 fork 单测、16项协议测试及真实三 Gateway HTTP/SSH/PTY 的来源离线续写场景通过；该产品测试在 marker 发布后移走来源原件，目标仍正确复制、冷读和新输入执行。仅测试入口调整的候选 `69ca0959e2a4ccfe9bdb689aa4a12dfddc4263b1` 又通过2项产品生成附件历史的 Session/child 重启恢复。Ruff、compileall、真实 Web 类型检查/build、协议 lint/build/生成一致性及严格规范验证通过；相对真实提交 e6e6caed2 的 breaking 报告仅包含本次明确下线的 blob_id 删除。首轮 staging 父目录遗漏已修复；候选依赖链接缺失及脱离 matrix 的 fixture 准入失败原件保留。准确命令、完整候选指纹、raw/退出码在 `out/tests/temp/attachment_frozen_transfer_20261024/artifacts/`。目录 manifest、新目录 fork/恢复、线上切换与 Windows 验收仍 TODO，本项未勾选；未追加团队/委派专项实现或验证。

## 5. owner 对账与延迟回收（后续实施）

- [ ] 5.1 复用运行时代次和原任务/operation 账本核对失效关联，接入 unload/Thread/Session 删除的幂等 detach；保护冷 Thread、排队输入和新代关联，禁止仅靠 Thread ID 存在无限保活。
- [ ] 5.2 在原资源 owner 完成旧代操作接管和挂起操作取消/终止核实；未知结果保持可发现管理状态，不以超时假定进程已停止，不新增 lease 账本。
- [ ] 5.3 实现最后一次零关联起算的默认 30 天持久 deadline、重新 attach 取消期限，以及启动/低频到期查询；删除与 attach/lease/fork互斥复核，并可在中途重启后继续。
- [ ] 5.4 统一清理 operation 的截止时间、取消/终止核实与迟到回执，隔离共享 owner 锁对无关资源的阻塞，处理同步阻塞回调/线程池排队；明确 admission、Session 删除和 shutdown 的有界等待及失败返回，保留未核实状态和原 locator，不重复副作用或以扫描超时假报释放。覆盖真实锁竞争、网络未回执、事件循环阻塞及重启恢复。

## 6. 真实验收与旧链物理下线（后续实施）

- [ ] 6.1 经产品入口上传、Shell 预处理、读取处理结果、fork 与重启恢复；覆盖 Linux/Windows 原生 cwd 和远端 URI 始终在目标机器解释。
- [ ] 6.2 外置时钟/故障点覆盖 29 天重关联、30 天到期、处理进程保活、attach/GC/fork竞争、漏 detach 崩溃、旧代迟到释放和删除中途重启；核对真实目录和外部进程终态。
- [ ] 6.3 迁齐调用方后物理移除旧可变附件 blob 身份合并、owner-ref 私有访问、Session 新引用生成与即时 GC 生产链及死测试；保留历史冻结资产及必要来源解析，不双读或提供兼容 fallback。
- [ ] 6.4 按 design 的替代顺序同步相关 owner 规范与验收项，测量目录增长、查询/回收及恢复耗时；短期性能长尾以原始测量证据登记 TODO，不降低正确性门槛。
