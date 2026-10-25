当前实施范围（2026-10-18）：A04 既有生产寻址/来源消费闭包已由 `c6c7b7af` 集成并接受，证据取协作收据 `accepted_A04`；这是源码闭包接受，不把未投影到原任务的验收整项补勾。补充 C1 的 registry setup 仍 UNMET。任务 1.4 的文件 child 语法编码已实现；新增目录根、Skill 包导航/child 文件访问及统一文件工具仍未实施。后续对表复用已接受范围，不按旧“无真实调用”描述重复开发。

## 1. 契约冻结与单一定义点（本 change 是唯一 owner；权威表已下发，据此定稿）

- [x] 1.1 建立**术语表唯一处**，按 2026-10-05 用户决定区分四层：整套机制称**统一资源寻址**，具体地址称**资源 URI**（简称 **URI**），业务层稳定身份继续称 **ResourceIdentity**，底层文件位置继续称**真实路径**。其余契约术语仍为 `作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；其余 change 只能引用本表。机制与具体地址共用旧名称的歧义由主线程按 6.6 统一清理，不保留同义别名。现有寻址语法、身份和授权行为保持既有合同；本项术语表与源码/公开字段正名已完成；验收见6.6。
- [x] 1.2 建立**scope 闭集与 scope_id 唯一表**（唯一处，定稿）：scope 闭集 = `workspace` | `user` | `gateway` | `inline`；scope_id 语义 = `workspace`→真实 workspace_id、`gateway`→**真实 gateway_id**、`inline`→**真实 distribution_id**、`user`→`local`（显式声明为单用户本地程序约定）。写明「scope_id 对所有 scope 都必填、MUST 由真实身份推导、MUST NOT 硬编码字面量」与「`memory` 不是 URI scope」。**`inline` 的 scope_id 来源与编码已按 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」定稿（发行 manifest 的 `distribution` + `version`）；`gateway` 的 scope_id 来源与注入 owner 已按 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」定稿（`identity.json` 的 `load_or_create_gateway_id`，Gateway 侧按请求经 `X-BoxTeam-Gateway-Id` 注入）。本表两行均已定稿，不再是初审或待定。** **（O-2 收口状态，2026-09-30 owner 复核）**：本项「定义点」已在 design 层建立（`design.md` D1–D12 与「契约的单一定义点」节）；本任务的实施闭包已于2026-10-19按唯一grammar与resolver binding核验，见文末具名证据。
- [x] 1.3 建立**拒绝码集中登记处（唯一处，分三套）**：分别登记 grammar 拒绝码（`grammar.py:25-44` 的现行17码快照；本次分段编码修订闭集见spec，未登记code构造仍抛 `ValueError`）、resolve 拒绝码（现有 6 个加专用于已识别 view 与资源 kind 不兼容的 `unsupported_view`，规范闭集共 7 个）与**联邦解析期第三套**（`app/gateway/federation/errors.py` 的 `FederationError.code`，归属 `app/gateway/federation/`），标明各自适用范围与「MUST NOT 混用」；写明「新增码只在此出现一次，其它 change/模块只能引用、不得自造同义码」及拒绝码闭合的可机械检查方式。 **（O-3 收口状态，2026-09-30 owner 复核）**：本项「定义点」已在 design 层建立（`design.md` D1–D12 与「契约的单一定义点」节）；本任务的三owner实现与跨域拒绝审计已于2026-10-19核验，见文末具名证据。
- [x] 1.4 实现统一 URI 语法的分段合同：结构/身份段保留严格 ASCII profile；只有 root 模板公布的文件 child 段使用 UTF-8 百分号编码和一次解码。引用 spec「URI 语法必须保留固定段序并单一实现」，不整体拒绝合法 escape，不放宽 Session/assembly 等身份。authority仍由3.4接通；query/fragment、编码分隔符、点段、控制字符、非法 UTF-8 在访问前拒绝。
- [x] 1.5 验收**原四值 kind 闭集**：`agent-spec` | `skills` | `config` | `session`（`config` 承载配置来源文件本身、`session` 承载会话上下文资源/会话定位，二者均为新增且在此定稿）。明确 `_RESOURCE_KINDS`（语法 kind，`grammar.py`）与 `_DESCRIPTOR_KINDS`（描述符 kind，`values.py`）是**两个独立闭集，不可混用**；**描述符闭集取值为 `agent-spec`/`skills`（原有成员 `memory` 已随提交 32bc6256 物理移除），语法闭集为 `agent-spec`/`skills`/`config`/`session`**，语法侧对两点式 `boxteam://memory/...` 以 `unknown_scope` 类拒绝码 fail-closed 拒绝。 **（此项验收仅覆盖原四值；2026-10-05 新增 files 与导航模板的生产接入由第8组未勾任务承接，不将原证据计作新增完成。）** **（本次落地，证据见 `out/tests/temp/uri_kind_closure/artifacts/report.md`）**：语法闭集 `_RESOURCE_KINDS` 扩为四值（`grammar.py`）；描述符闭集 `_DESCRIPTOR_KINDS` 保持 `agent-spec`/`skills` 不扩（`config`/`session` 是纯寻址 kind，无 payload 语义描述符，不进描述符闭集；`values.py` 已加注说明两闭集独立）。`config`/`session` 解析走发现式尾段（`/resources/{kind}/{...canonical path segments}`），`parse_resource_uri` 对二者要求非空规范化尾段；`SemanticResourceDescriptor(kind=config/session)` 仍以未知 kind fail-closed。证据用例：`tests/unit/services/infrastructure/resource_platform/virtual_resources/test_resource_uri_grammar.py::test_parse_config_and_session_kinds`、`::test_config_and_session_require_canonical_tail`、`::test_descriptor_closure_excludes_config_and_session`。

### 1.6 全仓越限项登记（判据式，含二级子目录）

判据沿用仓库既有门槛：目标目录 MUST 满足「直接源码文件 ≤20」「单文件 ≤800 行」「不与两个以上领域职责混合」，任一越界即 MUST 附拆分方案或含文件清单、职责/owner 映射、import graph、行数统计与复核结论的架构审查证据。豁免：生成目录（`app/protocol/generated/**`、`src/**/*protocol*_generated/**`）由上层 `generated/AGENTS.md` 声明不可手改，不计越限。

- 实测快照（提交 `69cc08fb`）：**本 change 归口的单文件 >800 行**：`app/gateway/main.py=3823`（**行数快照已随拆分失效**：现 main.py 为装配壳，路由在 `app/gateway/routes/**`，lifespan 在 `app/gateway/lifespan.py`，联动 `runtime_proof.py`/`route_registry.py`；本 change 已在 Impact 点名其 gateway federation 解析层与配置端点）、`app/gateway/control/gateway_state.py=3186`（**行数快照已随拆分失效**：现为同名包 `app/gateway/control/gateway_state/`，见提交 `579001a8`；Gateway 控制面库，`migrate-identifiers-to-uuidv7` 5B 逐表分类与 gateway_config 旧表裁定所在）、`app/gateway/config.py=2332`（**行数快照已随拆分失效**：现为包 `app/gateway/config/`；Gateway 配置来源，gateway 侧 `config_source_layers` 迁移落点，归属 `migrate-session-context-resource-uri` 的 5A.4/5A.5，非本 change 编号）、`app/services/infrastructure/config_service.py=2897`（**行数快照已随拆分失效**：现为包 `app/services/infrastructure/config_service/`；本 change 与 `migrate-session-context-resource-uri` 5A.x 的 config real path→URI 迁移横跨其 `config_source_layers.py`/`config_service_common.py`/`config_snapshot.py` 等模块 6 处调用点）、`app/services/infrastructure/workspace_state_store.py=2374`（**行数快照已随拆分失效**：实测现为 521 行；`config_source_layers`/`config_source_journal` 宿主，`migrate-session-context-resource-uri` 5A.2 在其上 DROP/ADD 列）、`app/services/infrastructure/workspace_config_source/workspace_config_source.py=907`（config 来源层写路径，`migrate-session-context-resource-uri` 5A.1/5A.2 的 `uri` 写入与 layer 兄弟字段所在）。这些文件必须在实施期拆分或提供架构审查证据。
- **本 change 归口的目录直接 `.py` >20**：`app/services/infrastructure=31`（config/trace/node_debug/session 族混居，本 change 5.x 的 config 来源代码即分布其中，须按 owner 下沉）。跨 change 归属：`app/core=38`、`app/agents=34`、`app/services/business=26`、`app/abstractions=23`、`app/schemas/internal_v2=28` 的主归属见 `add-itemized-rollout-context`、`add-context-injection-lifecycle`、`add-multi-workspace-backend-mounting` 台账，本 change 只引用不重复主张。

## 2. 三层分离与不变量

- [x] 2.1 定义三层职责的类型与持久化边界：`ResourceIdentity`（不透明、稳定、revision-free、不依赖激活工作区、持久化）、`URI`（可解析、持久化、允许悬空、禁 revision/hash）、`real path`（资源访问只在原生 owner 的最终点临时使用，不进模型、普通 DTO、解析结果或跨 Gateway 资源投影）；物理 workspace root 按多工作区 change 的唯一 Gateway binding 持久化，未注册目录根只按本 owner 的具名原生目录根绑定持久化。
  - 2026-10-20 类型入口闭包：实际 Registry 的 derivation descriptor/snapshot、资源依赖和来源 lineage 身份复用既有 validate_resource_identity，路径/URI/credential 形态在登记或发布前明确拒绝，capture/current/last_valid 不变；不新增身份规则、parser 或兼容表示，保留 namespace ID、独立 revision 与内部聚合必填 null URI。resource-identity-ingress-unit-001 为432 passed/3.25s（含45个身份负例与4个合法身份跨revision场景），static-001 两文件Ruff exit0；正式matrix resource-identity-ingress-aggregate-001 为2 passed/28.38s，真实 factory→Registry→execution/model-call→seal→SQLite→cold-read/fork 在两种Provider均保留绑定、null和原hash，删源后目标仍独立可读。独立限定复核确认原生产类型缺口已闭合。结合既有2.3/2.4/2.5及8.6的身份/解析结果/唯一原生根持久化证据，本项类型和职责边界完成；全部普通响应/业务记录审计仍由2.2/7.2承担，不据此宣称其完成。原始命令、源码前后指纹、输出与退出码在 out/tests/temp/openspec_takeover/artifacts/runs/resource-identity-ingress-*。
- [ ] 2.2 落实 **real path 不变量**的可机械检查：资源响应、普通响应 DTO、解析结果、联邦资源投影、业务持久化记录或系统生成的模型资源引用出现 real path 即为缺陷；具名的工作区/原生目录 root binding 以及两个新 requirement 登记的 Gateway 原始诊断日志/Browser 页面 URL 是限定例外，原生目录选择/注册及普通文件原生路径是具名请求输入边界，均不扩张输出许可；给出序列化/落盘/模型组装检查点与显式失败行为，禁止用脱敏或截断静默掩盖。
  - 2026-10-25 pinned fork 释放依赖缺失：target 控制库、source 控制库和source索引的三处拒绝改为公开稳定Session/Thread、artifact及阶段，完整物理位置写原诊断日志；gate、事务、preparing例外和target→source→target释放顺序的AST保持。产品消息及fork入口配合现有model_stream生成历史与pin，缺件拒绝不补建数据库、不释放claim/lease或修改canonical；恢复原件后冷Saver沿原记录结清，重复释放幂等。完整旧源码的三个反例全部失败；候选正式matrix 7 passed/80.12秒，共享7 passed/71.35秒，保留既有四个catalog提交前后中断场景；保留依赖单测9 passed，Ruff/compileall通过。共享matrix期间仅未加载的Browser客户端JS被并发修改，被测Python/配置/runner一致，完整指纹检查失败及限定复核均保留，不冒称全树冻结。准确命令、源码指纹、raw/退出码、反例归档、自查与合并收据见 `out/tests/temp/fork_retention_errors_20261025/artifacts/`；这是进程内产品/SQLite集成，冷Saver不是整个后端重启，不代真实Gateway/Web、Windows或2.2/7.2总门，两项继续未勾，团队与协作保持TODO。
  - 2026-10-25 历史详情路径错误：原 DetailFiles 统一处理结构拒绝与 lstat/mkdir/resolve 原生失败，普通原因只含 typed DetailRef、阶段和错误类型/errno，物理位置及完整异常保留诊断；读取与排他发布原语不变，未新增文件执行链。共享完整 fork/detail 套件41 passed/59.12秒，相邻存储及路径约束107 passed/16.89秒，Ruff/compileall通过。产品消息入口配合现有 model_stream 生成历史，验证拒绝符号链接、修复原件后冷 Saver 读取与 full_rollout_copy、来源身份及 request_hash 保持；冷 Saver 不等于后端进程重启。旧完整实现的读路径反例精确失败；直接 pytest 缺少 fork watchdog、禁用既有 fanout 初始化导致图拒绝以及首轮静态检查的失败原件均保留，已恢复正常装具且未追加团队功能实现或专项验收。准确命令、源码前后指纹、raw、退出码、自查和合并收据见 `out/tests/temp/detail_path_errors_20261025/artifacts/`；不代替真实 Gateway/Web、Windows 实机或全局不变量，2.2/7.2保持未勾，团队与协作继续TODO。
  - 2026-10-25 原生目录控制回包：绑定与解析的Pydantic校验合并到既有_request，普通400只公开操作、阶段、规则类别和数量，不复制input、loc、msg或context；完整回包及原ValidationError留在诊断日志和cause。旧完整HEAD上的单元及ASGI反例复现路径回显；候选80项定向与138项相邻通过，共享合并后218 passed/39.43秒，Ruff/compileall通过。覆盖损坏JSON、错误data/message、路径额外键、POSIX/盘符/UNC词法内容、信封/owner/原生绝对根拒绝、同request_id、失败零目录扫描、四表/原件不变及修复回包后原请求成功；不自动重放。首轮10项装具错误来自场景共用外部目录观察而未装配原绑定，改为每场景重建完整模板；一次测试移动路径误用为零用例退出4，原件均保留。准确命令、源码前后指纹、raw/退出码、自查和合并收据在 `out/tests/temp/native_directory_protocol_errors_20261025/artifacts/`。这是受控控制面回包的ASGI集成，不证明Gateway服务端未提交绑定，也不代替真实Gateway/Web、Windows实机或全部错误出口总门；2.2/7.2保持未勾，团队与协作继续TODO。
  - 2026-10-25 文件原生访问边界：目录扫描及文件路径解析在原owner处处理包含/不包含filename的OSError，统一复用原下载边界并迁齐延迟传输、清理及文本发布，删除file_download_access和文本私有_file_access。普通原因保留URI（尚未解析时为空）、阶段、类型、errno/winerror，完整原异常进日志及cause；不扩大下游formatter的errno判断，保留journal、锁及既有业务诊断，无文本扫描或第二文件链。旧完整实现的目录HTTP和文本预检两项反例均失败，精确复现仅errno时路径外泄；候选105项定向、147项文件/操作/恢复/模型导航及14项真实HTTP下载通过，新增阶段/URI断言后5项预检/上传/目录复验通过。共享合并后266 passed/49.43秒，Ruff/compileall通过；同request_id、403/404/400/409、四表及原字节、同ID上传和文本重试、Range与流式失败合同不变。运行中唯一变化为未选择的配置测试模块，原件核验按受影响范围复用，不称全树一致，见shared-limited-verification003.json。八处AST核原业务步骤不变，保留共享暂存索引；一次错误测试文件名的零用例退出4原件保留。命令、源码指纹、raw/退出码及自查在 `out/tests/temp/file_io_boundary_20261025/artifacts/`。不代替Gateway/Web、Windows实机或其它错误出口总门，2.2/7.2仍未勾，团队与协作继续TODO。
  - 2026-10-25 配置来源原生 I/O：共用稳定读取 owner 不再复制 strerror（其本身也可能含原生位置），普通原因保留操作阶段、异常类型、errno 及可用的 winerror；保留原异常类型与 cause，完整原文写具名诊断日志。来源、schema、缺失语义、重试和加载顺序不变，无新 loader 或响应扫描。完整旧实现的 reader 与 Gateway reload-status 精确反例2 failed；最终候选143 passed/81.81秒，Ruff/compileall通过，核四个读取阶段、配置/schema来源失败、同request_id、active/source/pending/events及原字节不变、故障移除后同配置恢复和关闭原owner后冷重开。六个初轮失败为测试在旧owner持锁时重开库，按独占合同调整检查顺序，未放宽业务断言。准确命令、源码指纹、raw、退出码与机械收据在 `out/tests/temp/config_source_io_errors_20261025/artifacts/`。HTTP为进程内ASGI及认证/最终文件I/O故障替身，不称独立Gateway、Windows实机或完整Web；2.2/7.2总门未勾，Agent团队与协作继续TODO。共享同组143 passed/81.67秒，Ruff/compileall与strict通过，源码前后一致、进程组无残留，核心单测合入现有configuration目录而不重建旧路径，保留共享暂存索引。
  - 2026-10-25 文件导航及文本失败出口：带 filename/filename2 的原生异常由现有文件模块唯一表达类型与 errno，完整原异常仅写诊断；HTTP 与文本修改移除各自重复格式化，上传 FileExistsError 沿同一边界保留409，模型导航保留原 owner/code/reason/type/errno 而不下发 strerror。全量迁移并物理删除两个旧 helper，原三种路径输入、URI业务错误、文件版本和执行效果合同不变。完整旧实现的同步/异步 ls 两项路径反例及目录/上传 ASGI 两项反例失败；修复后相关文件编辑、operation/恢复、文本发布/journal、API/SSE和模型读写270 passed/41.30秒，Ruff（排除既存B008）及compileall exit0。产品路由覆盖两种文件名属性的403/404/400与同request_id、四表及种子正文不变，真实OS暂存目录冲突409后原目录不被清理，同ID重试202经worker提交；文本预检失败零写入，故障移除后原请求完成。HTTP为进程内ASGI，认证依赖受控，目录/文本使用最终OS边界故障注入，不称独立后端进程、Gateway/Web或Windows实机验收。准确命令、源码前后指纹、失败原件、退出码及自查在 `out/tests/temp/workspace_navigation_errors_20261025/artifacts/`；首轮装具遗漏runtime属性/生产异常handler及filename2构造语义，修正后才计业务反例，原件保留。2.2/7.2总门保持未勾，Agent团队、成员、委派与协作继续TODO。
  - 2026-10-25 文件后继输出路径：删除WorkspaceService旧相对路径归一化和normalize注入，由原operation owner唯一处理relative_child_path，保留合法目录段首尾空格，不将原生绝对/盘符/UNC/URI或上级路径重解释为原输出下文件；拒绝错误不回显输入，原值留诊断。完整旧实现真实HTTP两反例均失败（24.58秒）：复制产物内空格目录的后继409，绝对后继路径400回显；最终产品复制202→worker提交→目录列表→后继创建202→查询终态正确落入原空格目录，9类非法输入受理前400且SQLite四表/种子正文不变。候选HTTP 2 passed/24.80秒，原文件编辑/operation/恢复64 passed/31.44秒，Ruff通过；普通path/source_path仍复用原三种输入解析，未收窄绝对路径能力。原件位于 `out/tests/temp/workspace_child_paths_20261025/artifacts/`；不代替Windows实机、完整Gateway/Web或2.2/7.2总门，团队协作保持TODO。共享复验64 passed/26.14秒、HTTP 2 passed/21.48秒、strict与compile exit0，源码前后一致且无残留；辅助只读审查无发现（review001.json），未冒称独立复跑。
  - 2026-10-25 文件 operation 持久回执：原SQLite读取owner只向调用方发布包含operation_id的明确格式错误，保留既有ValueError/HTTP400，原库位置及校验异常留原始诊断，不修改损坏记录或建立响应扫描。完整旧实现真实HTTP负例1 failed/12.80秒，普通查询精确返回/receipt-private/secret.txt；产品列表→创建202→worker提交→暂存回收生成关键状态后，POSIX/Windows/UNC/file URL分别污染created_path、item.path和directory_key动态键，经公开查询及同ID重试共24次拒绝，核同request_id、四表及文件不变，恢复原回执后逐次查询及最终同ID重试仍返回同一终态，不重建业务事实。候选回执/游标HTTP两项通过，日志调用规范修正后回执用例再次通过，文件operation/恢复与游标回归59 passed/30.59秒，最终Ruff/compileall exit0。准确命令、源码前后指纹、原始红绿/退出码和结构自查在 `out/tests/temp/workspace_receipt_errors_20261025/artifacts/`；不代替Gateway/Web、Windows实机或2.2/7.2总门，Agent团队协作保持TODO。共享最终日志调用版本复验59 passed/25.25秒、真实HTTP 1 passed/11.15秒，strict exit0，源码前后一致且无残留进程，见verification004.json。
  - 2026-10-25 文件树分页游标：原解码owner捕获目录键ValidationError，以既有400发布明确内容错误，原始键和完整原因只留诊断；复用中央URI校验，不新增解析器或响应扫描。完整旧实现12项非法键单测失败，独立真实后端HTTP负例复现Pydantic将原始输入诊断返回普通响应；最终相关单测及文件受理/恢复回归81 passed/31.39秒，真实HTTP覆盖POSIX/Windows/UNC/file URL在path、root及动态额外键共12项拒绝，核同request_id、SQLite四表与文件不变，原合法游标继续取得相同owner、revision、顺序和正文。准确命令、源码指纹、红绿raw、实际退出码及结构自查在 `out/tests/temp/workspace_cursor_errors_20261025/artifacts/`；仅为后端HTTP与值边界证明，不代替Gateway/Web、Windows实机或2.2/7.2总门，团队协作继续TODO。共享同组81 passed/25.92秒，HTTP 1 passed/11.13秒，strict exit0；共享运行唯一并行变化为未加载的Gateway联邦测试文件，按原raw/hash/退出码和其余源码一致性限定复用，不冒称全树一致，见shared-limited-verification004.json。
  - 2026-10-25 Gateway 辅助服务入口故障：缺失或非文件入口在原启动 owner 只发布 service、stage=entrypoint 与异常类型，真实安装位置完整保留在具名诊断日志；保留 FileNotFoundError 和既有 HTTP 状态，无响应端路径扫描或重写。完整旧实现的4项单测与真实 Gateway 注册 HTTP 负例均失败，后者在500响应精确复现绝对入口路径。候选单测47 passed，真实权限/日志目录/可执行文件/入口资产四类注册→清单→SQLite持久错误→Gateway重启复验为4 passed in 114.39s (0:01:54)，Ruff/compileall exit0；入口资产通过隔离安装目录提供完整当前 Python owner 并实际缺失 Node 资产，不修改源目录或替换启动函数。准确命令、源码指纹、原始红绿与机械收据位于 `out/tests/temp/gateway_auxiliary_entrypoint_20261025/artifacts/`。仅关闭启动资产错误出口，不替代2.2/7.2总门、完整Web或Windows实机验收；团队与协作专项继续TODO。共享完整四场景4 passed/104.56秒，但旧源码清单漏记移动/新增模块且运行中有无关React文件变化，仅按受影响范围限定复用；补齐当前tracked及untracked清单后，真实入口缺失复验1 passed/25.57秒、源码前后一致，机械收据verification004.json。
  - 2026-10-25 fork 资源策略值边界：`ResourceTargetFacts.resource_uri` 与 `ResourceTargetDecision.resource_uri/target_uri` 构造及 `replace` 共用中央 grammar，拒绝原生路径、file URL、空串、identity 冒充地址及 query/fragment；`omit` 不跳过校验。合法悬空 URI、编码文件段、远端 authority、独立 revision 及 live Terminal/Browser 的显式 None 保持原值，不增解析器或存在性查询。完整旧实现 `red-001` 为27 failed/6 passed（均为非法值未拒绝；unknown_scheme 预期在绿测前对齐中央闭集）；候选 `unit-002` 为308 passed/1.40秒，`activation-fork-002` 为4 passed/4.76秒，真实附件消息准入→来源捕获→fork→冷目标工具的正式 `attachment-fork-003` 为10 passed/44.95秒，Ruff/compileall exit0。准确命令、源码前后指纹、raw、实际退出码与机械收据位于 `out/tests/temp/resource_target_uri_invariants_20261025/artifacts/`；共享树正式附件fork复验10 passed/45.98秒、activation正式matrix复验4 passed；直接pytest首轮308 passed/4 setup errors因共享fixture要求matrix watchdog，改用现有matrix入口后通过。单测只证明值合同，产品回归证明现有调用方可用，未复现普通API输入绕过，也不替代2.2/7.2全局门或Windows实机验收。
  - 2026-10-24 SSH 转发清理后继闭合：工作区删除、失效投影对账及批量关闭复用原错误事实投影，完整原异常只进诊断；HTTP 分类由原端口 API 函数统一消费，7个调用迁齐并物理删除旧私有名称。保留工作区删除 PermissionError 的403与端口 API 的502，以及原404/409；不改变其它 owner 的失败归属。固定完整代码 tree `24566bfbd68062fb1853f4a75dea3151de915741` 的 `unit-006` 为126 passed/32.76秒，含四类原生路径×IO/权限错误，经公开删除及原投影提交/对账入口核同request_id、原异常完整留日志、失败不删registry/丢进程、后提交不回滚及可重试；批量close/reconcile核无关转发继续清理、失败句柄保留与重复重试零额外关闭。原实现 `red-001` 在公开删除错误含POSIX路径处失败（1 failed/1.96秒）；所有源码前后一致且无残留。`compile-006`通过；`static-006`核全部新增Ruff违规为零，路由27条既存B008与HEAD逐项相同，原始Ruff退出1不冒称全文件零告警；首次失效noqa已移除。结构自查无新增问题，既存测试根44个直接Python文件不扩大，非独立审查。准确命令、raw、指纹、基线差异及结构原件位于 `out/tests/temp/port_forward_cleanup_errors_20261024/artifacts/`；这是ASGI/受控进程边界证明，不替代真实SSH、Windows或全局2.2/7.2，整项未勾。
  - 2026-10-24 SSH 端口转发错误出口：移除普通回执对 SSH 日志/任意异常正文的拼接，runtime 与 HTTP 共用错误事实投影（阶段、类型、errno/winerror），进程退出保留 returncode；原异常/堆栈与 SSH 日志继续保存在具名诊断入口，不做路径匹配、脱敏或正文截断后发布。固定完整代码 tree `26263e88ddd4a49b9976cf03e4def467900bfe42` 的 `unit-003` 为95 passed/31.32秒，覆盖原端口转发、远端删除清理、SSH 命令及 Gateway 配置；新增四类路径×六个失败阶段的公开 HTTP 路由用例，核同一 request_id、原日志/定义不变、清理失败保留原进程并可重试、退出后同ID重连，以及404/409/502分类。Ruff `static-003`、compileall `compile-003` exit0，源码前后一致且无残留进程；旧完整候选 `red-001` 在列表 error 含原始POSIX路径的断言失败（1 failed/1.87秒）。首次静态两项未用变量/import格式失败原件保留，修正后通过。准确命令、指纹、raw及实际退出码在 `out/tests/temp/port_forward_errors_20261024/artifacts/`；这是受控进程替身下的单元/ASGI边界证明，不代替真实SSH、Windows实机或全局2.2/7.2验收，本项保持未勾。
  - 2026-10-24 运行回执 cwd_relative 根路径闭合：原宿主平台 Path/盘符校验在 Linux 放行 UNC、扩展 UNC 和反斜杠根路径；受保护完整 archive 的 `red-003` 在普通 Terminal 清单 API 返回 200 且包含真实 UNC（1 failed/0.68秒）。唯一校验改用纯词法 Windows anchor，统一拒绝 POSIX 根、盘符（含 drive-relative）、Windows 根和 UNC，不解析文件系统、不重写合法相对目标；原始值仅完整保留诊断日志。候选 `d08df5b9181256ed57c05177d0140152109429d3` 的 `unit-005` 为113 passed/5.67秒、`static-005` Ruff exit0，覆盖两kind×list/get×六种非法形态共24负例，以及八项合法相对cwd（含工作区外 ../ 和反斜杠形式），核400和原request ID响应头、明确错误、诊断原参数、owner/lease字节不变、零删除/额外创建及恢复原资源。004的24失败仅因最小API装具不装生产HTTPException handler而无body request_id，005改为显式发送并精确核响应头，生产未变；失败原件保留。准确命令、raw/退出码、源码前后指纹、机械收据和非作者只读复核位于 `out/tests/temp/runtime_relative_cwd_20261024/artifacts/`。本条只关闭运行回执的跨平台词法校验，不冒称Windows实机或2.2/7.2全局门通过。
  - 2026-10-24 外部资源清单身份错误闭合：Terminal/Browser 共用 index_resources 不再把服务返回的未经登记/重复身份拼入普通异常；普通500保留 kind、reason、清单index与同一request_id，原始身份完整写入具名诊断日志。固定完整tree `48a129b3d69a1e2d2be5111ed51e15c99e85cef9` 的 `unit-003` 为81 passed/4.46秒、exit0，含两kind×POSIX/盘符/UNC/扩展UNC/重复身份10项公开API负例，核拒绝后owner/lease文件逐字不变、无删除/重放且正常后续读取恢复；Ruff exit0。red-001保留旧API原值外泄，unit-002的75 passed/6 failed仅因日志repr转义与旧raw子串断言不匹配，后按原始LogRecord.args精确核对。准确命令、源码指纹、raw、退出码与机械收据在 `out/tests/temp/runtime_inventory_errors_20261024/artifacts/`；不替Browser自由错误正文、全局2.2/7.2或Windows实机验收。
  - 2026-10-23 原生外部资源异常：统一错误投影对 OSError（含 URLError 包裹的原生原因）只发布异常类型、errno 及可用的 winerror，不再用路径文本匹配决定这些错误的业务正文；原始异常、filename 和 traceback 仍由 owner 记录到诊断日志。资源 unknown、lease 释放和 BrowserRequestError 的超时/恢复 metadata 保持原合同。`out/tests/temp/openspec_resume_20261023/artifacts/runs/native-errors-unit-002/` 为 70 passed/3.36s，覆盖 POSIX、盘符、UNC、扩展 UNC 的直接/包裹错误，以及持久记录、重启 GET/list 和零自动重放；static-002 与非作者独立 Ruff 均 exit 0。首次整组 69 passed/1 failed 的原因是旧测试要求把 ConnectionError 自由正文写入业务 error，现按本合同断言系统错误事实并另核原始诊断；失败原件保留。其它非原生 producer 的既有文本兜底仍有 TODO，本切片不代表 2.2/7.2 全局完成，也不代替 Windows 实机验收。
  - 2026-10-22 文件条目名称入口闭合：目标目录仍经唯一文件 owner 接受相对/绝对/URI；multipart `relative_paths` 是目标下的相对文件名，移除原 `strip("/")` 的绝对名称重定向。上传统一在建 staging、读取任何正文及受理前校验整批名称，拒绝绝对根、Windows drive/UNC、空段/点段/NUL和重复；create条目名称与上传拒绝均不插值原始输入。固定完整tree `0b558309a2bf1d5fd69e7c1f7d68418651afc057` 的 `file-entry-errors-http-002` 为2 passed/13.54秒、exit0，真实HTTP17组非法上传/创建均400、request_id存在、目标及操作表零发布、暂存无残留；三种目标输入保中文/空格/反斜杠分隔上传、原字节及202→committed。`unit-001` 为42 passed/29.52秒，含既有操作/恢复及18项新反例；完整archive仅恢复旧生产的 `unit-red-001` 为18 failed/5.55秒，明确区分路径重定向、回显和读流顺序。Ruff static-002及compile-001均exit0。HTTP001负向通过、正向因新测试漏既有expected_workspace_id报422，修正调用后002通过，未改生产或放宽断言。全部原始命令/前后源码指纹/raw/退出码位于本任务artifacts/runs；本切片不代替2.2/7.2全局门或Windows实机验收。
  - 2026-10-23 用户最终裁定已同步proposal/design/spec：受现有管理授权保护、用户主动打开的Gateway原始服务日志/堆栈及联邦诊断同类日志保留原文；用户明确打开的Browser本地文件页面file: URL可保留在地址栏/导航请求/相关错误/页面快照/恢复检查点，始终由目标机器解释。仅具名入口与字段豁免，不包括普通业务响应、资源引用、其它持久记录或checkpoint文件/下载/截图存储位置；普通文件相对/绝对/URI三输入及单一操作链保持。此裁定取代历史收据中的“待裁定”，不把过去某些日志无路径断言当作原始管理日志的全局政策；登记不代替产品验收，2.2/7.2保持未勾。
  - 2026-10-22 Gateway健康协议错误闭合：真实独立Gateway和独立HTTP替身的 `workspace-health-errors-http-red-001` 为1 failed/9.98秒，原list错误确含非法健康身份的绝对位置。固定完整tree `a74981cd4da148174c3ef6d6478e35bcd94edce3` 的 `http-002` 为1 passed/10.48秒、exit0：POSIX/Windows文本及null三种非法身份均使checked list offline、probe 502，unchecked list和SQLite connection_error只保原绑定UUID及错误阶段；恢复后同一UUID ready。列表、启动/配置等待及pending恢复复用唯一健康身份校验，组/config proof公开失败字段、不复制异常值，非200正文仅留Gateway诊断，保原拒绝类型及超时；无DTO截断/脱敏或第二持久链。`consumers-002` 为202 passed/10.08秒（含新增28项及registry/federation/reconnect），Ruff static-002、compile-002 exit0。红与最终HTTP各4原件和SHA清单在同名run/product-evidence，全部run有进程外保护、源码前后一致、无残留；独审见workspace-health-errors-review-001.json。本条仅关闭具名健康响应出口，不代替2.2/7.2或multiworkspace4.3全局门。
  - 2026-10-22 Node Inspector原生sourceURL出口闭合：Node22.17.0/Linux受保护probe证明scriptParsed保原生绝对位置、paused空url经scriptId缓存命中；真实Gateway→Workspace→Node负例 `node-source-urls-product-red-003` 为1 failed/21.42秒、exit1，call_stack及last_stopped_frame确含原生根（函数uriSourceMarker、1:30），不再将NPM-002仅作潜在问题。唯一_source_path入口先按目标平台Path.is_absolute解释原生路径并复用既有file owner，native百分号不解码，file URI原authority/一次解码与内部/匿名/HTTP标签行为不变。完整tree `fcd72be5ea93f689ab8b05422569be60f12c5b9f` 的product-002为1 passed/23.02秒、exit0：产品配置/启动四个workspace/external×native/file-URI样本，核实际脚本/id、暂停函数与1:30、两种frame字段无原生根、引用API得同URI、URI读正文原字节、GET状态及stop exited。unit-001为12 passed/0.50秒，consumers-001为17 passed/0.79秒，Ruff static-002及compile-001 exit0；原生/file URI经cache与直接frame、外部binding、私有路径拒绝和标签保持分别覆盖。red001为测试helper参数写错，red002因V8忽略含空格sourceURL而仅匿名标签，均不计泄漏证据；green001因产品优先现有active配置而重跑首脚本失败，002显式创建/选择配置并新增精确断言，未改生产或放宽预期。产品与红003原件/SHA各在同名run/product-evidence，全部run进程外保护、profile前后源码一致且无残留；最终独审见node-source-urls-review-002.json。不声称Windows实机验证或全局UVAR2.2/7.2、multiworkspace4.3完成。
  - 2026-10-22 身份CLI错误出口闭合：唯一Workspace身份CLI边界不再把root、原始stdout/stderr或非法UUID文本拼入公开异常；目录/启动OSError保类别和errno，超时保TimeoutExpired和timeout，输出协议保RuntimeError/TypeError/ValueError，原异常cause与完整输出仅留Gateway诊断日志，正常UUID及read-only行为保持。固定完整tree `b6600b670f6ff1885d0a47b3f441b8f4fd59b51b` 的 `workspace-identity-errors-entry-004` 为1 passed/46.70秒、exit0：真实HTTP注册并启动→stop→外部损坏身份→start明确502→原身份字节未改→恢复原字节→同UUID ready；两health列表无路径，前置校验失败保持原offline投影及SQLite desired_running=false/connection_error=null，不冒称发布了新失败事实；root仅保合法binding，日志保损坏位置/内容。unit-002为20 passed/0.31秒且生产/单测与004逐字相同，consumers-004重连/托管111 passed/7.95秒，Ruff static-004 exit0。完整archive恢复旧生产的counterexample-unit-001为12 failed/8 passed，均由无路径断言区分；entry002最终因测试误写online失败，004依据现有DTO闭集修为ready且加强停机状态/SQL断言，未改生产。全部run进程外保护、profile前后源码一致且无残留；产品三份原件/SHA在entry004/product-evidence；独审见workspace-identity-errors-review-002.json。准确命令与raw位于本任务同名runs；不据此关闭UVAR2.2/7.2或multiworkspace4.3全局门。
  - 2026-10-22 托管工作区启动拒绝出口闭合：NPM-001确认权限校验的原生根可经 startup error→registry connection_error→普通工作区列表泄漏。唯一校验源头的缺目录、不可写根、非目录.boxteam及不可写.boxteam四支保持原异常类型和明确原因，完整位置仅进Gateway自有日志；不对DTO事后脱敏、不增加兼容链。固定完整tree `f54a3c37c42f91a80e00a64441de8c583491d12c` 的 `workspace-startup-errors-entry-005` 为1 passed/28.87秒、exit0，真实chmod→HTTP注册失败→Gateway重启，核两种health模式的offline与service错误、request_id、普通响应无根、SQLite保留原权限错误且root仅在合法binding、日志保留真实位置。`unit-001` 两文件9 passed/0.35秒、Ruff static-005 exit0；完整archive仅恢复旧生产实现的regression-red-003四项均在无路径断言失败。entry002/004把重启后owner离线投影误当原启动错误而失败，005分别精确核注册前后/重启阶段及SQL职责，未改生产或放宽状态断言；static001仅import排版错误。全profile前后源码相同且无残留，三份产品原件与SHA位于entry005/product-evidence；非作者最终复核见workspace-startup-errors-review-002.json，准确命令/raw/退出码位于本任务同名runs。不据此关闭UVAR2.2/7.2或multiworkspace4.3全局门。
  - 2026-10-22 补丁拒绝出口闭合：审查NP-001确认绝对补丁文件名会经ValueError→DiffError→API/工具回执回显；在唯一路径校验源头删除三处非法文件名插值，保留invalid_patch、DiffError、固定根规则及普通target原生绝对根，不对异常文本作事后脱敏。固定完整tree `1fa303d63072920aed55c48ce286ce52d874c921` 的 `patch-path-error-entry-003` 65 passed/14.55秒、exit0，Ruff static-003 exit0、源码前后相同；同步/异步工具共16项覆盖POSIX绝对名、Windows盘符/UNC、上级段、URI、等于根、空名和NUL，真实HTTP同八类×Add/Update/Delete/Move共32组，完整响应保owner/code/type且无原生路径，原文件、先行合法Add、越界目标及journal均无副作用。32份HTTP回执及既有权限场景原件封存在该run/product-evidence。早一轮entry-001另57 passed，整树archive只恢复旧生产实现的 regression-red-001 为9 failed/13.57秒，均在路径外泄断言失败；首轮Ruff仅新增import排版失败，已修正，不改断言。非作者review-003复核PP003/004新增边界；原始命令、前后指纹、raw和退出码在同名runs。本切片不关闭2.2/7.2及multiworkspace4.3的全量审计。
  - 2026-10-21 会话目录位置输出切片：SessionCatalogNodeDTO、Gateway 搜索 DTO/proto、Web 投影和 tooltip 删除 storage_relative_path，内部权威 resolver 保留定位职责；Workspace 私有快照和 Gateway UTC 创建时间保留名称/ID/原路径片段查询，普通输出及 schema2 缓存不含 locator。v1 缓存或带旧字段的 v2/远端导出明确拒绝，原字节和既有快照不改。catalog-output-position-unit-001 为31 passed/40.51秒，web-unit-001 为17 passed；e2e-final-003 四项真实目录入口通过，reload-final-004 补核真实 refresh 重载，覆盖 SQLite-only folder、分页、导航父链、循环拒绝、移动/改名不搬 Session 或改写 manifest、物理漂移409及 recursive settlement。product-001 真实 Gateway 聚合/重启/离线缓存1 passed/81.59秒；consumers-001 为9 passed、2 failed/549.99秒，恢复/通信/四种来源冷读及真实 Chromium 未注册目录编辑和双 owner 重启通过，生成首 Job 的原20次等待及 fork 汇报原40×0.1秒窗口失败，产品账本最终均完成，保持断言并保留 TODO，未计作完整生成/fork验收。官方 protocol/OpenAPI 生成、tsc、Web build、契约和严格 OpenSpec 检查通过；breaking 仅有本次授权删除 proto tag8 字段（原始exit1），已 reserved tag/name。14文件 Ruff 通过，catalog_search 保留原有2个TRY004和1个ISC004，15文件对基线无新增诊断。独立只读 CAT-LOCATOR-REVIEW-001 无阻塞。初轮 E2E 的500误预期已按原409修正；contract-001错误地在 Workspace OpenAPI 查 Gateway schema，final-002 已按各自真实 schema核验，均保留失败原件。原始命令/raw/源码前后指纹及退出码位于 out/tests/temp/openspec_takeover/artifacts/runs/catalog-output-position-*；产品 cache/catalog 原件在 product-001/product-evidence，限定汇总为 artifacts/catalog-output-position-validation-final-001.json。本项只关闭目录输出链路，不关闭2.2/7.2整体审计。
  - 2026-10-21 文件观察错误出口与文件响应：4个ASGI负例经真实文件owner在open/store.events阶段复现异常文本泄漏真实根；未声明异常现只返回owner/stage/type/errno的显式500信封，完整原异常/堆栈留在日志，已声明业务HTTP拒绝及独立订阅不变。red-001为4 failed，修复后unit-001为40 passed/4.79秒，含watcher、原状态码与收尾。真实Gateway http-final-003为1 passed（补齐Gateway registry前后全行不变），核roots/references/list/content、相对/绝对/URI同文件、未注册目录202创建/原operation终态、持久SSE快照及文件操作SQLite均不含真实根；原404/错owner400及零ModelCall保持。http-001的末尾fixture方法错误、static-001的导入排序失败保留，不计全绿；最终Ruff static-final-003 exit0。所有run前后指纹一致，无残留进程，准确命令/raw/退出码见runs/file-observation-uri-*，最终11份产品原件及SHA封存，汇总artifacts/file-observation-uri-validation-final-003.json；仅此文件链路证据，不关闭2.2/7.2全量DTO、持久化和模型引用审计。
  - 2026-10-20 删除排空错误与持久化屏障：子树 drain/CAS/隔离/恢复错误只报告稳定 Session/operation/artifact/阶段；rename 保原 RuntimeError，mkdir/fsync 保具体 OSError/errno/cause。独立审查发现并闭合 rename 后 fsync 失败的恢复缺口：首次与目标已存在的恢复共用三个目录 barrier，全部成功后才补记进度。`session-delete-uri-unit-003` 72 passed/19.81秒，核五处 I/O 失败、持续失败零进度、解除后实际重做三个 barrier、同 key 恢复和旁系不变；真实 Gateway `session-delete-uri-http-004` 1 passed/58.73秒，核目录冲突的 repair_required 回执与 SQL 原文一致、头/体 request_id、两侧原件及 owner 重启后原 operation settled/原 control inode/工具正文保真。Ruff `session-delete-uri-static-final-002`、compileall `session-delete-uri-compile-final-001` exit0；非作者复核确认原 barrier 风险闭合。HTTP001漏筛选被中止、002测试错期待enqueue为200、003移走控制库过早命中既有后台任务预检，均保留原件且不计通过；unit002因误用筛选profile退出5/零测试，已用正确profile补验。运行命令、raw、源码前后指纹及退出码仍在本任务 artifacts/runs；该切片不代表2.2/7.2整体完成。
  - 2026-10-20 调试存储和工具结果错误出口：调试 manifest/claim/配置共用受校验读取，JSON/schema/owner/符号链接及 I/O 失败保留逻辑 owner、阶段、原异常类型/errno/cause，不回显真实路径或损坏输入；只有真实初始缺失才可返回缺项，枚举/read race 不伪装成功。`debug-drain-catalog-uri-unit-001` 96 passed/10.34秒，含删除前 control catalog 缺失/目录/符号链接拒绝与原件不变；真实 Backend/Agent/Inspector 完整流程 `debug-storage-uri-http-final-002` 2 passed/96.22秒，最终 lstat 后六类损坏 HTTP 复验 `debug-storage-uri-http-post-stat-003` 1 passed/13.74秒，核409/request_id/修复后200。工具输出普通/并发内容冲突及读写错误统一报告资源 URI，`tool-output-uri-errors-unit-001` 12 passed；真实 Gateway 导航 `tool-output-uri-errors-http-001` 1 passed/37.59秒，核CRLF/Unicode原正文、删除后404/unknown_resource、不重建与恢复原字节。最终八文件 `uri-errors-static-closure-001` Ruff、`uri-errors-compile-closure-001` compileall exit0，相关run前后源码指纹一致；非作者只读审查无已证实阻塞。原始命令、raw与退出码见 `out/tests/temp/openspec_takeover/artifacts/runs/`。本批只关闭具名错误出口，2.2/7.2整体不变量仍未勾。
  - 2026-10-20 配置 schema 失败诊断切片：真实 reload→HTTP reload-status 最小复现确认原 config/schema 路径会被公开；校验器在产生异常处只保留 location 与 jsonschema 原因、原异常链，删除无校验用途的 config_path 参数，全部生产/测试调用迁齐。两项实际失败→保持原 active/无 pending 或事件→修正文件后清错场景 config-validation-uri-status-final-002 通过（2 passed）；同批 config-validation-uri-unit-001 的 Gateway 配置、配置安装/迁移和 API 合同共92项通过，唯一两项失败为新测试将空 tuple 错写为空 list，修正后仅补跑受影响两项，未重跑已通过检查。6文件Ruff static-final-002 exit0，非作者限定只读复核无阻塞。baseline-001保留修复前真实HTTP泄漏原件；准确命令、源码指纹、stdout/stderr及退出码均在 out/tests/temp/openspec_takeover/artifacts/runs/config-validation-uri-*。仅覆盖schema校验错误，不据此声称所有配置文件读取/解析错误已清理；本项继续保持未勾。
  - 2026-10-20 Gateway connection-id 迁移持久化切片：备份位置只在配置owner依据显式config_path与既有migration_id临时推导，创建/恢复/回滚共用入口，不在gateway_connection_ids记录持久化backup_path。内部KV记录统一升4版，旧开发记录及废弃native字段明确拒绝且不改原件，不双读或猜测迁移；用户配置版本和注释/连接身份合同保持。独审发现既有copy2备份中断可留部分最终文件，已复用原子写入发布冻结来源字节，补备份与source发布前后四阶段恢复及真实replace前失败用例。最终gateway-migration-uri-unit-final-003为68 passed/29.91s（两份Gateway配置与验收矩阵文件），覆盖SQLite重开、同migration/connection ID、精确备份/回滚、缺失/损坏备份拒绝、旧记录不静默迁移；3文件Ruff final-003 exit0，非作者限定复核确认已知备份缺口修复。首轮57 passed/8 failed均为新fixture缺必填private_key_path而停在schema，修正fixture后8项定向通过；import空行静态失败已修，原件保留。准确命令、源码前后指纹、输出和退出码位于 out/tests/temp/openspec_takeover/artifacts/runs/gateway-migration-uri-*；这不关闭全仓路径审计，本项保持未勾。
  - 2026-10-20 一次性迁移审计切片：移除 source_artifacts 的原生文件 path 与 quarantine 回执的闲置 path，read-only report 使用来源 artifact 名称/行坐标/原行哈希；导入后的既有 legacy-import/{migration_id}/source/rollout.jsonl 审计坐标字节不变，不造通用资源 URI 或兼容 reader。真实owner的7项成功/拒绝原件场景通过（legacy-migration-uri-integration-001，5.15s），核只读来源、无路径回执、SQLite report_json/持久audit与返回结果一致、quarantine保留全部私有原件及逐行hash；CLI direct/module 两入口完成 report→非空namespace导入→独立进程重开，2 passed/22.78s（legacy-migration-uri-cli-001），精确owner/新执行绑定和原source不变断言保留。6个变更Python文件Ruff通过，非作者限定只读审查未发现阻塞。命令、源码指纹、原件与退出码位于 out/tests/temp/openspec_takeover/artifacts/runs/legacy-migration-uri-*；未将这9项证据外推为完整migration或全部real path不变量，本项保持未勾。
  - 2026-10-20 离线恢复持久化切片：全仓核对 restore-manifest.json 及 source_path 消费者后，移除备份真实路径字段，不造新 URI 模板或兼容读侧；保留 operation/session/checkpoint 身份、来源/安装 SHA-256 与隔离文件名称和哈希。两条真实 Saver/SQLite 成功测试核清单无备份目录路径、来源哈希精确匹配原件、损坏目标隔离及重启后权威状态，连同 foreign session/thread、可读 target、无效 source 的拒绝且不改原件场景共6 passed；受影响3个Python文件Ruff exit0。准确命令、前后源码指纹、原始输出和退出码在 out/tests/temp/openspec_takeover/artifacts/runs/offline-restore-uri-*；本切片不代替迁移报告等剩余持久化审计，本项保持未勾。
  - 2026-10-20 Gateway 原生目录选择与注册切片：原生绝对路径、显式目录基准下的相对路径及 gateway/files URI 由目标 Gateway 同一解析器消费；目录列表/上级/主目录/条目均返回规范 URI，选中态和本机/managed/联邦注册直接消费该回执。远端输入原样交目标 owner，拒绝外来 Gateway URI、隐式相对基准与越界；不注册所浏览目录、不生成其 .boxteam，保留未注册目录文件编辑和快捷方式。枚举及注册错误从产生点保留原因/阶段而不回传真实根。最终 Python 单测 gateway-directory-python-unit-final-002 为15 passed/1.06s，Web 单测 gateway-directory-web-unit-final-002 为48 pass；真实类型检查、Web build、协议官方生成/兼容检查及精确路由/OpenAPI快照通过。真实浏览器 selector-final-003 为1 passed/40.37s，核原生路径→相对点目录→无匹配禁注册→URI恢复→搜索/上级/刷新/URI重开→实际注册；复用 browser-final-001 中通过的未注册目录编辑与双 owner 重启、聊天文件引用、三 Gateway/SSH 固定目标场景，未把该轮3 passed/1 failed表述为全绿，唯一选择器失败由上述最终复验覆盖。旧路由数量基线错误由完整31f5bbb1快照复现后修正，精确断言未放宽。7个变更Python文件Ruff通过，4个route文件的44条既存诊断与HEAD逐条相同无新增。命令、源码指纹、原件和退出码保留在 out/tests/temp/openspec_takeover/artifacts/runs/gateway-directory-*，最终selector源码前后指纹一致，产品原件已封存。相关2.1/5.2/7.2仅据此积累证据，本项不代替全仓持久化与响应审计，保持未勾。
  - 2026-10-20 Node Debug 启动配置切片：能力接口非空 program/working_directory 经目标文件 owner 发布 URI，保留空值及尚未创建文件的预填行为；实际启动、断点工厂与 fork 目标预发布复用 WorkspaceFilePaths，目标 workspace_id 改为必填。全仓迁齐生产与测试消费者后删除 fork 自有路径解析及 Agent WorkspaceToolPathResolver，不保留兼容模块；Agent 输入约束由同一配置工厂承接。工作区内三种输入、工作区外不重定向、外来 workspace URI 及非 Windows 目标盘符拒绝均有断言。最终 `node-debug-owner-unit-001` 109 passed/10.92s、`node-debug-owner-static-final-001` 14个现存变更Python文件Ruff exit0；正式matrix `node-debug-owner-http-final-001` 真实Backend/Agent/Node Inspector为1 passed/108.35s，核未来文件能力URI与随后HTTP启动/断点/变量/重启。复用未受最终工具入口方法迁移影响的 `node-debug-profile-lifecycle-unit-001` 54 passed及 `node-debug-profile-fork-core-001` 10 passed/56.06s（三种fork模式×两类目标的显式配置、漂移拒绝与冻结后source离线恢复）。前半与最终增量均经非作者只读审查，无已证实阻塞。首次HTTP在setup被未知9000监听拒绝；定位为完整集成套件下同名fixture覆盖，用专用fixture名恢复本文件预留9020端口，未终止未知服务。准确命令、源码指纹、原件与产品日志在 `out/tests/temp/openspec_takeover/artifacts/runs/node-debug-profile-*` 及 `node-debug-owner-*`。本切片不宣称Windows实机或全部普通响应不变量通过，本项保持未勾。
  - 2026-10-20 来源失败诊断切片：StableSourceReader 从错误产生点使用规范 URI、原因与阶段，Skill 固定目录使用 scope/scope_id/skill 逻辑标识；不把宿主路径写入 ObservedSourceRevision 或沿 accepted source 传入 catalog 诊断。系统 I/O 错误由来源 owner 的 read_failed 原因明确发布 unavailable，保留 exception type/errno 与原异常链，不混用 URI 拒绝码；上一有效 revision/content、恢复及 no-follow 合同不变，并修正打开时 ELOOP 的 errno 判定。`source-error-uri-unit-002` 95 passed/7.93秒，含真实 catalog 失败/恢复与全部 reader 隔离审计；`source-error-uri-pipeline-001` 1 passed/9.50秒，核真实容器文件 owner→语义图的无效来源/保留正文及无路径诊断；7个受影响 Python 文件 Ruff `source-error-uri-static-002` exit0。原件在 `out/tests/temp/openspec_takeover/artifacts/runs/`，均带进程外保护、指纹、准确命令和退出码，独立只读审查无阻塞。旧测试中要求错误携带真实路径的两条预期已迁为 URI，并保留原因/恢复断言；初轮93 passed/2 failed与import排序失败原件保留。本切片不代替全部普通响应和业务持久化不变量，本项保持未勾。
  - 2026-10-20 Python 工具结果切片：全仓核实无结果字段消费者后，删除系统附加的解释器真实路径，保留解释器选择、目标工作区执行、returncode 及原 stdout/stderr。`python-result-uri-http-004` 经真实 Gateway/Backend、Agent 与受控 Provider 验证工具结果，并在硬重启后核持久 message-stream 及 history 的正文、canonical tool identity、顺序与完整 data 一致，1 passed/40.18秒；解释器选择原两项单元测试在 `python-result-uri-http-002` 通过。最终 Ruff `python-result-uri-static-003` exit0，非作者只读审查无可行动问题。初轮测试调用不存在的 restart helper、误用进程内 Job events 作冷历史及把 Provider local tool ID 当 canonical ID，失败原件001–003保留；改用既有 hard_restart、持久 Turn stream/history 及权威 stream 的 ID，未放宽身份断言。原始命令、源码指纹、保护与退出码均在 `out/tests/temp/openspec_takeover/artifacts/runs/`；本子切片不关闭整项路径审计。
  - 2026-10-20 Node Debug 源码 URL 切片：Inspector 在帧产生点经既有 WorkspaceFilePaths 将 file URL 转为规范 URI，工作区外源码交原生 Gateway 目录绑定；保留工作区相对 path、原断点/Web打开、内置/匿名标签及程序输出。帧解析移到工作线程，绑定等待不阻塞后端事件循环，失败不回退真实路径。42处 Service 构造与全部 Inspector 测试迁齐。最终 `node-debug-source-uri-final-unit-005` 102 passed/46.69秒、`node-debug-source-uri-admission-001` 43 passed/19.74秒、`node-debug-source-uri-final-http-003` 14 passed/129.61秒（真实 Backend/Agent/受控 Provider/Node Inspector，核工具与HTTP的URI、原断点、变量、重启/人工操作）；Ruff及compileall `node-debug-source-uri-final-static-002` exit0。非作者只读审查提出的同步Gateway I/O已修复并复核。早期夹具目录ID/作用域名错误、9000端口被未知服务占用的失败原件保留；改用本文件分配端口块内的偏移，不终止未知进程。原始命令、源码指纹、输出、保护和退出码均在 `out/tests/temp/openspec_takeover/artifacts/runs/`。无产品前端或协议字段改动；未证明Windows实机或全部普通响应不变量，本项保持未勾。
  - 2026-10-21 文件树快捷方式错误出口：session/workspace 配置的 stat/read/validation/atomic write 在唯一 owner 发布工作区身份、文件类别与阶段；OS 错误保留原类型、errno 与内部 cause，JSON/UTF8 保留位置，校验错误只公开规则与数量，不回显控制文件路径或损坏输入。真实 HTTP 修前复现500泄露会话物理路径，修后 `file-tree-errors-http-green-001` 8 passed/12.29秒，覆盖产品创建及添加/设默认后两种 scope 的形状、校验、真实读写权限失败、原件不变与修复重试。`file-tree-errors-unit-final-003` 52 passed/2.86秒，覆盖定点 I/O、JSON/UTF8/校验、三种输入一致与既有快照继承；双故障保留写入及清理两条原因链，清理拒绝仍可能遗留 temp，未改既有策略。原件、准确命令、源码指纹和退出码位于 out/tests/temp/openspec_takeover/artifacts/runs/file-tree-errors-*；本切片不代替2.2/7.2全仓审计，保持未勾。
  - 2026-10-21 工具选择存储错误出口：唯一锁/读改写 owner 发布 artifact/operation 与错误类别，OS 和锁库错误保留具体类型、errno（可用时）及 cause；JSON/UTF8 只公开位置，磁盘损坏 Agent 键与原始输入不回显。schema、选择 revision、共享读/独占写及10秒锁期限不变。真实 HTTP 修前复现配置路径外泄，`selection-errors-http-green-001` 8 passed/41.76秒，覆盖真实 GET/PATCH、损坏输入、非root读写权限、跨进程真实锁超时、原件及修复重试；`selection-errors-unit-001` 62 passed/7.13秒，含既有 ToolService、快照与锁模式，明确区分 replace 失败尚未发布和 unlock 失败可能已发布，不将错误伪装成回滚。4个Python文件 Ruff/compileall exit0，成功run前后指纹一致且无残留；准确命令、raw与退出码在 out/tests/temp/openspec_takeover/artifacts/runs/selection-errors-*。该切片不代替2.2/7.2全仓审计，保持未勾。
  - 2026-10-21 会话默认设置错误出口：唯一 SessionDefaults owner 在 read/write 边界保留 OS 类型/errno/cause，JSON/UTF8 保原异常类别与位置；形状/版本错误及磁盘已存 Agent/provider 的无效值只公开 artifact/field，原输入保留内部原因链。请求输入校验、schema、缺文件静态默认值与唯一写入链不变。修前 HTTP 复现控制文件路径外泄；`session-defaults-errors-http-green-001` 8 passed/27.62秒，从真实默认Agent/provider写入后验损坏值、JSON/UTF8、非root权限、原件不变和同入口恢复。`session-defaults-errors-unit-final-002` 22 passed/4.10秒，含既有 ConfigService 静态层不变/重启读取与损坏拒绝，另核定点 I/O、同类异常坐标/cause、缺文件不创建。共享工作区fixture并发变更后补验 `session-defaults-errors-joint-final-003` 30 passed/20.75秒、前后源码指纹一致且无残留。3个Python文件最终Ruff/compileall与strict OpenSpec均exit0；只读独审无阻断。准确命令、指纹、raw及退出码见 out/tests/temp/openspec_takeover/artifacts/runs/session-defaults-errors-*。仅关闭该 owner，不代替2.2/7.2全仓门。
  - 2026-10-23 Terminal HTTP 错误出口闭合：TerminalManagerClient 不再把完整响应正文拼入 RuntimeError，统一抛 TerminalRequestError，保留 method/status、内部 path/response_body 与原 HTTPError cause；完整正文显式写诊断日志，普通异常/API、owner.error 只保类型与HTTP失败事实。四种POSIX/盘符/UNC/扩展UNC正文在旧实现的 `terminal-http-errors-red-001` 全部复现泄漏；`unit-001` 为44 passed/2.01s，覆盖真实client拒绝→API500/request_id→unknown落盘→重启GET/list一致、lease释放及零自动重放；`integration-001` 为1 passed/10.96s，真实PTY服务同步/异步的非法cwd/启动失败仍显式拒绝、原cause/状态和服务诊断保持，旧detail字符串消费者已迁齐。Ruff `static-001` exit0，各run受测指纹前后一致且无残留；准确命令/raw/退出码与源码指纹见 `out/tests/temp/openspec_resume_20261023/artifacts/terminal-http-errors-proof-001.json`。本切片不改变500与unknown合同；Browser自由错误正文、其它producer和facts.py剩余正则TODO仍未闭合，2.2/7.2继续未勾选。
