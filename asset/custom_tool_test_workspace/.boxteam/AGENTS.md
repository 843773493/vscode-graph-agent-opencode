# 目录用途

`asset/custom_tool_test_workspace/.boxteam/` 存放扩展工具 e2e 测试所需的测试 skill。

# 可修改内容

- 可以维护 `skills/` 下的测试 skill 目录，用于说明模型如何通过固定入口调用扩展工具。

# 不可修改内容

- 不放测试运行时生成的 checkpoint、日志、缓存或真实用户数据。
- `sessions/` 只允许存放仓库提交的静态 rollout fixture，包括明确标注的 mock 和预生成真实模型快照；运行时生成的数据必须写入 `out/tests/`。
- 不放应用源码、前端组件或后端业务实现。

# 规范

- 运行配置统一从 `configs/tests/` 复制到隔离工作区，不在 asset 模板中维护第二份配置。
- 静态 rollout fixture 必须使用当前 `rollout/rollout.jsonl`、`rollout/index.sqlite` 和会话目录索引格式，不得混入旧的 `segment-*.jsonl`、`checkpoints/` 或 payload 文件。
- 扩展工具调用参数应放在对应 `skills/*/SKILL.md` 中，不在本层重复展开。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
