# 目录用途

`app/core/session_catalog_store/` 是 workspace 导航权威库 `session-catalog.sqlite`
基础设施 `SessionCatalogStore` 的唯一实现点（原先的 `app/core/session_catalog_store.py`
单文件已拆分入本包）。facade 落在本包 `__init__.py`，只保留 `SessionCatalogStore` 的类声明
（七个本包 mixin）、连接生命周期与 schema 闸门方法（`__init__`/`connection`/`close`/
`_ensure_open`/`_connect`/`_initialize`/
`write_transaction`/`_bump_generation`/`current_generation`/`read_transaction`），并按原名
再导出模块级公开符号。其余方法按垂直链路逐字搬迁到同包各 mixin，由多继承装配。

本包承载的族：canonical 校验器与生命周期栅栏（`validators.py`）、不可变投影 DTO 与错误类
（`contracts.py`）、nodes 表读写（`nodes.py`）、fork retention claim（`fork_retention.py`）、
creation record journal（`creation_journal.py`）、fork source capture journal（`fork_source_capture.py`）、
共享创建行读取/投影（`creation_record_rows.py`）、subtree delete journal 与空 folder 删除
（`subtree_delete.py`）、只读查询（`queries.py`）、备份与目录一致性（`backup.py`），表 DDL 与
共享列清单收敛在 `_schema.py`。

`sqlite_integrity.py` 是启动与只读备份共用的 SQLite 页、索引、约束和外键校验；
调用方注册既有表达式索引函数并持同一读快照，不重建索引或修复数据。
`schema_validation.py` 是这两个入口共用的只读schema闸门；既有当前版本缺表、
导航队列结构或revision触发器不符时拒绝，不补空表。导航队列3表/2索引的物理DDL
与其余catalog结构统一放在 `sql/`；业务队列的行语义与执行仍由business owner负责。

# 可修改内容

- 可以维护 facade `__init__.py` 中的 `SessionCatalogStore` 类声明、连接生命周期方法、
  `SCHEMA_VERSION` 与 `__all__`/再导出面。
- 可以维护各族 mixin 文件中的方法体与族内私有静态方法；`creation_envelope.py` 统一创建、恢复与启动读取的纯 metadata/请求意图/preimage 校验，不访问数据库或物理目录；`creation_record_rows.py` 是创建与 capture 共用的唯一行读取和投影，不持有连接或发起事务。
- 可以维护 `_schema.py` 中的表 DDL、索引 DDL、版本-表绑定与 canonical ID/locator 形态正则；
  新增 DDL 常量时落在该模块，不在各族复制。
- 可以维护随本包落地的四段式 `AGENTS.md`。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/core/` 下。

# 不可修改内容

- 不得改变对外契约：`SessionCatalogStore` 类名、构造签名、公开方法名与语义、模块导入
  路径 `app.core.session_catalog_store.SessionCatalogStore`，以及 `__init__.py` 对
  `SessionCatalogNode`/`SessionCreationRecord`/`SubtreeDeleteRecord`/`ForkRetentionClaim`/
  `CatalogBackupManifest`/`CatalogIntegrityReport`/`SessionLifecycleFence`/各错误类与全部
  `validate_*`/`uuid7_embedded_utc_date` 的再导出，均 MUST 保持不变。
- 不得为兼容旧调用点保留转发方法、旧模块 shim 或双套实现；有状态跨族协作必须走同一宿主 `self`；纯行读取/投影由中立模块直接导入，不添加宿主转发方法。
- 不得静默改动搬迁方法的异常类型、错误消息或注释；搬迁必须逐字保留语义（禁止把
  `RuntimeError` 改成 `TypeError` 之类）。
- 不得改动 `write_transaction` 的事务边界与 `_bump_generation` 在 COMMIT 前的无条件调用
  顺序（8.1-F 缓存失效与 10.3b 的 `current_generation()` 正确性依赖此不变量）。
- 不得在 `_initialize` 内把权威表当空表重建；缺表/版本未知必须 fail-closed。
- 当前完整v10库保持原SQL结构与数据，不因DDL归属迁移升级版本；缺队列结构的开发半库
  不能作为合法旧格式自动补建，仍须走显式维护。

# 规范

- 静态结构 SQL 位于同包 `sql/`，所属 Python 模块只加载和协调原执行顺序；SQL 资产不自行提交事务，不保留内联结构副本。

- 方法族 mixin 之间只通过宿主 `self` 协作；每个方法在 `SessionCatalogStore.__mro__` 中只允许
  定义一处（`__init__` 除外）。
- 各 mixin 只依赖宿主类提供的 `database_path`、`sessions_root`、`_connection`、`_closed`、
  `_connection_lock`、`_ensure_open()`、`write_transaction()`、`read_transaction()` 等宿主接口，
  不得新增对其它族私有方法的隐式依赖。现有 nodes 族的 `_require_node`/`_fetch_node` 由组合后的宿主提供；此处仅明示这两个既存依赖，不授权扩展其它族私有接口。capture 不再调用 creation 族私有方法。
- 表 DDL 的唯一定义点在本包 `sql/`，`_schema.py` 加载结构并维护列清单；各 mixin 从这里取用，不复制 DDL。
- 错误分类沿用 `session_catalog_store`：`TypeError` 输入类型错误、`ValueError` 输入形态非法、
  `KeyError` 目标行不存在、`RuntimeError` 语义冲突；库被外部改动一律 fail-closed。
- 修改本目录后运行 `uv run ruff check app/core/session_catalog_store` 与带进程外保护地跑
  `out/tests` 下 `session_catalog` 相关聚焦测试。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
