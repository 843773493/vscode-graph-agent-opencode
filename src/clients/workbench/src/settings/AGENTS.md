# 目录用途

通过原 SettingsEditor／Command／Quick Input 编辑当前窗口配置及明确目标的正式业务来源。

# 可修改内容

原设置输入的稳定目标、按编辑器组派生的来源 schema 投影和文本引用生命周期；默认 Agent 来源的选择、JSONC 单键编辑、来源预检和提交状态展示。`pluginConfiguration.ts` 持有自有插件唯一参数／绑定来源编辑流程，包详情与独立设置命令共用原预检、CAS 和私有输入链。`sourceOwner.ts` 统一表单和来源 JSONC 的原 owner 传输；`sourceEditor.ts` 只适配原 FileService／TextFileService，使用内部编辑器身份、原脏状态和备份中的 CAS。未结清的原提交键与连接路由沿 Workbench 本机存储保留，不直接写来源文件。

# 不可修改内容

不维护另一份配置真相，不将业务配置注册到 Code OSS 配置服务，不复制 HTTP 客户端或绕过来源 writer，不代替 Backend 重启与健康证明。

# 规范

资源 URI 打开同样经原 ConfigurationSourceOwner 目录取得正式 source_key，复用同一个来源命令、JSONC 模型和 CAS writer；没有唯一目录匹配时明确拒绝，不维护 URI 到来源的第二张映射表。

配置编辑器复制引用复用原内部 URI 解析与来源目录元数据，只复制 owner 已公布的规范 URI；没有 URI 的逻辑来源明确报告能力边界，不从 source_key 拼地址或读取正文。

冻结 Gateway／工作区／来源；只读来源仍可查看。复用 web API owner 和原 JSONC AST 编辑器，完整保留 CAS 与幂等身份；冲突保留输入，显式重新预检，未知结果仅查询原提交。使用中文提示与注释。

当前窗口的主题、布局及编辑器设置继续使用原 WorkspaceService／registry。业务设置只从正式来源 JSONC 及目标安装 schema 投影原设置树，不读旧 Extension Host profile/view，不向窗口 IConfigurationService 注册业务值。输入仅保存稳定 Gateway／工作区／来源；每组 child 和模型引用随原 Pane 关闭回收，同一来源表单与 JSON 共用原文本模型、dirty、undo、保存及备份。分类、目标、范围和来源选择均在当前设置 Pane 内导航，不创建新的设置 Tab；每个 Pane 独立持有来源上下文，来源模型按稳定身份保留，切换不改写原草稿。

配置统一从原生“设置”进入。`categories.ts` 声明功能分类及允许的真实配置范围，`navigation.ts` 贡献到原 TOC，目标／范围／工作区选择复用搜索下方的统一范围栏；窄布局用同一原生 SelectBox 访问分类。普通用户／工作台标签仅在外观与编辑器功能内保留，不能再与 Gateway／后端默认／业务工作区混排。Gateway 配置持续独立生效，只有明确支持的后端默认值与工作区设置存在覆盖关系；选择配置对象不改变导航激活或会话。返回功能沿当前 Pane 已访问的来源模型恢复范围，不增设业务配置持久缓存。分类选择不自动展开／折叠其它节点；TOC 跨来源只保留分组名称和结构，不持有旧字段模型，子组接受时重新定位当前来源分组。公共范围栏不因分类切换消失，来源与保存详情折叠显示，目标身份冻结、原模型的 dirty／undo／保存仍沿原链路。`entities.tsx` 在同一个 Settings Pane 内托管 Provider／Agent 表单，常用操作使用原生 ToolBar，次要操作进入其溢出菜单；React 只渲染列表与字段，不重复创建工具栏。来源选择限制在内容宽度内，链接使用原生 Link，来源折叠改变高度时须触发原设置页布局。实体列表采用主题行样式，窄布局依据组件容器宽度选择布局，直接编辑原来源文本模型；定义 ID 不重命名，继承定义定位原来源，Provider 数组覆盖显式确认。只读来源禁用原表单控件；实际写入始终由原 source writer 判权，秘密参数使用原受保护参数入口。

正式来源 JSONC 只显式保存；来源保存、层导入和运行生效分别展示。相对 schema 读取直接复用同一文档回执的 `json_schema`，不能把 schema 文件名当成另一个来源反复请求。查询提交不改变草稿基线；冲突显式重新预检，未知保存仅沿原键查询，不能把后续已变正文当作当前草稿已保存。

Provider 新输入的密码只发送到冻结目标的命名密钥库，不进入 source writer、文本模型、撤销或备份；配置只保存 secret: 引用或显式环境变量引用。原生设置的密钥管理入口复用 Quick Input 与 Dialog，改名保持 ID，替换／删除核对修订号，不扫描工作区引用或自动同步远端。来源提交键在 PUT 前持久保存，恢复只查询原路由；回执仍在 applying 且尚未写入时保留原键。原提交结果按 edit 身份、source_saved 和文件版本判断。保存后的占位归一只由原文本文件生命周期刷新干净模型，不覆盖后续草稿。

新会话默认值只在用户打开原 Quick Input 时读取 active Agent 目录，复用原 SessionDefaults API；明确固定 Gateway／工作区及连接路由。它与来源的未保存定义和 `default_agent` 草稿分开，成功消费完整回执，失败只重读原 owner，不重投、不修改既有 Session，不建立另一份默认值缓存或轮询。

打开来源 JSONC 的原命令同时消费明确目标／来源参数与交互选择，复用同一 owner 和编辑器投影。Quick Access 可指定原组、预览／固定、侧开或后台；异步读取前捕获选项，身份失效或原组关闭时拒绝打开，不重绑其它草稿。

`management.tsx` 将连接、设备访问、受管服务、诊断与背景资源接入同一原 Settings Pane，主操作沿原 ToolBar，次要操作进入溢出菜单；来源字段仍由原设置树呈现。`gatewayCommands.ts` 是状态栏／账户菜单／Welcome／Quick Access 的统一管理入口，默认本地且不激活工作区；远程状态栏先打开原 Quick Pick，不弹独立控制中心。
