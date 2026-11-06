# 目录用途

在原 Workbench 扩展服务和 IExtensionHost 边界接入明确 Backend 安装实例、工作区视图与原 IPC／RPC 传输。

# 可修改内容

可信库存准入、宿主连接、原初始化数据、typed URI 投影和原命令入口的目标分派。

# 不可修改内容

不得建立第二份扩展注册表、安装账本或工作区归属表，不得扫描全部登记目标、使用 RemoteAgent 回退、改写公开扩展 ID 或广播有目标的副作用。

# 规范

目标 Node 的文件系统事件仅投影当前宿主 workspace view 与具名配置文件；配置父目录可监听但其中其它文件不转发。working-copy participant 与 didRun 仅处理归属完整的文件操作，扩展编辑在应用前完整核验。代际关闭后 watcher 与异步 participant 不得继续发布事件或应用编辑；Web 保留窗口广播语义。

安装身份来自 API envelope，工作区集合来自原 IWorkspaceContextService；每个 Backend 当前只支持一个明确 default view，集合变更必须 reload。初始化位置只读已认证 binding，URI 只转换 typed 字段并复用 workspaceFiles 原投影；原宿主协议和代际撤销机制保持。测试和临时产物遵守根 AGENTS。

目标配置与 workspace context 由每个 host 的原 WorkspaceService 同时持有；原 MainThread customer 从同一 child DI 取得它。profile／多根配置只访问认证 binding 的两个具名资源，folder 配置复用普通工作区文件链；不轮换窗口 RemoteAgent，不新增正文、保存基线或配置合并实现。共享配置 provider 只合并同一资源的有效传输引用，文件 RPC 仍核发起 host 的精确资源。
