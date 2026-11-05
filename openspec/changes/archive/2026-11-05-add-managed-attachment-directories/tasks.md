## 1. 已完成基础与本次规划修订

- [x] 1.1 已有 schema 4 独立目录、上传/接受、目录级关联与旧 blob writer 下线；保留原件保护和既有迁移入口。
- [x] 1.2 已接入统一 URI、原文件操作/媒体预览、Shell workdir 与真实进程占用；不新增附件专用工具。
- [x] 1.3 已有 immutable asset/version、历史请求重建、同/跨工作区 fork 和原 claim/publication/删除结算链。
- [x] 1.4 按 2026-11-04 用户裁定统一 proposal/design/specs/tasks：取消统一 30 天附件策略，改为无占用时检查修改并保留或释放工作副本；通用生命周期治理移出附件归档门槛。

第 2—6 组按本轮实现与实际验证原件逐项记录。旧 21/23 进度及 29/30 天测试仅证明旧合同，不计作新策略验收；此前完整批次可从修订前 Git 历史及原测试产物核验。

## 2. 原件来源与存量数据

- [x] 2.1 全仓检索并固定上传、接受、预览/读取、目录/文件操作、Shell、关联释放、fork、启动/删除及旧 GC 的全部直接/间接调用方和测试。
- [x] 2.2 复用原 immutable asset/version owner，在上传成功及跨工作区导入发布前可靠保存目标端原件来源；附件 catalog 绑定原身份与来源，不新增正文库或第二份可变附件。
- [x] 2.3 支持当前 schema 4 的原件、处理结果及旧期限；来源不明/缺件保留并报告，旧明确保留承诺继续保护。仅实际改结构时升 schema，并迁齐现有 schema 3 离线转换/安装/回退目标与生成物，不双读。

## 3. 最后关联解除后的唯一处理链

- [x] 3.1 在原目录 owner 实现相对初始可重建基线的修改检查，覆盖原文件工具与原生 Shell 的新增/修改/删除；排除系统可再生预览，既有处理结果不因下次无新写入而消失。
- [x] 3.2 迁齐 runtime unload、Thread/Session 删除、显式解除关联和失效代次对账：最后关联实际解除且无占用时，有修改保留原目录，无修改且原件可靠则释放工作副本。
- [x] 3.3 复用原目录锁及现有占用/接受/fork 事实，串行化关联、操作准入、检查与释放；其它 Thread、排队输入、后台进程或待核实操作继续保护目录，占用结清后沿同一 owner 再处理，不为释放附件强停操作。
- [x] 3.4 在原 catalog 记录必要的待处理/释放/重建事实，接通占用结清、显式重试和启动恢复；检查或保存失败保留原目录与原因，不另建任务队列、租约或全局进程扫描。

## 4. 普通访问、历史与 fork 迁齐

- [x] 4.1 原件/预览接口、资源导航、目录浏览、文件读取/编辑、Shell 和 fork 统一通过原附件 owner 按需重建；并发重建只发布同一资源/目录，原件缺失或损坏明确失败。
  普通读/列表/媒体/下载、文件修改、Shell、fork 及持久 outbox 回放已迁并有定向证据。watch 使用原 locator 和工作区 watcher，不因初始化或回放重建；reveal 的解析/stat/原生派发持原 access，已验证卸载竞争与重建。2026-11-04 最终源码核对未发现普通入口遗漏，复用下表已有产品证据；外部系统文件管理器持续使用期按下方无身份接管 TODO 延期，不声称已实现。
- [x] 4.2 保留 canonical/assembly 的实际输入版本及原 request_hash；释放工作目录不删除其依赖，不读取当前配置/文件补历史，不复制第二套完整消息或目录历史。
- [x] 4.3 同工作区 fork 先重建可释放原件并复用身份/关联，跨工作区保存目标端原件及选定冻结结果；导入派生结果在目标卸载后仍保留，required/optional 真实缺件和失败/删除结算合同不变。

## 5. 旧实现删除与合同同步

- [x] 5.1 所有附件调用方迁齐后，物理删除被替代的 30 天期限计算/到期查询、6 小时周期 GC、旧删除分支及无调用方字段；按新行为迁移或删除旧期限测试，不保留兼容策略或死调用。
- [x] 5.2 保留其它模块仍使用的资源账本/关闭能力、既有共享修复及历史 URI/v2 冻结解析；同步受影响 owner 规范/AGENTS/协议生成物，不将移出范围的未实现功能标为已完成。

## 6. 定向验收与归档

