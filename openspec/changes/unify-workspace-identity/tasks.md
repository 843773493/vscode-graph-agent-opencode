## 1. 身份合同与迁移范围

- [x] 1.1 全仓检索工作区别名生成、`backend_workspace_id`／`remote_workspace_id` 及其直接和间接消费者，标出真实工作区引用与独立连接／导航身份。包括 camelCase、旧身份头、projection 生成器和不含旧字段名的持久引用；范围见 design 的迁移输入表。
- [x] 1.2 明确支持的 registry、配置及客户端旧版本，确认需迁移的实际表、字段和恢复格式；与多工作区、quick-access、扩展管理规划对齐，避免新增旧映射调用方。输入基线与正确 owner 不改写边界见 design；迁移实现及恢复验收仍在第 5／6 组。
- [ ] 1.3 在现有受控身份入口完成注册前 UUID 获取、已有 UUID 保留及同原生 Gateway 根冲突拒绝，不增加第二身份源或占位 workspace_id。
- [x] 1.3a 身份文件完整写入后原子不覆盖发布，消除并发读取空文件；覆盖创建／绑定竞争、候选失败清理与真实 Gateway 损坏恢复。此子项不代表 1.3 的登记与根冲突验收完成。Windows 实机验证尚未完成：`hyf-win` 目标已有两项本地删除，快照激活拒绝，未覆盖目标改动。

## 2. 原生 Gateway 登记与本地请求链

- [ ] 2.1 将本地工作区登记、唯一 root binding、活动选择和路由键迁为工作区 UUID 与 Gateway 归属，保持 owner／租约／registry revision CAS。
- [ ] 2.2 迁齐本地管理和业务代理的工作区定位，统一路径与头部 UUID，拒绝冲突及错误归属，保留原 request ID 和管理授权。
- [ ] 2.3 同步协议 descriptor／proto、公开 DTO、生成映射与校验、OpenAPI 和 SDK；更新破坏性协议版本，执行生成产物一致性检查和受影响类型检查。
- [ ] 2.4 通过真实注册和 HTTP／URI 文件入口验证各层 UUID 相同、两工作区隔离、相同 UUID 不同根冲突及未授权根管理拒绝。

## 3. 共享 Backend 生命周期

- [ ] 3.1 将 managed Backend 成员、generation 启动合同及 mount 校验迁为单一 UUID，保留唯一原生 Gateway root binding 和唯一进程句柄。
- [ ] 3.2 迁齐 blockers、完整影响集合、force、配置 fan-out、移除、shutdown、preserve/adopt 与恢复，删除组生命周期中的工作区 ID 换算。
- [ ] 3.3 经产品入口验证 sibling blocker、排空超时、显式 force、完整组恢复及辅助服务保留；缺失或错误 binding 必须拒绝部分 adopt。
- [x] 3.4 用受进程外保护的独立 Backend 子进程验证不同 home 争用同一隔离工作区仍失败、释放锁后可接管且 UUID 与业务历史保持不变。Linux 真实 Backend A／B／C 使用三个独立 home，A 由消息入口产生执行历史，B 被物理锁拒绝且 A 继续服务；A 退出后 C 保留 Session／main Thread、完整历史条目与身份文件，并从原检查点续跑，实际规范化模型请求前缀完全相同。矩阵 `integration-workspace-process-exclusivity`：1 passed／64.69s，沿现有 model_stream 精确回放，Ruff／Pyright 通过；不代表跨 Gateway 身份迁移或 Windows 实机验收。

## 4. 联邦与连接身份分离

- [ ] 4.1 将 manifest、projection 和远程代理迁为来源 `(gateway_id, workspace_id)`，移除投影 ID 生成及远程 UUID 换算，保留 cursor、CAS 与 lease。
- [ ] 4.2 迁齐连接配置对账和工作区关联，同一来源多连接只保留一个资源身份，删除连接不误删其他关联；保留 generation 排空和独立凭据边界。
- [ ] 4.3 接通独立目标 Gateway 载体，迁齐远程生命周期、目录和 Generator 引用，不改变发送方身份头、管理授权或有界联邦规则。
- [ ] 4.4 通过两个真实 Gateway 验证同 UUID 不同归属、多连接同来源、连接更新／离线、快照恢复及不兼容协议拒绝，检查请求 UUID 不经换算。

## 5. 客户端引用与持久恢复

- [ ] 5.1 迁齐共享 owner、Gateway 请求、工作区选择、目录／搜索 scope、缓存键及相关客户端运行面，删除路由 ID 到 Backend UUID 的转换函数和调用方。
- [ ] 5.2 迁齐编辑器 serializer、活动选择、导航／Generator 与用户视图态的持久引用；按 Gateway 归属保存同一 UUID。
- [x] 5.3 接入显式迁移恢复材料，准确升级旧客户端引用；无法确认的项逐项报告并保留可重试信息，不切到当前工作区或清空有效状态。
- [ ] 5.4 运行受影响客户端真实类型检查及 Workbench 构建，以真实 Gateway 和隔离浏览器验证两工作区切换、远程选择、文件打开、搜索及重载恢复。

## 6. 存量迁移与单链路收尾

- [x] 6.1 实现只读预览与显式可恢复迁移，核验完整映射、owner、引用和未结算状态；仅按实际结构变化提升 schema，离线或歧义时保留原记录并阻塞发布。
- [x] 6.2 原子迁移 registry／metadata、root binding、managed_backends、活动选择、目录／Generator 和 user_view_state；保留有效 URI、canonical 正文、历史 hash 及 Session／Thread 身份。
- [ ] 6.3 用受支持旧版本完整输入验证成功、重入、冲突、远端离线、写入中断恢复和维护窗口回退，检查迁移后真实重启与客户端恢复，不手工补齐产品应生成的关联。
- [ ] 6.4 迁齐后物理删除旧别名生成器、重复字段、换算接口、死代码和旧协议分支；完成全仓消费者核对、相关静态分析及定向回归，保留准确命令、源码版本与原始结果。所有测试使用既有矩阵或进程外超时／内存保护。

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
