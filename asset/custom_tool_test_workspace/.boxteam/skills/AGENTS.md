# 目录用途

`.boxteam/skills/` 存放测试工作区中的扩展工具说明 skill。产品级 skill 来自项目根目录 `resources/skills/`，由 E2E 工作区准备器复制；本目录只维护测试专用 skill。

# 可修改内容

- 可以新增或维护用于 e2e 的测试专用 `SKILL.md`。
- 可以调整 skill 描述，让模型能根据用户提到的扩展工具名选择正确 skill。

# 不可修改内容

- 不放测试运行时产生的 checkpoint、日志或会话数据。
- 不放真实用户工作区数据。

# 规范

- 具体测试扩展工具调用参数必须在对应的 `SKILL.md` 中使用 `tool_name` + `arguments_schema` JSON 对象描述；产品级 skill 应修改 `resources/skills/` 中的源码。
- `AGENTS.md` 只做目录说明，不替代具体 skill 指令。
- 测试专用 Skill 不重复写完整的 `invoke_custom_tool` 调用示例；固定入口 schema 由模型工具定义提供。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
