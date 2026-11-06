# 目录用途

在唯一 sourceTransform 管线中迁移固定上游的扩展安装实例、Host 视图、原 owner、状态、启用记录和必要文档投影。

# 可修改内容

按原生产 owner 分组的精确源码转换与实际转换文件清单；新增转换同步进入完整类型检查和构建。

# 不可修改内容

不得修改 upstream 原件、复制生产 Registry 或协议、保留 RemoteAgent 回退，或按公开 ID 混合不同 Backend 实例。

# 规范

原窗口 Registry 是唯一扩展事实源；Host 集合由其派生且保持公开 ID 唯一。静态贡献候选必须先验证后启动 Host，主线程回调保留原代际准入。每 Backend 当前只有一个明确 default view，集合变更要求 reload；必要文档过滤不代表全部扩展 API 已完成隔离。测试及产物遵守根 AGENTS。

VSIX 原命令 ID、菜单及拖放调用保持；只精确替换两条原 action 的安装处理与菜单门槛，不保留单 local／remote 安装回退。目标视图 getter 直接投影原运行位置 owner 的完整视图，不能另存安装目标；其中只有宿主管理获准的视图产生 Node running location，权限来自同一次完整目标准入，不由扩展是否存在猜测。转换源码纳入同一真实类型检查和构建。

Memento 的目标与稳定视图身份在原 ExtensionStorageService 内生成，MainThreadStorage 读写和通知使用同一身份；Web 保留原 profile 行为。目标文件型存储位置沿认证初始化映射，不把窗口状态库当目标文件系统。目标 Node 的跨设备 setKeysForSync 未迁齐消费者时从原公开 API 明确拒绝，不能写无人消费的声明。

目标 SecretStorage 尚未接入消费侧 owner，Node 入口在秘密进入浏览器 RPC 前拒绝；主线程不读写或向目标宿主广播 Web 秘密。Web 原秘密 API 保持，明确拒绝不等同目标能力已实现。

诊断继续由原 MainThreadDiagnostics／MarkerService 持有；目标 collection 的内部 owner 使用原 extHostId 区分，清空及释放只撤本代。事件按 workspace view 投影并过滤外部关联位置，保留普通正文及合法文档链接；写入先核整批资源，不能部分发布后报错。Web 保留原 owner 与窗口事件语义。

配置转换复用原 WorkspaceService、scope 合并及 ConfigurationEditing；显式目标 settings 不等待窗口 RemoteAgent。原文件夹 parser 保留 raw／名称并按目标平台解析，结果须匹配已认证的有序根；所有 MainThread customer 共用该 host 的配置／context child。配置读取的目标不可用错误不能吞成空配置。

标签投影只维护原 MainThreadEditorTabs 模型及索引；完整 EditorInput 的全部资源决定可见性，不把混合 MultiDiff 拆成可单独关闭的假标签。增量使用本宿主数组索引，结构重同步保留 Tab 对象身份；关闭混合组仅关闭当前宿主的完整输入，真实非空组不得移除。初始化、observable 和异步关闭遵守同一代际，错误不通过全量重建掩盖。

语言 Provider 在原 LanguageFeatureRegistry 的每个注册项绑定固定评分回调；归属在原排序和 exclusive 选择前生效，候选与 RelativePattern 只在评分时投向目标原生域，继续复用原 score／glob。共享注册表的缓存仍按原模型失效，不按当前焦点切换评分器；Web 原评分与 Provider 的 RPC／取消链保持。

工作区符号沿原 Provider 登记发起宿主，明确范围在调用前选择完整 host view；原 API 无目录参数，部分视图不得先查询后过滤。符号与原 Provider／cacheId 绑定，resolve 沿同一 RPC；取消和注销不发布迟到结果。原无 scope 的窗口查询及 Web 窗口级能力保持。

直接 workspace.applyEdit 复用原 MainThreadBulkEdits／BulkEditService，不增加编辑应用链。整批端点校验先于 revive，错误保留原 warn／false，代际关闭通过同一原 token 取消；BulkEditService 按实际完成资源区分取消后的零写入与已完成结果，不用关闭标志抹去已经发生的副作用。 原 BulkEdit 在预览最终集合确定后、任一类型副作用前核已有文本／Notebook版本；已加载文本先由原 ModelService 拒绝已知冲突，所有带版本的资源沿原 resolver 本次去重并持有到整批结束，结算后释放，不新增版本账本。全部异步解析结束后统一核对，保留各类型临执行检查处理随后变化；未携带版本的文件／opaque／文本不合成版本，尚缺的宿主版本生产接线继续保留 TODO。

Code action 的 provide／resolve 在原 MainThread 内先核整批 edit，非法后项不能发布合法前项。请求 token 同时响应调用取消和注册释放；迟到 provide 释放准确的新 cache，resolve 拒绝后不改原 action。已发布 list 继续持有原 cache，由其幂等 dispose 收尾，不增加 session registry。目标 Provider 将同一注册生命周期留在原 Provider，原 CodeActionItem／applyCodeAction 合并调用方取消，并将同一 token 交原 BulkEditService；撤代后不启动编辑或后继 command，已开始的副作用不回滚。普通 Web／内置 Provider 不附带目标生命周期；不把本闭包扩大为其它语言编辑入口。

原 CodeActionAdapter 在 provide／resolve 各自扩展回调前，仅冻结本宿主已同步文档的 URI 与数值版本，交原 WorkspaceEdit.from；同一转换在 Node 分流前执行，不能只修 renderer 而漏掉真实宿主。快照只活到本次转换完成，不保存正文或建立版本账本；异步等待后的新版本不能认证旧编辑。未知资源及同批新建目标保持原无版本语义，Notebook 版本仍由其原文档 owner 后续接线。原 CodeActionController 的 UI 应用入口将拒绝交原通知服务显示，同一 finally 结清进度和原刷新；核心 apply 继续拒绝，不改全局错误处理或把版本冲突当启动失败。

RenameAdapter 与 CodeAction 共用原文档版本快照，在 provide 回调前冻结；MainThread 的 provide／prepare 共用原请求生命周期，返回后先核取消，再整批校验实际 edits 并沿原 revive。保留无 edits 的 rejectReason、undefined 和 prepare 原位置语义，注销及关闭拒绝迟到回包，不新建 Provider registry。已返回的窗口结果以不可枚举字段保留注册 token，避免公开命令回包串入 RPC；原 BulkEditService 在 lift／早退前合并调用 token，并贯穿预览／实际应用和 finally 释放。RenameController 冻结原父操作 token 并等待应用，不用自身编辑会取消的状态 token；inline 缓存过滤退役结果，原命令等待应用后再清理。原预览在打开视图前启用其 when context，取消沿原 Pane._done 释放输入和 resolver，仅准确会话清上下文；原预览 Provider 在异步源读取、正文准备及后台自引用处核退役，尚未取得引用的新模型和迟到引用准确释放，活跃自引用错误仍交原错误处理。Pane 的异步点击在两次准备等待后核发起 input／provider，不让旧结果打开新预览；已生效资源保留真实结果。原 Chat response stream 分项移交后的应用仍留源码 TODO，不以 BulkEdit 入口保护冒充该链完成。
