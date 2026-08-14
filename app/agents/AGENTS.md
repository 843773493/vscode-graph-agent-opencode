# 目录用途

`app/agents/` 存放 Agent 运行时装配、工具接入、中间件适配和与 LangGraph/DeepAgents 直接相关的运行时边界代码。

# 可修改内容

- 可以新增或调整 Agent runtime、middleware、provider 和 checkpoint 适配器。
- 可以封装第三方 Agent 框架的私有结构访问，向业务层暴露清晰语义接口。
- 可以维护 Agent 工具注册与运行时配置解析。

# 不可修改内容

- 不要把 API 路由、业务服务编排或用户会话业务规则放在这里。
- 不要在这里硬编码环境变量值或用户 workspace 路径。
- 不要在这里实现前端展示、SSE 推送或数据库式持久化业务。

# 规范

- 与 LangGraph/DeepAgents 私有字段或私有方法交互时，应集中在适配器内，避免扩散到 business service。
- 运行时配置应通过 `ConfigService` 或显式参数传入。
- 产品级 Skill 源码统一位于 `resources/skills/`；DeepAgents 后端内部通过 `/.boxteam/bundled-skills/` 和 `/.boxteam/skills` 只读路由加载 Skill，面向 Agent 的提示必须分别展示为 `.boxteam/bundled-skills/` 和 `.boxteam/skills`。
- Agent 文件工具和面向模型的源码调试路径统一使用标准工作区相对路径，`.` 表示当前 workspace 根目录；DeepAgents 的 `/` 虚拟路径只允许存在于内部适配层。不得在同一个模型路径字段中重载宿主机绝对路径语义；确需宿主机路径的能力必须使用独立字段或独立工具。
- 失败时抛出明确错误，不要返回虚假的默认值。
