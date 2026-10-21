
CREATE TABLE IF NOT EXISTS federation_replay_registry (
    issuer_gateway_id TEXT NOT NULL,
    origin_gateway_id TEXT NOT NULL,
    audience_gateway_id TEXT NOT NULL,
    grant_kind TEXT NOT NULL,
    nonce TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (
        issuer_gateway_id, origin_gateway_id, audience_gateway_id,
        grant_kind, nonce
    )
);
CREATE TABLE IF NOT EXISTS federation_channel_registry (
    channel_instance_id TEXT PRIMARY KEY,
    peer_gateway_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    channel_epoch INTEGER NOT NULL,
    direction TEXT NOT NULL,
    connected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_route_hint (
    gateway_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    resolved_main_thread_id TEXT NOT NULL,
    catalog_revision TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, workspace_id, session_id)
);
