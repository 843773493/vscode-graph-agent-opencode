本清单按 capability 与完整纵向链组织，编号用于依赖和证据定位，不代表所有工作必须串行。实施项仅按已完成的闭包勾选，宽项的局部证据不代表整项完成。每项勾选需对应真实源码／协议和适用验证；源码盘点、进程启动或原版接口存在不能代替产品行为证明。

## 1. 既有 owner 与公共目标合同

- [ ] 1.1 清点原扩展／MCP／设置 UI、源配置、所有直接与间接调用、生成协议及测试，列明权威写入／读取 owner、单链路迁移与删除清单，不改变他人已有任务证据。
- [ ] 1.2 在现有协议 owner 明确安装目标、配置范围、宿主视图／generation、能力和操作状态的 typed 合同；稳定 Gateway／Backend／workspace 身份与临时 route 分开，不新建通用插件对象体系。
  - 2026-11-05 安装域身份基础切片：目标 `BOXTEAM_HOME/state/backend/identity.json` 一次分配稳定 Backend UUID，正常读取不写盘，首次发布沿跨进程锁和原子写；Backend lifespan／health 与原 Gateway 本机启动、接管、完整组验证共用该 owner，进程组 owner／成员／generation 不作安装域身份。相关单测 277 项通过，补充错误／缺失身份接管边界 6 项通过；真实 Gateway 1 passed／99.19 秒，覆盖独立启动、挂载合并、切换活动工作区及 Gateway 重启。跨进程首次创建在最终集成位置 1 passed，身份负面单测最终 8 passed；Ruff 通过，生产类型诊断与改前同为 3 项且新增 0。原件见 `out/tests/temp/backend_installation_identity_20261105/artifacts/`；公开安装／host typed 合同、目标发现、远端及 Windows 尚未验证，本项保持未勾选。
- [ ] 1.3 迁齐目标管理 API、host 通道协商、proto／OpenAPI／生成客户端和消费者；验证实际序列化、未知类型／版本拒绝与单请求 request_id，全链真实类型检查先于产品验收。
  - 2026-11-05 MCP 管理合同切片：原 Workspace proto／OpenAPI、生成客户端及七个成功出口统一返回目标和协议版本，保留原 data 数组／对象；目录 revision／generation 与精确 connection_ref 从唯一 publication／runtime 投影，无新账本。Web 在消费前拒绝错误 owner、未知类型／版本，两个原 MCP 浏览器驱动迁齐完整 Gateway／UUID 和双目标头。真实 Gateway→Backend→MCP 1 passed／37.22 秒，Web 授权／真实工具执行及逐调用审批 2 passed／284.11 秒；HTTP 合同 322 passed，最终 Python 定向 59 passed，Web API／hook 22 passed。Buf 格式／lint／build、真实 HEAD 基线 breaking、生成一致性、Ruff 和完整 Web 构建通过；Web tsc 的 28 条诊断与完整 HEAD 归档原始输出相同，Pyright 两条原测试诊断无新增。命令、源码指纹、原始输出及基线对照见 `out/tests/temp/extension_target_contracts_20261104/artifacts/`；安装／host 协议及跨目标闭包尚未完成，本项保持未勾选。
- [ ] 1.4 在原联邦 policy 登记安装／宿主管理能力及 configuration_read／configuration_write 与 schema，迁齐默认拒绝／认证主体规则和调用方；覆盖脱敏读取、订阅披露、预检／提交／pending 管理和独立重启权限，验证撤权、Relay 不提权、只有发现权限时无目标写入，不另建权限系统。
  - 配置能力已沿原 policy／schema 接入 Gateway 自有配置路由与两种 workspace 代理 mount；安装／宿主管理能力和实网目标验收仍未完成，整项保持未勾选。
  - MCP 候选规划切片沿原 policy／schema 登记独立 `mcp_installation_manage`，默认拒绝且须精确绑定认证主体；两种 workspace mount 的原件读取／计划／重验共用原生 Gateway 权限边界。该切片不代替后续制品安装、宿主管理及整项验收，本项保持未勾选。
- [ ] 1.5 实现绑定稳定目标的能力／状态发现及实际路由，区分离线镜像和实时状态；验证同名、route 变化／重用、停止和权限撤销，无隐式启动或本地 fallback。
- [ ] 1.6 清点各功能将使用的既有 URI 与字段；在原配置／联邦／MCP owner 补本稿已明确需要的 source 读写、私有输入引用、受管 launch binding、管理能力正式 delta／schema／消费者；确需新增 URI 时补原寻址 owner delta，不在前端拼 grammar。

## 2. MCP 目录与目标安装

- [x] 2.1 沿原 gallery 服务合同接入可配置 MCP 来源，验证实际协议、分页、取消及未配置／失败／无匹配；与 VSIX 来源分开，查询不下载、不启动或扩展来源范围。
  - 2026-11-05：原 MCP Gallery 统一单源 queryPage／pager，显式 v0.1／v0，不再探测版本或将失败折叠为空页；换源取消旧查询并拒绝晚到结果，原列表／只读详情共用同一服务。受保护定向 54 passed／220 断言，44 份实际转换源码类型检查、Python 静态及正式 Workbench 构建通过；真实 Gateway／Workbench 对受控 registry 完整场景 1 passed／79.22 秒（`run-b402b78e555b`），覆盖分页、续页503、未配置、无匹配、首屏错误、只读详情和换源。旧失败定位为测试驱动跨浏览器序列化 IEditorPane，改为浏览器内等待完成，不改生产期限；原件与最终指纹在 `out/tests/temp/mcp_gallery_contract_20261105/artifacts/2026-11-05T05-28-14-246Z-fingerprint.json`。不代表公网、安装或原生 Gallery 验收。
