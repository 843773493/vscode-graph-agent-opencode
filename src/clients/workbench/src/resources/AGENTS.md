# 目录用途

完整工作台的来源及运行资源快速访问，使用原领域 owner 的真实发现、打开与管理能力。

# 可修改内容

来源与运行资源候选的 Quick Input 展示、具名查找／管理命令及原 Opener 接线。

# 不可修改内容

不拼装公开 URI 模板、不维护资源索引或上下文激活状态，不以普通文件或文本冒充其它资源能力。

# 规范

AGENTS／Skill 候选的复制按钮直接交原 `resourceActions` 命令，保留来源 Gateway 与工作区，不预先打开资源或读取正文；按钮接受后保留原选择器及输入焦点。

发现工作区根 AGENTS、本地 Skill、已配置 HTTP Skill 与所属 Gateway 发布的 Skill／插件 Skill metadata；后者沿原工作区代理及目标后端的既有认证 Gateway 连接，只投影原发布来源，不绑定来源或启动观察。本地固定一层发现沿原 ResourceNavigation，HTTP 只消费原 owner 的 accepted metadata，不抓取上游、登记来源或编译 runtime。同次 provide 只发现一次、逐键本地过滤，不保留跨打开缓存。查询固定发起 owner，结束选择器或身份失效时取消实际读取，全部 metadata 请求共用三并发限制且各分支独立报告失败。打开复用原 Opener、来源只读 Provider 和编辑器组选项，不额外 describe/read；其它类别按各自原合同继续接入。

终端／Browser 候选只读取原工作区资源清单，保持独立种类、稳定 owner 和所选 revision。管理复用同一 Action／Command 与原 Hook 临时句柄，原组件成功完整替换、失败回读；picker 的独立动作使用同一结算函数，不重放未知写入。stop、detach、delete 使用各自原端点，关联保持明确 SessionThread 与原来源／保留范围。Session 元数据投影携原 ID／revision 进入该命令，完整读取后不悄悄改用新版；原 Session loader 继续负责权威刷新。浏览和二级取消不写入，关闭 picker 不撤销已受理动作；聚焦沿原工作区切换、面板恢复及实例选择，选择 Browser 不自动 attach，MCP 与跨平台验收按各自任务继续。

配置来源沿原 ConfigurationSourceOwner 只读目录；每个选定工作区独立发现，同一已登记 Gateway 只发现一次。来源文件 URI 只消费 owner 回执；没有文件 URI 的正式逻辑来源用既有来源身份打开，不造公开地址。接受时固定目标、source_key、发起编辑器组及打开选项，统一交原设置 JSONC 命令读取、建模和保存，取消或逐键筛选不读正文、不预检或写入。

只有公布 URI 的配置候选显示独立复制按钮，直接复用同一资源 Command；不重新发现目录、不打开配置或读取正文，保留输入焦点和选择器。

可引用来源与配置候选的独立附加按钮同样交原 Composer Command，保留发起接收者。来源多选只向原调用方返回 URI 与 owner，不执行打开或行内复制／附加；无 URI、不可用及诊断行禁用勾选，续页仍沿原按钮。范围在进入只选择流程前明确，发现不查询终端／Browser／MCP／工具结果／变更，不为引用另建资源目录或正文缓存。

会话变更只发现本次发起 Thread 所属 Session 的 changeset 摘要，且其工作区须在明确范围内；没有明确 Session 不扫描其它会话。接受把原 changeset 身份交同一个 Thread 更改面板，Diff／完整补丁继续由原更改 owner 读取，浏览不读取预览正文、不标记审阅或改写文件。

附件只发现发起 Thread 在入选工作区内的原 catalog 元数据；显式续页按钮一次读取一页，筛选不重复查询。关闭或身份失效取消原请求，迟到续页不发布；接受冻结原附件引用并进入同一个 Thread 预览，MIME／原件／重建继续归原附件 owner，不伪造普通文本文件。

可用附件的复制按钮将原规范 file_id 和冻结 owner 交同一资源复制命令及 URI parser；不提前打开预览或读取原件，复制后保留选择器与焦点。不可用来源不展示复制动作。

附件引用按钮复用原 Composer 命令，只追加规范 URI。provide 在 Quick Input 夺焦前捕获原 ThreadPane 和原草稿 owner，范围选择往返继续持有同一临时句柄；明确没有发起者时才使用原可见 Thread 选择器，后来获得焦点的 Thread 不成为默认接收者。关闭、未就绪或凭据变化均明确失败，不上传附件、不读原件、不发送消息。

工具结果候选仅发现发起 Thread 的原历史元数据，续页携原 view 与 revision；接受复用原 around／ToolRow 的 typed 定位，浏览不读正文或重放工具，不伪造资源地址。
