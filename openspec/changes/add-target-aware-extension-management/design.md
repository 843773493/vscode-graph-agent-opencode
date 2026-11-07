## Context

实施与验收提示：普通目录、配置和状态读取遵守[交互总延迟预算](../enforce-interactive-latency-budgets/design.md)。包首次验证、迁移／恢复及用户明确执行的安装单独计时；不能将制品全量校验、整个环境预检或每层独立长超时默认放进每次列表／状态暖读。鉴权、精确owner、CAS和实际数据检查保留，不另建性能监控体系。

本稿只制定规划；新增 capability 与任务不代表实现完成。一个 Gateway 可管理一个多工作区 Backend，并通过远端 Gateway 管理其它 Backend；同一 Workbench 可以同时显示这些工作区的编辑器。项目业务 workspace 与 Code OSS 的窗口级 Workspace／WorkspaceFolder 不能直接互换。

### 已有 owner 与上游接入点

| 范围 | 当前依据 | 接入边界 |
|---|---|---|
| 多工作区 | `app/main.py` 的 mounted registry、显式 workspace API；`add-multi-workspace-backend-mounting` | 不为每工作区另启动 Backend，不复制 root binding；目标原生 Gateway 持有登记权威 |
| MCP | `app/services/infrastructure/mcp/`、`app/api/mcp.py`；现行 `mcp-connection-supervision` | 同后端不同 workspace 的连接、凭据、目录 revision、generation 和调用资格保持独立 |
| 配置 | `app/api/config.py`、Gateway 配置 API、`configuration-domains`／`configuration-evolution` | 已有 source layer、SQLite 物化、CAS、候选、active／pending、fan-out 与生效分类；当前 Workspace REST PATCH 仅支持有限字段的 runtime_override，来源正文编辑须补原 owner 合同 |
| MCP 管理 UI | 固定上游 `contrib/mcp/browser/mcpWorkbenchService.ts`、`mcpServersView.ts`、`mcpServerEditor.ts` | 原界面已有目录、配置和安装服务合同；新实现沿同一服务 seam 接入项目 owner，不复制界面或另启 MCP 运行时 |
| MCP 目录 | `platform/mcp/common/mcpGalleryManifest.ts`、`mcpManagement.ts` | 独立 gallery manifest、服务配置、包与远程地址，区别于 VSIX 扩展目录 |
| 扩展宿主 | `services/extensions/common/extensionHostKind.ts`、`extensionRunningLocation.ts`、`extensionRunningLocationTracker.ts`、browser extension service | 原版 LocalWebWorker／LocalProcess／Remote 分派与管理器可复用，但 Remote running location 未区分多个后端目标 |
| 快速入口 | `add-workbench-quick-access` | 原 Registry、命令、Opener 与两级搜索，不再造插件专用快速访问平台 |

参考基线为当前固定上游 HEAD `fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca`。以上只是源码盘点；真实可用宿主、市场、扩展 API 和原界面需经任务验收，不能由类型存在推断完成。2026-11-07 统一目标修订改变了原 Web profile 安装归属；既有源码和历史验收不视为已经满足新规划。

同日进程归属与职责修订：VSIX 管理由目标 Gateway 通过现有生命周期机制常驻托管，替换 Backend 按请求启动 Node 的实现。Node 仅复用固定上游的原安装／扫描／必要 profile 语义及其内部依赖，不承担 Python 的通用基础设施；下载、配置与包资源文件 I/O、监听和日志收集迁齐目标侧原 Python 服务。原安装器内部必需的 FileService／watcher 不要求重写，也不向 Python 开放为通用服务。不引入整套 VS Code Server、第二个进程管理器或安装账本；Node Extension Host 的运行归属和 MCP owner 不随之迁移。旧 Node 配置／包文件接口的历史验收不视为满足本次职责收口。

## Goals / Non-Goals

**Goals:** 在原管理视图的标题工具栏选定目标，后续操作直接沿用，扩展、MCP 和设置复用同一控件与规则；全部用户安装由目标持有，browser／Node 只决定运行分派；复用原版交互及扩展 API；让本地／远端多工作区操作始终路由到精确 owner；维持单一 MCP、配置及安装事实源；在 Web 与 Electron 保持同一产品功能边界。

**Non-Goals:** 自创插件 ABI、通用插件总线或权限平台；让 Python Backend 直接执行 VSIX JavaScript；增加 Electron main/preload 系统能力；隐式共享 MCP 逻辑会话；承诺微软发行版专属服务或任意 VSIX 全兼容；配置跨 Gateway 自动继承／同步；以项目私有磁盘扫描替代正式 API。

## Decisions

### D1. 一个变更、三个 capability，分别闭合各自的纵向链

`mcp-installation-management` 管目录、安装与工作区使用；`multi-target-extension-hosts` 管 VSIX 和真实宿主；`target-scoped-settings` 管编辑目标、来源及提交。共享的只是明确目标、操作归属与现有管理路由，不新增同时统治三者的业务对象模型。

采用独立 change，而不把插件宿主、包安装和设置状态机塞入[快速访问设计](../add-workbench-quick-access/design.md)。快速访问只消费正式命令和对象；这三个 capability 不要求全部串行落地。只有共享合同／依赖必须先迁齐相应生产消费者，未实现能力在 UI 明确不可用。

### D2. 在管理视图标题工具栏选目标，默认本地；后续操作直接沿用

在原 Extensions 视图容器的标题工具栏增加目标按钮／下拉，与 Refresh、More Actions 等原动作共处；保留原侧边栏入口、列表、搜索、详情及安装交互，通过原标题动作贡献接入，不复制面板或操作 DOM。初次打开默认“本地”：它指当前页面入口的原生 Gateway 管理环境，不指浏览器所在电脑；直接访问 B 的页面时，本地即 B。只有用户点击目标动作时才打开原 Quick Input 选择目标，不能在每个插件点击 Install 时重复询问。

管理视图保存自己的当前目标，列表、库存、市场搜索的目标能力判断及新打开的详情都使用该选择；原 Install／Update／Uninstall 等动作直接沿用相应视图或详情绑定的目标。切换目标时刷新该目标的数据，取消或丢弃旧目标的迟到查询，不混显安装状态。由视图打开的详情携带明确目标；已有详情和已开始操作保持原目标并持续显示归属，不因之后切换列表目标而改投。命令面板的安装入口复用原扩展管理视图当前目标，尚未选择时默认本地；文件或 VSIX 来源不隐式改变安装目标。

MCP 管理视图标题工具栏、设置编辑器顶部在需要目标切换时复用相同控件、目标选项与交互规则，各自保留当前选择；共享控件不等于共享一个随焦点变化的全局目标。打开带有明确目标的既有管理对象时，展示并保留该对象目标，不重写其它管理视图的选择；普通编辑器、会话或工作区焦点变化不改变管理目标。选择沿原 registry／导航／Quick Input，不新增目标登记或选择平台。

设置目标发现属于原配置 owner 的专用 `/api/gateway/extension-configurations/targets` 入口，使用 `configuration_read`，不复用扩展安装域目录或其 VSIX 管理授权。请求只带 Gateway 身份及可选的明确远端连接；响应只含 Gateway 身份和已登记、身份已确认且当前可路由的 Backend ID 列表。列表由现有 registry／Backend 管理路由得出，不要求 workspace，不推测未知 ID，不触发启动，并且不暴露库存、扩展能力或 route 别名。远端 Relay 沿 `management_gateway_route` 保持 request_id 与连接租约，并在响应披露前复核连接身份及 `configuration_read`；配置写操作仍独立要求 `configuration_write`。

远端目标由其原生 Gateway 判权并交所管理的 Backend／配置 owner 执行；当前 Gateway 只转送。SSH 是既有连接方式，不形成另一安装路径。每个 Gateway 可以提供自己的工作台页面；直接打开 B，或在 A 的页面选择 B，看到的 B 安装、版本与配置来自同一 owner。跨端搜索和消息传递沿既有索引、资源归属与路由，不触发插件复制或隐式跨目标启用。

只有配置／启用确实属于工作区或来源层时才补选范围，已有明确上下文则带入。稳定 Backend、profile、view 和 generation 继续用于内部路由及生命周期，不逐项暴露为用户必须重复选择的“目标”。一个 Gateway 下若有多个合法安装域且上下文不足以唯一确定，在视图选择目标时明确补选该域，后续安装沿用，不逐个插件重选或任选首项。

| 维度 | 身份与规则 |
|---|---|
| 安装位置 | 所选原生 Gateway 管理的稳定 Backend 安装域，Web／Node VSIX 共用原安装与版本事实；主机名、端口、当前 route 和显示名不作身份 |
| 配置／启用范围 | 已选目标内合法的用户来源层、profile 或精确 workspace／view；纯窗口展示态仍归当前 Workbench，不作为另一插件安装域 |
| 运行实例 | browser 入口在当前浏览器的原 Web Extension Host，Node 入口在所选目标的 Node host；均保留安装目标来源及所需客户端／view／generation，MCP 沿精确 workspace／server／connection generation |

物理机器相同不自动合并不同 BOXTEAM_HOME、目标安装域或授权身份；同一明确安装域内可复用精确版本包。A/B 使用同一个包时，MCP 连接与 credentials 仍各自创建；不因包相同复用原逻辑 session。已有的跨 execution MCP 资源使用延期设计仍保持 TODO，不借安装功能引入持有／释放工具。

安装操作在发起时冻结目标、来源及版本；配置操作另冻结来源层和 CAS 基线；运行选择消费后端已确认绑定。切换管理视图目标只影响后续读取和新操作，不重新绑定已有详情、未提交设置或在途请求，也不停止、迁移或重建其它目标正在运行的扩展宿主。切换 Tab、导航项或 Gateway 不改投在途请求。目标失效明确失败，不按同名目标、localhost 或当前活动工作区回退。

### D3. Gateway 统一托管常驻扩展管理服务，运行宿主保持独立

```text
同一 Web／Electron Workbench
├─ 原扩展／MCP／设置界面 + 各视图目标控件（默认本地，共用交互）+ 原 Quick Access
├─ 原 Web Extension Host（执行所选目标提供的 browser 入口）
└─ 当前 Gateway：登记、授权入口、管理路由、目标生命周期
   ├─ 本地 Node 扩展管理服务：每安装域一个，随服务组启动并持续复用
   │  └─ 原 VSIX 安装／更新／卸载／库存／必要 profile 与 scanner
   ├─ 目标 Python 基础设施：原 HTTP 下载、文件 I/O、异步监听、日志收集
   ├─ 本地 Backend target
   │  ├─ 原 MCP 安装 owner + 独立 Node Extension Host
   │  └─ workspace A／B：各自配置、MCP owner、业务数据
   └─ 远端 Gateway：原生目标的最终授权、路由及组件托管
      ├─ 远端 Node 扩展管理服务：同一目标安装域的唯一服务
      └─ 远端 Backend target：原 MCP owner、Node Extension Host、workspace C／D
```

