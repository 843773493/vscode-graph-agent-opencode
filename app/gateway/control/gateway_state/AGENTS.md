# 目录用途

`app/gateway/control/gateway_state/` 是 Gateway 全局状态库 `gateway-state.sqlite` 基础设施
`GatewayStateStore` 的唯一实现点（原先的 `app/gateway/control/gateway_state.py` 单文件已拆分
入本包）。facade 落在本包 `__init__.py`，保留 `GatewayConfigRecord`、`GatewayStateStore` 的
类声明（四个既有兄弟子包 mixin + 本包七个 mixin）与连接生命周期 / KV 读写方法
（`__init__`/`path`/`diagnostics`/`connection`/`set_config`/`get_config`/`close`/`__enter__`/
`__exit__`），并按原名再导出 `_GATEWAY_MIGRATIONS`。其余方法按垂直链路逐字搬迁到同包各 mixin。

本包承载的族：legacy 秘密迁移（`lifecycle.py`）、active snapshot 与 pending candidate
（`active_snapshot.py`）、active 提升（`apply_promotion.py`）、config apply journal
（`apply_journal.py`）、config apply claim（`apply_claim.py`）、gateway restart intent
（`restart_intent.py`）、restart apply 与失败记录（`restart_apply.py`），schema 迁移序列与
一次性镜像行迁移收敛在 `_schema.py`。registry/config source/config event/runtime generation
仍归各自的兄弟子包，不在此重复。

# 可修改内容

- 可以维护 facade `__init__.py` 中的 `GatewayStateStore` 类声明、连接生命周期与 KV 读写方法、
  `GatewayConfigRecord` 与 `_GATEWAY_MIGRATIONS` 再导出面。
- 可以维护各 mixin 文件中的方法体与族内私有静态方法（行投影 helper）。
- 可以维护 `_schema.py` 中的迁移 DDL 序列与一次性镜像行迁移函数；新增表结构改动落在该模块，
  不在各族复制 DDL。
- 可以维护随本包落地的四段式 `AGENTS.md`。
- 可以维护对应单元测试；测试仍放在 `tests/unit/gateway/` 与 `tests/unit/configs/` 下。

# 不可修改内容

- 不得改变对外契约：`GatewayStateStore` 类名、构造签名、公开方法名与语义、模块导入路径
  `app.gateway.control.gateway_state.GatewayStateStore`，以及 `_GATEWAY_MIGRATIONS` 的再导出
  （`tests/unit/gateway/test_gateway_state.py` 直接读取其前 15 项构建独立库）。
- 不得改动 `_GATEWAY_MIGRATIONS` 的条目顺序或内容：其序号即 `SQLiteStateDatabase.schema_version`
  的权威来源，顺序变化会静默改变既有库的迁移路径。
- 不得改动任何写路径的 `BEGIN IMMEDIATE` / `COMMIT` / `rollback` 边界、写入顺序、CAS 条件、
  fencing token 与幂等键生成方式；`begin_config_apply` / `acquire_config_apply_claim` /
  `promote_active_config_snapshot` / `begin_gateway_restart_apply` 的并发语义必须逐字保持。
- 不得为兼容旧调用点保留转发方法、旧模块 shim 或双套实现；跨族协作必须走同一 `self`。
- 不得静默改动搬迁方法的异常类型、错误消息或注释；搬迁必须逐字保留语义。

# 规范

- 七个 mixin 一律扁平：彼此不继承、也不继承宿主其它 mixin；宿主 `GatewayStateStore` 显式
  列出全部 mixin。每个方法在 `GatewayStateStore.__mro__` 中只允许定义一处（`__init__` 除外）。
- 跨族调用（如 `begin_gateway_restart_apply` 调 `get_config_apply_claim`）经宿主 `self` 的 MRO
  解析，不引入 mixin 之间的继承边，以避免 MRO 线性化冲突。
- 各 mixin 只依赖宿主提供的 `_database`（以及内核 `self._insert_config_event` /
  `self._read_registry_revision` 等兄弟子包提供的唯一实现），不重复实现兄弟子包的表读写。
- 表 DDL 与迁移序号的唯一定义点在本包 `_schema.py`；`logger` 名固定为 facade 模块名，
  保证搬迁前后日志路由不变。
- 错误分类沿用 gateway_state 约定：`ValueError` 输入形态非法、`KeyError` 目标记录缺失、
  `ConfigConflictError` CAS/幂等冲突、`RuntimeError` 事务后读取失败。
- 修改本目录后运行 `uv run ruff check app/gateway/control/gateway_state` 与带进程外保护地跑
  `tests/unit/gateway/test_gateway_state.py` 等聚焦测试。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
