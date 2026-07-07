# 目录用途

`.boxteam/skills/test-tool-2/` 存放 `test_tool_2` 扩展工具组的测试 skill。

# 可修改内容

- 可以维护 `test_tool_2` 的 skill 元数据、调用入口说明和返回值验证要求。

# 不可修改内容

- 不放其它扩展工具组的说明。
- 不放运行时日志、checkpoint 或缓存。

# 规范

- skill 必须说明目标扩展工具通过固定入口 `invoke_custom_tool` 调用。
- skill 中的调用参数应保持最小且可被模型直接用于真实工具调用。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
