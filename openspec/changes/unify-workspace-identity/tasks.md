## 1. 身份合同与迁移范围

- [x] 1.1 全仓检索工作区别名生成、`backend_workspace_id`／`remote_workspace_id` 及其直接和间接消费者，标出真实工作区引用与独立连接／导航身份。包括 camelCase、旧身份头、projection 生成器和不含旧字段名的持久引用；范围见 design 的迁移输入表。
- [x] 1.2 明确支持的 registry、配置及客户端旧版本，确认需迁移的实际表、字段和恢复格式；与多工作区、quick-access、扩展管理规划对齐，避免新增旧映射调用方。输入基线与正确 owner 不改写边界见 design；迁移实现及恢复验收仍在第 5／6 组。
- [x] 1.3 在现有受控身份入口完成注册前 UUID 获取、已有 UUID 保留及同原生 Gateway 根冲突拒绝，不增加第二身份源或占位 workspace_id。
- [x] 1.3a 身份文件完整写入后原子不覆盖发布，消除并发读取空文件；覆盖创建／绑定竞争、候选失败清理与真实 Gateway 损坏恢复。此子项不代表 1.3 的登记与根冲突验收完成。Windows 实机验证尚未完成：`hyf-win` 目标已有两项本地删除，快照激活拒绝，未覆盖目标改动。

## 2. 原生 Gateway 登记与本地请求链

- [x] 2.1 将本地工作区登记、唯一 root binding、活动选择和路由键迁为工作区 UUID 与 Gateway 归属，保持 owner／租约／registry revision CAS。
- [x] 2.2 迁齐本地管理和业务代理的工作区定位，统一路径与头部 UUID，拒绝冲突及错误归属，保留原 request ID 和管理授权。
- [x] 2.3 同步协议 descriptor／proto、公开 DTO、生成映射与校验、OpenAPI 和 SDK；更新破坏性协议版本，执行生成产物一致性检查和受影响类型检查。
- [x] 2.4 通过真实注册和 HTTP／URI 文件入口验证各层 UUID 相同、两工作区隔离、相同 UUID 不同根冲突及未授权根管理拒绝。

## 3. 共享 Backend 生命周期

- [x] 3.1 将 managed Backend 成员、generation 启动合同及 mount 校验迁为单一 UUID，保留唯一原生 Gateway root binding 和唯一进程句柄。
- [x] 3.2 迁齐 blockers、完整影响集合、force、配置 fan-out、移除、shutdown、preserve/adopt 与恢复，删除组生命周期中的工作区 ID 换算。
- [x] 3.3 经产品入口验证 sibling blocker、排空超时、显式 force、完整组恢复及辅助服务保留；缺失或错误 binding 必须拒绝部分 adopt。
- [x] 3.4 用受进程外保护的独立 Backend 子进程验证不同 home 争用同一隔离工作区仍失败、释放锁后可接管且 UUID 与业务历史保持不变。Linux 真实 Backend A／B／C 使用三个独立 home，A 由消息入口产生执行历史，B 被物理锁拒绝且 A 继续服务；A 退出后 C 保留 Session／main Thread、完整历史条目与身份文件，并从原检查点续跑，实际规范化模型请求前缀完全相同。矩阵 `integration-workspace-process-exclusivity`：1 passed／64.69s，沿现有 model_stream 精确回放，Ruff／Pyright 通过；不代表跨 Gateway 身份迁移或 Windows 实机验收。

## 4. 联邦与连接身份分离

- [x] 4.1 将 manifest、projection 和远程代理迁为来源 `(gateway_id, workspace_id)`，移除投影 ID 生成及远程 UUID 换算，保留 cursor、CAS 与 lease。
- [x] 4.2 迁齐连接配置对账和工作区关联，同一来源多连接只保留一个资源身份，删除连接不误删其他关联；保留 generation 排空和独立凭据边界。
- [x] 4.3 接通独立目标 Gateway 载体，迁齐远程生命周期、目录和 Generator 引用，不改变发送方身份头、管理授权或有界联邦规则。
- [x] 4.4 通过两个真实 Gateway 验证同 UUID 不同归属、多连接同来源、连接更新／离线、快照恢复及不兼容协议拒绝，检查请求 UUID 不经换算。

## 5. 客户端引用与持久恢复

- [x] 5.1 迁齐共享 owner、Gateway 请求、工作区选择、目录／搜索 scope、缓存键及相关客户端运行面，删除路由 ID 到 Backend UUID 的转换函数和调用方。
- [x] 5.2 迁齐编辑器 serializer、活动选择、导航／Generator 与用户视图态的持久引用；按 Gateway 归属保存同一 UUID。
- [x] 5.3 接入显式迁移恢复材料，准确升级旧客户端引用；无法确认的项逐项报告并保留可重试信息，不切到当前工作区或清空有效状态。
- [x] 5.4 运行受影响客户端真实类型检查及 Workbench 构建，以真实 Gateway 和隔离浏览器验证两工作区切换、远程选择、文件打开、搜索及重载恢复。

## 6. 存量迁移与单链路收尾

- [x] 6.1 实现只读预览与显式可恢复迁移，核验完整映射、owner、引用和未结算状态；仅按实际结构变化提升 schema，离线或歧义时保留原记录并阻塞发布。
- [x] 6.2 原子迁移 registry／metadata、root binding、managed_backends、活动选择、目录／Generator 和 user_view_state；保留有效 URI、canonical 正文、历史 hash 及 Session／Thread 身份。
- [x] 6.3 用受支持旧版本完整输入验证成功、重入、冲突、远端离线、写入中断恢复和维护窗口回退，检查迁移后真实重启与客户端恢复，不手工补齐产品应生成的关联。
- [ ] 6.4 迁齐后物理删除旧别名生成器、重复字段、换算接口、死代码和旧协议分支；完成全仓消费者核对、相关静态分析及定向回归，保留准确命令、源码版本与原始结果。所有测试使用既有矩阵或进程外超时／内存保护。
  - 2026-11-05 搜索元数据消费者：发现与冻结范围测试改用完整 Gateway／workspace owner，远端准备沿正式 projection 发布入口；保留相同 UUID 不同 Gateway、发现零启动、分页不改活动项／导航、重启后旧 owner 不重定向到同名新工作区的断言。发现补验 13 passed，冻结范围真实 Gateway 重启单项沿首批通过记录复用，源码指纹未变；两文件 Ruff／Pyright 零诊断。准确命令、失败归因与补验原件见 `out/tests/temp/identity_metadata_consumers_20261105/artifacts/final-index.json`；全仓其余消费者仍待核齐，本项不勾选。
  - 2026-11-05 名称／文件联邦搜索验收迁齐：外部后端登记和请求统一使用原生 Gateway／workspace UUID，移除测试中的路由别名及退役 DTO 字段；真实 Gateway 与双 Backend 1 passed／118.02 秒，覆盖重启续页、目录及 child 版本冲突、文件正文／UTF-16 列、实际读失败、停止目标和连接拒绝。原身份、顺序、游标、错误归属及输出字节预算断言保留，Session／child 状态仍由产品入口生成；Ruff／Pyright 零诊断。命令、原始输出和收据见 `out/tests/temp/identity_federated_search_20261105/artifacts/final-index.json`；本场景不代表跨 Gateway 实网或客户端验收。

  - 2026-11-05 辅助代理验收消费者迁齐：三份 HTTP／WebSocket／路由认证单测使用原生 Gateway／workspace UUID，远端准备沿正式 projection 发布；保留目标冲突拒绝、发送方头剥离、联邦授权、request_id 及引用结清，原 RuntimeError 边界在合法 owner 前提下验证原 cause、503 和零转发。三文件 75 passed，Ruff／Pyright 零诊断，源码与生产依赖指纹一致；原件见 `out/tests/temp/identity_auxiliary_proxy_20261105/artifacts/evidence-index.json` 及其 `static-analysis-2026-11-05T00-56-21-805Z/evidence-index.json`。本批只迁单元验收，不代表真实网络或客户端通过，6.4 仍未勾选。
  - 2026-11-05 Gateway 最终消费者切片：真实 Terminal 管理、原生目录绑定、导航、三类搜索、旧格式 Generator 导入及健康／诊断选择器使用唯一 UUID 与明确 Gateway owner；导航 HTTP 替身删除第二个 UUID 入参，两处调用方全部迁齐，生命周期单测 36 passed。目录／导航各 1 passed，搜索 4 passed，Generator 1 passed；健康／代理环境隔离先通过 2 项，按现行 UUID 校验合同修正最后一个选择器后仅补该项 1 passed。各切片最终 Ruff／Pyright 零诊断，保留 registry 原件、request_id、冻结范围、重启／重试及错误归属断言。Terminal 两个真实场景分别通过；第二项失败由本正式工作区九天前遗留 Backend 持续覆盖资源账本导致，只核实并终止该测试持有的两个孤儿进程后补验，原现场与清理回执保留。准确命令、源码指纹、失败与补验原件见 `out/tests/temp/identity_final_consumers_20261105/artifacts/`；Web／Electron 其余消费者尚在迁齐，6.4 继续未勾选。

### 2026-11-04：6.1 只读登记身份预检切片

