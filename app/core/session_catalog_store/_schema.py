"""session-catalog.sqlite 的 DDL、形态正则与共享列清单（单点定义）。

本模块只承载表结构常量与 canonical ID / locator 形态正则，不含任何读写
逻辑；各垂直链路模块与 facade 从这里取用，不重复定义 DDL。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import re

# canonical ID profile：前缀 + 32 位小写 hex，恰好 36 个 ASCII byte。
_SESSION_ID_PATTERN = re.compile(r"ses_[0-9a-f]{32}")
_THREAD_ID_PATTERN = re.compile(r"thr_[0-9a-f]{32}")
# storage locator 形态：sessions/YYYY/MM/DD/{session_id}。
_STORAGE_LOCATOR_PATTERN = re.compile(
    r"sessions/([0-9]{4})/([0-9]{2})/([0-9]{2})/(ses_[0-9a-f]{32})"
)

# UUIDv7 位 profile：payload 第 13 个 hex（0 基 index 12）固定为 '7'，
# 第 17 个 hex（index 16）必须属于 variant 一组 '8'|'9'|'a'|'b'。
_UUID_VERSION_HEX_INDEX = 12
_UUID_VARIANT_HEX_INDEX = 16
_UUID_VARIANT_HEX_CHARS = "89ab"

# 路径预算：每组件 ≤255 bytes、总长 ≤4096 bytes（落盘前由 resolver 校验）。
_MAX_PATH_COMPONENT_BYTES = 255
_MAX_PATH_TOTAL_BYTES = 4096

_LOCATOR_PREFIX = "sessions/"

_NODE_COLUMNS = (
    "node_id, kind, parent_node_id, display_name, title_source, state, revision, "
    "workspace_id, created_at, storage_relative_locator, main_thread_id"
)

_NODES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'session')),
    parent_node_id TEXT REFERENCES nodes(node_id),
    display_name TEXT NOT NULL,
    title_source TEXT CHECK (title_source IN ('default', 'user', 'auto')),
    state TEXT NOT NULL CHECK (state IN ('active', 'deleting')),
    revision INTEGER NOT NULL DEFAULT 1,
    workspace_id TEXT NOT NULL,
    created_at TEXT,
    storage_relative_locator TEXT,
    main_thread_id TEXT,
    CHECK (
        (kind = 'session'
            AND created_at IS NOT NULL
            AND storage_relative_locator IS NOT NULL
            AND main_thread_id IS NOT NULL
            AND title_source IS NOT NULL)
        OR (kind = 'folder'
            AND created_at IS NULL
            AND storage_relative_locator IS NULL
            AND main_thread_id IS NULL
            AND title_source IS NULL)
    ),
    UNIQUE (workspace_id, main_thread_id),
    UNIQUE (workspace_id, storage_relative_locator)
)
"""

_IDX_NODES_PARENT_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_node_id)"
)
_IDX_NODES_WORKSPACE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_nodes_workspace ON nodes(workspace_id)"
)

# SessionCreationRecord journal 表（8.1-A）：与导航 node 同库、不是第二权威。
# record 冻结 Session 身份/locator/preimage/父节点 revision；publish 是唯一
# 可见性提交点（node 行在本表同事务内插入）。
_CREATION_RECORDS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS session_creation_records (
    session_creation_idempotency_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    main_thread_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    parent_node_id TEXT,
    display_name TEXT NOT NULL,
    title_source TEXT NOT NULL CHECK (title_source IN ('default', 'user', 'auto')),
    purpose TEXT NOT NULL CHECK (purpose IN ('normal', 'context_fork')),
    graph_id TEXT NOT NULL CHECK (length(graph_id) > 0),
    graph_revision INTEGER NOT NULL CHECK (graph_revision >= 1),
    graph_schema_hash TEXT NOT NULL,
    capability_profile_hash TEXT NOT NULL,
    session_metadata_json TEXT NOT NULL,
    request_intent_json TEXT,
    created_at TEXT NOT NULL,
    storage_relative_locator TEXT NOT NULL UNIQUE,
    preimage_hash TEXT NOT NULL,
    parent_revision INTEGER,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'published', 'aborted')),
    abort_reason TEXT,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL
)
"""

_IDX_CREATION_RECORDS_STATE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_creation_records_state "
    "ON session_creation_records(state)"
)

_CREATION_RECORD_COLUMNS = (
    "session_creation_idempotency_key, session_id, main_thread_id, workspace_id, "
    "parent_node_id, display_name, title_source, purpose, "
    "graph_id, graph_revision, graph_schema_hash, capability_profile_hash, "
    "session_metadata_json, request_intent_json, "
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
_SUBTREE_DELETE_RECORDS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS subtree_delete_records (
    subtree_delete_idempotency_key TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    root_node_id TEXT NOT NULL,
    frozen_node_ids TEXT NOT NULL,
    frozen_session_locators TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'deleting', 'draining', 'completed', 'aborted')),
    abort_reason TEXT,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL,
    drained_session_ids TEXT NOT NULL DEFAULT '[]'
)
"""

_IDX_SUBTREE_DELETE_STATE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_subtree_delete_state "
    "ON subtree_delete_records(state)"
)