目标原生 Gateway 复用管理 Backend／Browser／Terminal 的现有组件生命周期，连接、检查就绪、接管日志并在完整停机时停止独立 Node 扩展管理服务。现有 Node 组件直接提供仅限 loopback、沿原本机凭据认证的 HTTP 服务，不新增代理进程，也不移交 Launcher supervisor。Gateway 启动先核验已有服务的安装域、实际进程、运行实例、组件版本及日志归属并接管，确认没有实例后才启动；不能因发现记录存在就信任任意地址或进程。未配置运行时时明确报告扩展管理能力不可用，不由普通读取隐式下载或补启动；已配置组件启动失败或异常退出沿原进程管理机制明确报错，不能假报就绪。

每个稳定安装域只有一个管理服务实例，独占锁由管理服务自己持有，Gateway 连接和接管不抢锁；安装域身份仍消费原生 Gateway／稳定 Backend 安装域登记，不以进程 PID、workspace 或客户端区分。多个 Backend、客户端及换代中短暂并存的新旧 Gateway 沿已有受权内部通信机制调用同一实例，具名操作由该服务串行准入。复用原 `ExtensionManagementService`、安装／profile／scanner 及适用的具名管理通道，按原变更通知语义更新安装状态，不靠每请求退出消除过时内存。服务发现记录只保存运行时接管所需信息，不是安装账本。迁齐全部消费者后删除 Backend 自行启动管理 child、逐请求初始化／排空／退出及临时 stdio 管理入口，不保留两套实现，不增 broker、目标 registry 或独立进程管理框架。

Python 与 Node 按职责隔离，不按“哪个进程已有某个库”决定调用方向：

| 职责 | 唯一承担方与边界 |
|---|---|
| VSIX 安装／更新／卸载、库存与 profile 成员 | 原 Node 安装服务；Python 消费权威结果及必要的精确发布／profile 描述，不扫描目录或另存成员清单 |
| VSIX 与依赖制品下载 | 目标 Python 既有 HTTP／下载能力；通过原 `IDownloadService`／`IRequestService` 的适用接缝提供传输，原市场及依赖规划不重写成另一套业务实现 |
| 配置模型、scope、语言覆盖与 JSONC 编辑 | 原 Code OSS 配置服务；目标文件 I/O 交 Python，不重写为项目业务 candidate 或另建有效配置 |
| 配置文件和已授权包资源的 stat／read／write | 目标侧原 Python 文件基础设施，按原 Gateway 基础设施与工作区 owner 的归属处理；包资源仅只读，安装成员和版本选择仍以原安装器为准 |
| 文件变化通知 | 复用 Python `WorkspaceFileWatchService`／`ConfigFileWatcher` 的适用能力；它们是所属进程内的异步服务，不新增独立 watcher 守护进程或借 Node 另开通用监听 |
| 日志与环境 | 各组件产生自身日志，由原 Python 进程管理／日志体系收集；目标环境沿原运行时 owner，Node 只提供本组件和安装域必需的信息 |

管理通道只开放上述具名安装操作及必要描述，不能透传任意 method、脚本、shell、文件或监听命令；原生 VSIX 路径只作为已准入安装来源参数。原安装器内部必需的 FileService、配置依赖与 watcher 继续随该组件运行，但不得向 Python 提供通用 API。Python 缺少某项通用能力时，先在原 Python 服务补齐，不扩大该 Node 进程的职责。进程分离和受权接口不是 OS 沙箱，本轮不新增沙箱或权限平台。

包资源由原安装 owner 确认精确发布身份和合法版本，再由 Python 读取字节；沿原安装锁／运行引用保护读取与回收，不在交接间引入版本切换或目录回收竞态，不按文件请求重扫整包或另建发布账本。配置及包文件消费者迁齐后，删除 Node 对外的配置 stat／read／write、视图配置文件创建和包文件 stat／read 链路；必要 profile 定位描述不因此成为任意文件访问入口。

工作区切换、页面关闭及单个 Backend 停止／重启不终止共享管理服务。Gateway 换代时旧版释放自身管理句柄和日志描述符，服务继续运行；候选 Gateway 初始化失败只释放其接管句柄，不能停止旧版仍在使用的实例。完整停机才停止新准入、结清已接受工作及包资源读取、释放原安装消费者，并由原生命周期机制核实实际进程退出后结清日志归属。取消某次管理请求不等于关闭共享服务；包资源描述与 Python 字节 I/O 共用 Node 服务的串行准入，读取结清后才允许后续安装或回收。写入结果未知时保留原结果查询与错误归属，不因组件恢复自动重放安装。不由任一 Backend 私自接管或终止共享进程。

实际执行扩展代码的 Node Extension Host 仍由目标 Backend 按客户端／明确 view 管理，Web 入口仍在浏览器 Worker 运行；MCP 安装和连接继续使用原 owner。Gateway 请求进程负责路由、授权和基础设施托管，不执行插件或 Agent 业务，也不直接读写工作区 `.boxteam/`。管理 Node 只处理原 Code OSS 安装与必要 profile 语义；工作区文件来源仍交原文件 owner 核验解析，目标基础设施文件交相应 Python owner。宿主 RPC 使用独立认证通道，沿现有连接／联邦机制协商，不把任意客户端 URL 当作目标。

复用目标原生 Gateway 的现有本地管理认证与联邦 policy；发现／读取权限不推出安装、进程执行或配置写入权限。新增管理动作须在原 policy 合同中明确登记并同步 schema／消费者；不复用一个语义不符的权限名称冒充授权。Relay 透传原请求与错误，由原生 owner 最终判权；单次管理请求保持同一 request_id，operation 与 host generation 是不同标识。初始请求后的独立状态读取使用自己的 request_id。

远端设置新增 `configuration_read` 与 `configuration_write` 能力：前者覆盖获准目标的脱敏 schema／source／effective／pending／提交状态读取及事件订阅；后者覆盖来源预检／候选创建／提交／删除覆盖和 pending 管理，需读取能力时分别核验。重启另沿原生命周期管理能力，不由配置写权限隐式授予。两者沿原 policy 注册，作为管理能力默认拒绝，允许须绑定已认证主体；通配的 discovery／read／managed_workspace_admin 不代授。长订阅在实际披露点核现行权限，撤权停止后续披露／写入准入；已接受操作仍按原身份查询可获准状态，不伪报撤销副作用。该要求不擅自重定义其它既有业务 API 的授权语义。

MCP 候选规划与后续制品安装管理使用原 policy 的独立 `mcp_installation_manage`。该能力默认拒绝，只有精确绑定已认证主体的显式 allow 生效；discovery、read、managed_workspace_admin 和配置读写均不代授，安装管理也不授予配置写入、Backend 重启或宿主管理。安装域发现、候选原件读取、安装计划、消费前重验、库存与操作均走明确 Gateway／Backend 的管理入口，不借活动 workspace 路由。两种 workspace API mount 只承载精确工作区的配置与私有输入；其中候选配置规划及候选私有输入仍另核安装管理能力。所有入口均在下游请求前及实际披露前核现行能力；Relay 沿原目标路由透传，原生 Gateway 最终判权，无权限时不访问 Backend 或目录来源。迁齐消费者后，旧 workspace 安装入口明确返回已移除，不保留第二条安装链。

目标扩展管理使用两个互不代授的原生 policy capability：`extension_installation_manage` 覆盖明确目标上的 VSIX 制品安装、更新、移除及其安装状态；`extension_host_manage` 统一覆盖该目标 Web／Node 扩展的运行准入、激活、运行状态与停止，以及各自适用的宿主生命周期／通道管理。Web 代码仍在当前浏览器执行，运行授权由安装目标的原生 Gateway 判定，不以浏览器位置或 Node 宿主可用性代替。两者均沿原 `ADMINISTRATIVE_CAPABILITIES` 注册并默认拒绝，仅精确绑定已认证主体的显式 allow 生效；discovery、read、`managed_workspace_admin`、配置能力及另一个扩展能力均不得代授。制品管理权限不授予 Web／Node 扩展运行，宿主管理权限也不授予安装或替换制品。Relay 透传目标和原操作，由目标原生 Gateway 最终判权；没有对应授权时不得触达目标 Backend、包资源或宿主。

只读安装描述与包资源可由上述任一能力读取，供安装详情、资源展示或宿主初始化复用；读取包字节不代表获准在产品宿主执行。资源 owner 同时限制实际目标、安装域、扩展及版本：当前安装版本可按上述读取权限访问，更新或逻辑卸载后仅为仍有效的原运行实例保留其冻结旧版本，不能凭任意版本 URL 读取其它保留包。旧实例资格须仍有运行授权；仅安装权限不能新建旧版本实例或延续已撤销的运行绑定。资源准入及实际披露沿原生 Gateway 的现行策略，浏览器缓存与 Relay 不构成另一授权来源，不新增第三项权限或独立资源授权账本。

| 操作 | 权限与执行边界 |
|---|---|
| MCP 候选原件读取、确切 variant 计划、消费前重验及后续制品安装管理 | `mcp_installation_manage`，原生 Gateway 按精确认证主体校验；配置提交、重启与宿主管理分别核其能力 |
| VSIX 制品安装、更新、移除及安装状态 | `extension_installation_manage`，目标原生 Gateway 按精确认证主体校验；不授予 Web／Node 扩展运行 |
| 目标 Web／Node 扩展运行准入、激活、运行状态、停止及适用的宿主生命周期／通道管理 | `extension_host_manage`，目标原生 Gateway 按精确认证主体校验；不授予 VSIX 制品变更，Web 不要求 Node 宿主可用 |
| 只读安装描述、入口脚本、静态贡献及相对包资源 | `extension_installation_manage` 或 `extension_host_manage` 任一；限当前安装版本或合法运行实例冻结的版本，旧实例继续核运行授权，不授予执行或制品写入 |
| source／effective／pending／操作状态读取和事件 | `configuration_read`，按目标和实际披露内容校验 |
| 预检、创建／提交、删除覆盖、pending retry／discard／resolve／失败记录 | `configuration_write`；返回配置投影另核 read，保留原 CAS／证明／fencing 校验。retry 状态不等于执行重启，resolve 不接受客户端伪造健康证明 |
| 实际重启受管 Backend | 原 `managed_backend_restart` 是操作名，仍核 `managed_workspace_admin` 管理能力；若消费配置 candidate，再核 `configuration_write`，不以 retry 按钮绕过 |
| Gateway 自身／开发栈重启 | 保留原 `restart-development` 的 local-only 与源码环境要求；远端入口只显示需在原生管理端处理，不假定 Backend 重启权限涵盖 Gateway，不新增联邦 Gateway 重启能力 |

