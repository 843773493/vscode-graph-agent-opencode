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
