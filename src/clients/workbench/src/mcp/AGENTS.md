# 目录用途

将项目 Workspace MCP owner 的完整管理合同接入原 Workbench MCP 服务、对象及命令。

# 可修改内容

- 后端目录与操作镜像、精确工作区请求、原列表／详情对象和 OAuth 窗口桥接。

# 不可修改内容

- 不创建 MCP client、安装记录、凭据库或 enabled/status 权威；Agent 执行继续归 McpCatalogOwner。
- 不把显式动态命令当作制品安装，不实现延期资源持有／释放。

# 规范

- 所有请求冻结 Gateway／workspace；目录完整替换，失败重新读取，迟到结果不能覆盖新操作。
- 原 Workbench 服务仅消费同一组对象；未公布的安装、配置写入及宿主能力明确拒绝。
- code/state 只由原同源回调窗口交给精确操作，不写入 URL、存储或模型上下文。
