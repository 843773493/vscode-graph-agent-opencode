# 目录用途

存放 Workspace 文件服务与 `workspace_files` owner 的单元测试。按路径绑定、用户文件操作、资源观察和持久发布划分，不把同一文件能力散落在基础设施测试根目录。

```text
workspace_files/
├── test_native_directory_files.py          Gateway native 目录绑定与 Workspace 文件 API 边界
├── test_workspace_file_edit.py              内容读取编辑、列表、上传复制与下载
├── test_workspace_file_mutation_lock.py     同进程和跨进程文件写锁
├── test_workspace_file_operation_recovery.py durable mutation 崩溃恢复
├── test_workspace_file_operations.py         HTTP 接受、排队、目录版本和文件操作状态
├── test_workspace_file_resources.py          文件资源身份、Skill 与 AGENTS.md 观察
├── test_workspace_file_watch_service.py      watcher 生命周期、过滤、共享和有界通知
├── test_workspace_text_publication_errors.py 文本读取和原子发布错误合同
├── test_native_staging.py                    原生目录暂存和发布
└── test_text_journal_errors.py               文本变更 journal 的持久化错误
```

# 可修改内容

- Workspace 文件路径、native 目录绑定、读取写入及 HTTP API 测试。
- durable mutation 的受理、执行顺序、冲突、重启恢复和文件资源观察测试。
- 文本 journal、条件发布、暂存清理和定点 I/O 故障测试。

# 不可修改内容

- 不覆盖 Gateway 全局生命周期、代理路由或浏览器流程；native 目录测试只覆盖 Workspace 文件路径所需的 Gateway 绑定 API。
- 不写真实用户工作区，不用手工 journal 状态冒充完整 Agent 执行或恢复验收。
- 不在此目录实现或测试会话业务规则。

# 规范

- 文件副作用使用 `tmp_path` 或当前测试文件对应的 `out/tests/unit/services/infrastructure/workspace_files/<测试名>/`。
- 文件操作测试覆盖工作区和外部目录边界；错误测试同时核验错误类别、原因链、原件与发布效果。
- watcher 测试关闭订阅和服务；持久操作恢复测试保留独立进程与故障阶段证据。
