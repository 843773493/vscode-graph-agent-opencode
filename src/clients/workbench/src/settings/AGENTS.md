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

当前窗口的主题、布局及编辑器设置继续使用原 WorkspaceService／registry。业务设置只从正式来源 JSONC 及目标安装 schema 投影原设置树，不读旧 Extension Host profile/view，不向窗口 IConfigurationService 注册业务值。输入仅保存稳定 Gateway／工作区／来源；每组 child 和模型引用随原 Pane 关闭回收，同一来源表单与 JSON 共用原文本模型、dirty、undo、保存及备份。切换目标打开新输入，不重绑原草稿。

设置标题只投影冻结归属和原 owner 已有保存／生效状态，不为展示轮询或重复读取配置。`entities.tsx` 在同一个 Settings Pane 内托管 Provider／Agent 表单，直接编辑原来源文本模型；定义 ID 不重命名，继承定义定位原来源，Provider 数组覆盖显式确认。只读来源禁用原表单控件；实际写入始终由原 source writer 判权，秘密参数使用原受保护参数入口。

正式来源 JSONC 只显式保存；来源保存、层导入和运行生效分别展示。相对 schema 读取直接复用同一文档回执的 `json_schema`，不能把 schema 文件名当成另一个来源反复请求。查询提交不改变草稿基线；冲突显式重新预检，未知保存仅沿原键查询，不能把后续已变正文当作当前草稿已保存。

Provider 密码仅作为原 source writer 的瞬时请求输入，不进入文本模型、撤销或备份。提交键在 PUT 前持久保存，恢复只查询原路由；回执仍在 applying 且尚未写入时保留原键。原提交结果按 edit 身份、source_saved 和文件版本判断，不比较明文与脱敏正文。保存后的占位归一只由原文本文件生命周期刷新干净模型，不覆盖后续草稿。

打开来源 JSONC 的原命令同时消费明确目标／来源参数与交互选择，复用同一 owner 和编辑器投影。Quick Access 可指定原组、预览／固定、侧开或后台；异步读取前捕获选项，身份失效或原组关闭时拒绝打开，不重绑其它草稿。