- [x] 2.2 实现目录条目、手动定义及 HTTP 服务的同一候选计划，冻结来源／entry／完整 manifest digest／确切 variant 原 ordinal 与 digest／目标原生平台／版本与参数；沿 mcp_installation_manage 由原生 Gateway 判权并在消费前重验，验证同名不同源、无稳定 ID 的真实 envelope、variant 消失、多同类候选和 SSE 明确拒绝，无首项 fallback，HTTP 不虚报包下载，候选不启动 MCP 或写安装／配置事实。
  - 2026-11-05 候选后端切片：原 McpCatalogOwner 扩展三个无副作用 API，读取完整 v0.1／显式 v0 原件，确切选择与消费前重验使用唯一 JCS 摘要；新 secret input 在私有引用能力完成前明确拒绝，原显式 env／headers 沿原 parser 并脱敏展示。候选／API／原生权限短批 102 passed，旧 MCP／配置／代理及最终 API 回归 350 passed，公开 HTTP 合同 57 passed，各批有重叠不相加。真实双 Gateway／双 Backend socket 1 passed／50.08 秒，覆盖同 workspace UUID 不同 Backend、两种 mount、原生默认拒绝／精确主体／撤权和无权限零 Backend；仅 SSH 执行与隧道复用受控本机替身。另实际 localhost registry→HTTPX→owner→API 1 passed／3.39 秒，原件变化后旧计划 409。完整协议生成及一致性、Buf lint／build／完整 HEAD breaking、OpenAPI 导出、Web 构建与 OpenSpec strict 通过；Web tsc 的 28 条诊断与完整基线相同，无新增。准确命令、源码指纹、原始输出、静态基线对照及未验证边界见 `out/tests/temp/mcp_candidate_plan_20261105/artifacts/final-evidence.json`。未启用安装 UI、制品发布、配置保存或秘密引用；集成复核确认本项完成，1.4 保持未勾选。
- [ ] 2.3 在目标侧建立唯一安装记录与真实制品发布链，复用生态工具／隔离环境、锁定依赖及不可变镜像 digest；核 launch 实际使用发布路径／generation，不能把原版动态 npx／uvx 配置视为安装。缺 runtime／安装能力明确失败，保留既有显式动态命令，不自动全局安装系统环境。
  - 2026-11-06 Windows 深路径启动闭包：原制品安装／发布完成后，uv console trampoline 内部启动341字符Python路径报206，正式配置500原件保留。受管启动统一由同一venv Python直接执行生态生成的原脚本／ZIP入口，原入口路径、generation、摘要、参数、环境和cwd保持；删除未使用的可选摘要校验分支，所有实际启动仍校验，不新增包装器或第二启动链。73项定向单元、Ruff及4份Python的Pyright零诊断；Linux真实发布／Thread、双工作区共享引用／卸载及保留合同共16项通过。Windows最终原真实wheel／配置CAS／Thread调用及第二工作区场景1 passed／128.48秒，包含teardown；原入口350字符、Python341字符保持，未缩短目录或换别名。完整指纹、命令、失败与成功原件见 `out/tests/temp/2026/11/06/mcp_deep_launch/artifacts/verification-v1.json`。仅闭合现有PyPI启动，npm／OCI／NuGet及其真实发行仍未支持，2.3保持未勾选；不代替Windows完整页面或Electron验收。
- [x] 2.4 实现有界进度、日志、意图指纹／幂等操作、取消及中断查询，暂存验证后发布；核同键异参数冲突、断流／重启不重放未知脚本，安装、配置与 ready 分阶段报告。
  - 2026-11-06 用户裁定：每个共享下载消费者独立取消并使用 `cancelled`；最后使用者取消才停止实际任务并核退出。跨进程取消意图归原 SQLite，由原执行者结算；已先提交发布保持 `succeeded`。须验独立取消、最后使用者退出、跨进程和发布竞争，尚未以本次裁定标记实现完成。
  - 2026-11-06 已补齐当前支持的 PyPI 安装闭包：原 SQLite owner 持共享执行，各 operation 独立取消；最后消费者等待原进程容器退出，跨进程取消、成功先提交及失败／失联分别结算，原 Workbench 通知接入取消并继续查询。151 项相关单元、最终启动修改后的75项单元、17项客户端检查通过（批次有重叠，不相加）；最终 Linux 真实 Gateway／Backend／uv 的发布与原 Thread 调用、双 Backend 取消、最后消费者退出、重启恢复共4项通过，真实页面取消1项通过。Windows 原生取消／进程17项通过，真实 wheel 已安装、验证并发布；该批完整 Thread 调用当时被 uv console trampoline 的深路径206错误阻断，后续启动修复见2.3，不记该批为 Windows 完整验收。Ruff、生产及新增测试 Pyright、57份实际转换源码类型、生成一致性及正式构建通过，Web独立tsc仍为相同28条既有诊断。准确命令、指纹、原始失败与通过及复用范围见 `out/tests/temp/2026/11/06/mcp_installation_cancel/artifacts/verification-v1.json`；尚未支持的安装生态仍由2.3追踪，不以本项宣称其可用或代替第9组验收。