- 新增显式 `python -m configs.boxteam preview-workspace-identity`，只读 registry 13／Gateway SQLite 18／connection record 4；不安装配置、不创建缺失身份、不启动目标。运行时与维护入口复用唯一 registry 快照投影，metadata 与行使用同一读事务，避免并发发布时混合 revision。
- 本地身份经既有目标侧只读入口核验，映射保留登记 Gateway、旧别名、原生 Gateway／UUID 和连接；重复 UUID、错误归属、父链、成员、活动引用及未结算控制状态明确阻塞。远端缓存声明不算验证成功，保留联邦核验 TODO；`apply_available=false`，完整引用迁移、恢复材料和原子发布仍未实现，6.1 不勾选。
- 独立审查发现空库／损坏库的 SQLite 错误会越过 CLI 诊断出口；已显式纳入同一错误出口，并以真实 CLI 验证 JSON 归属、非零退出及原件不变。最新定向 19 passed／8.78s，Ruff 与 Pyright 退出 0；更广 registry／配置回归日志与 XML 为 175 passed／2 skipped，但外层进程句柄丢失，未取得该次精确退出码，不表述为完整退出码验收。原件位于 `out/tests/temp/workspace_identity_preflight_20261104/artifacts/`；本轮收尾指纹、独立审查及严格校验见 `out/tests/temp/2026/11/04/openspec_parallel_01/root/artifacts/`。

### 2026-11-04：6.1 导航持久引用预检切片

- 导航 owner 暴露并复用唯一只读节点读取与图校验，格式 1 之外明确拒绝；普通导航和维护预检不再各自解析。预检不调用会同步保存的 `list_tree`，按原 node ID 输出工作区字段的候选归属，不改 folder、父链、显示名或原文件。未知引用、重复节点、父链循环、错误格式及没有 registry 的孤立引用均阻塞，不以自动删节点掩盖迁移缺件。
- 产品导航 owner 生成的本地及远端引用覆盖同 UUID 不同 Gateway，并验证文件原件不变；已有规范 URI 和业务正文不进入此映射。配置／导航定向 41 passed／8.10s，Ruff、实际项目解释器 Pyright 和严格规范校验退出 0；准确耗时以原始日志为准，原件与源码指纹位于 `out/tests/temp/identity_navigation_preflight_20261104/artifacts/`。本切片不包含 user_view_state、旧 Generator、客户端恢复与可恢复发布，6.1 保持未勾选。

### 2026-11-04：6.1 用户视图持久引用预检切片

- 在原 SQLite 只读事务内读取 `user_view_state` 的完整原主键，与 registry 共用同一快照；仅按登记旧 key 精确关联，保留用户、Session 及所有视图业务字段。不能根据 Backend UUID、显示名或当前活动项猜测归属；未登记引用和缺少 registry 的孤立行明确阻塞。
- 原用户／视图 owner 生成多用户、多 Session、同 UUID 不同 Gateway 及多连接输入，核对原主键、全库逻辑不变与并发发布时的快照一致性。定向 97 passed；Ruff、项目解释器 Pyright、编译均退出 0，源码指纹与准确命令、原始日志、实际退出码位于 `out/tests/temp/identity_view_preflight_sol_20261104/artifacts/verification.json`，集成时指纹一致。
- 本切片未实现 apply、引用写迁移、远端身份核验、旧 Generator、客户端恢复材料或可恢复原子发布，不提升 schema；6.1 保持未勾选。

### 2026-11-04：1.3 本地 UUID／根冲突拒绝切片

- 原 registry 准入、冷恢复和代次校验共用本机 UUID／物理根约束；同原生 Gateway 已登记 UUID 的不同根在发布前明确拒绝，保留原登记、活动项、代次及身份文件。远端投影不套用本机根约束，原受控 target 更新仍沿原 owner 流程。
- 真实 Gateway 的 managed／external 注册均拒绝复制身份形成的根冲突，原工作区继续健康；连同已有身份损坏恢复共 3 passed／99.70s。定向 registry／managed 73 passed；扩大相关组 277 passed／13 failed，失败均为两个辅助用例不同根共用假 UUID，修正 fixture 后分别 12 passed 和 1 passed，未放宽错误归属、顺序或状态断言。
- 静态对照完整原基线无新增诊断：生产切片存量 Pyright 42／Ruff 27，相关测试存量 Pyright 16。精确命令、实际退出码、失败归因与最终指纹见 `out/tests/temp/identity_root_binding_20261104/artifacts/verification.json`；尚未验证 symlink alias、受控换根及 generation 冲突实际触发。本切片仍使用待迁齐的原路由别名，1.3 保持未勾选，不代表完整身份统一。

### 2026-11-04：6.1 旧 Generator 冻结导入预检切片

- 显式 Generator 导入和身份预检共用唯一只读输入校验，按原 bytes、冻结映射／owner／来源、source_hash 和精确回执区分 pending／imported／invalid；未结算或失配阻塞身份切换，已退休历史不按当前登记重绑。预检不写文件、不导入、不发网络请求，完整迁移仍 `apply_available=false`，6.1 不勾选。
- 独审指出 snapshot 发布前中断留下半备份时，输入变化可能先导入新 hash 再报告旧备份失败。4 个反例先复现；原链路现先只读准备整批 bundle，所有半备份必须命中同一 source_hash 且已有归档 bytes 一致，再执行缓存结果。新增 preview／apply × 原 bytes、映射、归档冲突、定义重建失败共 8 场景，核零 POST、无新备份／snapshot／receipt、原件及原库不变；原输入不变的中断、丢应答、退休中断重入继续成功。
- 最终 66 passed／18.24s，Ruff、Python 3.12 Pyright 退出 0，独立只读复核确认上述缺口闭合。准确命令、原始输出及源码指纹见 `out/tests/temp/identity_generator_unsealed_sol_20261104/artifacts/p2_verification.json`；基础预检证据保留在 `out/tests/temp/identity_generator_preflight_sol_20261104/artifacts/verification.json`。HTTP 边界采用明确 MockTransport，未声称真实目标 HTTP 导入或完整可恢复迁移已完成。

### 2026-11-04：1.3 已登记根的注册身份保护

- 原 root-binding owner 统一负责本机注册身份：已有根只能只读核验 UUID 并与原登记精确匹配；真正新根才允许沿原入口建目录、初始化身份。managed、external 和默认工作区启动共用该入口，移除各自初始化调用；已登记根丢失时，即使要求创建目录也不能重建，更不能因重复注册生成新 UUID 后才报错。
- 原行为的真实 CLI／入口反例为 3 failed／1 passed；修复后注册、冷恢复、默认工作区改名与新／已有身份共 114 passed／19.76s。真实 Gateway 文件 4 passed／130.19s，新增场景逐一核 managed／external 的身份缺件与 UUID 改写拒绝、原文件和 SQLite 登记／活动项／revision 不变，修复测试注入后原工作区保持健康；缺件保留原 Trace 500，UUID 冲突为 workspace_create 400。
- 最终静态对照完整原基线：Pyright 91／91、Ruff 27／27，无新增诊断；独立只读复核确认写入与调用顺序，9 份 Python 内存编译通过。准确命令、指纹及原件见 `out/tests/temp/identity_known_root_20261104/artifacts/verification.json`。symlink alias、并发注册和 Windows 未作专门验收；现有路由别名与完整迁移仍属后续任务，1.3 不勾选。


### 2026-11-04：6.1 远端原生身份显式核验切片

- `preview-workspace-identity --verify-remote` 使用同一冻结登记中的连接与原远端路由、已有联邦凭据和短期 SSH 隧道，复用原 discover 链路；默认预检继续离线。不配对、启动目标或改登记，连接记录解析与 registry 冷恢复统一。报告不包含 SSH 参数或凭据，诊断日志沿显式 home 的共享日志布局。
- 原 `/federation/workspaces` 增加显式只读核验选项，复用既有 `initialize=False` 身份 CLI 核验原生根并比较 registry revision，之后才标明 `identities_verified`；普通发现不增加磁盘探测。核验方严格要求同 Gateway／协议及布尔确认，旧服务仅返回缓存不能通过；原路由消失、UUID 改变、重复身份、凭据缺失／过期／归属错误均阻塞，不能按同 UUID 另一路由重绑。
- 独审两处 P2 已闭合：Gateway 根统一展开规范化；发现／隧道健康检查被取消且关闭再次失败时保留原取消对象、以原因链保留清理错误，不继续下一连接。公共同步 worker 的原生失败优先合同保持，原 11 项用例通过。
- 相关登记／联邦／预检回归 138 passed，最终远端及原生 proof 26 passed，公开合同 62 passed（场景重叠不相加）。原生 HTTP 使用 ASGI、真实认证／DTO／registry／只读 CLI；远端 SSH 和 HTTP 使用明确替身，不代表真实双 Gateway／SSH 或 Windows 验收。协议格式／lint／build／breaking／生成一致性、实际 sourceTransform 类型检查及完整 Workbench 构建均退出 0；静态与完整 HEAD archive 对照无新增诊断，Pyright 60／60、Ruff 12／12 为存量。
- 准确命令、实际退出码、源码指纹及原始日志：`out/tests/temp/identity_remote_preflight_20261104/artifacts/verification.json`。完整 registry／持久引用／客户端迁移与可恢复发布仍未完成，`apply_available=false`，4.4／6.1 保持未勾选。

### 2026-11-04：2.2／4.3 独立目标 Gateway 载体切片

