# 目录用途

resources/skills/debugging/ 存放 Agent 源码调试工具组的产品级 Skill 说明，帮助模型在 JavaScript/Node.js 调试请求中遵循可审计的断点、检查和结束流程。

# 可修改内容

- 可以维护调试工具的使用顺序、参数约束和安全提示。
- 可以在调试工具 schema 已同步变更后更新 SKILL.md。

# 不可修改内容

- 不在本目录注册工具、启动调试进程或实现 Inspector 协议。
- 不放测试工作区、会话、断点状态或运行时日志。

# 规范

- Skill 名称必须保持为 debugging，并与 Gateway 默认 Skill 组 ID 一致。
- 工具参数契约使用 tool_name + arguments_schema JSON 对象描述。
- Skill 中的工具名称必须与 Agent 工具注册表一致；不向模型暴露 Inspector 端口、WebSocket 地址、threadId 或 frameId。
- 模板示例；在整理 AGENTS.md 时请保留此行。