- [x] 2.5 实现同安装域精确版本复用、工作区引用、更新失败保留旧版及物理卸载检查；制品引用绑定原 connection generation，沿原退休／监督释放；验证 A1/A2、并发同意图、旧调用／cleanup_failed／结果未知保留和有引用拒绝卸载。
  - 2026-11-06 用户裁定：旧调用、封存资格或结果未知未结清时拒绝切换，保留旧配置和原连接；结清后用户再次显式切换，不支持新旧 stdio 并行、不自动续做被拒绝的切换。后续验收按此边界修订，原“在途调用跨版本继续执行／stdio 并行策略待裁定”由此消解；下方2026-11-06切片已闭合该裁定。
  - 2026-11-05 已发布 PyPI 暖复用限定切片：原安装账本按完整来源、manifest／variant、平台、精确版本及入口匹配并全量校验原 generation；制品身份不含工作区 args／env 或 plan_digest，新 operation_id 保留独立意图和回执。真实 Gateway／Backend／uv／wheel 双工作区 1 passed／57.71 秒，覆盖 A1 发布后 A2 无新增索引或下载请求、仅一份制品与 generation、各自参数／cwd／环境／进程／连接，以及断开 A1 后 A2 原 connection_ref 仍 ready。原连接指纹已迁齐 managed_launch JSON 消费并保留 unmanaged 指纹与配置失效行为；31 项安装单测、8 项指纹／准入／重启对账单测及 Ruff／Pyright 通过。准确命令、源码指纹、原始失败与成功输出及补丁见 `out/tests/temp/extension_next_20261105/artifacts/warm-final-evidence.json`。冷态并发归并、工作区持久引用、旧调用／cleanup_failed／结果未知占用及物理卸载保护仍未完成，Windows／macOS 未验，整项保持未勾选。

  - 2026-11-05 冷态并发归并切片：同安装域按精确制品身份在原 SQLite 事务选定唯一 producer，各 operation 保留独立意图、ID 和回执，共享真实进度／失败／发布；进程退出整批 interrupted，原 ID 不重放，显式新意图才重试。v1→v2 迁移在原事务内核活进程、歧义和成功记录一致性，DDL 与升级语句均归 SQL。真实双 Backend／Gateway 的 warm、cold、cold-failure 三模式 3 passed／170.82 秒，核唯一下载／准备／generation、独立工作区参数与连接、403 原失败及新操作恢复；48 单测、Ruff 与真实 Pyright 通过。准确命令、源码指纹、原始输出与退出码见 `out/tests/temp/frozen_wire_20261105/artifacts/mcp-cold-root-evidence-001.json`；工作区持久引用、旧调用／结果未知占用及物理卸载保护仍未完成，整项保持未勾选。

  - 2026-11-05 唯一 stdio Process 前置切片：启动前冻结原 connection_ref 并在原资源账本登记 unknown，实际 Process 持有 PID／创建时间／wait 退出码，POSIX 核原进程组、Windows 使用 suspended→Job→resume 并核 Job 归零。stdio 复用官方协议类型和同一 ClientSession，删除原无句柄启动链；物理 stopped 与协议失败、目录资格及未结算 invocation 分离。Linux 331 项单元、真实 EOF、原登记／关闭及非合作关闭场景按源码依赖验收；Windows 8 项原生场景及最终受影响单项通过，覆盖真实 exe／cmd／npm／npx。精确合入后50项定向和真实 EOF 1项、Ruff通过，Pyright零错误／4条pywin32来源警告。完整原件与复用边界见 `out/tests/temp/2026/11/05/mcp_process_lifecycle/artifacts/delivery-manifest-v2.json`，共享树收据 `out/tests/temp/2026/11/05/mcp_process_lifecycle/artifacts/root-integration-002.json`。完整generation引用、旧调用／未知占用及物理卸载仍未闭环，2.5不勾选。

  - 2026-11-05 generation 引用与卸载切片：原安装账本仅登记工作区 locator，引用从原 source／active／未终结 pending 及资源账本核实；首次引用与卸载共用安装事务，保留 disabled／遮蔽来源、旧连接、cleanup_failed 和未结清调用。卸载先提交 removing，再删除精确目录；失败／中断明确持久化，不允许启动或重放原意图。核心 Integration 14 passed／12.57 秒（SDK session 为明确替身），真实 Gateway 双工作区／uv wheel／真实进程 API 1 passed／121.32 秒，核 A1 移除后 A2 原配置及连接仍阻止卸载，退出和租约结清后才删除。精确合入后136项定向单元、Ruff及具名生产模块Pyright通过；正式构建与生成一致性复用代理同源码证据。完整命令、原始结果、源码指纹及混合文件边界见 `out/tests/temp/2026/11/05/mcp_generation_retention/artifacts/delivery-manifest.json` 和 `root-integration-002.json`。真实新generation升级、旧调用持续执行及失败保留仍待闭环；stdio并行策略已向用户澄清，2.5保持未勾，不以本片代替远端、卸载UI或原生矩阵。
  - 2026-11-05 同源新版切片：真实 Gateway／托管 Backend／本地 uv wheel 经同一 Thread 四次工具调用，验证 v2 安装失败和独立发布均不修改 v1 配置／连接，空闲后仅由原 candidate／CAS 显式切换至 v2；历史工具结果保持 v1，原 Process 已核退出、租约结清且两版制品仍发布。受保护单项 `1 passed / 135.47s / exit 0`，Ruff及正式 Workbench 构建通过；测试关闭裁剪 Provider 后遗留的网页 embedding 引用，未改生产切换策略。精确增量、源码指纹、原始失败与通过记录及进程回收见 `out/tests/temp/2026/11/05/mcp_generation_retention/artifacts/update-delivery-manifest.json`（SHA256 `22cb7e58746d0d6333797044b5a89af906ace735c314381ec0986969b4a4b147`）。在途调用跨版本继续执行及 stdio 并行策略仍待裁定，2.5保持未勾，不替代远端、UI或原生矩阵。


  - 2026-11-06 升级准入闭环：原来源 preflight／CAS writer 和 MCP 候选准备复用原 retirement／资源账本，封存资格、调用、结果未知或 cleanup_failed 未结清时在来源写盘和旧 stdio 停止前明确冲突；同一配置事务暂时阻止新资格，失败／取消／发布结算后释放，不新增持久占用或并行策略。普通 unmanaged HTTP 热替换沿原合同。真实 Gateway／托管 Backend／uv wheel／产品 Thread 1 passed／163.75秒，核候选保存、通用来源预检和保存均409，原字节／层／active／连接／launch不变，旧工具继续返回原依赖；结清不自动切换，重新显式提交才启用新版，原进程退出、历史结果与两版制品保留。
  - 当前安装／配置替换70项、制品保留14项及配置服务120项通过；私有来源2项在补齐原生安装 owner 的既有fixture接线后通过，未放宽断言。原保留测试已按裁定迁为在途及未知时拒绝切换、结清后显式切换、核旧进程退出后才卸载。Ruff具名模块通过，配置工具族原11条及Pyright原14条与完整HEAD基线相同，无新增。完整命令／源码指纹／首轮装具错误和120秒未完成回归原件见 `out/tests/temp/2026/11/06/mcp_version_switch/artifacts/verification.json`。结合前述A1/A2暖复用、冷归并、引用及物理卸载证据关闭2.5；2.3其它安装能力、2.4独立取消、远端／UI与原生矩阵仍按各自任务继续验收，不据此宣称整体完成。

## 3. MCP 与现有 Agent 唯一链路

