# 目录用途

`app/services/infrastructure/attachment_blob_catalog/` 承载 workspace 级附件正文 content-addressed blob store 这条垂直链路的唯一实现：`blob_identity.py` 定义 `blb_[0-9a-f]{64}` 身份与日期分桶 locator 原语；`locator.py` 是受检 relative locator（blob 与 staging 两类）的唯一生产者/解析者；`store.py` 编排 pin 与 workspace ingest 的跨库 saga、统一正文完整性校验、读取与引用感知 GC；`recovery.py` 唯一负责消息接受结算、定点恢复与删除收敛。`fork_store.py` 提供宿主唯一使用的 fork 方法族，消费统一资源目标决策并区分消费 Thread 与实际正文保活 owner；`fork_import.py` 是跨域导入与终态 staging 回收的唯一实现；`fork_claims.py` 负责冻结 alias、精确 operation claim、引用预留和终结，不复制附件字节。

catalog 侧原先的单文件 `catalog.py` 已按垂直链路拆入本包，facade 落在本包 `__init__.py`：只保留 `AttachmentBlobCatalog` 的类声明（组合 `IngestMixin`/`BlobsMixin`/`QueriesMixin` 三个 mixin）、`SCHEMA_VERSION` 与连接生命周期方法（`__init__`/`connection`/`close`/`_connected`/`_connect`/`_initialize`/`write_transaction`），并按原名再导出模块级公开符号。表 DDL/索引/触发器与种子唯一定义在 `sql/`，`schema.py` 显式加载并保留必需表清单与两个形态工具；不可变行投影与 `derive_attachment_id` 在 `records.py`；ingest record + digest claim 链在 `ingest.py`；blob availability + owner reference 链在 `blobs.py`；引用感知 tombstone/GC 链在 `queries.py`。

# 可修改内容

- 可以维护 `blob_identity.py` 的身份原语、`locator.py` 的 locator 形态与预算校验、`sql/` 的表/索引/触发器/种子及 `schema.py` 的加载、必需表清单与形态工具、`records.py` 的行投影与身份派生、`ingest.py`/`blobs.py`/`queries.py` 的方法体、facade `__init__.py` 的类声明与生命周期，以及 `store.py` 的写入 saga、统一正文校验、读取和异步 `collect_garbage`，以及 `recovery.py` 的接受结算、`recover_session` 与引用释放；fork 捕获、目标保活与发布方法仅维护在 `fork_store.py`，SQLite alias/claim 事务仅维护在 `fork_claims.py`；`fork_import.py` 承载导入与临时文件清理。
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