- 普通工作区 HTTP、辅助 HTTP／WebSocket、资源导航及上下文 read／search 共用 `X-BoxTeam-Target-Gateway-Id` 的严格身份解析。canonical 工作区路由可按该归属区分相同 UUID 的不同 Gateway；明确 URI 或已选工作区与目标头冲突时，在初次租约和上游调用前拒绝。远端出站按原登记重建目标头，本跳发送方仍用 `X-BoxTeam-Gateway-Id`；本机 Backend／辅助服务不接收目标头，不改变认证或有界联邦规则。
- 私有 HTTP 头部构造器接收本请求已读取的本跳身份，移除新增链路中的重复身份文件读取，迁齐直接及参数化消费者。旧导航歧义 fixture 改为同 UUID、同物理根的重复路由，保留原 409 和不回退 active 的断言，不放宽先前根冲突约束。
- 最新主线定向 61 passed；较广代理／导航批次的其余通过结果按未变源码复用，辅助批次 205 passed、最终新增场景 35 passed，重叠场景不相加。扩大检查曾出现一次未改流取消用例超时；完整 HEAD archive 单例及组合均通过，当前组合也通过，保留原始失败，不据此改公共清理逻辑。Ruff／Python 3.12 Pyright 与完整 HEAD archive 对照分别为 7／7、25／25 项存量诊断，新增为零；独立只读复核未发现本切片问题。准确命令、退出码、指纹及全部原始结果见 `out/tests/temp/target_gateway_carrier_20261104/artifacts/verification.json` 和 `out/tests/temp/auxiliary_target_gateway_20261104/artifacts/verification.json`。
- 本轮仍保留待整体迁移的 alias／Backend UUID 合同，未实现复合 registry 键、远程生命周期／目录／Generator 与客户端迁移；无真实双 Gateway／SSH 或 Windows 新验收，2.2／4.3／4.4 保持未勾选。

### 2026-11-04：2.2／4.3 查询出站收敛与双 Gateway HTTP 验证

- 注册表 `target_request_headers` 统一健康、领域搜索、会话目录镜像、资源目录及间接 Generator 导入的工作区定位／凭据；远端目标头取自原登记连接。物理删除 `_target_headers` 和两套目录凭据拼装，发送方与 request_id 仍由各调用边界提供，本机查询不带目标头。无 schema 或公开 DTO 变更，完整 UUID／复合键迁移仍未完成。
- 主组 235 passed；间接导入补查发现旧单测 fixture 使用 Mock registry，已迁为真实 registry，登记与旧迁移输入分目录，离线用原 owner 移除登记，原备份／顺序／丢应答断言均保留，7 passed。Ruff 全部通过；修改文件 Pyright 与完整 HEAD archive 比较为 73／76 项存量诊断，新增零、消除三项，导入测试单独为零。独立只读复核确认查询和间接消费链；准确命令、源码指纹、原始失败与最终结果见 `out/tests/temp/target_query_headers_20261104/artifacts/verification.json`。
- 两个真实 Gateway＋两个真实 Backend 经产品入口创建独有会话、注册远端并发现 owner；相同 UUID 按目标分别读取独有会话，客户端伪造 sender 被覆盖，A→B 为 target=B／sender=A，B→Backend 剥离 target／sender=B；错误、空和歧义目标不进入下游，不改变 active，request_id 全链一致。最终精确源码运行 1 passed／51.65s、退出 0，16 份源码首尾指纹一致；新测试 Ruff／Python 3.12 Pyright 均通过。原混合源码窗口运行另存，不作最终版本证据。
- 实网测试只替换 SSH 执行为原 pairing CLI 的隔离本机执行、隧道为 loopback URL；注册、凭据解析、HTTP 发现／投影／路由仍是真实链路。已登记 `integration-target-gateway-routing`（180 秒／4GiB），原 runner 列表校验通过，业务通过来自受相同保护的直接 pytest。原件见 `out/tests/temp/target_gateway_network_20261104/artifacts/verification.json`，正式产物在 `out/tests/integration/gateway/test_target_gateway_routing/`。本轮不代表真实 SSH／跨机、UI、辅助服务网络或 4.4 完整迁移验收，2.2／4.3／4.4 仍未勾选。


### 2026-11-04：5.3／6.1／6.2 显式维护与客户端恢复

- `migrate-workspace-identity` 消费完整旧 registry 13／SQLite 18 输入，沿唯一备份、CAS 与启动屏障发布 schema 19、registry 14 和导航 2。迁齐活动选择、根归属、共享组、远端连接关系及用户视图；相同来源多连接合并，跨 Gateway 同 UUID 隔离，导航／视图冲突保全原件并拒绝。继续及回退只用原冻结计划，新写入或备份变化拒绝覆盖。已结算 Generator 冻结正文／回执保留，未结算输入继续阻塞。
- 发布完成后导出标准 UUID 的恢复材料；工作台显式入口可一次选择多个 Gateway 的材料，逐项恢复展示记录、编辑器及同事务队列，保留 Blob、操作身份、unknown 回执、顺序和独存高水位。缺来源、歧义、写入中断或旧窗口阻塞均保留原件，不增加运行时别名表。
- 维护／中断／回退／来源冲突 14 项通过，原生身份 ASGI 5 项通过，预检与 Generator／远端核验 82 项通过；另以认证 HTTP MockTransport 贯通真实 CLI 核验、发布、导出与生产 registry 重开。原日志维护相关 62 项通过。准确命令、原始输出和指纹见 `out/tests/temp/identity_convergence_20261104/artifacts/` 及 `out/tests/temp/identity_client_recovery_20261104/artifacts/`，不是实机 SSH 证明。
- 客户端恢复 10 项／85 断言、受影响原测试 41 项通过；实际 sourceTransform 类型检查及完整 79 扩展 Workbench 构建退出 0，真实 CLI 材料已由客户端 parser 消费。最终新增原件前缀为 `identity-multi-gateway-client-final-1793797585131750584`、`identity-multi-gateway-types-1793797600886220974`、`identity-multi-gateway-build-1793797718841380477`、`identity-real-cli-client-contract-1793797639022454499`。独立 Web tsconfig 的既有测试类型／lib 问题未改环境；尚无本切片浏览器重载、真实远端及 Windows 完整验收，5.4、6.3 与归档保持未完成。

### 2026-11-04：4.2／4.4 多连接请求准入与显式解绑

- 原路由 owner 在发布请求／流占用之前完成目标及唯一连接解析；同一来源有多连接时仍明确拒绝，不再遗留虚假占用阻塞用户解绑。普通和流两参数在修复前均复现泄漏，最终 registry 文件 8 passed／0.69 秒；保持相同 UUID 的本机租约、revision 与原资源归属。
- 两个真实 Gateway＋两个真实 Backend 经产品注册同源第二连接，只保留一个工作区资源身份；歧义请求不进入下游，显式解绑第一连接后原 URI 恢复读取远端独有会话，request_id、目标／发送方及活动归属保持。最终 1 passed／62.05 秒，退出 0；仅 SSH 执行与隧道沿原测试明确替身，不代表真实 SSH、连接更新／离线、快照恢复或 4.4 全量验收。
- 共享 skill-source fixture 迁为既有业务 UUID 和显式 Gateway 归属，移除已下线的 backend_workspace_id 参数，未绕过 managed_gateway 或认证。依赖它的 Agent/model_stream 文件错误双场景由 URI 切片重验 2 passed／88.31 秒。四个修改文件 Ruff 通过；Pyright 对完整 ce41fc18a 归档按文件／规则／完整消息及重数核对，240／243 项既有诊断，新增 0、消除 3，两个 Gateway 测试文件零诊断，不称全库类型全绿。准确命令、源码指纹、原始反例、实际退出码与比较见 `out/tests/temp/identity_convergence_20261104/artifacts/identity-route-admission-verification.json`；宽任务继续未勾选。

### 2026-11-04：4.1／4.2／6.4 投影测试消费者迁齐

- `test_projection_identity.py` 移除旧投影 ID 生成和双 UUID 断言，沿现有注册／配置／发现入口验证两个 Gateway 的同一 UUID 按复合 owner 隔离，以及同来源双连接合并为一个资源、歧义准入不泄漏占用和显式解绑。保留完整 SQLite 快照、活动归属、cursor 跳跃／回退、重复 owner 拒绝、stale CAS、租约／计数及冷恢复证明；HTTP／SSH 明确使用原单测替身。
- 最终定向 4 passed／0.96 秒，Ruff、Pyright 和 diff 检查通过，旧字段类型诊断 7→0；准确命令、源码指纹、实际退出码和原失败保留于 `out/tests/temp/identity_projection_consumers_20261104/artifacts/` 的 `*-verified.*`。首轮新增断言误要求 CAS 前置拒绝写 journal、连接容器保持未约定迭代顺序，已按实际合同改为 journal 精确不变及 connection_id 全量值比较，原 SQLite payload 与连接成员顺序断言仍保留。生产指纹未变；另外三组旧消费者尚待迁齐，生产生成器未提前删除，不据此勾选宽任务或宣称真实网络验收完成。

- 后续 registry 消费者定向 35 passed／1.41 秒，保留 SQLite 重开、cursor、CAS、排空和完整组归属断言，远端登记沿正式 snapshot/batch 发布。真实 Gateway 导航 CRUD、循环／非空拒绝与重启恢复 1 passed／51.54 秒，核对注册 UUID、目标端身份文件和嵌套 Gateway owner；下游使用原 HTTP 替身。两份测试 Ruff、Pyright 均退出 0，原件位于 `out/tests/temp/identity_local_consumers_20261104/artifacts/` 的 `registry-*-fixed-*`、`navigation-*-owners-*`；registry 最后仅调整空白并核 AST 相同。本轮不代表客户端或 Windows 验收，6.4 保持未完成。