- [x] 2.3 落实 **identity 独立于 URI**：同逻辑名跨两个不同 scope（如 `user` 与某个 `workspace`）为两个不同 identity；跨来源等价/覆盖是独立 concern；给出「覆盖只影响后续解析与 catalog 快照、既有 identity 与已封存绑定稳定」的验证点。
  - 2026-10-20 2.3 复核真实 Skill owner：`uri-identity-shadow-002` 通过（1 passed、1956 deselected、28.19s、exit 0）。Gateway 与 Workspace 同名 Skill 由真实 watcher/producer 发布，前后 catalog 指向不同 entry identity/URI；已 tracked 的 Gateway identity、revision、封存 frames 与正文保持原值，新 Saver 重启后分别恢复旧 catalog 与新 catalog，历史 skill_load 不 rebind。`uri-owner-runtime-closure-001` 同时覆盖同 URI revision 更新、缺历史快照明确失败、同名跨 scope 的独立身份及 provenance 无路径矩阵；生产恢复走 activation/CSM owner，不使用当前 URI 重解历史。准确原件均在 `out/tests/temp/openspec_takeover/artifacts/runs/`。
- [x] 2.4 落实 **「locator 是输入，不是输出」**不变量：解析命中（含跨 gateway）只返回稳定身份与内容，返回值 MUST NOT 携带 locator；给出可机械检查点。
  - 2026-10-20 2.4：`SemanticResourceDescriptor`、`ResourceProvenance`、`ResolvedResourceHandle` 的闭合字段及 identity/snapshot_ref 校验拒绝 locator 形态；resolver 只返回 typed handle，导航 DTO 只返回 URI/shape/operations/child_path/内容。`uri-locator-output-001`（9 passed、exit 0）覆盖真实 provenance 序列化、locator 风格 snapshot_ref 拒绝和远端响应注入 locator 后 HTTP 显式失败且不回显。`uri-navigation-auth-real-triad-001`（1 passed、66.28s、exit 0）覆盖真实三 Gateway 直达/relay，回包不含各 node workspace/gateway_home 根；该测试与生产导航源码指纹仍与原件一致。`uri-owner-runtime-closure-001` 已通过完整 virtual_resources/Skill runtime，当前 values/resolver 指纹未变。本项只收口解析输出，不代替 2.2/7.2 全部业务持久化及普通响应的路径不变量。原件均在 `out/tests/temp/openspec_takeover/artifacts/runs/`。
- [x] 2.5 落实 requirement「「位置」必须有可机械判定的统一判据」（6 条 Scenario）：以唯一 owner 的两项事实判据检验持久化与输出，覆盖「可由 owner 身份推导的位置不落盘」「不可推导但需寻址时用既有 API 端点」「owner 自身运行态字段以相对于工作区根的原生相对路径持久化」「物理工作区根只作为原生 Gateway 控制面绑定持久化」「判据可机械判定」「配置应用事件的路径字段不是位置」。`add-workspace-persistent-resource-management` 的 D-A2/D-A3 与运行态字段边界只引用该判据；六条 Scenario 各须至少一条真实 owner 的生产/持久化/序列化断言，不另造无生产消费者的分类器。
- [x] 2.6 落实「普通文件入口必须保留原生路径输入并统一目标解析」的公共边界：登记相对/绝对/URI 三种输入、明确目标目录与目标平台、工作区外既有能力/授权及具名请求输入例外，保持 URI 输出/持久引用不变量；消费者引用同一定义，不自行增 kind、parser 或独立执行链。普通文件工具实现仍由 `add-unified-resource-file-tools` 承接。公共定义见 spec 对应 requirement 与 design D1a/D1b，生产及真实页面证据见文末；Windows 实机仍归文件工具3.4，不由本项勾选代替。

  2026-10-20 文件引用消费验收：聊天 Markdown 在转 HTML 的 URL 编码前封装文件目标，保留中文、空格、字面百分号及 Windows 分隔符；不再用 URI 根猜物理路径。所有目标先原样交同一内容 owner，仅普通相对简写的真实404可沿既有唯一文件名查找，显式绝对路径/URI与403/502不回退。预览引用以回执 URI 为准。`file-reference-final-unit-007` 25 passed，包含真实 MarkdownContent→Provider→HTTP 参数回归、URI 文件树简写和私有目录不下钻；真实 tsc `file-reference-final-web-types-006`、正式 Web build `file-reference-final-web-build-005` 与 Python Ruff `file-reference-final-static-004` 均 exit 0。完整页面 `file-reference-browser-005` 2 passed/85.17秒，经真实 Gateway、Workspace 与 Chromium 验证六种链接、行定位、外部目标/同名 decoy、缺失绝对路径、内联 URI、刷新重开，以及未注册目录编辑、URI 快捷目录和双 owner 重启；Provider 仅为受控本地装具，不代表真实外部模型验收。`file-reference-markdown-repro-001` 保留原生输入被 URL 编码的失败原件，浏览器001为用户消息纯文本装具不匹配、002为外部原生路径编码缺陷，均不计通过。所有运行命令、保护、源码指纹、原始输出及实际退出码位于 `out/tests/temp/openspec_takeover/artifacts/runs/`。真实 Linux 远端与模型写能力验收仍引用文件工具3.4/4.7的具名证据，未据此声称 Windows 实机或全部路径不变量通过。
- [x] 2.7 按本 owner「原生目录根绑定必须由目标 Gateway 唯一持有」已定稿的 gateway/files/{directory_id} 根，同批迁移文件树响应、编辑、快捷目录持久引用及重开消费者；保留原浏览/编辑能力，不伪造工作区身份、不自动或强制注册。验收真实目录内容与编辑结果、快捷目录重开、工作区注册表不变及 URI 输出/持久化不变量，不能以模板缺失为由删功能。
  - 2026-10-20 `native-directory-browser-006` 经完整生产 Web 页面、真实 Gateway/Backend 与 Chromium，读取并编辑未注册目录中的中文/空格/百分号文件；页面刷新及 Backend、Gateway 双重重启后以新浏览器 context 从持久快捷 URI 读回相同正文。普通响应 request_id 一致且无原生目录，快捷文件只存 URI；注册表完整业务行及除 checked_at 采样时间外的全部 Workspace DTO 字段前后相同，目录无 `.boxteam`。修复普通文本的编辑按钮仅在 debugMode 显示的缺口，保存仍消费原 owner 的 revision；模型未在本场景调用，协议桩不冒充外部模型 E2E。运行 exit0/55.63秒，无残留进程；`native-directory-web-static-001` 真实 tsc 与 `native-directory-web-build-001` 正式 Web 构建 exit0。原件在 `out/tests/temp/openspec_takeover/artifacts/runs/`，截图/HTTP回执在正式同名测试 artifacts 下。初轮 Chromium socket 路径、URI选择器、LiteLLM价格表网络阻塞及健康检查采样时间误比较的失败原件保留，未放宽身份或内容断言。

## 3. scope、scope_id 与 gateway authority

- [x] 3.1 实现定稿 scope 闭集 `workspace` | `user` | `gateway` | `inline`（含 `grammar.py:18` 的 `_SCOPE_KEYWORDS`；`_SKILL_SCOPES` 已不存在、全仓零命中；grammar.py 已无 `builtin` 字面量），实现 `scope_id` 段**对所有 scope 都必填**，把「依赖当前激活工作区 / 当前 gateway / 当前发行版补齐 scope_id」判为失败。
- [x] 3.2 实施 `builtin` → `inline` 正名（含 layer `bundled` → `inline`），向 config 域既有词汇（`app/schemas/internal_v2/config.py:20`）收敛；**删除** `app/agents/skill_runtime.py` 的 `bundled`→`builtin` 改名 shim（该 shim 已由 `298ef599` 物理删除，`layer_order` 现为 `(inline, gateway, workspace)`，无需再删）；同步 layer 名与 scope 名（`_SKILL_LAYER_LABELS` 亦已随正名移除）；不保留任何运行时别名。同步更新断言 `tests/unit/agents/test_skill_runtime.py` 与 `virtual_resources` 测试中 `scope="builtin"` 的用例，避免零回归红线破裂。**只改真依赖 URI scope 的位置**：`grammar.py:18` 的 `_SCOPE_KEYWORDS` 与 `resolver.py` 的 scope 构造校验（grammar.py 已无 `builtin` 字面量、`_SKILL_SCOPES` 已不存在）；无关同名（工具 origin、主题来源、`builtin_tool_registry`）MUST NOT 误改。
- [x] 3.3 建立**真实身份来源**并据此推导 scope_id：`gateway`→真实 gateway_id，取 `${BOXTEAM_HOME}/gateway/identity.json` 的 `load_or_create_gateway_id`（`gateway_<32hex>`，按 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」由 Gateway 侧按请求注入并经 `X-BoxTeam-Gateway-Id` 读取，MUST NOT 用 host:port/监听端口、MUST NOT 用进程级单例；**已落地**，见本条修订注）；`inline`→真实 distribution_id，来源与编码按 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」从发行包 runtime manifest 的 `distribution` + `version` 推导（来源已定稿，装配状态见本条修订注）；`user`→`local`（单用户本地程序约定）；把 `skill_runtime.py:539` 的 `else "local"` 拆为 `gateway`→真实 gateway_id、`inline`→distribution_id，并建立 `ResolutionContext` 第一条生产构造链路。 **（修订注（2026-10-01 第四轮复核更正）：`inline` 部分已由 `298ef599`+`f3bd8213` 落地——`app/core/distribution_identity.py` + `skill_runtime._layer_scope_identity`；`gateway` 请求级注入**已落地**，提交 `64ba30c8`（父 `ccec61a0`）——Gateway 侧按请求注入 `X-BoxTeam-Gateway-Id` 并接进工作区后端 `ResolutionContext`/`require_scope_binding`，`53befbfc` 补重启恢复的 pending job gateway 身份载体，`9881a3b2` 补三条代理通道）** **（修订注 `f6fc990f`，2026-09-29）**：`user` 部分的「scope_id 推导」已经落地——`app/core/user_identity.py::user_scope_id` 单点返回 `local`（不虚构用户名），与 `inline` 的 `app/core/distribution_identity.py::distribution_identity`、`gateway` 的真实 gateway_id 推导同族；resolve 时经 `ResolutionContext.user_scope_id` 显式携带并由 `resolver.require_scope_binding` 校验（`app/services/infrastructure/resource_platform/virtual_resources/resolver.py`、`values.py` 的 `ResolutionContext`），`user` 不再是「parse 成功而 resolve 无绑定」的悬空 scope。对应测试：`tests/unit/core/test_user_identity.py`、`tests/unit/services/infrastructure/resource_platform/virtual_resources/test_resource_uri_resolver.py`（`user` scope 的 scope_id 绑定与拒绝无绑定用例）。本条 `gateway` 请求级注入与 `inline` manifest 推导均已完成（见上修订注与 `298ef599`+`f3bd8213`），本任务 3.3 完成状态以自身门槛判定。
- [x] 3.4 实现「其它工作区 = `workspace` scope + 另一个 `workspace_id`」且不新增 scope；实现可选 gateway authority 段并固定三种含义：缺省 = 本机 gateway、== 本机 gateway_id = 等价缺省、== 对端 gateway_id = 跨 gateway；且 authority MUST NOT 承载瞬时通道标识（channel instance/epoch/route）。
- [x] 3.5 承认多工作区前置条件：一个后端进程可挂载多个工作区，scope_id 身份在寻址层（HTTP API 与 URI）显式；持久化数据不绑定「当前激活工作区」。实现细节由 `add-multi-workspace-backend-mounting` 承载，本 change 只提供寻址层要求与验证点。

## 4. 星型 gateway 解析链

