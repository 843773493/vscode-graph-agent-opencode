# 目录用途

`app/core/session_control_store/` 是 per-session `session-control.sqlite` 基础设施
`SessionControlStore` 的唯一实现点（原先的 `session_control_store.py` 单文件已拆分入
本包）。facade 落在本包 `__init__.py`，只保留 `SessionControlStore` 的类声明（四个既有
mixin + 五个本包 mixin）、连接生命周期方法（`__init__`/`connection`/`close`/
`_ensure_open`/`_begin_immediate`/`_write_transaction`/`_connect`）与模块级公开符号的
再导出；其余方法按垂直链路逐字搬迁到同包各 mixin，由 `SessionControlStore` 多继承装配。

本包承载的族：thread creation record（`thread_creation_record.py`）、创建发布与终结
（`thread_creation_publish.py`）、初始 execution intent（`execution_intent.py`）、
collaboration 成员账本（`collaboration.py`）、schema 初始化与版本升级
（`_schema.py`），以及共享 SQL 常量 `sql.py`。thread catalog/fence、operation lease、
owner binding 与跨 Session 通信 ledger 各自已在同名兄弟子包中承载，不在本包重复实现。

# 可修改内容

- 可以维护 facade `__init__.py` 中的 `SessionControlStore` 类声明、连接生命周期方法、
  `SCHEMA_VERSION` 与 `__all__`/再导出面。
- 可以维护各方法族 mixin 文件中的方法体、族内私有静态方法与族专属的行投影 dataclass。
- 可以维护 `sql.py` 中收敛同语义重复的 SQL 常量；新增共享常量时落在该模块，不在各族复制。
- 可以维护随本包落地的四段式 `AGENTS.md`。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/core/` 下。

# 不可修改内容

- 不得改变对外契约：`SessionControlStore` 类名、构造签名、公开方法名与语义、模块导入
  路径 `app.core.session_control_store.SessionControlStore`，以及 `__init__.py` 对
  `ThreadCreationRecord`/`ThreadExecutionIntent`/`CollaborationMember`/`_INITIAL_STATE_VALUES`
  等模块级符号的再导出，均 MUST 保持不变。
- 不得为兼容旧调用点保留转发方法、旧模块 shim 或双套实现；跨族协作必须走同一 `self`。
- 不得静默改动搬迁方法的异常类型、错误消息或注释；搬迁必须逐字保留语义（禁止把
  `RuntimeError` 改成 `TypeError` 之类）。
- 不得在本目录重新实现 thread catalog/fence、operation lease、owner binding 或通信 ledger
  的 DDL/读写；这些分别归 `session_control_thread_catalog/`、`session_control_operation_lease/`、
  `session_control_thread_owner_binding/`、`session_control_communication_ledger/`。
- 不得静默吞掉不一致：库被外部改动、record/intent 状态冲突、CAS 失败必须直接抛错。

# 规范

- 方法族 mixin 之间只通过宿主 `self` 协作；跨 mixin 引用静态方法时按 MRO 凸性规则用具体
  mixin 类名限定（例如 `ThreadCreationRecordMixin._validate_frozen_json_text`），每个方法在
  `SessionControlStore.__mro__` 中只允许定义一处（`__init__` 除外）。
- 各 mixin 只依赖宿主类提供的 `database_path`、`_connection`、`_closed`、`_ensure_open()`、
  `_begin_immediate()` 与 `_write_transaction()`；不得假设其它族的私有方法存在，跨族调用须
  经由定义该方法的 mixin 提供的公开/受保护接口。
- 表 DDL 与列清单常量的唯一定义点在其所属族模块；`_schema.py` 只编排建表顺序，不复制 DDL。
- 错误分类沿用 `session_control_store`：`KeyError` 目标行缺失、`RuntimeError` 库被外部改动
  或语义冲突、`ValueError` 输入形态非法、`TypeError` 输入类型错误。
- 修改本目录后运行 `uv run ruff check app/core/session_control_store/` 与带进程外保护地跑
  `uv run pytest tests/unit/core/test_session_control_store.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
