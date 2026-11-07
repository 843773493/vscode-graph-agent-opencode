# 目录用途

Web 与 Electron 共用的唯一完整 Code OSS 工作台入口。复用同级 web 的 React 会话产品，不维护第二套外层工作台。

# 可修改内容

本目录的启动、构建、宿主和业务贡献。upstream 是固定提交的官方 Git 子模块；项目定制放在宿主及贡献中，保持上游可核验。

# 不可修改内容

不得导入旧 web 的 App 或外层工作台布局；允许复用会话 React 组件、业务 hook 与组件样式，样式必须限制在 React 宿主中；不得依赖 reference_repo；不得伪造 Gateway/后端业务状态；不得运行 upstream 的测试。

# 规范

`extensionHostStateTransform.mjs` 在浏览器与 Node 构建分流前应用同一宿主状态合同。描述和归属先验证候选，路径索引成功后一次发布两个原 registry、激活事件和 wire 版本，再发通知；registry 本地 revision 不代替 wire 版本。状态 RPC 失败由原扩展服务移除准确宿主代际、撤销退出订阅并报告，不能继续消费后续增量或自动重放。原 Node／Worker RPC 与 eager 启动语义保持，不能为此增加另一套协议或状态存储。

每代`IInternalExtensionService`闭包在eager／lazy Manager的disconnect首次await前及dispose入口撤销；状态回调不能更新后续代际，已准入激活迟到完成明确失败，不自动重放。MainThread查询与激活错误描述只读发起host当前快照，注册等待后再核代际；三类运行状态回调仅接受自有项，激活错误保留原activator对可执行`api:none`依赖的诊断归属。显式及新增扩展激活只发给唯一持有者，同ID歧义不得广播。其它MainThread异步UI及RPC载荷仍须按各owner完成代际与视图投影，生产快照也须完成多目标投影，不把这些准入检查当作完整隔离。

保留完整原版 Workbench 的服务和行为，不以 Grid 仿制替代。bun 管理独立依赖；生成文件写 dist 并忽略；测试及临时工作区遵守根 AGENTS。原版子模块沿用其目录和上游指引，项目新增源码目录须有四段式 AGENTS。

`server.mjs` 是 Electron 与独立静态服务的同一 HTTP／SSE／WebSocket 代理；响应关闭时释放对应 HTTP 上游请求，不等待已经结束的请求体再次触发 aborted，也不能关闭其它 owner 的连接。传输边界验证归 `tests/integration/clients/web/workbench_server.test.mjs`，不替代真实 Gateway 或原生宿主验收。

`sourceTransform.mjs` 同时服务正式构建和 Vite。日志及设置共用的 IndexedDB 连接随浏览器文档释放，不能在同步 shutdown 开始时主动关闭并中止仍在提交的日志；不得屏蔽存储错误或替换持久日志。正式元数据页面回归同时验证真实刷新恢复与受控生命周期后的日志提交。

原 Layout 隐藏底部面板前先记录焦点是否属于该面板；仅此时交回编辑器，自动恢复不得抢走 Quick Input 等外部控件焦点。工作区面板的标签／尺寸恢复仍沿原 owner 和原生面板服务，不能另存选中状态或靠隐藏后重新 focus 补偿。

`dev.mjs` 为统一开发栈提供 JSON `/health`；`--outdir` 沿用构建入口的静态资源输出语义，`--cache` 只指定 Vite 派生缓存目录。正常开发默认使用本目录 dist 和 Vite 默认缓存，正式测试必须显式传入自己 runtime 下的输出／缓存目录；两者都不能改变业务工作区或用户配置。

Vite 依赖扫描只从本产品 index.html 开始，不扫描上游样例／测试 HTML；源码转换与依赖预构建共用参数装饰器及类字段赋值语义，不以禁用扫描或放宽浏览器期限掩盖冷启动失败。

开发服务先通过 Vite client environment 转换产品入口及其静态依赖图，health 等待预热完成后才返回 200／ok。业务源码仍交给同一 Vite 转换与热更新，不用发布 bundle 回退代替开发入口。

业务文件的原图片编辑器沿同一源码转换消费删除事件；重新读取失败时按当前加载代际撤下旧图、释放 object URL 并显示原错误。恢复后的真实 watcher 仍沿原编辑器重读，不新增图片 Pane 或字节缓存，也不改其它原生文件的行为。

原 FileEditorInput 的并发 resolve 共用同一在途模型引用获取，失败后允许明确重试，取得的引用仍由原 input 关闭／迟到关闭分支释放；不另建模型或引用账本。

原 TextEditorService 的输入创建点统一归一项目只读引用；旧更改 FileEditorInput、历史和新打开都收敛为相同资源身份及原只读输入。转换仍走同一精确锚点管线并纳入实际转换源码类型检查，不在各消费者保留旧投影解析。

ConfigurationCache 继续使用原 type／key、cacheHome 与单条目队列；目录准备和正文写入必须一起排队，防止迟到保存越过后发更新或删除。目录或写入失败保留原原因，不以返回成功掩盖；本地 workbench 设置仍不重复缓存，非本地配置由原消费者更新。FolderConfiguration 的默认值／scope／信任重解析只使用已加载正文，不重新读盘刷新原文缓存；首次 provider 就绪与真实文件变化仍由原 onDidFolderConfigurationChange 更新缓存。

