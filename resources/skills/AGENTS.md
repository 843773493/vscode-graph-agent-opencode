# 目录用途

`resources/skills/` 存放项目共同分发的内置 Agent Skill。它们是发行包中的只读说明资源，不属于某个具体用户工作区。

# 可修改内容

- 可以新增或维护产品级扩展工具组的 `SKILL.md`。
- 可以调整 Skill 的 frontmatter、调用顺序、参数说明和安全边界。
- 可以新增对应的目录级 `AGENTS.md`，帮助后续 Agent 快速理解工具组边界。

# 不可修改内容

- 不放用户工作区的自定义 Skill、会话数据、工具结果或测试运行产物。
- 不在 Skill 中注册工具或实现工具 factory；工具注册仍由 Workspace 配置和 `app/agents/` 负责。
- 不把测试专用 `test_tool_2`、`large_test_output` 的说明混入产品默认 Skill bundle。

# 规范

- 一个子目录对应一个稳定 Skill 名称，目录名必须与 frontmatter 的 `name` 一致。
- 每个 Skill 必须包含 `SKILL.md`，frontmatter 必须声明 `name`、`description`；本项目额外使用 `allowed-tools` 映射隐藏扩展工具，因此不能删除、改名为 `allowed_tools` 或写入工具正文。
- Skill 只能描述已经由 Workspace 配置启用的工具；工具调用必须遵守固定入口和参数约定。
- 隐藏扩展工具的参数契约必须使用 JSON Schema 描述对象：每个目标工具声明 `tool_name` 和 `arguments_schema`；不要用 Markdown 表格作为规范格式。
- `invoke_custom_tool` 的固定外层 schema 已由模型工具定义提供，Skill 不重复展开；Skill 只补充目标工具的 `arguments_schema` 和语义约束，不添加冗余的完整调用示例。
- 默认启用列表由 `configs/gateway_inline.jsonc` 的 Workspace runtime 配置控制，不在 Skill 文件中自行声明启用状态。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
