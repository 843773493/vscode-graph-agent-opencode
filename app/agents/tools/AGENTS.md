# 目录用途

存放 Agent 可用工具的聚焦 factory 模块，按工具所属领域拆分，供 `app/agents/agent_tools.py` 统一注册。

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
- `invoke_custom_tool` 有意只作为固定扩展工具入口暴露给模型，不得把所有扩展工具名称和参数 schema 展开到该入口描述或模型 tools 列表中；这样可避免扩展工具数量增长时持续膨胀每次 LLM 请求。
- 扩展工具只有在模型先从当前工作区的 `AGENTS.md`、`SKILL.md` 或普通说明文档读到目标工具名称与参数后才应调用；新增扩展工具必须同时提供或更新对应文档，并用 E2E 覆盖“先读文档、再通过 `invoke_custom_tool` 调用”的行为。
- 动态工具文档的规范格式是每个目标工具的 `tool_name` + `arguments_schema` JSON 对象；不需要重复写固定入口的完整调用示例。参数 schema 应由工具的模型可见 schema 生成或校验，不能只维护一份口语化表格。
