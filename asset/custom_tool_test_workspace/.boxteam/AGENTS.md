# 目录用途

`asset/custom_tool_test_workspace/.boxteam/` 存放扩展工具 e2e 测试所需的工作区本地配置与测试 skill。

# 可修改内容

- 可以维护测试用 `boxteam.json`，配置测试 agent 的自定义扩展工具 factory。
- 可以维护 `skills/` 下的测试 skill 目录，用于说明模型如何通过固定入口调用扩展工具。

# 不可修改内容

- 不放测试运行时生成的 checkpoint、日志、sessions、缓存或真实用户数据。
- 不放应用源码、前端组件或后端业务实现。

# 规范

- 配置文件只保留 e2e 必需的最小配置。
- 扩展工具调用参数应放在对应 `skills/*/SKILL.md` 中，不在本层重复展开。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
