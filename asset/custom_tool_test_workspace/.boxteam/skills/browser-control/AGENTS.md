# 目录用途

`.boxteam/skills/browser-control/` 存放可附加浏览器扩展工具的测试 skill。

# 可修改内容

- 可以维护 `SKILL.md` 中的浏览器工具调用说明、参数 schema 和示例。

# 不可修改内容

- 不放测试运行时产生的 checkpoint、日志或会话数据。
- 不放真实用户工作区数据。

# 规范

- 工具调用必须通过固定入口 `invoke_custom_tool`。
- 本目录只描述浏览器扩展工具，不混入其它扩展工具说明。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
