# 目录用途

`app/services/infrastructure/attachment_blob_catalog/` 承载 workspace 级附件正文 content-addressed blob store 这条垂直链路的唯一实现：`blob_identity.py` 定义 `blb_[0-9a-f]{64}` 身份与日期分桶 locator 原语；`locator.py` 是受检 relative locator（blob 与 staging 两类）的唯一生产者/解析者；`store.py` 编排 pin 与 workspace ingest 的跨库 saga、读取、定点恢复与引用感知 GC。

catalog 侧原先的单文件 `catalog.py` 已按垂直链路拆入本包，facade 落在本包 `__init__.py`：只保留 `AttachmentBlobCatalog` 的类声明（组合 `IngestMixin`/`BlobsMixin`/`QueriesMixin` 三个 mixin）、`SCHEMA_VERSION` 与连接生命周期方法（`__init__`/`connection`/`close`/`_connected`/`_connect`/`_initialize`/`write_transaction`），并按原名再导出模块级公开符号。表 DDL/索引/必需表清单与两个形态工具收敛在 `schema.py`；不可变行投影与 `derive_attachment_id` 在 `records.py`；ingest record + digest claim 链在 `ingest.py`；blob availability + owner reference 链在 `blobs.py`；引用感知 tombstone/GC 链在 `queries.py`。

# 可修改内容

- 可以维护 `blob_identity.py` 的身份原语、`locator.py` 的 locator 形态与预算校验、`schema.py` 的表 DDL/索引/必需表清单与形态工具、`records.py` 的行投影与身份派生、`ingest.py`/`blobs.py`/`queries.py` 三个 mixin 的方法体、facade `__init__.py` 的类声明与生命周期，以及 `store.py` 的写入 saga、读取、`recover_session`、`release_session_references` 与 `collect_garbage`。
- 可以维护随本包落地的四段式 `AGENTS.md`。
- 可以维护对应的单元测试（放在 `tests/unit/services/infrastructure/`）。

# 不可修改内容

- 不得在 catalog 或 store 中扫描日期目录来定位、吸收或清理 blob：唯一权威是 `catalog.sqlite` 冻结的记录。
- 不得接受调用方拼接的路径；一切物理路径必须经 `locator.py` 的受检解析器。
- 不得为同一 digest 生成第二个 locator，不得按上传日期复制已有 blob；同一 blob-id 对应不同 digest/length/bytes 必须抛 `BlobIdentityConflictError`，不得覆盖。
- 不得改动 blob 身份/内容寻址哈希、去重语义、lease 与 GC 判据；`derive_attachment_id` 的 preimage 与 `_sha256` 折算必须逐字保留。
- 不得跨正文写入或模型执行持有 `SessionLifecycleGate`；不得同时持有两个 gate 或两个数据库写事务。
- 不得把 pin/lease 的 typed 合同复制到本目录：`SessionOperationLease` 字段集与状态闭集仍由 `app/core/session_lifecycle_gate.py` 单点定义，通用 lease 持久化仍由 `app/core/session_control_operation_lease/` 承担。
- 不得改变对外契约：`AttachmentBlobCatalog` 类名、构造签名、公开方法名与语义、模块导入路径 `app.services.infrastructure.attachment_blob_catalog.catalog`，以及 `__init__.py` 对四个 record 类/`derive_attachment_id`/`CATALOG_DATABASE_NAME`/`INGEST_RECORD_TERMINAL_STATES`/`BlobIdentityConflictError` 的再导出面（`__all__`）。
- 不得为兼容旧调用点保留转发方法、旧模块 shim 或双套实现；方法族 mixin 之间只通过宿主 `self` 协作，每个方法在 `AttachmentBlobCatalog.__mro__` 中只允许定义一处。
- 不得静默改动搬迁方法的异常类型、错误消息或注释；搬迁必须逐字保留语义。
- 不得保留旧「按 session 目录拼 `attachments/{sha256}{suffix}`」的定位或任何兼容 adapter/双写/fallback。

# 规范

- 严格照 saga 顺序：取 gate + fresh 校验 + create-or-get `AttachmentOperationPin` → create-or-get `AttachmentIngestRecord(preparing)` → 写 staging 并 durable → 推进 `hashed` 并竞争唯一 `BlobCommitClaim` → 原子 rename 到 claim 冻结 locator 并复验 → 再次取同一 gate 复验 pin/fence/thread 后发布 availability/attachment/owner ref → 主体 durable 后才把 pin 推进终态。
- 统一锁序：一个 `SessionLifecycleGate` → 至多一个 catalog/SQLite 写事务。
- ingest record 的 digest/blob_id/length + 状态 UPDATE 是单点：`ingest._update_ingest_record_identity`，`mark_ingest_hashed`（走 `hashed`）与 `publish_blob_and_owner_ref`（走 `published`）都调用它，不得在两处各写一条同型语句。
- 各 mixin 只依赖宿主提供的 `database_path`、`_closed`、`_connection`、`_connected()`、`write_transaction()`；表 DDL 与列清单常量的唯一定义点是 `schema.py`，不复制 DDL。
- 错误分类沿用 `session_catalog_store`：`TypeError` 类型错、`ValueError` 形态非法、`KeyError` 目标行缺失、`RuntimeError`（含 `BlobIdentityConflictError`）语义冲突；库被外部改动一律 fail-closed。
- GC 必须先原子提交 tombstone/availability，再删物理正文；失败可幂等重试。
- 修改本目录后运行 `uv run ruff check`、`uv run python -m compileall` 与带进程外保护地跑 `uv run pytest tests/unit/services/infrastructure`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

