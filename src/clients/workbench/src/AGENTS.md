# 目录用途

Web 与 Electron 共用的唯一完整 Code OSS 工作台入口。复用同级 web 的 React 会话产品，不维护第二套外层工作台。

# 可修改内容

本目录的启动、构建、宿主和业务贡献。upstream 是固定提交的官方 Git 子模块；项目定制放在宿主及贡献中，保持上游可核验。

# 不可修改内容

不得导入旧 web 的 App 或外层工作台布局；允许复用会话 React 组件、业务 hook 与组件样式，样式必须限制在 React 宿主中；不得依赖 reference_repo；不得伪造 Gateway/后端业务状态；不得运行 upstream 的测试。

# 规范

保留完整原版 Workbench 的服务和行为，不以 Grid 仿制替代。bun 管理独立依赖；生成文件写 dist 并忽略；测试及临时工作区遵守根 AGENTS。原版子模块沿用其目录和上游指引，项目新增源码目录须有四段式 AGENTS。

来源和更改文档沿原只读内容 Provider／resolver 共享模型与释放引用；更改仅合并在途变更集读取，不缓存第二份历史正文。只读输入只序列化稳定引用，原编辑器组保存视图态；旧更改投影在唯一输入工厂一次迁移，不复用临时路由或保留旧文件 Provider。

配置 URI 的 Opener 先经唯一 grammar 解析，再匹配原配置 owner 公布的 URI／source_key 并交原 JSONC 来源命令；远端 reference 仅由同一 parser 在目标 owner 上归一，不截取文件名推断来源、不交普通只读文档 Provider。发起组、选项和 owner 在异步读取前冻结，凭据或 Gateway 改变拒绝迟到打开。

所有规范 URI 的打开共用单次身份生命周期，覆盖解析、重解路由、main Thread 查询及只读模型取得；身份失效后不继续读取、不创建侧边组或打开旧输入，已取得的模型引用仍沿原 resolver 释放。不追加健康探测或自动重试。

文件、来源文档及精确 Thread 的复制引用共用 `resourceActions` 的原 Action／Command，编辑器菜单、文件菜单与 Quick Access 不另写复制逻辑。规范引用由 Gateway 唯一 URI owner 提供，包含明确 Gateway；Thread 只交付已有稳定身份，不查历史或启动目标。复制不打开资源、不读取正文、不固定版本或激活来源。

配置候选复制直接使用原来源目录已公布的 URI；已打开的来源编辑器只还原原 target／source_key 并重读该目录元数据。不从文件名生成规范地址，不读取配置正文或保存；没有 URI 的逻辑来源明确拒绝复制，仍保留原设置编辑能力。

引用与选定正文粘贴由 `composerActions` 的独立原 Action／Command 处理；来源在发起时捕获，接收者使用发起 Thread 或明确选择的可见 Thread。通过原 `ThreadPane.threadTarget` 捕获原 Composer writer，关闭、切换与身份失效明确拒绝，不维护最近 Thread 注册表、不另写草稿存储，也不伪装上传附件或启动执行。

Thread 发起的文件引用通过原 Quick Access 的只选择／多选模式取得未打开文件；先捕获草稿与工作区，再选择来源。勾选后取消不解析引用、不写草稿；接受后由唯一 URI owner 逐项解析，全部成功才一次追加原草稿。选择过程不打开编辑器、不读正文，不给混合打开列表增加附加修饰键。

只读来源／更改的损坏恢复记录沿原 TextResource serializer 保留原件及明确原因，使用原 EditorPlaceholder 和组关闭移除展示记录；没有可核验资源身份时不得发起路由或正文读取。正常引用仍交原 TextEditorService，工厂／服务故障不得误判成记录损坏，也不建立第二恢复存储。
