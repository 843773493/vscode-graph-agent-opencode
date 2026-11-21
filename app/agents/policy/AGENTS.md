# 目录用途

存放 Agent 运行时使用的纯策略计算，例如工具可用性解析和工具依赖约束。

## 职责树

按工具声明到运行时能力的方向查找；本目录只计算策略，不执行工具。

```text
app/agents/policy/
├── custom_tool_spec.py  校验和归一化内置/扩展工具声明
├── tool_groups.py       工具分组映射；自定义声明的模块分组由 catalog_group_for_custom_tool 统一推导
├── tool_policy.py       解析工具全集、allow/deny selector 与跨工具依赖
├── tool_capability.py   工具策略身份与能力合并；metadata_for_custom_tool 处理自定义声明，metadata_for_tool 处理注册工具定义
└── __init__.py          导出策略目录的公共类型与函数
```

# 可修改内容

- 无 I/O 的 Agent 策略数据结构、集合运算和校验函数。
- 可由配置、业务服务和运行时共同复用的权威策略 API。

# 不可修改内容

- 不在这里读取配置文件、构建 LangChain 工具或执行工具。
- 不在这里实现 API、会话业务或前端展示。

# 规范

- 策略函数必须保持纯粹、确定且可单独测试。
- `metadata_for_tool` 是注册工具策略身份的统一推导入口，接收 `tool_identity.py` 的 `ToolDefinitionLike`；工厂不再自行组合名称、kind 和 group。自定义配置声明仍由 `metadata_for_custom_tool` 保留工厂模块分组，MCP 保留 Server 归属。
- 无效策略必须抛出包含冲突工具名称的明确错误。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
