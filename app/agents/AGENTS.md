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
- 产品级 Skill 源码统一位于 `resources/skills/`；DeepAgents 后端内部可以通过受信虚拟路由解析 Skill，但面向 Agent 只展示 Skill 的 `name`/`description` 和 `skill_load(name=...)`，不得展示内部路径。
- 普通文件目标按用户裁定接受相对路径、目标机器原生绝对路径与资源 URI；三者经同一目标解析后执行，工作区外目标按实际 owner 能力访问或明确拒绝，禁止去除根或盘符后改投工作区。`.` 表示显式目标工作区根；DeepAgents 的 `/` 虚拟路径只存在于内部适配层，补丁内文件名仍只允许相对固定根。源码调试继续遵守其工作区能力边界。
- 失败时抛出明确错误，不要返回虚假的默认值。