- `legacy_references.py` 在目录 catalog 内解析旧 Session 附件 URI 与精确 Thread 的冻结 fork alias；离线发布核验实际消费该入口。来源 owner/blob、原件版本和 claim preimage 必须一致；来源 locator 仅作历史事实，跨机器不得用它定位目标。未映射的外部 URI 不以相同 digest 猜测归属；不访问旧库或旧正文。该解析不等于线上 schema 4 已切换。
- 目录资源的 `YYYY/MM/DD/{attachment_id}` 生成、真实日期/身份校验和路径预算解析唯一定义在 `locator.py`；冻结日期使用显式创建 intent 的 UTC 日期，旧非 UUIDv7 的 attachment_id 原样保留。离线转换复用这些原语，不在迁移脚本或未来上传方各自拼目录。`directory_schema.py` 与 `sql/managed_directory_schema.sql` 定义隔离目标的 schema 4 基础目录结构，由离线转换准备；线上目录上传及切换尚未完成，现有 blob/staging 生产链待调用方迁齐后统一下线。
- 在线目录访问与离线维护共用 `maintenance.py` 的跨进程共享/独占锁，锁文件位于附件根同级，不得删除或随 catalog/目录切换替换。Backend 在任何业务初始化前持共享锁至全部 writer 排空；catalog 的惰性目录访问另持共享锁至连接确认关闭，关闭失败保留连接与锁并允许重试。独占冲突直接报错，不以端口/PID/锁文件存在推断停机。该保护不替代离线迁移对持久 operation、原件和历史依赖的核对，线上 schema 3 → 4 的唯一切换未实现前不得修改线上版本或声称已迁移。
- `migration/` 提供显式离线 prepare/verify，持独占锁只读核对原 owner 状态并保存备份；未知占用或不完整 schema 明确拒绝。schema 参照与线上初始化共用 `schema._SCHEMA_STATEMENTS`，不复制 DDL；线上数据库使用 SQLite backup API，冻结 capture 逐字保全，完成备份用 immutable 读取核验。prepared 仅证明备份阶段完成；directories_prepared 和 catalog_prepared 分别证明隔离目录、目标基础 catalog 完成。目标 catalog 保留完整旧 owner/blob 行和终态 fork 冻结原文，不改旧 hash；线上读取解析、上传/fork/ingest、恢复及切换仍 TODO，不增加线上双读或修改历史身份。
- 严格照 saga 顺序：取 gate + fresh 校验 + create-or-get `AttachmentOperationPin` → create-or-get `AttachmentIngestRecord(preparing)` → 写 staging 并 durable → 推进 `hashed` 并竞争唯一 `BlobCommitClaim` → 原子 rename 到 claim 冻结 locator 并复验 → 再次取同一 gate 复验 pin/fence/thread 后发布 availability/attachment/owner ref → 主体 durable 后才把 pin 推进终态。
- 正文提交复用 `OperationHolderGate(namespace="attachment_blob_commit")` 的固定分片跨进程锁。普通上传在该锁内推进 hashed、完成唯一 locator 的 rename/复验；释放锁后才取得 Session gate 发布。hashed record 在 catalog 事务中保护尚未发布的正文，fork import 则由原 preparing/owner_reserved claim 保活。正文提交锁内不得等待 Session gate、网络或另一把正文锁。
- 上传 pin、owner-ref 发布及 fork owner reservation/finalize 的短临界区锁序为 `NavigationTopologyGate` shared → 一个 `SessionLifecycleGate` → 至多一个 catalog/SQLite 写事务；新 child reservation 也须 fresh 验证所属 Session catalog active。正文写入和传输不持有 topology/Session gate。GC 可在删除 gate 内调用，但只能非阻塞探测正文提交锁（wait=False）；未取得即留待下次回收，绝不反向等待。GC 取得锁后事务重查 owner/retention、非终态 ingest 和 fork claim，再提交 tombstone/aborted，最后按冻结 locator 删除；不扫盘。
- ingest record 的 digest/blob_id/length + 状态 UPDATE 是单点：`ingest._update_ingest_record_identity`，`mark_ingest_hashed`（走 `hashed`）与 `publish_blob_and_owner_ref`（走 `published`）都调用它，不得在两处各写一条同型语句。
- catalog mixin 依赖宿主提供的 `database_path`、`_closed`、`_connection`、`_connected()`、`write_transaction()` 和同一 catalog 的既有查询；`ForkAttachmentStoreMixin` 显式消费宿主的 catalog、locator、Session gate、控制库及正文验证端口，不建立第二套 store。表、索引与触发器唯一定义在 `sql/`；`schema.py` 显式加载，列清单和执行协调保留在 Python，不复制 DDL。
- 无 claim 的复用必须冻结真实已发布 owner 引用；保活范围和消费 Thread 不混同。必要 claim 在 source guard 释放前建立，reserved 引用在 finalizer 提交前不可读取；失败、删除与 GC 只处理本 operation 的精确引用，无 claim 的操作不建立 settlement record。
- 附件单文件 URI 只由公共 grammar 构造与解析；原 attachment_id、blob/lease/GC 语义不变。fork 快照同时冻结目标 Gateway/Workspace URI，投影不得按当前请求重新拼目标或重绑映射。
- 错误分类沿用 `session_catalog_store`：`TypeError` 类型错、`ValueError` 形态非法、`KeyError` 目标行缺失、`RuntimeError`（含 `BlobIdentityConflictError`）语义冲突；库被外部改动一律 fail-closed。
- GC 必须先原子提交 tombstone/availability 或未发布 claim 的 aborted，再删物理正文；失败可幂等重试。未发布 claim 保留首次日期/locator，新 operation 可重新领取；旧 ingest/pin 不重开。已发布正文缺失或损坏必须报错，普通重新上传只允许恢复已 tombstone 正文的 availability。
- 修改本目录后运行 `uv run ruff check`、`uv run python -m compileall` 与带进程外保护地跑 `uv run pytest tests/unit/services/infrastructure`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
- fork import 全程持有 `attachment_fork_import` operation holder，内部仅嵌套短时正文提交锁，不取得 Session gate。中止和 GC 只以 wait=False 探测 holder，取得后重查全部 claim 已终结，再由原 operation/blob preimage 清理 staging；终态 claim 保留 frozen snapshot，不依赖已删除 mappings，不扫描目录。
- `ForkAttachmentSnapshot.to_payload()` 是旧 v2 附件冻结载荷的唯一序列化入口，claim、captured marker 和 source hash 都复用它；不得用 dataclass 全字段反射隐式扩展 v2。目录快照必须显式登记新版本，旧原文和 hash 不改写。

- 跨域附件正文先复制进原 fork capture 的 `attachment-files/`，captured marker v3 核对其完整集合与冻结 digest/length；网络统一使用原 files/chunk，不提供 live blob 分块接口。目标 importer 只消费已传入的冻结文件，复制线程由原 operation holder 覆盖，取消须等待真实 writer 退出。旧 v2 marker/载荷只保留原文读取，不补造历史文件；升级前须在旧版本收敛非终态跨域导出。