- [ ] 3.1 在原 MCP 管理服务 seam 消费目标配置、安装及原 MCP owner 的完整对象，迁齐旧 React 连接入口；移除该运行面重复 client、enabled/status 与目录，不复制原列表／详情。
  - 2026-11-05 原入口迁移切片：原 `IMcpService`／`IMcpWorkbenchService` 消费项目 MCP owner 的精确目标 DTO，底部原列表、原详情 Webview、Server Options 和授权管理 Workbench 共用同一组对象；旧 React 列表／授权页／hook 及此运行面的原发现与工具 client 已物理下线。原可见性观察、完整替换、失败重读、目录成员通知、迟到请求隔离与面板恢复均迁齐；未接入的安装／配置／凭据写入明确拒绝。受保护定向单测 59 passed，31 份实际转换源码类型检查通过，Web 构建通过，Web tsc 保留与完整基线相同的 28 条既存诊断；本地完整浏览器场景通过，远端 turn／model_call 与真实 300 秒期限错误场景另 3 passed／712.02 秒，保留目标归属、真实工具执行、canonical／assembly request_hash 与秘密保护强断言。正式 Workbench Webview 使用与固定源码 v6 协议核对一致的官方 1.145.0 资产，仍保留 UUID 子域隔离。MCP 专项先经真实目标 `/agents` 等待 SDK 配置检查，不构成冷启动 Thread 性能验收；冷初始化及一次未进入业务的端口占用失败原件均保留。最终源码指纹、准确命令、原始输出和各场景边界见 `out/tests/temp/workbench_mcp_owner_20261105/artifacts/mcp-owner-final-evidence.json`；安装、完整配置与跨目标闭包未完成，整项保持未勾选。
- [ ] 3.2 配置计划通过原 workspace source／candidate／CAS 提交，补受管 launch binding／私有输入引用 schema 与解析消费者；在原 MCP 凭据 owner 扩展按 workspace／server／输入键版本及来源绑定的私有写入与引用，不能把 OAuth store 当完整 input secret 实现。核 A1/A2 secret 实际调用、重启、换地址不借用、正文／DTO／事件／日志脱敏，保留既存配置与 OAuth。
  - 2026-11-05 私有 HTTP header 切片：新增原目标私有写入口及不可变引用，source／candidate／CAS、原 SDK session、联邦配置授权和 proto／schema／生成消费者迁齐。真实 Gateway／双受管 Backend／HTTP MCP／Agent 1 passed／176.21 秒，核 A1/A2 同名服务的独有值、重启后原会话再调用、跨工作区及换 endpoint 拒绝、普通产物无原值。独审复现并修复首尾空格经 transport 异常泄漏，补写入口和冷私有文件负例，最终定向 152 passed；原候选单元 186＋135 和客户端 16 项按源码依赖复用。生成一致性、Buf breaking／format、完整 Web build 通过；Ruff 原 7 条与 Web tsc 原 28 条诊断无新增，均不记为全通过。准确命令、源码指纹、失败原件及复用边界见 `out/tests/temp/2026/11/05/openspec_delivery/artifacts/mcp-private-delivery.json`；目录 input／env 私有引用、完整安装 UI、Windows ACL 和真实远端联邦尚未由本切片验收，整项保持未勾选。
  - 2026-11-05 私有 stdio env 切片：物理统一为原 `McpPrivateInputStore`，迁齐 header 调用方；env 引用绑定 workspace／server／input_key／定义摘要／不可变版本，目录绑定真实来源与完整 manifest／variant，手动定义绑定目标规范化 command／args／cwd，仅在原 SDK 准备阶段取值。两条私有写路由沿原配置权限，目录另核安装管理；配置重新消费原候选并经原 source／candidate／CAS 保存，不保存脱敏展示投影。
  - 正式 Gateway／双工作区／uv wheel／原 Thread env 场景 1 passed／257.50 秒，覆盖 8 次实际工具调用、两次 restart-safe、两次同 generation 暖复用、3 类错误引用／来源／命令 422 及两次陈旧 CAS 409；原私有 HTTP header 回归随后独占串行 1 passed，4 次原 Thread 调用与两次重启。最终 58 项定向单测、客户端 17 项、生成一致性、真实基线 Buf breaking、相关 Pyright 和完整 Workbench build 通过；扩大 308 项按未变依赖复用，各批可能重叠不相加。Web tsc 28 项与 Gateway Ruff 7 项保留既有诊断及原始对照；新测试首轮装具向受管制品写 pyc 的失败保留，改为独立 stdlib 命令后通过，未改制品摘要或业务断言。
  - 准确命令、37 份源码指纹、原始输出及首轮失败归因见 `out/tests/temp/2026/11/05/mcp_private_inputs/artifacts/mcp-private-env-final-evidence-r2.json`（SHA256 `18f559e3dac055346b5bf3596a3e293c6021baedf089ef5dfd0910b820e0efb6`）。root 合入时全部 base／changed SHA 匹配，共享树定向检查另见 `out/tests/temp/frozen_wire_20261105/artifacts/env-shared-targeted-1793893938421006129.json`。真实同源新 generation 仍缺产品入口并保留代码 TODO，不能用暖复用替代；Windows、真实远端 B1 与其它 secret 模板未验，3.2 整项保持未勾选。
  - 2026-11-06 配置提案后端切片：原 MCP owner 重新消费冻结候选／已发布制品，提供脱敏 JSONC 提案及原 source 无副作用预检；不回显候选输入，不建立草稿存储。精确 Server 之外的成员、注释、秘密 token、CRLF 和缩进保留；新 Server 占位只以原候选还原，其它秘密仍由原文件 writer 处理。原生环境表达式保留，指纹仍核目标解释后的值。删除保存前重复全量预检，由原 writer 先结算幂等键并独占 CAS／副作用准入。
  - 最终真实 Gateway／双工作区／uv 制品／原 Thread 1 passed／238.55秒（外层242.17秒），覆盖提案零文件写入、脱敏、实际保存、同键重试、陈旧CAS409、8次工具调用、两次重启及错误引用／来源／命令拒绝。定向51项、公开合同／授权／原保存187项、客户端56项分别通过，各批存在重叠不相加。协议生成一致性、真实HEAD基线Buf breaking、格式、OpenAPI导出、完整Workbench构建及Ruff通过；生产与新增测试无Pyright诊断，旧联邦测试1条由完整b610归档重现，Web tsc仍与基线完全相同28条。独审的CRLF／缩进P3已修复并复审。原Workbench配置界面、Windows及真实远端该入口尚未验，3.2整项保持未勾选。准确命令、源码指纹、原始输出、失败修正与类型基线见 `out/tests/temp/2026/11/06/mcp_configuration_draft/artifacts/verification-v1.json`。