- 联邦及转发消费者迁齐后物理删除 `build_projected_workspace_id`，代码调用方清零。公开转发六接口沿原 `resolve_workspace_key` 解析目标 Gateway，保持原管理授权；同 UUID 的不同 Gateway 隔离，路径/头冲突在 manager 前拒绝。原错误对象、系统码、持久文件不变与清理重试断言迁到实际拥有转发删除的公开入口，manual CRUD 越过 `remote_projection` owner 仍拒绝。三份定向测试 131 passed，公开 schema 1 passed；最后成功回执仅增加非空断言，export 单项复验通过。四文件 Ruff 通过，联邦模块保留 5 条原有 TRY004；Pyright 两份生产文件无诊断，三份测试仍有 18 条替身/object/winerror 等类型诊断，不表述为全绿。准确命令、指纹、原始反例与结果在 `out/tests/temp/identity_federation_consumers_20261104/artifacts/`。连接删除协调转发清理仍未验收，本轮不代替实机 SSH/Web 或完整 6.4。

### 2026-11-04：恢复与本地别名生成器收口

- 恢复失败事实沿原 owner 补齐连接写入范围，未放宽 SQLite 授权；rename/restore 迁齐 UUID、完整 owner 和单一成员身份。删除旧模块后的定向测试 67 passed／17.22 秒，Ruff 通过。两份测试 Pyright 零诊断；`routes.py` 有 44 条 mixin 成员诊断，已对照改前同方法源码，未执行旧 HEAD 类型检查。修复提交 `243ebcdf5`，原件在 `out/tests/temp/identity_restore_consumers_20261104/artifacts/`。
- 全仓迁齐本地生成器直接及间接消费者后，物理删除 `workspace_ids.py` 三个函数与 registry 再导出。14 个装配／消费者模块实际导入通过；SSH 装具原单测 17 passed。双工作区真实路由与 Session UUID、托管工作区重启恢复、跨工作区上下文工具三个场景分别通过；其中恢复／上下文合跑 2 passed／130.69 秒。旧恢复测试的重复 start 已删除，由创建结果 ready、generation 与完整成员断言承接。一次复验端口占用后未清理未知进程，确认端口可绑定才补跑失败及未执行场景；失败原件保留。
- 身份准备真实权限拒绝、executable 启动失败及 actor 代理三项 3 passed／95.19 秒，五份消费者 Ruff／Pyright 均通过；未预建权限场景身份，明确验证无半登记 target／owner。其余启动失败参数、完整 Launcher/Web、Docker SSH 和真实 Provider E2E 未重跑。原件在 `out/tests/temp/identity_remaining_clients_20261104/artifacts/`；主线程命令、指纹、端口观察和结果在 `out/tests/temp/identity_routing_consumers_20261104/artifacts/`。主线程五文件 Pyright 仍有 22 条测试诊断，逐条源码与改前相同，未运行旧 HEAD 完整类型检查；生产 facade 与目标侧只读 helper 无诊断。独立只读审查未发现本批漏迁，不据此勾选完整 6.4。

### 2026-11-04：4.2 连接删除与转发清理闭环

- 公开连接 DELETE 在清理前共用 registry 的 owner／活动引用校验，清理成功后沿原 remove 复查并解绑。原转发对账同时识别已消失 owner、已失效连接关联和待删连接，不改绑另一连接；同来源其它有效连接及另一 Gateway 的同 UUID 转发保留。
- 关闭失败保留原进程与定义；原子保存失败恢复定义内存并保留已停止运行态，原 DELETE 可重试且不重复关闭。公开入口保留未知 404、配置 owner 403、活动请求／流 409，核对 request_id、原异常因果、定义文件和 SQLite 冷恢复。
- 四模块批次 176 passed／2 failed，失败均在原固定端口 41236 可用性检查；保留失败原件，确认真实 bind 可用后只补跑两例，2 passed。64 个新增参数场景全部通过。验证为 ASGITransport 进程内公开路由、真实 registry／SQLite／定义文件及 socket listener，进程关闭使用受控替身，不代表真实 HTTP 服务或 SSH 验收。Ruff 剩余 7 条与实际改前相同；Pyright 50 条均逐项归因于改前源码，未跑旧类型基线，不称类型全绿。最终源码与检查指纹一致，主线程复核差异；准确命令、原始结果及归因在 `out/tests/temp/identity_connection_cleanup_20261104/artifacts/connection-cleanup-final-evidence-1793809342971461555.json`。4.2／4.4／6.4 的整体迁移验收仍未完成。


### 2026-11-04：1.3 canonical 注册冲突链路收口

- 注册身份读取和原 registry 发布共用同一个 UUID／物理根检查，始终按本机完整 owner 查询；managed 在通用重复判断前、external 在健康探测前拒绝异根冲突，不新增身份源或兼容链。原 upsert、冷恢复与 generation 的发布前校验保持；不同 Gateway 的同 UUID 不互相阻挡。
- root-binding 最终 20 passed／11.48 秒；复制身份的新根读取反例修复前 4 failed，修复后全部通过，保留 JSON／SQLite、初始化开关、原身份与登记不变、冷恢复损坏精确拒绝。真实 Gateway 的 managed／external 两个重复 UUID 场景通过；原登记身份缺件／改写及失败恢复两个场景补跑通过（90.31 秒）。managed 原组 34 passed，唯一被并行导入中断的参数补跑 1 passed；不将重复执行相加。
- 并行 itemized 改动曾暂时删除仍被引用的函数，导致身份 CLI 导入失败；原始失败保留，确认导入恢复后才补跑失败项。最后产品补跑中两份 assembly／request 源码仍发生变化，本批身份与注册源码未变，因此这是共享工作区观察结果，不声称全仓冻结快照验收。Ruff 通过；本批三文件 Pyright 与改前按文件／规则／完整消息及重数核对为 148／148 条存量诊断，新增零；root-binding 测试单独零诊断。
- 准确命令、指纹与原始结果见 `out/tests/temp/identity_registration_current/artifacts/` 和 `out/tests/temp/identity_root_binding_current/artifacts/verification-precise-1793822445037641024.json`。本项不代表完整 2.4／3.3／6.4 验收；symlink 同物理根、授权释放后搬根、generation 冲突与 Windows 本轮没有专门新增验收，后续对应范围继续保留。


### 2026-11-04：2.2／4.3／6.4 目标代理与协议消费者迁齐

- 目标代理测试移除旧本地／远端别名与重复 UUID 字段，使用正式快照发布连接关联；三种 Gateway 的同 UUID 保持独立。裸 UUID 只选本机，即使活动项是远端也不回退；错误目标、active／URI 冲突保持精确拒绝、零下游和零租约，并核对发送方、目标、request_id 及各自认证。
- 真实 Gateway＋独立 HTTP 协议替身迁齐 canonical UUID、完整 blocked owner／活动选择及显式目标头。保留 HTTP 拒绝、非法 data、诊断原文、完整回执及原错误类别／日志断言；生命周期四动作的出站路径与 UUID 完全一致。远端投影、回执属于协议装具，不作为真实远端注册或 blocker 产生过程的验收。
- 单元最终 23 passed／3.14 秒，Ruff／Pyright 均通过。HTTP 分两组 2 passed／63.51 秒、6 passed／172.87 秒，源码首尾指纹稳定；纠正装具投影 URL 元数据后，仅补直接读取该字段的删除场景 1 passed／30.44 秒，新增精确 URL 断言，其余不受影响的协议结果复用。重复场景不累加。HTTP 测试 Ruff 通过；Pyright 剩两条 Popen bytes／str 存量诊断，对应装具函数与改前 AST 完全相同，未运行旧版类型基线，不声称类型全绿。
- 原始命令、实际退出码、失败与最终指纹见 `out/tests/temp/identity_target_proxy_consumers/artifacts/verification-final-1793823103774822530.json` 和 `out/tests/temp/identity_federation_protocol_current/artifacts/`。本轮未改生产、前端或共享运行 fixture，不代表真实 SSH、Windows、完整客户端和 2.2／4.3／6.4 全部验收。


### 2026-11-04：2.2／2.4 文件代理的统一身份与真实目标验证

- Gateway 导航及文本修改路径统一公开 `workspace_id`，移除旧 Backend 参数；真实 OpenAPI 核对路径参数，公开结构 validate-only 退出 0。URI 选择目标与当前界面工作区上下文分开：无工作区路径的 URI 入口沿 URI owner 分派，显式路径与头冲突仍在准入前拒绝，目标 Gateway、授权和 request_id 保持。
- 相关代理、流、上下文与公开合同集中 241 passed／30.02 秒；导航短检查单独记录。最新真实 Gateway 双挂载经产品注册／组重启产生身份，核身份文件、HTTP 路径、头和 URI 的 UUID 完全相同；相对／绝对／URI 并发读写隔离，跨工作区 URI 导航、未注册目录编辑与路径／头冲突均通过，1 passed／66.61 秒。此前真实未授权 peer 拒绝项通过且无登记／目录／进程副作用；同 UUID 异根冲突复用 1.3 的真实注册证据。首次文件验收的 URI／上下文头误判失败原件保留，修复后仅补失败项。
- 原始命令、实际退出码与源码前后指纹在 `out/tests/temp/identity_file_proxy_current/artifacts/`：`final-proxy-contract-unit-1793824492530125704`、`final-openapi-validation-1793824553303697765`、`product-owner-admission-1793823934739974081`、`product-file-uri-fixed-1793824718895518575`。最终文件产品检查追踪源码首尾一致；本切片不代表完整客户端、真实 SSH、Windows 或所有身份恢复验收。
- Root 四个路由／测试文件静态零诊断；共享代理保留 Ruff 7 条／Pyright 6 条，与改前完整该文件在当前依赖下按规则、完整消息和重数比较无新增，不称全库静态全绿。导航及准入测试迁移证据另在 `identity_resource_navigation_current` 与 `identity_proxy_admission_current` 临时产物目录；完整 2.1／2.3／6.4 继续保留未完成。