- [x] 6.1 通过产品上传/消息/卸载/原 URI 重开，验证无修改及仅系统预览目录释放、原件重建、稳定身份与原字节、真实缺件错误；关键状态由产品生成，不手填关联或恢复事实。
- [x] 6.2 通过文件工具与原生 Shell 生成结果，验证修改保留、连续多次卸载/重启不丢失、删除结果回到基线可释放，以及 Session 删除后工作区处理结果保留。
- [x] 6.3 验证多 Thread、排队输入、真实后台处理/延迟写入、关联/准入竞争及原占用结清后处理；覆盖释放/重建中断与新代使用，保持原身份、冲突、顺序及错误归属断言。
- [x] 6.4 验证同/跨工作区 fork、来源离线后的目标重建和历史请求 hash；按受影响实现补 Linux/Windows 文件及真实 PTY 检查，远端目录始终在目标机器解释。旧跨平台通过记录不能替代新策略验收。
- [x] 6.5 完成相关静态检查、OpenSpec strict，以及实际受影响的协议生成一致性/前端类型和构建；测试带进程外超时/内存保护，保留准确命令、源码指纹、原始输出和实际退出码。无需为未改动运行面重复全仓长回归。
- [x] 6.6 核对唯一调用链及旧实现物理下线，确认新合同全部必做项有证据后同步主规范并归档；仅文档更新不归档、不把旧 30 天验收记作新策略通过。

## 后续 TODO：不作为本次附件归档门槛

原 5.2“旧代操作接管/挂起操作取消终止核实”与 5.4“通用 operation/Session 删除/shutdown 的完整期限治理”移出本次实施清单。已实现部分继续保留，未完成部分按以下范围独立跟踪，不能勾选为已实现：

- TODO(resource-takeover)：没有可靠原生身份的旧代进程/浏览器接管，以及不合作 SDK、脚本或原生 I/O 的强制终止；附件在对应结果未知时继续保留并显示原因，不为归档补做通用接管。
- TODO(attachment-reveal)：系统文件管理器启动后持续使用期没有可靠占用身份；本轮仅保护原生派发期间，不新增常驻 lease、进程追踪或 GUI 关闭确认，也不将该延期项设为普通访问验收门槛。
- TODO(resource-shutdown)：整个 Backend/shutdown 的硬期限与上层启动器协调；MCP、Node Debug、Terminal/Browser 自身未直接影响本次附件目录链路的关闭、锁及 I/O 治理。
- TODO(resource-storage-performance)：共享账本写入、WAL、终态元数据和恢复长尾；原测量原件保留，后续以实际负载选择优化，不把遍历所有共享消费者设为附件收口条件。
- TODO(agent-collaboration)：团队、成员、委派与协作专项保持用户既有延期裁定。

## 既有证据索引

详细旧批次记录保留于修订前 Git 版本 `bf0f2e1d9d037efcb0210c458fa157fb4d54a27a`，原始测试输出不删除/覆盖。下表只说明旧实现的证据归属，不能代替第 2—6 组新合同验收。

| 已验证范围 | 既有原件 |
| --- | --- |
| 全仓调用盘点与旧库转换 | `out/tests/temp/attachment_directory_inventory_20261024/artifacts/`、`out/tests/temp/attachment_legacy_product_20261024/artifacts/` |
| 线上目录 owner、上传/接受与旧链下线 | `out/tests/temp/attachment_owner_commit_20261024/artifacts/` |
| URI、文件操作及 Shell 工作目录 | `out/tests/temp/attachment_uri_contract_20261024/artifacts/`、`out/tests/temp/attachment_shell_directory_20261024/artifacts/` |
| Windows 文件/PTY 与跨机器 URI | `out/tests/temp/attachment_windows_short_20261024/artifacts/`、`out/tests/temp/cross_machine_attachment_uri_20261024/artifacts/` |
| 历史冻结、fork、目录回收后的请求重建 | `out/tests/temp/attachment_fork_lifecycle_20261024/artifacts/`、`out/tests/temp/attachment_fork_gc_20261101/artifacts/` |
| 旧 30 天策略、GC 中断/竞争与跨平台生命周期 | `out/tests/temp/attachment_retention_measurement_20261024/artifacts/`、`out/tests/temp/attachment_gc_restart_20261024/artifacts/`、`out/tests/temp/attachment_stop_restart_20261026/artifacts/` |
| 共享 operation 原关闭批次观察 | `out/tests/temp/attachment_close_sol_20261104/artifacts/verification.json`；属于原共享治理证据，不替代新目录策略验收 |

## 本轮新策略验证原件（2026-11-04）