# ForkRetentionClaim 表（8.1-D）：pinned fork 在 source capture 前的 durable
# retention 占位，与导航 node 同库、不是第二权威。claim 准入与整树删除在
# **同一个 SQLite 写事务序列**上竞争（同一 DB 的 BEGIN IMMEDIATE），因此
# 「claim 先行则删除返回 blocker」与「删除先行则 claim 零副作用失败」不需要
# 任何额外的本地 fence 窗口。``state`` 闭集 preparing/active/released；
# ``fork_retention_claim_id`` 即 fork_id（uuid hex）。claim 自身按
# ``operation_kind=fork_retention`` 承担 Session operation lease（由
# session-control.sqlite 侧的 lease 承载，本表只保存导航层可见的占位）。
_FORK_RETENTION_CLAIMS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS fork_retention_claims (
    fork_retention_claim_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    source_lifecycle_generation INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'active', 'released')),
    release_reason TEXT,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL
)
"""

_IDX_FORK_RETENTION_SOURCE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_fork_retention_source "
    "ON fork_retention_claims(workspace_id, source_session_id, state)"
)

_FORK_RETENTION_CLAIM_COLUMNS = (
    "fork_retention_claim_id, workspace_id, source_session_id, "
    "target_session_id, source_lifecycle_generation, state, release_reason, "
    "record_created_at, record_updated_at"
)

# catalog 单调 generation（8.1-F）：每个写事务提交时自增一次，作为可校验
# 一致性快照/备份的版本锚点。备份清单记录 snapshot 时刻的 generation 与
# sqlite 文件 checksum；当前 generation 大于备份 generation 即「备份落后于
# 已提交操作」，只能进入维护模式核对。导航 revision 单独跟踪 nodes 真实变化。
_CATALOG_METADATA_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS catalog_metadata (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    generation INTEGER NOT NULL,
    navigation_revision INTEGER NOT NULL
)
"""

_NAVIGATION_REVISION_TRIGGER_DDLS = (
    """
    CREATE TRIGGER IF NOT EXISTS nodes_navigation_revision_insert
    AFTER INSERT ON nodes
    BEGIN
        UPDATE catalog_metadata
        SET navigation_revision = navigation_revision + 1
        WHERE singleton_id = 1;
        SELECT CASE WHEN changes() != 1
            THEN RAISE(ABORT, 'session catalog navigation revision row missing')
        END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS nodes_navigation_revision_update
    AFTER UPDATE OF node_id, kind, parent_node_id, display_name, title_source,
                    state, revision,
                    workspace_id, created_at, storage_relative_locator, main_thread_id
    ON nodes
    WHEN OLD.node_id IS NOT NEW.node_id
      OR OLD.kind IS NOT NEW.kind
      OR OLD.parent_node_id IS NOT NEW.parent_node_id
      OR OLD.display_name IS NOT NEW.display_name
      OR OLD.title_source IS NOT NEW.title_source
      OR OLD.state IS NOT NEW.state
      OR OLD.revision IS NOT NEW.revision
      OR OLD.workspace_id IS NOT NEW.workspace_id
      OR OLD.created_at IS NOT NEW.created_at
      OR OLD.storage_relative_locator IS NOT NEW.storage_relative_locator
      OR OLD.main_thread_id IS NOT NEW.main_thread_id
    BEGIN
        UPDATE catalog_metadata
        SET navigation_revision = navigation_revision + 1
        WHERE singleton_id = 1;
        SELECT CASE WHEN changes() != 1
            THEN RAISE(ABORT, 'session catalog navigation revision row missing')
        END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS nodes_navigation_revision_delete
    AFTER DELETE ON nodes
    BEGIN
        UPDATE catalog_metadata
        SET navigation_revision = navigation_revision + 1
        WHERE singleton_id = 1;
        SELECT CASE WHEN changes() != 1
            THEN RAISE(ABORT, 'session catalog navigation revision row missing')
        END;
    END
    """,
)

_REQUIRED_NAVIGATION_REVISION_TRIGGERS = (
    "nodes_navigation_revision_insert",
    "nodes_navigation_revision_update",
    "nodes_navigation_revision_delete",
)

# 已登记为 v7 的目录缺少权威表、元数据列或 revision triggers 说明 catalog
# 被外部改写，必须在任何 DDL 前 fail closed。旧 schema 不作运行期迁移；只能由
# 明确维护流程处理，不能把日期目录或旧 JSON 当作回退来源。
_REQUIRED_TABLES_BY_VERSION = {
    7: (
        "nodes",
        "session_creation_records",
        "subtree_delete_records",
        "fork_retention_claims",
        "catalog_metadata",
    ),
}

# 全新 v7 catalog 的备份 generation 与导航 revision 都从 0 起。
_CATALOG_METADATA_SEED = (
    "INSERT INTO catalog_metadata "
    "(singleton_id, generation, navigation_revision) VALUES (1, 0, 0)"
)

_SUBTREE_DELETE_RECORD_COLUMNS = (
    "subtree_delete_idempotency_key, workspace_id, root_node_id, "
    "frozen_node_ids, frozen_session_locators, state, abort_reason, "
    "record_created_at, record_updated_at, drained_session_ids"
)
