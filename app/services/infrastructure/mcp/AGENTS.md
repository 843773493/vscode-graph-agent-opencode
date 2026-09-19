# 目录用途

`app/services/infrastructure/mcp/` 负责读取 MCP Server 配置、建立 MCP Client 连接，维护唯一的工具目录 owner `McpCatalogOwner`（完整 relist、不可变 catalog revision、`tools/list_changed` 通知消费、连接 generation lease、`mcp.catalog/*` typed 轻量事件），并将目录中的工具适配为 Agent 可使用的 LangChain 工具。

# 可修改内容

- MCP stdio 与 Streamable HTTP Client 配置解析。
- MCP Server 连接生命周期、工具目录 relist/通知消费/generation lease 与调用、状态查询。
- `mcp.catalog/*` 目录事件合同的发布接线。
- MCP Tool 名称映射和 LangChain 适配。
- 扩展目录 binding（ExtensionCatalogBindingRef）与目录 generation lease 的维护。
- MCP 工具指引派生（McpToolGuidanceProducer）与激活边界 binding/指引原子冻结（McpCatalogActivationBinder）。

# 不可修改内容

- 不实现 Agent 编排、API 路由或前端展示。
- 不在代码中硬编码 MCP Server 命令、URL、凭据或工作区路径。
- 不把 MCP Server 运行状态写入 Gateway 全局目录。
- 目录事件只携带 identity/revision，不得携带工具 schema 正文/credential，不得进入 job.events，也不得注册 ResourceRegistry source 或 CSM 来源。
- 不得绕过 `McpCatalogOwner` 建立第二套工具发现/刷新路径。
- sealed binding ref 一经返回不可变；目录增删改/权限变化不得改写已封存 ref，旧 tool call 只按 sealed ref 解析。

# 规范

- MCP Server 配置错误、连接失败和协议错误必须直接抛出详细异常。
- stdio Server 必须使用命令与参数数组启动，不得通过 shell 字符串执行。
- MCP 工具名称必须带 Server 命名空间，禁止同名工具静默覆盖。
- 目录 relist 必须完整读取并验证；连续 relist payload 不变时不得推进 revision；增删改发布新 immutable revision，删除以 tombstone 事件标记；目录为空也发布固定 envelope/revision。
- 目录 generation 只在 revision 实际推进时递增；binding ref 的 binding_id/binding_hash 必须可重算核对。
- 指引只从已验证目录派生并保持有界确定性；外部 description 是不可信数据；binding 与指引必须同 revision 原子冻结，relist/派生失败 fail closed，不半发布。
- 不具备 `tools/list_changed` 通知能力的 server 只在显式激活边界 relist，不得谎称立即生效。
- 凭据只允许通过环境变量引用解析，不得写入日志或模型工具描述。