扩展 staging／发布的目录过滤仅检查复制源内部的相对路径；源码祖先目录名不得影响结果，路径分隔符使用当前平台约定。`extensions.test.mjs` 用真实文件复制验证祖先目录、内部测试／依赖排除及上游原件不变；不代替完整扩展编译与 Workbench 页面验收。

Webview 允许根中的 HTTP 扩展资源沿原 `asWebviewUri`／隔离 Worker／FileService 加载；根外 URL 保留原直接加载规则。Markdown 在首次生成贡献资源地址前更新同一允许根，HTTP Provider 的占位 stat 不充当缓存 validator；业务文件保留原 ETag／NotModified。字体 MIME 在原映射补齐，不复制 Worker、放宽 CSP 或建立第二份字节缓存。构建与 Vite 共用转换清单；类型检查按原 Workbench 与各扩展的宿主声明和精确依赖别名分别检查全部转换源码。

原 OutlineModelService 的旧版本最后消费者取消只释放自己的缓存条目，不能按模型 ID 删除后来建立的新版本；沿原 LRU／模型版本与引用生命周期，不新建分析缓存。

浏览器 WebExtensionsScannerService 仅保留可再生 system／custom 缓存及其原扫描回退，用户 profile 安装读写统一归目标原生管理服务；目标库存损坏或读取失败向上传递原错误，不以空列表覆盖原件，不恢复浏览器权威安装链。

`nodeHost.mjs`构建原宿主和目标管理两个独立进程入口，共用固定上游与冻结依赖闭包。管理入口只装配原生安装／profile／scanner服务，不启动整套Remote Agent或扩展代码；当前只发行空的内置扩展目录，不承诺Node内置扩展可用。`typecheckNode.mjs`从明确组件manifest解析真实运行依赖，与浏览器类型检查共用`typecheckProjection.mjs`投影实际转换源码；Node配置同时检查实际Extension Host与管理进程两个入口、原vscode／monaco／debug声明；Node类型版本与固定上游锁一致，不能用管理图代替整个宿主类型闭包或检查未转换上游。类型通过仍不代替真实API／目标产品验收。原安装器尚未创建任务时直接保留前置错误；原scanner的后台缓存校验消费dispose取消，其他错误仍走原unexpected-error处理，不加全局rejection吞噬。

`nativeManagementTransform.mjs`只进入Node构建，暴露原watcher／目录事件／下载缓存的初始化及原异步回调结算；旧版本仍沿官方profile与.obsolete标记，不调用物理cleanUp。Gallery下载的缓存释放Promise必须由原安装任务观察，签名或传输失败不能产生未处理拒绝而杀死常驻组件。原nodeRequest的gzip解压使用Node标准pipeline传递源断流错误，错误继续交原消费流和安装器，不新增重试或吞错。管理组件关闭时先释放消费者，再在其明确持有的disk provider上await原watch([])，最后dispose provider并自然退出，不复制watcher算法或扫描安装目录。

直接位置和原 Gallery 安装写入前消费同次 toScannedExtension 的 isValid／validations，拒绝无效版本并保留原件；警告及原扫描诊断语义不变，不重复计算 manifest 校验。

项目 protobuf 运行库随 descriptor 内联进 Node 产物，沿项目锁解析；其余外部运行依赖仍只取固定上游 npm 锁。不得为协议选项校验额外要求目标安装项目依赖或复制另一份手写校验器。

原生VSIX在原`ExtensionManagementService.install`创建任务前复用官方`validateExtensionManifest`的Error诊断；保留原engines兼容判断和合法Warning，不在Python或窗口另建manifest校验器。此转换仅进入Node清单，拒绝首次安装或升级时不得改写profile及旧制品；安装预检不等于目标能力、依赖或运行激活已验收。

原生VSIX的`keepExisting=true`沿原scanner只复用有效且ExtensionKey完全一致的已发布目录；候选先预检，原扫描对象通过同次context交给依赖规划和原task，依赖与正文都消费实际复用manifest。`reused`只来自原提取／准备结果，不按目录存在、hash或第二库存猜测；扫描失败保留原错误和目录，成功扫描前不得解除`.obsolete`。false或未指定仍沿原替换语义，原Gallery的复用扫描失败也不得隐式删除重提取；这不代表物理清理或活宿主引用保护已完成。

`fileDialogTransform.mjs` 只沿原 FileService／SimpleFileDialog 传递工作区目录错误；真实 stat 缺失仍用原 FILE_NOT_FOUND 候选合同，readdir 拒绝保留原原因，不添加探针、目录缓存或另一套选择器。所有原更新入口回到同一对话框终结器；取消只释放当前更新，失败先终结再清 busy，避免排队接受消费旧项。确认 prompt 由原对话框释放，关闭后迟到验证不能复活控件或开始创建目录；非工作区 scheme 和保存新文件候选保持原合同。