- [ ] 3.3 接入连接操作、OAuth、目录通知和工具调用原生命周期；验证目录 revision／generation、热替换、封存资格、排空及结果未知合同，无第二套资源租约或逻辑 session 固定功能。
- [ ] 3.4 支持受验证 VSIX 动态 definition Provider，保留来源实例／host generation／collection／原定义、provide／启动前 resolve／change／dispose／取消语义，仍明确选择 workspace 并交项目 owner 启用；验跨目标同 ID、离线、迟到 resolve、配置 unavailable，拒绝 A 本机动态定义到 B 执行，静态可迁移模板须重新校验且不搬秘密，不在 Extension Host 另启项目 Agent MCP client。
- [ ] 3.5 在本地 A1/A2 与远端 B1 的真实 Thread 调用独有测试工具，核同名 server、凭据、授权等待、原 tool_call_id 和实际目标；包安装成功不能替代 Agent 真调用。

## 4. 原版扩展管理与 Web 宿主

- [ ] 4.1 复用原扩展视图、详情和安装服务，接入经验证目录及用户明确 VSIX；安装前校验 manifest、版本、依赖、平台、所需 API／运行位置，区分安装与激活。
- [ ] 4.2 将纯声明式及 browser 扩展接入原 Web Extension Host／Workbench profile，验证真实命令和 UI 贡献；Node-only 扩展没有合法目标时明确不可用。
- [ ] 4.3 将安装选择器与目标分组接入原 Quick Input，冻结发起身份与版本；验证切 Tab、目录失败、目标离线不改投，不抢占默认 Ctrl+P 查询范围。
- [ ] 4.4 统一 VSIX 安装与目标安装服务的操作／版本／引用事实，保持 Web profile 资源与目标制品各自存储；实现原 profile／workspace view 启用、版本更新／停用／卸载和 reload，分清新版安装与运行激活，保留旧实例引用制品，不伪造动态卸载支持。

## 5. 多目标 Node Extension Host 闭包

- [ ] 5.1 清点原 Remote 及 extension ID 单例假设：description registry、enablement、RunningLocation／Tracker、HostManager／Factory、activation、依赖、MainThread／ExtHost RPC、贡献／卸载及全部消费者／测试，确定唯一扩展服务内的多目标接入点。
- [ ] 5.2 在现有 sourceTransform 与扩展服务闭包中实现稳定目标／客户端／明确 workspace view 的 RunningLocation 和 host 分派；以真实 A/B Node 扩展原型验证协议，不仅修改枚举或轮换全局 remoteAuthority。
- [ ] 5.3 在明确后端目标侧接入匹配上游的 Node runtime／host 组件及认证 RPC 通道，复用原启动和 activation 协议；不放到 Gateway 请求进程、Python Agent 或 Electron main。
- [ ] 5.4 绑定同目标明确 workspace 集合与原 multi-root 事件，分离各客户端活动态；不同 VSIX 启用集使用不同明确视图，不伪报同 host 的 folder 级隔离。验证 A1/A2 不包含 B1、视图变更有明确生命周期、一个客户端不覆盖另一个。
- [ ] 5.5 迁齐相同扩展 command ID 的来源记录与目标分派，保留 manifest／ID、菜单条件与快捷键；有发起目标直接路由，无上下文且多候选时明确选择，切焦点不改投。
- [ ] 5.6 实现宿主 generation、迟到 RPC fencing、惰性启动、有界断线回收与后端生命周期排空；真实崩溃／重启不重放未知副作用，宿主退出不删除安装或稳定存储。
- [ ] 5.7 迁齐公开 extension ID 与内部安装／运行实例身份，extensions.all／getExtension／exports 和依赖只解析该 host 合法实例；验证 A/B 同 ID 不同版本真实激活，更新／停用 A 不改变 B；静态声明冲突明确诊断并要求合法版本／窗口选择，不忽略重复注册或更名公开 ID。

## 6. 扩展 API 与状态的逐类验收

- [ ] 6.1 完成 design D8 能力矩阵与机器可查询诊断，按实际服务声明支持／拒绝；未支持 API 不返回空成功，不把类型存在记为可用。
- [ ] 6.2 迁齐文档／语言／符号／搜索 Provider 的目标注册、原 URI identity、模型同步与取消，沿现有 SearchService；明确过滤初始／dirty textDocuments、visible／active editors、诊断／watcher 和所有事件，跨资源编辑预检全部归属及版本。验 A/B 同开 dirty 文件并切焦点，非所属 active 为 undefined，无内容泄露或迟到发布。
- [ ] 6.3 接通 host workspace view 的 fs／watcher／configuration 与目标 owner，复用原 typed URI transformation；真实扩展分别经 Node 文件 API 与 workspace.fs 读目标独有文件并返回可打开位置，fsPath 在目标真实有效。验嵌套根原 folder 匹配、重复本机路径身份不足时明确拒绝，不把任意字符串全局替换、不建第二根绑定，不访问浏览器 IndexedDB 同名根或中继文件系统。
- [ ] 6.4 接通实际支持的 terminal／tasks／debug／process API 到目标运行服务，核目标 PATH／平台；不支持项明确错误，不在本地或 Electron 创建替代进程。
- [ ] 6.5 在原窗口 command dispatcher、TreeView／WebviewView resolver、setContext／菜单及资源通道实现按 host／view 的运行注册与展示绑定，静态声明一次登记；验 B 在 A 焦点调用、A dispose 不撤销 B、同 ID view 正确切目标、CSP／端口／迟到消息隔离，无绕过权限资源通道。
- [ ] 6.6 迁齐原 MainThreadStorage／ExtensionStorageService 等权威存储的目标隔离、globalState／workspaceState 稳定身份与变更通知；补目标本机有效 storageUri／globalStorageUri／logUri／extensionUri。验证真实文件写入、两客户端并发／重启和 A/B 隔离，不虚构事务合并，秘密留消费端。
- [ ] 6.7 核扩展依赖、原生模块与 proposed API 的真实兼容条件，保留明确不支持原因；用至少一个真实 Web 与两个目标的 Node 测试扩展逐类证明，不只验 mock handler。