以下原件位于 `out/tests/temp/attachment_new_policy_20261104/artifacts/`，同名 JSON 记录准确命令、源码指纹、实际退出码与原始日志；正式 matrix 的工作区和进程产物保留在各自镜像路径。首轮失败原件保留，修复后只补受影响节点，不将包装脚本退出码记作测试通过。

| 已验证范围 | 本轮原件与实际结果 |
| --- | --- |
| catalog、上传/恢复、工作副本 worker、schema 4→5、维护及目录准入 | `core-unit-all-final-1793793970833503173`：175 passed，35.73s；含保留修改、缺根明确失败、同步/异步双错原实例、FIFO/symlink 结果与取消/账本重试 |
| 原文件 operation、恢复、观察事件与普通保存 | `core-unit-files-final-1793793056708901929`：69 passed，33.14s |
| 最后解绑与 content/list/media/ASGI 访问、排队交接、旧分片碰撞、观察恢复、FIFO 原 API 错误及观察锁序 | `access-lifecycle-matrix-1793793784113376646`：9 passed、1 failed；唯一失败是测试误匹配公开错误文案，原 cause 已保留。`access-lifecycle-disconnect-1793793930125533383`：完成/断连 2 passed、8 deselected，23.88s；10 个场景均有逐项通过证据 |
| 持久文件操作事件回放不重建已释放目录 | `outbox-replay-final-1793794842952162298`：1 passed、10 deselected，18.04s；真实 operation 生成事件、原文本工具删除结果并卸载后，核原 event_id、完整目录 revision/快照及 released/pending0 |
| fork 与释放跨进程竞争、Session/child 发布/删除/abort | `fork-lifecycle-final-1793794003953009290`：11 passed，128.40s；竞争的 pending 由真实条件创建/删除生成，未直接填 catalog |
| 工具结果、full copy、副本执行、多次 Backend 重启、Session 删除、显式删除结果回基线、历史及下载重建 | `frozen-history-final-1793794147960306137`：1 passed，178.47s；核原身份、URI、bytes、assembly 与 request_hash |
| 生产类型检查与完整旧 HEAD 对照 | `pyright-final-comparison-1793794928758961420.json`：当前 27 文件 235 条既有诊断，完整 `bf0f2e1` 基线 26 文件 244 条；按文件/rule/完整 message 多重集无新增。此结果不是全库类型全绿 |
| events 与新 file-access/schema 4 测试类型 | `outbox-types-complete-1793794976518058543`：3 文件、0 errors；生成器声明准确提供原 aclose 能力，无运行逻辑变化 |
| watch/reveal 导航入口与真实 API outbox 回放 | `navigation-integration-1793797654578122025`：6 passed、9 deselected，59.98s；真实上传/卸载生成 released，再核原 URI 重建和受限事件、目录错误分类、reveal 派发期间 holder、selected/private 持久回放；不包含外部 GUI 持续使用期 |
| 导航首批单测与静态对照 | `navigation-unit-1793797623599908796`：123 passed、8 fixture setup errors，7.23s；8 项停在未迁的 Gateway `backend_workspace_id` 构造参数，归独立身份切片处理。`navigation-static-1793797654529996885`、`navigation-js-syntax-1793797654676207477` 退出0；`navigation-static-comparison-1793798027656736883.json` 按原语义 AST 节点及重数核 Pyright 15/15、API Ruff B008 27/27，新增0，不能称类型或 API Ruff 全绿 |
| watch 最后解绑竞争与首轮定向检查 | `navigation-watch-final-1793798601260916326`：5 passed、11 deselected，48.59s，新增初始化阻塞期间最后 detach 已完整释放/pending0 的场景；复核 released 根、私有事件及真实 API 回放。`navigation-unit-final-1793798643363493289`：22 passed，2.04s。该轮 watch 5 项与首批未变的 reveal 2 项共 7 个导航场景，后续新增类型拒绝场景见下，不将重复运行相加 |
| watch 真实节点类型回归与最终收口 | `navigation-watch-node-type-1793799269988377918`：6 passed、11 deselected，60.47s；真实上传后由原生 Shell 子进程在附件根执行 `rmdir derived && printf result > derived`，公开 watch 入口按真实文件返回 400/NotADirectoryError，原件与生成结果保留，同时复核最后 detach 竞争、released 根及真实 API 回放。单次 stat 优先核实际节点，仅缺失的固定布局保留虚拟目录语义，不持正文 holder 或物化；本项未复验 PTY、Windows 或 Web。`navigation-node-type-static-final-1793799401657212994` Ruff 退出 0；`navigation-node-type-types-final-1793799401652778323` 与完整旧 HEAD 核生产诊断 15/15，新增 0，测试诊断 0；本批导航测试引入的 7 条流类型诊断已按实际 AsyncGenerator 合同修正。`navigation-node-type-comparison-1793799532512692685.json` 核精确语义 AST/重数、归档与 Git 源码一致，以及最终测试仅增加类型导入/cast，复用上述行为结果；原 API Ruff 源码未变，仍为 27 条既有 B008，不称全绿 |
| 导航最终静态及旧 Web 测试合同 | `navigation-static-final-1793798643345766954` 退出0；`navigation-static-final-comparison-1793798727991487815.json` 核最终生产源码 Pyright 15/15，API 源码未变复用 Ruff 27/27，新增0。旧 GC 事件和两组 30 天字段断言已物理移除，迁为 claim 保护、无修改释放及原 URI 重建；JS 语法通过。两项 copy focus 的 `navigation-browser-reuse-entry-1793798146521753755` 停在原 Python bootstrap 读取已删除 `active_workspace_id`，1 failed/94.40s，Chromium/MJS 未启动；不算附件浏览器验收。`navigation-workbench-reuse-1793798148110056744.json` 明确记录与正式构建一致的 890 个客户端源码及原 build/type 收据，未重复构建 |
| Ruff、compileall、矩阵和规范 | `final-static-complete-1793794459419517970`、`final-compile-complete-1793794459670264094`、`outbox-ruff-complete-1793794977716849676` 退出 0；`api-comment-ruff-comparison-1793794928758961420.json` 核 API 文件 27 条既有 B008 与旧 HEAD 完全一致且 AST 不变；`matrix-references-final-1793793815825635312` 核 27 个附件 suite、36 个精确引用；最终 strict 原件见本目录最新 `tasks-strict-complete-*` |

