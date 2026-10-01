# 目录用途

`app/services/infrastructure/attachment_blob_catalog/` 承载 workspace 级附件正文 content-addressed blob store 这条垂直链路的唯一实现：`blob_identity.py` 定义 `blb_[0-9a-f]{64}` 身份与日期分桶 locator 原语；`locator.py` 是受检 relative locator（blob 与 staging 两类）的唯一生产者/解析者；`catalog.py` 是 `<workspace>/.boxteam/attachments/catalog.sqlite` 的 SQLite owner（blobs/ingest_records/commit_claims/owner_refs 四张表与 create-or-get、CAS、tombstone）；`store.py` 编排 pin 与 workspace ingest 的跨库 saga、读取、定点恢复与引用感知 GC。

# 可修改内容

- 可以维护 `blob_identity.py` 的身份原语、`locator.py` 的 locator 形态与预算校验、`catalog.py` 的表 DDL/行投影/读写方法、`store.py` 的写入 saga、读取、`recover_session`、`release_session_references` 与 `collect_garbage`。
- 可以维护对应的单元测试（放在 `tests/unit/services/infrastructure/`）。

# 不可修改内容

- 不得在 catalog 或 store 中扫描日期目录来定位、吸收或清理 blob：唯一权威是 `catalog.sqlite` 冻结的记录。
- 不得接受调用方拼接的路径；一切物理路径必须经 `locator.py` 的受检解析器。
- 不得为同一 digest 生成第二个 locator，不得按上传日期复制已有 blob；同一 blob-id 对应不同 digest/length/bytes 必须抛 `BlobIdentityConflictError`，不得覆盖。
- 不得跨正文写入或模型执行持有 `SessionLifecycleGate`；不得同时持有两个 gate 或两个数据库写事务。
- 不得把 pin/lease 的 typed 合同复制到本目录：`SessionOperationLease` 字段集与状态闭集仍由 `app/core/session_lifecycle_gate.py` 单点定义，通用 lease 持久化仍由 `app/core/session_control_operation_lease/` 承担。
- 不得保留旧「按 session 目录拼 `attachments/{sha256}{suffix}`」的定位或任何兼容 adapter/双写/fallback。

# 规范

- 严格照 saga 顺序：取 gate + fresh 校验 + create-or-get `AttachmentOperationPin` → create-or-get `AttachmentIngestRecord(preparing)` → 写 staging 并 durable → 推进 `hashed` 并竞争唯一 `BlobCommitClaim` → 原子 rename 到 claim 冻结 locator 并复验 → 再次取同一 gate 复验 pin/fence/thread 后发布 availability/attachment/owner ref → 主体 durable 后才把 pin 推进终态。
- 统一锁序：一个 `SessionLifecycleGate` → 至多一个 catalog/SQLite 写事务。
- 错误分类沿用 `session_catalog_store`：`TypeError` 类型错、`ValueError` 形态非法、`KeyError` 目标行缺失、`RuntimeError`（含 `BlobIdentityConflictError`）语义冲突。
- GC 必须先原子提交 tombstone/availability，再删物理正文；失败可幂等重试。
- 修改本目录后运行 `uv run ruff check`、`uv run python -m compileall` 与 `uv run pytest tests/unit/services/infrastructure`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

