# 目录用途

存放 Agent 可用工具的聚焦 factory 模块，按工具所属领域拆分，供 `app/agents/agent_tools.py` 统一注册。

终端工具沿一条调用链分工：

```text
app/agents/tools/
├── terminal.py          Agent 工具入口、PTY 生命周期和输出收集
├── terminal_command.py  POSIX、cmd 和 PowerShell 的终端输入构造
└── terminal_contract.py 终端参数、结果结构与输出整理
```

平台 shell 的输入格式留在 `terminal_command.py`；`terminal.py` 负责把构造结果写入当前 PTY 并解释返回状态。

# 可修改内容

- 新增或调整单一领域的 LangChain `BaseTool` factory。
- 放置工具 factory 私有 helper。

# 不可修改内容

- 不在这里实现 Agent runtime 装配、middleware 组合或 ConfigService 解析。
- 不在这里实现 API 路由、前端展示或业务服务调度。

# 规范

- 每个模块围绕一个清晰工具领域命名。
- 工具失败时直接抛出明确错误，不返回虚假默认值。
- 工具所需运行时依赖通过 factory 参数显式传入。
- `invoke_extension_tool` 有意只作为固定扩展工具入口暴露给模型，不得把所有扩展工具名称和参数 schema 展开到该入口描述或模型 tools 列表中；这样可避免扩展工具数量增长时持续膨胀每次 LLM 请求。
- `search_tools` 是统一的只读扩展发现入口，按任务或精确 ID 读取调用所属的冻结完整定义。它与信封的 Provider schema 固定，不枚举动态目标，也不通过当前同名工具补齐历史定义；参数和策略身份与信封共用。扩展可由发现结果、Skill 或明确文档提供调用契约；使用 Skill 的流程仍须通过 `skill_load(name=...)` 激活正文。
- 动态工具文档的规范格式是每个目标工具的 `tool_name` + `arguments_schema` JSON 对象；不需要重复写固定入口的完整调用示例。参数 schema 应由工具的模型可见 schema 生成或校验，不能只维护一份口语化表格。