宿主传输沿独立 WebSocket 通道协商，不把 RPC 混入普通工作区 API。首个 JSON 请求固定协议版本、原生 Gateway、稳定 Backend、view 和有序 workspace 集合；原生 Backend 返回同一绑定、目标生成的 generation 与原 request_id，随后只传原上游协议二进制字节。Gateway 在披露握手前核对目标、视图、客户端绑定及 request_id，不重编码或重放扩展 RPC。

已认证握手同时返回目标平台、Backend 父进程 PID、原生管理组件提供的 appRoot／profile 存储位置、该 generation 的日志位置及原 mount 解析的有序根集合。每个根同时带工作区资源 URI 与目标原生 file URI，二者只是此次已绑定视图的投影，不建立第二份根登记；file URI 只作为目标侧原协议初始化参数，不能在 Relay 或浏览器机器上解释。稳定 workspaceState ID 由原生 Gateway、Backend、明确 view 及有序工作区集合共同确定，不含临时客户端、generation 或 endpoint；重连保留身份，变更视图不借用旧状态。客户端沿原宿主初始化和 typed URI transformation 消费这些参数，不根据导航焦点或本机目录猜测目标位置。

本地通道同时核本机管理 token 与既有 UserAccess cookie；客户端身份由访问会话及租约代际派生，不接受浏览器自报，也不把原 cookie 传给 Backend。用户与游客沿既有有效访问租约，续租仍由原 heartbeat 入口负责，宿主通道不替闲置客户端续租。联邦入站按已认证 peer 划定客户端命名空间，并在原生 Gateway 核独立宿主管理权限；已认证联邦请求不能再次转发到另一远端目标。每个 workspace 分别取得原 route reference，集合须抵达同一 Backend；任一路由失效、租约失效或权限撤销均结束连接，策略发布直接唤醒空闲连接，不增加逐帧数据库轮询。

Backend 私有通道只接受 loopback、无浏览器来源头及已配置运行时控制凭据的请求，且复用唯一宿主 owner。关闭先通知传输停止，再等待双向任务实际结清，随后关闭 IPC 并核实原子进程退出；不能用通知已发出代替收发任务已退出。传输认证和内部 mount 集合校验不代表 MainThread 初始载荷、事件和窗口贡献已完成工作区投影，后者继续按 D7／D8 单独迁齐与验收。

宿主实时发现使用 `POST /api/gateway/extension-hosts/resolve`，输入明确 Gateway、有序 workspace 集合及可选的已知 Backend 身份；已知身份不得因当前地址可达而改写。该入口与通道共用原路由解析和逐 workspace 引用，沿同一原生 `extension_host_manage` 授权读取 Backend 唯一 owner。响应标注 `source=live`，返回实际稳定 Backend、平台、已绑定组件版本及独立 `channel_state`；缺少专用 runtime、平台未支持和 Backend 排空分别具名说明。通道可用不代表完整扩展 API、激活或业务执行已通过；离线、路由换代或撤权直接失败，不以登记镜像替代实时结果，也不为发现启动 Backend、宿主或下载运行时。

共同目标发现使用 `POST /api/gateway/extension-targets/resolve`，从原生 Gateway 的原安装域登记及组件管理事实返回稳定身份和当前主体的两项能力，不披露宿主运行态、不启动 Backend、宿主或管理进程。至少一项管理能力获准才访问目标管理服务；目标级发现不以 Backend 活跃或工作区已挂接为前提。原生 Gateway 在准入及披露时重核，Relay 不以自己的权限扩大结果。原窗口的运行视图仍从明确挂接的根按稳定 Backend 分组；目标级安装管理独立消费原 registry，不能只列出已运行或已挂根的宿主；多根组再核完整有序集合并使用该次完整组的权限，单根复用原回执，不合并各根的权限。运行视图仍由原 running-location owner 持有，安装权限与宿主权限分别消费同一目标投影，不增目标 registry。

目标安装库存沿 `POST /api/gateway/extension-installations/inventory` 读取，绑定所选 Gateway 和稳定安装域。目标级发现／库存不要求用户先挂接工作区、启动 Backend 或扩展运行宿主；现有以非空 workspace 集合定位 Backend 的入口须迁齐原 registry 的目标解析，只有工作区来源／启用操作才携带相应集合，不编造 workspace。此只读清单可由 `extension_installation_manage` 用于安装管理，也可由 `extension_host_manage` 用于原宿主初始化；后者不授予安装、更新或移除能力。原生 Gateway 转交其常驻 Node 管理服务读取 Code OSS 默认 profile，安装、profile 成员与 scanner 共用原实例；Python 按原发布描述提供包资源字节，不扫描安装目录或持有第二份清单。响应绑定原 Gateway／稳定安装域、实际需要的 workspace 及 `profile_scope=default`，保留原描述及 invalid 诊断，不声称已覆盖系统扩展、开发扩展或窗口启用集。Relay 保留目标和 request_id，读取前及披露前核现行权限与路由；身份不符、离线、组件不可用及损坏明确报错。管理服务关闭保留 profile；写入取消或控制结果未知不自动重放，单个 Backend 排空不关闭该共享服务，Gateway 服务组关闭按前述唯一 owner 结清日志和进程资源。

用户明确选择的目标侧 VSIX 通过 `POST /api/gateway/extension-installations/install` 进入目标 Gateway 托管的同一原生管理服务；请求冻结稳定目标、有序工作区集合及 `source_uri`，当前支持该目标已挂接工作区的普通文件来源，由工作区原文件 owner 保护私有目录并解析目标路径，再移交同机管理服务；Gateway 不自行解释或读取工作区私有数据。安装回执返回原安装器的 ID／版本／profile 与完整库存，不表示已有宿主切换版本。原安装器保留已存在版本的目录；同版本沿其复用规则处理，新版本发布不自动卸载旧版。传输失败不重放安装，客户端应先查询原目标库存；物理卸载与在用版本回收仍须另行闭合原引用规则。Gateway 沿原安装管理权限及管理路由转交本机唯一服务，Relay 转交远端原生 Gateway，不能按当前导航替换目标或把中继机器的路径当作来源。

原生 VSIX 的 manifest 预检在原 `ExtensionManagementService.install` 创建安装任务前完成，沿用官方 `validateExtensionManifest` 及原安装包根语义，仅将 Error 诊断作为拒绝原因；Warning 继续允许安装。保留原 engines 前置兼容判断及错误，包括完整 validator 对纯声明式扩展跳过版本校验的场景。首次安装或升级被拒绝时，原 profile 的存在状态、字节和已有制品均保持；不能发布无效候选后再由库存检查报错。Python、窗口及 Relay 不复制字段校验器或补建安装清单。该预检只阻止原 scanner 本就会标为 invalid 的 manifest，不代替目标平台、宿主能力、依赖及激活验收。Node 与浏览器类型检查共用源码投影，按各自运行面的实际转换结果检查，不以未转换的上游源码替代产品输入。

原 `Install from VSIX` 命令与扩展视图菜单直接使用扩展管理视图当前目标，不先弹目标 Quick Input；尚未选择时默认本地，带有明确目标的既有详情动作使用详情绑定。Backend 安装域在视图选择目标时沿既有登记解析，来源需要时再选择工作区并打开原文件选择器；文件选择器只选择来源，不能反向决定安装目标。安装目标发现不依赖已运行的 Web／Node 宿主；资源上下文菜单与既有拖放调用保留同一公开命令 ID 和 `URI | URI[]` 形式，URI 表示来源，不取代管理视图已选安装目标。入口在打开文件选择器前冻结 Backend 与所需来源范围，安装期间切标签、活动工作区或视图目标不改投；文件选择器只限定文件系统及初始目录，返回来源仍须整批核验属于所选已挂接根，不支持的跨目标来源明确拒绝并保留用户选择。工作区文件 Provider 的 `readdir` 复用现有目录 API 及游标，取得完整目录后交原 FileService；不另建目录树、缓存或路径事实，读取失败不能伪装为空目录。直接资源批次先核全部来源及冻结目标，再依次安装，后项失败不回滚前项；已核实回执合并通知，真实版本与当前运行版本分别表达，重新加载交原 HostService。没有成功回执的错误保留原原因并要求先查库存，不按 HTTP 状态推断是否已经发布，也不重放安装。

原窗口初始化按共同目标发现的能力分别推进：安装管理按原安装读取权限查询目标库存及只读包资源，不读取宿主运行态或创建 Web／Node 运行实例；运行初始化另核 `extension_host_manage`，按实际入口加载 browser 扩展或建立 Node 通道。已获准的合法 browser 入口不要求启动或具备 Node 宿主，只有安装权限时不得借资源读取自动激活扩展。原 VSIX action 从同一视图检查安装能力；未获准的目标显示明确原因，在打开文件对话框前拒绝，不妨碍同屏另一个获准目标。实际安装仍由原生 Gateway 按请求时现行策略判权，旧窗口投影不成为授权凭据。浏览器或中继机器的原生 `file:` 来源当前不能直接用作目标安装来源，原拖放的错误通知明确指引先把 VSIX 放入目标工作区；本片不新增上传链。完整安装详情、市场及物理卸载继续由后续任务验收。

工作区目录失败沿原 Provider、FileService 和 SimpleFileDialog 传递，不以空 children 或空列表替代错误，也不增加预探测请求。原选择器标识目录的单个尾分隔符由唯一文件反投影入口消费，不把空尾段送进资源 URI；内部空段及不透明根保持原校验。真实 stat 缺失仍保留原文件名候选／保存新文件语义；stat 成功后的目录 404、权限不足及离线须保留原原因和请求归属，不能再次解释为合法候选。原选择 Promise 统一结算并释放该次 QuickPick 和上下文，初始加载、导航、输入、隐藏文件重载及自动补全共用同一错误收尾；取消的旧更新和关闭后的迟到结果不覆盖新更新或结束后续操作。失败由原命令呈现，用户重新执行命令重试，不自动重放安装或改投目标；其它文件系统保留原 Code OSS 行为。

### D4. 目录发现、安装、配置与运行分别报告事实