- [x] 4.1 实现唯一解析顺序：本地 parse（fail-closed）→ 无 authority 或等价本机时由本进程按 workspace registry 解析 → authority 指向对端时按联邦关系转发（本地是 hub 可直接解析直接 spoke；本地是 spoke 经唯一 hub 做一次有界 transit）→ 不可达/未共享/未找到 fail-closed 返回结构化拒绝码。禁止回退本地猜测路径、空路径或虚假默认值。
- [x] 4.2 把上界做成**显式策略常量**（集中定义，非散落魔法数字）：`visited set`、`max_transit_gateways=1`、`max_gateway_hops=2`、总 deadline；声明「拓扑变化时改策略而非重写解析器」，并覆盖超上界/超 deadline 的显式失败。
- [x] 4.3 固定跨边界传输契约：只传 identity、URI、revision 与资源内容；绝不传 real path、provider locator 或 credential；对应「locator 不是输出」不变量。
- [x] 4.4 实现集中登记的跨 gateway 失败语义（按**联邦解析期闭集**选码：`federation-unknown-peer`、`target_not_resolvable`、`federation-channel-closed`、`target_ambiguous`、`federation-deadline-exceeded`、`federation-transit-limit-exceeded`；不得混用 grammar 或 resolve 闭集），并保证「未授权存在」与「不存在」返回同一结果、不泄露 locator；未知 gateway 不按名称猜测路由。


### 2026-10-20 通用只读导航联邦验收

- 3.4/3.5、4.1–4.4、7.4、8.5：Gateway 正式 `/api/v1/resources/navigation` 复用唯一 grammar、registry 和原 read grant/channel；Workspace/Skill owner 执行读取，回包子 URI 保留固定目标 authority，普通访问不登记长期文件身份。
- `out/tests/temp/openspec_takeover/artifacts/runs/uri-general-navigation-triad-001/`：真实 Gateway/SSH/三节点与本地导航 2 passed，覆盖 hub 直达、spoke 经唯一 hub、目录/搜索/读取、中文编码/Skill child/截断、无 activation/tracking、缺失/私有目录/未知 peer。`uri-navigation-frame-regression-001` 的 43 passed 进一步核本机与联邦工作区 owner、拒绝归一。
- `uri-navigation-multiple-mounts-001`：1 passed，同一后端两挂载、独立 Session/Job/目录，在激活第二工作区并提供相反选择头后，两个 URI 仍各自读到原根。
- `uri-navigation-http-auth-001`：59 passed，两个公开导航端点用真实 FederationCredentialStore 凭据验证直读拒绝，固定远端 Workspace 统一走签名 read grant；`uri-navigation-auth-real-triad-001`：真实三 Gateway/SSH 1 passed，复核 canonical 远端调用与对端凭据拒绝。独立增审见本轮 `uri-navigation-review/final-auth-fix-review.md`。
- `uri-navigation-grant-negative-001`：68 passed；`uri-navigation-final-boundaries-002`：47 passed，导航只允许 read、逐跳绝对 deadline、不扩 hops/transit/visited 预算、撤权/恢复/断线零额外 owner 调用、非法回包不外泄原文；`uri-navigation-owner-http-001`：27 passed，补实际 Workspace HTTP 目标/剩余预算、非法 owner 响应及 transport 错误不外泄 locator。
- 协议生成、真实 Web TypeScript 检查、Web build、OpenAPI 导出、compileall 均 exit 0；新增/接线代码 Ruff 通过。被触达但仅装配/删 wrapper 的 lifespan.py/workspace_proxy.py 原有 10 条 Ruff 诊断与 HEAD 基线相同，不表述为全仓静态通过。
- 本证据仅收口这条只读导航与星型联邦链；文件树普通 DTO 的真实路径治理、其它模型/持久化不变量和独立文件工具消费仍由各自未勾任务承接。

## 5. 默认寻址政策、语法收敛与配置来源迁移

- [x] 5.1 由 `app/core/resource_uri.py` 唯一定义统一语法，经 `app/services/infrastructure/resource_platform/virtual_resources/` 公共出口消费，替换旧资源形态；在**同一原子步骤**内修正 `app/agents/skill_runtime.py:52`（`boxteam://workspace/agents`）与 `:619`（`boxteam://workspace/{id}/resources/skills/catalog`）两处绕过 owner 的裸拼接，避免删定义与修消费方之间出现悬挂中间态。
- [x] 5.2 使解析链接入生产链路：Skill、配置、状态等引用默认以 URI 解析与传递，real path 只在最后访问点出现；新增持久化字段若需定位资源一律用 `identity + URI(+ 独立 revision 字段)`，禁止存 real path。
  - 2026-10-20 生产接线验收：Skill/AGENTS由来源owner私有handle读取，经graph/Registry使用identity、URI与独立revision发布；activation冻结并由唯一Saver持久化，历史Skill读取封存snapshot，不重解当前URI。配置inline走唯一URI构造器、不可寻址层显式null；session/state定位使用既有领域URI及identity，不复制真实根。复用 uri-owner-runtime-closure-001 的完整virtual_resources/Skill runtime、2.3的真实同名覆盖/重启和2.4的locator-free解析输出证据；本批 resource-identity-ingress-unit-001 的432项及 aggregate-001 的两种Provider完整封存/恢复/fork复验通过，原资源身份、URI、revision和hash一致。非作者限定复核确认此项生产接线可独立收口；不代替2.2/7.2的全仓输出与持久化审计。
  - 2026-10-20 生产消费者清理：Web 不再用 `.boxteam/skills/…/SKILL.md` 路径或普通 `read_file` Markdown 推断 Skill 来源/激活，四条展示投影改用唯一 `skill_load` 成功回执。最终定向测试30项、真实类型/构建与现有唯一 E2E owner（真实三视图、停止跟踪）通过；证据见 itemized §9.6 及 `out/tests/temp/openspec_takeover/artifacts/runs/skill-receipt-*`。本条保留未勾，未把展示切片等同全部资源解析接线完成。
- [x] 5.3 删除旧形态与其解析实现：不提供别名、双读或兼容层；移除解析链路中「仅打印地址」的半接入状态，确保解析/授权侧有真实生产调用。
  - 2026-10-20 5.1/5.3 核验：唯一 grammar 与 Migration Plan 第2步一致，旧 `virtual_resources/grammar.py` 已物理下线；Skill/AGENTS URI 由各自 owner 构造，`skill_runtime.py` 两处旧裸地址已移除。Workspace/Gateway ResourceNavigation 的真实入口调用中央解析、scope binding 与操作授权，session adapter 只消费中央 grammar。`uri-owner-runtime-closure-001` 覆盖整个 virtual_resources 组与 Skill runtime，exit 0，原件在 `out/tests/temp/openspec_takeover/artifacts/runs/`，非作者只读审查确认生产调用闭包。附件/工具结果旧 scheme 仍由仅规划的文件工具 change 承接，不计为本项迁移完成。
- [x] 5.4 按 D10 迁移**已确证的 real path 持久化违约**：把 `app/services/infrastructure/config/state.py:475` 的 `ConfigSourceLayerRecord.source_path` / `:485` 的 `backup_path` 与 `:632` 的 `ConfigSourceJournalRecord.source_path` 换成 URI；`app/api/config.py:102` 的 `path=str(source.path)` 改为 URI，**直接复用 config 侧既有平级属性模式**（对齐 `app/core/config_sources.py:16` 的 `path` + `layer` + `precedence` 与 `layer_revision`/`layer_digest`/`source_generation` 兄弟字段），即「`path`→`uri`，其余 sibling 原样保留」；不另发明第二套结构。 **（固定1c20源码核验及已集成C03/URI收据复用：config layer/journal现行存储为nullable uri，旧path/backup列已下线，公共API与生成类型已统一；5.6的source control/activation既有源码闭包已由A01–A04收据接受，完整task级覆盖另行核对；后增文件工具迁移仍仅规划。本项证据见本轮 `addressing-task-evidence-review/attempt-1/report.md`、`coordinator/artifacts/c03-main-validation.json` 与 `uri-terminology-plan/validation-attempt-1/python-boundaries.json`。）**
- [x] 5.5 实现配置来源文件的 `config` kind 与既有 URI 形态：可寻址的发行包 `inline` 来源以逻辑资源名构造 config URI；`layer` 与 `precedence` 保持为 URI 外的兄弟字段。**（已落地：`config` 已进语法闭集，inline 来源使用稳定发行文件与 `workspace_inline` / `gateway_inline` 逻辑资源名；具体地址与 nullable URI 规则由 spec owner requirement 唯一定义。本任务不裁定 config layer 值。）**
- [x] 5.6 按来源控制与activation唯一owner核对当前schema/真实producer，补必要字段并接通新URI写入和读侧；内部MCP/Skill catalog聚合按spec「内部语义聚合资源不得伪造 URI」统一为必填nullable display_uri，同一闭包迁齐descriptor/snapshot、Registry、provenance/serde、activation读写/schema/fork/detail及必要协议/投影/fixture，合法Skill条目地址保留。历史157 live + 44 dev/temp库零命中仅作旧基线证据，不要求实施后仍零落盘。删除不兼容开发中间数据，不扫描升级旧URI、不增alias/双读；新增附件/工具结果旧scheme源码消费迁移由文件工具change验收。
  - 2026-10-20 补齐 nullable 聚合 URI 生产闭包：`d390e292` 统一必填 nullable 字段；新增真实 factory 测试覆盖 Skill 与空 MCP catalog producer、Registry capture、execution/model-call freeze、seal、SQLite null、合法 Skill URI 的 workspace 身份与规范形态、full-copy 后 source 离线及目标 Saver 冷读所有 binding payload。两个 Provider 协议均通过，完整 snapshot、bindings/provenance hash 与源值相同；有 members 的 JCS v1 null preimage、缺字段/空串/非法值拒绝复用此前 10 项定向矩阵。原件在 `out/tests/temp/openspec_takeover/artifacts/runs/aggregate-uri-lifecycle-005/`（2 passed、exit 0）、`aggregate-uri-lifecycle-static-003/`（Ruff exit 0）及 `uri-owner-runtime-closure-001/`；非作者只读核实真实入口与 source 离线边界。来源控制与 activation 的既有 writer/restore 接线仍复用本 change 的 A01–A04 已接受闭包；不扫描或升级旧开发数据。此项不代替 7.2 的其它真实路径不变量，附件/工具结果旧 scheme 消费仍按文件工具 change 单独实施。
- [x] 5.7 保持已有 mutable source 的读侧 `layer`/`precedence` 一致：`user`/`user_local`/`workspace` 不因 SQLite carrier 被改写成物理层名；同一 source key 的来源构建与快照基线恢复使用各自配置 owner 的唯一权威映射。**（已落地并核验：workspace 的三条读路径一致，相关实现见 `523e3cd8`。本项只记录这些既有来源的读侧一致性；runtime override 的逻辑层改名与 snapshot 排除来源清单由任务 5.8 跟踪。）**
- [x] 5.8 按 spec requirement「配置 layer 必须表示逻辑来源且与载体和快照分离」实施 layer 轴收敛：Workspace 的 `workspace_runtime_override` 读侧由旧 `sqlite` 改报逻辑来源 `runtime_override`，保留 precedence；Gateway 的 `active_snapshot` / `pending_snapshot` 旧伪来源项从 `sources[]` 移除，MUST NOT 重映射为 `runtime_override`。SQLite carrier 仅作内部实现，不作为 layer 或新增公开 storage/carrier 字段；各配置 owner 的 source-key 权威映射供所有读路径共用。`runtime_override` 没有稳定可寻址来源文件时允许 `uri=None`，其身份仍独立于 URI，按 owner identity policy 处理；MUST NOT 为 carrier 或 runtime override 编造新 kind/URI 地址。旧开发中间数据可删除并重建，MUST NOT 增加兼容读取、回填或双轨。同步更新受影响的来源 DTO/proto/API contract、生成类型与契约测试；不得新增公开 carrier/storage 字段。**（C01/C02 已集成 `f42b6f77`；闭集为 inline/user/user_local/workspace/runtime_override，Gateway 快照从各来源 owner 的真实 baseline 恢复。R08 独立审查无阻塞；主树 API/Gateway/Workspace 113 项、OpenAPI 合同 14 项通过，Ruff、协议/OpenAPI 生成、Web 类型检查与构建通过。证据：`out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/c01-main-validation.json`。本项仅验收逻辑来源切片，DTO 字段正名由独立任务 5.9 验收。）**
- [x] 5.9 完成 Workspace/Gateway 配置来源的公开 URI 字段正名：nullable `uri` / `schema_uri` 替换旧 `path` / `schema_path`，保留其余来源事实；不可寻址返回 null，protobuf reserve 旧号和名称、新字段 optional，不新增 alias。**（C03 `69d77eea` 已集成；R11 独立审查无阻塞，主树 46+10+73+2 项关联验证及协议检查/类型检查/Web 构建通过。证据 `coordinator/artifacts/c03-main-validation.json`，位于本轮 `out/tests/temp/2026/10/04/024121-team-execution/`。持久化 real-path 替换仍由 5.4/5.6 独立验收，不因本项完成而勾选。）**

## 6. 与其它 change 的收口（消除多套定义并存）

- [x] 6.1 更新未归档的 `openspec/changes/add-context-injection-lifecycle/tasks.md`：把 `3.14`（解析器本体）标注为「由本 change 的语法/scope/scope_id/kind/拒绝码定义取代」，把未完成的接线任务 `6.6` 与 `7.1` **指向本 change**（引用而非复制定义）。
- [x] 6.2 更新该 change 的 `specs/context-injection-lifecycle/spec.md` 中 URI resolver requirement 的归属声明：写明 scope 闭集定稿为 `workspace`/`user`/`gateway`/`inline`、`memory` 已确证不是 URI scope，语法、scope_id 语义、kind 与拒绝码以本 change 为准；保留其自身行为要求（隐藏 locator、activation snapshot 恢复、`skill_load` name-only 等）。
- [x] 6.3 与其余三个 change 对表：`migrate-session-context-resource-uri` 复用本 change 的 scope/语法/kind 归属；`add-multi-workspace-backend-mounting` 复用显式 scope_id 身份要求；`add-workspace-persistent-resource-management` 复用持久化引用政策；四方术语、scope 名、scope_id 语义、kind 与拒绝码写法一致，无同义异名。
- [x] 6.4 收口在途 change 的现行旧词与 `memory` 形态：CIL proposal 的三层 Skill 来源、design 的 SkillCatalog 来源图及 tracked/rebind 优先级、spec 的 catalog 来源/manifest/同名优先级、tasks 的4.1/4.1-A/6.6 均统一为 `inline`；URI 示例采用 `inline`，`memory` 明确为非 URI scope，itemized session-turn-history 的资源详情示例同样一致。按 requirement「正名 builtin 到 inline 与 layer 改名必须同步且不得误改无关同名」核查，只保留历史调查、原名删除说明和拒绝示例。2026-10-10 文档交叉核账完成；本项仅收口规范文本，不代替源码正名、消费迁移或生产验收。
- [x] 6.5 收口第四个并行 change `add-workspace-persistent-resource-management`：复核 owner 主记录的稳定 `resource_id` 是身份键，不是资源位置、real path 或 provider locator；需要引用外部资源位置时仍遵守本 change 的 identity + URI 政策。2026-10-10 实施核账确认终端/浏览器采用既有 `/api/v1/resources/{resource_id}` → 领域 owner → transport 同 ID 的绑定，符合位置判据 D-A3 的既有 API 端点边界；没有 `resource_id` → `boxteam://` URI 绑定，也不据此自造 terminal/browser URI kind。八个相关源码/测试与已接受 HEAD 字节同，deps 的三个资源依赖函数 AST同；收据 `COORD/artifacts/resource142-binding-verification.json` 与 `resource142-deps-function-verification.json`，COORD取团队实时台账坐标。本项仅关闭原绑定复核；持久资源 spec 已明确 owner 为自己创建的同一资源保存的主记录与真正外部资源/产物引用的边界，后者仍须 identity + URI，不以本勾选豁免。资源生命周期、真实路径禁止或 URI 生产验收不由本项代替。

- [x] 6.6 由**主线程**统一实施 1.1 的术语收口，在相关业务候选固定后清点源码符号、模块/文件名、公共 DTO/proto/API 字段、测试、当前文档与未归档 OpenSpec 的直接和间接引用；必要的命名改动一次迁移全部消费者并使用官方生成器，不保留同义类型、字段、函数或兼容别名。机制与具体地址不得共用一个名称，`ResourceIdentity` 和真实路径不得改作 URI。名称调整保持 `boxteam://` 字节、scope/kind/authority、revision/hash 边界及寻址/授权行为；不为正名重新设计语法。已有 change 路径或 capability 名需要调整时，同步全部引用，禁止只改目录留下悬空链接。原始失败日志、固定补丁与历史报告保留原件，检索验收明确排除这些历史证据。门槛：当前源码及规范旧名称零命中或逐项证明确属无关同名；引用检查、相关静态/契约测试、协议生成、Web 独立类型检查与构建通过。主线程已完成精确正名、全部当前引用与官方protocol/OpenAPI生成；317项后端/契约、16项前端及50项文案受影响寻址测试通过，各组有重叠、不相加；独立tsc、Web build、严格OpenSpec全量40/0通过，78Python静态相对基线无新增诊断。固定收据在 `out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/uri-terminology-plan/`；历史raw/补丁与另一聊天handoff草稿保留，两个bun.exe二进制非术语引用。

## 7. 验证与收口

### 2026-10-25 Windows Node 调试存储长路径（2.2/7.2 部分收口）

- 原唯一调试store的枚举、读取、原子写入与删除在最终I/O边界统一使用`native_io_path`；manifest、launch claim和方案继续共用原链，逻辑locator、DTO、URI、错误归属与持久格式不变。作者AST核对去除四处原生路径转换及import后原模块完全一致，无新目录/兼容链。
- Linux最终存储/准入83 passed/10.48秒；Windows快照`fff98ffc753d8a40a92a7b943ec95fb3c606bc3d`的存储71 passed/14.78秒、原服务/API准入12 passed/11.16秒。新增冷store场景验证超过260字符的真实方案路径、登记正文、更新、删除和manifest清单刷新；原三个Windows服务入口长路径失败已通过，业务断言未改。
- 两处测试工作区准备统一到正式`out/tests/<测试路径>/workspace/`和既有prepare入口，故障注入/原件观察使用原生I/O路径；远端只注入离线占位key，不同步凭据或请求真实Provider。首轮命令转义、缺占位key、旧rmtree及单测前置Session创建长路径的失败原件均保留，不计为产品通过。
- Ruff/compileall通过，Pyright 1.1.414对产品文件零诊断（首次`uv run pyright`因未安装入口未执行，随后复用uvx工具通过）。准确命令、源码指纹、raw/真实退出码与作者结构自查位于`out/tests/temp/node_debug_native_io_20261025/artifacts/`。本片不代替调试进程、真实Gateway/Web或全局路径不变量总门，2.2/7.2仍未勾。
- TODO：Windows单测原runtime/tmp深工作区在调试store调用前于`SessionCreationService._prepare_staging`的`main_thread.mkdir`复现WinError3；本片按规定使用正式workspace仍保留长方案路径验收，Session创建/恢复的原生I/O作为后续独立边界，不能声称该缺陷已解决。团队、成员、委派与协作继续暂缓。

- [x] 7.1 增加统一构造/解析矩阵：固定 resources/scope_id、身份闭集和大小写 profile、技能根/SKILL.md/多层child、files/root、中文/空格/点号/字面百分号、合法 escape 规范化与只解码一次；拒绝无效 escape/UTF-8/编码分隔符/点段/控制字符/query/fragment，未登记 scope/kind和错误 scope组合显式失败。身份版本的既有编码规则不因文件 profile 改变。
- [ ] 7.2 增加三层分离与不变量测试：real path 外泄即缺陷（普通响应/DTO、业务持久化、系统生成的模型资源引用与解析输出检查点；具名的工作区/原生目录 Gateway root binding 为持久化例外，原生目录选择/注册和普通文件路径按具名请求输入边界核验）、URI 悬空合法、revision 不进 URI、identity 不承担寻址、同名跨 scope 是两个 identity、locator 不在解析输出中；内部聚合必填null在publish/seal/restart/fork/hash中一致，缺字段/空字符串/非法非空地址仍拒绝、不降级null、不伪造默认地址或额外展示内部身份。；检查必须按具名日志管理入口与 Browser 页面 URL 例外逐字段划界，不能扩大到普通错误、资源引用、checkpoint 文件及下载/截图位置。
  - 2026-10-25 原生目录控制回包子边界的反例、218项共享复验与局限见2.2及 `out/tests/temp/native_directory_protocol_errors_20261025/artifacts/verification003.json`；仅集中现有两调用方校验，不据此关闭普通响应与持久化不变量总门。
  - 2026-10-24 普通文件输入分类切片：目标平台原生绝对路径优先，只将输入开头的协议形式交给唯一 URI grammar；paths、普通导航、文本路由/条件写入和 Gateway 原生目录选择共用分类，不再因路径中间含 `://` 拒绝。POSIX `./build://debug`、对应绝对路径和 URI 经同步/异步 ls/read/glob/grep/write/edit 到同一文件，回执仅 URI；file:// 未知协议、非法 URI、私有存储、外部绝对路径拒绝及版本竞争合同保留。代码候选 `517b12926a89f822641d8155fd7b83458a25f95f`：350 passed/1 skipped（19.20秒），真实 Gateway→Workspace HTTP 三输入读写/重读1 passed（23.24秒）；相关 Ruff/compileall exit0，Gateway `_shared.py` 全规则仍有4条既存 TRY004，已逐项与修改前基线比对无新增。原始输出、源码指纹和收据在 `out/tests/temp/native_file_scheme_20261024/artifacts/`；不将此切片视为全仓路径不变量或跨平台/跨机总验收通过，本项保持未勾选。
  - 2026-10-20 导航与 Session 错误出口：导航 node/entries/matches 的引用在 DTO 边界统一校验中央 URI grammar，远端 POSIX、Windows、UNC 原生路径帧明确失败；文件正文和搜索文本保留原字节。Session/Thread locator 失败保留稳定身份及原因，不把物理位置加入异常；真实 child-threads HTTP 验证 409、原 request_id 与缺失目录不重建。`navigation-uri-contract-final-unit-002` 213 passed，`session-locator-errors-unit-001` 262 passed，最终 `navigation-uri-contract-http-003` 正式 matrix 1 passed/70.22秒，核悬空会话产物返回 404/unknown_resource。工具补验首轮94 passed/2 failed为旧路径文案及错误分类断言，迁齐统一解析边界并增加拒绝后原文件不变/新文件不存在断言后，`navigation-tool-rejection-004` 96 passed；各轮原始输出、命令、源码指纹与退出码均保留于 `out/tests/temp/openspec_takeover/artifacts/runs/`。相关 Ruff 与编译通过；此切片不代替其余输出和持久化不变量。
- [x] 7.3 增加 scope_id 推导测试：`gateway`/`inline` 的 scope_id 不再是字面量 `local` 且两者不再相同；`inline` 使用真实 `distribution_id` 并按 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」的编码规则单射（不同 (distribution, version) 必得不同 scope_id，由 version charset 不含下划线保证）、跨安装路径稳定、manifest 缺字段时 fail-closed；`gateway` 按 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」验证「按请求注入而非进程级单例」「缺失/非法时 fail-closed」「MUST NOT 用 host:port/监听端口」；`user` 恒为 `local`。
- [x] 7.4 增加星型解析与失败语义测试：authority 缺省与本机 gateway_id 同解、hub 直连直接 spoke、spoke 经唯一 hub 一次有界 transit、上界与 deadline 为 policy 常量且超限显式失败、不可达/未共享/未知 gateway 分别返回集中登记的**联邦解析期**拒绝码（`federation-channel-closed`/`target_not_resolvable`/`federation-unknown-peer`）、「未授权存在」与「不存在」不可区分、不递归转发。
- [x] 7.5 增加拒绝码登记测试：grammar、resolve 与联邦解析期三套闭集各自独立、未登记码构造即失败、三套不混用、其它模块无自造同义码。
- [x] 7.6 增加配置来源契约测试：校验 source URI/`layer`/`precedence` 继续为平级字段，逻辑来源映射符合 owner requirement；读侧 runtime override 报逻辑 layer 与原 precedence，物理 SQLite carrier 不外露，`active_snapshot` / `pending_snapshot` 不进入 `sources[]`；无可寻址来源文件时 URI 为空且不伪造地址；持久化与 API 不泄漏 real path。
  - 2026-10-19 `uri-config-source-contracts-001` 对 config API、workspace config source storage、ConfigService 三个完整文件验证通过（97 passed，94.91s，exit 0，源码前后指纹一致）。具名断言覆盖平级 URI/layer/precedence、runtime override 逻辑层及原 precedence、无稳定来源时 URI null、拒绝 active_snapshot/pending_snapshot 伪来源、持久化与 API 无 real path、冷读恢复层级一致；命令及原始输出见 `out/tests/temp/openspec_takeover/artifacts/runs/uri-config-source-contracts-001/`。
- [x] 7.7 运行仓库既有校验并记录证据：`openspec validate --strict --all` 必须 0 failed，且本 change 与其它四个 change 之间均无残留的第二套 URI 语法、自造 kind 或自造拒绝码。
  - 2026-10-20 最终语法核查：`uri-strict-all-20261020-001` 与 `fork-identity-openspec-strict-001` 均为41 passed/0 failed；中央grammar/resolver闭集、拒绝码与访问前校验 `uri-final-owner-contracts-001` 为220 passed/1.02秒。全仓复核生产URI构造/解析调用，领域适配均调用 `app/core/resource_uri.py`，旧虚拟资源grammar模块已物理删除。最终发现并删除Web fork目标路由专属正则：发布owner从冻结intent返回target_gateway_id，后端核目标身份与规范URI一致，Web仅按结构化身份唯一匹配已登记路由；错误Gateway、Workspace、Session、Thread、未限定/编码/query/fragment/追加路径等负例迁到权威后端边界，未降低拒绝能力。该切片56项Python与67项Web/协议测试通过，真实类型检查及Buf格式/生成/真实提交breaking均exit0，独立只读审查无阻塞。检索中的scheme分派、文档字符串及独立测试oracle不定义生产语法；未引入kind、scope或URI拒绝码。准确命令、源码指纹、原始输出与退出码均在 `out/tests/temp/openspec_takeover/artifacts/runs/`；本项不替代仍未完成的real path不变量验收。

## 8. 可导航资源根与轻量文件寻址（2026-10-05 新增，部分实施）

- [x] 8.1 在唯一寻址 owner 实现 directory/file/domain_object shape、公布的 child path 和操作能力，登记 files/root 工作区根、技能包根及单文件边界；不把 Session/assembly/config私有存储转成目录，不动态装配资源插件。
- [x] 8.2 完成技能包根、SKILL.md 与 references/scripts多层child的单一构造/解析；使用第1.4/7.1组分段编码，验证根 containment及越界symlink拒绝，目录可导航不等于可写。
- [x] 8.3 为 Workspace 文件树登记 `files/root` 根资源，并按显式固定的 `workspace_id` 解析；不以当前激活态补充身份。普通 ls/search/read 不为每个文件登记长期身份、revision 或快照，显式来源追踪仍由原 owner 登记。本项仅记录已实现的 `files/root` 登记与解析范围，不表示物理根已由原生 Gateway root binding 提供，也不证明 URI、解析结果或普通 DTO 不含真实路径；这些边界仍由未勾选的 8.6、7.2 等项验收。
- [x] 8.4 在本 owner 登记 Session 产物单文件模板：`session/{session_id}/attachments/{attachment_id}`、`session/{session_id}/tool-results/{result_id}`；保留业务 ID 的既有 profile，原owner决定读取/授权/保留，不允许目录猜测或通用修改。消费迁移归 add-unified-resource-file-tools。
- [x] 8.5 为本地/远端目录结果、Skill读取无activation、轻量搜索无注册、固定目标、文件能力拒绝和路径编码逐项留生产验收；文件执行错误归其独立owner合同，寻址错误仍只在本登记处。先固定本公共合同，再交独立文件工具消费change，不以规范修订勾实现。
- [x] 8.6 验收 files/root 的 root-binding 边界：原生 Gateway 唯一 registry 按 target 与 `workspace_id` 提供根；本地 managed token 或经目标 Gateway 现有 federation policy 授权的 `managed_workspace_admin` 注册输入可送达该 owner，权限合同以 `add-multi-workspace-backend-mounting` 为准；解析结果、跨 Gateway 投影、模型载荷与普通 Workspace DTO 不含根路径，缺失 binding 或身份不符明确失败且不扫描/猜根。
  - 2026-10-19 根绑定原生边界已按多工作区唯一 owner 的 4.5 完整验收：`root-binding-native-001` 2 passed、`root-binding-contracts-001` 212 passed、`root-binding-cold-restore-001` 37 passed，均 exit 0。真实 URI 读取跨 Gateway 重启仍命中原根，普通本地/联邦 Workspace DTO 无根路径；缺失/错误 binding 在写入或启动前响亮失败。准确原件见 `out/tests/temp/openspec_takeover/artifacts/runs/`；其它资源 DTO/业务持久化位置不变量仍由 2.1/2.2/7.2 独立验收。

## 9. Fork容器寻址与统一资源目标检查（2026-10-13，待实施）

- [x] 9.1 同批实现session集合根、Session和Thread正式模板、ResourceIdentity归属与允许操作；迁齐构造/解析/Gateway路由/DTO/API和测试，集合根不伪造Session ID，Thread不存在/跨归属明确失败，send/read/wait原main合同保持。
- [x] 9.2 在现有资源解析/owner路由接入唯一目标检查入口，复用现有字段得到reuse_reference/retain_reference/copy_snapshot/omit/unsupported；只增加确实缺少的owner retain/export/import能力，不扩充身份或逐资源fork布尔字段，不建立第二registry。
- [x] 9.3 验证同存储域共享blob零字节复制/内容身份复用/必要保活、不同Gateway挂载同owner的域核实、固定revision与可变源、目标已有内容去重、显式跨域导入及不支持/未授权失败；URI解析成功不代替目标能力完成。
- [x] 9.4 与itemized第12节、会话URI及工作区资源change同批核消费合同；删除平行fork资源矩阵和专属URI解析，保留现有导航、轻量访问、授权与scope不变量。
  - 2026-10-20 9.2–9.4验收：统一目标检查及7个caller、副作用owner与具名测试逐项见 `add-itemized-rollout-context/tasks.md` 12.6；尤其 `fork-resource-owners-003` 的16项经真实source/target owner验证，同域共享身份、跨Gateway同owner、异域核验、required失败和cold工具均有断言。`fork-blob-dedup-main-001` 2项经真实capture→accept/reserve/finalize→project/read证明跨域已有内容零传输/不重写与损坏拒绝，`fork-live-deadline-main-001` 12项证明目标终态和远端release分开核验。与已完成会话URI第10节、工作区资源owner同一合同，未新增URI kind/scope/专属fork解析器。全Web `web-default-010` 在完整报告JSON序列化时内存失败，保留失败日志且不计完整产品验收。

2026-10-19 具名复核：scope/identity来源与三owner闭集按实际生产代码核验，纯 grammar 已收敛到 `app/core/resource_uri.py`；没有第四份运行时 registry。1.2/1.3/3.1/3.2/7.1 复用 `out/tests/temp/openspec_takeover/artifacts/runs/uri-mount-unit-001/`（428 passed）和 `uri-identity-rejection-001/`（146 passed）具名矩阵；7.5 新增三owner全部跨域码拒绝、规范联邦解析子集对照及全生产错误构造点 AST 审计，`uri-error-domain-audit-002/` 40 passed，exit 0；`identity-uri-static-002/` Ruff通过。这里只验收列出的子项，未完成生产入口与完整fixture仍保持未勾。

2026-10-19 3.3/7.3身份fail-closed闭包：`app/core/identifier.py::validate_gateway_id`统一验证identity文件与请求上下文读取；拒绝host:port、监听端口、local、短ID、非hex、大写和channel值，保留缺头按实际gateway消费时拒绝的既有边界。非法identity文件原字节保留、不重建，两个请求不串身份；inline manifest编码/跨安装稳定/缺字段、user local与既有真实注入矩阵一并验收。`out/tests/temp/openspec_takeover/artifacts/runs/gateway-identity-unit-002/` 453 passed，exit 0；Ruff见`identity-uri-static-002/`。旧测试中伪Gateway值同步为合法固定身份；预算测试保留长错误输入与非零/省略断言，仅将总预算从1024调到1200以容纳真实长度身份，未缩短错误正文。旧identifier闭集4个零调用前缀已全仓检索后删除，原失败在冻结基线同样存在。

2026-10-19 real path 出口补证（2.1/2.2/2.5，尚不代替全项审计）：`5eb6e701` 将前端快照从任意request root与物理html_path改为注入工作区、catalog Session节点和稳定snapshot_id；响应仅给既有runtime能力下显式workspace API URL，metadata不保存root/path/html_file。真实HTTP保存/读取、移动/删除不复活、损坏metadata显式失败、protobuf reserved及本地/远端两层Gateway固定归属，由 `ui-snapshot-main-regression-001` 32 passed/8.18s验证；主树Ruff `ui-snapshot-main-static-002`、compile `ui-snapshot-main-compile-001`、真实tsc `ui-snapshot-main-typecheck-001` 与Web build `ui-snapshot-main-build-001` 均exit 0。Buf lint/build/format通过；`ui-snapshot-breaking-002` 仅报告授权删除 `LogSnapshotResultDTO` 与 `html_path` 两项，字段编号/名称已reserved，保留实际非零结果。原始候选与主树收据均位于 `out/tests/temp/openspec_takeover/artifacts/`；无新URI kind或别名。

9.1 验收（2026-10-19）：集合根、Session/Thread 构造/解析、ResourceIdentity 归属和公开 schema/proto/API/Web 消费已迁至唯一寻址 owner。具名源码与五个真实 transfer 场景、当前主树 default/normalization/API拒绝/grammar-resolver/Thread归属证据见 `migrate-session-context-resource-uri/tasks.md` §10；包括复制中激活切换不漂移、source child 不回退 main、集合根新建 main和已有 Session child，保留 §9 send/read/wait 原合同。仅关闭容器寻址9.1；资源目标能力9.2–9.4继续分别验收。

2026-10-20 2.5 六场景验收：可推导产物位置由 BrowserStateStore 的身份重推导与拒绝 path 落盘断言覆盖（`uri-position-browser-002`，45 passed）；不可推导产物复用截图 API URL 与 UI snapshot 真实 HTTP 保存/读取端点（同次 browser 验证及 `ui-snapshot-main-regression-001`）。终端原生 cwd 采用工作区根相对路径，新增 `terminalCwdPersistence.test.js` 经真实 manager.create→正式 terminals.json→shutdown/新 manager.init→PTY 参数核对，外部目录保持原目标且两端绝对根均未落盘（`uri-position-native-cwd-002`，1 passed；原终端组 `uri-position-terminal-002`，34 passed）。物理根控制面边界复用本项 8.6 的三组原生 Gateway 验收；机械判据由各 owner 的路径字段拒绝与上述序列化/落盘断言执行，不新增无消费者的分类器。配置事件经真实 ConfigService diff/reload、HTTP 与 SQLite 重开，验证 JSON Pointer 的 /~ 转义及 applied/restart_required 两分支（`uri-position-config-events-001`，8 passed）。Ruff、JS 静态解析与严格 OpenSpec 41/0 通过；原件均在 `out/tests/temp/openspec_takeover/artifacts/runs/`，非作者审查确认场景边界。本项不代替普通文件树和快捷目录的尚未完成路径治理。