两条真实后端重启原件位于 `out/tests/temp/identity_convergence_20261104/artifacts/`：`attachment-process-live-final-1793794482374124887` 为真实后台父子处理进程跨 Backend 硬重启，1 passed、2 deselected，121.18s；`attachment-runtime-restart-final-1793794652556874428` 为 queued 输入与 stale runtime 重启对账，1 passed，100.65s。

runtime/entry/Shell/terminal、离线转换/安装/回退及原目录释放中断的并行验证由各 owner 保留独立原件，不与上表重复相加。外部 GUI 持续使用期按上述 TODO 延期。6.3 的附件占用与释放/重建中断已由下方补验证据核齐；通用进程接管、强停和完整关闭期限不作为补验要求。Linux 的 fork/历史及重启通过不能代替 Windows 新策略验证，6.4 与归档 6.6 保持未完成。

2026-11-04 旧代码收敛：物理删除无生产调用的 blob 身份/去重/locator 生成 API、解析包装及对应死测试，保留迁移和冻结历史的只读校验，4 个文件净减 197 行。两份定向单测 104 passed（16.78 秒），Ruff、compileall 均退出 0；命令、外部保护、源码指纹和原始输出位于 `out/tests/temp/attachment_new_spec_cleanup_20261104/artifacts/`。本批无前端或协议改动，不追加 Web、Windows 和长集成回归。

2026-11-04 6.3 补验：真实 Gateway/Backend、模型工具及父子 PTY 在附件目录未修改时跨两次硬重启保持原占用，随后由附件外门控允许首次写入，再经原管理入口停止并核结清、结果/locator/canonical 保留；`remaining-process-delayed-write-v3` 为 1 passed／130.35 秒。原 Shell 准入竞争 `remaining-shell-admission` 为 1 passed／21.83 秒。两项前后依赖指纹一致，最终 Ruff、Pyright、矩阵解析退出 0；原件在 `out/tests/temp/attachment_remaining_acceptance_20261104/artifacts/`。

同目录 `working-copy-recovery-fixed` 的前三参数已完成，整组累计超出旧 90 秒预算，实际退出 3；同指纹 `working-copy-recovery-after-publish` 精确补验 1 passed／24.20 秒、退出 0，核齐四个释放/重建中断边界。该 suite 预算按实测约 95 秒调整为 120 秒，保留 4GiB 保护；未重新运行调整后的完整矩阵，不称整组退出 0。此次未重跑 before-stop/after-stop、Windows 或 Web，原失败与补验记录均保留。

