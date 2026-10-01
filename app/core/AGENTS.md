# 目录用途

`app/core/` 存放不带具体业务语义的后端通用内核，包括环境读取、路径契约、标识符、事件总线、后台任务注册、Trace 中间件和一次性存储迁移。

## 子包索引

- `atomic_fs.py`：文件级 durability 原语（`fsync_directory`/`fsync_file`/
  `atomic_write_bytes`）。会话创建、child thread 创建、catalog 迁移与子树删除
  四条链路共用同一套 tempfile + fsync + os.replace + 目录 fsync 语义，单点承载。
- `session_control_primitives.py`：per-session `session-control.sqlite` 各垂直链路共享的形态原语（`CONTROL_DATABASE_NAME`、`SHA256_HEX_PATTERN`、`EXECUTION_BINDING_ID_PATTERN`、`EXECUTION_JOB_ID_PATTERN` 与 `validate_claim_fields`）。只放跨子包共用的形态约束与校验器，不放任何表的 DDL、行投影或读写方法。
- `session_control_thread_catalog/`：thread catalog 与 lifecycle fence 一条垂直链路（main/child 权威指针、生命周期闸门 CAS、已发布 child 的冻结 locator 解析、`thread_catalog` v1→v2 加法升级）。只放这两张表的职责；creation record、execution intent、operation lease、owner binding 与通信账本不放这里。
- `session_control_thread_owner_binding/`：thread owner binding 字段槽一条垂直链路（`thread_owner_bindings` 行投影、canonical JSON 列表槽解析、2.1 负面合同校验、ensure/get/update，以及该表行插入的唯一 SQL 实现）。只放 owner 侧记录槽；prefix epoch 与 ToolSet revision 的权威解释仍属对应 domain owner，不构成第二 writer。
- `session_control_operation_lease/`：通用 operation lease 一条垂直链路（`session_operation_leases` DDL 与非终态索引、行投影、create-or-get 幂等准入、fencing token CAS 链与读取）。只放持久准入/操作 lease；`SessionOperationLease` 的 typed 字段集与状态闭集仍由 `session_lifecycle_gate.py` 单点定义。
- `session_control_communication_ledger/`：跨 Session 通信 ledger 一条垂直链路（`communication_outbox`/`communication_inbox` DDL 与 target_accepted 索引、行投影、source 侧 create-or-get 与状态 CAS、target 侧 create-or-get/领取/绑定/失败记录与读取、kind=reply 双端因果证明）。只放这两张 telemetry 账表；typed 合同的 field 集与状态闭集仍由对应 facade 单点定义。
- `session_control_store/`：per-session `session-control.sqlite` 基础设施 `SessionControlStore` 的唯一实现点（facade 在 `__init__.py`，保留类声明、连接生命周期与模块级符号再导出）。其下按垂直链路分 mixin：thread creation record（`thread_creation_record.py`）、创建发布与终结（`thread_creation_publish.py`）、初始 execution intent（`execution_intent.py`）、collaboration 成员账本（`collaboration.py`）、schema 初始化/升级（`_schema.py`）与共享 SQL 常量（`sql.py`）。thread catalog/fence、operation lease、owner binding 与通信 ledger 仍归各自的兄弟子包，不在此重复。
- `session_catalog_store/`：workspace 导航权威库 `session-catalog.sqlite` 基础设施 `SessionCatalogStore` 的唯一实现点（facade 在 `__init__.py`，保留类声明、连接生命周期与 schema 闸门、模块级符号再导出）。其下按垂直链路分 mixin：canonical 校验器与生命周期栅栏（`validators.py`）、不可变投影 DTO 与错误类（`contracts.py`）、nodes 表读写（`nodes.py`）、fork retention claim（`fork_retention.py`）、creation record journal（`creation_journal.py`）、subtree delete journal 与空 folder 删除（`subtree_delete.py`）、只读查询（`queries.py`）、备份与目录一致性（`backup.py`），表 DDL 与共享列清单收敛在 `_schema.py`。`write_transaction` 的事务边界与 `_bump_generation` 提交前无条件调用顺序是 8.1-F/10.3b 依赖的不变量，不得改动。
- `session_catalog_migration/`：旧 session catalog（JSON index + 嵌套 Session/Folder/children 物理树）→ SQLite catalog + 日期桶的**一次性迁移机器**的唯一实现点（facade 在 `__init__.py`，保留原红线 docstring、`SessionCatalogMigrator` 类声明与 `migrate_workspace_session_catalog` 入口、模块级符号再导出）。其下按迁移阶段分扁平 mixin：共享常量（`_constants.py`）、DTO/异常/映射工具（`_contracts.py`）、journal 读取校验与写入（`_journal.py`）、首次迁移入口（`_fresh.py`）、预检/旧权威/备份复验/冻结与 quarantine 分类（`_preflight.py`）、主管线（`_pipeline.py`）、gate 内 catalog 幂等重建与对账（`_catalog.py`）、物理树迁移与 session-control 初始化/终验/通用工具（`_physical.py`）。迁移执行顺序、staging/journal/隔离区目录命名、事务边界、物理段幂等判据与 fail-closed 行为是数据安全关键路径，不得改动。

# 可修改内容

- 可以维护跨业务模块共享且没有领域流程的基础能力。
- 可以为 `session_control_*` 子包补充目录索引与职责边界说明；新增垂直链路时同步在此登记。
- 可以在 `path_utils.py` 中定义全局目录、工作区目录和会话物理树根目录；权威索引读取、完整性校验及按稳定 ID 查找节点的规则必须集中在正式会话路径解析器中。
- 可以在 `user_storage_migration.py` 中实现配置安装维护入口所需的用户级存储迁移。

# 不可修改内容

- 不要放入会话、消息、Agent、工具或 Gateway 的业务流程。
- 不要让路径初始化隐式创建默认工作区、安装用户配置或修改 Gateway 注册表。
- 不要通过当前文件的 `parent` / `parents` 向上猜测仓库根目录。
- 不要让新的运行时代码继续写入旧目录 `~/.boxteam/` 或已废弃的工作区顶层会话数据目录。

# 规范

- 全局根目录统一为 `${BOXTEAM_HOME:-~/.boxteams}/`，工作区业务根目录统一为 `${workspace_abs_path}/.boxteam/`。
- 路径函数应返回明确的绝对路径；调用方必须显式提供工作区路径或从受控运行时状态取得它。
- 工作区目录初始化只处理当前显式工作区，不产生与该工作区无关的全局副作用。
- 禁止业务调用方把 session ID 直接拼到 `.boxteam/sessions/` 后定位会话；会话路径必须由权威索引解析，并校验对应物理目录。
- 迁移必须先检查目标、不得覆盖新数据；无法可靠归属到会话的旧数据应移入 `.boxteam/orphaned/` 并保留可诊断信息。
- 失败时抛出包含源路径、目标路径和操作阶段的明确异常，不得返回虚假默认路径。