2026-10-20 普通 UI 设置路径清理（2.1/2.2/7.2 子切片，三项仍未整项关闭）：全仓确认 `recent_local_workspace_paths` 无展示消费者后，删除注册成功→前端 UI 设置→Gateway DTO/proto→profile 的闲置绝对路径历史链，两个已发布 field 6 的编号/名称 reserved，全部直接/间接装具及官方生成物同批迁齐。原生目录选择/注册仍把 root_path 仅作为管理请求输入；未注册目录浏览、编辑与 URI 快捷方式仍保留。旧 profile 含该字段时读写明确失败，原字节不改写，不静默迁移或清理。`ui-directory-history-python-unit-001` 42 passed、`ui-directory-history-web-unit-001` 123 passed、`ui-directory-history-contract-001` 14 passed，真实 tsc、Web build、官方 Buf lint/build/generate/format 及 OpenAPI 导出均 exit 0。`ui-directory-history-browser-001` 经真实 Gateway/Workspace/Chromium 验证未注册目录编辑、URI 快捷目录刷新及两个 owner 重启后重开，注册表完整行不变，1 passed/52.63秒、exit 0。Ruff 其余四文件 `ui-directory-history-static-clean-001` exit 0；user_profile 原有5条 TRY004 与 `ui-directory-history-static-baseline-001` 完全同源表达式，未新增。`ui-directory-history-protocol-breaking-001` 对真实 b0d903a0 基线 exit 100，仅对应上述两个授权字段删除，保留失败且不声称 breaking 全绿。原始命令、保护、源码指纹、输出、实际退出码及进程回收均在 `out/tests/temp/openspec_takeover/artifacts/runs/`；只读独审未发现可行动问题。本批未据局部通过勾选全部资源持久化与普通响应审计。

2026-10-20 普通文件私有边界（2.1/2.2/5.2/7.2 子切片，未代替整项审计）：Web 文件树、原生目录、URI 导航和模型文本修改复用唯一路径 owner 的私有判据；直接路径、嵌套 `.boxteam`、原生根及 symlink 别名均不能绕过领域 owner。复制、上传与 ZIP 逐层在读取或发布前检查，已受理来源变成私有别名时 rejected/none，worker 继续工作；普通 `.config` 和 skills owner 保持既有能力。分类保留循环和断链 symlink 节点，实际访问仍明确失败。最终 `private-files-final-unit-001` 245 passed/30.92秒，Ruff `private-files-final-static-001` exit0，完整页面 `private-files-final-browser-001` 2 passed、exit0，经过真实 Gateway/Workspace/Chromium 验证私有请求403、原件不变、无正文/根路径外泄及未注册目录编辑重开。无前端源码变化，复用前批真实 tsc/build。失败原件 unit002 为 fixture 继承上次目录绑定、unit003 为清理控制树却残留旧工作区 URI，已同时隔离两者并保留精确注册表断言；未通过不计通过。只读独审复核循环 symlink 修正及来源/目的/ZIP/文本/skills 链，无新可行动问题。准确命令、源码指纹、原始输出、实际退出码和进程回收位于 `out/tests/temp/openspec_takeover/artifacts/runs/`。

2026-10-20 UI 文件偏好持久化（2.1/2.2/7.2 子切片，整项仍未关闭）：preview list、active file 与 expanded map 共用中央 grammar，仅接受 files URI，保留规范编码、固定目标与合法悬空引用。profile 冷读与更新先校验已有值，旧路径明确失败、不覆写；请求422及磁盘旧值409不回显原生位置，并保持 request_id。最终 `ui-file-preferences-request-unit-002` 110 passed/4.39秒、前端五个关联 fixture `ui-file-preferences-web-unit-001` 84 passed；真实 tsc、正式 Web build 均 exit0。`ui-file-preferences-final-browser-003` 经真实 Gateway/Workspace/Chromium 验证编辑、刷新自动恢复预览、两个 owner 重启后冷读及原注册表不变，2 passed/85.15秒；新增422响应检查的 `ui-file-preferences-request-browser-004` 1 passed/56.94秒，随后仅补齐标准 msg 字段，由最终110项中的9个真实HTTP拒绝矩阵覆盖。最终 Ruff `ui-file-preferences-final-static-003` 与 JS 静态解析002 exit0；user_profile 原有5条TRY004的code/message/raise AST与HEAD相同，比较原件见 `ui-file-preferences-static-baseline-001`。首次 browser001 失败由并行构建清理静态入口造成；browser002 的聊天引用失败来自 reload 后新增诊断监听无法读取CDP response body，该监听已删除，显式GET回执检查保留，003补验通过。失败原件均保留，不计入通过。只读独审无新可行动问题；准确命令、源码指纹、原始输出与实际退出码均在上述 runs 目录。本批没有产品前端源码或协议字段变更。

2026-10-20 会话文件变更 owner（2.1/2.2/5.2/7.2 子切片，整项仍未关闭）：record/index/reviewed map 冷读及写入复用同一中央 URI 合同，悬空 Workspace/Gateway 文件引用和规范编码保留，非法引用、非布尔审查状态、规范化冲突及旧快照字段明确失败、不改写原件。快照改由 session/edit/phase 在存储访问时推导，索引、metadata、changeset DTO/摘要和工具事件删除 before_file/after_file/diff_file，proto 10–12及名称 reserved，直接/间接消费者与官方生成物迁齐；diff_text、preview、create/edit/delete、多次聚合与内部执行保持。保留符号链接 containment，外部快照别名明确拒绝。最终 `session-changes-position-final-unit-003` 66 passed/28.64秒；`session-changes-position-final-federation-002` 1 passed/110.00秒，经真实 Gateway/Workspace、脚本 Provider 工具入口验证条件补丁/写入/修改、原事件模型解析、HTTP 变更与审查，以及强制后端重启后的原索引和完整 files 一致。前端 `final-web-unit-002` 12 passed；真实 tsc002、正式 Web build001、11文件Ruff003、严格OpenSpec001均exit0，均带同一 session-changes-position 前缀。官方协议生成同时 lint/build，format001通过；breaking001 对真实 ef62355f 基线 exit100，仅三项授权字段删除，未称全绿。早期静态001的两个导入格式已修正；Web unit001中未改动的旧装具把原生/相对位置误称合法响应，按已有URI合同修正并新增两种路径拒绝断言，失败与归因指纹原件保留。非作者只读独审无可行动缺陷，所提delete/悬空/实际事件解析覆盖缺口已由最终单元与真实入口补齐。准确命令、源码指纹、原始输出、实际退出码和进程回收均在 `out/tests/temp/openspec_takeover/artifacts/runs/`；构建后仅测试fixture改动，产品前端源码未变。

- **2026-10-20，2.2 配置读取错误补齐**：必需 JSONC 读取复用同一稳定快照 parser，删除 runtime 的重复解析实现；root 非对象、必需 schema 缺失、目录读取错误、resolve/stat/read 系统错误、持续变化和解析后漂移的错误正文不再附带原生来源地址。系统错误保原类别、errno、operation 及原始异常链，不在响应层替换/截断路径。真实 Gateway reload→HTTP status 验证坏来源不改变 active 配置/SQLite snapshot、无 pending/event，修复后同入口恢复。基线 `config-source-errors-baseline-001` 四负例失败；最终 `config-source-errors-final-003` 153 passed/exit0（含 Workspace config 回归），`config-source-errors-static-003` 四文件 Ruff exit0，`config-source-errors-openspec-001` strict exit0，均位于 `out/tests/temp/openspec_takeover/artifacts/runs/`。限定独审发现的 resolve 捕获边界已补并复核；不代全仓其它错误 producer/业务持久化的2.2、7.2验收，两项继续未勾。

- **2026-10-21，2.2 JSONC 解析错误出口**：同一稳定快照 parser 只发布解析异常类型与结构化位置，不回显完整文档/token；UTF-8 错误报告字节范围，JSON 重建后的错误位置明确标为 `normalized_json_line`，原异常完整保留在内部 cause。实际 commentjson/Lark/JSON 负例和 Gateway reload→ASGI HTTP status 验证配置或 schema 损坏时不改变 active/config/pending/event，source 的 revision/digest/generation/payload 等全部语义字段不变，修复后同入口恢复；既有秘密迁移仍会更新 source 审计时间，不声称数据库行零写入。`config-jsonc-regression-002` 241 passed/177.25s、exit0；`config-jsonc-static-002` Ruff、`config-jsonc-compile-001` 均exit0。red001保留11失败/13通过，其中一个新增token装具原为合法JSON，已改为真实冲突token；focused001的6失败仅为既有审计更新时间，修订前后证据保留。regression001达到180秒外部上限、exit124，不计通过；最终带逐项输出补验完整配置关联组。非作者只读审查无阻塞，准确命令、源码指纹、raw及退出码在同目录 `runs/config-jsonc-*`；该ASGI组件证据不代替独立Gateway进程或2.2/7.2整项审计。

- **2026-10-21，2.2 配置 Schema 错误合同补齐**：2026-10-20 切片直接使用 `jsonschema.message` 和动态 `absolute_path` 的诊断仍可能回显值、对象键或 Schema 原文，现由 `configs.runtime.validate_config_payload` 统一负责 Gateway 以及 Workspace source/startup、active/pending 恢复、shadow 校验。公开错误只发布校验阶段、内置规则名及零基对象成员序号/数组下标组成的结构位置；策略 manifest 不一致和未解析的 fragment/外部文件引用同样显式失败，完整原始异常保留为 cause，不在 HTTP 层替换文本或接受坏候选。Workspace 缺失 Schema 的解析错误也不再输出候选物理路径。`config-schema-red-001` 的10个值/键/动态键/Schema/策略负例及 `config-schema-ref-red-001` 的4个引用负例均在旧实现变红。关联回归 `config-schema-regression-001` 为282 passed、2 failed/221.19s；两项失败仅为 Gateway 测试仍逐字断言旧错误格式，现保留精确 cause 路径/规则断言并迁移公开诊断预期。最终 `config-schema-final-001` 49 passed/64.76s、exit0，覆盖 Gateway reload→ASGI status、Workspace 条件自定义 Schema→真实 reload→ASGI status、普通配置 metadata、失败不覆盖 active/不生成 pending/event、源修复后清错，以及 startup/shadow/缺失 Schema；前后指纹一致，复用其余已通过关联行为，不把重叠计数相加。最终 Ruff/compileall/lock-check 均exit0；包级 Ruff 的31条既有 TRY004 在4个与HEAD逐字相同的未修改文件，基线另存。只读独审 `config-schema-owner-review-final-001.json` 无阻塞。命令、源码指纹、raw及退出码位于 `out/tests/temp/openspec_takeover/artifacts/runs/config-schema-*`，该组件级证据不替代独立服务进程或其它错误 producer、持久化/模型/联邦投影的2.2、7.2整项验收，继续未勾选。

- **2026-10-21，2.2 生成结果与运行账本边界**：Workspace/Gateway 的生成输出删除 `storage_relative_path`，保留稳定工作区/Session ID、标题及逻辑导航位置；reporting 不再请求物理 locator，Web 运行历史复用导航展示，Proto field 6 的编号/名称 reserved，官方生成物与 OpenAPI 已更新。嵌套 output 拒绝旧字段，结果/运行模型公开校验错误保留字段位置与错误类型但不回显输入值，异常对象仍可诊断；不迁移或静默清理旧账本。`generation-output-position-unit-final-004` 26 passed/2.48s，含 Workspace 幂等冷读与 Gateway get/list 的旧记录拒绝及原件不变；`generation-output-position-product-final-004` 3 passed/78.19s，经过真实 Gateway→Workspace 生成新 Session、同键复用、实际完成与原账本查询，注入旧字段后 HTTP400/request_id/无值外泄/不覆写，并恢复原件；另两项为真实 Gateway 加下游服务桩，证明 CRUD、运行账本、跨工作区目标和重复调用，不将它们称为真实 Agent fork 验收。最终7文件 Ruff、compileall、协议 reserved/schema检查、Buf lint/build/generate/format、真实 tsc 与正式 Web build 均 exit0。breaking 对真实 `5456642e` 基线底层exit100（脚本exit1），仅一个授权字段删除，不计全绿。最终产品运行期间另一组修改了 `workspace_activity.py`，本切片源码/测试未漂移；完整前后指纹与限定复用记录见 `out/tests/temp/openspec_takeover/artifacts/generation-output-position-validation-final-001.json` 及同目录 `runs/generation-output-position-*`。首次集成筛选未缩小范围已主动停止；旧外部目标装具误用managed路径ID的失败、嵌套校验值回显的3个负例失败及修复原件保留。只读独审最终无finding。本项没有浏览器交互验收；catalog节点/搜索缓存仍待迁移，2.2/7.2继续未勾选。

- **2026-10-21，2.2/7.2 调试目标解析错误**：配置 factory 的缺失脚本/工作目录错误使用统一文件 owner 返回的规范 URI，绝对工作目录越界保留原400及 ForbiddenError cause、不回显原生输入；create/update/import/start 复用同一解析链。`debug-path-errors-unit-001` 69 passed/11.88s；真实工作区后端 HTTP `debug-path-errors-http-green-003` 2 passed/35.26s，核配置与启动入口的相对/绝对/URI缺失目标、外部绝对目录、缺失断点文件、原404/400与头体 request_id、失败后完整状态不变，以及有效绝对路径仍能创建无进程配置。最终 Ruff/compileall exit0，运行前后所记录源码指纹一致。red单元5 failed/2 passed及HTTP2 failed保留；HTTP green002因重复run ID产生2个setup错误、未进业务断言，不计产品失败。准确命令/raw/退出码在 `out/tests/temp/openspec_takeover/artifacts/runs/debug-path-errors-*`，限定汇总为 `artifacts/debug-path-errors-validation-final-001.json`。本切片不验证Gateway、Node启动或断点正文读取错误，2.2/7.2继续未勾。

- **2026-10-21，2.2/7.2 断点源码读取错误**：源码锚点/摘要/对账共用一次原始bytes读取和唯一文件owner生成的URI，删除旧文本/摘要双读链；缺失仍为原None/source_deleted，非UTF8仅在锚定时拒绝，既有摘要/变化检测继续消费原始bytes。显式stat只把FileNotFound/NotADirectory作为缺失，其它stat/read故障保原类型、errno及cause，公开文本使用URI、不带filename。`debug-source-errors-unit-final-003` 125 passed/14.54s，含两入口单次读取/CRLF/二进制摘要及stat/read×EACCES/EIO/ELOOP负例；真实后端HTTP `http-final-002` 4 passed/41.88s，配置/动作的三种路径输入保409/request_id/完整状态与坏源bytes不变，修复同一源码后重试原请求得到正确锚点和SHA。现有Agent→工具→真实Node Inspector流程`inspector-001` 1 passed/125.83s，发生于最后显式stat补强前，最终单元和HTTP覆盖补强、不冒称同源码完整重跑。最终8文件Ruff及compileall exit0，各成功run所记录源码前后指纹一致。red单元6 failed、HTTP2 failed真实复现；unit001因错误测试文件名退出4/零测试，修正命令后完成上述回归。只读独审纠正原Path.is_file EACCES判断：本机3.12保留EACCES，但会忽略ELOOP，显式stat已消除此误判。命令/raw/退出码和限定复用在 `out/tests/temp/openspec_takeover/artifacts/runs/debug-source-errors-*` 及 `artifacts/debug-source-errors-validation-final-001.json`。不替代Gateway/Web或其它错误出口审计，2.2/7.2继续未勾。

- **2026-10-21，2.2/7.2 文本文件最终访问错误**：预览保存及模型文本 owner 共用的原字节读取、原件核验、发布和删除，在最终 OS 访问处生成 URI 错误，保异常类别、errno 与完整 cause，不在响应层替换路径；域错误、条件版本、部分发布及发布后未知效果保留。旧真实后端读/临时文件写权限负例 `text-file-errors-http-red-001` 两项均暴露原生绝对路径。最终 `unit-001` 95 passed/2.19s，覆盖 stat/open/read/后stat×EACCES/EIO/ELOOP/ENOENT、mkdir/temp/fsync/replace/delete与原文本修改回归；`http-green-002` 2 passed/47.38s，真实HTTP三种普通文件输入保500/request_id/URI+errno，内部文本传输仍只接受URI，核零preflight journal或not_published及真实前后件、无tmp、原件不变与权限修复后原请求成功。Ruff `static-final-002`、compile001 exit0。green001仅测试误传相对路径给URI-only传输DTO，修正编排后补验，失败保留。HTTP运行期间仅summarization middleware有他人并发修改，本批及实际文件依赖未漂移，该场景不执行模型/摘要；限定汇总为 `artifacts/text-file-errors-validation-final-001.json`，准确命令/raw/指纹/退出码在同前缀runs。非作者只读审查接受；清理tmp再次失败仍沿用既有finally/异常链行为，本批未覆盖。未验证Gateway/浏览器/真实Agent或Windows ACL，不代替2.2/7.2整项审计，继续未勾。

- **2026-10-21，2.2 待处理队列诊断出口**：PendingRequestStore 的解析、冷读/恢复、canonical 输入投影、写入、删除及旧格式探测统一以 Session ID、artifact 与 stage 定位失败，不向普通响应复制队列物理位置、临时文件名或校验输入/动态字段名；已知格式检查发布固定 rule，Pydantic 只发布规则类型与数量，JSON/UTF-8 只报告结构位置。系统错误保具体类别、errno 和完整 cause，load 保持恢复 RuntimeError，缺失 Session 的 KeyError 仍在原边界；不新增 URI kind、兼容读取或正文事实源。HTTP 红测 `pending-errors-http-red-001` 在旧实现复现完整路径外泄（1 failed/10.81s）；最终 `pending-errors-unit-final-002` 66 passed/51.02s，含原 FIFO、canonical、取消收敛/失败重试、有界 header 与同 handle 原子版本回归，以及新 OS/格式/编码负例。`pending-errors-http-green-001` 3 passed/11.21s，经真实后端 API 创建 Session，再定点注入损坏 header/detail 或真实 POSIX chmod000，验证两种 pending API 的500/request_id、原件不变、无路径/输入回显及修复重试；这仅证明错误读取和重试，不代替经执行入口生成关键状态的完整恢复验收，Windows ACL 未验。Ruff `pending-errors-static-final-002`、编译 `pending-errors-compile-001` 与严格 OpenSpec 均exit0，原始命令/源码指纹/raw/退出码在 `out/tests/temp/openspec_takeover/artifacts/runs/`。四个最终 run 均无进程残留；HTTP/静态/编译指纹不变，单测期间他人对 records.py 的局部变量正名经完整 AST 归一核实等价，证据见 `pending-errors-validation-final-001.json`。非作者只读独审无阻塞；安全的队列序号/序列化检查及 catalog 自身枚举仍归各自既有合同，本批不声称全 owner 审计完成，2.2/7.2继续未勾，硬错误恢复 TODO 不变。

- **2026-10-21，2.2/7.2 文本变更 journal 错误出口**：create/read/update 共用 journal 存储边界与读取形状检查，公开错误使用 artifact/stage，保 OS 具体类别、errno 与完整 cause，JSON 保既有 RuntimeError 并报告结构坐标，UTF-8 保原异常属性；不回显物理位置或损坏内容。缺失、目录与坏形状明确失败，不改写原件。最终 `text-journal-errors-joint-final-002` 38 passed/11.68s，含真实后端HTTP权限失败与修复重试、远端传输及文件事件回归；随后仅补强精确异常坐标/cause断言，`unit-final-003` 37 passed/0.90s 与 `static-final-003` Ruff exit0，产品编译 `compile-final-002` exit0。发布前 journal 创建失败保持 not_executed/原件不变；文件已发布后 journal 更新失败仍为 completed/published，不伪报回滚。red单元29失败；HTTP red001只证明旧错误缺少owner诊断，不作为路径外泄证据；HTTP green001因新测试误认 journal_id:null 为字段缺失而失败，仅纠正测试预期，原件保留。只读独审无阻断，命令/raw/前后指纹/退出码及限定复用见 `out/tests/temp/openspec_takeover/artifacts/text-journal-errors-validation-final-001.json` 与同目录 `runs/text-journal-errors-*`。不改变既有直接写入策略，不声称原子写/失败回滚，不代替Gateway、浏览器、完整Agent执行或Windows ACL验收，2.2/7.2继续未勾。

- **2026-10-21，2.2/7.2 附件入口与生成引用**：MessageService 创建时先归一工作区相对/绝对/files URI 附件，DTO、Job、SSE、canonical 及模型标记共用同一引用、默认文件名和 MIME；附件自带的路径解析器及旧 relative_path/resolve_content_type 消费物理移除，复用 WorkspaceFilePaths。Session 附件保持既有 blob owner 和原引用，正文不复制，用户消息原文不改写。最终文件 I/O 使用 URI/errno 并保留 cause；ffmpeg 在临时 cwd 中只接收相对文件名。`attachment-uri-targeted-002` 中 100 passed/22.74s，新增 SSE 测试误把 wire dict 当 AttachmentRef 的三项失败保留；纠正为真实 model_dump 后 `attachment-uri-dispatch-003` 8 passed/7.57s，覆盖三输入 create→真实 acceptance→Job 一致性验证→SSE wire→新 Saver 冷读，复用前次不变源码的其余已过用例，共103项。外部路径拒绝且不重定向、显式工作区不借进程 cwd、图片/视频预览、原附件/fork owner 与失败诊断均覆盖；首轮44项中仅新增测试的超出/越出文案错误，原件保留。两次最终行为运行前后所记录源码指纹一致；Ruff `static-final-003`、compile001 exit0，非作者只读审查无阻断。原始命令、指纹、退出码、raw 和限定复用在 `out/tests/temp/openspec_takeover/artifacts/runs/attachment-uri-*` 及 `attachment-uri-validation-001.json`。精确提交仅选附件方法，保留他人未提交的 LRU 改动。未验证完整 Gateway/Web/模型执行或真实 Windows，本子切片不代替2.2/7.2全出口审计，两项继续未勾。


- **2026-10-22，2.2/7.2 HTTP 请求校验错误出口**：Gateway 与 Workspace 删除分散的默认/设置特判响应，共用一个 HTTP codec；保留原 422、规则类型、声明字段/数组位置和头体同一 request_id，动态字典键只发布零基成员坐标，输入值、ctx 和自定义消息留在服务端完整异常日志。字段按所在 schema 层级判定，别名、嵌入模型、引用、可空及 allOf 正常定位；多分支 union 无法可靠对应 Pydantic 标签时使用结构坐标，不借用兄弟分支字段。`request-validation-final-007` 133 passed/32.77s，含真实 Gateway→Workspace 的 POSIX/盘符/UNC 输入九次非法 body/query/注册请求、原文件与权威注册表全行不变及随后合法绝对路径保存；`consumers-007` 的远端 Skill 导航通过，浏览器部分因旧 locator 无法定位虚拟树未挂载行失败，原件保留。仅修测试真实滚动定位后 `browser-010` 1 passed/83.54s，覆盖未注册目录编辑、刷新、UI 设置拒绝及双 owner 重启冷开，没有 API 写状态代替交互。`static-010` Ruff、`compile-007`、浏览器脚本语法检查均 exit0；007→010 产品及 Python 测试逐字一致，复用不变的133项和导航通过证据，正式 Web 构建按相同前端源码树复用。非作者产品及浏览器脚本复审无剩余 finding；最终提交 `f2eb1bcc` 的 Git tree 与验证候选完全相同，共享暂存区未变。红测14失败、端口冲突/缺构建的 setup 错误，以及最初误将 checked_at 变化当成注册表变化的测试失败均保留，未计作产品通过。准确命令、指纹、raw、退出码及限定复用收据在 `out/tests/temp/openspec_takeover/artifacts/runs/request-validation-*` 与 `request-validation-verification-final-010.json`；本切片不代替 Windows、完整 Agent 或其它错误 producer 的不变量审计，2.2/7.2继续未勾。

### 2026-10-22 fork capture 私有坐标传输收口（部分收口）

- `ForkSourceCaptureDTO` 与 incomplete 409 detail 删除私有 `snapshot_relative_locator`；跨 owner 仅传冻结 `snapshot_id` 与原身份/hash，接收方由唯一存储规则生成并核对本地相对坐标。catalog 的内部 locator、原 release receipt 和磁盘布局保持原合同，无 schema 升级或双读。该字段原为私有相对坐标，此项不称真实绝对路径泄漏。proto tag/name 8 已 reserved，官方 Python、两套 Buf/TS 与 OpenAPI 快照同步。
- 固定候选005 `87c00ac0a1e26a14709edd3a2348677bd74ea87d`：`fork-transfer-locator-unit-003` 52 passed/2.90s；真实三端 Gateway/SSH 的 `recovery-004` 1 passed/118.36s，覆盖 partial capture 中止及原 owner 409 经联邦恢复；`matrix-006` 1 passed/124.09s，产品入口生成消息/附件/Goal，跨端复制后 source 离线，目标冷读、原字节与新输入通过。`final-static-005` Ruff/compileall 和101项 API 契约均通过；官方生成、Buf format、真实 tsc、Web build 通过。Buf breaking 相对214c0399 exit100，仅报告本次删除的field8，不另建兼容字段。004独审更正先前对extra=forbid与生成物的误判，未发现新增阻断。
- `matrix-004` 在首次fork前被旧422文案断言挡住：按已发布共享校验合同改为精确loc/type/msg，同时保留Schema领域错误和原零副作用断言；`matrix-005` 为并发隔离端口冲突的setup错误，未执行业务，串行006通过。早期候选准备、直接pytest缺matrix watchdog及import排版失败原件均保留，不计通过。各次准确命令、raw、源码前后指纹、退出码及无残留核验在 `out/tests/temp/openspec_takeover/artifacts/runs/fork-transfer-locator-*`，复用范围另见 `fork-transfer-locator-validation-reuse-006.json`。本收据不关闭URI2.2/7.2或itemized整体验收。

### 2026-10-22 启动日志经诊断接口输出真实路径的修复（部分收口）

- 独审 `uri-owner-final-independent-20261022-001.json` 确认main启动日志→Gateway diagnostics tail→federation diagnostics泄漏链；真实启动再发现env加载及共享watcher正常日志输出物理路径。一并从producer移除真实位置参数，保留阶段、owner ID、监听选项、耗时与原异常传播，不添加响应端通用脱敏或第二日志链路。
- 固定候选003 tree `72a33d56e5f1734783882cf4d12d5f7307ff3700`：`startup-root-product-003` Ruff通过、真实Gateway启动managed Backend后核本地/联邦两个诊断响应，1 passed/25.97秒；后端UUID与Gateway导航ID分别核实，tail非空且含实际启动阶段，roots、独立BOXTEAM_HOME及项目根均不出现，request ID一致。`startup-root-owners-003` 18 passed/2.19秒，覆盖env、watcher、diagnostics既有行为。
- 001测试保留失败原件：首先误用Gateway ID匹配Backend日志，原输出同时揭示env/watch路径，002修正producer与精确身份后通过；独审002发现独立BOXTEAM_HOME测试遗漏，003补齐。owners002误写不存在文件路径退出4，003按真实文件补验。原始命令、指纹、raw和退出码均在 `out/tests/temp/openspec_takeover/artifacts/runs/`；异常栈/自由文本等全producer闭包仍需继续审计，不据本条勾选2.2/7.2整体门。

### 2026-10-22 产物响应地址合同（部分收口）

- `ArtifactDTO.path` 复用中央资源 URI grammar，拒绝原生 POSIX/盘符/UNC、相对路径、file/http 与带 query 的非法地址，错误文本不回显输入。合法悬空本地及联邦 URI 保留规范编码，成功 APIResponse 经真实序列化对账；未实现的产物服务仍明确返回501，不新增存储或 URI kind。
- 固定候选002 tree `c39b1cfb300e97d5ffe554da2fecbe536910972b`：`runs/artifact-uri-contract-final-002` Ruff通过、14 passed/3.75秒、exit0，源码前后指纹一致、无残留进程。独审001在DTO grammar范围内无确定发现；002增补两条正向round-trip，后续独审收据单列。此处仅证明地址输入校验、响应序列化及既有501行为，不证明产物存在、读取或持久化，不据此勾选2.2/7.2整体门。


### 2026-10-22 URI grammar 拒绝出口（部分收口）

- 公共 grammar 不再将非法 URI、segment、scope/kind 输入或产物 ID 拼入公开拒绝文本；保留原异常类、reason_code、校验顺序与 cause。真实资源导航 HTTP 和同步/异步文件工具继续消费同一错误源，无响应端正则脱敏或第二 codec。闭集校验后的规则名称仍用于解释语法，合法 URI 字节不变。
- 固定最终 tree `56404a1ba11e1d962612da95cb7ebaa2c1c5e512`：`uri-grammar-errors-http-005` 正式 matrix 1 passed/58.40秒、exit0，七类非法输入均400且 owner/code/request_id 精确，原生根不回显、文件与 SQLite 注册完整行不变，随后中文文件 URI 读取成功。列表投影每次生成的 checked_at 单独核时间可解析/不倒退，其余字段完整相等。`unit-002` 为406 passed/14.66秒；最终逐字段错误码强化由 `static-unit-003` 217 passed/1.20秒补验，生产与其余工具测试字节未变，限定复用见 `uri-grammar-errors-final-proof-005.json`。Ruff/compileall通过、三次源码前后一致且无残留；独审001/004无阻断。
- 原生产完整archive负例 `red-001` 为55 failed/1 passed，实际区分回显；HTTP001的无效query与003的动态观察时间预期错误、004在pytest前触发嵌套runner内存上限冲突，原件保留，均不计产品通过。准确命令、raw、指纹与退出码位于本任务artifacts/runs。仅关闭该出口，不代替2.2/7.2全量不变量审计。


### 2026-10-22 终端原生路径错误出口（部分收口）

- 唯一 cwd 解析仍按目标端原生路径解释相对/绝对目录；stat 失败、非目录与 PTY 启动失败在 producer 发布固定阶段及终端 ID，原始异常/cause、cwd、command 留在服务诊断。Worker/退出清理的 release_reason 只保存阶段，不再把原生错误正文写入状态或广播；unknown、创建失败回收和原状态码保留，无响应端正则脱敏。
- 最终候选 tree `9527d2a8dc826bd6e79b93226a496f22bc1ba351` 的 `terminal-errors-http-tool-002` 为7 passed/71.21秒、exit0：真实 Node HTTP 五类拒绝逐项覆盖sync/async、500与完整错误正文，核零终端发布/原文件不变及服务日志保原原因；真实工具读取工作区外兄弟目录，核原目标、退出码、模型结果无绝对路径；既有PTY输入输出/租约/崩溃恢复通过。`static-node-001` Ruff/Node语法与37项Node测试通过；003与002的产品/Node测试逐字一致，复用原结果。旧生产完整archive的`red-001`四项全部失败，明确区分四处回显。
- 首轮`http-tool-001`首个HTTP用例通过，后续因模块工作区复用上例直接准入留下的无冻结队列执行，5个setup和1个子进程启动失败；改为每例完整fixture副本及相同配置后002全通过，不放宽生产恢复校验、不替测试补队列或终态。独审002/003无finding，前者独立Node37项通过；Bun的node:test mock不支持导致的审查尝试失败另存，不能算产品失败。各次准确命令/raw/指纹/退出码及原件在本任务artifacts/runs，限定复用和产品证据SHA见`terminal-errors-final-proof-003.json`。无运行组残留，未验证Gateway/Web或Windows，本项不关闭2.2/7.2整体。

### 2026-10-22 Browser检查点原生错误出口（部分收口）

- 检查点读/写/目录核验/删除由实际文件操作处生成资源ID、阶段和错误码；原路径、JSON解析/系统原因只保留在服务端日志及cause。发布与临时清理同时失败时保留两份原异常，主错误码不被覆盖；失败不更新配额或正式正文，下一次显式操作仍可继续。目录核验中的ENOENT明确失败，只有目标文件检查/读取的ENOENT表示不存在。客户端生成的超时公开消息保留method/timeout和重试恢复信息，协议route继续保留在BrowserRequestError.path，不混入该消息。
- 固定生产候选005 tree `501f29ebb91faa5daa0eb1f9575d49032f0f3ddb` 的 `browser-checkpoint-errors-static-unit-002` 为10项Node及75项Python通过，Ruff/Node语法exit0；006仅增强集成回执封存，`http-003` 真实Node HTTP与sync/async客户端→资源API→Node记录全部通过（1 passed/12.84秒，准确耗时以原始输出为准），同时Ruff/compileall exit0。覆盖原500与完整错误正文、同一request_id、verified失败回执、零临时残留、显式freeze/wake重试，以及独立API传输超时的408、完整metadata、重启后unknown和零自动重放。原生路径/底层错误在Node stderr可核，公开正文和持久错误不含该路径。
- 完整archive恢复旧生产的红测为3项Node失败及1项API失败，分别区分原路径回显及超时metadata被误抹。非作者独审指出的目录核验ENOENT问题已修并复核，10项Node独立复跑通过；目录项核验/清理共享阶段名只影响诊断粒度，cause与code仍完整。005→006不变源码的限定复用、准确命令/raw/指纹/退出码和封存产品原件SHA见本任务 `browser-checkpoint-errors-final-proof-006.json` 与 `runs/browser-checkpoint-errors-*`。本切片未移除通用public_error正则，其它producer仍需迁齐；不以此勾选2.2/7.2，不声称完整Gateway/Web或Windows验收。


### 2026-10-22 工具输出事件引用校验（部分收口）

- `ToolOutputReferencePayload.path/read_path` 在事件发布前共用中央 session artifact URI parser，并限定为 `tool-results`。拒绝原生/相对路径、旧 scheme、普通文件及附件 URI；不检查目标存在性，保留合法远端 authority、字段形状与 writer 生成的规范 URI。没有另建 parser 或响应端脱敏。
- 固定代码候选002 tree `f0928d79d7406bd00378ffad13d9d1942cfe2380` 的 `tool-output-uri-static-unit-002` 为71 passed/3.00秒、exit0，Ruff及compileall通过。覆盖真实 `ToolOutputStore.bound` 落盘回执到DTO、合法联邦悬空URI的durable listener/内存历史/订阅队列完整round-trip，以及两字段各六类非法引用在任何交付前拒绝。完整archive仅恢复旧事件schema的 `red-002` 为12 failed/1 passed，失败均为未拒绝非法输入；首轮装具误传part_id的失败保留，不算红证。
- 独立只读审查 `tool-output-uri-review-001.json` 未发现此三文件切片的剩余问题；候选目录是Git archive，版本以记录的tree及文件blob核验，不用向上检索所得的主仓HEAD代表候选。准确命令、前后源码指纹、raw、退出码位于本任务artifacts/runs；最终仅追加本收据，复用不变代码的71项结果。本切片不代替完整执行、Gateway/Web、Windows或2.2/7.2全局审计，两项继续未勾。


### 2026-10-22 Browser下载错误出口（部分收口）

- 实际下载存储owner在目录创建及正文保存处生成browser/download身份、阶段和错误码，原系统路径与异常保留在服务端诊断及cause；既有Session下载事件消费者只持久化该错误文本，不增加正则脱敏或第二错误链。成功下载记录和目标不变，失败后可显式重试。
- 固定完整tree `0c76e46e8652bf045944126e8b4d12535cf0b176`：`browser-download-errors-static-unit-001` Ruff/compileall/Node语法通过，12项Bun测试通过；`http-001` 为1 passed/13.00秒、exit0，真实Chromium下载事件经Node HTTP触发目录冲突，核原冲突文件不变、零成功下载发布，Node响应、Python资源GET和browsers.json的download_error一致且无原生路径，同一request_id；服务stderr保原存储位置。修复目录后再次通过真实下载事件保存并逐字核正文。
- 完整archive仅恢复旧存储producer的`red-001`两项均失败，区分目录创建及saveAs原路径回显。各run源码前后一致、无运行组残留；准确命令/指纹/raw/退出码均保存在 `out/tests/temp/openspec_takeover/artifacts/runs/browser-download-errors-*`。本切片只覆盖下载错误产生及消费链，Browser file URL合同仍待裁定，其它producer及2.2/7.2整体门继续未勾。


### 2026-10-22 Browser截图存储错误出口（部分收口）

