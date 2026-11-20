-- 固定来源：cb90835523b9ff6d91e3f8dfaf3491855d55191d，SessionCatalogStore.SCHEMA_VERSION=10。
-- SQL 按原 _initialize 顺序冻结；仅用于离线字段转换单元测试，不代表完整历史产品验收。

-- 来源：app/core/session_catalog_store/sql/nodes.sql
-- sha256: afd0460bcacab0188f747ae4d70283b479291508cff67d06cd7bd3172d124340

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
    session_kind TEXT CHECK (session_kind IN ('normal', 'context_fork')),
    current_agent_id TEXT CHECK (length(current_agent_id) > 0),
    current_provider_id TEXT CHECK (current_provider_id IS NULL OR length(current_provider_id) > 0),
    context_source_session_id TEXT,
    generation_origin_json TEXT CHECK (generation_origin_json IS NULL OR json_valid(generation_origin_json)),
    updated_at TEXT,
    CHECK (
        (kind = 'session'
            AND created_at IS NOT NULL
            AND storage_relative_locator IS NOT NULL
            AND main_thread_id IS NOT NULL
            AND title_source IS NOT NULL
            AND session_kind IS NOT NULL
            AND current_agent_id IS NOT NULL
            AND updated_at IS NOT NULL
            AND ((session_kind = 'normal' AND context_source_session_id IS NULL)
                 OR (session_kind = 'context_fork' AND context_source_session_id IS NOT NULL
                     AND length(context_source_session_id) > 0)))
        OR (kind = 'folder'
            AND created_at IS NULL
            AND storage_relative_locator IS NULL
            AND main_thread_id IS NULL
            AND title_source IS NULL
            AND session_kind IS NULL
            AND current_agent_id IS NULL
            AND current_provider_id IS NULL
            AND context_source_session_id IS NULL
            AND generation_origin_json IS NULL
            AND updated_at IS NULL)
    ),
    UNIQUE (workspace_id, main_thread_id),
    UNIQUE (workspace_id, storage_relative_locator)
);

-- 来源：app/core/session_catalog_store/sql/idx_nodes_parent.sql
-- sha256: e716a57669e7f13ab2627de6e5db60eea30221a894a217d626efac7693a14d69
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_node_id);

-- 来源：app/core/session_catalog_store/sql/idx_nodes_workspace.sql
-- sha256: 17ed9139445d841e6980bf94680921477f754f0d4f7da9e1c0d62068507caf43
CREATE INDEX IF NOT EXISTS idx_nodes_workspace ON nodes(workspace_id);

-- 来源：app/core/session_catalog_store/sql/idx_nodes_session_list.sql
-- sha256: e26bbb71a569d92a0feaaad60f50f007e851096e47a0d3b0b340fe4d434ee63e
CREATE INDEX IF NOT EXISTS idx_nodes_session_list ON nodes(session_list_created_us(created_at) DESC, node_id ASC, parent_node_id, session_list_member_part(node_id, created_at, 0), session_list_member_part(node_id, created_at, 1), session_list_member_part(node_id, created_at, 2), session_list_member_part(node_id, created_at, 3), session_list_member_part(node_id, created_at, 4), session_list_member_part(node_id, created_at, 5), session_list_member_part(node_id, created_at, 6), session_list_member_part(node_id, created_at, 7)) WHERE kind = 'session' AND state = 'active';

-- 来源：app/core/session_catalog_store/sql/session_creation_records.sql
-- sha256: c2a3033192e7734751b9d3434e60c08afa000f412e466ce37cab924d2b68ebcd

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
    fork_delivery_request_id TEXT CHECK(fork_delivery_request_id IS NULL OR (purpose='context_fork' AND length(fork_delivery_request_id)>0)),
    created_at TEXT NOT NULL,
    storage_relative_locator TEXT NOT NULL UNIQUE,
    preimage_hash TEXT NOT NULL,
    parent_revision INTEGER,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'published', 'aborted')),
    abort_reason TEXT,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL
);

-- 来源：app/core/session_catalog_store/sql/fork_source_captures.sql
-- sha256: b52079c40f40dd2762da3099bf0c40d9f8d0872f7d92fd72aa6c66d3a80875b7

CREATE TABLE IF NOT EXISTS fork_source_captures (
    copy_operation_id TEXT PRIMARY KEY,
    creation_key TEXT UNIQUE,
    source_session_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL UNIQUE,
    snapshot_relative_locator TEXT NOT NULL UNIQUE,
    fork_id TEXT NOT NULL UNIQUE,
    materialization_id TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'source_captured', 'materialized', 'aborted')),
    snapshot_sha256 TEXT,
    marker_sha256 TEXT,
    abort_reason TEXT,
    export_release_state TEXT CHECK (export_release_state IN ('pending', 'confirmed')),
    export_release_error TEXT,
    export_release_receipt_json TEXT,
    CHECK (
        (export_release_state IS NULL AND export_release_error IS NULL AND export_release_receipt_json IS NULL)
        OR (export_release_state = 'pending' AND export_release_receipt_json IS NULL)
        OR (export_release_state = 'confirmed' AND export_release_error IS NULL AND export_release_receipt_json IS NOT NULL)
    )
);

-- 来源：app/core/session_catalog_store/sql/idx_creation_records_state.sql
-- sha256: a5a29d4794e723c6d25a0dd3b9c7db460dd2ac10053686b9583772106bfbe0dd
CREATE INDEX IF NOT EXISTS idx_creation_records_state ON session_creation_records(state);

-- 来源：app/core/session_catalog_store/sql/subtree_delete_records.sql
-- sha256: b88d34f8636d2de34355d46d7b34f2773ad45b88469c4325b7c462c4c5398f5b

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
);

