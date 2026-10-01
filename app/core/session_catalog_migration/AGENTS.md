# 目录用途

`app/core/session_catalog_migration/` 是旧 session catalog(JSON index + 嵌套
Session/Folder/children 物理树)→ SQLite catalog + 日期桶的**一次性迁移机器**的唯一实现点
（原先的 `app/core/session_catalog_migration.py` 单文件已按迁移阶段拆分入本包）。facade 落在
本包 `__init__.py`，只保留原模块 docstring/红线、`__all__`、`SessionCatalogMigrator` 的类声明
（六个扁平 mixin）与模块级入口（`migrate_workspace_session_catalog`、
`_ensure_entry_workspace_id_bound`），并按原名再导出模块级公开符号。

本包承载的族：契约与共享常量（`_constants.py` 的目录/状态闭集常量、`_contracts.py` 的 DTO/
异常/journal 映射与拓扑序工具）、journal 读取校验与写入（`_journal.py`）、首次迁移入口
（`_fresh.py`）、预检/旧权威/备份复验/冻结与 quarantine 分类（`_preflight.py`）、迁移主管线
（`_pipeline.py`）、gate 内 catalog 幂等重建与对账（`_catalog.py`）、物理树迁移与
session-control 初始化/终验/通用工具（`_physical.py`）。

# 可修改内容

- 可以维护 facade `__init__.py` 中的 `SessionCatalogMigrator` 类声明、基线属性
  （`JOURNAL_SCHEMA_VERSION`/`MIGRATION_NAME`）、公开入口函数与 `__all__`/再导出面。
- 可以维护各族 mixin 文件中的方法体与族内私有静态方法。
- 可以维护 `_constants.py` 中的共享常量与 `_contracts.py` 中的 DTO/异常/映射工具；新增共享
  常量时落在 `_constants.py`，不在各族复制。
- 可以维护随本包落地的四段式 `AGENTS.md`。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/core/` 下。

# 不可修改内容

- 不得改变对外契约：`SessionCatalogMigrator` 类名、构造签名、公开方法名与语义、模块导入路径
  `app.core.session_catalog_migration.{SessionCatalogMigrator,migrate_workspace_session_catalog}`，
  以及 `__init__.py` 对 `QuarantinedNode`/`SessionCatalogMigrationError`/
  `SessionCatalogMigrationResult` 的再导出，均 MUST 保持不变；`JOURNAL_SCHEMA_VERSION=2` 与
  `MIGRATION_NAME="session-catalog-json-to-sqlite"` 必须仍挂在 `SessionCatalogMigrator` 上。
- **不得改变迁移语义**：迁移执行顺序（阶段1 gate 内重建 → 阶段2 备份复验 → 阶段3 物理树迁移
  → 阶段4 终验 → 阶段5 completed）、staging/journal/隔离区目录命名（`.staging/<migration_id>/`、
  `maintenance/session-catalog-migration/journal.json`、`orphaned/session-catalog-migration/`）、
  事务边界、物理段幂等判据（pending/staged/placed/control_state 的定点继续规则）与 fail-closed
  行为均 MUST NOT 改变。物理段 session/folder 一律按旧路径深度**降序**处理，不得重排。
- 不得为兼容旧调用点保留转发方法、旧模块 shim 或双套实现；跨族协作必须走同一 `self`。
- 不得静默改动搬迁方法的异常类型、错误消息或注释；搬迁必须逐字保留语义（禁止把
  `RuntimeError` 改成 `TypeError` 之类）。
- 不得扫盘吸收旧树改动、不得返回虚假默认值、不得静默失败；不一致一律 fail closed 或隔离。

# 规范

- 方法族 mixin 一律**扁平**：mixin 之间不得互相继承；跨族调用经宿主 `SessionCatalogMigrator`
  的 MRO 通过同一个 `self` 解析，且宿主显式列出全部 mixin。
- 本包内不得引入 `logging.getLogger(__name__)` 之类依赖模块名的 logger；如需日志须显式 pin
  名称，避免换包静默改名。
- 共享常量与 DTO 的唯一定义点分别在 `_constants.py`/`_contracts.py`；各 mixin 从这里取用，
  不复制。
- 迁移机器的红线 docstring（不切权威、不写旧 index/manifest、不做 rollout thread 化、
  不装配 container）必须原样保留在 facade。
- 修改本目录后运行静态分析，并带进程外保护地跑 `tests/unit/core/` 下迁移相关聚焦测试。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
