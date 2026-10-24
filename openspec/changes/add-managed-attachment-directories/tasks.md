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
- [ ] 2.2 在原附件 owner 实现每次独立上传的目录资源、稳定身份/locator、原件/预览/派生区和目录级关联，结构变更时提升 schema 并提供明确支持范围的迁移或拒绝行为。（2026-10-24：已准备备份内独立目录与 schema 4 基础 catalog，保全旧来源/终态 fork 原文；线上上传、读取、fork/ingest 与唯一切换仍 TODO，本项保持未完成。）
  - 2026-10-24 先完成在线访问与离线维护的跨进程互斥：Backend 全 mount 在业务初始化前持共享锁至关闭排空，独立构造、上传 pin/ingest、catalog/历史投影遵守同一锁；目录根同级锁文件永不替换，关闭失败保留连接与占用并允许重试。新锁、目录访问、崩溃自动释放及失败路径随相关回归为119 passed；唯一失败的预览诊断旧断言在改动前 HEAD 完整 archive 也复现（`Errno 13` 与现有结构化 `errno=13` 不一致），未改断言。真实双 mount 启停、启动冲突/失败释放与上传拒绝集成共6个场景通过；维护时上传未创建 pin/ingest，原控制库逻辑内容不变。准确命令、tree/源码指纹、原始退出码与基线失败原件位于 `out/tests/temp/attachment_maintenance_20261024/artifacts/`。目录格式、schema 3 → 4 离线迁移、持久任务核对、一致备份和切换仍 TODO，本项未完成且未提升 schema；本轮未执行模型请求、团队/委派专项或 Windows 实机验收。
  - 同日接续完成 `migration prepare/verify` 离线备份阶段：独占锁内只读核对 schema 3、Session/Thread 操作、ingest/commit/fork claim、执行队列与外部占用；queued 保留，未知或活动任务拒绝。SQLite backup 纳入已提交 WAL，原件与历史按登记 locator 保全，复制前后及全批结束核源指纹；清单和所有内容 durable 后才发布 prepared，失败保留 preparing 且不覆盖。产品创建、上传和接受生成源数据，完整命令、队列/canonical hash/原件核验、WAL、九类损坏/活动输入、复制中新提交与中断、清单篡改和 locator 边界共24 passed/73.45秒；相关附件 owner 回归119 passed/1 deselected，跳过项为上条已复现的既存诊断断言；Ruff及compileall均exit0。证据位于 `out/tests/temp/attachment_backup_20261024/artifacts/`；首轮空WAL误判、第二轮核验生成sidecar、第三轮故障输入违反claim表约束的原始失败保留，后续分别修复并通过。目录格式/schema 4、恢复/切换、终态真实 fork 历史与 Windows 迁移验收仍 TODO，不能把 prepared 当作任务 2.2 完成；未执行团队/委派专项或真实模型请求。
  - 同日补齐离线外部 owner 准入：Terminal/Browser 先持久创建记录、后登记通用账本的真实产品窗口原先会误放行；新增回归在完整原 HEAD 候选以 DID NOT RAISE 复现。现复用领域记录原格式及终态闭集，终态与 verified 同时满足才允许备份，dispatching fork attach 明确拒绝；已核实记录与 Thread 关联逐字保存，不调用 recover 或改变来源。38 项备份集成通过/88.58秒（含两类产品创建登记失败、原24项及离线状态/回执/原文保全），Ruff、compileall与严格规范验证通过；原始命令、源码指纹、红测与收据在 `out/tests/temp/attachment_backup_owners_20261024/artifacts/`。离线已存终态记录使用局部数据夹具，不代替真实 PTY/Browser 停止协议验收；schema 4、目录转换、恢复/切换仍 TODO，2.2 保持未勾选。
  - 同日完成备份内目录物化：`convert/verify-directories --backup` 将每个旧 attachment_id 转为独立目录，原件与已有预览逐字复制、派生区留空；同 blob 多 owner 不使用硬链接。保留完整旧 owner/blob、UTC 创建日期、释放时刻与更晚的显式 retention；活动精确 owner 缺失、时间不明和正文版本冲突明确拒绝，tombstoned 不创建空目录。复制中断可核验并复用已完成文件后续跑，损坏成品不覆盖，校验与 durability 完成后才发布 directories_prepared。产品创建/上传/接受与真实预览生成的16项转换集成通过/34.27秒，原备份38项回归通过/88.32秒，Ruff与compileall通过；命令、完整候选源码指纹、raw和退出码在 `out/tests/temp/attachment_directories_convert_20261024/artifacts/`。释放/损坏状态为局部迁移夹具，不充当真实卸载或GC验收。旧库、备份data与canonical原文不变；schema 4、冻结alias/claim解析、初始化阶段任意崩溃恢复、线上切换和Windows实机仍TODO，2.2不勾选；本轮未执行团队/委派专项或真实模型请求。
  - 同日将目录 locator 生成/校验/路径预算归回原 `locator.py`，迁移不再自行拼日期/身份。目录使用冻结创建时刻的 UTC 日期，旧非 UUIDv7 附件 ID原样保留；校验真实日历日期、严格ASCII形态和完整路径预算，不修剪或重定向。新旧 locator 定向53项通过/3.19秒，转换集成17项通过/35.40秒，Ruff、compileall与严格规范校验通过；准确命令、候选源码、raw和失败归因在 `out/tests/temp/attachment_directory_locator_20261024/artifacts/`。首次单测失败是末尾对超长路径调用exists触发OS错误，路径预算拒绝已正确生效；改为比较父目录内容证明无写入，未修改生产行为或放宽拒绝。此批仍只供离线转换，不声称线上目录上传/切换或Windows实机完成。
