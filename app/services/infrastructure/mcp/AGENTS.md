# 目录用途

`app/services/infrastructure/mcp/` 负责读取 MCP Server 配置、建立 MCP Client 连接、发现远端工具，并将其适配为 Agent 可使用的 LangChain 工具。

# 可修改内容

- MCP stdio 与 Streamable HTTP Client 配置解析。
- MCP Server 生命周期、工具发现、调用与状态查询。
- MCP Tool 名称映射和 LangChain 适配。

# 不可修改内容

- 不实现 Agent 编排、API 路由或前端展示。
- 不在代码中硬编码 MCP Server 命令、URL、凭据或工作区路径。
- 不把 MCP Server 运行状态写入 Gateway 全局目录。

# 规范

- MCP Server 配置错误、连接失败和协议错误必须直接抛出详细异常。
- stdio Server 必须使用命令与参数数组启动，不得通过 shell 字符串执行。
- MCP 工具名称必须带 Server 命名空间，禁止同名工具静默覆盖。
- 凭据只允许通过环境变量引用解析，不得写入日志或模型工具描述。
