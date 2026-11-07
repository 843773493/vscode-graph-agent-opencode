## Why

同一工作台需要管理当前 Gateway 及远端 Gateway 下、一个后端承载的多个工作区。仅展示原版扩展／MCP 面板无法确定安装位置、执行宿主、工作区启用范围与配置归属，也不能让原版 MCP 运行时自动接入项目 Agent。

用户在 Extensions 视图的标题工具栏选择目标 Gateway，默认本地；列表、搜索、详情和后续安装沿用该选择，不逐个插件重新询问目标。MCP 和设置需要切换目标时复用同一控件与交互规则，各管理视图保留自己的选择。每个 Gateway 是其管理环境的安装与配置入口；直接打开远端页面，或从本地页面经联邦访问它，使用同一份目标安装与配置事实。需要复用 Code OSS 的成熟界面与宿主机制，以及现有工作区 MCP owner、配置提交及联邦管理链路。

## What Changes

- 在原 Extensions 视图容器的标题工具栏增加目标切换动作，默认本地；点击目标动作时复用原 Quick Input，原列表、搜索、详情及 Install／Update／Uninstall 操作消费视图已选目标，不逐项弹目标选择器。MCP／设置复用该控件与规则，仅在业务需要时补选工作区或来源层；不增加随焦点变化的工作台全局目标，也不将“此工作台／Web”列为另一安装目标。
- 分开 VSIX 扩展市场与 MCP 服务目录，使用可配置且经真实验证的来源；本地 VSIX、手动 MCP 定义与目录安装进入各自同一管理链，不承诺所有第三方市场或扩展均可用。
- Web／Node 扩展和 MCP 制品均由所选目标侧管理，多个前端共享该目标的安装包、版本和库存；工作区独立启用、参数、凭据与 MCP 连接。配置范围和运行实例保留为内部必要信息，不让用户重复选择三套目标，也不将共享包解释为共享进程或授权。
- 原生目标 Gateway 通过既有受管进程机制连接每个安装域唯一、持续复用的 Node 扩展管理服务。该组件直接提供受本机认证保护的 HTTP 服务并自行持有安装域独占锁；Gateway 启动先核验接管已有实例，没有实例才启动。新旧 Gateway 换代只交接管理句柄，完整停机才排空并关闭服务；候选 Gateway 启动失败不得关闭旧版正在使用的实例。不新增代理进程或移交 Launcher。仅复用原 Code OSS 安装／更新／卸载、库存及必要的 profile／scanner 语义，不将该进程开放为 Python 的通用 Node 服务。多个 Backend 和客户端共用，不按请求启动 Node，也不随工作区切换、页面关闭或单个 Backend 重启而退出。Gateway 最终判权，Relay 只转送；实际 Node 扩展仍在 Backend 所持的独立 Extension Host 运行，Web 扩展仍在浏览器执行，MCP 安装与连接继续交原 owner。
- VSIX 下载、目标文件读写、包资源字节读取、文件监听与日志收集优先复用目标侧现有 Python 服务；缺少能力时在原 Python owner 补齐，不借扩展管理 Node 新建通用文件、监听或环境服务。Code OSS 配置模型、scope 与 JSONC 编辑语义保留在原服务，配置文件 I/O 和通知迁齐 Python 链；Node 安装器自身必需的内部文件服务和监听保留为实现细节，不形成第二份对外服务或安装事实。
- MCP 的新安装界面消费现有配置 owner、`McpCatalogOwner`、连接操作、OAuth 与调用监督；继续使用唯一目录／generation／租约，不再启动一套与项目 Agent 无关的 MCP 运行时。
- Web 扩展从所选目标的同一安装记录加载，通过 Gateway 提供已授权的静态贡献与 browser 入口，在当前 Workbench 原 Web Extension Host 执行；Node 入口在该目标后端宿主执行。browser／main 只决定运行分派，不形成两套安装服务或独立的浏览器权威库存。Electron 仍只承担窗口、生命周期和固定 profile，复用同一 renderer，不增加原生文件系统 IPC 或 Electron 插件宿主。
- `extension_host_manage` 统一覆盖目标 Web／Node 扩展运行，安装管理不代授执行；只读包资源允许安装管理或宿主管理任一权限，但不扩大可访问版本。已运行 Web 实例冻结目标及包版本，更新或逻辑卸载不截断其合法资源读取，直到实例关闭或运行授权撤销；新实例只使用当前安装版本，物理回收继续尊重原引用。
- 将原版单 Remote running location 扩展为有明确目标及工作区视图的宿主路由，复用原扩展 API／RPC／贡献机制；明确同名命令、资源 Provider、终端、调试、Webview 和存储的归属，不靠切换全局 remoteAuthority 支持多目标。
- 迁齐按扩展 ID 唯一索引的内部注册与激活链，支持同 ID 多目标实例；文档、编辑器、配置事件和运行态贡献按宿主视图投影，窗口级声明冲突明确诊断。
- 复用现有配置来源层、schema、CAS、幂等提交、active／pending、用户级 source fan-out 和 restart 分类，在设置编辑器顶部使用相同目标控件并提供来源说明、表单与 JSONC 编辑。远端保存自己的配置，本地只保存连接登记与展示镜像；已打开的编辑内容不随其它视图切换目标。
- 明确补齐现有配置 API 尚缺的来源正文／schema／source writer，以及远端配置读写能力；VSIX 设置独立沿原 Code OSS profile／remote user／workspace／folder／语言覆盖模型，不混入 Python 业务配置。
- 新增目标侧实际 MCP 制品安装与普通敏感输入的私有引用链；保留动态 MCP definition Provider 的发现／resolve／变更／撤销生命周期，制品引用沿原连接代际监督结算。
- 扩展／MCP 命令与管理对象接入 `add-workbench-quick-access` 的原 Registry、Command、Opener 和查询合同；注册能力后由标准入口消费，不另建项目插件 ABI、全局索引或设置事实源。
- 明确兼容能力矩阵、安装／配置／运行的不同阶段、断线及重启恢复、同机多工作区隔离和真实 Web／Electron／远端验收。任务按职责组织，不定义全局串行实施顺序。

