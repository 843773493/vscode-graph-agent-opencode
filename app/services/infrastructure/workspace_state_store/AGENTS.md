# 目录用途

`workspace_state_store/` 实现工作区 SQLite 状态库，集中管理 store 装配、配置生命周期方法、共享 SQL 与静态 schema 迁移。

```text
workspace_state_store/
├── __init__.py          WorkspaceStateStore 组合、schema 顺序与基础 lifecycle 方法
├── apply.py             配置 apply claim 与 journal
├── pending.py           pending candidate 状态与读取
├── restart.py           重启状态和恢复边界
├── snapshot.py          active config snapshot 生命周期
├── sql_statements.py    多个职责族共享的 SQL 语句
└── workspace_state_sql/
    ├── 001.sql–013.sql  有序 schema 迁移
    ├── 015.sql          MCP 连接操作及幂等 alias；第14次为 Python 来源合并
    ├── 016.sql          持久任务定义版本与运行账本
    ├── 017.sql          任务计划游标、接受序号与v16运行迁移
    ├── 018.sql          外部通知触发输入与v17运行迁移
    ├── 019.sql          执行结束来源游标与v18运行迁移
    ├── 020.sql          模型判断的原执行引用
    ├── 021.sql          Goal任务应用状态、当前归属及一次性导入指纹
    └── legacy_config/   仅供一次性旧表迁移的 SQL
```

外部代码通过 `app.services.infrastructure.workspace_state_store` 导入公开 store 与 DTO。职责模块只由本包装配；`workspace_state_sql/` 是唯一静态迁移来源，迁移顺序由 `__init__.py` 显式维护。

# 可修改内容

- 可以按配置生命周期职责维护 `apply.py`、`pending.py`、`restart.py` 与 `snapshot.py`。
- 可以维护 store 装配、schema 顺序和 `sql_statements.py` 中被多个职责族共用的语句。
- 可以维护 `workspace_state_sql/` 下的版本迁移及 `legacy_config/` 的一次性迁移 SQL。

# 不可修改内容

- 不在包外复制 schema、迁移 SQL 或配置状态写入逻辑。
- 不让职责 mixin 各自创建数据库或维护第二套迁移顺序。
- 不通过扫描目录推断迁移顺序；不静默回退到旧数据源。
- 不修改公开导入路径前，先迁齐全部直接和间接消费者。

# 规范

- `WorkspaceStateStore` 是唯一数据库 owner；mixins 只使用 owner 提供的 `_database`。
- 保持配置生命周期的事务、revision、冲突及错误归属语义。
- SQL 迁移由 Python 显式排序和执行；迁移资源缺失时直接失败。
- 修改后运行 `uv run ruff check` 和受保护的对应 pytest。
