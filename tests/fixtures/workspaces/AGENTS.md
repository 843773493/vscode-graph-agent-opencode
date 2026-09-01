# 目录用途

`tests/fixtures/workspaces/` 存放可复制生成正式测试工作区的完整静态 fixture。

# 可修改内容

- 可以维护测试所需的工作区输入文件、静态会话、rollout JSONL、SQLite 索引和导航索引。
- 可以新增经过测试验证的确定性边界场景 fixture。

# 不可修改内容

- 不放测试运行时生成的日志、checkpoint、截图、缓存或真实用户数据。
- 不在这里实现产品源码；运行时工作区必须复制到 `out/tests/` 后再修改。

# 规范

- 测试通过 `tests/support/workspaces.py` 从本目录复制工作区。
- 静态会话必须同时维护 `session.json`、`rollout/rollout.jsonl`、`rollout/index.sqlite` 和权威导航索引。
- fixture 内容必须确定性、可复查，并与对应测试场景保持明确命名。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