- 导航最终短检查 51 passed／2.76 秒；URI 业务归属复用既有 Gateway ID 校验，非法归属在 owner／RPC／租约前明确 400，合法未登记归属仍 404，已登记远端共享 UUID 继续成功。首次真实导航的非法 authority 500 原件保留；修复后完整主集成 1 passed／74.77 秒，覆盖真实双工作区注册激活、工具结果 URI、Session 删除隔离与失败修复，源码首尾一致。原件 `out/tests/temp/identity_resource_navigation_current/artifacts/navigation-owner-fixed-integration-1793824974306682510.*`；本场景不包含真实模型执行。
- 最后路由消费者补验及独立十文件差异复核完成；导航生产 Pyright 零诊断，两测试保留 11 条与改前比较无新增的存量诊断。身份 OpenSpec strict 退出 0。最终源码指纹与准确命令索引在 `identity_file_proxy_current/artifacts/final-evidence-index.json`，独立复核原件在 `identity_proxy_admission_current/artifacts/identity-ten-owned-review-index-1793825005852749713.json`。

### 2026-11-04：2.1 登记身份与冷恢复收口

- 冷恢复在构造新建对象前要求持久 owner、namespace、generation 和连接集合真实存在；generation 非字符串在原准入入口拒绝，缺件不再被新建默认值替代。新建、owner 授权、root binding、revision CAS 和租约仍沿唯一登记链路。
- 身份恢复 53 项、健康／查找 100 项通过；本地登记／根／owner 与恢复集中检查 129 passed，另一个配置迁移重入用例失败单独处理，不算本项成功。真实 Gateway 经产品注册、激活后重启恢复 1 passed／107.80 秒，源码首尾一致；复合活动归属、原错误 cause、JSON／SQLite 原件和租约断言保留，独立六文件只读复核未发现问题。
- 准确命令与原始结果在 `out/tests/temp/identity_registry_recovery_root/artifacts/`、`identity_registry_recovery_current/artifacts/`、`identity_registry_read_consumers_current/artifacts/`。Root 四文件 Pyright 完整 HEAD 基线／当前 108／108、Ruff 1／1，均无新增诊断；代理三测试静态零诊断。未将该证据扩展为 Windows、真实远端、客户端或完整 6.3／6.4 验收。

- 6.3 重入补充：已完成身份迁移只返回冻结回执和导出恢复材料，不重新锁定数据库或发布旧快照；正常 schema 升级、后续视图写入不再触发旧 digest 误判。首次／中断发布、显式回退仍用原 CAS／备份／屏障，后续写入仍拒绝被旧备份覆盖；恢复输出冲突保全原件。参考 Copilot `sessionStore.ensureSchema` 的已完成版本门，未运行参考仓库测试。直接迁移四文件组 103 passed，Ruff 通过，Pyright 7／7 为既存测试诊断、生产零诊断；准确命令、原件和指纹见 `out/tests/temp/identity_completed_migration_current/artifacts/verification-1793826712816757004.json`。真实重启及客户端恢复的整体 6.3 继续未勾选。

### 2026-11-04：2.3 公开协议消费者收口

- Gateway／Workspace 公开绑定统一为 v2，联邦协商为 2，旧重复身份字段保留为 reserved；现有 Python／Node／Web 绑定和 OpenAPI 共用 descriptor 校验。迁齐目录结构反例和快照两层代理装具，远端只通过正式快照发布连接关联，不再手填旧别名或 runtime 字典；每跳核 UUID、Gateway、request_id、查询参数及活动 owner 不变。联邦列表的序列化装具补带真实隔离本地凭据，保留生产授权。
- 生成一致性、真实 sourceTransform 类型检查和完整 79 扩展 Workbench 构建退出 0，客户端及生成源码检查前后一致；构建期间只有并行测试文件变化。Node／Web 往返 5 passed，目录合同 34 passed，联邦真实 ASGI JSON→Python 绑定→两套 JS 消费者 2 passed，快照 7 passed；三个修改测试 Ruff／Pyright 零诊断。
- Python 扩大回归共收集 262 项，原运行在完成 216 项后触发 90 秒外层超时（退出 124），不称整组全绿；余下联邦 10 项通过，两个认证装具失败修正后通过，目录组沿上述独立结果复用。原命令、失败、补验、收集顺序和源码指纹保留于 `out/tests/temp/identity_registry_recovery_root/artifacts/`，快照最终索引为 `out/tests/temp/identity_snapshot_contract_current/artifacts/verification-index-1793827109683158503.json`。该证据不代替真实双 Gateway、浏览器交互或 Windows 验收，5.4／6.4 继续未完成。

### 2026-11-04：3.1 共享 Backend 成员合同收口

- shutdown 装具迁为双 UUID 成员、唯一 owner／Backend 句柄，保留每成员辅助服务、关闭顺序、失败重试及未发布候选清理。启动入口验证完整两份 mount、generation 和原入口产生的身份文件，不保留别名／重复 UUID 字段；合法的目标侧 `resolve_backend_workspace_id` 仅读取原生 UUID，不是旧身份换算。
- 两文件与直接 preserve／restore／adopt 组 9＋26＋31＝66 项通过；Ruff 通过，Pyright 6→4、新增零，剩余为既存诊断。准确命令与原件见 `out/tests/temp/identity_runtime_consumers_current/artifacts/verification-1793827324622743603.json`。
- 真实 Gateway 经产品入口组建共享成员后，四种 sibling 生命周期请求都返回完整组 blocker，过期成员集合 409 且原 generation／完整成员仍 ready，1 passed／142.35 秒。原运行索引 `out/tests/temp/identity_registry_recovery_root/artifacts/product-shared-group-owner-cas-1793827095499456128.json`；生产首尾一致，只有并行单元测试文件变化。完整排空／force／组恢复及 Windows 的 3.2／3.3 继续未勾选。

### 2026-11-04：4.3 目录 owner 消费者迁齐

- 目录镜像与资源查询装具统一为 canonical UUID 和明确 Gateway，远端通过正式 snapshot 建立连接关系；保留旧合同拒绝、cursor、错误原实例与部分失败恢复。真实反例发现本机有资源时误取远程连接，现仅远端查询连接，本机 DTO 的连接为 null，连接错误仍归原 scope。
- 两份单测 39＋35＝74 项通过；真实 Gateway 与独立下游 HTTP 的正常查询、错误隔离及恢复 2 项通过／104.12 秒。单测 Ruff 通过、Pyright 21→1，无新增；生产与 HTTP 测试静态零诊断。原件见 `out/tests/temp/identity_catalog_consumers_current/artifacts/verification-1793828004925186304.json` 及 `identity_registry_recovery_root/artifacts/catalog-product-http-recovery-1793827721836850898.*`；HTTP 首尾仅并行客户端／单测文件变化，生产与集成装具稳定。未将查询装具当作真实资源创建或完整 4.4 验收。

### 2026-11-04：5.1 outbox 身份请求减法

- outbox 直接复用选定目标的完整 owner 建立原持久分区，删除仅为身份换算的工作区 GET、重复校验和不可能的分区映射冲突；生产净减 39 行。principal／generation、广播、迟到持久清理和接受队列保护保持，同 UUID 不同 Gateway 分区重开及零身份 GET 由行为测试核验。按 Copilot workspace owner 取值方式对照，未运行参考仓库测试。
- runtime 24 项和原 driver 的 principal／取消核心 3 项通过。实际 sourceTransform 类型检查及完整 Workbench 构建均退出 0；构建期间只发生独立搜索单测变化。原 Web tsc 的 28 条外部既存诊断保留，修改两文件零诊断，不称全仓类型全绿。原件见 `out/tests/temp/identity_outbox_owner_current/artifacts/verification-index-1793828133506734641.json`，Root `identity_registry_recovery_root/artifacts/outbox-workbench-types-1793828047948199746.*` 与 `outbox-workbench-build-1793828133506377581.*`；未以此替代真实浏览器 5.4 验收。

### 2026-11-04：5.1 搜索 owner 输入边界收口

- 搜索初始与续点请求共用唯一 Gateway／Workspace 身份校验；非法旧别名、非规范 UUID 或 Gateway 在 DTO 边界返回 422，合法但未登记 owner 仍返回 404。联邦 DTO 复用同一校验，保留公开字段元数据及冻结范围、取消、失效、错误归属和部分失败语义。
- 三份搜索测试 69 passed，公开目录／联邦合同另 39 passed；Ruff 与最终生成一致性退出 0。搜索单测 Pyright 49→29，无新增；生产剩两条原 Session／Thread 校验的存量诊断，函数 AST 与 HEAD 相同。测试后仅恢复原公开 Field 元数据并调整空白，未改行为。准确命令、原 500 反例及交付指纹见 `out/tests/temp/identity_search_consumers_current/artifacts/verification-1793828684093307646.json`；Root 最终静态及生成检查在 `identity_registry_recovery_root/artifacts/search-owner-final-*`。此项不代替真实浏览器或完整 5.1／6.4 验收。