2026-11-05 公共文件系统收口（`ddb377c3f`）：Windows 句柄、目录同步、独占 rename 与私有 ACL 集中到 `app/runtime/windows_fs.py` 的 pywin32 边界，删除对应 ctypes／icacls；附件只调用公共原件保护及窄范围只读清理。Windows 新策略正式矩阵分别通过目录入口 18 项、工作副本恢复 4 项、目录 Shell 11 项（2 项既有跳过）、产品 fork／冷重启 1 项及终端回执 7 项；凭据错误定向 8 项（2 项 POSIX 场景跳过）通过。各批不重复相加；混合单元首批的旧平台预期失败及 Windows Pyright 既有 `os.fchmod` stub 诊断保留，不能称全部 Windows 检查通过。18 个 owned 文件及五份生产源码已按最终 manifest 逐项核 SHA；准确命令、原日志与适用限制见 `out/tests/temp/attachment_windows_new_policy_20261105/artifacts/remote_validation/passed-evidence-index-01.json` 与 `public_fs_before_after_11.json`。冻结历史首次启动缺少的环境凭据引用已通过既有同步入口修复；第二次已进入附件／工具／fork，随后在 Backend 重启端口绑定处失败，旁路原件保留，原因继续核对。6.4／6.6 尚未完成，不归档。

2026-11-05 Windows 冻结历史补验：10048 的完整现场确认公共停止装具只等待 venv 启动器，实际 Uvicorn 子解释器仍持有监听。公共 `terminate_process` 现于停止前枚举自有子树，并在启动器退出后等待实际子进程；硬退出调用方统一使用同入口的 `force=True`，不删除端口检查、不增加固定 sleep。`hyf-win` 正式 `integration-attachment-frozen-history` 矩阵 1 passed／189.15 秒、退出 0；原生普通停止／硬退出 Uvicorn 与装具单测共 8 passed，本地相邻装具 76 passed。公共停止实现及新增单测 Pyright 零诊断，Ruff 通过；宽查的 242 条诊断均在与 HEAD 相同的旧测试语句，未称宽查通过。准确命令、源码 SHA 和原日志位于 `out/tests/temp/windows_process_stop_20261105/artifacts/`；矩阵及回执已成功保存，临时采集脚本随后打印原始 Windows 日志时因 UTF-8 解码退出 1，另存 `windows-history-collection.json` 说明，未覆盖原日志或重跑测试；两次错误启动方式的原件也保留，不计业务通过。本次关闭 Windows 冻结历史重启缺口，不代替尚未闭合的完整跨 Gateway fork 验收。


### 2026-11-05 最终跨 Gateway 历史与归档核对

- `7e403e670` 将当前目标请求投影与历史 wire 冷读明确分开。历史读取复用原 assembly、canonical、detail、prefix 与 activation 校验，返回该次已封存的规范化 wire；不查询当前附件 alias、不读取当前文件或配置、不新增消息历史。新请求仍按当前目标投影。强反例覆盖独立已登记 owner 拒绝、返回副本隔离及 assembly/activation 损坏；Chat/Responses 所在完整文件 63 passed。后续图片 setup 因未走正式 matrix 被拒绝，保留组合退出1；只补正式 `integration-attachment-frozen-history`，1 passed／162.87秒、退出0。
- 正式 `integration-identity-source-child-fork`：2 passed／276.07秒、退出0，覆盖 main/child 来源隔离、实际后继执行、来源离线后目标工作副本释放与原 URI 重建、完整冻结请求和 request_hash；main 场景再完成第二次跨工作区 fork，关闭前两端后冷重启读取原请求并实际发送新目标 URI。模型请求数、来源身份、字节及 hash 强断言保留，不手填关键恢复事实。
- 准确命令、首尾 SHA、原始失败与最终回执保留于 `out/tests/temp/frozen_wire_20261105/artifacts/`：`frozen-wire-cross-gateway-with-source-services-1793867977877409747`、`frozen-wire-prefix-and-image-history-1793868280048412988`、`frozen-wire-image-history-formal-1793868474416575959`。最终 Ruff退出0；完整提交归档类型对照 `owner-types-final-comparison-1793868235602303677.json` 的84条既存诊断无增减，不称类型全绿。本批未改平台文件/PTY实现，复用前述新策略 Windows 产品与历史矩阵，不以Linux结果替代Windows。
- 原附件 owner 全部24份源码 SHA 与 `fork_transfer_batched_20261105/artifacts/attachment-chain-scan.json` 一致；旧期限/周期GC已物理下线，仍有命中的共享资源字段属于其它资源及只读格式拒绝。同步 `managed-attachment-directories` 和 `structured-user-message-attachments` 主规范后归档，通用生命周期、外部GUI持续占用及已登记性能TODO保持未实现。