-- 来源：app/core/session_catalog_store/sql/idx_subtree_delete_state.sql
-- sha256: 84ab93b8d14f6b939adfa72eaa8288fe6eb1e3f6c6ddb132d92d2dea8324e40e
CREATE INDEX IF NOT EXISTS idx_subtree_delete_state ON subtree_delete_records(state);

-- 来源：app/core/session_catalog_store/sql/fork_retention_claims.sql
-- sha256: e9c2e07374973d6a6045b6da0f56244a152e253952a0a156c57a53755b60e7a0

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
);

-- 来源：app/core/session_catalog_store/sql/idx_fork_retention_source.sql
-- sha256: 5aa6ffd9103038521de400d45bf9c46406947266daa84f108ba04f3da0572edf
CREATE INDEX IF NOT EXISTS idx_fork_retention_source ON fork_retention_claims(workspace_id, source_session_id, state);

-- 来源：app/core/session_catalog_store/sql/catalog_metadata.sql
-- sha256: 623e0a87357d77262fddf5bc1a2c8f8483e58abfe3891c58f669ae0f89f83173

CREATE TABLE IF NOT EXISTS catalog_metadata (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    generation INTEGER NOT NULL,
    navigation_revision INTEGER NOT NULL
);

-- 来源：app/core/session_catalog_store/sql/nodes_navigation_revision_insert.sql
-- sha256: 305137e74bf9d03f4d2adabd077e971c619da56c05aa32d32350ad660b79e30a

    CREATE TRIGGER IF NOT EXISTS nodes_navigation_revision_insert
    AFTER INSERT ON nodes
    BEGIN
        UPDATE catalog_metadata
        SET navigation_revision = navigation_revision + 1
        WHERE singleton_id = 1;
        SELECT CASE WHEN changes() != 1
            THEN RAISE(ABORT, 'session catalog navigation revision row missing')
        END;
    END;

-- 来源：app/core/session_catalog_store/sql/nodes_navigation_revision_update.sql
-- sha256: d58cec6b4dc9f6395c7b9fcea3ac45bdc85a4044d35c38f802048a13153e38fd

    CREATE TRIGGER IF NOT EXISTS nodes_navigation_revision_update
    AFTER UPDATE OF node_id, kind, parent_node_id, display_name, title_source,
                    state, revision,
                    workspace_id, created_at, storage_relative_locator, main_thread_id,
                    session_kind, current_agent_id, current_provider_id,
                    context_source_session_id, generation_origin_json, updated_at
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
      OR OLD.session_kind IS NOT NEW.session_kind
      OR OLD.current_agent_id IS NOT NEW.current_agent_id
      OR OLD.current_provider_id IS NOT NEW.current_provider_id
      OR OLD.context_source_session_id IS NOT NEW.context_source_session_id
      OR OLD.generation_origin_json IS NOT NEW.generation_origin_json
      OR OLD.updated_at IS NOT NEW.updated_at
    BEGIN
        UPDATE catalog_metadata
        SET navigation_revision = navigation_revision + 1
        WHERE singleton_id = 1;
        SELECT CASE WHEN changes() != 1
            THEN RAISE(ABORT, 'session catalog navigation revision row missing')
        END;
    END;

-- 来源：app/core/session_catalog_store/sql/nodes_navigation_revision_delete.sql
-- sha256: 6ae320a189c39d65d347b919f4f4fae6d658ecb49683fa3948ef503432ee4c3e

    CREATE TRIGGER IF NOT EXISTS nodes_navigation_revision_delete
    AFTER DELETE ON nodes
    BEGIN
        UPDATE catalog_metadata
        SET navigation_revision = navigation_revision + 1
        WHERE singleton_id = 1;
        SELECT CASE WHEN changes() != 1
            THEN RAISE(ABORT, 'session catalog navigation revision row missing')
        END;
    END;

-- 来源：app/core/session_catalog_store/sql/navigation_queue/navigation_mutation_records.sql
-- sha256: d992419bc5f3da9272f844a2191f05f80b357febb734b1d92d277b2fd8e5dd34

CREATE TABLE IF NOT EXISTS navigation_mutation_records (
    gateway_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    client_sequence INTEGER NOT NULL,
    queue_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'committed', 'rejected', 'cancelled',
                  'dependency_failed')
    ),
    params_json TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    created_by_operation_id TEXT,
    target_node_id TEXT,
    reserved_node_id TEXT,
    result_node_id TEXT,
    result_node_revision INTEGER,
    committed_catalog_revision INTEGER,
    error_code TEXT,
    error_detail TEXT,
    pending_settlement INTEGER NOT NULL DEFAULT 0,
    holder_id TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    receipt_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, workspace_id, actor, operation_id)
);



-- 来源：app/core/session_catalog_store/sql/navigation_queue/navigation_events.sql
-- sha256: d32c037127f0de59dcf492393de971c8bc666c1a41c340829ac5c132f22a232f

CREATE TABLE IF NOT EXISTS navigation_events (
    workspace_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    queue_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    result_state TEXT NOT NULL,
    committed_catalog_revision INTEGER,
    affected_node_ids_json TEXT NOT NULL,
    error_code TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, event_seq)
);

-- 来源：app/core/session_catalog_store/sql/navigation_queue/navigation_queue_owners.sql
-- sha256: 7c9c8874e207028cb501786ba5058c71828348b940884a5dede2bdf300fd7fbd

CREATE TABLE IF NOT EXISTS navigation_queue_owners (
    workspace_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    owner_generation INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

PRAGMA user_version=10;