安装取消按 operation 独立结算，并使用独立 `cancelled` 终态。共享 producer 的下载／准备只有最后一个使用者取消才停止，且须核实际执行进程退出；其它消费者不随之取消。同安装域的另一后端可在原 SQLite 安装账本写入取消意图，由 producer 原执行者处理，不新增第二取消存储或重放未知脚本。取消与发布在原事务中决定先后，已经提交的 `succeeded` 不被迟到取消覆盖。

共享执行状态归原安装进程 owner，各 operation 保留各自取消意图与终态；首个用户操作取消后不再被当作整个共享任务的终态。最后消费者取消期间返回 `running` 及 `cancellation_requested`，原执行者确认进程容器退出后才发布 `cancelled`；清理失败保持失败，进程失联保留 `interrupted`，不能仅凭持久取消意图推断实际任务已停止。监测执行状态与消费者使用同一 SQLite 读快照，发布事务重新核对消费者。

PyPI 发布记录的 `command` 保留生态生成的原 console 入口文件。唯一安装 owner 在实际准入时核原入口与制品摘要，再由同一 venv 的 Python 直接读取该脚本或 Windows launcher 内的 ZIP 入口，转交原参数、环境和工作目录；不经 trampoline 再启动 Python，不复制或重写入口脚本，不新增包装进程、短路径别名或另一份 launch 记录。原用户显式动态命令继续沿普通 stdio 合同执行。

新版制品发布不等于配置或连接切换。旧调用、封存资格或结果未知尚未结清时，切换明确冲突并保留旧配置及旧连接，不启动新旧 stdio 进程重叠；全部结清后仍由用户重新显式切换，不自动续做先前被拒绝的切换。此边界不新增并行策略或通用连接固定机制。

VSIX gallery 与 MCP gallery 使用各自原版合同及分页；来源明确记录 endpoint／registry 身份。未配置目录显示未配置，目录不可达显示失败，不能显示“没有扩展／没有 MCP”。目录查询不启动目标服务、不下载执行包、不扫描工作区或扩大到其它登记目标。

MCP 来源由原配置 `chat.mcp.gallery.serviceUrl` 指定，协议由 `chat.mcp.gallery.version` 明确选择 `v0.1` 或 `v0`，默认 `v0.1`。不为识别版本额外发送探测请求，失败不自动切换协议或来源；仅支持 `v0` 的目录须显式配置。更换来源或协议时取消旧查询并丢弃原分页缓冲，迟到成功或失败不能覆盖新列表。续页失败沿原列表错误区和通知呈现，不能误报完整无匹配或成为未处理 Promise。

VSIX 可从经验证目录或用户明确提供的 VSIX 安装；MCP 可从目录、手动服务定义或明确 HTTP endpoint 配置。HTTP MCP 通常只需登记与授权，不显示虚假的“已下载”。源条目包含多个包或运行方式时，按目标能力显式选择，不以浏览器机器的 OS／PATH 判断远端能力。

安装计划展示确切目标、来源、解析后的版本、平台、依赖／运行方式和配置范围。版本范围在一次操作内解析为精确制品并记录摘要；不在每次运行时偷偷追随 latest。下载完成、包验证成功、配置已保存／待生效、连接就绪和工具目录已发布是不同阶段；安装不自动向任意 Thread 附加工具或触发 turn。

来源身份包含目录 endpoint／registry 身份、该来源稳定 entry ID（仅有名称时冻结名称及 manifest digest）、显式选择的 package／remote variant 和解析后的版本／制品摘要，再与安装域组合；显示名与 server_id 不是包身份。多个 registry 同名条目不合并；安装／更新冻结 manifest 和 variant，候选消失或同名内容被替换须重做计划，不能退回第一个 remote、package 或另一 registry。只支持当前 stdio／Streamable HTTP 的合法映射，旧 SSE 不冒充 Streamable HTTP。

候选原件读取、安装计划与重验由原 `McpInstallationOwner` 承担；精确工作区的配置计划继续交 `McpCatalogOwner`，两者复用同一候选读取与解析实现，不建立计划数据库、缓存或 token。目标侧按显式 endpoint／协议／entry URL 有界读取完整原件；v0.1 的 official metadata 只认 envelope 顶层 `_meta`，不从发布者 `server._meta` 借用身份，无稳定 ID 就保持缺省。完整 manifest digest、原 variant ordinal／digest、目标原生平台及解析参数共同冻结计划；消费时原 owner 重读并核原 digest，变化返回冲突。返回的 parameters 只是脱敏展示，配置保存须重新提交原请求并经原 source／CAS owner，不能把该投影保存作 launch。package 安装计划只解析制品准备所需字段，不要求或消费 workspace 运行参数、私有 env；提交运行输入时明确要求改走配置计划。安装后配置再按精确 workspace 解析参数、秘密引用及来源 CAS。HTTP 表达纯配置，既有显式动态命令仍不受管；候选阶段不下载、不启动 MCP、不写配置或安装记录。

配置提案由同一 owner 消费原候选、已发布制品及来源 CAS 生成，返回脱敏 JSONC 和原来源预检；不回显候选输入，不维护草稿账本。JSONC 单成员编辑复用现有 commentjson 语法树和 lexer，只替换精确 Server，保留外围原件。保存时仅以重验后的原候选还原新 Server 的秘密占位，其余占位继续交原文件 writer；整份配置的最终 CAS、幂等键和副作用准入只由原 writer 结算。显式环境引用保留原表达式，计划指纹仍核目标侧实际解释后的参数，不把展开的环境值回写来源。

上游 MCP “安装”主要写 manifest／launch configuration，并以 npx／uvx／docker／dnx 运行时解析包；它不是本设计要求的已验证制品发布。新增的目标安装服务沿原 install seam 工作，但必须准备锁定依赖的隔离环境／精确可执行制品，容器使用不可变镜像 digest；项目连接的实际 launch 绑定安装 generation／digest，不能配置看似 pinned、实际仍运行动态 latest。生态无法提供该链时，条目标明不支持制品安装。用户已有的显式动态命令仍按原合同运行，不能追认成受管安装或在升级界面时擅自改写。

目标安装服务唯一持有安装记录、制品、锁定版本及有引用的保留信息。制品和安装元数据位于目标环境 `${BOXTEAM_HOME:-~/.boxteams}/` 的基础设施区域；workspace 配置、连接与调用记录继续位于各工作区 `.boxteam/`。Web／Node VSIX 均复用目标原 Code OSS 安装／profile／scanner 服务，不拆成前端安装器和后端安装器；MCP 继续使用其原生态安装 owner，不为统一入口重造通用账本。Gateway 提供已授权的查询与资源传输，不写第二份安装账本。浏览器可沿原资源机制缓存已发布版本的字节和展示镜像，但不持有可独立安装／更新／卸载的权威记录；直接访问目标页面与经 Relay 加载使用同一版本来源。

包安装复用各生态标准工具与隔离环境，仓库 JS／Python 依赖仍遵守 bun／uv；不自动全局安装缺失的 Node、Python 或容器环境。目录要求的能力不具备时给出目标侧具体原因，不转换成另一种执行方式冒充原计划。

PyPI 制品发布使用目标已有 uv／Python 与标准 wheel，使用明确 index 的 `uv lock` 和 `uv sync --locked --no-build`。console entry point 由用户显式输入，并核根 distribution 的实际版本和可调用入口。准备开始即使用最终 installation／generation 绝对路径，避免改名破坏 venv shebang；验证完整文件内容、权限和符号链接后，由唯一 SQLite 事务同时发布制品与操作完成。

npm 制品沿同一安装 owner、SQLite reserve／producer、取消、发布及引用链准备，使用目标已有 Node／npm、明确标准 registry、精确 semver 和 `package-lock` v3／`npm ci`，不新增账本或动态启动链。npm 自带解析器验证根包及版本，原锁保留嵌套依赖版本和 integrity；隔离用户 npm 配置，核实际依赖树及根 package 的显式 JavaScript bin 后才发布。记录目标 Node 的实际路径与初始版本，launch 直接使用记录路径与原 bin，不重新按 PATH 选择，也不在每次启动额外 hash runtime 二进制。原制品 digest 继续保护发布内容；Node 路径缺失或不可执行明确失败，runtime 原地升级与现有 Python 基运行时遵循相同边界。窗口按所选生态核对应 capability 与原因，PyPI 缺失不阻止 npm。首批 npm 不执行 install scripts，实际依赖需要构建时明确失败；OCI／NuGet、自定义 runtimeArguments、sdist 和单文件 fileSha256 继续明确不支持，不把这些限制隐藏为安装成功。

同一 BOXTEAM_HOME 的稳定 Backend 安装域允许多个真实 Backend 进程共享账本；schema 初始化仅短持锁，安装意图由 SQLite 唯一 reserve，不持生命周期独占锁。操作 owner 记录 PID 与进程创建时间；另一个活 Backend 不得中断原操作，只有确认原进程已退出时才将运行中记录标为 interrupted。相同意图始终返回原操作，不因重启重放，未发布目录不具备可启动 binding。

工作区 stdio 配置以 `managed_launch` 精确引用 Backend／installation／generation／artifact digest，与显式 command 互斥；原安装 owner 解析已发布绝对 executable，原 SDK 启动准入再次核完整摘要，禁止动态 uvx／npx 解析替代。该 binding 随原连接 generation／retirement 保留；物理卸载由原安装 owner 核对工作区配置与资源账本引用，未知占用或未结清调用明确拒绝，版本不随安装指针迁投。原 Workbench InstallAction 仅发布制品并返回原发布事实，明确提示尚未配置；配置仍经原 source preflight／CAS 显式提交，连接 ready 由原 MCP owner 独立核实。来源预检、保存与原候选准备共用旧版本占用检查；当前配置事务在锁外写盘及准备期间暂时拒绝相关旧连接的新资格，直到原提交或失败结算。已有资格／调用或未知结果未结清时明确拒绝升级并保留原配置和连接；结清后仍需重新显式提交。

### D5. 安装操作可追踪，更新与删除尊重已有引用

安装服务保存有界操作状态、幂等身份、目标／制品基线和可复查日志，阶段至少可区分计划、准备、验证、发布、完成／失败／取消／中断待核实。复用该服务唯一记录，不复用会话任务数据库或新建外部队列。网络重试只能查询原操作或提交相同幂等意图；取消不等于系统包管理器已停止，超时不伪报回滚完成。

先在暂存位置验证再发布可用版本；更新失败保留已验证旧版本及实际错误。新配置与 MCP 连接代际的激活仍交现有配置监督 owner，不以文件已解压作为工具可调用证明。旧版本在引用／运行进程／原调用租约未结算前不得删除；MCP 结果未知操作继续沿原账本处理。

