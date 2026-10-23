"""session-catalog.sqlite 的 DDL、locator 正则与共享列清单（单点定义）。

本模块只承载表结构常量与 locator 形态正则，不含任何读写
逻辑；各垂直链路模块与 facade 从这里取用，不重复定义 DDL。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import re

from app.core.sqlite_state import load_sql

# storage locator 形态：sessions/YYYY/MM/DD/{session_id}。
_STORAGE_LOCATOR_PATTERN = re.compile(
    r"sessions/([0-9]{4})/([0-9]{2})/([0-9]{2})/(ses_[0-9a-f]{32})"
)


# 路径预算：每组件 ≤255 bytes、总长 ≤4096 bytes（落盘前由 resolver 校验）。
_MAX_PATH_COMPONENT_BYTES = 255
_MAX_PATH_TOTAL_BYTES = 4096

_LOCATOR_PREFIX = "sessions/"

_NODE_COLUMNS = (
    "node_id, kind, parent_node_id, display_name, title_source, state, revision, "
    "workspace_id, created_at, storage_relative_locator, main_thread_id, "
    "session_kind, current_agent_id, current_provider_id, context_source_session_id, "
    "generation_origin_json, updated_at"
)

_NODES_TABLE_DDL = load_sql(__package__, "sql/nodes.sql")

_IDX_NODES_PARENT_DDL = load_sql(__package__, "sql/idx_nodes_parent.sql")
_IDX_NODES_WORKSPACE_DDL = load_sql(__package__, "sql/idx_nodes_workspace.sql")

# 页读取只消费既有 nodes 权威。微秒排序键与256bit成员指纹的8个分量
# 在同一表达式索引写入时派生；COUNT/SUM 无需逐成员 Python 回调。
_SESSION_LIST_INDEX_NAME = "idx_nodes_session_list"
_SESSION_LIST_FINGERPRINT_PARTS = 8
_IDX_NODES_SESSION_LIST_DDL = load_sql(__package__, "sql/idx_nodes_session_list.sql")

# SessionCreationRecord journal 表（8.1-A）：与导航 node 同库、不是第二权威。
# record 冻结 Session 身份/locator/preimage/父节点 revision；publish 是唯一
# 可见性提交点（node 行在本表同事务内插入）。
_CREATION_RECORDS_TABLE_DDL = load_sql(__package__, "sql/session_creation_records.sql")

_FORK_SOURCE_CAPTURES_TABLE_DDL = load_sql(__package__, "sql/fork_source_captures.sql")

# 导航队列与node事实同库：物理结构由catalog一次初始化，业务owner不补建空表。
_NAVIGATION_QUEUE_SCHEMA_OBJECTS = (
    "navigation_mutation_records", "idx_navigation_mutation_queue",
    "idx_navigation_mutation_pending", "navigation_events", "navigation_queue_owners",
)
_NAVIGATION_QUEUE_DDLS = tuple(
    load_sql(__package__, f"sql/navigation_queue/{name}.sql") for name in _NAVIGATION_QUEUE_SCHEMA_OBJECTS
)

_IDX_CREATION_RECORDS_STATE_DDL = load_sql(__package__, "sql/idx_creation_records_state.sql")

_CREATION_RECORD_COLUMNS = (
    "session_creation_idempotency_key, session_id, main_thread_id, workspace_id, "
    "parent_node_id, display_name, title_source, purpose, "
    "graph_id, graph_revision, graph_schema_hash, capability_profile_hash, "
    "session_metadata_json, request_intent_json, fork_delivery_request_id, "
    "created_at, storage_relative_locator, "
    "preimage_hash, parent_revision, state, abort_reason, "
    "record_created_at, record_updated_at"
)

# NavigationSubtreeDeleteRecord journal 表（8.1-B）：与导航 node 同库、不是
# 第二权威。record 冻结递归 CTE 得到的精确子树 (node_id, revision) 集合与
# 每个 session 的不可变 locator；mark 的单事务 CAS 整树 active→deleting 是
# 唯一逻辑可见性关闭点；``drained_session_ids`` 追加已物理隔离 session
# （JSON 数组，恢复时按此定点继续）。R14 起本表随 ``_initialize`` 加法补建
# （``CREATE TABLE IF NOT EXISTS`` 对既有 v2 库幂等，不改 user_version）。
_SUBTREE_DELETE_RECORDS_TABLE_DDL = load_sql(__package__, "sql/subtree_delete_records.sql")

_IDX_SUBTREE_DELETE_STATE_DDL = load_sql(__package__, "sql/idx_subtree_delete_state.sql")

# ForkRetentionClaim 表（8.1-D）：pinned fork 在 source capture 前的 durable
# retention 占位，与导航 node 同库、不是第二权威。claim 准入与整树删除在
# **同一个 SQLite 写事务序列**上竞争（同一 DB 的 BEGIN IMMEDIATE），因此
# 「claim 先行则删除返回 blocker」与「删除先行则 claim 零副作用失败」不需要
# 任何额外的本地 fence 窗口。``state`` 闭集 preparing/active/released；
# ``fork_retention_claim_id`` 即 fork_id（uuid hex）。claim 自身按
# ``operation_kind=fork_retention`` 承担 Session operation lease（由
# session-control.sqlite 侧的 lease 承载，本表只保存导航层可见的占位）。
_FORK_RETENTION_CLAIMS_TABLE_DDL = load_sql(__package__, "sql/fork_retention_claims.sql")

_IDX_FORK_RETENTION_SOURCE_DDL = load_sql(__package__, "sql/idx_fork_retention_source.sql")

_FORK_RETENTION_CLAIM_COLUMNS = (
    "fork_retention_claim_id, workspace_id, source_session_id, "
    "target_session_id, source_lifecycle_generation, state, release_reason, "
    "record_created_at, record_updated_at"
)

# catalog 单调 generation（8.1-F）：每个写事务提交时自增一次，作为可校验
# 一致性快照/备份的版本锚点。备份清单记录 snapshot 时刻的 generation 与
# sqlite 文件 checksum；当前 generation 大于备份 generation 即「备份落后于
# 已提交操作」，只能进入维护模式核对。导航 revision 单独跟踪 nodes 真实变化。
_CATALOG_METADATA_TABLE_DDL = load_sql(__package__, "sql/catalog_metadata.sql")

_NAVIGATION_REVISION_TRIGGER_DDLS = (
    load_sql(__package__, "sql/nodes_navigation_revision_insert.sql"),
    load_sql(__package__, "sql/nodes_navigation_revision_update.sql"),
    load_sql(__package__, "sql/nodes_navigation_revision_delete.sql"),
)

_REQUIRED_NAVIGATION_REVISION_TRIGGERS = (
    "nodes_navigation_revision_insert",
    "nodes_navigation_revision_update",
    "nodes_navigation_revision_delete",
)

# 已登记为当前版本 的目录缺少权威表、元数据列或 revision triggers 说明 catalog
# 被外部改写，必须在任何 DDL 前 fail closed。旧 schema 不作运行期迁移；只能由
# 明确维护流程处理，不能把日期目录或旧 JSON 当作回退来源。
_REQUIRED_TABLES = (
    "nodes",
    "session_creation_records",
    "fork_source_captures",
    "subtree_delete_records",
    "fork_retention_claims",
    "catalog_metadata",
    "navigation_mutation_records",
    "navigation_events",
    "navigation_queue_owners",
)

# 全新 catalog 的备份 generation 与导航 revision 都从 0 起。
_CATALOG_METADATA_SEED = (
    load_sql(__package__, "sql/catalog_metadata_seed.sql")
)

_SUBTREE_DELETE_RECORD_COLUMNS = (
    "subtree_delete_idempotency_key, workspace_id, root_node_id, "
    "frozen_node_ids, frozen_session_locators, state, abort_reason, "
    "record_created_at, record_updated_at, drained_session_ids"
)