- [ ] 2.3 迁齐 ingest/pin、用户/内部消息接受及媒体预览，发布目录与原件 URI；排队输入在 runtime 首次建立前也有可核验保活，不新增附件专用工具。

## 3. 统一 URI、文件 owner 与 Shell（后续实施）

- [ ] 3.1 在唯一 URI owner 扩展 attachments kind、workspace 目录模板和 child 编解码/导航能力，迁齐 DTO、Proto/生成类型和普通文件消费者；新生产引用不再生成旧 Session 单文件模板。
- [ ] 3.2 将附件目录导航/读取/派生区写入接入原文件 owner，保留原件限制、媒体读取、修订校验、私有存储和 containment 边界。
- [ ] 3.3 Shell 结构化 workdir 在目标机器解析目录 URI并持有目录操作 lease；后台处理进程继续占用直到真实终态，保留原生相对/绝对路径与命令语义。
  - 2026-10-24 已先行接入现有 files 目录 URI，删除工具层重复 cwd 解析/投影和额外 workspace_root 参数，全部创建入口由 Terminal owner 复用固定文件 owner。只将精确 `boxteam://` 前缀视作 URI，保留原生相对/绝对/工作区外、Shell 私有目录和 POSIX 含 `://` 目录名。完整 archive 红测为6单测失败，真实HTTP/PTY在第三种URI输入失败，证明旧实现误投本机相对目录。最终代码候选 `5f143640aa76378603c58d375ca27dfaa73e36d6` 的 unit-006 为63 passed/2.56秒，http-006 为1 passed，Ruff和compileall均exit0；覆盖中文/空格目录三种输入同文件、派生写入/原件不变、命令内URI字面量、两种冒号原生cwd、跨目标/非法URI拒绝且owner/lease/Node记录逐字不变、真实symlink环原路径仅在diagnostic/cause、verified删除与lease结清。http-002还覆盖工作区外绝对cwd及stdin完成（4 passed/45.26秒）；后续修改未改stdin链。独立只读审查提出的任意scheme误分类已修复并复核闭合。准确命令、源码指纹、raw/退出码和机械收据位于 `out/tests/temp/terminal_workdir_uri_20261024/artifacts/`。附件目录URI、后台全进程占用、Windows实机和跨机产品入口仍TODO，本项保持未勾选；002的错误引号断言及Ruff格式失败原件保留，003后已修正。
- [ ] 3.4 模型附件 manifest 提供目录/原件 URI 及处理输出提示，迁齐 Web 附件查看和工作区资源管理；前端变更完成真实类型检查与 Web build。

## 4. 历史与 fork（后续实施）

- [ ] 4.1 使用既有 immutable asset/version 冻结模型实际使用的原件、预览与派生输入；目录变化/回收后仍可重建原请求 hash，普通查看明确展示不可用。
- [ ] 4.2 迁齐统一目标检查与 fork journal/claim：同工作区共享目录关联、跨工作区冻结版本复制，保留原历史身份和 URI，失败/删除释放本次保留且不发布半个目标。

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
