# 目录用途

在原 Workbench 扩展服务和 IExtensionHost 边界接入明确 Backend 安装实例、工作区视图与原 IPC／RPC 传输。

# 可修改内容

可信库存准入、宿主连接、原初始化数据、typed URI 投影和原命令入口的目标分派。

# 不可修改内容

不得建立第二份扩展注册表、安装账本或工作区归属表，不得隐式扫描其它 Gateway、使用 RemoteAgent 回退、改写公开扩展 ID 或广播有目标的副作用。

# 规范

目标 Node／Web 的文件系统事件仅投影当前宿主 workspace view 与具名配置文件；配置父目录可监听但其中其它文件不转发。working-copy participant 与 didRun 仅处理归属完整的文件操作，扩展编辑在应用前完整核验。代际关闭后 watcher 与异步 participant 不得继续发布事件或应用编辑；窗口内置 Web 保留窗口广播语义。

公开 workspace.applyEdit 与文件操作参与者共用整批端点归属检查，resource／oldResource／newResource 全部核验后才恢复 DTO 和进入原 BulkEditService。直接编辑沿原错误日志及 false 合同返回，宿主关闭取消原生命周期 token；零资源完成的取消返回 false，已经完成的资源保留原结果。原 BulkEdit 对预览最终集合已有的文本／Notebook版本先整批预检，原 resolver 引用持有到本次批次结束；分组临执行检查仍保留。缺失的宿主版本生产接线、无版本字段的文件操作及其它语言 Provider 编辑入口仍须按原合同迁齐，不能把本入口保护称为完整编辑隔离。

安装身份来自目标 Gateway 的 API envelope，不依赖窗口工作区或已运行宿主。运行宿主的工作区集合仍来自原 IWorkspaceContextService；每个 Backend 当前只支持一个明确 default view，集合变更必须 reload。初始化位置只读已认证 binding，URI 只转换 typed 字段并复用 workspaceFiles 原投影；原宿主协议和代际撤销机制保持。测试和临时产物遵守根 AGENTS。

宿主关闭用原取消机制结清快照、共享认证、绑定、配置与原协议初始化等待；只撤销本宿主的认证等待，不取消共享凭据请求。每个异步结果在创建后续资源或发布成功前核当前代际，撤销同时释放监听与期限；明确取消保持拒绝，真实失败仍保留原原因。

运行诊断每次派生原扩展服务的视图、宿主与实例状态，不建立支持状态缓存；实现范围、授权、通道、绑定和激活分别报告。原命令派发元数据区分只读核心命令与扩展命令：前者不发送激活事件并保留真实调用宿主范围，后者保留原依赖路由。扩展查询不得枚举另一宿主或以管理选择替代发起代际。

目标 Web 先取得精确 profile／view／有序根／扩展 key 的冻结资格，再进入原 Registry；包位置为同源冻结 GET。复用原 iframe、Worker、握手与 RPC，不构造 Node 初始化或反转 typed URI。浏览器存储／日志命名空间使用目标、profile 和稳定 view，generation 不进入持久身份；目标 secrets 和跨设备 setKeysForSync 在公开 API 的 RPC 前明确拒绝。原 Worker 仍只支持 CommonJS browser 入口，ESM 入口及裸相对 dynamic import 不属于已实现能力。

原 Web 管理服务只有一个目标原生安装事实源；scanner 仅扫描系统／额外内置与派生缓存，不再读写用户 profile 账本。管理资源使用 current GET，运行资源使用资格 generation 的冻结 GET，二者不能互换越过 Registry 准入。市场／VSIX／metadata／卸载沿同一原生 owner，消费完整回执；原市场更新沿已安装对象冻结目标/profile，descriptor 负责机械字段序列化。启动只读入口等待已有 Gateway 初始化；共享库存随用户或入口身份失效立即淘汰并取消，迟到结果不能交给新身份。管理选择是展示态，在途操作不改投；非默认浏览器 profile 尚未接入原生合同，必须拒绝。

目标配置与 workspace context 由每个 host 的原 WorkspaceService 同时持有；原 MainThread customer 从同一 child DI 取得它。profile／多根配置只访问认证 binding 的两个具名资源，folder 配置复用普通工作区文件链；不轮换窗口 RemoteAgent，不新增正文、保存基线或配置合并实现。共享配置 provider 只合并同一资源的有效传输引用，文件 RPC 仍核发起 host 的精确资源。

运行视图的目标发现消费明确根及原生 Gateway 的两项管理能力，多根最终核完整有序集合，单根不重复发现。零根窗口只从缓存的页面入口 Gateway 身份发现唯一安装域并创建空 Web 视图，不读取管理标题选择或请求 Node 宿主；合法空目录明确诊断组件未配置，故障不能转换为空目录。视图仍保存在原 running-location owner；只有安装权限不读取宿主运行态或启动宿主，宿主初始化可只读原 profile 描述，不授安装写入权限。

自有包管理由 `servicePluginManagement.ts` 直接消费目标 Gateway 库存及原配置来源；Gateway、明确工作区与新建默认值各改原 owner 的 CAS 来源，不伪造 VSIX 或把 enabled 当 ready。工作区期望从原配置 owner 读取，异步交接状态另显；后台刷新以当前目标及代际结算。旧原生宿主模块尚在后续纵向迁移范围，不恢复已删除的 `installation.ts` 或旧安装命令。

自有命令由 `pluginCommandRegistry.ts` 持有原 Command／Menu／Keybinding 句柄，同逻辑 ID 只注册一个 dispatcher；完整快照先预校验再替换对应绑定，旧资格不得改投。`pluginCommands.ts` 负责窗口订阅与交互，传输归 Web `api/pluginCommands.ts`。命令面板在原 Quick Access 聚焦前冻结目标和已公布上下文，菜单保留自身冻结参数；程序化调用在首次 await 前捕获业务 owner。普通调用不经过旧宿主激活或重新发现；断连释放本目标投影，窗口关闭不修改 enabled。
