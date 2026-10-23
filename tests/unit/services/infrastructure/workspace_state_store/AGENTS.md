# 目录用途

本目录按工作区状态库的职责族组织单元测试，避免把配置来源、配置 apply、配置事件
和宿主组合行为堆在一个大型测试文件中。生产实现仍由
`app/services/infrastructure/workspace_state_store.py` 及其专属职责模块提供。

```text
workspace_state_store/
├── test_workspace_state_store.py       # 宿主组合、工作区路径与活动游标边界
├── test_workspace_config_source.py     # 来源层、journal、fan-out 与旧库迁移
├── test_workspace_config_apply.py      # 配置 apply 账本与补偿
└── test_workspace_config_events.py     # 配置事件、游标与 outbox 投递
```

# 可修改内容

- 可以维护上述四个测试模块，按其对应职责族扩展行为覆盖。
- 可以添加仅服务于本目录测试的 fixture；跨目录复用的 fixture 应放在既有测试支持层，
  并由 pytest fixture 注入。
- 可以调整本目录结构，但新增子目录必须有自己的 `AGENTS.md`，且文件职责应继续与生产
  职责族对应。

# 不可修改内容

- 不得在测试中复制或重写工作区状态库的生产逻辑，也不得为满足测试而放宽状态、顺序、
  冲突或错误归属断言。
- 不得把本目录用作其它基础设施模块的杂项测试区；跨模块场景放在对应测试职责目录。
- 不得直接修改 `tests/fixtures/workspaces/` 中的模板，测试工作区须由 fixture 复制并注入。

# 规范

- 每个文件只验证其名称和目录树标注的职责族；状态库组合测试保持最小。
- 测试统一使用 pytest fixture 注入依赖，不在模块级创建共享状态库或工作区。
- 测试命令使用 `uv run pytest`，并遵守仓库进程外超时与内存保护要求。
- 修改配置热重载行为测试时，同步检查 `tests/unit/gateway/test_config_hot_reload_acceptance_matrix.py`
  是否应指向本目录的权威测试入口。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