## 7. 目标感知设置编辑

- [ ] 7.1 在原 Settings Editor／命令接入目标及来源层选择，持续展示 scope、目标、可写能力与原发起上下文，切导航不改投未提交内容。
- [x] 7.2 在原 Gateway／Workspace 配置 owner 补现有 REST 缺失的合法来源层能力、脱敏正文、schema、完整 effective／遮蔽读取及 source writer，迁齐协议和消费者；区分 SQLite 物化与文件回写，无 writer 层明确只读，不以摘要或有限 runtime PATCH 冒充完整能力。
  - 2026-11-04 Workspace 后端切片：原配置 API 提供按 source_key 的脱敏 JSONC、schema、各层正文、最终来源／遮蔽及 active 状态；仅工作区来源层提供 writer，其余层明确只读。原 proto／descriptor／OpenAPI／SSE 生成物已迁齐，未登记来源 URI 保持为空。用户级 source fan-out 仍归 7.5，设置界面仍归 7.1／7.3，不因本后端任务完成而勾选。
  - 2026-11-04 Gateway 后端切片：原配置 owner 提供 user／user_local 来源正文、纯预检和文件／层／active CAS 保存，inline 明确只读；writer 与 watcher 共用原 reload 锁、pending／claim／restart 链及原幂等键查询，不新增提交账本。脱敏占位按原字段还原，注释、格式及下层遮蔽保留；无变化保存不提升 active。真实进程覆盖 local／user 保存、原生文件删除 watcher、pending restart／重试及 request_id；恢复批覆盖真实 runtime snapshot 身份、六类启动恢复与撤权，不以假 applier 代替运行快照验证。Gateway schema 20 的 SQL、协议生成物和原消费者已迁齐。
- [ ] 7.3 业务配置表单与 JSONC 共用原候选预检、CAS、幂等提交和新增来源 writer，支持删除选中层覆盖、格式保留、冲突重取与编辑内容保留；禁用通用文件保存旁路，不把脱敏占位写回秘密。
  - 2026-11-04 Workspace 后端切片：预检复用原有效配置构建且不导入来源、不写文件或改变 readiness；保存核文件／来源层／active CAS，移除 JSONC 键即删除该层覆盖。秘密占位只在原字段及对象绑定还原，保留原秘密 token、注释和格式；普通正文前缀不触发占位协议。原 pending 行在文件替换前登记幂等意图，watcher／重试只认领匹配的原候选，pending_restart 等状态不重复执行资源副作用；有效配置未变化时不提升 active、不新增事件。表单及编辑器消费者尚未完成，本项不勾选。
- [ ] 7.4 接入原配置 applied／deferred／restart 状态及 pending 管理入口，核真实健康证明；未登记字段不宣称热应用，不以保存成功代替运行生效。
- [ ] 7.5 实现用户 Workspace source 影响预览与原 fan-out 汇总，保留逐 workspace 候选、版本和结果；验 B1 成功／B2 冲突／B3 停止追赶，不跨到 A 或伪造全局事务。
  - 2026-11-04 盘点：原 `WorkspaceSourceOwner` 持有用户环境的 `workspace:user` 来源和 generation／fan-out 事实，多个 Backend 可共用同一来源；Backend owner_id、generation 或当前 mounts 均不代表完整影响范围。实施前仍须明确跨 Backend、停止工作区的来源环境归属与成员交付。现有层导入的 `applied/materialized` 尚未关联后续 candidate／commit／active 结果，不能把直接公开 summary 当作本项完成；用户层来源 writer、影响预检和逐目标汇总合同仍未实现。不新增第二账本或跨库事务。
- [ ] 7.6 远端设置透传到原生 owner 判权与提交，断流后查询原幂等提交；本地只保留原连接登记和显示投影，不增加可生效远端配置副本。
  - 2026-11-04 双 Gateway／双 Backend 真实 socket HTTP：2 passed／128.88 秒，覆盖同 UUID 不同物理工作区、目标默认拒绝／精确主体读写／撤权、无权限零下游、预检零文件写入、同键并发只一次 applied、原键查询／重试及旧 CAS 409；只修改目标 B，A 保持原状态，request_id 与转发目标归属正确。复用原受控本机 SSH 执行／隧道替身，未验证真实 SSH、Windows 或设置界面，本项不勾选。完整命令、源码指纹、原 HTTP 响应及静态检查见 `out/tests/temp/configuration_federation_network_20261104/artifacts/network-final-evidence-1793813123212470975.json`；网络测试文件 Ruff／Pyright 均零诊断。
  - 2026-11-04 原生 Gateway 来源网络补验：1 passed／162.75 秒，产品配对生成的 A 主体经真实 HTTP 操作 B，覆盖默认拒绝、无主体通配不授权、只读拒写、精确读写及撤权、预检零文件写入、并发同键只一次 applied、原键查询／重试、旧 CAS 和错误 Gateway 409；A 文件不变，B 请求不进入 Backend。该测试直接访问原生 owner，不代表客户端 Relay、真实 SSH 或设置界面。命令、31 个依赖前后指纹与原始 HTTP 原件见 `out/tests/temp/gateway_config_source_edit_20261104/artifacts/native-network-1793816376452606941.json`。
  - 2026-11-04 原生来源 Relay 切片：复用原来源 JSON API、稳定目标请求头、registry 连接与管理发送链；由 B 判权并保存，原 body、CAS、幂等键、状态及 request_id 贯通。真实双 Gateway／双 Backend socket 1 passed／112.67 秒，覆盖只修改 B、同键并发只一次 applied、原键查询／重试及撤权；SSH 执行／隧道仍为原 loopback 替身。最终 stable02 的 50 项跨模块检查覆盖连接换代、请求先释放后 promote／rollback、目标歧义及禁止联邦入站再转发。网络在 stable01 运行，独审确认未变来源／授权／CAS 分支可复用，不称 stable02 网络重跑。status／pending 管理／events／SSE 等尚未接通远端的入口面对显式目标返回 409，不回落本机；UI、真实 SSH、Windows 及其余远端设置未验，本项仍不勾选。