制品引用在原 connection generation 建立、进程准入前附着到其 launch binding；释放由原 retirement／调用监督事实驱动，须满足实际子进程退出已验证及原封存资格／未结算调用的保留条件。不是另建一套调用租约。cleanup_failed 或结果未知时保留必要引用并拒绝卸载；新连接使用新制品，旧调用继续原连接，不随安装指针更新改投。

移除某 workspace 的使用配置不删除其它 workspace 配置或共享制品。物理卸载列出并检查实际引用；有引用时拒绝并说明来源，不暗中批量禁用。停用 VSIX 贡献沿原宿主卸载／reload 规则处理，不伪造动态卸载支持。恢复对账只报告已观察事实，不自动重放未知安装脚本、OAuth 交换或外部工具调用。

Web 实例在原运行 owner 准入时冻结安装目标、扩展及确切包版本，入口、后续动态 import、静态贡献和相对资源均沿该绑定读取，不经可变的“当前版本”指针。更新或逻辑卸载只改变后续实例的安装可见性：既有合法实例仍可读取旧包，不能在一次运行中混入新版；新实例只从准入时当前安装版本创建，已逻辑卸载则拒绝新建，不以浏览器缓存或磁盘残留恢复旧版。与安装变更并发时由原安装／运行引用发布顺序确定版本，不新增历史安装副本。

实例关闭或运行授权撤销即失去旧版本资源资格，并沿原生命周期撤销该实例贡献、结清引用；运行授权撤销后，即使仍有安装权限也不能延续该实例。其它客户端或目标的合法实例不受牵连。物理回收等所有实际运行及其它合法引用结清后再沿原制品 owner 执行；实例资格由原运行绑定及既有客户端租约管理，不另建资源租约账本。已下载字节不能作为后续激活或资源访问的授权凭据。

### D6. 原版 MCP 管理界面直接消费唯一项目 owner

在原 MCP 管理服务 seam 实现项目目标感知的查询／修改／运行投影，替换该运行面同义后端，保留原列表、详情和命令。配置计划提交现有 Workspace 配置 API；目录、ready、授权和工具调用继续交 `McpCatalogOwner`、connection service 与原 supervision。原 VS Code 自带执行路径不得同时为同一项目 server 创建连接；原 React “MCP 连接”管理调用迁齐后复用同一权威对象，不维护另一套 enabled/status。

MCP 配置使用当前 schema 的 stdio／Streamable HTTP 字段。原目录的 inputs、包参数、headers、OAuth 或不支持字段先解析为明确候选；秘密由本变更扩展的目标 MCP 私有凭据 owner 接收，普通 DTO、安装日志、前端存储与 Quick Access 历史不保存其值。未能无损映射的定义明确不可安装，不丢字段后显示成功。

既有用户明确配置的 env／headers 等输入继续按原 schema 解释，不因新界面擅自删除、拒绝或改写。当前私有 MCP store 处理 OAuth token，尚无通用 gallery secret input 的引用解析链；本变更须在原 MCP 凭据／配置 owner 扩展工作区私有输入存储和引用读写合同，以稳定 workspace、server、输入键及版本隔离，存入该工作区 `.boxteam/credentials/`，不是安装域共享密码库。引用绑定相应服务定义／endpoint 身份，改来源或地址不能自动借用原秘密。新管理入口采集的敏感值经目标私有入口一次性接收，直到连接准备时才在消费端解引用；配置投影只含合法引用或脱敏状态，遮罩不得作为原值写回。schema／正式 delta／解析器／连接消费者须同批迁齐，能力未具备前明确阻止依赖秘密输入的条目。旧明文配置迁移只有在显式官方预览和授权写入时进行，不另建 OAuth 流程。

若 VSIX 通过受支持的 MCP definition API 提供服务定义，它只贡献带来源的定义；启用仍按明确 workspace、目标配置与项目 owner 流程处理。不能在 Extension Host 中顺便创建项目 Agent 的第二套 MCP client。原版聊天及其它消费方若使用该 server，须明示使用关系，不能把原生 Chat 的目录误报为项目 Agent 可用目录。

该 API 是动态 Provider，不是一次性导入配置。保留原 `provideMcpServerDefinitions`、变更事件、启动前 `resolveMcpServerDefinition`、取消与 dispose 语义：发现阶段不交互授权；真正准备启动时才向原 host／Provider／定义对象及 generation 调用 resolve，返回 undefined 或异常就停止该次启动并显示原原因。解析结果经项目 owner 校验及凭据入口使用，不永久固化短期 token 或把同名定义当作原对象。Provider 更新／撤销使待启动候选失效，已准入调用仍沿原 MCP 监督结算；host 不可用时明确报告依赖不可用，后台任务不得以最后一次定义快照伪装可重新 resolve。只有已明确导出且不依赖回调的静态配置才具有独立恢复语义。

动态定义的来源执行上下文与所选运行目标必须匹配：Node Provider 的 stdio／本机路径／env 默认仅适用于该稳定目标，不允许 A host resolve 后直接在 B 启动。Web Provider 无目标原生执行上下文时同样不能把浏览器侧路径当远端路径。跨目标只接受 Provider 明确导出的可迁移静态模板，经目标能力、参数与私有凭据重新校验后进入普通候选链；HTTP 也不能默认转移来源 host 的 token、localhost 或授权上下文。没有明确可迁移定义就拒绝，不新增隐式跨目标 resolve 代理。

### D7. 多目标 Extension Host 复用原协议，扩展运行位置语义

UI／声明式贡献及符合 manifest／宿主条件的 browser 扩展，与 Node 扩展安装到同一所选目标并从同一原安装记录加载。原 Web Extension Host 在当前浏览器执行 browser 入口，匹配固定上游版本的目标侧 Node Extension Host 执行 Node 入口，复用原 activation 和 MainThread／ExtHost RPC；不新增项目插件 SDK。包含 browser／main 的包沿原 manifest 和能力选择合法入口，不因有两类入口安装两份。Electron 与浏览器共用该规则，本地 Node host 同样属于本地后端目标。

Web 资源由目标原生 Gateway 经现有认证资源链提供，Relay 只转送；入口、静态贡献及相对资源必须绑定同一目标和发布版本，复用原扩展资源加载、Worker 与 CSP 机制，不将远端原生 file 路径交给浏览器解释。目标身份、来源授权、版本变化和撤销沿原 owner 处理，浏览器缓存不成为离线安装权威。内置扩展仍随发行提供，不要求用户重新安装。

多个前端可以共享同一目标安装包与版本，不能共享可变运行实例。每个浏览器窗口独立激活和释放 Web 实例；其注册、配置、资源请求及消息保留目标来源，跨目标同 ID 不能在单个无归属 registry 中互相覆盖。沿原 Web Host／Registry／分派闭包处理需要并存的目标实例，窗口级声明冲突仍按 D8 解决。关闭一个窗口不卸载目标制品，也不撤销另一窗口实例。

原 manifest 的 extensionKind、browser／main、engines、依赖、workspace trust 与 proposed API 条件继续参与选择。不能只按“有 main 就放到任意远端”判断；要求本机 UI Node 环境而当前运行面不具备该语义的扩展明确不可用，不伪装成 workspace 扩展。

原 `RemoteRunningLocation.equals` 仅比较 Remote kind，不能辨认 A/B Backend。实施需在现有 RunningLocation／HostManager／Factory／RPC 闭包内增加稳定目标与工作区视图维度，并使用原有 sourceTransform 构建链交付；不修改 submodule、不复制 extension service 或通过全局 remoteAuthority 来回切换。身份对照清点、真实原型和 API 判别场景是该闭包的实施门，不能只改枚举后勾选。

同样必须迁齐目前按 extension ID 唯一索引的 description registry、running location tracker、enablement、activation、依赖与卸载消费者：内部安装实例键至少区分公开 extension ID 与稳定安装目标，运行实例再含 view／generation；公开 manifest/API ID 保持原样。不能只给 Remote 枚举加字段，让后注册描述被忽略。A/B 可运行同 ID 不同版本的独立实例，依赖只在对应目标／view 的合法实例集合解析；窗口级声明冲突另按 D8 明确处理，不借另一目标依赖满足本目标。

Node host 实例归属于一个客户端会话、一个原生目标及一组明确挂接的同目标 workspace。其 extension API workspace view 只包含该集合；不同目标不能进入同一 Node 进程视图。同目标可呈现原 multi-root 语义，挂接集合变化经原 workspace/activation 生命周期显式更新，不能混入导航当前全部工作区。每个 host 带 generation，客户端断线、目标重启或权限失效禁止旧 generation 接受新调用。

VSIX 的启用／禁用范围沿原 profile／workspace view 语义，多根视图不是每文件夹独立扩展进程；若 A1/A2 需要不同扩展启用集，建立不同明确宿主视图，不能在同一共享 host 中宣称 folder 级运行隔离。MCP 的启用仍按精确业务 workspace；不能将这两种“工作区范围”混用。更改扩展版本或停用需沿原 host reload／重启要求，用户看到已安装新版不等于旧进程已经执行新版。

目标 VSIX 安装明确使用原 `keepExisting`，按官方 ExtensionKey 复用已发布的可验证同版本目录；当前 profile 或 `.obsolete` 中保留的版本均不得因再次提交 VSIX 被覆盖。回执的 `reused` 来自本次原扫描／提取分支，窗口分别显示新制品发布与已有版本复用，不先全盘扫描或比较包体 hash 来猜测结果。同 ID／版本／平台的另一个 VSIX 不代表可替换内容；需要更新代码应使用新版本。已有目录无法扫描或验证时保留原件和移除标记，明确拒绝，不走删除重建回退。官方未选择 `keepExisting` 的替换行为保持；这个安装约束不代替后续跨 Backend 活宿主引用、物理卸载和清理保护。

进程数量按明确使用的目标／视图惰性创建；不同浏览器客户端不共享可变命令回调、活动编辑器或 workspace view。客户端断线后按有界租约回收 host，保留目标安装及稳定存储；未结算有副作用操作按原服务语义报告，不能盲目重放。Backend 停止／重启通过现有生命周期入口排空受管 host。

### D8. 同一贡献 ID 的分派与 API 能力矩阵必须明确

同一 VSIX 在 A/B 目标安装时，原 command ID 和 manifest 不改名；内部注册记录保留 extension／host／target 来源。工作台展示复用一个可辨识命令入口，执行时优先使用发起编辑器的精确目标；没有接收目标且存在多个候选时由原 Quick Input 选择，再冻结目标。不同贡献不能靠最后注册覆盖。URI 能定位的语言／文件 Provider 按资源 owner 分派；工作区查询使用明确 scope；主题等工作台级贡献按原 profile 选择，不在 A/B 切换时隐式重设全局主题。