## Capabilities

### New Capabilities

- `mcp-installation-management`: 目标感知的 MCP 目录发现、安装、版本、工作区启用、共享包引用与既有 Agent MCP owner 集成。
- `multi-target-extension-hosts`: 原版扩展管理与 Web／多目标 Node 宿主、贡献路由、能力约束、存储隔离及生命周期。
- `target-scoped-settings`: 同一工作台内按 Workbench、原生 Gateway、Workspace 来源层编辑设置，并展示真实来源、生效与冲突状态。

### Modified Capabilities

- `configuration-domains`: 补明确选中来源文件的读取、无副作用预检与文件 CAS writer，保留原单工作区 SQLite 层物化及用户 source fan-out 语义。
- `configuration-evolution`: 区分来源保存、层物化与 active／pending 生效，沿原候选、幂等提交及配置事件报告真实状态。
- `remote-gateway-federation`: 登记默认拒绝且绑定认证主体的 configuration_read／configuration_write，覆盖实际披露与写入点，并与原生命周期权限分开。
- `virtual-resource-addressing`: 为目标原 Code OSS profile settings 和稳定多根 workspace 文件登记两个具名 `config` URI 模板、精确文件／直接父级能力及认证宿主映射，不改变既有业务 ConfigSource 的单段逻辑名。

现行 `mcp-connection-supervision`、`extension-connection-management` 和多工作区挂载的原业务状态机保持不变；私有输入引用、受管 launch binding 及安装／宿主管理合同仍须随对应实施切片按 task 1.6 补齐原 owner delta／schema／消费者。涉及原合同的其它变化须先明确规划，不借 UI 接入默改行为。

## Impact

- Workbench：现有贡献及 sourceTransform、原扩展／MCP 管理服务、扩展宿主路由、设置编辑器和快速访问接入；不修改固定上游子模块或复制原版面板。
- 后端：保留工作区 MCP、业务配置与 Node 扩展运行宿主 owner；VSIX 安装管理调用同一目标 Gateway 托管的原安装器，配置及资源文件 I/O／监听消费目标侧 Python 服务，移除 Backend 自行按请求启动管理 Node 及借 Node 提供通用文件能力的链路。
- Gateway：作为统一目标入口，复用 registry、联邦管理授权、路由、生命周期和请求追踪，与 Backend／Browser／Terminal 一同管理 Node 安装组件的启动、就绪、日志和停止；安装事实继续由组件中的原 Code OSS 服务持有，Python 不维护另一份库存或 profile 成员。通用能力按既有 Python owner 归属提供；Gateway 不执行插件代码、不直接读写工作区业务数据，远端 Relay 不复制安装／配置事实，SSH 只承担既有连接与传输职责。
- 协议与发行：增加明确目标、能力、安装操作及宿主通道合同，迁齐生成物及消费者。运行时组件、离线资源与版本需进入真实发行产物验证；不依赖开发服务器或源码目录。
- 规划组织：一个独立 change 内拆分三个 capability，与快速访问变更双向关联；本次仅修订规划，保留已有实施证据，统一目标、Web 安装归属、Gateway 常驻安装器及 Python／Node 职责收口均按新要求验收，不将规划修改或旧链路的通过记录记为新架构已实现。