- 截图存储owner在mkdir/writeFile边界生成browser/screenshot身份、阶段及错误码，原异常与真实目标保留于cause和Node诊断；现有HTTP、Python同步/异步及Agent工具消费者沿同一链路传播，不增加截图错误持久状态或正则脱敏。
- 固定完整tree `291a3fcf8077453debcf378b998fc7abc6cec309`：`browser-screenshot-errors-static-unit-001` Ruff/compileall/Node语法通过，14项Bun测试通过；`http-002` 为1 passed/12.68秒、exit0。产品API创建浏览器，真实Chromium截图分别经同步HTTP、异步HTTP及Agent工具遭遇目录冲突，错误无原生路径、原冲突文件不变，资源归属仍verified且lease释放；移除冲突后经同一工具取得真实PNG并逐字核HTTP读取正文。正文写入失败在store单测注入验证，真实HTTP只覆盖目录创建失败。
- 完整archive只还原旧截图producer的`red-001`为2 failed，均精确区分旧错误回显路径及缺身份。首轮`http-001`截图通过，后续两项因共用旧装具留下无冻结队列的执行记录在下一lifespan初始化失败；未计三项整组通过，装具迁移另作独立切片。各run源码前后一致、无运行组残留，准确命令/指纹/raw/退出码见 `out/tests/temp/openspec_takeover/artifacts/runs/browser-screenshot-errors-*`。本项不代表Browser file URL或其它producer及2.2/7.2整体完成。


### 2026-10-22 Browser恢复记录存储错误出口（部分收口）

- Browser状态读取/发布仍由同一store负责，在正文读取、解析、记录核验及目录创建、正文写入、文件发布的实际失败处生成workspace身份、artifact、阶段和原错误码。原生存储位置与完整异常只留在cause和服务诊断；发布失败只清本次临时文件，清理同样失败时保留两份原异常，主错误码不被覆盖。不以响应正则掩盖路径，也不把持久化失败伪报为内存回滚。
- 固定代码候选004 tree `0af3ed741574e11792144e99bb1ecf166958d893`：复用逐字相同的003产品/JS测试blob，`browser-record-errors-static-unit-003` 为20项Bun/132断言与5项Node通过；004补精简应用注册现有产品HTTP异常处理器，`browser-record-errors-static-004` Ruff/compileall/Node语法exit0。`browser-record-errors-http-004` 为2 passed/15.03秒、exit0：真实API创建Browser，同步/异步Node HTTP与资源API均遇到真实发布目录冲突，核完整安全错误、同一request_id、原状态字节不变、零临时残留、owner仍verified及lease释放，恢复原文件后同一操作显式重试成功；同时复验既有checkpoint失败/恢复链。
- 完整archive只恢复旧read/write producer的`browser-record-errors-red-001`为6 failed，分别区分原路径回显及缺错误归属。首轮003真实HTTP的sync/async已通过，后续因精简应用漏注册生产HTTP异常处理器而缺request_id，不算整轮通过；候选002构造及输入缺失失败均保留原件。准确命令/源码指纹/raw/退出码、产品原件SHA与复用范围见本任务`browser-record-errors-proof-004.json`及`runs/browser-record-errors-*`。不代替Gateway/Web、Windows或2.2/7.2整体门，其它producer与Browser file URL边界仍独立处理。

### 2026-10-23 Terminal响应协议错误出口（部分收口）

- HTTP成功但响应JSON/UTF-8、顶层结构、列表成员、授权data或Terminal记录损坏时，实际client解码边界只发布固定阶段，完整响应和原异常留服务诊断及cause；读取与删除/停止/steering共用envelope解析。保留ValueError类API400、TypeError/OverflowError类API500、unknown、lease释放和禁止自动重放；HTTP失败仍沿用原TerminalRequestError合同，无响应端路径正则新增。
- 最终tree `bca0891bfc0a21c5d87f3cc2891b7984b1a106be`：`terminal-protocol-api-004`为36 passed/4.11秒，覆盖POSIX、盘符、UNC、扩展UNC、原始cause对象、重启GET/list及仅一次DELETE；`integration-004`为4 passed/35.92秒，真实PTY核同步/异步HTTP错误、工作区外原生cwd及真实工具准入/lease/最终回执。Ruff004通过；003其余57项client/owner/codec测试全部通过且相关生产与测试字节到004不变，限定复用；003整体77 passed/16 failed，16项为新增400场景误用500响应字段的测试预期错误，004只修fixture既有detail形状，不改生产状态码。
- 完整archive旧producer的`red-001`为24 failed/4 passed，区分旧正文回显或缺失协议失败阶段；001静态导入排版及002曾将ValueError误归TypeError的候选不计最终通过。非作者只读复核004无剩余finding，未独立跑测试。各run原件、准确命令、前后指纹、退出码、零进程残留与限定复用记录在 `out/tests/temp/openspec_resume_20261023/artifacts/terminal-protocol-proof-004.json`。不替代Gateway/Web、Windows或2.2/7.2全量不变量；Browser自由错误正文及其它producer仍待迁齐。

### 2026-10-23 Browser响应协议错误出口（部分收口）

- Browser client在JSON/UTF-8、顶层/data/envelope/list及codec解码失败处发布固定阶段，完整原响应和异常留诊断及cause，保留ValueError类400、TypeError/OverflowError类500。非JSON/非对象HTTP失败仍为原RuntimeError/API500，消息只保method和原HTTPstatus；结构化BrowserRequestError及其code/retryable/recovery/timeout_ms、verified失败回执保持原合同，未用新正则脱敏。合法file页面URL原样经过同一decoder。
- 同链审查发现成功owner-revision空对象及残缺grant被当成功：sync现消费真实snapshot并核browser/workspace/revision；成功grant只核既有4字段完整性和ID/版本绑定，返回原回执，不另定token格式或过期策略。坏grant在owner内转unknown并释放lease，空sync阻止恢复完成，修复传输后可重新恢复。
- 最终tree `ebb0d9d322dec44259dca76c6ad5e7c5655ed7f1`：`browser-protocol-unit-002`为116 passed/12.13秒，`integration-002`为4 passed/23.02秒，真实Chromium/Node HTTP验证sync/async检查点失败与显式重试、Agent锁定零callback、Playwright超时后操作及成功grant→WebSocket attach/delete；Ruff002通过。完整archive旧producer的red001为40 failed，区分原值回显/缺阶段或诊断；001通过103项但缺成功回执校验，不计本项最终闭合。非作者只读复核002无剩余finding，未独立运行。准确命令/raw/指纹/退出码/零残留及证据SHA见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-protocol-proof-002.json`；不替代完整Gateway/Web、Windows或2.2/7.2，结构化自由错误及其它producer仍待迁齐。

### 2026-10-23 Browser超时恢复错误出口（部分收口）

- 超时恢复的同步throw、异步reject与延迟失败共用同一错误写入点，只发布恢复阶段，原异常保留在cause和服务诊断。Session的同一恢复屏障以browser/page身份与阶段报告失败，避免后续排队操作重新抛出原生异常；超时立即返回、后台恢复及原408/后续500分类保持，读取/截图/脚本调用方同批迁齐，不增加响应正则或第二恢复链。
- 最终tree `8efc0d2156c828958cab02145ecfdf64b43fe2b1`：`browser-timeout-static-unit-002` Node语法/Ruff通过，56项Bun/268断言通过；`integration-001`为3 passed/23.02秒，真实Chromium/Node HTTP核sync/async的408/metadata、后续API阻止脚本及同request_id、verified/lease释放、持久记录不含原生路径、原异常诊断和显式删除，并复核正常恢复后的键盘/读取/截图。第三方newPage故障为测试注入，不声称真实系统EACCES；001与002产品/集成测试逐字一致限定复用，002只修新增Session单测装具缺cdpSession及补首次超时断言。
- 旧生产完整archive红测52 passed/4 failed，四项均区分原正文或恢复屏障回显；首轮静态单测55 passed/1 failed源于装具未进入assertRunning，失败原件保留。非作者只读复核002无finding，未独立跑测试。准确命令/raw/前后指纹/退出码/复用范围见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-timeout-proof-002.json`。不代替Gateway/Web、Windows或2.2/7.2，其他Browser producer仍待迁齐。

### 2026-10-23 Browser标签页注册错误出口（部分收口）

- 注册owner的共享promise只发布browser/page身份和register_page阶段，保留原cause、code与statusCode，原异常写诊断。context-page观察者消费同一错误正文写error_message并emit完整state，不再把原生正文或自身stack复制到普通快照；初始启动、页面创建、恢复和HTTP/WS调用方继续沿既有注册链消费，不改页面发布或运行状态合同。
- tree `cc2ab0e790be68a5ff093947da89e07b643e9654`：`browser-registration-static-unit-001` Node语法/Ruff通过，73项Bun/299断言通过；`integration-001`为3 passed/22.37秒，真实Chromium/HTTP验证sync/async失败、WS state、资源API/request_id、failed operation及error_message落盘、verified/lease释放、显式重试成功，并复核正常超时恢复。注入第三方newCDPSession的失败覆盖pageEntries发布前；独立popup观察者本身没有新增持久化，落盘由真实HTTP操作finish证明。pendingRegistration成功后的独立activatePage失败仍归激活链，不以此声称全部注册/激活错误已闭合。
- 旧生产完整archive红测46 passed/1 failed，明确区分原生异常未转换；非作者只读复核无finding，未独立跑测试。准确命令/raw/前后指纹/退出码/零进程残留及产品回执SHA见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-registration-proof-001.json`。不替代完整Gateway/Web、Windows或2.2/7.2，其它producer继续推进。


### 2026-10-23 Browser冻结、冷恢复与释放错误出口（部分收口）

- 在保护检查、检查点采集/恢复、冻结/唤醒CDP及运行时释放的实际失败处发布browser/page身份与阶段，原异常只交cause和诊断，原code/statusCode保留。原冻结回滚、unknown阻断、context句柄及显式终态核实合同不变；导航/控件恢复仍保原恢复失败code及具名file页面URL，检查点存储发布错误不被覆盖。
- 固定tree `9ad947ef565638d335de1bdbec2e503e259d975f`：`browser-lifecycle-static-unit-002` Node语法/Ruff通过，95项Bun/416断言通过；`integration-002`为7 passed/33.87秒，真实Chromium/HTTP验证sync/async及资源API、保护/冻结回滚/冷恢复/释放失败、request_id、持久错误与诊断、lease释放、unknown零执行和真实关闭后owner核实，并复验检查点发布失败及中文/空格/#/%的file页面URL在freeze/discard/wake与超时恢复中的保留。第三方故障为一次性注入，不声称真实系统EACCES。
- 完整archive旧生产红测41 passed/18 failed，区分原路径正文/缺阶段及原cause丢失。首轮集成失败源于unknown用例直接detach而未先显式关闭，不放宽状态或目录精确断言；002通过真实Node关闭和owner stop核终态清理。准确命令/raw/前后指纹/退出码/无进程组残留及产品产物SHA见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-lifecycle-proof-002.json`。不代替完整Gateway/Web、Windows或2.2/7.2；activatePage及部分事件回调仍待后续切片。


### 2026-10-23 Browser标签页激活与事件错误出口（部分收口）

- 标签页激活复用同一取帧/流控制CDP错误出口，pointer reset及页面状态读取按实际失败阶段保留cause/code/status；URL与title的更新顺序保持，存储错误继续由原store产生，不加外层包装。dialog/filechooser/close仅将安全message写入恢复记录，完整stack和cause交诊断；已完成的标签页切换与流状态不伪报回滚。
- 最终tree `9f651f8a7adafa75a00c77fbaafa7f9df5949aff`：`browser-activation-static-regression-002` Node语法/Ruff、252项Bun及9项Node存储故障测试通过；`node-regression-002`另8项Node帧统计通过。`integration-002`为8 passed/37.02秒，新增真实冷恢复末步title失败，核完整释放后仍discarded/verified；复验上一批sync/async/API与file URL恢复。`form-state-002`三项真实Chromium场景通过，覆盖混合控件、storage、重排与大型匿名表单。该场景的Manager持久化为空操作，不替代HTTP落盘验收。
- 完整archive旧生产红测78 passed/8 failed，分别区分激活五阶段及三种事件的原错误/stack写入。首轮扩大验证误将node:test文件交Bun，失败均为Bun未实现mock，002按原生运行器执行全部通过；不改测试或生产规避。001→002仅保持既有URL先于title更新的顺序，最终版本重新执行静态、回归与产品测试。独立真实producer复核002无finding，001关于persist原错误的假设已撤回，原件保留。准确命令/raw/指纹/退出码/零进程残留及产品证据SHA见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-activation-proof-002.json`。其它Browser动作及2.2/7.2全量门仍未关闭。


### 2026-10-23 Browser普通导航错误出口（部分收口）

- goto/reload/back/forward与主文档requestfailed事件共用导航错误DTO，只发布请求/实际页面URL、browser/page身份、阶段及结构化错误码或提取的Chromium net错误码；原生调用日志只留cause和诊断。保留file页面URL具名例外，普通导航与启动失败仍保原code/status分类。异步导航冻结发起page，切换标签页不把旧失败交给新页面；发布失败状态又遇到URL读取/同步/取帧失败时按主错、次错顺序保留AggregateError，主分类不被次错覆盖。
- 最终tree `586b0528e1afe79bad2d1b0790fb09cd16399331`：`browser-navigation-static-regression-002` Node语法/Ruff及262项Bun/1556断言、17项Node通过；`integration-002`为6 passed/28.93秒，真实Chromium/HTTP验证四种导航经sync/async/API失败、request_id、错误落盘无原生位置、完整原因诊断、verified与lease释放、再次导航成功和显式删除，并复验file页面URL恢复/失败、真实unsafe-port启动失败及原page.goto/Chromium错误码。一次性第三方注入不冒充真实系统EACCES。
- 旧生产完整archive红测区分普通导航原正文、发起page误归属和次生失败覆盖主错；准确数量见`runs/browser-navigation-red-002`原件。001六项HTTP已通过，独审后002恢复no-history back/forward保留旧错误的成功行为，并增加原错/次错和并发切换断言；002全部重新验证。独立只读复核002无finding，结构化原code的有意保留已明确，不将其误作仅限Chromium码。准确命令/raw/指纹/退出码/零进程残留及产品SHA见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-navigation-proof-002.json`。不代替完整Gateway/Web、Windows或2.2/7.2全量门。

### 2026-10-23 Browser启动与设备模拟错误出口（部分收口）

- 启动和冷恢复共用Session的acquireRuntime，移除assignRuntime及重复获取包装；初始newPage、默认UA/platform读取、viewport和五条设备模拟CDP在实际操作处产生带身份/阶段/code/status的错误，原异常保留cause与诊断。启动清理再次失败时保留主错分类和有序AggregateError，不丢失unknown事实及context句柄；原注册、导航、存储错误owner与成功顺序保持。
- 最终tree `a74570cc071e53930edb717625bcca92cbbfc74f`：Node语法/Ruff、275项Bun/1672断言、17项Node通过；13项真实Chromium/HTTP集成通过（60.45秒），覆盖五种启动失败、资源API/request_id、sync/async读取、落盘与完整诊断、首次creating观察采用Node已核实failed事实、operation lease释放、显式stop/删除和再次创建；复验冷恢复六阶段、file URL与真实unsafe-port启动失败。第三方一次性注入不声称真实系统EACCES，cleanup双失败限单测。001误读会话简表、002混淆首次creating与既有unknown，均只修正新测试；持久unknown不因普通GET核实的原约束未改。
- 完整archive旧生产红测95 passed/13 failed；执行单测与003相同，区分原异常正文、阶段缺失和cleanup覆盖主错。独立003只读审查无finding；归档无.git，不能以向上发现的主仓HEAD判断其tree，已逐blob核验完整候选内容。准确命令/raw/指纹/退出码、零进程组残留及产品SHA见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-startup-proof-003.json`。普通标签页/其它动作及2.2/7.2全量门继续推进。


### 2026-10-23 Browser标签页与基本控制错误出口（部分收口）

- 启动、冷恢复、普通建页与超时恢复共用实际newPage错误入口，保留原cause/code/status及诊断；close_page、停止加载与客户端截图在原生操作边界发布安全身份/阶段。超时恢复继续保留reset_page公开合同，因果链精确为reset_page→create_page→原异常；原页面、流、状态和持久化时机不变。
- 最终tree `a7296e51cb17da2762aec80c31f2a55a2fbd15b2`：280项Bun/1713断言、17项Node及Node语法通过（JS与002逐字相同限定复用），最终Python Ruff通过；18项真实Chromium/HTTP/WS集成通过（81.87秒），覆盖四控制边界经sync/API/WS失败、request_id、原page/页数/verified与lease、失败重试和PNG字节，并复验启动/冷恢复/file URL/超时恢复。截图为既有不立即落盘的并发操作，测试经原resource-policy入口发布后精确核相同失败operation_id，未改变持久化合同。
- 完整archive旧生产红测87 passed/5 failed；001选错pytest node未执行，002遗漏WS browserId，003错误假设并发截图立即落盘，均保留原件并按真实合同修新测试。最终候选4283个blob逐一核实；独立004只读审查无finding，未独立跑测试。准确命令/raw/指纹/退出码、零进程组残留及四份产品证据SHA见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-page-controls-proof-004.json`。第三方故障为一次性注入，不声称真实系统EACCES；不代替完整Gateway/Web、Windows或2.2/7.2全量门。


### 2026-10-23 Browser动态设备切换与帧关闭屏障（部分收口）

- 动态切换的Network开关、导航和URL恢复按实际阶段发布错误，保留原cause/code/status与完整诊断；命令先登记逆序恢复，逐项尝试全部清理和逐页设备回滚，主错不被次错覆盖。失败后为仍连接的用户恢复原帧流；恢复失败加入同一因果链并置unknown。页面导航已提交时不伪报文档回滚，replaceState失败保留真实当前页面URL，file页面URL例外不扩大到原生存储位置。
- 集成001的最终Node记录与stderr共同确认：context关闭时在途帧ACK触发unhandled rejection，导致后续检查级联失败，并非12个独立产品失败。现在以具体page entry登记回调，关闭前阻止新接纳并排空既有截图/ACK，再释放page/context；默认5秒超时或排空失败明确unknown并保留句柄，迟到旧entry错误只留诊断、不污染新页。回调失败明确发布stream_error/state，未用TargetClosed字符串吞错。超时脚本恢复原有中断旧页行为保持，由entry归属隔离迟到帧失败。
- 最终产品tree `0cffd5d78c0dcbe262c78f2cbbba50821569fa49`：295项Bun/1835断言、17项Node、Node语法与Ruff通过；14项真实Chromium/HTTP/WS集成通过（70.12秒），覆盖8种设备失败、失败后新JPEG帧、恢复失败unknown阻断、精确URL、重试、持久记录/诊断/lease、附加状态删除存活与file恢复。003扩大回归18项通过（88.16秒），其启动路径不变；004新增单页drain失败分支重新单测与集成验证。完整archive旧生产红测85 passed/14 failed；前后指纹、全部blob和零进程组残留均核验。
- 非作者004只读审查无finding；准确命令、原始输出、退出码、候选SHA和产品回执见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-device-switch-proof-004.json`。一次性原生故障注入不冒充真实系统EACCES；底层永久悬挂的CDP任务仍保留供诊断，上层有界返回。本组不代替完整Gateway/Web、Windows或2.2/7.2全量门。

### 2026-10-23 Browser文件选择错误出口（部分收口）

- 目标机器原生文件路径与客户端内存文件仍按各自输入合同进入同一次Playwright `setFiles`；实际选择失败按chooser所属page发布browser/page身份、select_files阶段及原code/status，完整异常与路径保留在诊断及cause。失败不清空chooser，显式重试仍操作原选择器；不将用户绝对路径改写为工作区路径。
- 最终产品tree `83ebf8eb5ae1387f410ad152f640605664ab3349`：297项Bun/1857断言、17项Node及语法检查通过（与003相同JS限定复用），最终Python Ruff通过；`integration-005`为3 passed/22.44秒，真实Chromium/HTTP/WS覆盖真实ENOENT的sync/async/工具/API错误、request_id、无路径落盘/正文、lease释放与verified、本机中文空格文件重试后的精确名称/正文、客户端内存上传、既有工具动作和file页面恢复。完整archive旧生产真实红测明确回显缺失文件绝对路径；独立小型红绿为新12通过、旧2失败/10通过。
- 001新测试误读result层级、integration-003与红测并行产生端口冲突、004新测试participantId不合协议，原件保留；005已修新测试并串行通过。等待WS helper遇error立即失败，不再忽略协议拒绝后等超时。WS失败的payload分支由单测覆盖，真实网络故障来自native输入；不扩大证明范围。4283个blob、前后指纹和零进程组残留均核验，限定证据见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-file-selection-proof-004.json`。本切片不代替2.2/7.2、完整Web或Windows验收。

### 2026-10-23 Browser截图与下载读取错误出口（部分收口）

- 两类产物共用同一FileHandle的open/stat/stream/close链，普通文件核验在响应头前完成；实际文件系统失败只发布browser/artifact/kind/stage/code，完整位置及原异常保留诊断/cause。缺失文件维持404；流开始后失败明确销毁响应，关闭次错不覆盖主错分类，并按顺序保留AggregateError。成功响应头、中文空格文件名与原始字节不变。
- 最终tree `289963883cda5ab9cd0e278db0fa85436781d97e`：Node语法/Ruff通过，7项真实Chromium/HTTP集成通过（34.54秒），覆盖两类产物的真实ENOENT/ENOTDIR/EISDIR/EACCES、安全错误和不变恢复记录、文件恢复后的精确字节/头，以及流、关闭、双失败和响应销毁后的重试。后三类为一次性文件句柄/流注入，不声称真实磁盘EIO/EBADF。已有截图故障用例不再假定截图目录未创建，而是保留/恢复既有目录；首次扩大回归的准备阶段失败原件保留，最终同批复验通过。
- 完整archive旧生产两项真实红测回显截图/下载原生位置。非作者002只读复核无finding，003仅调整负例顺序并完整重验；4283个blob、前后指纹与零进程组残留均核验，限定收据见 `out/tests/temp/openspec_resume_20261023/artifacts/browser-artifact-read-proof-003.json`。不代替完整Web、Windows或2.2/7.2全量门。

### 2026-10-23 外部资源错误投影移除文本路径兜底（部分收口）

- Browser/Terminal原生producer迁移与固定HEAD调用图复核后，删除`public_error`的路径正则与临时TODO；保留OSError/URLError按类型及errno/winerror投影。非原生错误保留原owner发布的正文，不再把`/api/browsers/...`接口路由误判为本机文件路径，也不新增通用脱敏层。
- 最终tree `1e357a50891693c319abd31986c5737c7c8ec9f7`：Ruff及122项API/owner/fork检查通过（4.08秒），8项真实Chromium/HTTP错误入口通过（36.92秒），复验检查点、文件选择和生命周期安全响应/记录/重试。新测试经真实BrowserManagerClient和受控urllib HTTPError证明code-only回执的路由信息保留到API、unknown记录与重启读取，零重放且释放lease；完整archive旧生产红测明确复现误吞。首次新测试误用完整应用error body合同，轻量API fixture只装middleware，002修为核实际header，原件保留。
- 非作者只读审查未找到这些consumer仍需文本兜底才能阻止native locator的具体producer；不将任意用户脚本/自由正文泛化成资源引用。全部4283个blob、前后指纹与零进程组残留已核，收据 `out/tests/temp/openspec_resume_20261023/artifacts/resource-error-contract-proof-002.json`。Terminal直接HTTP/WS错误另行核查，不以本项关闭2.2/7.2。

### 2026-10-23 Terminal状态存储错误与显式重试（部分收口）

- 实际存储owner按读写阶段发布workspace/artifact/stage/code，完整位置和原异常只留日志/cause；仅正文读取ENOENT表示无状态。临时写入/发布失败才清理本次临时文件，清理次错按顺序保留且不覆盖主错。首次resize已生效但保存失败时保留既有待写标记，同尺寸显式重试完成落盘；在途同尺寸请求等待同一保存结果，干净同尺寸零重写，不自动重试PTY操作或伪报回滚。
- 产品tree `a63b27772afac25a781a5804c60e7db7e9c17155`：Node语法/Ruff、33项存储/Manager单测及79项Session/输出/进程回归通过；真实PTY/HTTP集成1项通过（2.38秒），依次覆盖真实mkdir冲突、非特权Linux EACCES、rename发布失败，核安全响应、旧字节、内存已变化、同请求恢复后落盘和完整诊断。Windows/root不冒充chmod权限验收。旧Manager完整archive的新增3项红测均失败；旧StateStore真实HTTP红测回显原生存储位置。初次集成误用了另一聊天未提交fixture签名，修正后002明确复现同值重试不落盘，003修产品后通过，原件保留。
- 非作者003只读独审无阻断finding；4284个blob、前后指纹与零进程组残留核验见 `out/tests/temp/openspec_resume_20261023/artifacts/terminal-state-contract-proof-003.json`。共享测试文件的既有fixture优化未纳入本切片。不代替完整Gateway/Web、Windows或2.2/7.2全量门。


### 2026-10-23 Workspace原始文件与目录ZIP下载错误出口（部分收口）

- raw与download共用唯一准备入口，移除旧resolve_raw_file及其消费者；目标解析、stat、临时归档及延迟open/read/close在实际I/O边界发布URI/阶段/异常类型/errno/winerror，原生位置和原异常保留诊断/cause。复用Starlette的Range和响应头实现，首body前保留响应头，首读失败可返回原request_id错误；流开始后失败中断连接，不拼接错误正文。临时ZIP在成功、非法Range、传输失败和取消时清理，主错与清理次错按序保留。
- 固定产品tree `76111cb761d89d9e60aaaf7c502a88c58ba06953`：Ruff通过（API原有27项B008与HEAD完全一致，未新增，限定检查显式忽略）；60项文件/API定向回归通过，14项真实HTTP测试通过（10.22秒），核三种输入原字节、中文空格百分号文件名、单/多Range、目录ZIP、原件保持、真实非特权POSIX EACCES、注入stat/归档/延迟读取失败及流中断/显式重试。传输与清理双失败、close及取消由定点单测覆盖，不冒充真实磁盘EIO。
- 完整旧HEAD archive加新HTTP回归5项失败，其中stat两路、mkstemp及归档write回显原生路径；归档检查项因旧链无该阶段返回200，只计行为区分。固定001的测试import排版诊断已修，002仅import格式化，7个相关Python AST等价，复用60项通过结果；共享测试目录重组与helper调整不纳入本切片。4287个blob、前后指纹、无残留进程组及独立只读审查见 `out/tests/temp/openspec_resume_20261023/artifacts/workspace-download-proof-001.json`。不以此关闭2.2/7.2或声称完整Gateway/Web/Windows验收。


### 2026-10-23 导航队列原始记录错误出口（2.2/7.2 部分收口）

- 唯一行投影在JSON、原始时间及typed模型校验的实际失败点发布固定schema字段/校验类别，不将损坏输入、额外键名或validator正文复制到普通响应。保留原TypeError/ValueError分类；启动核验与事件错误仅报告artifact/stage/rule，物理DB位置、原值和异常链留服务端日志/cause。后台worker失败只公开异常类型，启动超时继续公开明确的owner等待原因；不改HTTP 400/500/503、序号、事务或schema，参数编码/preimage函数AST相同。
- 固定完整tree `87cde2fd6f3e34b2d7185eeb6b4892c2020cccb0`：11项新增真实API/worker/SQLite的ASGI与冷启动集成通过（24.14秒），111项相邻意图/事件/owner/元数据/worker/路由回归通过（173.45秒），Ruff/compile/strict44项均通过。相同新增测试在完整旧生产archive下11项全部失败，明确复现响应与启动错误复制原生路径。首轮邻居68失败含候选遗漏.env导致的Provider环境缺失，以及worker旧全文断言；补候选依赖后，unit改核三个入口的安全摘要及精确原cause，启动超时保留原有原因，未放宽身份、顺序和原件断言。
- 非作者005只读独审GO，无finding、未独立复跑；4302项全源码前后指纹、raw/SHA、实际退出码、零进程组残留与准确命令见 `out/tests/temp/navigation_public_errors_20261023/artifacts/validation-proof-005.json` 及runs。该集成不代表真实网络、完整Gateway/Web或Windows；不据此关闭2.2/7.2。完整Web rc2断言已另提，后继default在共享资源场景收到Gateway 502；后端在该响应之后仍写入日志，不能认定后端先退出，具体上游RequestError仍待诊断，不混记为本批通过。


### 2026-10-23 删除物理收尾错误出口（2.2/7.2 部分收口）

- 删除正常执行与启动恢复复用唯一 repair 回执写口，只持久化 operation/target 身份、artifact/stage、异常类别及适用的 errno/winerror/SQLite 数值码；完整异常及 cause 统一记入服务端日志。逻辑 committed、物理 repair_required、原 journal/冻结集合、owner generation、回执修订幂等与成功重试清错保持原合同，不改 schema。
- 固定完整 tree `35401f289d9e41a8e3332acd0f892b1bd77ba990`：6 项新增 ASGI/API、worker 与 SQLite 集成通过，覆盖 Session/目录删除与 RuntimeError/原生文件/SQLite 故障；44 项相邻导航回归通过，Ruff、compileall、strict OpenSpec 全部通过。相同最终测试在旧生产完整 archive 下 6 项全部失败，检出原路径泄露。非作者只读独审 GO，未独立复跑。首轮测试错误地把 SQLite 的正常 WAL/SHM 旁文件当成业务变更，随后按普通文件精确字节及 SQLite 完整逻辑 dump 核对；Windows 路径日志按异常的标准转义表示精确核对，未放宽原件、身份、顺序或恢复断言。
- 4303 项源码前后指纹、原始输出 SHA、实际退出码与零进程残留见 `out/tests/temp/delete_settlement_errors_20261023/artifacts/validation-proof-003.json`；只证明 ASGI 跨模块与 worker stop/start，不代真实网络、OS 进程重启、完整 Web 或 Windows 实机。2.2/7.2 保持未勾。


### 2026-10-23 初始执行绑定错误出口与限定 Web 复验（2.2/7.2 部分收口）

- 初始 execution worker 的唯一失败写口只保存冻结 Session/Thread/binding/job 身份、stage、错误类别及系统数值码；异常原文、完整异常链和非法返回值只留原始诊断日志。identity 漂移仅公开字段名。保留逐条继续、pending/claim/generation、冷重开恢复及准入后失败的幂等，不改 schema 或执行队列 owner。
- 固定完整 tree `6f9e9bfe4be640542df38888855ef92c294ddec9`：26 项 worker 单测、10 项真实 Session/Thread 创建和 SQLite 准入/恢复集成、Ruff、compileall 通过。相同最终测试在旧生产完整 archive 下 10 项失败，其中 8 项明确检出异常/漂移路径泄露，2 项非法类型检出身份/stage/诊断缺失。初轮 pytest 启动参数错误和未物化错误文案的测试预期错误均保留原件；相邻委派组 15 failed/3 passed，在旧生产同样失败于 binder 之前的工作区身份装具，未把该组记为通过。4305 项源码指纹、raw/SHA、退出码、零残留及非作者只读审查见 `out/tests/temp/initial_binding_errors_20261023/artifacts/validation-proof-002.json`。仅控制库冷重开，不代表 OS 进程重启或完整模型执行；2.2/7.2 仍未勾。
- Web 固定 tree `8f91e5be57eca9d28e0252366c64ba7706faea80` 经真实浏览器、Gateway 和 Workspace，使用现有 model_stream replay，运行 `runtime_shared_resources,weekly,initial_admission_recovery,generated_internal_history` 四个场景，matrix exit0，1 passed/1052.69秒。原命令、构建、DOM/网络及产品原件见 `out/tests/temp/web_upstream_diagnostics_20261023/artifacts/focus-web-proof-001.json`；这不是完整 default 或 Windows 验收。本轮未复现先前上游502，不据此声称根因已修复；新增非零退出进程快照分支未动态执行。

### 2026-10-23 Gateway 配置运行时应用与回退错误出口（2.2/7.2 部分收口）

- 唯一 reload owner 在 runtime apply/promotion/rollback 边界发布固定阶段、错误类型和可用的 errno/winerror/SQLite code，pending、journal、compensation、事件、状态响应与抛出异常共用该事实；完整原异常及 cause 保留于具名管理诊断日志。合并原有 conflict/普通失败的重复处理，保留 conflict/recovery_required/compensated、旧 active 与 claim 释放行为。未按路径文本过滤，也未增加第二写入链。
- 固定 tree `6df77f001bd766d7bd9167a6e60ddc7a9bcc1375` 的 `runtime-state-final-004` 为9 passed/18 deselected、exit0，覆盖三阶段×POSIX/盘符/UNC，经真实配置 loader、runtime callback、SQLite、ASGI status/events 及关闭原 owner 后冷重开。003 的三个相关测试文件131 passed/2 failed；两个失败是新日志断言未考虑 OSError.filename 的 repr 转义，004仅修该断言并重跑9项，生产逐字相同，复用其余124项既有通过用例，不表述为一次133项全绿。完整旧实现 red002 的9项均检出路径泄露；red001的datetime序列化错误、green002的测试重开锁冲突原件保留，不计通过。
- 最终Ruff exit0，compileall沿未变生产复用003；4306项源码前后指纹、raw/SHA、实际退出码、零进程组残留与非作者只读复核见 `out/tests/temp/gateway_config_runtime_errors_20261023/artifacts/validation-proof-004.json`、`review-004.json`。仅为ASGI及控制库冷重开，不代替真实网络/OS重启/Windows；其它producer及2.2/7.2全局门保持未勾。


### 2026-10-23 Session 创建存储与 prepared 校验错误出口（2.2/7.2 部分收口）

- 创建 owner 在准备、读取、发布 rename 与隔离的实际 I/O 边界公开 workspace/session 身份、stage、错误类型及系统数值码，完整原异常留诊断日志/cause；prepared 目录、manifest、GraphBinding 与 fence 冲突不再复制未经校验的条目、正文或物理位置。路径预算仍在持久创建前拒绝，原准备/耐久失败的异常类型与补偿、保留现场、冻结身份及显式重试合同不变，无 schema 变更。
- 固定 tree `a6e55a609d978edf89eb3ac33500875693b18c51` 的新增集成文件21项通过，覆盖四阶段×POSIX/盘符/UNC注入、7类真实prepared损坏、真实sessions路由/TraceMiddleware的400/500及后继显式重试。002的189项创建/Thread/启动/SessionService回归通过；003只新增HTTP测试和import排序，生产/unit逐字相同，复用其余170项与compile，不表述为一次191项全绿。Ruff通过；旧生产完整archive的12项红测准确检出路径外泄。001的3项失败源于测试注入同时拦截补偿rename，收窄注入目标后重验，原件保留。
- 全4307项源码前后指纹、raw/SHA、实际退出码、零进程残留与非作者只读审查见 `out/tests/temp/session_creation_uri_errors_20261023/artifacts/validation-proof-003.json`、`review-002.json`。仅ASGI与SQLite冷重开，Windows文本注入不代替Windows实机；其它stat/catalog/Thread/fork出口及2.2/7.2总门保持未勾。


### 2026-10-23 Thread 物理创建、校验与清理错误出口（2.2/7.2 部分收口）

- 唯一物理创建链在准备/读取/rename/目录耐久/定点清理处公开 Session/Thread 身份、stage、错误类型和系统码，原生位置、未知条目及原异常保留诊断日志/cause；六处正式目录校验与所有直接测试调用方同批迁齐显式 owner，无缺省身份或旧签名。partial 复用、未知项拒绝、先全部校验再删除、清理失败不 abort、原生异常类型与 rename 的 RuntimeError 分类保持；os.walk 的扫描失败改为明确抛出，不再静默当作空目录。路径预算的 ValueError、原生 OSError 及 resolve 的 RuntimeError 均在 record 冻结前报告逻辑身份。
- 固定 tree `d5dd074c8779eef2925da102d3103456fefb4b84` 的 Session/Thread 两个单测全文件与新增集成全文件共168项通过（30.95秒），Ruff/compileall通过。通过真实 Session/child 创建覆盖三种平台位置文本×prepare/read/walk/rename/cleanup、5类实际目录损坏、partial staging重建删除、同内容final的冗余staging删除、rename后fsync及真实POSIX自指symlink循环；核 preparing、零发布、原件、精确cause与同key恢复。旧生产完整archive的rename三项红测准确复现位置外泄。001的审查遗漏resolve RuntimeError已修并在003重验，不能把Windows文本注入称为Windows实机。
- 原始命令、全源码指纹、raw/SHA、实际退出码及零进程残留见 `out/tests/temp/thread_creation_uri_errors_20261023/artifacts/validation-proof-003.json` 和 `review-003.json`。相邻fork的直接pytest因规定的matrix watchdog前置条件产生15项setup错误；改用正式matrix后遇到既有测试从已关闭catalog owner再次解析源路径的失败，继而被未终结fork期限watchdog以143退出，该组不计通过。任意publish/物化callback错误写abort_reason、其它owner出口及2.2/7.2总门仍未关闭；本项不代替完整Gateway/Web、进程重启或Windows验收。


