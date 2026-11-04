-- 资源 owner 与连接身份分离；存量 payload 必须先经显式身份维护核验。
ALTER TABLE gateway_workspace_registry RENAME TO workspace_registry_identity_source;
CREATE TABLE gateway_workspace_registry (
    gateway_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, workspace_id)
);
INSERT INTO gateway_workspace_registry
SELECT json_extract(payload_json, '$.gateway_id'), json_extract(payload_json, '$.workspace_id'),
       MIN(position), MAX(active), payload_json, MAX(updated_at)
FROM workspace_registry_identity_source
GROUP BY json_extract(payload_json, '$.gateway_id'), json_extract(payload_json, '$.workspace_id');
ALTER TABLE user_view_state RENAME TO user_view_identity_source;
CREATE TABLE user_view_state (
    user_id TEXT NOT NULL REFERENCES user_account(user_id) ON DELETE CASCADE,
    gateway_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_anchor TEXT,
    scroll_offset REAL NOT NULL DEFAULT 0,
    follow_latest INTEGER NOT NULL DEFAULT 0,
    projection_version INTEGER NOT NULL DEFAULT 1,
    tool_details_expanded INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, gateway_id, workspace_id, session_id)
);
INSERT INTO user_view_state
SELECT v.user_id, json_extract(r.payload_json, '$.gateway_id'), json_extract(r.payload_json, '$.workspace_id'),
       v.session_id, v.turn_anchor, v.scroll_offset, v.follow_latest,
       v.projection_version, v.tool_details_expanded, v.updated_at
FROM user_view_identity_source v LEFT JOIN workspace_registry_identity_source r ON r.workspace_id = v.workspace_id;
DROP TABLE user_view_identity_source;
DROP TABLE workspace_registry_identity_source;