### 2026-11-04：3.2 生命周期迁齐与 3.3 产品补验

- 完整组动作、配置应用、关闭及 preserve/adopt 消费同一 UUID 和原生 Gateway；未保留组内身份换算。reconnect／managed／config 集中收集 265 项，90 秒保护在完成前 247 项后终止，剩余 18 项精确补验通过；shutdown／启动／adopt 的此前 66 项证据继续适用，不把截断运行称为整组退出 0。原件见 `identity_registry_recovery_root/artifacts/canonical-group-*` 及上方 3.1 索引。
- 真实候选进程失败恢复完整旧组 1 passed／96.11 秒；双挂载由消息入口产生历史后重启 Backend，两工作区原 Thread、消息顺序、规范化请求前缀及续跑隔离通过。原件为同目录 `product-shared-candidate-after-gate-release-1793828578895237626.*`、`product-shared-history-and-workbench-lifecycle-1793828754894097214.*`。后一批另一个 Web 项在浏览器前因缺少 fixture 接线报错；直接导入原装具后，补跑在监听探测时报 psutil.AccessDenied，进一步核对原 Gateway 日志确认首因是 46720 的 EADDRINUSE。复用已有端口租约预留后进入真实页面，最终旧 UUID 选择器失败已按完整 owner 修正。原始失败均保留，不更改宿主权限或跳过监听身份检查。

- 通信 startup 装具迁为完整 owner，新增同 UUID 错误 Gateway 归属拒绝，原期限、错误 cause、HTTP 分类与诊断断言保留，57 passed／3.51 秒。Ruff 通过，Pyright 当前／实际 HEAD 均两条原异常联合类型诊断，无新增；原件在同目录 `communication-owner-readiness-*`、`communication-readiness-baseline-types-*`。此处 HTTP 使用原请求替身，不代替真实通信恢复或完整 6.4 验收。

### 2026-11-04：5.2 导航与 Generator 消费者收口

- 导航节点、父关系、持久 JSON 及公开请求／响应迁为完整 owner，保留排序、循环拒绝、原子失败和冷重开断言。Generator 保留原件、冻结 hash、接受／退休断点与请求重试的完整断言；当前版本仅处理 canonical 引用的旧文件格式，独立旧别名反例验证预览／应用均保全原件、零下游且不发布备份。仍有别名源或未结算快照的 registry 13 必须在升级前版本先完成显式退休，已在 design 明确；未新增普通请求别名表。
- 三文件 16 passed，Ruff／Pyright 退出 0，原 17 条类型诊断消除，源码检查前后一致。原件见 `out/tests/temp/identity_navigation_consumers_current/artifacts/verification-1793829450550130776.json`；不把合法旧格式的 mock 导入称为真实旧服务升级验收，5.2／6.3 整体保持未完成。

### 2026-11-04：5.1 Gateway inventory 消费者收口

- inventory 装具直接消费公开 Gateway／Workspace owner、`active_workspace` 与 `remote_gateways`，删除旧双身份 helper 和绕过 DTO 校验的旧字段注入。保留分页预算、顺序、动态 checked_at 不改 revision、名称变化和 cursor 失效；同 UUID 分属不同 Gateway 的分页与活动归属切换不混用资源。同步修正 Gateway 控制与公开 schema 的过时目录指引，避免重新引入双身份。
- 单文件 18 passed／1.70 秒，Ruff／Pyright 退出 0，原 15 条类型诊断消除；Root 核对差异及最终指纹。准确命令、原始输出和源码指纹见 `out/tests/temp/identity_context_inventory_current/artifacts/verification-1793829922390166468.json`。本切片仅改测试和目录指引，不代表真实远端或完整客户端 5.4 验收。

### 2026-11-04：3.3 工作台完整组及接管拒绝补验

- 正式 Workbench 经真实 Gateway 完成取消、合并、sibling SSE 阻塞、显式 force、旧流失效、陈旧确认 CAS、成员移出、空组退出与最后成员删除。删除行复用产品的完整 owner key，不修改产品选择器或放宽身份断言。Web 项 1 passed，原件在 `identity_registry_recovery_root/artifacts/product-workbench-owner-and-real-adoption-1793829953190433344.*`；同批接管项因最终探针漏传显式 UUID 返回 400，未计成功。产品／驱动源码稳定，仅并行的独立集成和单测文件发生变化。
- 接管装具删除手写 Backend HTTP 服务，直接使用正式 Backend 启动、健康与受认证日志证明；首选端口不符仍拒绝，不启动候选或辅助进程，原 PID／UUID 继续服务。补齐最终探针 owner 后单项通过，原件 `product-real-adoption-owner-health-1793830280394796427.*`，源码首尾一致。Ruff 通过；Web／接管静态零诊断，共享生命周期装具仍有与 HEAD 相同的四条存量类型诊断。完整辅助资源保留和部分 adopt 拒绝仍待核齐，3.3 不提前勾选。

### 2026-11-04：4.1／4.2／4.3／5.1／5.2 实现项独立收口

- 只读复核及 Root 核对确认：manifest、投影、代理及连接关系使用来源 Gateway／UUID；多连接合并和解绑复用唯一 registry CAS，连接歧义不隐式选路。目标头与发送方身份分开，生命周期、目录和当前 Generator 沿原授权与 owner 执行；旧 Generator 的升级前退休边界保持。
- 客户端选择、HTTP、搜索 scope、历史／文件缓存、Thread serializer、活动选择和目录 outbox 均消费完整 owner；旧字段限于显式迁移输入，原生 UUID 读取入口不是别名换算。相关定向回归、协议生成一致性、真实 sourceTransform 类型检查及完整 Workbench 构建沿上方各切片证据复用。本次勾选实现项，不将只读审查当作双 Gateway、全部浏览器恢复或 Windows 验收；4.4／5.4／6.3／6.4 保持未完成。

### 2026-11-04：3.3／4.4 已晋升候选的后续交接

- 运行时保留依据当前 active／draining 与另一个 serving generation 判断，删除“曾以候选启动就不能保留”的条件；未改变 Launcher 先启动新候选、晋升后再停旧进程的顺序，未扩展为非产品的冷交换流程。真实 SQLite 代次与正式配置 owner 的定向反例修复前普通来源通过、候选来源失败，修复后相关配置／lifespan 两文件 64 passed／62.14 秒。
- 准确命令、原始反例、源码首尾指纹及类型／lint 对照在 `identity_registry_recovery_root/artifacts/promoted-handoff-*`；Ruff 7／7、Pyright 49／49 均为存量，新增零，生产 Pyright 2／2。不把该单测称为真实 managed 资源保留验收；双 Gateway 的真实 HTTP 测试也已改为产品的候选交接顺序，正在独立验证，3.3／4.4 仍未完成。

### 2026-11-04：3.3 共享组恢复与辅助服务保留收口

- 原真实双挂载历史恢复场景增加 Gateway 辅助服务健康入口观察，核两成员 UUID 和各自 Terminal／Browser 的四个真实 PID；Backend 组重启后四个 PID 保持不变。原 Session／Thread、消息顺序、规范化请求前缀、双工作区续跑及跨工作区 404 断言全部保留，1 passed／139.18 秒。原件 `identity_registry_recovery_root/artifacts/product-shared-history-auxiliary-owner-1793830635211828970.*`，生产与本场景源码稳定，只有并行独立联邦单测变化。
- 结合上方真实工作台的完整组排空阻塞／force／CAS、候选失败恢复、此前启动／接管及真实身份 CLI 的非法 binding 零写入／零进程反例，3.3 完成 Linux 范围验收。Ruff 退出 0；本文件 Pyright 仍为与 HEAD 相同的四条存量诊断，新增观察逻辑零诊断。本项不扩展为 Windows 或真实 SSH 验收。

### 2026-11-04：4.4 配置连接恢复的代次修复

- 真实双 Gateway 在第二次连接更新时已从新目标端口成功读取 manifest／workspaces，但恢复事务用临时操作代次校验已绑定 registry 而启动失败。连接快照现复用 registry 的实际运行代次，不更换绑定、不跳过 proof、不另建事实源；原 CAS、手动投影保留、cursor 与凭据边界维持。
- 直接对账的初始／已绑定代次参数反例修复前 1 passed／1 failed，同一原始错误；修复后联邦、消费者事务与投影身份共 43 passed／8.53 秒。Ruff 1／1、Pyright 16／16 与实际 HEAD 源码对照无新增；原件在 `identity_registry_recovery_root/artifacts/configured-remote-generation-*` 和 `configured-remote-generation-static-comparison.json`。双 Gateway 原失败及稳定指纹见 `identity_remote_recovery_current/artifacts/verification-third-run-1793830715947422744.json`；后续真实更新／离线恢复未通过，4.4 保持未完成。

- 6.4 文件缓存合同收尾：普通预览的 Vary 删除退役 Backend 身份头，改按现行 Target-Gateway 分区；200／304 均核对完整 Vary，保留 Accept-Encoding 合并、读权限、Range、原弱 ETag 与缓存行为。原单文件 21 passed，Ruff／diff 退出 0；项目解释器下 Pyright 1／1 为原测试 disposition 类型诊断，生产零诊断。Root 复核两文件差异，准确命令／原始输出／指纹见 `identity_file_vary_current/artifacts/verification-1793830797593.json`。未把该响应头单测扩展为真实浏览器缓存验收。