扩展自己发起的 `executeCommand` 继承调用 host／view，不借用此刻窗口焦点；资源参数的归属须与所选服务一致。未激活命令按选定来源触发 activation，不向全部目标广播同名 `onCommand`。同 ID 静态贡献按选定 manifest 版本登记一次，保留来源对应关系；运行态 TreeView／WebviewView resolver、菜单上下文及 `setContext` 按所属 host／view 投影，切焦点只改变展示绑定，dispose 只撤销原注册者。主题、快捷键、schema 等窗口级贡献不能靠重复注册覆盖；同扩展不同版本的声明冲突须在启用前给出明确诊断并要求选择可共存版本或不同窗口，不篡改公开 ID 掩盖冲突。

工作区符号 Provider 继续登记在原窗口注册表，条目携带发起宿主；明确工作区查询在调用前选定完整 host view，结果及 resolve 复用原 Provider／cacheId／RPC。原公开 Provider 没有目录参数，因此只选 A1 而宿主绑定 A1/A2 时明确提示需要完整视图，不先查询 A2 后过滤。目标扩展的公开符号查询继承原宿主代际，内置 Web 及原无 scope 的窗口查询保持原窗口级范围，目标安装的 Web Provider 则保留其明确来源和业务视图；默认列表的符号聚合仍待迁齐同次查询发布，不以 `#` 闭包宣称完成所有符号入口。

Node host 的文档和编辑器同步同样按明确 view 投影，不能只过滤 workspaceFolders：初始模型、dirty buffer、打开／关闭／变更、visible／active editor、语言诊断和 watcher 事件只到合法资源订阅者；焦点在其它 view 时，该 host 的 active editor 为 undefined，不泄露另一个目标模型。文档版本、RPC generation 与取消贯穿注册、请求和迟到结果回收。跨资源 WorkspaceEdit 在副作用前核验全部目标、映射和版本；不把其它目标 URI 降为本机同名路径，失败沿原编辑合同报告，不声称额外跨目标原子事务。内置 Web UI host 保持原窗口级能力；目标安装的 Web 扩展对业务资源和配置的访问仍保留该目标归属，不能借窗口级 UI 能力改投其它 Gateway。以上投影不是 Node 文件进程的系统沙箱。

直接 workspace.applyEdit 沿原 MainThreadBulkEdits 在 DTO 恢复前共用整批端点归属检查，再交原 BulkEditService；原 warn／false、undoRedoGroupId 与 refactoring 自动保存选项保持。每个 MainThread 实例以原 cancellation token 绑定宿主代际，取消后的结果由原 bulk owner 实际完成资源决定：零完成为 false，已完成的真实结果不被外层关闭覆盖。文件操作参与者复用相同端点检查，不另建文件 writer。预览最终集合已有的文本／Notebook版本由原 BulkEdit 在首个类型副作用前统一核对，并沿原 resolver 持有本次引用；尚缺的 Notebook 版本生产和其它语言 Provider 编辑仍是未完成边界。当前产品 WorkspaceFiles 只支持原有创建／保存，不支持的重命名／删除明确拒绝，不能用 Node fs 绕过原 owner。

Code action 的原 MainThread 在发布列表前预检全部 action 的 WorkspaceEdit，再复用原 revive；resolve 同样先验证后修改原 action。每个注册持有 cancellation source，调用的原父 token 与注册释放共同取消 RPC，await 后重核；迟到或拒绝的 provide 只释放其新 cache，已发布 list 沿原幂等 dispose 收尾，不增加缓存注册表。原 metadata、action 身份、command 正文、无 edit 与 Web 语义保持。已发布 action 沿原 Provider／CodeActionItem 保留目标注册 token，原 applyCodeAction 合并调用取消并将其交原 BulkEdit；原 Adapter 在 provide／resolve 回调前冻结文本版本，原 UI owner 显示应用失败并结清进度。paste／drop 等其它编辑入口仍需沿各自原 owner 迁齐。

Rename 的调用前文本版本快照与 CodeAction 共用原 ExtHost 文档 owner；结果归属与注册取消在原 MainThread 准入。返回结果只在窗口内以不可枚举字段保留原注册 token，原 BulkEditService 与调用 token 合并后贯穿预览及实际应用，不扩展 RPC DTO 或新建编辑链。原 RenameController 冻结父操作 token 并等待应用，避免自身文本变化取消后续组；inline 沿原 runnable token 等待结算并过滤退役结果。预览复用原 Pane、会话、Provider 与模型 resolver：原视图 when 在打开前启用，取消只释放准确会话；异步模型准备和自引用退役后停止，未引用模型与迟到引用释放，活跃解析错误明确报告。旧点击在异步准备后核原 input／provider，原 Chat response stream 分项移交后的生命周期仍是独立 TODO，不据同一 BulkEdit 入口声称该链已完成。

复用原 RPC 的 URI transformation，区分工作台稳定内部 URI 与目标 Node API 的原生文件视图。Node 扩展取得的 workspace／document 文件 URI 及 fsPath 必须能用于该目标真实 Node 文件访问，返回 URI 再投影回同一工作区稳定身份；根绑定只消费已登记 Gateway 事实及 Backend mount view，不持久化另一套路径注册表。转换限定 typed URI／协议字段，不全局替换任意字符串或 env／配置正文。验收必须用真实扩展经 `node:fs` 及 `workspace.fs` 分别读取目标独有文件，并核返回位置在原编辑器打开；只让自定义 URI 在浏览器 VFS 可读不足以证明 Node 扩展兼容。

同目标嵌套根沿原 folder 最长匹配规则；同一本机路径若对应多个业务身份且上下文不足以唯一还原，使用已有请求／文档来源消歧或明确拒绝该投影，不能任选根并声称稳定 URI 往返。`extensions.all`／`getExtension`／exports 也以该 host 合法实例集解析，不能凭公开 ID 取另一个目标的运行对象。

| API／贡献 | 必须保持的行为 | 能力缺失时 |
|---|---|---|
| commands／menus／keybindings／activation | 原声明与条件，绑定实际 host，活动上下文只显示合法目标 | 不注册假 handler／不激活错误目标 |
| text documents／language providers／symbols／search | 原内部 URI 唯一投影，文档同步和取消归资源目标，沿原 SearchService／搜索合同 | 不以本地 IndexedDB 同名资源替代 |
| workspace folders／fs／watcher／configuration | host 的明确工作区视图，目标文件与设置 owner | 拒绝不支持的操作，不转向浏览器文件系统 |
| terminal／tasks／debug／process／环境 | 在该后端目标执行，使用目标 PATH／平台和真实生命周期 | 明示具体能力不可用，不改到 Electron／Gateway 执行 |
| views／webviews／资源与端口 | 复用原贡献、CSP 与资源通道，带 host 来源；远端 localhost 地址按原明确端口转发能力处理 | 不把远端 localhost 解释为浏览器机器 |
| storage／secrets | globalState 按 profile／原生目标／extension，workspaceState 再按稳定工作区视图；秘密留实际消费侧 | 不复制 token 到另一 Gateway／前端缓存 |
| extension dependencies／原生模块／proposed API | 按 manifest、平台和实际 runtime 校验；同目标解析依赖 | 精确说明不支持，不承诺全市场兼容 |

capability discovery 只读取版本与实际已支持服务，不启动 Agent。兼容矩阵与实际运行结果可查询；缺少必要能力的扩展不得显示为已可用。宿主启动、扩展激活、命令执行与业务结果分别验收。安装包可复用，但 host 运行态不跨目标／客户端共享；持久 storage key 不含临时客户端会话或 endpoint，以保持重启恢复。

`globalStorageUri`、`storageUri`、`logUri` 与 `extensionUri` 也属于 URI／存储闭包：Node host 取得的是目标本机可用路径，包目录与可写状态目录分开；工作台资源访问仍走原受权通道。Memento 的共享持久数据沿原 storage owner 及变更事件同步，同身份客户端并发沿原写入语义，不虚构逐字段事务合并；运行回调与瞬时活动态不随持久存储共享。 原 Workbench profile／IndexedDB 继续持有 Memento 正文，ExtensionStorageService 统一按稳定安装目标及 workspace view 生成键，MainThreadStorage 的通知还原原公开 extension ID；不另建目标状态库。目标 Node 的 `setKeysForSync` 跨设备同步尚未迁齐原同步消费者，当前从原公开 API 明确拒绝并登记 TODO，不保存无人消费的同步声明；Web 原同步与清理保持。目标侧秘密 owner 尚未接通，Node SecretStorage 在秘密进入浏览器 RPC 前明确拒绝，MainThreadSecretState 也不读写或广播 Web profile 的秘密；完整目标秘密支持仍是 TODO，不以浏览器同名扩展的秘密库代替。

### D9. 设置只有一个编辑入口，各域仍持有自己的权威

复用原 Settings Editor、JSON schema／JSONC editor 与命令，在编辑器顶部使用 D2 的目标控件，默认本地；从明确配置对象打开时保留对象目标，再按实际需要选择来源层。设置编辑器持有自己的选择，修改多个设置时不逐项重选；扩展或 MCP 视图切换目标不改写已打开的设置。页面顶部持续展示目标、来源层及实际可写能力；用户主动切换设置目标时沿原 dirty 编辑处理保留或结清草稿，不能把旧目标未提交内容用于新目标；切 Tab 不改变正在编辑的目标。未知／不可访问／不允许的设置范围在保存前明确拒绝。内置默认值只读，可在合法覆盖层建立覆盖，不直接改发行包。

| 编辑对象 | 写入 owner 与来源 | 不允许的替代 |
|---|---|---|
| Workbench 主题选择／布局／快捷键等纯窗口展示态 | 原 profile／Workbench storage，Web 与 Electron 原固定 profile 各自生效；主题制品仍从所选目标加载 | 把窗口展示态当成另一 Web 插件安装域，或写入 Workspace／Gateway 业务配置 |
| Web／Node 扩展安装与目标扩展设置 | 同一目标的原安装／profile 与 Code OSS 配置 owner，按合法用户／view／folder／语言 scope 处理 | browser 入口另存一份权威安装或因在浏览器执行就把目标设置写入当前机器 |
| Gateway 配置 | 该原生 Gateway 配置 API／层 revision，含合法用户 source 与 local 覆盖 | 让 Backend 或本地 Relay 修改远端配置文件 |
| 目标环境 Workspace 用户默认 | 该环境原 source writer，写 JSONC 后走原 fanout_id／逐 workspace CAS | 假装是跨 Gateway 全局默认或原子批量事务 |
| 精确 workspace 来源层／runtime override | 该 workspace 原配置 API，按真实来源层修改及物化 | 表单自行重写整份 JSONC、合并掉 user_local 或覆盖其它层 |
| 远端 Gateway 连接登记 | 本地控制面原 registry／连接配置 owner | 将远端运行配置复制成本地可生效副本 |
| 会话设置、Gateway 用户视图、手动 registry CRUD | 原 session／view／registry owner；可从设置 UI 导航，但分别提交 | 统一伪装成 runtime config candidate |
| 凭据 | 原目标私有 credential storage，UI 只持引用与必要一次性输入 | 写进普通配置、globalState、日志或历史 |

