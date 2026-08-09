# 目录用途

本目录存放浏览器扩展工具组的产品级 Skill。

# 可修改内容

- 可以维护浏览器工具调用顺序、参数 schema、页面锁和对话框处理说明。

# 不可修改内容

- 不放浏览器 manager 状态、截图、下载文件或用户页面数据。
- 不混入 Gateway Context、Web 搜索或测试工具说明。

# 规范

- 工具调用必须通过固定入口 `invoke_custom_tool`。
- `SKILL.md` 中的工具名称必须与 Workspace 工具注册表保持一致。
- 每个工具使用 `tool_name` + `arguments_schema` 描述参数；不要添加冗余的完整调用示例。
- 参数 schema 以 Workspace 工具的模型可见 schema 为准，流程顺序、页面 revision 和锁等跨字段规则用简短自然语言补充。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
