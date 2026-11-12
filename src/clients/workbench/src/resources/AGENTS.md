# 目录用途

完整工作台的来源及运行资源快速访问，使用原领域 owner 的真实发现、打开与管理能力。

# 可修改内容

来源与运行资源候选的 Quick Input 展示、具名查找／管理命令及原 Opener 接线。

# 不可修改内容

不拼装公开 URI 模板、不维护资源索引或上下文激活状态，不以普通文件或文本冒充其它资源能力。

# 规范

发现工作区根 AGENTS、本地 Skill、已配置 HTTP Skill 与所属 Gateway 发布的 Skill／插件 Skill metadata；后者沿原工作区代理及目标后端的既有认证 Gateway 连接，只投影原发布来源，不绑定来源或启动观察。本地固定一层发现沿原 ResourceNavigation，HTTP 只消费原 owner 的 accepted metadata，不抓取上游、登记来源或编译 runtime。同次 provide 只发现一次、逐键本地过滤，不保留跨打开缓存。查询固定发起 owner，结束选择器或身份失效时取消实际读取，全部 metadata 请求共用三并发限制且各分支独立报告失败。打开复用原 Opener、来源只读 Provider 和编辑器组选项，不额外 describe/read；其它类别按各自原合同继续接入。

终端／Browser 候选只读取原工作区资源清单，保持独立种类、稳定 owner 和所选 revision。管理复用同一 Action／Command 与原 Hook 临时句柄，原组件成功完整替换、失败回读；picker 的独立动作使用同一结算函数，不重放未知写入。stop、detach、delete 使用各自原端点，关联保持明确 SessionThread 与原来源／保留范围。浏览和二级取消不写入，关闭 picker 不撤销已受理动作；精确聚焦与 MCP 接入尚待完成，不以管理命令冒充完成。

配置来源沿原 ConfigurationSourceOwner 只读目录；每个选定工作区独立发现，同一已登记 Gateway 只发现一次。来源文件 URI 只消费 owner 回执；没有文件 URI 的正式逻辑来源用既有来源身份打开，不造公开地址。接受时固定目标、source_key、发起编辑器组及打开选项，统一交原设置 JSONC 命令读取、建模和保存，取消或逐键筛选不读正文、不预检或写入。

会话变更只发现本次发起 Thread 所属 Session 的 changeset 摘要，且其工作区须在明确范围内；没有明确 Session 不扫描其它会话。接受把原 changeset 身份交同一个 Thread 更改面板，Diff／完整补丁继续由原更改 owner 读取，浏览不读取预览正文、不标记审阅或改写文件。

附件只发现发起 Thread 在入选工作区内的原 catalog 元数据；显式续页按钮一次读取一页，筛选不重复查询。关闭或身份失效取消原请求，迟到续页不发布；接受冻结原附件引用并进入同一个 Thread 预览，MIME／原件／重建继续归原附件 owner，不伪造普通文本文件。