- [ ] 7.7 将 Workbench 设置、会话设置、Gateway 用户视图和 registry 操作分别交原 owner，设置 UI 只导航合法对象；凭据沿原私有入口，不以 runtime candidate 包办所有状态。
- [ ] 7.8 新增字段确需版本变化时补同族 schema、默认值及官方预览／备份／可恢复迁移；保留现有 JSONC 层语义，不自动重命名或清空用户配置。
- [ ] 7.9 在原 Code OSS 配置服务迁齐 profile local／目标 remote user／稳定 view／folder／语言覆盖与 setting scope，按 host/view 路由原配置模型，经已登记目标 FileService／writer 访问 multi-root 原 `.code-workspace`；不轮换全窗口 workspace，扩展键不写业务 JSONC。验 get／inspect／update／change payload 投影、原缺省推导、非法资源拒绝和 A1/A2/B1 不随焦点改投。
- [ ] 7.10 设置搜索与 @modified／@ext／@lang 绑定冻结域／目标／来源层及真实注册 schema，验同 key 不同值、切焦点和旧查询取消，不将业务配置伪装成支持所有扩展过滤维度。
- [x] 7.11 落实 D3 权限表：pending retry／discard／resolve／失败记录核配置 write 和原证明／fencing，状态返回另核 read；实际 managed_backend_restart 核 managed_workspace_admin，消费候选再核 write。验配置权限不授予重启，Gateway restart-development 仍 local-only，不新增远端 Gateway 重启能力。
  - 2026-11-04：生产 ASGI 路由与受控上游验证 45 项通过，原 runtime 组 248 项通过，Gateway 序列化契约与认证迁移单例各 1 项通过。撤权后使用公开 `error`／`message` 事件，停止第二条配置披露、关闭上游并释放路由引用；原 controller 在 retry／drain／进程动作前拒绝缺少配置 write 的候选消费。该证据不代表真实网络或真实 SSH 验收。
  - 原始命令、逐文件 SHA 与退出码：`out/tests/temp/configuration_federation_permissions_20261104/artifacts/configuration-permissions-final-evidence-1793811673988170281.json`。Ruff 42 条与实测改前逐项相同；Pyright 生产组 70 条与实测改前逐项相同，main 另 1 条位于未改 handler 且未独跑其改前基线，新增 ASGI 测试无类型诊断。扩大代理回归尚被旧 `backend_workspace_id` fixture 阻断；配置扩大检查及迁后 validation 检查超时，后者仅观察到 46 个通过点，完整回归未完成。

## 8. 界面、快速入口与单链路迁移

- [ ] 8.1 原扩展／MCP 视图按此工作台及目标分组，详情显示安装／启用范围、来源版本、连接与日志；主窗口底部仍按工作区，右侧仍按会话，不维护重复状态卡片。
- [ ] 8.2 将合法操作与设置定位贡献到原 Action／Command／Opener，复用 `>`、`view `、`ext `、`mcpr ` 和帮助的真实能力；不因注册入口而承诺整个市场或全部 Node API 已可用。
  - 2026-11-06 MCP 制品管理入口：原 Action／F1 提供“管理目标制品”，明确选择已就绪工作区并冻结 Gateway／workspace／Backend；沿唯一安装 owner 列制品、实际引用、物理卸载和查询原操作，不修改工作区配置或创建新账本。取消浏览／确认不发 DELETE，有引用拒绝；确认期间新增引用由后端409拒绝并展示实际引用，结果未知保留原 operation ID 且不重放。原身份或Gateway失效中止同一信号，阻止401恢复后重发旧确认；409后回读失败同时保留两因及原request_id，两项独审问题已修复并复审。最终真实 Gateway／Backend／uv 发布与正式 Workbench 页面 1 passed／94.85秒，覆盖前述正常分支、实际文件删除、列表移除、原操作成功／removed／完整 binding 和 request_id；401及回读失败由请求／服务边界测试证明，未称页面已覆盖。其它命令、设置及 VSIX 入口仍未完成，本项不勾选。
- [ ] 8.3 按迁移清单迁齐旧 API、UI、间接调用与测试后物理删除同义实现和死命令，核无第二 MCP client、安装账本、有效设置或扩展服务。
- [ ] 8.4 迁移稳定目标格式及来源记录，核旧记录拒绝／官方迁移，保留 profile、草稿、凭据和引用；回退源码不自动反向执行已提交外部操作。

## 9. 真实产品与发行验收

