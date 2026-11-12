# 目录用途

Web 与 Electron 共用的唯一完整 Code OSS 工作台入口。复用同级 web 的 React 会话产品，不维护第二套外层工作台。

# 可修改内容

本目录的启动、构建、宿主和业务贡献。upstream 是固定提交的官方 Git 子模块；项目定制放在宿主及贡献中，保持上游可核验。

# 不可修改内容

不得导入旧 web 的 App 或外层工作台布局；允许复用会话 React 组件、业务 hook 与组件样式，样式必须限制在 React 宿主中；不得依赖 reference_repo；不得伪造 Gateway/后端业务状态；不得运行 upstream 的测试。

# 规范

保留完整原版 Workbench 的服务和行为，不以 Grid 仿制替代。bun 管理独立依赖；生成文件写 dist 并忽略；测试及临时工作区遵守根 AGENTS。原版子模块沿用其目录和上游指引，项目新增源码目录须有四段式 AGENTS。

`server.mjs` 是 Electron 与独立静态服务的同一 HTTP／SSE／WebSocket 代理；响应关闭时释放对应 HTTP 上游请求，不等待已经结束的请求体再次触发 aborted，也不能关闭其它 owner 的连接。传输边界验证归 `tests/integration/clients/web/workbench_server.test.mjs`，不替代真实 Gateway 或原生宿主验收。

`sourceTransform.mjs` 同时服务正式构建和 Vite。日志及设置共用的 IndexedDB 连接随浏览器文档释放，不能在同步 shutdown 开始时主动关闭并中止仍在提交的日志；不得屏蔽存储错误或替换持久日志。正式元数据页面回归同时验证真实刷新恢复与受控生命周期后的日志提交。

原 Layout 隐藏底部面板前先记录焦点是否属于该面板；仅此时交回编辑器，自动恢复不得抢走 Quick Input 等外部控件焦点。工作区面板的标签／尺寸恢复仍沿原 owner 和原生面板服务，不能另存选中状态或靠隐藏后重新 focus 补偿。

`dev.mjs` 为统一开发栈提供 JSON `/health`；`--outdir` 沿用构建入口的静态资源输出语义，`--cache` 只指定 Vite 派生缓存目录。正常开发默认使用本目录 dist 和 Vite 默认缓存，正式测试必须显式传入自己 runtime 下的输出／缓存目录；两者都不能改变业务工作区或用户配置。

`devConfig.mjs` 使用 Vite 原生源码服务：固定 Code OSS 及第三方依赖沿 Rolldown 预构建，业务源码沿 React SWC 按需转换和热更新；两者复用 sourceTransform，保留参数装饰器及类字段赋值语义。扫描仅从产品源码入口开始；上游反向引用的业务模块保留真实源码 URL，不能复制服务或类身份。转换脚本及内置元数据变化须使 Vite 预构建缓存失效，普通重启复用缓存。

开发服务的 health 等待源码入口预热及依赖预构建完成后才返回 200／ok，错误保留原原因；源码和转换脚本仍由 Vite 监视、热更新或配置重载，不用发布 bundle 回退代替开发入口。

业务文件的原图片编辑器沿同一源码转换消费删除事件；重新读取失败时按当前加载代际撤下旧图、释放 object URL 并显示原错误。恢复后的真实 watcher 仍沿原编辑器重读，不新增图片 Pane 或字节缓存，也不改其它原生文件的行为。

原 FileEditorInput 的并发 resolve 共用同一在途模型引用获取，失败后允许明确重试，取得的引用仍由原 input 关闭／迟到关闭分支释放；不另建模型或引用账本。

原 TextEditorService 的输入创建点统一归一项目只读引用；旧更改 FileEditorInput、历史和新打开都收敛为相同资源身份及原只读输入。转换仍走同一精确锚点管线并纳入实际转换源码类型检查，不在各消费者保留旧投影解析。

ConfigurationCache 继续使用原 type／key、cacheHome 与单条目队列；目录准备和正文写入必须一起排队，防止迟到保存越过后发更新或删除。目录或写入失败保留原原因，不以返回成功掩盖；本地 workbench 设置仍不重复缓存，非本地配置由原消费者更新。FolderConfiguration 的默认值／scope／信任重解析只使用已加载正文，不重新读盘刷新原文缓存；首次 provider 就绪与真实文件变化仍由原 onDidFolderConfigurationChange 更新缓存。

扩展 staging／发布的目录过滤仅检查复制源内部的相对路径；源码祖先目录名不得影响结果，路径分隔符使用当前平台约定。`extensions.test.mjs` 用真实文件复制验证祖先目录、内部测试／依赖排除及上游原件不变；不代替完整扩展编译与 Workbench 页面验收。

Webview 允许根中的 HTTP 扩展资源沿原 `asWebviewUri`／隔离 Worker／FileService 加载；根外 URL 保留原直接加载规则。Markdown 在首次生成贡献资源地址前更新同一允许根，HTTP Provider 的占位 stat 不充当缓存 validator；业务文件保留原 ETag／NotModified。字体 MIME 在原映射补齐，不复制 Worker、放宽 CSP 或建立第二份字节缓存。构建与 Vite 共用转换清单；类型检查按原 Workbench 与各扩展的宿主声明和精确依赖别名分别检查全部转换源码。

原 OutlineModelService 的旧版本最后消费者取消只释放自己的缓存条目，不能按模型 ID 删除后来建立的新版本；沿原 LRU／模型版本与引用生命周期，不新建分析缓存。

`fileDialogTransform.mjs` 只沿原 FileService／SimpleFileDialog 传递工作区目录错误；真实 stat 缺失仍用原 FILE_NOT_FOUND 候选合同，readdir 拒绝保留原原因，不添加探针、目录缓存或另一套选择器。所有原更新入口回到同一对话框终结器；取消只释放当前更新，失败先终结再清 busy，避免排队接受消费旧项。确认 prompt 由原对话框释放，关闭后迟到验证不能复活控件或开始创建目录；非工作区 scheme 和保存新文件候选保持原合同。

`pluginCommandTransform.mjs` 将自有命令接入原 CommandService、菜单动作和 Quick Access 的冻结参数，并为原 KeybindingsRegistry 提供可释放的单条登记及 resolver 失效通知；保留键盘布局和用户覆盖。不得按目标调用全量 `setExtensionKeybindings` 或复制原解析器，不修改固定上游。新增接缝纳入实际转换源码类型检查。


原用户 Node／Web Extension Host、VSIX／Gallery 安装与专用构建脚本已退役。浏览器 scanner 只保留产品内置贡献和派生缓存，原 Worker 与 Registry 继续运行；自有包管理沿原配置及 Gateway 插件 owner。