**Code OSS 配置与项目业务配置是不同的权威域。** VSIX 的 `contributes.configuration` 继续进入原 configuration registry，遵守 APPLICATION／MACHINE／WINDOW／RESOURCE／LANGUAGE_OVERRIDABLE 等 scope、原 policy、信任限制、默认值和语言覆盖；`python.*` 等扩展键不得塞入严格校验的 Gateway／Workspace 业务 JSONC。统一 Settings Editor 按域导航，分别提交原 owner，不能把扩展配置 API 全部转成 Python runtime candidate。

`workspace.getConfiguration` 的 get／inspect／update 及变更事件使用该 host 的配置模型：Global 沿原 USER 及 setting scope 选择已选目标的用户配置；纯窗口展示设置仍使用当前 profile，不能仅因 browser 入口在当前页面执行而将目标扩展配置改写成本地值；Workspace 指向该稳定 host view；WorkspaceFolder 指向显式 resource 所属的挂接 folder，语言覆盖保留原 overrideIdentifier。省略 target／resource 时沿原推导规则，无法唯一确定或资源不属该 view 时明确拒绝，不改投导航当前工作区。`MainThreadConfiguration` 的初始化、inspect 与 change payload 都须投影，不能向每个 host 广播完整窗口／其它目标配置。API inspect 与 Settings UI 对同一键、resource、语言必须一致；项目业务来源层的诊断字段不能冒充原 ConfigurationInspect 的 user／workspace／folder 语义。

视图配置 owner 不能留空：挂接的业务 workspace 根只作为原 Code OSS WorkspaceFolder；folder 扩展设置使用该根原 `.vscode/settings.json`，不写 `.boxteam/workspace.jsonc`。单根视图沿原 folder 模型；multi-root view 由原 workspace/configuration 服务持有稳定 `.code-workspace` 资源及其 settings，未由用户选择现有资源时在目标基础设施 profile 区物化该视图的资源。资源沿原文件／configuration editing 服务读写，view ID 与根集合经原工作区模型绑定，不建立第二业务 mount registry；增减根不以临时客户端 ID 重置配置。用户 remote 设置同样沿原目标用户配置资源。若资源尚未建立或不可写，相关 update 明确拒绝，不把 Workspace 写分摊到 A1/A2 的业务配置，也不退到 profile。该原 Code OSS 配置链遵守自己的 JSONC 编辑语义；项目业务配置仍走前述 candidate／CAS，不将二者包装成同一提交事务。

原 `IConfigurationService`／`IWorkspaceContextService` 是单窗口上下文，不能轮换它们的全局 active workspace 代替多视图。须在原配置服务边界按 host／view 构造或路由相应 WorkspaceConfiguration／配置模型，复用同一 registry、scope 合并与 JSON editing；目标 `.code-workspace` 和 folder 资源经已登记目标 FileService 读写，renderer 不猜服务器本地路径。

目标 settings 与多根 workspace 文件使用本 change 的 `virtual-resource-addressing` delta 登记的两个具名 `config` URI，由原配置 URI owner 构造；原生 settings 地址只取原 `profiles.defaultProfile.settingsResource`。原 Code OSS 配置模型、scope、JSON editing 和 renderer FileService 保留，目标配置文件的 stat／read／write 及稳定 view 文件物化统一交目标侧 Python 文件基础设施，不再通过管理 Node 的配置／文件接口。只准入两个精确文件和可 stat／watch 的直接父级，不开放 readdir 或任意 BOXTEAM_HOME 文件。变化复用原 WorkspaceFileWatchService 的两个精确父目录、非递归订阅和稳定 filter 对象，只投影具名文件事件；既有 watcher 在所属 Python 进程内异步运行，不新增监听进程或 Node 通用 watcher。配置访问绑定原生 Gateway、Backend、已授权 profile／明确 view，客户端身份仍由原用户租约派生，不能信请求正文身份；Node 宿主发起的请求继续额外核原 host generation 和有序根。目标安装的 Web 扩展与设置编辑器沿同一目标 Python 文件 owner 和既有认证入口访问，不以启动 Node host 作为读取目标设置的前置条件，不为此新建通用 session 或配置账本。重连／overflow 只 invalidate 原模型；保存沿原 mtime／etag、dirty 和队列，不新增 hash／CAS／保存基线或跨客户端原子事务。原安装器自身配置依赖的内部文件读取／监听可保留，但不代替 Python 对外文件服务。

现有 `/api/v1/config` PATCH 只允许 Workspace-owned `runtime_override` 的有限字段；sources 诊断不能代替来源正文读取、schema 发现与 source writer。实施须在原配置 owner 增补目标来源的可编辑能力、正文／脱敏投影、revision／digest、预检和 CAS 写入合同，并同步正式 delta／生成协议及消费者，不能把既有 PATCH 放宽成任意路径或任意 JSON 写入。Gateway 与 Workspace 来源分别核实真实可写层；没有 writer 的层只读并明确缺口，不用通用文件 API 补旁路。

原单 workspace API 对来源层的 SQLite 物化语义保持不变；“编辑来源文件”是明确选中 source 的操作，由新增原 owner writer 做文件 CAS／原子写入后再沿原导入链处理，用户级来源沿既有 fan-out。不能让一次 runtime／SQLite 更新顺带回写 JSONC，也不能把来源文件编辑伪装成已经同时激活全部工作区。入口与结果明确是来源正文写入、层物化还是 active 生效，避免引入双写事实源。

Gateway 来源编辑的预检和保存要求连接具有稳定身份。缺少 `connection_id` 的既有条目只从原 connection ID owner 只读复用可明确匹配的身份；真正新增或不能明确匹配的条目返回 409，指出位置并要求显式提供 ID，不按数组位置借用其它连接的身份。该输入边界仅适用于新增 source writer；原文件 loader 对已支持旧配置的 ID 分配和迁移行为保持。writer 不兼作版本迁移入口：若 v1 缺 ID 会使原 loader 另行改写来源文件，预检和保存先明确要求沿原迁移入口处理或补齐 ID。预检不创建迁移记录、备份、ID 或来源导入事实，也不另建预览 token／身份账本。

原生 Gateway 来源 JSON 的 Relay 沿现有目标 Gateway 请求头选择稳定归属，连接仅提供当前路由。唯一连接可从原 registry 解析；多个连接须明确选择，显式 `gateway_connection_id` 必须与目标 Gateway 一致。Relay 复用原管理发送链及原生来源 API，不新增远端配置副本或 `federation/config` 别名；认证后的联邦入站不得借本机凭据再次转发。首次网络等待前固定目标、连接、URL 和凭据，短请求占用归原 registry，释放只结清本次引用，不删除合法的无工作区连接；旧隧道仍有请求时继续受原 retired 跟踪保护。目标状态、错误、CAS、幂等键和同一请求的 request_id 原样贯通，断流不自动重放保存。配置状态、pending retry／resolve／discard 和事件列表沿同一目标选择及发送链抵达 B，保留原 candidate_ref、证明、CAS 与错误，配置 retry 不启动进程。事件 SSE 透传 after／Last-Event-ID、原 id／config／error／heartbeat，由 B 在实际披露点核现行权限；A 不创建第二个 consumer、不推进游标、不自动重连或重放。开放响应及原客户端由唯一关闭任务持有，响应头失败、流未开始或重复取消仍等实际关闭后结清原连接引用。Gateway 开发栈重启继续限定原生本机管理入口，不借配置能力新增远端重启。

设置搜索与 `@modified`、`@ext`、`@lang` 等原过滤器基于当前编辑器冻结的域、目标、来源层及实际注册 schema／扩展集合计算；切焦点不换搜索上下文。业务配置仅展示真实支持的过滤维度，不造虚假的扩展或语言归属；查询旧代际失效，不能把 A 的命中或修改标志显示为 B 的值。

保持实际合并顺序：Gateway inline → user → user_local；Workspace inline → user → user_local → workspace，以及原系统已定义的 runtime override。对象递归合并、数组整体替换；不存在前端发明的第三套优先级。新增配置仍按 `config-file-conventions` 使用同族 JSONC／schema；仅当发生不兼容变更才递增版本并提供官方预览、备份和可恢复迁移，不为 UI 新增假版本。

项目业务设置查询展示来源路径／资源、layer revision/digest、effective 值、遮蔽关系、active／pending 和生效分类；Code OSS 设置沿其原 inspect 和编辑语义，不伪造业务 candidate 字段。业务 JSONC 编辑也提交同一 owner 的候选预检与 CAS，不经通用文件保存旁路修改有效配置。注释与格式沿原 JSONC writer 保留；API 只物化 SQLite 的来源层应如实显示，不能伪报已经回写来源文件。删除覆盖只删除选中来源键，不把默认值写回或删除整个工作区配置。

同一用户 source 更新可影响目标环境的多个 workspace，保存前展示实际影响范围；每 workspace 保持独立 candidate／revision／结果，停止的工作区沿原追赶机制，部分冲突不能回滚其它成功项或假报整体成功。目标服务报告 active、pending、requires restart、invalid、conflict 等事实；UI 成功用后端完整对象替换，失败重取该目标状态，不本地改几个字段冒充应用。

远端 source／配置写入由远端原生 owner 最终判权和提交；Relay 只传精确身份、CAS、幂等请求及结果，不形成跨 Gateway 事务。断流后查询原提交身份，不自动用新幂等键重放；无法确认则显示待核实，不称失败已回滚。读取授权不自动允许写配置、安装或启动进程。

### D10. 界面入口及快速访问的分工

扩展视图保留 VS Code 名称和交互，目标控件放在原 Extensions 视图容器的标题工具栏，默认本地；各插件保留原安装／更新／卸载按钮，直接消费已选目标，不增加逐插件目标菜单或安装前目标弹窗。标题及详情持续显示目标，MCP／设置在各自标题工具栏或编辑器顶部复用同一控件与规则；切换其中一个视图不强制联动其它视图。Web／Node 是能力与运行位置说明，不再作为并列安装目标分组。目标内需要时显示安装域／workspace；MCP 条目附带启用 workspace，不按相同服务名合并，配置操作明确“为工作区…启用”。详情集中显示安装版本、配置来源、生效状态、运行与日志；停止、断开、失败、授权等待、未安装及仅有离线镜像分别表达。

