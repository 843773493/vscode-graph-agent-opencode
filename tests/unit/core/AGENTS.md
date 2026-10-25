# tests/unit/core

## 目录用途

存放后端核心基础设施的单元测试，例如事件总线、任务上下文等。

## 按职责查找

下面先列出已物理归组的测试目录，再列出仍在根目录的测试职责。新增测试先按行为归属查找，已有稳定测试组优先放入对应目录：

```text
tests/unit/core/
├── identity/
│   ├── test_identifier*.py、test_canonical_identifier_matrix.py
│   ├── test_distribution_identity.py、test_user_identity.py、test_workspace_identity.py
│   ├── test_non_canonical_exemptions.py、test_hashing.py
│   └── test_canonical_creation_id_allocation.py
├── storage/
│   ├── test_atomic_fs.py、test_file_permissions.py
│   ├── test_sql_assets.py、test_sqlite_state.py
│   └── test_sqlite_vfs.py
├── environment/
│   ├── test_boxteam_env.py、test_mounted_workspaces.py
│   └── test_user_storage_migration.py、test_backend_activation.py
├── configuration/
│   ├── test_config_sources.py、test_checkpoint_config.py
│   └── test_logging_config.py
├── events/
│   ├── test_background_message_bus.py、test_job_event_bus.py
│   └── test_job_event_bus_channel_adapter.py
├── execution/
│   ├── test_cancelable_stream.py、test_execution_admission_context.py
│   ├── test_key_lock_pool.py、test_lifetime_handle.py
│   └── test_lifetime_scope.py、test_turn_execution_scope.py
└── 根目录测试（按职责分组）
    ├── Session 目录与生命周期
    │   ├── catalog_workspace_helper.py、test_catalog_workspace_helper.py
    │   ├── test_session_catalog*.py、test_session_creation*.py
    │   ├── test_thread_creation.py、test_session_subtree_delete.py
    │   ├── test_fork_retention_release.py、test_session_control*.py
    │   └── test_session_lifecycle_gate.py、test_session_interrupt_state.py、test_session_thread_context.py
    ├── 路径与数据：test_path_utils*.py、test_bounded_json.py
    └── 追踪与领域合同：test_trace_middleware.py、test_itemized_contract.py
```

身份、存储、运行环境与工作区边界、运行配置、事件通道、执行生命周期测试已分别集中在 `identity/`、`storage/`、`environment/`、`configuration/`、`events/`、`execution/`。核心目录根部保留 Session 生命周期与少数未成组测试；迁移时同步核对测试矩阵选择、`out/tests/` 镜像路径和仍有效的文档引用，不要只移动文件留下悬空路径。

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