### 2026-11-04：6.3 完整旧产品输入迁移与两次重启

- 完整归档受支持旧提交 `bf0f2e1d9d037efcb0210c458fa157fb4d54a27a`，使用该版真实 Gateway、Backend、HTTP 与现有 model_stream 生成 Session、main Thread、消息、封存请求、关联 Turn 的用户视图和导航文件夹；停止旧服务后确认 registry 13／SQLite 18。当前维护 CLI 完成迁移及两次幂等重入，再经两轮当前真实服务核原 UUID、导航、视图、消息、canonical 字节和 request_hash；未直接补写业务或恢复关联。
- 单项 1 passed／226.44 秒、退出 0；此前维护／冲突／离线 proof／中断／回退负例及客户端恢复 parser 的证据沿上方 5.3／6.1／6.2 复用，负例的明确替身边界不变。准确命令、完整源码归档来源、指纹及原始输出见 `out/tests/temp/identity_migration_restart_20261104/artifacts/verification-1793832689136496966.json`，正式原件为 `run-45cea3b955a7`。产品通过后仅加强测试命令的中断回收，业务顶层 AST 未变；超时、中断和正常命令三项短探针确认整棵子进程树回收，最终 Ruff／Pyright 退出 0。新矩阵入口保留 420 秒／4GiB；未重新运行矩阵，不将列表校验记作业务通过。本项不代表真实 SSH、Windows 或浏览器 5.4 验收。

### 2026-11-04：4.4 双 Gateway 更新、协议拒绝与离线冷恢复收口

- 配置连接的普通冷启动在来源及全部连接参数未变时复用原恢复循环；离线连接保留原 owner、UUID、快照、活动选择和可见错误。新增／移除／参数变化、重复 ID、manual 来源及候选启动仍走原严格 batch，不新增第二恢复链。真实反例在 B 离线时导致 A 冷启退出 3，修复后同一完整场景通过。
- 双真实 Gateway／Backend 完成两次候选先晋升后停止旧 PID 的交接、端口更新及旧 token 401、协议不兼容拒绝且原 lease／cursor 保留、B 离线后 A 冷重启及原 owner 重连，所有远端读取均核本机 Backend 零命中。第七轮 1 passed／266.03 秒（含独占锁等待）、退出 0，154 项依赖首尾指纹一致；原件为 `run-1a193ec1000b`。协议拒绝只注入正式发现端点的旧版本声明，不是完整旧版服务；SSH pairing／隧道沿原明确本机装具，不代表真实 SSH、Windows 或 Launcher 自动协调。
- 启动恢复全文件 74 passed，关联批次 34 passed／3 个旧健康 fixture 构造失败，迁齐完整 owner 后仅补该文件 3 passed；manual 来源测试准确限定为必须进入 reconcile，再补单项通过。最终 Ruff／Pyright／编译退出 0，证据索引为 `out/tests/temp/identity_remote_recovery_current/artifacts/verification-index-1793832974572841795.json`。同 UUID 不同 Gateway 与同源多连接验收沿上方已通过实网场景复用；旧路由套件排除新长场景并单列 360 秒／4GiB 恢复套件，列表校验通过，未重复长跑。

### 2026-11-04：5.4 正式工作台的跨 Gateway 文件、搜索与恢复收口

- 文件 stat 沿已有固定工作区导航入口携完整 owner，远端返回补齐 authority 的等价 URI 不再被重复字符串比较拒绝。会话目录定位、高亮及删除判断从编辑器的会话 owner 取值，逐层显式传入；当前活动工作区改变时，不再把仍打开的另一 Gateway 会话发往新目标。
- 双真实 Gateway／Backend、相同工作区 UUID 的正式 Workbench 经真实浏览器完成切换、文件打开、独立草稿、正文搜索、原标签重载恢复及 9 次 breadcrumb 请求归属核对；4 份完整 SSE 收据保留 owner，页面错误为空。最终单项 1 passed／142.22 秒、退出 0，依赖首尾指纹一致，原件为 `run-efd9469f6ee2`。准确命令、源码版本／指纹及原始日志在 `out/tests/temp/identity_browser_recovery_20261104/artifacts/integration-1793832896926473222.json`；沿原 SSH 本机装具，不声称真实跨机或 Windows 验收。
- 文本搜索消费者迁为单 UUID 与完整 owner，原路径注册新工作区须为不同 UUID，原工作区搬迁再登记仍保留原 UUID；旧搜索失效时 404 且零下游，不误查新工作区，恢复后按原 owner 重查。与双 Gateway 首轮合跑 2 passed／444.55 秒（`owner-and-text-integration-1793832267838600683`），后续仅 breadcrumb 产品变更，双 Gateway 最新单项补验覆盖；未为无关源码重复整组长跑。文件 stat 单测 2 passed／29 断言，目录相关四文件 59 passed／276 断言；真实 sourceTransform 类型检查、JS 语法及 Python Ruff／Pyright 退出 0。最新产品测试完成同一正式 Workbench 构建，Web build 脚本也是该唯一构建入口，复用原构建产物与收据。

### 2026-11-05：6.4 Electron 原生消费者补验

- 便携 Electron 的驱动、HTTP 审计、延迟 bootstrap 与只读文档读取统一使用原 `(gateway_id, workspace_id)`；移除 `BOXTEAM_ELECTRON_ROUTE_ID` 和临时 route 字段。Diff 选择先等待 Turn 变更集的实际 `aria-pressed`，避免仍展示旧 all 列表时提前打开；未修改产品行为或放宽请求次数。
- 正式 `integration-electron-packaged-workbench` 矩阵 1 passed／198.64 秒，退出 0，重新构建并迁移到源码目录外的 ASAR。覆盖真实工具调用、保存、图片 200／304、Diff 分屏共享、来源只读、正常关闭重启及独立 profile，15 项产品断言通过，页面／存储／代理错误均为空。源码首尾指纹与当前磁盘一致，原失败回执仍保留。命令、指纹及原始输出见 `out/tests/temp/identity_thread_consumers_20261105/artifacts/electron-turn-selection-product-1793855365775026207.json`，正式产物为 `run-36601aaccd5d`。Python 静态复用此前相同源码的零诊断收据，最终驱动语法检查退出 0；此项不代替 Windows 或其余未完成消费者验收，6.4 保持未勾。

### 2026-11-05：6.4 持久任务、资源纠正与历史分支消费者

- 持久任务四组驱动和资源身份纠正统一传原生 Gateway／Workspace owner，目录节点选择使用完整 owner key；模型调用、目标切换、错误重读和浏览器刷新原断言保留。原组合前五项逐项通过，随后 fork 看门狗因未经矩阵退出，未将整组记为成功；fork 改由原正式矩阵单独补验 1 passed／186.41秒，Turn来源冻结矩阵另 1 passed／208.12秒。
- 交付测试源码与各次验收前后SHA一致，Ruff／Pyright及JS语法沿相同源码既有零诊断收据复用，根线程核对差异及调用方。原始命令／退出码／失败与补验在 `out/tests/temp/identity_thread_consumers_20261105/artifacts/thread-owner-product-1793852080482450852.json`、`thread-owner-fork-matrix-1793854149195698064.json` 和 `thread-owner-turn-matrix-1793854497728786801.json`；不替代其余消费者，6.4继续未勾。

### 2026-11-05：6.4 元数据初始化与搜索协议消费者

- 元数据浏览器驱动删除双工作区 ID，按完整 active owner 选择目标，核初始化请求双目标头及原 UUID；目录节点使用完整归属 key，未注册目录编辑、原生能力、配置刷新和零意外模型调用断言保留。正式矩阵在静态 Workbench／真实 Vite 两个运行面均通过，2 passed／257.95秒、退出0，测试源码首尾指纹一致。原件见 `out/tests/temp/identity_remaining_web_20261105/artifacts/metadata-product-1793856808190962658.json`，正式运行 `run-3ca8197a28c6`；复用同批 Workbench 构建，不代表 Windows 验收。
- 两个搜索 API 单测移除退役 target_id 和路由别名预期，保留来源、顺序、请求次数及失败不改投断言，16 passed／93断言。Ruff、驱动纯语法检查退出0；完整 Web 真类型检查与完整提交归档基线逐条一致，28／28原有诊断，新增0，原始输出及对照在同目录 `search-owner-unit.log`、`final-root-ruff-1793857413091164909.json`、`final-root-js-parse-1793857413102710692.json` 和 `web-type-comparison-final.json`。其余消费者尚在核验，6.4保持未勾。

### 2026-11-05：6.4 浏览器视图、缓存与未读消费者

- 驱动、HTTP观察与搜索收据统一核完整Gateway／UUID；真实IndexedDB观察使用原 `gatewayWorkspaceKey`／`browserViewKey`，不再用裸UUID拼已退役的存储键。首轮因此在滚动持久化观察超时，迁齐观察入口后正式矩阵1 passed／165.94秒、退出0，`run-707cfdfd3abf`；草稿／滚动重载、新页面恢复、历史缓存先显、独立上下文与未读标记的11项业务断言通过，页面错误为空。
- 查询展示历史仍由原fixture准备，未读执行状态通过真实消息与model_stream产生；不将手工历史作为执行／恢复完整验收。测试两文件SHA与验收首尾一致，Python静态与完整Web类型沿同批相同源码收据复用，最终驱动纯语法退出0；原失败和补验见 `out/tests/temp/identity_remaining_web_20261105/artifacts/browser-views-product-1793857351147908025.json`、`browser-views-owner-key-product-1793857601642843175.json` 及 `browser-view-key-js-parse-1793857592107051056.json`。其余消费者仍未完成，6.4不勾。

