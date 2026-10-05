# 目录用途

`tests/fixtures/workspaces/custom_tool_test_workspace/.boxteam/` 存放扩展工具测试所需的测试 skill；canonical 会话由测试准备器写入隔离工作区。

# 可修改内容

- 可以维护 `skills/` 下的测试 skill 目录，用于说明模型如何通过固定入口调用扩展工具。

# 不可修改内容

- 不放 workspace identity、session catalog、sessions、checkpoint、日志、缓存或真实用户数据；测试运行期间生成的会话数据必须写入 `out/tests/`。
- 不放应用源码、前端组件或后端业务实现。

# 规范

- 运行配置统一从 `configs/tests/` 复制到隔离工作区，不在 asset 模板中维护第二份配置。
- 测试准备器在隔离工作区使用 canonical catalog 和 rollout API 生成会话，不得将运行时会话快照写回模板。
- 扩展工具调用参数应放在对应 `skills/*/SKILL.md` 中，不在本层重复展开。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
