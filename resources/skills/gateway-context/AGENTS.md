# 目录用途

本目录存放 Gateway Context 扩展工具组的产品级 Skill。

# 可修改内容

- 可以维护 `read_context`、`search_context` 的资源地址、分页和一致性说明。

# 不可修改内容

- 不放 Gateway 注册表、会话记录或运行时日志。
- 不混入浏览器、Web 搜索或测试工具说明。

# 规范

- Skill 必须说明目标扩展工具通过固定入口 `invoke_custom_tool` 调用。
- 工具参数使用 `tool_name` + `arguments_schema` 描述，并保持最小且可被模型直接用于真实工具调用。
- 不添加冗余的完整 `invoke_custom_tool` 调用示例。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
