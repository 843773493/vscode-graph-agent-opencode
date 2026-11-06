# 目录用途

在唯一 sourceTransform 管线中迁移固定上游的扩展安装实例、Host 视图、原 owner、状态、启用记录和必要文档投影。

# 可修改内容

按原生产 owner 分组的精确源码转换与实际转换文件清单；新增转换同步进入完整类型检查和构建。

# 不可修改内容

不得修改 upstream 原件、复制生产 Registry 或协议、保留 RemoteAgent 回退，或按公开 ID 混合不同 Backend 实例。

# 规范

原窗口 Registry 是唯一扩展事实源；Host 集合由其派生且保持公开 ID 唯一。静态贡献候选必须先验证后启动 Host，主线程回调保留原代际准入。每 Backend 当前只有一个明确 default view，集合变更要求 reload；必要文档过滤不代表全部扩展 API 已完成隔离。测试及产物遵守根 AGENTS。

Memento 的目标与稳定视图身份在原 ExtensionStorageService 内生成，MainThreadStorage 读写和通知使用同一身份；Web 保留原 profile 行为。目标文件型存储位置沿认证初始化映射，不把窗口状态库当目标文件系统。目标 Node 的跨设备 setKeysForSync 未迁齐消费者时从原公开 API 明确拒绝，不能写无人消费的声明。

目标 SecretStorage 尚未接入消费侧 owner，Node 入口在秘密进入浏览器 RPC 前拒绝；主线程不读写或向目标宿主广播 Web 秘密。Web 原秘密 API 保持，明确拒绝不等同目标能力已实现。

诊断继续由原 MainThreadDiagnostics／MarkerService 持有；目标 collection 的内部 owner 使用原 extHostId 区分，清空及释放只撤本代。事件按 workspace view 投影并过滤外部关联位置，保留普通正文及合法文档链接；写入先核整批资源，不能部分发布后报错。Web 保留原 owner 与窗口事件语义。

配置转换复用原 WorkspaceService、scope 合并及 ConfigurationEditing；显式目标 settings 不等待窗口 RemoteAgent。原文件夹 parser 保留 raw／名称并按目标平台解析，结果须匹配已认证的有序根；所有 MainThread customer 共用该 host 的配置／context child。配置读取的目标不可用错误不能吞成空配置。

标签投影只维护原 MainThreadEditorTabs 模型及索引；完整 EditorInput 的全部资源决定可见性，不把混合 MultiDiff 拆成可单独关闭的假标签。增量使用本宿主数组索引，结构重同步保留 Tab 对象身份；关闭混合组仅关闭当前宿主的完整输入，真实非空组不得移除。初始化、observable 和异步关闭遵守同一代际，错误不通过全量重建掩盖。

语言 Provider 在原 LanguageFeatureRegistry 的每个注册项绑定固定评分回调；归属在原排序和 exclusive 选择前生效，候选与 RelativePattern 只在评分时投向目标原生域，继续复用原 score／glob。共享注册表的缓存仍按原模型失效，不按当前焦点切换评分器；Web 原评分与 Provider 的 RPC／取消链保持。

工作区符号沿原 Provider 登记发起宿主，明确范围在调用前选择完整 host view；原 API 无目录参数，部分视图不得先查询后过滤。符号与原 Provider／cacheId 绑定，resolve 沿同一 RPC；取消和注销不发布迟到结果。原无 scope 的窗口查询及 Web 窗口级能力保持。

直接 workspace.applyEdit 复用原 MainThreadBulkEdits／BulkEditService，不增加编辑应用链。整批端点校验先于 revive，错误保留原 warn／false，代际关闭通过同一原 token 取消；BulkEditService 按实际完成资源区分取消后的零写入与已完成结果，不用关闭标志抹去已经发生的副作用。