### 2026-11-05：6.4 文件操作与持久意图消费者

- Chromium文件装具以真实UUID／Gateway装配生产hook、HTTP文件owner与IndexedDB，原 `gw-files-a/b` 仅作测试控制标签；刷新、目标切换与多Tab持久意图不再保存双工作区ID。文件根接口的首轮500定位为装具缺原生目录绑定，现复用真实 `GatewayStateStore` 根owner，未添加产品回退；日志／trace改用原 `TestRunContext` 分运行保存。
- 受保护完整脚本28.77秒通过、退出0，正式产物 `run-8d52e06916b7`。新建文件／目录、复制／粘贴／上传、Blob刷新恢复、未知结果原ID续查、冻结目标、多Tab因果依赖及SSE原强断言保留；此边界的Gateway认证／传输仍为显式受控替身，不等同完整Workbench E2E。Ruff与JS纯语法退出0，Pyright剩1条原有私有worker替换签名诊断，新增0。首轮和最终原件在 `out/tests/temp/identity_remaining_web_20261105/artifacts/file-operations-product-1793857502355789163.json`、`file-operations-native-binding-product-1793857796702424509.json`、`file-owner-ruff-1793857712881955058.json`、`file-owner-pyright-1793857712861006124.json` 与 `file-owner-js-parse-1793857712881328628.json`；交付源码与最终验收首尾SHA一致，6.4仍未完成。

### 2026-11-05：6.4 资源关闭与跨 Gateway URI 消费者

- 资源关闭按原生Gateway／UUID唯一选目标并核本地连接，两种模型协议在组合中逐项通过；随后a06因测试客户端的来源目标头与远端URI冲突返回400而停止，未将组合记为通过。context/read现显式带URI目标双头，后续消息仍固定来源owner；a06独立补验1 passed／116.68秒、退出0，保留冷读零物化、切换／重连、send／wait冻结目标、真实工具顺序配对和持久通信事实断言。
- 原组合与补验命令／源码指纹／原始结果分别为 `out/tests/temp/identity_remaining_web_20261105/artifacts/backend-owner-consumers-product-1793857872642301432.json` 和 `context-uri-target-product-1793858070035877626.json`。资源关闭静态沿已交付相同SHA的 `identity_remaining_backend_20261105/artifacts/evidence-index.json` 复用；a06最终Ruff退出0、Pyright8／8既存诊断无新增，对照 `context-uri-target-type-comparison.json`。其余Backend消费者尚未完成，6.4继续未勾；联邦沿原loopback传输装具，不代表真实SSH或Windows。

### 2026-11-05：6.4 联邦验收装具的启动与端口归属

- source-child fork 两次在 setup 失败，尚未进入业务：首次辅助服务39224，补验Backend39200；保留原始失败，不记fork通过。补验后实际 `ss -tanp` 观察39200作为出站源连接处于TIME-WAIT，宿主自动源端口范围32768–60999与固定测试块重叠；这不证明首次39224占用的来源。
- 联邦装具复用既有 `E2EProcessLease`，在启动前持有固定Backend、Gateway、辅助服务、sshd与隧道端口，全部句柄退出后才释放；并行Backend／下游Gateway先完成移交再统一失败回收，上游仍等待下游就绪。不加盲重试、不终止未知监听或修改宿主配置。
- 端口租约、真实Backend并行启动失败清理及HTTP就绪定向11 passed／22.62秒，Ruff和项目解释器Pyright退出0。最终仅格式化一个测试导入，完整AST相同。命令、首尾源码指纹与原始结果在 `out/tests/temp/identity_remaining_web_20261105/artifacts/federation-port-lease-behavior-1793858923834718097.json`、`federation-port-lease-type-1793858915450571697.json`、`federation-port-lease-final-static-1793859023481622292.json` 和 `federation-import-format-attribution.json`。本次不代替完整fork／Web／Windows业务验收，6.4仍未完成。

### 2026-11-05：6.4 会话生成与共享 HTTP Skill 消费者

- 两组真实Gateway装具均从原生Gateway健康响应与工作区身份文件核验完整owner，普通请求固定双目标头，不从活动项补齐归属。HTTP Skill双工作区入口保留共享原件只获取一次、每个工作区冻结与更新后的原强断言，正式矩阵1 passed／93.32秒、退出0。
- 会话生成策略正式矩阵6 passed／1 failed／129.08秒；失败项已创建并执行分支，在原40次等待内仍reporting，未放宽期限或宣称整组通过。原trace随后显示汇报Job完成，创建至完成8.90秒，agent_start至模型请求4.87秒；TODO保持：进一步定位请求前准备耗时。另一个原长窗口终态用例通过，不能替代短窗口性能验收。
- 原始命令、首尾源码SHA及退出码：`out/tests/temp/identity_remaining_web_20261105/artifacts/gateway-http-skill-owner-product-1793858643861942969.json`、`task-generation-owner-product-1793858388101593363.json`、`generation-report-timing-attribution.json`。两文件与既有静态交付及真实执行首尾完全相同，Ruff通过；五文件Pyright72／72既存诊断，新增0，原件在 `identity_remaining_backend_20261105/artifacts/evidence-index.json` 和 `pyright-attribution-01.json`，匹配收据为 `generation-skill-source-attribution.json`。6.4尚未完成。

### 2026-11-05：6.4 Skill、代理及日志面板单测消费者

- 原生Gateway的Skill publication／联邦路由、catalog端口刷新、WebSocket、目录注册及lifespan装具删除旧身份字段；registry与controller明确读取同隔离Gateway根的权威身份。Skill43项通过；Gateway原51项中的41项通过后保持源码不变，修正controller身份装具的完整文件补验10项通过，原失败保留。
- 日志面板3 passed／24断言，以相同UUID、不同Gateway验证真实API双目标头，保留迟到输出、切换空窗和隐藏停止读取的原断言。最终Ruff／ESM与diff检查通过，八文件Pyright当前26／完整提交归档基线36，无新增；完整Web tsc28／28无新增。根复核差异及交付SHA，命令、源码和原始结果在 `out/tests/temp/identity_remaining_backend_20261105/artifacts/extra-evidence-index-04.json`、`extra-checks-index-04.json`、`extra-pyright-attribution-seal-04.json` 与 `extra-web-type-attribution-04.json`。此组不代替真实Web／联邦网络验收，6.4保持未勾。

### 2026-11-05：6.4 文件树、正文引用与消息流消费者

- 八份客户端测试使用正式完整owner key，核真实API双目标头；同UUID跨Gateway的child列表切换保留原空窗与迟到隔离。消息流的终态缓存定位复用原 `sessionScopeKey`，删除测试自身的裸字符串拼键，保留全部终态清理、失败归属及410重连断言；文件树的健康请求期望按现行固定owner链删除，目录重读次数不放宽。
- 最终定向67 passed／510断言；首轮消息流23／25的缓存键装具失败和整文件25／25补验均保留。完整Web tsc仍28／28原诊断，本批八文件零诊断，新增0。根复核差异和完整交付SHA，准确命令／外部保护／原始输出在 `out/tests/temp/identity_remaining_backend_20261105/artifacts/src-evidence-index-05.json`、`src-checks-index-05.json` 与 `src-web-type-attribution-05.json`；不代替真实浏览器或原生客户端验收，6.4仍未完成。

### 2026-11-05：6.4 附件组件与目录搜索身份

- 附件装具使用完整owner、原生UUID URI及真实API双目标头；保留缩略图失败重试、原件预览和具体附件错误正文断言。附件组件在受保护组合中通过，随后manual browser失败，未把整组记为通过；首轮Vite扫描与多alert歧义按准确入口／预览region修正。
- 同UUID跨Gateway的目录搜索流单测核运行、未读及离线摘要隔离，原定向5 passed／28断言。附件harness真实tsc、Ruff与JS语法通过，完整Web原28条诊断无新增；源码匹配与原始结果见 `out/tests/temp/identity_remaining_web_20261105/artifacts/attachment-component-source-attribution.json` 及其引用的03证据。本批不代替完整产品附件／远端验收，6.4继续未勾。

### 2026-11-05：6.4 Browser attach 早期输入与归属

- 样式阻塞的真实Chromium最小复现确认：原HTML在提交监听器安装前接受回车，触发原生表单跳转、清空地址及owner查询参数。现将同一早期处理提前到样式前，模块完成后继续复用原navigate，不新增导航链路；回归明确阻塞首次样式并核URL、双目标创建头及Thread归属。
- 真实Gateway/Workbench手动Browser场景通过，保留首次导航、焦点、空白标签、导航错误恢复、无randomUUID和模块失败重载断言；同组合file-cache与session-info通过，catalog在随后main打开阶段失败，整体3 passed／1 failed，不记全组通过。HTML／JS静态通过，准确命令、首尾源码及独立patch见 `out/tests/temp/browser_early_navigation_20261105/artifacts/source-attribution.json`；viewport的他人改动未纳入本提交。6.4继续未勾。