- [ ] 9.1 为三个 spec 的全部场景建立证据映射，使用本地 A1/A2 与真实远端 Gateway B1/B2，设置同名对象和独有内容；先最小有判别力闭包，独立 fixture 才并行，不默认全套串行测试。
- [ ] 9.2 通过实际 Gateway 与原 Workbench 浏览器验证初始化 API、原扩展／MCP／设置交互、命令、作用域、权限与 request_id；保留目标侧进程、网络和原始状态证据。
- [ ] 9.3 用真实 MCP 工具、真实 Web／Node 扩展完成调用／文件／配置／终端及不支持能力断言；覆盖并发、取消、冲突、断流、迟到结果和重启，不以进程启动代替业务验收。
- [ ] 9.4 在源码目录外且关闭开发服务器的 Electron 打包产物验证静态资源、相同目标链、设置与扩展状态重启恢复；目标侧运行时独立发行可核，不增加 main/preload 原生能力。
- [ ] 9.5 每批完成相关静态分析、真实类型检查、协议生成一致性及受影响构建；涉及浏览器前端完成 `bun run --cwd src/clients/web build`。测试统一使用现有 runner 或进程外超时／内存保护，复用同源码已有构建证据。
  - 2026-11-06 MCP 制品管理切片：最终相关 MCP／安装／目录／转换／API／共享HTTP 113项通过，不与此前75项／29项重叠批次相加；57份实际转换源码的正式类型检查通过，最终产品测试已完成同源码的完整 Workbench 构建（Web build 指向同一脚本）。Web 完整 tsc 与前批基线均为完全相同28条既有诊断；额外将 Bun 测试并入浏览器类型程序的检查因全局计时器声明冲突及旧安装测试 DTO 不完整未通过，不记作正式类型程序通过。新正式 Python 测试 Ruff／Pyright 和页面脚本语法检查通过；未修改公开协议，未将本页验证扩大为 Windows／Electron／远端全部验收。准确命令、逐文件指纹、原始成功／失败输出、前轮驱动误判、独审修复及正式页面原件见 `out/tests/temp/2026/11/06/mcp_artifact_management/artifacts/verification-v1.json`。
  - 2026-11-04 Workspace 来源编辑切片：来源 API 恢复批 29 passed，启动／MCP／原故障合同批 22 passed，合法 pending 启动补验 1 passed，配置与旧库迁移单元批 128 passed；各批有重叠，不汇总相加。Buf 格式／lint／build／breaking、协议生成一致性、OpenAPI 导出及 Web 构建通过。Web 实际 tsc 退出 2，完整 HEAD 解包基线与当前均为相同 28 条诊断、无新增；新生产文件及 API／协议 Pyright 零诊断，其余生产组按真实 HEAD 对照无新增，调用方扩大类型检查被资源保护终止，未完成。
  - 扩大协议／reload 合同批为 81 passed 后遇旧 ValueError 文案预期失败，完整 HEAD 单例同样失败。发行 SQL 单例先遇既有 Gateway schema 18 断言（实际及 HEAD 迁移均为 19），尚未执行到已迁齐的 Workspace schema 24 断言，不记作发行验收通过。本批最终证据及 39 个文件指纹见 `out/tests/temp/workspace_config_source_edit_20261104/artifacts/workspace-source-edit-final-evidence-20261104T174359459564Z.json`；Ruff 最终与原 HEAD 对照无新增，仅合并嵌套条件的修订按短路语义等价核验复用行为证据。不将本后端切片扩为整个设置模块、真实 Web 交互或原生目标验收。
  - 2026-11-04 Gateway 来源编辑切片：30 个不同来源单元参数实例有逐项通过记录，非一次整组全绿；SQL 升级／冷重开 21 项、HTTP／descriptor／Node 与 Web 协议消费者 51 项、原 loader／迁移／pending／hot／restart／补偿 12 项通过。真实来源进程的两项分别通过，restart 单例 55.37 秒，不重复计算前轮已通过实例。Buf 格式、breaking 与生成一致性及完整 Workbench 构建通过；Web tsc 与完整旧版本均为相同 28 条诊断，生产 Pyright 与完整 dc660 归档均为相同 25 条诊断，新增测试及独立网络测试 Pyright 零诊断，Ruff 无新增。原状态文件 current／dc660 基线均为 43 passed／16 failed，失败实例及异常原文一致，保留既有失败。发行 SQL 单例已迁齐 Gateway 20／Workspace 24 并通过，覆盖重开及缺 SQL 资产明确失败。各批可能重叠，不相加；准确命令、逐文件指纹、旧失败对照及原始输出见 `out/tests/temp/gateway_config_source_edit_20261104/artifacts/gateway-source-final-evidence-20261104.json`，独立网络证据见 7.6。最终仅清除 SQL owner 文件末尾空行，AST 不变并复用行为证据；该后端边界不代表设置界面、真实 SSH、Windows 或全部扩展功能验收。
  - 2026-11-04 来源 Relay：50 项集成、61 项 HTTP 合同、5 项 sender／退役回归及 2 项直接调用回归分别通过，各批可能重叠，不相加。完整 HEAD 静态对照 Ruff 10→10、Pyright 125→121，直接调用方 Pyright 47→47，无新增诊断；独立网络测试 Ruff／Pyright 均为零。扩大单元首批的旧 removal 装具缺 gateway_id，在 DTO 解析时失败，未扩修；新增三项关闭顺序断言迁齐原 terminate→close 后已由最终集成覆盖。未改 proto／TS 正文，复用同依赖的前批生成及构建证据。准确命令、退出码、日志、45 项 stable02 指纹与 owned SHA 见 `out/tests/temp/gateway_config_source_relay_20261104/artifacts/gateway-source-relay-final-evidence-20261104.json`；真实网络原件见同目录 `network-socket-1793818654614861011.json`。
- [ ] 9.6 保留准确命令、源码指纹、原始输出、退出码、未验证阶段及真实兼容矩阵，按正式／临时目录规范落盘；交付前检查无双轨、静默失败、越权、无关改动和虚假完成，再按实际证据勾选。

### 2026-11-05：2.4 PyPI 操作终态与中断查询

- 原 SQLite 安装账本在同一写事务内读取原意图与状态；迟到进度／取消／失败只能观察既有终态，发布只能从 running 同事务提交制品和 succeeded。复用原 run_sync_shielded 等待进度／摘要／发布 worker 结算，删除失败前先读状态的竞态分支，不新增账本或 schema。
- 定向19 passed／0.87秒，真实 Gateway→Backend→uv 恢复1 passed／56.98秒，均有外部超时与4GiB保护；四份owned Python的Ruff、Pyright零诊断。覆盖发布中取消后保留成功、进度中取消后中断、迟到发布拒绝、原错误及写失败原因保留；真实入口验证4个同键并发唯一操作、成功重试、异入口409、阻塞实际wheel下载后原PID退出、正式runtime/start恢复、原操作interrupted且不重放、旧制品保留及工作区配置不变。
- 准确命令、源码／日志SHA、原始输出、实际退出码及owned patch见 `out/tests/temp/extension_next_20261105/artifacts/final-evidence.json`；产品原件为 `out/tests/integration/gateway/test_mcp_installation_recovery/runs/run-4f18389ed259/nodes/node-c9d6543a3801/artifacts/`。首轮误用要求存活drain端点的restart-force，改用既有死亡owner的runtime/start后通过；失败原件保留，未修改Gateway行为。
- 仅完成既有PyPI链的终态与恢复子项；未新增用户取消API、npm／OCI／NuGet、版本复用、引用或物理卸载，不将2.4整项及其它生态记为完成。没有公开协议或前端变更，不重复运行无依赖变化的Workbench构建。