### 2026-10-23 Thread 发布失败与成员中止原因（2.2/7.2 部分收口）

- 发布阶段的 deletion/CAS 失败与已非 preparing 出口使用 Session/Thread、阶段和异常类型，完整原异常保留日志/cause；删除中仍为 SessionDeletionPendingError，并保留稳定 session_deletion_pending 标识。定点清理失败仍 preparing，published/aborted 不进入再次清理。成员完整性失败只把稳定身份/阶段写入中止原因，原异常作为诊断与精确 cause；先提交原 operation/member 终结，再抛错误的顺序保持。原始系统码格式由 Thread 创建唯一错误投影共用，无新存储或 schema 变化。
- 固定 tree `b0a487ef1820080e376a7185e994a8dfc7f16be8` 的集成文件35项通过（33.72秒），9项正式委派入口覆盖三种平台位置文本×deletion/CAS/membership，核即时异常、原cause/log、冷重开中止原因、成员cancelled、零child/intent及同delegation拒绝换绑；另核已aborted保留final原件。001的289项Thread/ControlStore单测通过，002仅修正9项委派重试测试预期，生产与既有单测逐字相同，复用其单测结果和compile，不称为一次324项全绿。Ruff通过；旧生产完整archive的10项红测准确检出位置外泄。001的9项失败均源于测试错把cancelled-member先拒绝预期为后置ThreadCreationAbortedError，按实际既有合同修正，未修改生产来迎合测试。
- 命令、全源码前后指纹、raw/SHA、退出码、零残留及非作者审查见 `out/tests/temp/thread_failure_publication_20261023/artifacts/validation-proof-002.json`、`review-002.json`。仅覆盖发布阶段和成员中止原因；fork物化callback/capture中止原因、其它owner与URI 2.2/7.2总门仍未关闭，不代替真实Gateway/Web、进程重启或Windows实机。


### 2026-10-23 Browser 原生对话框失败与重试（2.2/7.2 部分收口）

- 原生 accept/dismiss 共用既有运行时错误 owner，公开 browser/page 身份、dialog.accept/dialog.dismiss 阶段及原 code/status；完整原异常保留诊断/cause。page 身份在原生调用前从 pending 冻结，失败保留原对话框，成功才清除；默认无参 accept、prompt 文本与取消语义保持。
- 固定 tree `bc5d7c827493f0ba418ae06fd61db069d4a9c473`：21 项动作单测、8 项端口分配测试、JS 语法及 Ruff 通过；6 项真实 Chromium/HTTP 集成通过（30.16秒），覆盖同步/异步/工具/Workspace API、失败落盘、owner/lease、同一对话框重试及精确页面返回值。新增测试在旧生产完整 archive 中4项全失败，旧实现单测9项红；非作者只读独审 GO。现有213个集成文件超过旧200端口容量，沿既有分区公式扩至250，不混入共享树命名整理。
- 首次集成止于端口容量，第二次因新测试误在 Node 访问 window 失败，修为 page.evaluate 后重验；命令、全源码前后指纹、raw/SHA、退出码及零残留见 `out/tests/temp/browser_native_dialog_20261023/artifacts/validation-proof-003.json`、`review-003.json`。Workspace API 使用 ASGI，不代完整 Gateway/Web 或 Windows 实机，2.2/7.2 总门保持未勾。


### 2026-10-23 Browser 调试快照采集错误出口（2.2/7.2 部分收口）

- DOM、来源和标题采集在原生调用边界复用既有运行时错误 owner，公开 browser/page 身份、debug_dom/debug_sources/debug_title 阶段及原 code/status；完整原异常和位置保留诊断/cause，不复制进 WebSocket 错误或 last_operation。DOM/来源前缀限制及正常返回合同不变，失败不销毁页面，下一次显式请求仍可采集。
- 固定完整 tree `43e6c2f6918a340247cd64eefabf553fe2e88af9`：132 项 Browser 操作/诊断单测、Ruff、JS 语法及 Python compile 通过；3 项真实 Chromium/Browser Manager HTTP/WebSocket 集成通过（19.53秒），覆盖三阶段一次性原生故障、安全响应/落盘记录、owner/lease、原日志原因和同连接重试。注入原生方法失败不冒充真实系统 EACCES。旧生产完整 archive 的新增3项单测全部失败，明确区分未包装的原异常。
- 完整源码前后指纹、准确命令、raw/SHA、退出码、零进程组残留及只读审查见 `out/tests/temp/browser_debug_snapshot_20261023/artifacts/validation-proof-001.json`、`review-001.json`。Workspace API 使用 ASGI，Browser Manager 使用真实网络；不代替完整 Gateway/Web、进程重启或 Windows 实机，2.2/7.2 总门保持未勾。


### 2026-10-23 Browser 文本查询失败与显式重试（2.2/7.2 部分收口）

- 复制选中文本与页内查找在各自原生 evaluate 边界复用既有运行时错误 owner，调用前冻结 page 身份；普通响应及 last_operation 仅保存 browser/page、read_clipboard/find_text 阶段和原 code/status，完整异常与位置留诊断/cause。正常选中文本、window.find 参数与结果合同不变，不增加解析或执行链。
- 固定完整 tree `ab0a39718ae19b1fc9d39192b5ee8da7b3aa5d79`：134 项操作/诊断单测、Ruff、JS 语法、Python compile 和 strict OpenSpec 通过；2 项真实 Chromium/Browser Manager HTTP/WebSocket 集成通过（18.41秒），核授权、安全错误与落盘记录、owner/lease、原日志原因，以及原页面重试后的精确复制文本和查找结果。旧生产完整 archive 的单测2项及集成2项全部失败，真实 WS 明确回显隔离 workspace 原生路径。
- 准确命令、4310项源码前后指纹、raw/SHA、退出码、零进程组残留和只读审查见 `out/tests/temp/browser_text_query_20261023/artifacts/validation-proof-001.json`、`review-001.json`。故障为一次性原生方法注入；非可见 clipboard 失败记录经后续真实 policy writer 序列化，不声称该 writer 前硬退出仍耐久。Workspace API 使用 ASGI，不代完整 Gateway/Web 或 Windows 实机，2.2/7.2 保持未勾。


### 2026-10-23 团队任务分派与 recipient 失败记录（2.2/7.2 部分收口）

- 团队任务dispatch与逐recipient投递的唯一失败写口只保存Session/Thread/team/task或ledger revision、阶段、异常类别及可用errno/winerror/SQLite code；完整原异常留诊断，任务抛错保原cause，部分recipient失败仍以原ExceptionGroup聚合。失败任务/事件和recipient pending记录不再复制异常正文，无新schema、writer或错误文本过滤；原receipt/finalizer/lease及显式恢复顺序不变。
- 最终完整tree `ecbfa0d267298b398f763558c8d80c0f56681d9b`：fanout集成全文件与团队business/infrastructure单测42项通过/61.21秒，Ruff及compileall通过。三种平台路径文本在回执后/任务绑定后与recipient故障共9项经真实TeamStore/ControlStore/Saver入口验证，另开read-only ControlStore核持久数据、DTO无位置，原日志/cause保原因，原Job与通知回执不重复，恢复只补失败recipient且冻结payload/hash/lease不漂移。完整旧生产archive的相同9项全部失败/13.53秒；Windows文本不代Windows实机，故障为受控原生异常注入。
- 4311项源码前后指纹、准确命令/raw/SHA、退出码、零进程残留及非作者行为/架构审查见 `out/tests/temp/team_failure_boundaries_20261023/artifacts/validation-proof-004.json`、`review-003.json`。初次runner因隔离HOME目录尚未创建在child启动前拒绝，零测试；static003发现新增logger使旧BLE001 noqa无用，移除后AST不变且最终全回归通过，原件保留。仅为服务/SQLite与cold reader、fanout owner重建，不冒充真实HTTP/完整Web/OS重启，2.2/7.2总门仍未勾。


### 2026-10-23 Fork 来源 export 释放失败与原操作恢复（2.2/7.2 部分收口）

- 来源释放失败的唯一 writer 只向原 catalog 和普通 fork DTO 保存冻结 operation/目标 Session/Thread、阶段、异常类别与可用系统码，完整原异常留诊断；pending/confirmed、取消重新抛出、真实回执确认及原冻结请求重试合同不变，无新 schema 或第二链路。
- 固定完整 tree `20a7cfc1ea9e359adb48bf63994bed611dd73c1b`：正式 matrix 的三类传输失败/三种平台路径文本与 durable abort/坏回执相邻回归7项通过（73.02秒），Ruff/compileall通过；独立冷 catalog 与 SQL dump 核业务记录无位置、原日志对象保留、重试不重复 capture 或 Job。旧生产完整 archive 的3项反例均在原生路径或序列化 private 标记检查失败（24.02秒）。remote target fixture 同批复用源端已准备的隔离模型配置，不再依赖个人模型密钥。
- 真实三端 Gateway/SSH、model_stream 与 Backend 的两项产品集成通过（263.02秒）：new Session/main 与 child 各自从真实消息生成来源，在来源已释放后硬杀造成回执丢失，核 HTTP pending/detail 与 SQLite 原件相等，两端重启后同操作确认；来源离线后目标冷读附件/历史、原请求重放与后续真实执行保持归属及冻结请求。51份产品原件独立封存；准确命令、4310项源码前后指纹、raw/SHA、退出码、进程回收与独审见 `out/tests/temp/fork_export_error_20261023/artifacts/validation-proof-003.json`、`review-003.json`。
- 初轮非标准 runner 调用在 pytest 前因内存上限冲突退出97；第二轮3项在目标配置读取缺个人环境变量处失败，修正测试配置后重验，原件保留。服务级用例沿用既有 seeded history/RecordingDeadline，不称真实60秒验收；产品测试没有 Web UI 或真实外部模型网络，文本注入不代 Windows 实机。大文件拆分为非阻断结构建议；其它错误出口与2.2/7.2总门保持未勾。

### 2026-10-23 Fork 来源 intent 校验错误出口（2.2/7.2 部分收口）

- 远端来源 intent 的字段校验失败保持原错误类别与 cause；普通错误只保冻结 operation ID、来源 Gateway/Workspace、阶段及异常类型，原始畸形输入与异常链仅留诊断日志。不修改来源校验、creation/capture writer、发布次序、deadline 或 schema。
- 固定完整 tree `94233a61f6b2bf27bfa7d03dbfa39a739ec7b4da`：正式 matrix 三项通过（20.31秒），覆盖 POSIX/drive/UNC 文本的结构化原异常、普通错误无原生输入、日志原对象、来源/目标零创建副作用，以及同一 request/key 修正后真实发布、export confirmed、幂等重放与一次 capture；既有默认 main/隐式安全 anchor 回归一项通过（13.73秒），Ruff/compileall通过。旧生产完整 archive 的三项反例均在普通错误的 private/native_path 检查失败（20.63秒）。
- 首轮2通过/1失败由测试依赖 Pydantic 截断后的文本造成，改为 errors() 精确核完整原始输入并保留字段泄漏检查；未放宽产品行为。准确命令、4313项前后源码指纹、raw/SHA、实际退出与进程回收见 `out/tests/temp/fork_intent_validation_20261023/artifacts/validation-proof-002.json`。来源历史复用 seeded Saver 装具，transport 为显式替身；不称 Agent/HTTP/Web/Windows 实机验收，2.2/7.2 总门仍未勾。

### 2026-10-23 Gateway fork wire 校验错误出口（2.2/7.2 部分收口）

- Gateway fork transport 的成功回执解码和已识别 incomplete-capture 错误回执校验只向普通错误提供原 prefix、请求身份、目标 URI、action、阶段与异常类型；畸形原始字段留在原 cause 和日志。不改 HTTP status 归属、connection、request_id 漂移、capture identity 校验或未完成状态恢复合同。
- 完整 tree `d42a107fd51759493022122a76ed13a032200f7e` 的新6项及原48项客户端单测合计54 passed/3.06秒；旧生产完整 archive 六项均在普通错误的 native_path/private 检查失败（1.43秒），涵盖两类回执×POSIX/drive/UNC 文本。修正回执后同一请求的 method/URL/headers/body 完全相同，有效 preparing capture 仍返回原 typed incomplete 异常，不伪造成功；Ruff/compileall通过。
- 首轮Ruff要求显式 fixture 重导出说明，末次仅补4个noqa注释，AST等价复用原测试结果；原失败、准确命令、4314项源码指纹、退出码、日志SHA、无残留进程及限定审查见 `out/tests/temp/gateway_fork_protocol_errors_20261023/artifacts/validation-proof-002.json`。HTTP请求入口是显式stub，不代真实网络/Web/Windows；普通HTTP错误正文与连接异常仍需独立核验，2.2/7.2不勾总项。


### 2026-10-23 附件预览失败的 canonical 与模型输入（2.2/7.2 部分收口）

- 图片/视频预览保持既有 catch 类别及失败后继续发送附件清单的行为，唯一 Builder 错误生成点只提供附件 URI、stage、异常类别及 errno/winerror 数值；原异常及完整预览程序输出仅保留在原始诊断日志。不扩展日志例外，不增加持久诊断副本或第二条附件链路。
- 固定完整 tree `3c2de0bee7358ed1db0e102488a57c006b089477`：2项集成从真实 Session 创建、inline 上传、消息准入、Agent 执行与 model_stream 回放生成 canonical/history/assembly/provider request，核预览失败仍完成、无 image block、正文可读、原日志对象、冷 Saver 字节/身份相等和 request_hash；18项相关单测、Ruff/compileall通过。相同最终测试在旧生产完整 archive 下2项集成均明确检出 canonical 原生路径，5项短单测均拒绝旧原文诊断。
- 001误取未持久化 attachment_diagnostics、002的路径断言误命中系统提示普通词组及import格式失败均保留原件；修正为核真实canonical manifest和精确注入路径，不新增持久字段。4316项源码前后指纹、raw/SHA、退出码、进程回收与只读架构/行为审查见 `out/tests/temp/attachment_preview_errors_20261023/artifacts/validation-proof-003.json`、`review-003.json`。预览 I/O 与程序故障是受控注入，模型走现有异步回放；不代真实外部Provider、HTTP/Gateway/Web或Windows实机，2.2/7.2总门保持未勾。


### 2026-10-23 Gateway 普通上下文回执校验错误（2.2/7.2 部分收口）

- 普通 read/search/inventory 共用的唯一 HTTP 回执校验点将 JSON/envelope/DTO 校验失败归为不可恢复 RuntimeError，普通错误保留 method、API path、request_id、stage和异常类型；完整原始输入保留 cause及诊断日志。request_id漂移仍使用原拒绝；HTTP状态到可恢复/致命分类、联邦错误正文及连接错误出口不在此切片改动。
- 固定完整tree `6ee3bd9866f14aa7d508953f8563ba794c17f325` 的6项新增及80项既有客户端/查询/target resolver单测合计86项通过，Ruff/compileall通过；覆盖read/search/inventory畸形DTO、JSON/envelope与request_id，修正回执后原method/URL/headers/body保持相同。旧生产完整archive三项均在普通错误中检出native_path原始输入，保留POSIX/drive/UNC的精确原cause校验。
- 4317项源码前后指纹、raw/SHA、退出码、无残留进程及范围审查见 `out/tests/temp/gateway_context_protocol_errors_20261023/artifacts/validation-proof-002.json`。请求是显式HTTP stub，不冒充真实网络/Web/Windows；泛HTTP正文和连接错误仍需后续独立收口，2.2/7.2总门未勾。


### 2026-10-23 Gateway 上下文/公开 fork 的传输错误出口（2.2/7.2 部分收口）

- 唯一客户端的连接异常与HTTP失败不再把原始异常/响应message/detail复制到普通错误；连接保留原cause并记录异常日志，HTTP原始有界正文留日志。普通错误保留method/API path/request_id/stage/异常类别或status，明确404目标不可用，并只投影原联邦闭集及resolver/context-revision/fork-capture具名owner码。未改变共享可恢复状态集合、异常类别、fork incomplete严格身份校验或release uncertain outcome的pending责任，不新增兼容异常类或第二错误协议。
- 完整tree `ff3cab98e874a71ca80c4bb61c2f31135b58f1ef`：30项新增与既有客户端/上下文查询/target resolver/fork service/API合计176 passed/4.60秒，Ruff/compileall exit0。旧生产与相同最终测试完整archive为30 failed/3.46秒，均明确检出普通错误原生路径；覆盖POSIX/drive/UNC连接异常及read/fork/inventory的404、409、500、503、redirect、非JSON和畸形码。核连接重试原method/URL/headers/body、原cause及原日志、HTTP分类和专用fork拒绝边界。
- 4318项源码前后指纹、raw/SHA、退出码、无残留进程、独立只读审查及strict见 `out/tests/temp/gateway_context_transport_errors_20261023/artifacts/validation-proof-001.json`、`review-001.json`。请求为显式HTTP stub，不代真实网络/Web/Windows。通信client及其它普通响应出口另按原合同继续审计，2.2/7.2总门未勾。

### 2026-10-23 Gateway 通信 HTTP 错误出口（2.2/7.2 部分收口）

- discovery/send/read/wait 共用的客户端不再向普通错误复制 HTTP 原始正文；保留原 WorkspaceSessionContextAccessError 分类、HTTP status、API path、request_id 与登记过的联邦拒绝码。read/wait 的六类 observation 拒绝仍抛原 RemoteObservationError 和精确 code；原始正文只留有界诊断日志。标为 JSON 但正文损坏的拒绝继续按原 HTTP 状态分类，不以解码错误覆盖它；连接异常和成功回执合同不在本切片变更。
- 固定完整 tree `4ef022a31e9b4f35bfeeb9eb4ab7d4cde1697c25`：131项客户端、50项 observation 消费方通过，Ruff/compileall exit0；覆盖四入口的404/409/500/503/502、非JSON、畸形码/正文与read/wait全部观察拒绝，失败后原URL/headers/JSON/timeout逐字段相同。旧生产完整 archive 配相同最终测试36项失败。首轮测试深拷贝HTTPX默认哨兵造成比较失败，修正为实际请求字段后集中重验，旧原件保留。
- 4350项源码前后指纹、准确命令/raw/退出码与审查见 `out/tests/temp/gateway_communication_http_errors_20261023/artifacts/`。这些是HTTP stub及真实SQLite观察消费者验证，不代真实网络、完整Web或Windows实机；2.2/7.2总门继续未勾。


### 2026-10-23 通信 discovery 回执校验错误（2.2/7.2 部分收口）

- 成功 HTTP 回执的目标地址与 main pointer 在原 discovery 解码点校验，错误只公开 API path、request_id、阶段和异常类别；保留原内置异常类型或 CommunicationContractError/原code，完整原异常留cause与诊断日志。没有改变目标身份规则、HTTP分类、请求/响应身份校验或持久化。
- 固定完整tree `7d5cd4abdc869b06b571146745d3429f09fde269` 的新增12项通过，覆盖未知目标字段、Session/Thread非法值及main pointer漂移×POSIX/drive/UNC，核精确cause对象、原code、日志及同请求修正后重试。相同生产的上一候选客户端/通信合同/观察回归221项通过，另12项中10通过、2因TypeError原文与repr转义比较错误失败；最终只修该cause断言并补验全部12项，不将混合运行改为全绿。真实Gateway/Backend/Agent与model_stream的C1 send→worker→wait→read为1 passed/53.73秒，Ruff最终exit0；旧生产完整archive的12项均检出普通错误原生路径。
- 首次回归因HEAD测试误导入尚未提交的support模块新位置而收集失败，私有候选仅对齐实际HEAD平面support，保留共享树其它代理的搬迁；Ruff首次import格式及所有失败原件保留。准确命令、全树前后指纹、raw/退出码及只读独审见 `out/tests/temp/communication_discovery_errors_20261024/artifacts/`。真实C1验证正常发现与后继执行，畸形回执用显式HTTP stub；不代真实外部Provider、完整Web或Windows实机，2.2/7.2继续未勾。


### 2026-10-23 冷观察失败记录与普通 HTTP 错误（2.2/7.2 部分收口）

- source/target 两处观察失败统一交给既有 observation_persistence owner，在原 Session gate、page/root fencing 与 lease 结算边界提交；删除 service 内旧方法。owner_failure 只持久异常类型及可用整数系统码，不再保存原异常文本，完整异常保留本机日志。普通观察 API 的 Runtime/Value/Key 仍为409，OSError/SQLite仍为500；保留原500的code/message/data/request_id信封及同一X-Request-ID，专门观察拒绝仍保持原闭集和409。
- 固定完整 tree `f2ecd17c84c7105aa9d0ec5afc8e4e9bff750599` 最终定向24 passed/11.63秒；其中15项覆盖POSIX/drive/UNC×Runtime/Value/Key/OSError/SQLite，经真实catalog/Session/control准入和ASGI路由核HTTP、持久事实、数字码、原异常日志对象、冷owner重开同记录/lease、失败重放不重复读取及修复后新读取。相同存储/恢复代码的上一候选87 passed、1 deselected，另补选真实Gateway/Backend HTTP lifecycle为1 passed/35.34秒；最终仅500投影与精确响应断言变化，已定向补验。Ruff最终exit0，仅忽略messages.py原有15条B008，Depends默认值AST与HEAD相同。
- 旧生产完整archive的15项负例全失败；早期候选漏捕500、初次静态import错误及004缺少.env导致恢复读取失败均保留，不计通过。005只补接既有.env后同源码通过。准确命令、4322项源码前后指纹、raw/退出码、真实HTTP原件SHA与只读补审见 `out/tests/temp/observation_failure_records_20261023/artifacts/`。故障矩阵使用受控projection故障和本地授权override，不代完整外部Gateway/Web/Windows；真实HTTP单测补验正常生命周期，不声称覆盖真实网络故障。2.2/7.2总门保持未勾。


### 2026-10-23 通信准入失败事实与原 inbox 恢复（2.2/7.2 部分收口）

- 唯一InboxAdmissionWorker在binding失败时仅向last_error及上层异常提交Session/communication身份、阶段、异常类型与整数系统码，完整原异常由本机日志保留。failure-record自身的Runtime/Key/OSError/SQLite失败明确报告未提交，只暴露阶段和类型，不再拼接任意异常正文，也不虚称last_error已记录；取消固定文本、claim/fencing/CAS及原lease规则保持。store仅更新职责注释，未改结构或写入协议。worker的纯类型ledger依赖移入TYPE_CHECKING，修复单独冷导入循环，无运行时shim。
- 完整tree `57427b919f81e48af6ca777c4f96e82052bd4833` 的45项新增矩阵与相邻worker/生命周期用例合计75 passed、1 deselected/19.41秒；真实Gateway/Backend/Agent、model_stream的C1 send→worker→wait→read为1 passed/53.32秒，Ruff exit0。故障矩阵经正式send/accept/worker准入生成关键inbox/lease状态，覆盖POSIX/drive/UNC×Runtime/OSError/SQLite绑定故障×正常记录及Runtime/Key/OSError/SQLite记录故障；核持久安全事实、两类原异常日志对象、上层RuntimeError→InboxAdmissionError，以及冷owner重开后同communication/admission/hash/wakeup/thread/request、递增claim generation、原lease完成与唯一binding。
- 旧行为完整archive的27项原始矩阵全部因原生路径进入公开错误失败；最终45项反例的实际原因与退出码保留在red-008。首次red-001因既存冷导入循环收集失败、static-004空行及无用noqa失败，均未计通过；green-006已针对扩展的真实存储失败完成验证。准确命令、4323项源码前后指纹、raw/退出码、产品原件SHA及只读独审见 `out/tests/temp/inbox_admission_failures_20261024/artifacts/`。故障恢复使用受控typed binder、非真实Job执行故障；独立C1验证真实执行正常链路，不声称完整Web、外部Provider或Windows实机。2.2/7.2总门仍未勾。


### 2026-10-24 通信目标回执校验与原发送重试（2.2/7.2 部分收口）

- 唯一send saga在原目标回执解码点仅公开源Session/Thread、communication、request身份、阶段和异常类别，完整校验异常与畸形输入留cause和诊断日志。Pydantic校验保持ValueError类别，CommunicationContractError保持原code；后继receipt/outbox身份核对、routing/settling与不确定结果重试不变。业务admission改为直接引用唯一domain DeliveryPolicy，修冷导入循环，仍为同一运行时类型。无新schema或兼容链路。
- 固定完整tree `75de1e4a785ba77252abfdb99ccbcbb6b7653733` 的新增12项通过（6.24秒）；覆盖四种畸形回执×POSIX/drive/UNC，经真实send/accept/worker/control建立关键状态，核原异常日志、源outbox仍routing且无receipt、lease仍settling、修正后相同请求与原accepted_at/communication/admission和唯一target binding。旧生产配同一导入修复的完整archive为12 failed/6.17秒，全部检出普通错误的原生路径。相邻回归141 passed、1 failed、1 deselected/20.70秒；唯一失败是已有exact-wire fixture漏terminal_replay=False，现行schema与生产重放早已使用该字段。同树只补齐fixture的 `05e0bdce081732b370eb3d64047db34dc35abc7a` 定向15 passed/0.23秒，未弱化精确相等断言。Ruff、四独立进程冷导入、domain类型同一性与compileall通过。
- 真实Gateway/Backend/Agent与model_stream的C1 send→worker→wait→read为1 passed/53.43秒；故障矩阵为受控本地transport/binder，C1仅验证正常真实HTTP链路。原red-001冷导入collection失败保留且不计行为证据。准确命令、4323项源码前后指纹、raw/退出码、无残留进程及两次独立只读审查见 `out/tests/temp/communication_receipt_validation_20261023/artifacts/`。不代完整Web、外部Provider或Windows实机；2.2/7.2总门保持未勾。


### 2026-10-24 Gateway 通信连接与启动恢复诊断（2.2/7.2 部分收口）

- 唯一通信client的连接异常保留HTTPX具体类别、原request及cause；普通错误仅提供API path/request_id/stage/error_type，原异常留诊断日志。startup只重试原有ConnectError/ConnectTimeout，沿用health与精确源backend的同一总deadline；超时仍RuntimeError，非法backend状态仍立即失败，HTTP拒绝仍原HTTPStatusError及request/response。原生services字段/非法状态/连接异常只留诊断，不复制到普通错误。
- 固定完整tree `f9cafdac460734bedfe4473958c69253eb701b20` 的Gateway客户端全目录212 passed/12.19秒，Ruff exit0；覆盖discovery/send/read/wait×三类连接异常×POSIX/drive/UNC，核类型、原cause/request、原日志及修复后请求逐字段一致；startup分别在health/backend验证两类可重试及ReadTimeout立即失败、offline/非法状态、HTTP拒绝不重试及瞬时连接恢复。可控clock只注入被测模块的原fixture，health消耗0.75秒后backend预算0.25/0.15/0.05，总deadline仍1秒。最终旧生产完整archive的60项反例全failed/3.65秒，明确拒绝普通错误原生路径。
- 相同生产代码的green-003真实Gateway/Backend/Agent与model_stream source-outbox崩溃窗口1 passed/58.92秒，生成真实outbox后硬杀，正常startup等待与恢复沿用原communication/lease/preimage并完成唯一投递；compileall通过。green-001的17项短预算失败、green-003的84项跨模块fixture依赖错误及早期red均保留，不冒充行为反例；最终fixture自包含修复后全目录通过。准确命令、4323项源码前后指纹、raw/退出码、进程回收与独立审查见 `out/tests/temp/communication_transport_errors_20261024/artifacts/`。畸形诊断矩阵仍是显式HTTP stub，真实进程验收不覆盖真实网络故障、完整Web、Windows或外部Provider；2.2/7.2总门保持未勾。


### 2026-10-24 联邦 Workspace HTTP 错误与请求身份（2.2/7.2 部分收口）

- 本地 cold catalog 与联邦工作区 HTTP port 不再将原始响应或连接异常文本复制到普通错误；公开错误保留请求身份、API path、阶段、状态及异常类型，完整原文留原始诊断日志。连接投影保留原具体异常类型、可选 request 和 cause，不访问未初始化的 request getter。所有 HTTP 错误先核对正文/响应头 request_id，再映射 fork、导航、resolver、revision 或 observation 的原 owner 分类；错配或缺失身份只报工作区不可用，不接纳具名拒绝。
- 固定完整 tree `7914441d2b8b65d7caebfcaa34f05ed82f61081f`：431项定向回归、Ruff、compileall通过；最终候选真实三 Gateway/SSH/WebSocket 反向通道问答1 passed/107.38秒。合法身份保留原拒绝码，body/header漂移及body缺失无法进入这些语义分支。旧生产完整archive的102项路径反例及42项无request连接投影反例均明确失败，原件保留。
- 4324项源码前后相同、准确命令/raw/退出码、无残留进程与独立审查见 `out/tests/temp/federation_workspace_http_errors_20261024/artifacts/validation-proof-007.json`、`review-007.json`。定向错误验证使用HTTP stub，真实C3验证正常产品流程；不代完整Web、Windows或全部普通响应审计，2.2/7.2总门仍未勾。


### 2026-10-24 联邦下游客户端错误身份（2.2/7.2 部分收口）

- 通信与上下文客户端仅在错误正文 request_id 与 X-Request-ID 均匹配本次请求时解释已登记业务码；缺失/漂移身份不接纳远端 owner 语义，保持原 HTTP status 分类。未绑定请求身份的 inventory 不提取业务码；有效身份沿用原拒绝语义，原始正文仍仅留诊断日志。
- 固定完整 tree `1312da3c7f6da6bfc50ea131ebe8d810bb39c939`：491项定向回归/18.31秒、Ruff、compileall通过；真实三 Gateway/SSH/WebSocket 反向通道问答1 passed/106.95秒。旧生产完整archive的身份反例50 failed、10 passed，准确区分旧实现无身份即采信业务码。早期导入错误收据保留，不计行为证据。
- 4324项源码前后相同、准确命令/raw/退出码、无残留进程与独立审查见 `out/tests/temp/gateway_error_identity_20261024/artifacts/validation-proof-002.json`。错误矩阵使用HTTP stub，真实C3验证正常产品流程；不代完整Web、Windows或全部路径不变量审计，2.2/7.2总门仍未勾。


### 2026-10-24 用户 profile 原生存储错误（2.2/7.2 部分收口）

- UserProfileStore 唯一 owner 为 initialize/read/write/theme-assets/delete 的 OS 故障生成具名操作、user_id、原异常类别与 errno，保留原异常类别/cause及完整诊断日志，普通错误不携带原路径。读取统一至 read_profile，移除仅接收Path的旧私有读取入口；非对象/版本错误使用user_id，JSON/UTF-8失败报类型及位置，损坏内容不重写。原子写入、首用户主题初始化、创建账户回滚与先删账户后删profile的顺序不变。
- 固定完整tree `5a570c8edf82219608d5121325fa3295be153028`：152项定向回归/8.98秒，真实独立Gateway故障与恢复1 passed/21.19秒；39项旧生产完整archive反例全部失败/4.65秒。覆盖POSIX/drive/UNC故障诊断、回滚、原字节与临时文件、配置缺失/损坏、四类设置/主题入口、lease阻止删除及原删除顺序。compileall通过；Ruff exit1的五项TRY004逐项与HEAD基线相同，无新增，保留原ValueError以维持create捕获及回滚合同。
- 准确命令、4326项源码前后指纹、raw/真实退出码、进程回收、基线对照与只读独审见 `out/tests/temp/gateway_profile_errors_20261024/artifacts/validation-proof-001.json`。权限故障为unit注入，产品用例使用真实磁盘损坏/缺失/错误类型；不代真实ACL、Windows、完整Web或全局不变量审计，2.2/7.2总门未勾。


### 2026-10-24 Gateway 主题图片 owner 错误与清单校验（2.2/7.2 部分收口）

- 图片owner统一投影manifest/image/local-image的读写删除故障，保留原OS异常类别/cause与完整原始诊断，普通错误只含操作、已验证asset身份、异常类别及errno。清单读取一次校验身份/摘要/文件名与全部消费元数据，TypedDict记录进入DTO时不再用str/int隐式转换；损坏数据明确失败且不重写。原内容寻址、上传去重、API URL及各路由既有状态分类不变。普通主题file:背景URL仅报scheme，原URL留具名诊断日志，不套用Browser页面例外。
- 固定完整tree `8bd8fa8c0adae6260cf39289f7db78271dd399ee`：173项定向/5.59秒、真实独立Gateway上传→损坏→恢复→同内容去重→删除1 passed/21.48秒，Ruff及compileall全部exit0。最终旧生产完整archive的52项反例失败、11项通过，覆盖POSIX/drive/UNC存储异常与清单字段污染。审查发现的size等元数据遗漏已修并补入同一产品入口矩阵。首轮产品误期望DELETE409，按原route仅捕获KeyError的实际500合同校正测试，无产品状态分类变更；全部旧收据保留。
- 准确命令、4329项源码前后指纹、raw/真实退出码、进程回收及只读独审见 `out/tests/temp/gateway_theme_errors_20261024/artifacts/validation-proof-004.json`。权限故障为unit注入，产品损坏来自真实磁盘；原始日志适用已登记管理入口例外。未验完整Web、Windows及全局配置持久引用，2.2/7.2总门未勾。


### 2026-10-24 Gateway 工作区导航存储错误

- 导航 owner 统一处理最终读写、JSON/UTF-8、typed节点与关系校验失败，普通错误只报告固定artifact、规则/位置及系统错误事实，不复制物理位置或损坏值；原生异常类别、errno、完整cause与诊断日志保留。原子发布仍由原writer负责，发布后清理失败保留实际新状态；不新增导航队列、schema或兼容reader。
- 固定完整tree `fabd9a80f9c6ffafc691af9ad06fff22f12ea555` 的新增及既有导航/catalog回归26 passed/31.53秒，真实Gateway HTTP 1 passed/21.30秒，Ruff/compileall均exit0。产品入口创建目录后分别损坏对象、nodes、JSON/UTF8及kind/position/parent，GET与POST保持原500/400归属、request_id、原坏字节不变；恢复原件后同revision可读并继续更新。旧完整实现unit反例12 failed/2 passed，真实HTTP反例在首个GET精确捕获物理路径。
- 准确命令、4331项源码前后指纹、raw、实际退出码、零进程残留与独立只读审查见 `out/tests/temp/gateway_navigation_errors_20261024/artifacts/validation-proof-001.json`、`validation-proof-http-red-001.json`及`review-001.json`。I/O注入与发布后错误由unit覆盖；未验Windows实机或完整Web，不据此关闭2.2/7.2全局门。


### 2026-10-24 普通文件正文与模型文本修改的写锁错误（2.2/7.2 部分收口）

- 唯一文件写锁 owner 逐阶段处理 prepare/open/acquire/release/close 的原生 I/O；公开错误只含逻辑 artifact、阶段、异常类型、errno/winerror，完整原异常与真实位置保留在管理诊断及原因链。OSError 保原类型与 errno，portalocker 非竞争错误转换为显式 RuntimeError 供原文本执行链收口。AlreadyLocked 沿原有等待和超时合同处理，临界区业务异常不被重写，不新增第二锁或文件写入链。
- 固定完整 tree `d49f713f1c29b7b5837816c12ab7262d9fd81e7f` 的 `out/tests/temp/file_mutation_lock_errors_20261024/artifacts/runs/unit-003/` 为94 passed/2.69秒、exit0，覆盖POSIX/盘符/UNC异常正文、有无filename、五阶段、原异常日志对象、文件及本地锁释放、原锁超时和模型apply_patch真实入口。释放/关闭失败仍保已发布文件及 completed effect，不伪报未执行；业务异常保持原对象。
- `http-env-003/` 为真实独立后端HTTP 1 passed/12.79秒、exit0：通过磁盘目录阻塞锁文件打开，正文PUT保400与request_id、无真实路径且原文件不变；修复锁位置后，同一URI和原revision成功写入。首次 `http-003/` 仅因归档树未接现有.env导致必需秘密引用解析失败，补接已有环境后同源码通过，不计首轮为行为证据。`static-003/` Ruff exit0；只读独审 `artifacts/review-003.json` 无可行动缺陷。
- 同一完整归档恢复旧锁实现的 `red-003/` exit1，区分原始路径/原因及模型工具结果错误；原件、准确命令与源码指纹保留。早期unit-002因新用例漏声明补丁末尾空行而有1项字节预期失败，已修正测试输入明确生成末尾换行；static-002的with语法检查已修，未改变产品补丁语义。上述故障注入不代Windows实机、完整Gateway/Web或2.2/7.2总门，两项仍保持未勾；最终提交相对受测候选仅新增此证据。

