# 目录用途

将 Gateway／Backend 的插件服务与 Workspace MCP 连接目录接入原 Workbench MCP 服务、对象及命令。

# 可修改内容

- Gateway 目标选择、精确工作区配置来源、插件服务引用、HTTP 私有 Header、原连接目录／操作镜像、原列表／详情对象和 OAuth 窗口桥接。

# 不可修改内容

- 不创建 MCP client、安装记录、凭据库或 enabled/status 权威；Agent 执行继续归 McpCatalogOwner。
- 不创建 MCP client、安装库存、第三方 Gallery／Registry、stdio 安装链或动态 VSIX MCP Provider。

# 规范

- MCP 管理视图独立选择 Gateway，初始为页面原生 Gateway；普通工作区焦点不改变该选择。新增配置只从该 Gateway 选择就绪工作区，并写入插件服务身份或外部 Streamable HTTP。
- 所有请求冻结原 Gateway／工作区／connection；工作区运行和配置继续携精确 workspace，目录完整替换，失败重新读取，迟到结果不能覆盖新操作。授权深链只打开原工作区对象，不重写管理视图选择。
- 插件 MCP 仅引用声明服务的稳定身份；连接目标实例及执行代际由原 Gateway／Backend owner 解析。外部 HTTP 与 OAuth 继续由原工作区 owner 管理。
- code/state 只由原同源回调窗口交给精确操作，不写入 URL、存储或模型上下文。
- 普通 HTTP Header 写入原私有凭据 owner，来源文件经原 preflight／CAS 保存；结果未知以原幂等键查询，不自动重放。
- 原身份失效或 Gateway 改变时中止同一请求信号，包括401恢复后的重试；配置拒绝后的回读失败须保留原错误及 request_id。