主窗口左侧侧边栏承载 Gateway／目标导航及全局扩展管理入口；底部“自动化”“MCP 连接”等继续表达当前工作区资源。原扩展视图可管理多个目标，但不改变底部面板状态的工作区归属，也不把工作区管理塞入会话右侧资源栏。旧入口经同一 owner 打开对应详情或工作区连接，不维护独立安装／状态卡片。

所有适用操作贡献为原 Action／Command，列表与详情使用原 Opener，必要时携带稳定目标及选择参数。`>`、`view `、`ext `、`mcpr ` 和帮助仅在实际能力成立时提供对应功能；默认 Ctrl+P 不因此自动搜索全部市场。用户主动访问市场才查询所选目录，目录分页／取消不扩大 Gateway 工作区范围。需要引用或重新打开的新增资源先由唯一寻址 owner 登记模板；本稿不发明 URI grammar，也不把命令或安装进度强制资源化。

### D11. 发行与验证必须经过真实入口

Web 与 Electron 共用 Workbench 源码和贡献；Electron 静态资源、Web 扩展资源及所需 host 通道元数据必须随发行正确定位，不依赖源码路径或 Vite。Node host 运行时由目标侧发行／安装服务提供匹配版本及平台验证，不借浏览器或 Electron 内置 Node 冒充远端 runtime。

验收使用本地 target A（workspace A1/A2）与远端原生 Gateway 的 target B（workspace B1/B2），故意设置同名工作区、命令、MCP server 和不同平台／文件内容。至少验证如下对照：

| 闭包 | 真实入口及关键断言 |
|---|---|
| 目录与安装 | 原视图搜目录、选 B 安装，记录 B 的真实安装日志与精确版本；A 无下载／执行；HTTP 条目不虚报下载；手动来源复用同链 |
| Gateway 托管管理服务 | 目标服务组启动时每安装域仅一个安装管理 Node；多个 Backend／页面查询库存及执行安装复用同一组件，切工作区、关页面或重启单个 Backend 不重建它；真实并发安装不丢 profile 成员，远端安装只使用 B 的服务。Gateway 服务组关闭核实实际子进程退出，组件失败明确报错，未知写入不重放；管理服务存活不等于 Node／Web 扩展已运行 |
| Python／Node 职责隔离 | 目标 B 的 VSIX 及依赖下载、配置文件读写、包资源读取和外部文件变化通知经过 B 的原 Python 服务；Node 只提供原安装事实及必要描述，不接收通用文件／监听／脚本命令。配置语义、精确版本、授权及在用旧包读取保持；订阅释放沿原异步 watcher 清理，无新增监听进程，不以 Node 内部必需 watcher 的存在判失败 |
| MCP Agent | 安装并为 A1 启用，从真实 Thread 调用测试 MCP 工具，仅 A1 目录／连接／调用改变；A2/B1 同名 server 不混入；OAuth、热替换、旧调用／结果未知沿原监督 |
| 包与生命周期 | A1/A2 引用共享版本、并发安装同意图、更新失败、取消／断流、重启对账、有引用卸载；未知脚本不重放，未结算旧版本不删除 |
| 标题工具栏目标 | 扩展视图初次默认当前页面的本地 Gateway，标题工具栏选 B 后列表／搜索及新详情归 B；连续安装两个扩展不再弹目标选择，切回 A 只影响后续新操作，B 在途安装和已打开设置保持归属；MCP／设置使用相同控件与规则，各视图不被焦点或其它视图选择改写，VSIX 来源不改变安装目标 |
| 共享 Web 安装 | 在 A 页面选 B 安装 Web VSIX，直接打开 B 页面和另一客户端看到同一库存／版本，浏览器实际执行 B 的 browser 入口；仅有 browser 的包不要求 Node host 可用，刷新／缓存清理不丢安装，B 离线不改投 A |
| Web 运行与包资源 | 仅安装权限可读详情及包资源但不能激活；仅运行权限可读并运行当前已安装包但不能安装／更新／卸载；两项均无时不披露资源。v1 实例在更新 v2／逻辑卸载后继续动态读取 v1，新实例只取 v2 或在卸载后拒绝；原实例关闭、运行撤权后其旧资源资格失效，即使安装权限尚在，另一合法实例仍可读，最后引用结清后才可物理回收 |
| 扩展 host | 一个真实 Web 扩展和 A/B 各一个 Node 测试扩展；同一 command ID 读目标独有文件／配置，语言 Provider、终端及状态各归正确目标；切焦点、两客户端、迟到 RPC 不串扰 |
| API 能力 | 按 D8 每类验证实际支持／拒绝；不能用 mock command 或 host 进程启动证明真实 extension API 与文件／终端链可用 |
| 配置 | 同字段不同来源与同名远端；表单／JSONC CAS、删除覆盖、invalid、pending／restart、用户 source fan-out 部分冲突及停止工作区追赶；本地无远端有效配置副本 |
| 权限／路由 | 只有发现权限时管理拒绝且无目标写入；route 变化重解稳定身份，复用为另一目标则拒绝；目标离线与权限撤销不改投本地 |
| 客户端与恢复 | 实际 Gateway 产品初始化、原键盘／主题／可访问性、Electron 打包产物重启；无浏览器时安装／MCP 状态继续可查，UI 扩展 host 按租约回收 |

仅按受影响链执行受保护的静态检查、真实类型检查、构建及定向产品验证。正式用例按 `out/tests/<同名测试路径>/`，临时探索只用 `out/tests/temp/<task>/workspace/` 与 `artifacts/`，不得注册项目根目录或改 fixture。已通过同源码构建可复用，不要求全套串行跑或无证据重跑。

### D12. 必要依赖图，不规定全局实施顺序

| 纵向链 | 必须具备的合同／服务 | 完成证明 |
|---|---|---|
| 目录到安装 | 稳定目标与管理权限、来源／variant 计划、目标制品服务 | 实际目标制品发布及精确 launch，不以目录 UI 为完成 |
| 安装到项目 MCP | source writer／凭据引用、launch binding、原连接监督 | 真实 Thread 调用、代际更新和旧引用释放 |
| 动态 VSIX 定义到 MCP | 可用来源 host、Provider 生命周期与同目标执行校验 | provide／resolve／变更／离线均不产生第二 client |
| 多目标扩展 | 内部实例身份、view、RPC／URI／模型投影、目标 Node runtime | 同 ID A/B 真实扩展及贡献／配置／存储闭环 |
| 统一设置 | 各原 owner 的实际读写／权限；Code OSS 独立 view 模型 | 表单／JSONC／扩展 API 读写与重启后同一事实 |
| 快速入口与发行 | 相应链已发布的真实命令／能力、静态资源与通道 | 原 Workbench 及打包 Electron 产品操作 |

每条链可按自己的依赖独立验证；快速入口不代替底层完成，MCP 不要求先实现全部 Node API。扩展 API 矩阵中的未支持能力须明确拒绝，但本稿明确列入的制品、凭据、配置读写及实例隔离不能永久以“不支持”代替实现完成；task 9.1 建立 requirement／scenario／任务／原 owner delta 对照并逐项保留证据。

## Risks / Trade-offs

- [单 Remote 假设散布于扩展服务] → 同批清点 RunningLocation、HostManager、所有 MainThread／ExtHost 消费者和测试；D8 形成明确能力矩阵，缺能力明确拒绝，不加第二 extension service。
- [目录来源与第三方包运行条件不同] → 来源可配置、平台及运行依赖在目标验证；外部目录不可用不伪报无结果，不自动安装系统 runtime。
- [同包多 workspace 更新影响在途调用] → 包版本与 workspace 配置／连接 generation 分离，沿原租约排空后删除旧制品。
- [配置 UI 容易形成另一套有效状态] → 只读原 sources／active／pending，写入沿原 CAS／candidate；不增加前端事实源或跨 Gateway 配置继承。
- [多目标 host 增加资源占用] → 按实际客户端与目标视图惰性启动、有界回收，持久安装与存储不随进程删除。

## Migration Plan

1. 清点原 MCP／扩展／设置入口、协议、间接调用及测试，定义每条链唯一 owner 和删除清单；保留他人的实施证据，不重置既有任务。
2. 每次只迁一条完整纵向链，声明／权威写入／查询／恢复／消费者及测试一并迁齐；编号不要求三个 capability 全局串行实现。
3. 安装及设置新记录只写稳定目标格式；已有配置经原 owner 正常读取，需要改写时走原官方预览／备份／版本迁移。不能靠清空 profile、凭据或工作区状态完成迁移。
4. Web 用户安装从浏览器权威 profile 迁至目标原安装 owner；先清点调用和可核验 release 基线，必要迁移走显式目标选择及原安装服务，不静默复制到所有 Gateway。迁齐扫描／安装／升级／卸载／加载及测试后删除旧权威写入链；浏览器仅保留派生缓存和原窗口展示态，历史 Web 验收不作为新归属已实现的证据。
5. 将原 VSIX 管理组件接入 Gateway 现有服务组，保持稳定安装域、原 profile 路径、已发布包及配置资源；迁齐 Backend、Gateway、设置、资源和宿主初始化消费者后，删除 Backend 按请求启动管理 child 的旧链。进程归属移交本身不搬安装数据、不另建账本，也不单独提升 schema 版本；实际数据结构变化仍按 release 基线判断。
6. 入口迁齐后物理移除同义管理实现、重复 MCP client、临时默认目标与死命令。保留标准扩展 API 与原业务非管理接口的合法用途。
7. 按源码／产物版本撤回未发布实现；已经提交的远端配置或安装操作按原记录查询／显式新操作处理，回退 UI 不反向执行外部副作用。不认识新记录的旧版本明确拒绝，不双写或静默回退。

## Open Questions

可延后确定的是所选目录的实际 endpoint、匹配上游的 host 运行时发行方式、安装服务的文件内拆分及基于目标能力的数值上限；实施前须完成实际协议／发行验证。它们不能改变已定的目标身份、权限、归属、单 owner、原 API 与 Electron 边界。若候选市场不可合法使用或协议不匹配，应报告并保留手动来源，不暗中换源。若真实 host 原型证明必须改变现有扩展 API 或客户端边界，应先回到本设计和用户裁决，不以兼容层补丁绕过。