### 2026-10-24 MCP 目录失败诊断（2.2/7.2 部分收口）

- 唯一目录 owner 删除异常正文截断链路，启动、显式刷新、通知刷新统一发布已校验 server ID、阶段、异常树类型及 errno/winerror/RPC code/HTTP status 数值事实；原始异常、cause 与完整堆栈保留在诊断日志。普通 API 503、owner 最近错误和 Registry unavailable 复用同一错误值，不新增 writer 或解析器；失败保持整个旧有效目录、binding、revision 与成员，恢复成功清除错误。
- 固定完整 tree `0599daee63368474967b5f6bb6fc450be4c2661d` 的 `unit-002` 为51 passed/13.74秒，包含POSIX、盘符、UNC、扩展UNC的显式/通知刷新，启动OS/RPC/HTTP/异常组、原cause/log、目录原子性及恢复。真实独立后端 HTTP→本地 stdio MCP 的 `http-002` 为1 passed/14.07秒：四种协议错误正文/data不进入503，request_id一致，原文在后端日志、协议原件可核，恢复后同目录可读；没有模型替身或外部模型请求。旧实现 `unit-red-001` 的8项全部失败，完整候选仅恢复旧owner的 `http-red-004` 在首个协议故障精确复现真实路径外泄。
- 最终源码相对受测 tree 仅调整测试import排版；`static-003` Ruff与`compile-003`均exit0。owner为802行，按1.6登记完整文件清单、AST符号/职责、import graph及目录计数：目录7个Python文件，仍为单一目录状态机，无新增循环；非作者结构复核未发现需在本切片强拆的职责混杂，后续增长应重新评估纯错误投影/session adapter边界。
- 准确命令、前后源码指纹、raw/真实退出码、零进程残留、协议/HTTP/原始日志及SHA清单见 `out/tests/temp/mcp_catalog_failures_20261024/artifacts/validation-proof-003.json`、`architecture-003.json`与`review-002.json`。首次static-002仅报新增import排版，已修正；unit反例的首次runner调用因隔离BOXTEAM_HOME目录未创建而未启动测试，补建任务目录后才运行。此切片不代Windows实机、完整Gateway/Web或2.2/7.2全局审计，两项继续未勾。


### 2026-10-24 Browser脚本原生I/O错误出口（2.2/7.2部分收口）

- 真实脚本通过Playwright保存截图时，Node原生系统错误携带errno/syscall及文件位置；执行owner在队列写入last_operation之前按此结构发布browser/page/run_code阶段与原code/status，完整错误和位置只留诊断/cause。不按自由正文或路径形状猜测；普通脚本Error、带业务code的Error、SyntaxError与字符串throw保持原合同，超时和页面file: URL例外不变。
- 完整HEAD archive上的真实Chromium红测http-red-001明确复现ENOTDIR路径进入HTTP与last_operation。最终代码候选006（tree `8a47054675f1676a87dafc090940454cbeed4f95`）unit-006为133 passed/888断言，http-006为3 passed/20.40秒：覆盖sync/async真实HTTP、资源API/request_id、Node记录/GET投影无原生位置、verified和lease释放、原件不变、业务错误原文及显式重试；并复验本地file页面导航/检查点/恢复和工具超时后立即继续动作。Ruff及Node语法检查均exit0。
- 004只读独审发现初版包装所有脚本错误的P2，006已收窄并新增反向回归，复核无新增finding。static-003的新测试循环变量引用和unit-005的JS引擎语法文案假定均已修正，失败原件保留。准确命令、退出码、源码前后指纹、零进程组残留及产品原件在 `out/tests/temp/browser_script_errors_20261024/artifacts/`（mechanical-006.json、product-evidence-006/及SHA清单）；不把Linux原生错误与词法Windows用例称为Windows实机通过，不代替完整Gateway/Web或2.2/7.2全局门。


### 2026-10-24 Gateway 受管进程日志错误（2.2/7.2 部分收口）

- 唯一 ProcessLogStore 在最终日志目录创建、权限设置、轮转及文件打开处保留系统异常类别、errno、可用的 winerror 和原 cause；公开错误只含逻辑日志名与阶段。非法文件名、日志与轮转符号链接明确拒绝，原输入和完整文件位置只进原始诊断日志。所有本地 Backend、Terminal/Browser 和 SSH 隧道继续复用同一日志 owner，不在注册表或路由增加脱敏与第二写入链。
- 完整源码 tree `d8de0c4a98905484bb3c947355010a870a9d588b` 的日志、Workspace/SSH 进程单测 38 passed/0.51 秒；真实独立 Gateway 与 Workspace HTTP 验收 2 passed/57.26 秒。产品注册入口由真实日志目录阻塞触发 FileExistsError，注册返回 500、request_id 一致；普通列表、健康检查与 SQLite connection_error 不复制原生位置，Gateway 重启后仍明确离线，诊断原文与原阻塞字节保留。既有工作区权限拒绝及重启验收同时通过。
- Ruff、compileall 通过；准确命令、完整源码前后指纹、原始输出和实际退出码保留于 `out/tests/temp/gateway_log_errors_20261024/artifacts/`。unit-002 因测试把轮转目录放在会先被合法移走的 .1 而未制造故障，改在 .3 阻塞 .2 后 unit-003 通过，未改轮转规则。http-001 在装具选择 Python 阶段发现候选缺少 .venv 链接，接入已有依赖后同源码 http-002 通过。以上不代替 Windows 实机、完整 Web、其它运行时启动边界或 2.2/7.2 全局门；Agent 团队与协作继续按用户裁定保留 TODO。


### 2026-10-24 Gateway 原生进程创建错误（2.2/7.2 部分收口）

- 统一 `_spawn_logged_process` 在 Popen 的原生系统失败处发布阶段、系统错误类型及 errno/可用的 winerror，保留原异常 cause 和完整诊断。日志只额外记录可执行文件与 cwd，不复制命令参数或环境；启动失败仍关闭原日志句柄，关闭再次失败明确记录并附系统事实，不能覆盖首个启动错误。非系统异常保留原对象和传播方式；本地 Backend、Terminal/Browser 与 SSH 隧道仍共用原创建链，注册表/路由没有新增异常文本脱敏层。
- 最终代码候选 tree `aaa7cf6537d1b2e34023e9bf4c453e0562d22ff9` 的日志、进程关闭、Workspace/SSH 启动单测 55 passed/0.71 秒；Ruff、compileall 通过。POSIX/盘符/UNC 字面量、有无 filename、系统/非系统启动错误及日志二次关闭失败覆盖类型、errno、原异常对象、日志记录、句柄关闭与安全错误注记；这些不代替 Windows 实机。
- 生产代码与上述相同的完整 tree `0ef894b2d1de29b4f2c87cf3e6331d28e446442f` 的真实 Gateway/Workspace HTTP 为 3 passed/84.23 秒，含新增的目标 Node 可执行文件缺失：经普通注册入口实际调用原生 Popen，500 与 request_id 保持一致，列表/健康投影及 SQLite connection_error 没有真实位置，Gateway 重启后明确离线，完整位置仍在诊断日志。两个 tree 仅差 unit-001 中 Windows filename 的日志断言：Python 按 repr 转义反斜杠，unit-002 改为核对完整原异常文本，不改变产品源码或 HTTP 用例。
- 完整 Git archive 的反例 tree `9ddce203f1fe724777df9ab6c5992869a8800de9` 仅恢复旧进程 owner，`http-red-001` 在注册 500 响应中精确捕获可执行文件路径，1 failed/20.73 秒，证明新验收能区分原缺陷。准确命令、完整源码前后指纹、原始输出、实际退出码和进程回收记录在 `out/tests/temp/gateway_spawn_errors_20261024/artifacts/`。未验 Windows 实机和完整 Web，不关闭 2.2/7.2 全局门；团队与协作专项继续 TODO。


### 2026-10-24 远程 Gateway 响应 envelope 错误（2.2/7.2 部分收口）

- 联邦发现和管理调用继续共用 federation 唯一响应解析。成功状态的 JSON 缺少 data 对象时，原 RuntimeError 类型和路由归属保留，普通错误只报告协议事实与 HTTP 状态；完整正文与请求 ID 仅进入原始诊断日志，不新增下游文本替换或兼容解析。正常 data、HTTP 拒绝及 JSON 解码错误保持既有合同，具名联邦原始日志仍完整传递。
- 代码 tree `3460b200f96ed1974740a82006569f87c6844e0e` 的真实独立 Gateway 与独立 HTTP 替身为 1 passed/27.60 秒：三类错误 envelope 经普通目录入口返回502、不复制POSIX/盘符/UNC路径且request_id贯通；远端恢复后目录URI可读，用户主动打开的联邦诊断日志保留原文。装具仅预置已连接远端和凭据，不冒充SSH配对或完整联邦E2E。
- 同一生产实现和单测的 tree `2aeb3fa180414fdb37535c89f3c1262bec32e729` 定向单测36 passed/1.05秒，覆盖两类发现响应、管理响应、六类无效envelope、完整诊断正文、HTTP409结构化拒绝、原JSON解码异常和正常响应。最终tree仅改真实HTTP装具的Gateway身份与UUIDv7目录ID格式；源码指纹证明生产及上述单测未变。compileall通过；Ruff在保留原RuntimeError合同、显式排除TRY004后通过。未排除规则时，原HEAD与候选均有同四处TRY004；不为静态偏好改变公开错误类型。
- 完整Git archive反例 tree `d65ae7ce9a0984db38bfacfb45e7c82982994d64` 只恢复旧响应owner，真实目录响应精确回显路径，1 failed/27.70秒，证明新增场景能区分原缺陷。扩大单测中联邦列表HTTP替身不接受既有trust_env参数的失败，在原owner完整归档也复现；不改无关测试预期。http-001装具占位凭据被真实channel owner拒绝，http-002的恢复装具使用带连字符目录UUID被真实grammar拒绝；均有原始证据，产品实现未因此改变。
- 准确命令、完整源码前后指纹、实际退出码、零进程组残留和HTTP/诊断原件在 `out/tests/temp/gateway_federation_envelope_20261024/artifacts/`。此切片不覆盖其它远端错误/DTO、Windows实机或完整Web，不关闭2.2/7.2全局门；Agent团队与协作继续TODO。


### 2026-10-24 Workspace 生命周期响应 envelope 错误（2.2/7.2 部分收口）

- Gateway runtime controller 继续为本机生命周期查询、drain、配置重启契约及远端生命周期调用使用原唯一响应解析。成功状态的JSON缺少data对象时，原TypeError与HTTP500归属不变，普通错误只报告协议事实与HTTP状态；完整正文和request_id保留原始诊断。没有复制联邦管理的RuntimeError合同，也没有新增通用兼容解析、下游脱敏或异常吞噬。
- 完整代码tree `aa381eeaaba12fe002496863fc4b3959184bed23` 的两项真实Gateway/独立HTTP替身验收为2 passed/54.27秒：远端start/stop/restart-safe/restart-force的三类无效envelope共12次请求，响应无POSIX/盘符/UNC路径，保留TypeError/500及贯通的request_id，错误后Gateway仍响应；前一切片的目录恢复与具名原始诊断日志同时回归通过。该装具只预置连接、凭据与远端投影，不替代真实SSH或完整远端Backend生命周期验收。
- 同一生产实现及单测的tree `4539f909aa65afbce59ea5f8c3517a3587fa4fd9` 为174 passed/9.54秒，覆盖状态、动作、pending状态、retry、startup-contract、身份错误及已有完整Backend生命周期场景；新增场景验证六类无效envelope、修复响应后的同请求重试、完整原始诊断、HTTP409结构化拒绝和原JSON解码异常。最终代码tree仅补齐HTTP装具的connection_id，生产与单测源码指纹相同。Ruff不排除规则、compileall均通过。
- 完整Git archive反例tree `91e3d10d0f46cdca9bdba794fdd3933672b470a4` 只恢复旧controller，真实start入口的普通500响应精确回显路径，1 failed/28.27秒。http-001在装具投影缺少connection_id时被真实registry拒绝、未进入HTTP断言；补齐后通过，产品实现和校验均未改变。
- 准确命令、4383项源码前后指纹、原始输出、实际退出码、零进程组残留及HTTP原件保留于 `out/tests/temp/gateway_runtime_envelope_20261024/artifacts/`。不代替其它响应字段、Windows实机、完整Web或2.2/7.2全局验收；Agent团队与协作继续TODO。


### 2026-10-24 Browser 页面摘要采集错误（2.2/7.2 部分收口）

- BrowserSession 在页面摘要采集边界复用既有 browserRuntimeFailure，公开错误与操作记录保留 browser/page 身份、read_summary 阶段和原 code/status；原生异常只进入诊断及 cause。BrowserToolTimeoutError 原对象继续传播，保持既有 timeout/retry/recovery，不修改用户脚本错误、操作可见性、正式 failed 回执或资源核实规则。
- 完整候选 tree `484f542b3e0b73534ca016cba9f878c421f6282d` 的真实 Chromium、sync/async HTTP、工作区 API 与 readPage 工具验收为3 passed（含既有本地 file 页面检查点/释放/恢复和超时后继续操作）。原生采集故障由脚本入口注入，关键资源与操作状态由产品入口生成；验证失败回执、request_id、verified、lease 释放、显式重试以及恢复记录没有原生位置。read 为非可见操作，使用原有策略写入入口保存当前恢复事实，不把内存观察误作立即落盘。
- 同一生产实现和单测的 tree `c1f8fccac581ed38e2916d47d53e1c6db0f86862` 为136 passed/918断言，覆盖POSIX/盘符/UNC字面量、原cause/诊断、队列后续重试、页面file URL以及原有超时恢复和普通脚本错误。最终代码tree只修正新增集成测试的同步payload参数、非可见操作持久化触发和DTO metadata读取。Node语法、Python Ruff/AST及差异检查均通过；未改Web源码，不重复Web构建。
- `red-001` 在旧生产实现上3项失败，显示原生异常直接进入读取失败；`integration-002` 缺少归档外本机.env，接入现有配置后继续；`integration-003` 暴露上述测试调用与落盘假定错误，按既有合同修正，失败原件均保留。`static-002` 仅因运行器将Git index隔离到/dev/null而失败，后续只读差异检查显式使用候选index。准确命令、4384项源码前后指纹、实际退出码、零进程组残留及产品原件见 `out/tests/temp/browser_operation_errors_20261024/artifacts/`。结构自查无新增职责或owner，既有大文件拆分风险仍在；不称为独立代理审查，不代替真实Windows、完整Gateway/Web或2.2/7.2全局门。Agent团队与协作继续TODO。


### 2026-10-25 消息流持久读取错误（2.2/7.2 部分收口）

- events、state、index、applied-event-id 继续由原存储 owner 读取；普通错误只含稳定 Session/stream 身份、具名 artifact、字段与错误事实，物理路径和损坏原文留在原诊断日志，OS/解码原异常保留 cause。移除快照重复读取，校验入口及所有调用方使用明确身份；全部恢复依赖校验成功后才发布 state/event-id 缓存，避免首次拒绝后第二次绕过损坏。既有日志尾部修复、身份/连续序号拒绝、裁剪和幂等合同保留。
- 候选定向单测113 passed/9.84秒（新增40项损坏/OS读取故障及原存储73项），合同/投影/运行时51 passed/1.94秒；旧完整基线加新增故障测试40 failed/3.25秒，证明原实现不满足公开错误及失败缓存合同。真实 Gateway/Backend/model_stream 产品验收1 passed/82.29秒：产品创建并执行四份历史，硬重启后对四类文件依次注入损坏，snapshot/events/SSE建流十二次请求均精确404、request_id一致、原件及canonical不变，恢复原件后可重开且再次硬重启历史一致。未手填执行或canonical状态；复用通信测试仅为产品进程装具。
- Ruff/compileall通过，限定只读审查未发现阻断问题；审查收据误称旧存储测试无改动，实际仅为一个直接调用补齐两个必填身份参数，未改既有断言。早期装具的缺目录、类型导入、日志转义断言及合同命令引用候选基线不存在的测试文件均已归因，原始失败保留。准确命令、源码前后指纹、raw和退出码在 `out/tests/temp/message_stream_read_errors_20261025/artifacts/`；profile002补齐新增单测指纹。合并共享迭代器版本的验证另记本节，不把候选通过冒称共享版本通过。2.2/7.2总门仍未勾，Windows实机及完整Web未验，Agent团队与协作继续TODO。
- 共享源码合并保留既有 `_iter_records`/deque 流式裁剪及测试 fixture 重构；错误处理迁入同一迭代器，列表读取仅消费它。共享版本 `shared-unit-001` 为165 passed/11.06秒，`shared-http-001` 为1 passed/74.33秒，Ruff/compileall与严格OpenSpec校验45 passed/0 failed均通过。共享暂存条目独立核对且未改写；本切片提交不包含他人的优化。


### 2026-10-25 消息流幂等记录归属（2.2/7.2 部分收口）

- 原 applied-event-id 只验证值是对象，会接受其它 Session/Thread/Turn/stream 的事件，且 retained 记录可掩盖损坏。现复用事件日志唯一校验并核索引键与 event_id；恢复、裁剪、显式追加的全部调用方传递明确Thread/Turn归属。追加前读取并验证已有幂等事实，避免校验失败前先落新事件；原错误身份/原件/cause政策、裁剪后的幂等重放与修复后原owner重开保留。未引入第二事件历史、writer或迁移。
- 候选存储单测134 passed/11.14秒（新增21项：10类损坏×裁剪前后及warm追加拒绝），真实 Gateway/Backend/model_stream 1 passed/58.57秒：产品执行生成历史，硬重启后注入另一Thread归属，snapshot/events/SSE建流连续三次404且request_id一致，原件/canonical不变，修复后同owner和再次重启均读回完整原历史。Ruff通过。完整基线Git archive反例的21项单测均失败；同轮HTTP在9500被另一候选测试占用时setup报错，不计产品反例；待其退出后，完整archive的HTTP反例1 failed/45.12秒，精确复现损坏归属返回200。
- 原始命令、完整源码前后指纹、实际退出码与反例完整源码在 `out/tests/temp/message_stream_event_ownership_20261025/artifacts/`。本切片为现有恢复合同补缺，2.2/7.2总门保持未勾；未跑Windows实机或完整Web，Agent团队与协作继续TODO。
- 合并共享版本的存储/合同/投影/运行时186 passed/12.48秒，两项真实HTTP均通过（耗时见原始stdout/result）；Ruff、compileall及strict 45 passed/0 failed。五组源码前后指纹均一致且运行组无残留，静态自查核8个内部调用全部显式传归属、验证先于追加，既有迭代器与列表消费者AST未变；非独立代理审查。候选HTTP期间仅单测import位置发生调整，生产与集成测试源码未变，不将它冒称完整冻结源码验证；最终共享运行已覆盖该文件的最终版本。共享暂存条目及他人流式优化继续独立保留。


### 2026-10-25 消息流启动恢复单链（2.2/7.2 部分收口）

- 删除启动恢复自行采用高水位快照、发布局部state及单事件幂等缓存的第二套逻辑，统一调用普通冷读取的完整恢复入口。所有快照关联、序号与applied-event归属验证成功后才发布缓存；终态历史随即逐流卸载，resumed Job保持原状态，其余遗留流只结算一次execution_lost。无已提交事件的空日志保留原跳过行为并记录诊断；不新增reader、writer、恢复格式或兼容层。
- 完整Git archive旧基线：快照错误Thread、超前序号、错误applied owner×completed/lost/resumed共9 failed，空/空白日志2 passed，1.56秒。候选生产代码净减28行，存储143 passed/12.69秒，Ruff通过；新增空日志正例随共享版本通过；真实 Gateway/Backend/model_stream 候选验收1 passed/107.15秒，产品执行生成终态历史，三类故障均在真实进程启动阶段明确失败，修复原件后每次均能重启并逐字段读回原历史，canonical及损坏原件均保全。
- 原始命令、完整源码前后指纹、退出码及完整反例源码在 `out/tests/temp/message_stream_startup_recovery_20261025/artifacts/`。这是现有持久恢复合同收口，不代替2.2/7.2全局门、Windows实机或完整Web验收；Agent团队与协作继续TODO。
- 最终共享版本：197 passed/13.74秒，三项真实HTTP/重启产品回归3 passed/216.92秒；Ruff/compileall与strict 45 passed/0 failed。五组源码前后指纹一致，运行组无残留。静态自查仅reconcile_unfinished_streams改变、其余55个方法AST不变，原迭代器/流式裁剪保留，无新源码目录；这是主线程自查，非独立代理审查。共享暂存条目独立保护，本次提交不包含他人的优化。


### 2026-10-25 消息流定位参数（2.2/7.2 部分收口）

- 原store的请求、索引、生成端与events/state/applied-event-id三个文件模板统一验证逻辑流ID：非空单一文件名，不接受路径分隔、盘符/URI冒号、ASCII控制字符或点段；不解码、清洗、截断或重定向。显式空值明确拒绝，只有未提供才按原索引选择。正常生成及既有合法不透明标签继续使用原格式，不强制UUID，不扩张或收紧普通文件的原生路径输入合同。公开错误只带稳定Session与字段/阶段，非法值留在原诊断。
- 候选存储207 passed/20.27秒（含两种既有标签的实际创建/持久化/冷恢复），真实Gateway/Backend/model_stream为1 passed/58.08秒。完整Git archive旧基线的60项请求/索引/文件模板负例均失败，两种合法标签通过；最终生成端隔离路径与空值反例另用完整tree `008406973cb1435a207778efdfdc6a8312e6726f` 运行，2 failed/0.73秒，均精确复现未拒绝。首次生成端绝对路径反例受宿主/private权限影响，不计该项为业务反例；改用正式隔离工作区后确认失败原因。
- 准确命令、完整源码前后指纹、原始输出和退出码在 `out/tests/temp/message_stream_locator_20261025/artifacts/`。POSIX真实越界读由fixture隔离哨兵与Path.open观察区分，Windows/UNC为词法拒绝，不计Windows实机通过；未跑完整Web，2.2/7.2总门不勾，Agent团队与协作继续TODO。

- 共享版本存储/API合同/投影/运行时269 passed/21.65秒；随后只把两项生成端测试的绝对路径移入隔离工作区，最终2 passed/0.39秒，其余267项及生产代码未变。首次共享2项失败源于捕获文件基线前尚未触发Thread查询的首次SQLite WAL/SHM建立，先完成必需查询后保留完整逐字节断言通过，没有排除sidecar或放宽业务断言。共享真实HTTP为2 passed/131.16秒：请求和损坏索引各5种值×snapshot/events/SSE建流共30次精确404，request_id一致、原索引/外部哨兵/canonical不变，修复后原历史及硬重启一致，并回归原四类持久损坏。
- 最终Ruff/compileall通过，strict OpenSpec 45 passed/0 failed；主线程差异与AST自查确认唯一ID验证、7处准入调用、旧falsy回退零残留，原迭代器/列表读取/流式裁剪及启动恢复未被改变，无新源码目录。这是主线程自查，非独立代理审查；共享暂存条目和他人优化独立保留。

### 2026-10-25 Node 调试进程原生错误与动作冷恢复（2.2/7.2 部分收口）

- 启动 owner 的 OSError 公开事实只含精确 Session/Thread、原生异常类别、errno 和 winerror；原异常对象仍是 cause，完整堆栈保留在诊断日志。信号、强杀和等待退出的三处原生停止错误也在原 producer 转换，复用既有动作 writer；不新增全局脱敏器、不改变停止期限、进程身份核实和非系统启动错误正文。
- 真实重启验收发现原 manifest writer 在 runtime 消费 pending actions 后仍写空 pending 历史。现在向唯一 SessionState 注入服务既有的只读 runtime mapping，manifest 直接选择当前 runtime 动作、无 runtime 时选择原 pending 动作；不另存或同步第二历史。已迁齐 service 构造和继承 SessionState 的源码对账 fixture。
- 原 HEAD 加新增验收：三种 POSIX/盘符/UNC 字面量的单元反例为3 failed/1 passed；真实 Gateway 的缺失 Node 可执行文件请求为1 failed/21.65秒，409 原文确含原生路径。最终候选37项启动/claim/停止回归通过（41.82秒），真实产品测试1 passed（35.55秒），从产品 Session/配置/启动入口生成失败动作，核 live、原 manifest bytes、settled claim、诊断原文、后端重启冷读及同一配置成功重试/真实停止；无手填关键业务状态。
- 扩展 Node 单元检查曾为238 passed/8 setup errors，错误均是继承 fixture 未传新增 runtime 依赖；迁齐后8项全部通过。HTTP002业务断言已走完但证据编码误把 store 的二元返回值当成 JSON 正文，HTTP003仍未正确解包；现保存原 bytes 和 DTO，失败原件保留。最终 Ruff/compileall 均通过；共享合并后的验证另记原件，不拿候选通过冒充共享通过。
- 准确命令、源码前后指纹、原始输出、实际退出码和主线程结构自查在 `out/tests/temp/node_debug_launch_errors_20261025/artifacts/`；不称独立代理审查，不代替 Windows 实机、完整主窗口或其它原生/Inspector 错误出口的总验收。2.2/7.2 保持未勾，Agent 团队与协作专项继续 TODO。
共享合并后：完整Node调试/API 280 passed（57.89秒），真实Gateway重启/原配置重试1 passed（31.18秒）；Ruff、compileall与45项strict均退出0，所记录源码前后指纹一致且进程组无残留。保留原暂存索引字节，并将fixture依赖修改合入共享现有的node_debug子目录，独立候选仍按其HEAD中的原路径提交该消费者修改。


### 2026-10-25 Node Inspector 接收故障（2.2/7.2 部分收口）

- 接收循环不再把原生异常或无效协议帧原文拼进公开错误。原 producer 统一记录精确 Session/Thread、异常类型，系统错误带 errno/winerror，JSON 语法错误带行/列/位置；等待中的命令与 live 状态消费同一公开事实。原异常对象保留为 cause，完整堆栈写诊断日志；正常断线、取消、closing 和已有日志点错误优先级不变，不增加文本脱敏、第二错误账本或传输实现。
- 完整 HEAD 候选加新增反例为5 failed（0.67秒），覆盖 JSON、带路径的无效帧和 POSIX/盘符/UNC 原生异常；修复后接收/超时9 passed，Inspector/启动/API 31 passed（4.24秒）。盘符/UNC 只验证错误文字边界，不计 Windows 实机通过。
- 真实 Node 进程和原 WebSocket 握手接通后，在外部接收边界注入 PermissionError；正式 API 路由、TraceMiddleware 与原异常 handler 返回409且 request_id 一致，公开错误/live/manifest无原路径，原 cause 可在诊断核对。关键 Session、方案、启动、claim和失败动作由产品入口产生；核实原PID已退出、claim settled、冷服务重开后的精确动作与方案，并在故障移除后从同一方案成功重试和停止。候选1 passed（12.18秒）。HTTP 使用进程内 ASGI、测试授权准入和传输故障替身，冷重开是服务对象而非整个后端进程，不称真实 Gateway、进程重启或完整 Web 验收。
- 初次 HTTP 装具遗漏生产异常 handler，409无 request_id；接入原 handler 后保留请求身份断言。第二次误将已启动又退出的真实进程当作未 spawn，按原快照代码改为精确核 PID、OS 不存在与 settled claim，而非要求历史 PID 为空；原失败输出均保留。最终 Ruff/compileall通过，主线程差异自查；未启动团队或独立审查代理。
- 准确命令、受测源码前后指纹、原始输出和退出码在 `out/tests/temp/node_debug_receive_errors_20261025/artifacts/`。本切片不代替2.2/7.2全局门、其它 Inspector/原生错误出口、Windows实机或主窗口；Agent团队、委派及协作专项继续TODO。
共享最终复验：Node调试与主API270 passed（55.53秒），请求身份/Thread范围14 passed（2.85秒），同一真实Node/ASGI故障恢复1 passed（10.13秒）；Ruff/compileall和strict 45 passed/0 failed。记录的源码指纹前后一致、进程组无残留；20个其它Inspector方法AST不变，未新增生产目录，原共享暂存索引独立保留。最终仅追加本验收文字，未修改受测代码。


### 2026-10-25 执行原生 I/O 失败出口（2.2/7.2 部分收口）

- 真实原生 I/O 的 filename、filename2 与 strerror 不再直接成为执行业务错误正文；原异常类型和 errno 形成唯一公开原因，由 step、Job 主收口与 task callback、自动命名提示及 LLM response/attempt 记录共同消费。原异常对象继续传播、完整堆栈写诊断日志；缺 execution 的早期失败和自动命名失败也保留原错。未改变业务超时、取消优先级、错误 code/reason、原失败状态与自动命名失败后继续执行的行为。未增加错误账本、第二执行实现或文本正则脱敏。
- 完整 HEAD 候选的真实 Session/消息准入、编译图、Runner、Saver 和 Job 复现 IsADirectoryError，旧代码将完整路径写入 Job 及消息流；修复后核冷消息流 HTTP、Job DTO、SQLite 与 LLM 记录无该位置，同 Thread 下一次执行完成，旧快照及 canonical 前缀不变。外部模型响应受控，HTTP 为进程内 ASGI，鉴权/request_id 使用已有装具替身；不称真实 Gateway、外部模型、进程重启或完整 Web 验收。
- 候选定向回归101 passed（27.22秒），覆盖原生多文件错误与 strerror 路径、Job 两个失败出口、执行前错误、自动命名、LLM attempt 及既有同步/异步 Provider、业务超时和取消；Ruff/compileall与严格 OpenSpec 45 passed/0 failed。早期复现装具未将 typed event payload 转 JSON，修正后在旧实现得到精确路径断言失败，原始失败保留。准确命令、完整源码前后指纹、原始输出及退出码在 `out/tests/temp/execution_io_errors_20261025/artifacts/`。
- 本切片只覆盖结构化原生 I/O 异常，不冒称其它业务异常、包装异常或全仓原生错误出口已收口；2.2/7.2全局门保持未勾。未新增生产目录，主线程自查，未启动独立审查代理；Agent团队、成员、委派与协作继续TODO。

共享最终复验：103 passed（23.84秒），Ruff/compileall及strict 45 passed/0 failed；候选及共享六组验证的记录源码指纹前后一致、进程组无残留。共享队列位置查询、日志裁剪及既有测试fixture命名优化保留；提交仅含本切片，原共享暂存条目不改写。最终只追加验收收据并清理新增测试末尾空行，AST与受测版本一致。


### 2026-10-25 Rollout 锁错误出口（2.2/7.2 部分收口）

- 在原文件锁 owner 统一公开错误事实：artifact、阶段、异常类型及 errno/winerror；真实路径与完整异常只留诊断日志和 cause。原 TimeoutError、RuntimeError、OSError 子类及 portalocker 解锁异常类型保留；处理本地排队/升级/并发复用拒绝、路径解析、打开、加锁、关闭与释放，不增第二锁或持久错误账本。Python 3.12 的符号链接环 RuntimeError 也由原解析点明确报告。
- FIFO、持锁归属、可重入深度、同步/异步准入、统一期限与取消释放规则保持；13个核心方法AST未变，共享工作区原有队首通知优化和测试fixture迁移独立保留。完整Git archive保留旧生产加最终新增测试，15 failed/1.54秒，精确复现原路径/原生错误出口；最终共享错误及完整Saver回归92 passed/33.31秒，含15项新增同步/异步错误、真实本地/文件锁超时与失败后重取锁证明。候选完整fork retention中断/恢复7 passed/82.39秒。
- 共享正式matrix的历史入口12 passed/81.91秒：关键历史由产品Session/消息执行入口配合现有受控model_stream生成；在原生打开/加锁/解锁处注入故障，bootstrap/history返回500及原Session身份、必要阶段而无路径，原异常可在诊断核对。句柄关闭、独立写者可重取锁，冷Saver核canonical身份/正文，修复后原Thread/Turn和公开历史一致。HTTP使用进程内ASGI和现有授权fixture，冷重开为Saver对象；不称真实Gateway、后端进程重启、Windows实机或完整Web验收。
- Ruff/compileall通过。共享历史检查期间仅未加载的 `scripts/install/AGENTS.md` 发生变化，完整源码冻结核验按实拒绝；限定收据核对全部后端/测试/配置输入未变、raw哈希、实际退出码及进程组回收，不为无关文档变化重复长跑。源码版本、准确命令、前后指纹、完整失败/成功原件和主线程自查在 `out/tests/temp/rollout_lock_errors_20261025/artifacts/`；主线程自查不称独立代理审查。全局2.2/7.2仍未勾，Agent团队、成员、委派及协作保持TODO。


### 2026-10-25 SQLite 路径预算与原生 VFS 诊断（2.2/7.2 部分收口）

- 在原 `sqlite_state` owner 修正路径预算、路径解析及 VFS 加载/符号/头部/原生探针错误：公开异常保留原类别、阶段、SQLite版本、UTF-8字节数、VFS上限与主库/sidecar类别，系统错误保留errno/winerror，探针错误保留sqlite_errorcode/name。数据库、扩展库和探针位置只写诊断日志，原生异常留cause；不扫描/截断原错误正文，不返回默认VFS、不加载另一份系统SQLite。
- 主库/rollback journal/WAL/SHM预算算法、当前解释器原生绑定、探针加载关闭顺序、连接/事务/SQL迁移和schema版本保持。新增错误反例随完整Git archive与旧生产组合运行，12 failed/0.83秒；最终共享底层存储/Session和Thread创建169 passed、3 skipped（8.26秒），跳过项均为Windows原生专用，未声称实机通过。既有Session预算测试把路径核对迁入日志，仍保留精确被校验路径、拒绝后无record/staging等断言。
- 共享真实创建错误与重试22 passed/19.22秒。新增ASGI请求只注入VFS上限数值，执行原产品预算校验，400响应及request_id保持，catalog nodes/creation rows与Session目录清单不变；恢复原VFS上限后，同一创建入口成功发布active Session。HTTP使用现有授权fixture，不称真实Gateway或完整Web。创建日志最终文字改为通用诊断位置，避免对非SQLite预算错误误指；该文字修订后2项定向入口/创建检查通过，其余业务AST未变。
- 发行源资源复制到隔离目录后，编译真实C探针并经当前Python SQLite extension API核VFS值、WAL写入和重开，删除探针仍明确抛原OperationalError；共享1 passed。此项不是重建/安装完整release，也不替代Windows。Ruff/compileall通过；候选首轮因使用共享迁移后的测试路径而无用例（exit4），第二轮168 passed/1 failed是旧cause路径断言，按上述合同迁齐后169通过，原件全部保留。
- 准确命令、包括共享未跟踪源码的完整指纹、原始输出/退出码、完整旧实现反例和主线程结构自查在 `out/tests/temp/sqlite_runtime_errors_20261025/artifacts/`。主线程自查不称独立代理审查；共享既有required-tables查重优化、测试目录迁移和暂存内容独立保留。rollout连接形态拒绝及SQLite进程持锁错误仍是后续缺口，2.2/7.2全局门不勾；团队、成员、委派和协作继续TODO。


### 2026-10-25 Rollout SQLite 连接路径校验（2.2/7.2 部分收口）

