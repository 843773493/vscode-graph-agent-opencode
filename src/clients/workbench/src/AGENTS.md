# 目录用途

Web 与 Electron 共用的唯一完整 Code OSS 工作台入口。复用同级 web 的 React 会话产品，不维护第二套外层工作台。

# 可修改内容

本目录的启动、构建、宿主和业务贡献。upstream 是固定提交的官方 Git 子模块；项目定制放在宿主及贡献中，保持上游可核验。

# 不可修改内容

不得导入旧 web 的 App 或外层工作台布局；允许复用会话 React 组件、业务 hook 与组件样式，样式必须限制在 React 宿主中；不得依赖 reference_repo；不得伪造 Gateway/后端业务状态；不得运行 upstream 的测试。

# 规范

保留完整原版 Workbench 的服务和行为，不以 Grid 仿制替代。bun 管理独立依赖；生成文件写 dist 并忽略；测试及临时工作区遵守根 AGENTS。原版子模块沿用其目录和上游指引，项目新增源码目录须有四段式 AGENTS。

来源和更改文档沿原只读内容 Provider／resolver 共享模型与释放引用；更改仅合并在途变更集读取，不缓存第二份历史正文。只读输入只序列化稳定引用，原编辑器组保存视图态；旧更改投影在唯一输入工厂一次迁移，不复用临时路由或保留旧文件 Provider。

文件树单击沿原会话资源预览；原生菜单的打开、预览、侧开及打开方式通过 IEditorOptions.sourceGroup 固定右键发起组作为选组起点，普通打开用 ACTIVE_GROUP、侧开用 SIDE_GROUP；锁定、已有编辑器复用、侧开方向与后台焦点沿唯一原生选组算法。下方打开只传单次 sideBySideDirection 意图，不自行查找或创建分组；明确拖放或当前编辑器内部切换仍可指定目标组。执行时或打开方式选择后原组已关闭则明确拒绝，不由活动组隐式接收。

配置 URI 的 Opener 先经唯一 grammar 解析，再匹配原配置 owner 公布的 URI／source_key 并交原 JSONC 来源命令；远端 reference 仅由同一 parser 在目标 owner 上归一，不截取文件名推断来源、不交普通只读文档 Provider。发起组、选项和 owner 在异步读取前冻结，凭据或 Gateway 改变拒绝迟到打开。

所有规范 URI 的打开共用单次身份生命周期，覆盖解析、重解路由、main Thread 查询及只读模型取得；已绑定 Gateway 离开后切回仍失效，不继续读取、不创建侧边组或打开旧输入。已取得的模型引用仍沿原 resolver 释放，结束时释放凭据与 Gateway 订阅。不追加健康探测或自动重试；复制引用同样拒绝本次生命周期中曾失效的结果。

来源发现的首次可接受候选沿原 Quick Input 聚焦，后续更新按稳定 ID 保留；已聚焦目标消失后清空，不改投其它候选。聚焦不生成已选择项，Esc 取消仍零交付。来源范围及运行资源二级选择在原 picker 隐藏后继续监听本次凭据与 Gateway 身份，结束即释放。

文件、来源文档及精确 Thread 的复制引用共用 `resourceActions` 的原 Action／Command，编辑器菜单、文件菜单与 Quick Access 不另写复制逻辑。规范引用由 Gateway 唯一 URI owner 提供，包含明确 Gateway；Thread 只交付已有稳定身份，不查历史或启动目标。复制不打开资源、不读取正文、不固定版本或激活来源。

配置候选复制直接使用原来源目录已公布的 URI；已打开的来源编辑器只还原原 target／source_key 并重读该目录元数据。不从文件名生成规范地址，不读取配置正文或保存；没有 URI 的逻辑来源明确拒绝复制，仍保留原设置编辑能力。

引用与选定正文粘贴由 `composerActions` 的独立原 Action／Command 处理；来源在发起时捕获，接收者使用发起 Thread 或明确选择的可见 Thread。通过原 `ThreadPane.threadTarget` 捕获原 Composer writer，关闭、切换与身份失效明确拒绝，不维护最近 Thread 注册表、不另写草稿存储，也不伪装上传附件或启动执行。

Thread 发起的文件引用通过原 Quick Access 的只选择／多选模式取得未打开文件；先捕获草稿与工作区，再选择来源。勾选后取消不解析引用、不写草稿；接受后由唯一 URI owner 逐项解析，全部成功才一次追加原草稿。选择过程不打开编辑器、不读正文，不给混合打开列表增加附加修饰键。

Thread 的来源资源引用使用独立命令，先沿原控制面范围选择，再复用 `resource ` 的只选择／多选；原版返回按钮回到已选范围，Esc 直接取消。AGENTS、Skill、配置与附件仍由各原 owner 公布引用。原文件选择入口与来源选择共用逐项规范化和一次草稿追加，身份或接收者失效不继续下一项；范围与来源选择取消均不写入，也不把当前内容冒充固定版本。

只读来源／更改的损坏恢复记录沿原 TextResource serializer 保留原件及明确原因，使用原 EditorPlaceholder 和组关闭移除展示记录；没有可核验资源身份时不得发起路由或正文读取。正常引用仍交原 TextEditorService，工厂／服务故障不得误判成记录损坏，也不建立第二恢复存储。

Thread 的不可恢复记录同样使用原错误占位页，通过所属组的关闭动作移除该条展示记录；不调用业务删除或清空历史、草稿，缺少精确身份时不解析当前目标。

Thread 恢复和最近历史打开只共享在途路由解析；本次解析订阅凭据与 Gateway 身份变化，失效后取消且不写入迟到路由。冷启动的首次身份发布不视为切换，已绑定身份离开后切回仍失效；结束即释放订阅，后续重开重新解析登记，不序列化传输路由。

Thread 通过原 `IEditorPaneWithSelection` 发布消息、请求记录、Trace 与阅读锚点；前进／后退交原 HistoryService，组内恢复使用原 viewState。普通滚动替换当前位置，明确跳转增加记录；大纲预览及布局保持不产生跳转记录，不另建历史栈或持久位置库。

ThreadPane 继承原 `AbstractEditorWithViewState`，在释放 React 前由原 memento 保存快照，按组和稳定 Thread URI 恢复；明确 options 优先于保存位置，遵守原 restoreViewState／sharedViewState 设置。定位不写入 ThreadInput serializer 或另建缓存，关闭重开及窗口保存沿同一原生机制。

原 memento 恢复 viewport 时仅还原阅读位置，不切换聊天／日志展示，也不产生主动定位提示；明确接受阅读位置仍进入聊天并发布导航。控制器尚未就绪时沿同一个待执行导航保留这一区别。

Thread 当前定位由 ChatSurface 的单个互斥引用持有，Pane 只在原 selection／viewState 合同取快照；消息、请求和 Trace 的关闭都交同一宿主入口，将精确引用替换为当前可见阅读位置，不能在分屏复制时重新打开它。消息及阅读位置接受回到聊天视图，预览取消恢复原展示；viewState 恢复发布 PROGRAMMATIC，明确接受发布 NAVIGATION。

大纲加载临时卸下阅读控制器时，取消仍立即恢复展示，阅读位置沿同一待执行导航在控制器就绪后恢复；后续明确接受替换该待执行导航。阅读控制器的 navigate 只负责位置，展示切换由宿主显式发起，不能在迟到恢复时重新打开已取消的预览。
