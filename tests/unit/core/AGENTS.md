# tests/unit/core

## 目录用途

存放后端核心基础设施的单元测试，例如事件总线、任务上下文等。

## 按职责查找

下面是职责索引，不代表当前磁盘子目录；现有测试多数仍位于本目录根部。先按行为归属查找，再决定新测试是否应下沉到对应模块目录：

```text
tests/unit/core/
├── 身份与标识
│   ├── test_identifier*.py、test_canonical_identifier_matrix.py
│   ├── test_distribution_identity.py、test_user_identity.py、test_workspace_identity.py
│   ├── test_non_canonical_exemptions.py、test_hashing.py
│   └── test_canonical_creation_id_allocation.py
├── Session 目录与生命周期
│   ├── catalog_workspace_helper.py、test_catalog_workspace_helper.py
│   ├── test_session_catalog*.py
│   ├── test_session_creation*.py、test_thread_creation.py
│   ├── test_session_subtree_delete.py、test_fork_retention_release.py
│   ├── test_session_control*.py、test_session_lifecycle_gate.py
│   └── test_session_interrupt_state.py、test_session_thread_context.py
├── 异步执行与事件
│   ├── test_background_message_bus.py、test_job_event_bus*.py
│   ├── test_execution_admission_context.py、test_cancelable_stream.py
│   ├── test_lifetime_*.py、test_turn_execution_scope.py
│   └── test_key_lock_pool.py、test_checkpoint_config.py
├── 文件、存储与环境
│   ├── test_atomic_fs.py、test_sql_assets.py、test_sqlite_state.py
│   ├── test_path_utils*.py、test_mounted_workspaces.py
│   ├── test_boxteam_env.py、test_user_storage_migration.py
│   └── test_bounded_json.py、test_backend_activation.py
└── 配置、追踪与领域合同
    ├── test_config_sources.py、test_logging_config.py、test_trace_middleware.py
    └── test_itemized_contract.py
```

本目录有 46 个直接测试模块，已超过 20 个的结构审查阈值。物理迁移应按单一职责成组进行，并先迁齐测试矩阵选择、`out/tests/` 镜像路径和仍有效的文档引用；不要只移动文件留下悬空路径。

## 可修改内容

- 新增、修改、删除核心模块的单元测试。
- 补充测试辅助类和 fixture。

## 不可修改内容

- 不要在此放置集成测试或 e2e 测试。
- 不要引入对业务服务的直接依赖测试。

## 规范

- 测试命名应清晰表达被测行为和预期结果。
- 优先使用 pytest-asyncio 测试异步代码。
- 每个测试应独立，不依赖外部状态。