- 四类目录/索引文件形态拒绝与检查时原生错误，复用原存储错误出口；公开原因保留 artifact、阶段、异常类别与 errno/winerror，真实路径及原异常留诊断和 cause。锁调用方全量迁齐后移除旧 helper 名称；不增加连接器、writer、迁移或 schema。AST 对账确认两个锁类在调用改名归一后相同，SQLite 连接、schema 探测、PRAGMA 和清理不变；共享队首通知优化独立保留。
- 完整 Git archive 的旧实现加新增测试为15 failed/0.66秒；最终共享存储及完整 Saver 107 passed/33.84秒，历史入口正式 matrix 4 passed/29.46秒，Ruff/compileall通过。产品 Session 与消息执行配合现有 model_stream 生成关键历史；bootstrap/history 在连接 owner 检查失败时明确500，原 SQLite 完整 dump、canonical 字节和身份不变，写锁可重取、冷 Saver 及原 API 故障移除后读回原历史。ASGI 与现有授权 fixture 不代表真实 Gateway、进程重启、Windows实机或完整Web。
- 准确命令、前后指纹、原始输出/退出码、完整反例与主线程结构自查位于 `out/tests/temp/rollout_index_paths_20261025/artifacts/`。共享存储检查期间三个未加载的Web文件被他人修改，完整冻结核验按实拒绝；限定收据核后端、测试、配置输入未变和进程组回收，其余共享检查完整冻结通过。候选 Saver 首次用了不存在的unit路径而exit4，定位真实integration路径后77项通过，保留失败原件。主线程自查不称独立审查。
- 2.2/7.2全局门仍未勾，SQLite进程持锁及其它存储错误出口另行处理；Agent团队、成员、委派与协作继续TODO。


### 2026-10-25 SQLite 状态库所有权与迁移拒绝（2.2/7.2 部分收口）

- 在原 SQLite owner 处理锁打开、初始化/加锁、解锁与关闭的原生错误，保留异常类别、errno/winerror和原cause；真实锁位置与原文只进入原诊断。真实占用仍立即以原RuntimeError拒绝，不等待、不共享。关闭、WAL失败、超前schema、账本缺号和缺表拒绝同样保留原原因及模式/版本/缺失项，移除公开真实路径；内部diagnostics合同未改。
- 新增12项反例随完整Git archive的旧实现均失败（0.72秒）；最终共享核心SQLite38 passed、3项Windows专用skipped（0.93秒），Gateway状态库、工作区状态/配置及活动游标93 passed（3.64秒）。覆盖真实锁竞争、失败后句柄关闭/重取、原库正文不变、迁移拒绝前后完整SQL dump一致、修复后原owner重开及原数据；WAL失败只替换原生PRAGMA返回值，不伪造业务状态。这是存储与消费方测试，不称真实Gateway HTTP、进程重启或完整Web验收。
- 锁标志、marker初始化、显式共享准入、SQL、WAL、事务和迁移顺序不变，不提升schema版本。AST对账确认SQLiteStateDatabase除RuntimeError正文参数外相同；共享required-tables查重优化独立保留。初次静态检查发现新增测试的import顺序和两处嵌套with，按规则整理后Ruff通过；compileall通过。源码前后指纹、准确命令、原始失败/成功输出、退出码和主线程自查在 `out/tests/temp/sqlite_owner_errors_20261025/artifacts/`，全部所记录运行源码冻结且进程组无残留。
- 全局2.2/7.2仍未勾；未声称Windows实机通过，不扩展到未覆盖的其它SQLite或业务错误。Agent团队、成员、委派与协作继续TODO，未启动代理或专项验证。


### 2026-10-25 Rollout 布局与 JSONL 恢复错误（2.2/7.2 部分收口）

- 旧布局、JSONL非普通文件/缺失拒绝及布局/文件检查、创建、已提交偏移检查与尾部恢复的原生错误，复用原storage错误出口。保留原因、artifact、阶段、异常类别、errno/winerror与原cause；真实位置和原文只留诊断。只按SQLite committed offset恢复尾部，已提交前缀、fsync顺序和恢复期限不变；不增加第二reader、writer或迁移。
- 完整Git archive的旧实现：新增尾部检查5 failed/1 passed（0.60秒，正例核偏移过大拒绝、相等不touch及尾部精确截断），产品历史场景5 failed（46.98秒）。共享尾部/完整Saver/schema拒绝107 passed（39.04秒）；共享历史集成5 passed，耗时及准确命令见原始result。Session/消息执行入口和现有model_stream生成关键历史；两类旧布局、JSONL符号链接/目录和原生检查拒绝后，原canonical字节与完整SQLite dump不变，移除故障后原owner初始化、冷Saver和历史API均返回原Thread/Turn与历史。fsync失败时只允许未提交尾部已截断，已提交前缀不变且可重试。
- AST对账确认维护类与尾部函数在移除错误上下文和RuntimeError正文参数后与基线相同；Ruff/compileall通过。初次静态检查仅发现新增测试的长import格式，集中整理后通过。源码前后指纹、完整反例源码、原始失败/成功输出、退出码及主线程自查位于 `out/tests/temp/rollout_recovery_paths_20261025/artifacts/`；全部记录运行源码冻结、进程组无残留。不新增源码目录，不称独立代理审查。
- HTTP使用ASGI及现有授权fixture，冷重开为Saver对象；不称真实Gateway、后端进程重启、Windows实机或完整Web验收。2.2/7.2总门保持未勾，未覆盖的其它存储错误另行处理；Agent团队、成员、委派及协作继续TODO。


### 2026-10-25 调试方案 fork 文件发布错误（2.2/7.2 部分收口）

- staging 检查/写入、旧 staging 清理、发布和已发布正文验证复用原存储错误出口。原生错误保留类别、errno/winerror及cause；布局拒绝保留具体原因。真实路径仅留原诊断，普通HTTP响应/创建记录只带原阶段和逻辑事实；不改变hash、journal、文件操作顺序或schema，不新增writer/兼容链。
- 候选局部发布/源捕获单测11 passed/0.57秒；完整Git archive的旧生产加新增单测8 failed/0.54秒。最终新增公开HTTP产品场景3 passed/49.00秒，另3项发布中断/冷Saver恢复在同一生产源码上通过：产品消息/model_stream生成执行、产品保存调试方案，staging/rename/验证原生错误后核source canonical/方案原件、创建记录中止、无可见半成品，故障解除后的新fork逐字段保全持久方案并冷读。
- 初次测试路径、URI helper导入、监督预算、遗漏公共订阅器正常启动，以及把摘要/缓存时间戳当持久源正文比较的失败均单列归因保留；未降低身份、次序、字节或错误断言。后续以持久owner的原完整配置为比较基准。准确命令、源码前后指纹、原始输出/退出码与AST对账在 `out/tests/temp/debug_fork_errors_20261025/artifacts/`；主线程结构自查不称独立审查。
- HTTP使用真实路由/依赖/TraceMiddleware及ASGI传输，冷恢复使用原Saver；不称真实Gateway、Backend进程重启、Windows实机或完整Web。2.2/7.2全局门继续未勾，团队、成员、委派和协作不追加实现或专项验证。
- TODO（独立既有行为）：调试方案保存时store更新updated_at而registry缓存保留写入前值；本批按持久冻结正文核全字段，不在错误合同切片扩改缓存。另行处理时应让原缓存消费权威保存结果，不能删除updated_at断言或引入第二可变事实源。
- 最终共享源码复核：11项单测通过（0.61秒）、完整6项fork产品集成通过（65.47秒）、Ruff/compileall通过、严格OpenSpec全量45 passed/0 failed；完整旧生产上的新增公开HTTP反例3 failed（49.10秒），均复现公开错误不符合阶段/真实路径合同。五项共享运行的源码冻结、原始输出、真实退出码及进程组回收由 verification003.json 核实，退出码全部0；反例收据见 verification002.json。此证据仍只覆盖本切片，不代替2.2/7.2全局验收。


### 2026-10-25 调试方案持久正文发布一致性

- 关闭上一切片登记的updated_at缓存TODO：原store在原子写入成功后返回实际持久正文，registry随后发布同一对象；创建、更新、导入、复制及两类隐式方案消费者迁齐该返回值。运行时状态回写仍沿原put入口，不新增writer、缓存补丁、回读链或schema；原写盘时刻规则保持，失败不发布未保存方案。
- 完整旧生产加相同新增测试为15 failed/1.96秒，分别复现返回/热缓存与冷读不一致，以及write/replace失败后缓存存在未保存变更；候选15 passed/1.84秒，相关配置、store、断点、源校验及fork回归合计167 passed/10.51秒。真实产品执行/model_stream生成来源的完整fork集成6 passed/65.03秒，新增全字段热读/持久正文与updated_at一致断言，原失败与冷Saver恢复断言保留；Ruff/compileall通过。
- 准确命令、完整源码前后指纹、原始输出与真实退出码、完整旧生产和主线程本地结构自查位于 `out/tests/temp/debug_configuration_publication_20261025/artifacts/`；verification001.json核实所有运行源码冻结与进程组回收。原单测与回归有重叠，不累加；产品HTTP仍为真实路由配合ASGI，不能替代真实Gateway、后端进程重启、Windows或完整Web。团队与协作继续TODO，2.2/7.2全局门仍未关闭。
- 最终共享三方合并后复核：相关167项回归通过（9.78秒）、完整6项fork集成通过（65.56秒）、Ruff/compileall通过；候选严格OpenSpec全量45 passed/0 failed。共享各运行的完整4591路径指纹未变，原始退出码/日志/进程回收分别由verification002/003核实。原store枚举局部变量整理独立保留，本地结构自查见architecture002.json；不把它称作独立审查。


### 2026-10-25 fork 冻结文件清单错误（2.2/7.2 部分收口）

- 原清单函数供来源捕获、marker恢复与附件冻结集合共同消费；现明确拒绝缺失/非普通/符号链接根，目录扫描错误立即抛原错，不再按os.walk默认行为遗漏子树。读取/元数据原生失败和布局拒绝复用原storage错误出口，保留类型、errno/winerror/cause，位置只留诊断；排序、排除项、文件hash与同一恢复链保持，不新增writer/reader或schema。
- 候选10项单测通过（0.31秒）；完整Git archive旧生产加相同新增测试为9 failed/1 passed（0.60秒），正确空目录/排序/hash/排除项正例保持通过。候选产品2项通过，旧生产2项HTTP反例失败（准确耗时见原始输出）：由原产品消息执行/model_stream生成来源，扫描原生失败与symlink拒绝均未发布目标，原创建记录中止且不含真实路径，原canonical字节不变；故障移除后新key重试成功，冷Saver读回完整历史，副本canonical与来源原字节一致。
- 源码前后指纹、完整旧生产、原始输出/退出码与AST核对在 `out/tests/temp/fork_source_manifest_20261025/artifacts/`。AST移除本次错误上下文、根检查、onerror及raise正文后，全部5个函数与原提交一致。HTTP使用真实路由、Depends、TraceMiddleware及ASGI，不称真实Gateway、后端进程重启、Windows实机或完整Web验收；其它capture文件I/O和detail复制出口仍待后续，2.2/7.2全局门未勾，团队/协作继续TODO。
- 最终共享三方合并后10项单测、2项公开fork集成（39.08秒）、Ruff/compileall均通过；候选完整严格OpenSpec为45 passed/0 failed。候选产品2 passed/41.76秒，旧生产HTTP反例2 failed/36.12秒，原始响应含真实路径；verification004.json核实四项共享运行的4593路径完整冻结、真实退出码及进程组回收。矩阵中他人的独立suite顺序保留；结构自查见architecture001.json，不称独立代理审查。

### 2026-10-25 fork captured marker 原生 I/O（2.2/7.2 部分收口）

- 捕获位置检查、发布、marker/manifest/control 读取、附件检查和清理统一消费原 storage 错误出口；普通错误保留 artifact/stage、类型与 errno/winerror，直接 cause 和真实位置留诊断。发布扫描错误立即抛出且拒绝符号链接目录；不改变 marker 字节/hash、fsync/rename 顺序、journal、来源身份或恢复链。未完成 preparing 与完整 marker 尚未 CAS 的恢复区别保留；其它 source 复制/detail I/O 仍待后续，2.2/7.2 不补勾，团队与协作继续 TODO。
- 候选 14 项单测通过（0.37秒）；完整旧生产加相同测试为12 failed/2 passed（0.74秒）。候选正式 matrix 产品验收10 passed/121.66秒；旧生产6 failed/4 passed/109.60秒。新增6项由产品消息执行与 model_stream 生成来源，经真实路由/Depends/TraceMiddleware 的 ASGI HTTP 注入原生故障，核公开错误无路径、原错误留诊断、创建中止、来源 canonical 不变、新key重试及冷Saver副本原字节一致；另4项沿原 lifespan 验证 Session/child 的 partial 中止和完整 marker 恢复不重捕获、不重复物化。
- 扩展验收发现两处旧测试问题：原附件目录 SQL/写入方使用 prepared，旧断言却期待不合法的 preparing；完整操作取消后恢复未从原产品入口取得终态回执，期限观察仍 active。保留最初候选/旧生产被原60秒看门狗终止的原件及提前输出的共同断言失败，按真实合同修正精确状态，并添加同key幂等产品回执与冻结目标身份断言，不关闭或放宽看门狗、期限及其它恢复断言。
- 准确命令、完整源码前后指纹、原始日志/真实退出码、完整旧生产与本地结构核对在 `out/tests/temp/fork_capture_io_20261025/artifacts/`；AST只移除本次错误上下文、onerror与目录symlink拒绝后，原6个顶层符号一致（ast002.json）。Ruff/compileall通过；这是作者自查，不称独立代理审查。ASGI及冷容器不代替真实Gateway、独立Backend进程重启、Windows或完整Web验收。
- 最终共享三方合并后14项单测通过（0.50秒）、正式产品矩阵10项通过（108.46秒）、Ruff/compileall通过；候选严格OpenSpec全量45 passed/0 failed。共享运行的完整4649路径指纹中，另一路改动更新了前端测试anchored_overlay_positioning_harness.tsx，故不能声称完整源码冻结；该TSX不在本片Python/matrix执行依赖内，其余4648路径未变，具体相关性、原始日志/退出码及进程回收由shared-relevance001.json显式核对，完整冻结检查的拒绝保留；静态/编译与两次strict运行由verification003.json核实完整冻结，共享strict同为45 passed/0 failed；测试导入整理和独立suite顺序保留在共享树，不纳入本片提交。

### 2026-10-25 fork typed detail 复制错误（2.2/7.2 部分收口）

- 原复制函数在原生元数据、目录创建、copy2与正文hash读取边界复用既有storage错误出口；布局拒绝保留原因，位置只留诊断。原冻结SQLite查询、历史owner/locator校验、声明顺序、原字节与hash不变，缺失正文仍由原资源owner裁定，不新增writer、reader或schema。其余source捕获/复制I/O仍待后续；2.2/7.2全局门未勾，团队与协作继续TODO。
- 候选局部10项复制合同检查通过（16.08秒），完整旧生产9 failed/1 passed（16.65秒）；同一声明顺序与可选缺失正文正例保持通过。首轮完整matrix为18 passed/1 failed（75.67秒），唯一失败是新增测试误将父目录故障诊断位置断言为叶文件，按真实故障节点修正，不放宽错误类别、身份、hash或期限；最终相关9项通过（67.51秒），其中3项由产品消息执行/model_stream生成状态、通过真实路由/Depends/TraceMiddleware的ASGI HTTP验证错误、中止、不变原件、新key重试、冷Saver详情身份/正文及request_hash，另外6项保留原必需正文缺失/篡改拒绝。旧生产相应3项产品反例失败、6项既有拒绝通过（77.29秒）。局部与matrix存在重叠，不累加为独立场景。
- 完整Git archive旧生产、准确命令、完整源码前后指纹、原始输出/真实退出码与AST核对位于 `out/tests/temp/fork_detail_copy_20261025/artifacts/`。移除本次错误上下文及布局错误正文后，原8个顶层符号AST一致。Ruff/compileall通过；结构核对为作者自查，不称独立代理审查；ASGI和冷Saver不代替真实Gateway、独立后端进程重启、Windows或完整Web验收。
- 最终共享正式matrix 19 passed/62.32秒，含10项局部合同、3项真实产品入口及6项原必需正文拒绝；Ruff/compileall通过，候选strict全量45 passed/0 failed。verification003.json核实本片所登记3778条后端/协议/harness/config/matrix路径前后未变、原始日志/退出码和进程组回收；profile明确排除本片不启动或构建的客户端源码及客户端专项测试，不声称全仓冻结。共享独立suite顺序调整三方保留，不纳入本片提交。


### 2026-10-25 fork 隔离 SQLite worker 错误（2.2/7.2 部分收口）

- 父进程原生调用失败复用原 storage 出口，保留异常类别、errno/winerror 与 cause；子进程非零退出仍为 RuntimeError，公开阶段与退出码，完整 stderr 留诊断，原 command/stdout/stderr/status 留 CalledProcessError cause。超时仍为 TimeoutExpired、原30秒与原cause，普通响应/创建记录不含真实位置；原 run 终止并等待 worker 后清理私有目录。子进程脚本、成功 manifest、固定事务、backup与hash未改，不新增复制链或schema。
- 候选6项单测通过（30.63秒），完整Git archive旧生产加相同最终测试为4 failed/2 passed（30.80秒）；真实worker正例核committed页与hash、持rollout锁时页/WAL/SHM全字节不变，真实child失败与真实30秒超时核源不变、回收与重试。首轮候选1 failed/5 passed、旧生产5 failed/1 passed，唯一共同错误是新增测试误要求在线session-control reader的SHM读标记也不变；原脚本明确允许在线reader，原始差异仅SHM，最终仍严格核持久页/WAL、完整源记录及持锁场景全部字节，不放宽身份、hash或期限。
- 正式产品matrix候选2 passed/38.14秒，旧生产2 failed/33.67秒；产品消息执行与model_stream生成来源，真实路由/Depends/TraceMiddleware配合ASGI验证原生spawn拒绝与真实child copyfile失败、请求头体request_id一致、中止/无可见目标/原canonical不变，新key重试后冷Saver按原检查点ID核完整checkpoint、metadata、pending writes、历史assembly全字段/owner及request_hash与原canonical字节。ASGI和冷Saver对象不代替真实Gateway、独立Backend进程重启、Windows或完整Web验收。
- 完整旧生产、准确命令、源码前后指纹、原始输出/真实退出码及本地结构核对在 `out/tests/temp/fork_sqlite_worker_errors_20261025/artifacts/`；verification001/002核实上述运行源码冻结与进程组回收。AST核原子进程脚本逐字不变，去除本次错误分支后原成功路径一致；结构核对为作者自查，不称独立审查。其余source捕获/复制及TemporaryDirectory创建清理的原生边界仍待后续，2.2/7.2不补勾，团队与协作继续TODO。
- 最终共享三方合并后6项单测通过（30.63秒）、正式matrix的2项产品入口通过（39.72秒），Ruff/compileall通过；候选严格OpenSpec全量45 passed/0 failed。verification003.json核实本片登记的3780条后端/协议/harness/config/matrix路径前后未变、原始日志/真实退出码及进程组回收；profile明确排除本片不启动或构建的客户端源码与客户端专项测试，不声称全仓冻结。共享独立suite顺序保留，不纳入本片提交。


### 2026-10-25 SQLite 私有目录错误与组合故障（2.2/7.2 部分收口）

- 原TemporaryDirectory仍唯一拥有目录，创建和原cleanup分别复用storage错误出口（sqlite_private_create / sqlite_private_cleanup），保留原生类别、errno/winerror和直接cause，真实位置只留诊断。原cleanup放入显式finally，返回成功manifest前必须完成；worker非零退出先形成原RuntimeError/CalledProcessError并记录stderr，再进入finally，使清理再次失败时原worker证据保留在原始cause/context和日志。不增加writer、复制链、清理链或schema；原子进程脚本、stdin、worker调用/30秒预算、成功manifest逐项AST不变。
- 首轮单测候选2 failed/7 passed、旧生产3 failed/6 passed；产品候选2 failed/3 passed、旧生产3 failed/2 passed，候选失败均因新增测试只替换shutil.rmtree，而当前Python的TemporaryDirectory使用tempfile模块已绑定的删除函数，未触发预期清理故障。读取实际stdlib实现后改在其真实调用边界注入，保留原失败原件，不放宽任何断言；候选新增3项通过（0.62秒），旧生产3项均失败（0.81秒）。反例为9eda87ff完整Git archive；负例在Ruff前复制测试，仅标准库import顺序不同，comparison001.json核完整测试逻辑AST一致，不声称测试文件字节相同。
- 正式matrix候选5 passed/62.53秒，旧生产3 failed/2 passed/60.21秒。原产品消息执行与model_stream生成来源，经真实路由/Depends/TraceMiddleware的ASGI HTTP核原spawn/child失败、私有目录创建、清理和真实child IsADirectoryError后再清理失败；错误阶段、头体request_id、中止/无可见副本、canonical原字节、新key重试及冷Saver原检查点/metadata/pending writes、assembly全字段/历史owner/request_hash保持。组合故障的真实worker原始stderr与退出码保留，清理原错的context/cause仍指向同一worker失败；没有伪造业务历史或替代模型实现。
- 准确命令、原始输出/退出码、完整源码前后指纹、完整旧生产和作者本地结构核对位于 `out/tests/temp/fork_sqlite_private_io_20261025/artifacts/`；verification001/002核实上述运行冻结与进程组回收。不是独立审查，ASGI与冷Saver对象不代替真实Gateway、Backend进程重启、Windows或完整Web。其它source捕获I/O、ignore_errors清理及transfer仍待后续，2.2/7.2不补勾，团队与协作继续TODO。
- 最终共享三方合并后完整9项单测通过（30.99秒）、正式matrix完整5项产品入口通过（59.07秒），Ruff/compileall通过，候选严格OpenSpec全量45 passed/0 failed。verification003.json核实登记的3780条后端/协议/harness/config/matrix路径前后未变、原始日志/真实退出码与进程组回收；明确排除本片不启动/构建的客户端源码与专项测试，不声称全仓冻结。


### 2026-10-25 rollout 来源捕获的失败清理（2.2/7.2 部分收口）

- 原异常清理移除rmtree(ignore_errors=True)，同一次rmtree复用storage错误出口（artifact=fork_source、stage=cleanup）。清理原生失败保留类别、errno/winerror与直接cause，此前捕获失败仍在原生context和诊断中，普通错误和创建记录不含真实位置；清理成功仍抛回原捕获错误。原复制/读取、锁释放、manifest/hash/journal和成功链保持，不新增worker、writer或第二清理链。
- 候选新增2项单测通过（0.26秒），完整63f4897fe Git archive旧生产加相同测试2项均失败（0.47秒）；通过真实os.rmdir拒绝验证缺锁/ACL失败后清理错误不再被忽略、未删除目录仍可观察、此前异常保留、原源文件不变，解除故障后原清理合同恢复。正式matrix候选完整6 passed/73.46秒，旧生产1 failed/5 passed/65.89秒：产品消息执行/model_stream生成来源，真实child copyfile失败后又遇真实rmdir边界错误，核公开错误阶段/无路径、头体request_id一致、原child stderr/退出码及cause/context、中止/无可见半成品、原canonical字节、新key重试和冷Saver原检查点/metadata/pending writes、assembly全字段/历史owner/request_hash。原5项worker场景保持，不累加为新增场景。
- 原始准确命令、输出/真实退出码、完整源码前后指纹、完整旧生产及作者本地结构核对在 `out/tests/temp/fork_rollout_cleanup_20261025/artifacts/`；verification001.json核实冻结与进程组回收。AST移除本次cleanup错误上下文并恢复ignore_errors参数后整个原模块一致，源码签名不变。首个apply_patch发现既有private_source_snapshot测试仅在共享树未入HEAD，补丁原子拒绝且未改源码；本片新增独立source_snapshot_cleanup测试，不覆盖或纳入他人的既有测试。结构核对不是独立审查，ASGI/冷Saver对象不代替真实Gateway、Backend进程重启、Windows或完整Web。其它source捕获原生I/O与transfer仍待核，2.2/7.2不补勾，团队与协作继续TODO。
- 最终共享三方合并后4项来源清理单测通过（0.28秒，含共享既有两项未修改测试）、正式matrix完整6项产品入口通过（67.05秒），Ruff/compileall通过，候选严格OpenSpec全量45 passed/0 failed。verification002/003.json核实登记的3781条后端/协议/harness/config/matrix路径前后未变、原始日志/真实退出码及进程组回收；profile排除本片不启动/构建的客户端源码与专项测试，不声称全仓冻结。既有未跟踪private_source_snapshot测试仅用于关联回归，不纳入本片提交。


### 2026-10-25 rollout 来源捕获的直接原生 I/O（2.2/7.2 部分收口）

- 来源布局、准备/权限、锁复制、rollout复制、SQLite副本检查、JSONL冻结、control读取和manifest发布复用原storage错误出口；保留原类别、errno/winerror及直接cause，ACL原RuntimeError保留原启动/超时链。目标目录碰撞不清理既有目录；原worker/reader/typed detail、复制顺序、锁、manifest/hash/journal保持。AST归一化本片错误上下文后整个原模块一致，全部签名未变；这是作者自查，不称独立审查。
- 候选10项单测通过（0.39秒），完整c5ab6c0a Git archive旧生产的新增6项全失败（0.57秒）。保留并迁移共享树既有private_source_snapshot两项测试，断言公开preparation阶段与原生cause，不覆盖原行为；cleanup组合失败的原始context链同步迁齐。
- 正式产品matrix候选16 passed/166.46秒，旧生产10 failed/6 passed/142.81秒；产品消息执行/model_stream生成来源，经真实路由/Depends/TraceMiddleware与ASGI核10项新增原生故障及原6项worker/cleanup场景。断言阶段/无路径、头体request_id、中止/无可见半成品、来源canonical原字节、新key重试、冷Saver同一检查点/metadata/pending writes、历史assembly全字段/owner/request_hash；copytree使用真实stdlib汇总失败，不伪造Error回执。matrix预算随场景数扩为360秒，4GiB保护及产品worker30秒/ACL10秒不变。
- 首轮新增产品测试补在定义模块而非生产已导入的调用绑定，10项未命中故障并正常fork；按实际调用模块修正后得到上述红绿，不改产品或减弱断言。首轮原输出保留；AST自查首轮归一化脚本留下嵌套list/None，修正脚本后通过，未据此修改生产。准确命令、原始输出/真实退出码、源码前后指纹、完整旧生产及自查位于 `out/tests/temp/fork_rollout_capture_io_20261025/artifacts/`；verification001/002核实运行冻结及进程回收。ASGI/冷Saver对象不代替真实Gateway、独立Backend重启、Windows或完整Web；transfer等仍待核，2.2/7.2保持未勾，团队/协作继续TODO。

- 最终共享三方合并后10项单测通过（0.36秒），正式产品matrix 16 passed, 61 warnings in 152.58s (0:02:32)；Ruff/compileall及OpenSpec strict 45 passed/0 failed。verification003/004.json核实登记的3782条后端/协议/harness/config/matrix路径前后未变、原始日志/实际退出码及进程回收；profile明确排除本片不启动或构建的客户端，不声称全仓冻结。共享独立suite顺序及原暂存区保持，既有private_source_snapshot两项仅迁精确错误合同。


### 2026-10-25 fork 冻结产物来源导出（2.2/7.2 部分收口）

- 原os.walk增加onerror传播，拒绝原先静默跳过的符号链接目录；清单元数据与分块读取沿原storage错误出口（fork_transfer / export_manifest / export_chunk）保留类别、errno/winerror与直接cause，真实位置留诊断。原128文件分页顺序、64KiB分块、offset/hash/身份和接收/清理/journal链保持，不引入第二清单或传输实现。AST去除本片错误上下文及扫描guard后整个原模块一致，全部签名不变；这是作者自查，不称独立审查。
- 候选7项单测通过（0.55秒），完整ecbff8817 Git archive旧生产加相同最终测试为5 failed/2 passed（0.78秒），覆盖根/嵌套扫描、元数据、chunk打开、文件/目录链接和跨页/跨块身份、顺序、长度及hash。正式matrix候选5 passed/69.50秒、旧生产5 failed/64.88秒；由原产品消息准入、真实图/model_stream生成来源，真实路由/Depends/TraceMiddleware与ASGI驱动不同存储owner的fork，核故障阶段、头体request_id、双端aborted、无可见半成品、无路径记录、来源原字节/链接目标不变及新key重试后冷Saver原checkpoint/metadata/pending writes、历史assembly全字段/owner/request_hash。
- 端间传输复用原SourceForkOwnerTransport并经过生产来源捕获/分页/分块/释放，不伪造历史或复制回执；这是ASGI及传输替身集成，不代替真实Gateway、跨机HTTP、独立Backend重启、Windows或完整Web。准确命令、完整旧生产、原始输出/退出码与源码前后指纹位于 `out/tests/temp/fork_export_io_20261025/artifacts/`，verification001.json已核冻结及进程回收。首次runner前置检查发现任务TMPDIR未创建，未启动子命令，创建任务隔离目录后继续；Ruff首轮仅指出新测试import换行，已迁齐，不改产品行为。
- 目标接收的原生I/O、其它路径出口及全局门仍待核；2.2/7.2保持未勾，团队、成员、委派与协作继续TODO，不追加实现或专项验证。

- 最终共享三方合并后7项单测通过（0.64秒），正式matrix 5 passed/64.17秒；Ruff/compileall与OpenSpec strict 45 passed/0 failed。verification002.json核实登记的3784条后端/协议/harness/config/matrix路径前后未变、原始日志/实际退出码及进程组回收，未运行或构建的客户端不在本片冻结范围；共享独立suite顺序及原暂存区完整保留。


### 2026-10-25 fork 冻结产物目标接收（2.2/7.2 部分收口）

- 在原本机调用点统一位置检查、目录/open、write、flush/fsync、close及文件/整份artifact目录发布的storage错误出口；保留类别、errno/winerror与直接cause，真实位置留诊断。原stream由显式finally关闭，写入与关闭双错保留两份原因链；上下文不跨越来源transport的await，不改写网络失败/取消归属。原顺序、块校验、四路并发、marker最后接收、原清理及两端journal保持。AST去除本片局部错误上下文并恢复等价stream-with后整个原模块一致、签名不变；这是作者自查，不称独立审查。
- 候选9项单测通过（0.42秒），完整b5e272575 Git archive旧生产加相同最终测试为8 failed/1 passed（0.62秒）。覆盖mkdir/open/write/flush/fsync/close/目录发布和写入后关闭双错，核原cause/context、来源字节、原stream关闭与故障解除重试；来源ConnectionError对象身份及cause不被本机出口改写，原实现与候选均通过该归属回归。
- 扩展原唯一产品测试至13场景，复用原产品入口与断言；候选13 passed/146.00秒、旧生产8 failed/5 passed/137.05秒。模型状态由产品消息准入/真实图/model_stream生成，原不同存储owner经真实路由/Depends/TraceMiddleware和ASGI完成双端捕获/传输/中止；8项新故障覆盖receive_path、mkdir/open、写入、实际文件descriptor的fsync、关闭及文件/整份目录发布。核头体request_id、双端aborted、无路径记录/半成品、来源canonical不变和新key重试后冷Saver原checkpoint/metadata/pending writes、历史assembly全字段/owner/request_hash。整份目录场景明确等到marker文件目录同步后的第二次root同步才注入；关闭注入在真实流关闭后报错，不声称模拟了所有操作系统close失败语义。
- 准确命令、完整旧生产、原始输出/实际退出码、源码前后指纹和作者AST核对在 `out/tests/temp/fork_receive_io_20261025/artifacts/`，verification001.json已核冻结与进程回收。matrix预算因13场景扩为360秒，4GiB保护及产品期限不变。ASGI和原SourceForkOwnerTransport不代替真实跨机HTTP、Gateway、独立Backend重启、Windows或完整Web；其它路径出口及全局2.2/7.2仍待核且不补勾，团队、成员、委派与协作继续TODO，不追加实现或专项验证。

- 最终共享三方合并后导出/接收16项单测通过（0.90秒），正式matrix 13 passed/150.05秒；Ruff/compileall及OpenSpec strict 45 passed/0 failed。verification002.json核实登记的3785条后端/协议/harness/config/matrix路径前后未变、原始日志/实际退出码及进程组回收，未运行/构建的客户端不在本片冻结范围；原共享suite顺序及暂存区完整保留。


### 2026-10-25 fork 目标暂存与准备同步（2.2/7.2 部分收口）

- 原目标暂存创建、复制、失败清理与准备同步复用既有 storage 错误出口，保留类别、errno/winerror/cause及清理双错；普通错误不输出路径或内部 locator。创建碰撞保留既有目录；bottom-up同步拒绝缺失根、链接和非普通文件，扫描失败不再被静默忽略。原字节/hash/SQL与发布/journal顺序保持，不新增安装链或schema。
- 候选18项单测通过（0.37秒）；完整旧生产同18项全部失败，覆盖原生路径、扫描错误、缺失根和链接拒绝。正式matrix的7项产品失败/重试场景与2项原有准备/安装回归共9项通过（75.89秒）：关键历史由产品消息入口、真实图和model_stream生成，断言中止未发布、来源原字节、冷Saver checkpoint/metadata/pending writes与完整assembly/request_hash。首次将2项原产品回归直接交pytest，因缺少matrix watchdog在setup失败；已移到正式matrix，不改业务断言。原始失败输出保留。
- 原始命令、输出/实际退出码、完整源码指纹及完整旧生产位于 `out/tests/temp/fork_target_staging_io_20261025/artifacts/`。ASGI与冷Saver对象不代替真实Gateway、独立Backend重启、Windows或完整Web；全局2.2/7.2仍未勾，团队、成员、委派和协作继续TODO，不增加实现或专项验证。
- 最终共享三方合并后18项单测通过，正式matrix完整9项通过（69.77秒），Ruff/compileall通过；严格OpenSpec 45 passed/0 failed。完整旧生产产品7 failed/2 passed（69.67秒）。verification001/002.json核实前后源码冻结、原始退出码和进程组回收；作者AST自查确认6个未变函数及全部签名一致，去除错误上下文后复制和编排成功路径一致，不称独立审查。共享index保持原指纹。


### 2026-10-25 fork 目标目录发布、回滚与恢复（2.2/7.2 部分收口）

- 原发布、定点恢复和最终 rollback 清理在既有目标排他锁内接入同一 storage 错误出口（artifact=fork_target，publication/publication_recovery/publication_finalize），保留原生类别、errno/winerror、直接 cause 和安装/回滚双错的原 context；真实位置只留诊断。AST 去掉三个新增错误上下文后整个原模块一致，签名、rename/fsync/还原/清理顺序、journal/hash 与来源字节保持，不新增链路或 schema；这是作者自查，不称独立审查。
- 候选新增12项单测通过（0.35秒），完整3ff64773f Git archive旧生产12项均失败（0.63秒）。原唯一目标安装产品matrix扩为16场景：候选16 passed/139.03秒，旧生产7 failed/9 passed/131.74秒。7项新增覆盖原目标rename/同步、安装rename/同步、安装后回滚再次失败、恢复检查及最终删除；保留原9项准备/安装回归。历史由原消息入口/真实图/model_stream生成，核公开阶段与无路径、头体request_id、aborted/无可见半成品、来源canonical原字节、新key重试后冷Saver原checkpoint/metadata/pending writes、assembly全字段/历史owner/request_hash。
- 首轮Ruff仅指出新测试的__all__顺序与dict字面量；迁齐后通过，`test-ast001.json`规范化这两项语法后核最终测试与反例逻辑一致，不冒称文件字节相同。compileall通过；原始命令、日志/实际退出码、完整源码前后指纹、完整旧生产及作者AST核对在 `out/tests/temp/fork_target_publication_io_20261025/artifacts/`。最终共享树30项目标暂存/同步/发布单测通过（0.48秒），正式matrix完整16项通过（127.63秒），Ruff/compileall通过；45项OpenSpec strict全通过。verification001.json核实前后源码冻结、原始日志/实际退出码及进程组回收；冻结范围排除未启动或构建的客户端，原共享index保持不变。
- ASGI、原传输/创建owner及冷Saver不代替真实Gateway、独立Backend重启、Windows或完整Web；全局2.2/7.2继续未勾，团队、成员、委派和协作继续TODO，不增加实现或专项验证。
