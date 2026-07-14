# 目录用途

`.boxteam/skills/web-search-fetch/` 存放 Web 搜索和网页正文抓取扩展工具的测试 skill。

# 可修改内容

- 可以维护 `SKILL.md` 中的搜索、抓取工具调用说明、参数 schema 和结果约定。

# 不可修改内容

- 不放测试运行时产生的 checkpoint、日志或会话数据。
- 不混入浏览器交互或工作区文件工具说明。

# 规范

- 工具调用必须通过固定入口 `invoke_custom_tool`。
- 搜索结果需要完整正文时，应继续调用 `fetch_webpage`，不能把搜索摘要当作页面原文。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
