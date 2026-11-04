-- 来源：bf0f2e1d9 的完整 Git archive；由产品登记、共享组、用户视图 owner 生成。
BEGIN TRANSACTION;
CREATE TABLE config_active_snapshot (
        config_domain TEXT PRIMARY KEY,
        active_revision INTEGER NOT NULL,
        candidate_id TEXT,
        payload_json TEXT NOT NULL,
        source_baseline_json TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        layer_revisions_json TEXT NOT NULL,
        layer_digests_json TEXT NOT NULL,
        effective_digest TEXT NOT NULL,
        secret_bindings_json TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        promoted_generation TEXT,
        promoted_apply_id TEXT,
        promoted_at TEXT NOT NULL
    , state TEXT NOT NULL DEFAULT 'active', last_error TEXT);
CREATE TABLE config_apply_claim (
        config_domain TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        apply_id TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        base_active_revision INTEGER,
        target_generation TEXT,
        lease_expires_at TEXT NOT NULL,
        fencing_token TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
CREATE TABLE config_apply_journal (
        config_domain TEXT NOT NULL,
        apply_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        base_active_revision INTEGER,
        pending_revision INTEGER,
        source_baseline_json TEXT NOT NULL,
        active_baseline_json TEXT NOT NULL,
        registry_revision INTEGER,
        side_effects_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('applying', 'committed', 'failed',
                      'recovery_required', 'compensated')
        ),
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
CREATE TABLE config_event_relay_delivery (
        event_id TEXT NOT NULL REFERENCES config_events(event_id) ON DELETE CASCADE,
        consumer_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('claimed', 'delivered', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        claimed_until TEXT,
        next_attempt_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (event_id, consumer_id)
    );
CREATE TABLE config_events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        config_domain TEXT NOT NULL,
        candidate_id TEXT,
        attempt_id TEXT,
        apply_id TEXT,
        idempotency_key TEXT,
        commit_revision INTEGER,
        active_revision INTEGER,
        pending_revision INTEGER,
        source TEXT NOT NULL,
        result TEXT NOT NULL,
        changed_paths_json TEXT NOT NULL,
        applied_paths_json TEXT NOT NULL,
        deferred_paths_json TEXT NOT NULL,
        error TEXT,
        occurred_at TEXT NOT NULL
    , activation_scope TEXT NOT NULL DEFAULT 'unknown', relay_state TEXT NOT NULL DEFAULT 'pending', relay_attempts INTEGER NOT NULL DEFAULT 0, relay_last_error TEXT, relay_claimed_by TEXT, relay_claimed_until TEXT, relay_next_attempt_at TEXT
        NOT NULL DEFAULT '1970-01-01T00:00:00+00:00');
CREATE TABLE config_pending_candidate (
        config_domain TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        pending_revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        source_baseline_json TEXT NOT NULL,
        candidate_digest TEXT NOT NULL,
        effective_digest TEXT NOT NULL,
        target_generation TEXT,
        fencing_token TEXT,
        state TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        last_attempt_id TEXT,
        last_apply_id TEXT, base_active_revision INTEGER, source_generation INTEGER, secret_bindings_json TEXT
        NOT NULL DEFAULT '{}',
        PRIMARY KEY (config_domain, candidate_id),
        UNIQUE (config_domain, idempotency_key)
    );
CREATE TABLE config_revision_meta (
        config_domain TEXT PRIMARY KEY,
        next_revision INTEGER NOT NULL
    );
CREATE TABLE config_source_journal (
        source_key TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        source_event_id TEXT NOT NULL UNIQUE,
        presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
        layer_revision INTEGER NOT NULL,
        layer_digest TEXT,
        previous_digest TEXT,
        origin TEXT NOT NULL,
        fanout_id TEXT NOT NULL,
        created_at TEXT NOT NULL, uri TEXT,
        PRIMARY KEY (source_key, source_generation)
    );
CREATE TABLE config_source_layers (
        config_key TEXT PRIMARY KEY,
        presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
        config_version INTEGER NOT NULL,
        payload_json TEXT,
        layer_revision INTEGER NOT NULL,
        layer_digest TEXT,
        source_generation INTEGER NOT NULL,
        previous_digest TEXT,
        updated_at TEXT NOT NULL
    , previous_payload_json TEXT, uri TEXT);
CREATE TABLE config_source_owner (
        source_key TEXT PRIMARY KEY,
        next_generation INTEGER NOT NULL
    );
CREATE TABLE gateway_config (
        config_key TEXT PRIMARY KEY,
        config_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
INSERT INTO "gateway_config" VALUES('workspace_registry_meta',1,'{"active_workspace_id": "gw_local_0", "managed_backends": [{"backend_url": "http://127.0.0.1:49999", "debug_port": null, "generation": "generation_fixture", "members": [{"backend_workspace_id": "9c559036-31d2-48b3-bf29-50f34af43a54", "workspace_id": "gw_local_1"}, {"backend_workspace_id": "79588e63-9d63-4ffe-9ff0-405b054ec7ea", "workspace_id": "gw_local_2"}], "owner_id": "backend_fixture"}], "order_customized": false, "registry_revision": 9, "remote_gateway_connections": [{"connection_error": null, "connection_id": "rgw_fixture_0", "host": "fixture.invalid", "name": "测试连接", "port": 22, "private_key_path": null, "protocol_version": 1, "remote_candidate_ref": null, "remote_config_event_cursor": null, "remote_config_state": null, "remote_gateway_id": "gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "remote_gateway_port": 8014, "remote_pair_command": null, "remote_restart_required": false, "source_owner": "manual", "ssh_config_host": "fixture-host", "username": "fixture"}, {"connection_error": null, "connection_id": "rgw_fixture_1", "host": "fixture.invalid", "name": "测试连接", "port": 22, "private_key_path": null, "protocol_version": 1, "remote_candidate_ref": null, "remote_config_event_cursor": null, "remote_config_state": null, "remote_gateway_id": "gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "remote_gateway_port": 8014, "remote_pair_command": null, "remote_restart_required": false, "source_owner": "manual", "ssh_config_host": "fixture-host", "username": "fixture"}], "runtime_generation": null, "schema_version": 13}','2026-11-04T12:53:15.528141+00:00');
CREATE TABLE gateway_restart_intent (
        intent_id TEXT PRIMARY KEY,
        candidate_ref TEXT NOT NULL UNIQUE,
        candidate_id TEXT NOT NULL,
        base_active_revision INTEGER,
        old_generation TEXT,
        target_generation TEXT NOT NULL,
        fencing_token TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('pending', 'applying', 'active', 'failed',
                      'recovery_required', 'discarded')
        ),
        requested_by TEXT NOT NULL,
        health_proof_json TEXT,
        last_error TEXT,
        requested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    , gateway_id TEXT, expires_at TEXT);
CREATE TABLE gateway_runtime_generation (
        config_domain TEXT NOT NULL,
        generation_id TEXT PRIMARY KEY,
        process_id INTEGER,
        loaded_source TEXT NOT NULL CHECK (loaded_source IN ('active', 'pending')),
        candidate_id TEXT,
        active_revision INTEGER,
        pending_revision INTEGER,
        candidate_digest TEXT,
        effective_digest TEXT NOT NULL,
        secret_binding_digest TEXT,
        fencing_token TEXT,
        listener_state TEXT NOT NULL CHECK (
            listener_state IN ('reserved', 'serving', 'draining', 'closed')
        ),
        state TEXT NOT NULL CHECK (
            state IN ('starting', 'healthy', 'active', 'failed', 'closed')
        ),
        health_proof_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
CREATE TABLE gateway_workspace_registry (
        workspace_id TEXT PRIMARY KEY,
        position INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
INSERT INTO "gateway_workspace_registry" VALUES('gw_local_0',0,1,'{"active_request_count": 0, "active_stream_count": 0, "backend_url": "http://127.0.0.1:49999", "backend_workspace_id": "6b67f566-61c5-4848-921d-f546e59b1e07", "connection_error": null, "connection_id": null, "connection_kind": "local", "desired_running": false, "local_service_urls": {}, "managed": false, "name": "工作区 0", "name_customized": false, "owner": "manual", "parent_workspace_id": null, "remote_affected_workspace_ids": [], "remote_backend_generation": null, "remote_config_event_cursor": null, "remote_gateway_connection_id": null, "remote_service_names": [], "remote_workspace_id": null, "removable": true, "root_path": "__TEST_ROOT__/workspace-0", "runtime_lease_id": null, "system_default": false, "target_generation": "target_generation_2d43074321fa4e7ea789cb52261f5842", "target_namespace": "gateway", "workspace_id": "gw_local_0"}','2026-11-04T12:53:15.527910+00:00');
INSERT INTO "gateway_workspace_registry" VALUES('gw_local_1',1,0,'{"active_request_count": 0, "active_stream_count": 0, "backend_url": "http://127.0.0.1:49999", "backend_workspace_id": "9c559036-31d2-48b3-bf29-50f34af43a54", "connection_error": null, "connection_id": null, "connection_kind": "local", "desired_running": false, "local_service_urls": {}, "managed": true, "name": "工作区 1", "name_customized": false, "owner": "manual", "parent_workspace_id": null, "remote_affected_workspace_ids": [], "remote_backend_generation": null, "remote_config_event_cursor": null, "remote_gateway_connection_id": null, "remote_service_names": [], "remote_workspace_id": null, "removable": true, "root_path": "__TEST_ROOT__/workspace-1", "runtime_lease_id": null, "system_default": false, "target_generation": "target_generation_4cb96110ea3a4d0fafd43194f9294e0d", "target_namespace": "gateway", "workspace_id": "gw_local_1"}','2026-11-04T12:53:15.527983+00:00');
INSERT INTO "gateway_workspace_registry" VALUES('gw_local_2',2,0,'{"active_request_count": 0, "active_stream_count": 0, "backend_url": "http://127.0.0.1:49999", "backend_workspace_id": "79588e63-9d63-4ffe-9ff0-405b054ec7ea", "connection_error": null, "connection_id": null, "connection_kind": "local", "desired_running": false, "local_service_urls": {}, "managed": true, "name": "工作区 2", "name_customized": false, "owner": "manual", "parent_workspace_id": null, "remote_affected_workspace_ids": [], "remote_backend_generation": null, "remote_config_event_cursor": null, "remote_gateway_connection_id": null, "remote_service_names": [], "remote_workspace_id": null, "removable": true, "root_path": "__TEST_ROOT__/workspace-2", "runtime_lease_id": null, "system_default": false, "target_generation": "target_generation_061cdd51ff224c5b83c14fb2d13454f4", "target_namespace": "gateway", "workspace_id": "gw_local_2"}','2026-11-04T12:53:15.528018+00:00');
INSERT INTO "gateway_workspace_registry" VALUES('gw_remote_0',3,0,'{"active_request_count": 0, "active_stream_count": 0, "backend_url": "http://127.0.0.1:49998", "backend_workspace_id": "00000000-0000-4000-8000-000000000099", "connection_error": null, "connection_id": "rgw_fixture_0", "connection_kind": "remote_gateway", "desired_running": false, "local_service_urls": {}, "managed": false, "name": "同一远端工作区", "name_customized": false, "owner": "remote_projection", "parent_workspace_id": null, "remote_affected_workspace_ids": [], "remote_backend_generation": null, "remote_config_event_cursor": null, "remote_gateway_connection_id": "rgw_fixture_0", "remote_service_names": [], "remote_workspace_id": "gw_native", "removable": true, "runtime_lease_id": null, "system_default": false, "target_generation": "target_generation_88fe712840e248d8b4f6623cd17a5d7b", "target_namespace": "gateway", "workspace_id": "gw_remote_0"}','2026-11-04T12:53:15.528049+00:00');
INSERT INTO "gateway_workspace_registry" VALUES('gw_remote_1',4,0,'{"active_request_count": 0, "active_stream_count": 0, "backend_url": "http://127.0.0.1:49998", "backend_workspace_id": "00000000-0000-4000-8000-000000000099", "connection_error": null, "connection_id": "rgw_fixture_1", "connection_kind": "remote_gateway", "desired_running": false, "local_service_urls": {}, "managed": false, "name": "同一远端工作区", "name_customized": false, "owner": "remote_projection", "parent_workspace_id": null, "remote_affected_workspace_ids": [], "remote_backend_generation": null, "remote_config_event_cursor": null, "remote_gateway_connection_id": "rgw_fixture_1", "remote_service_names": [], "remote_workspace_id": "gw_native", "removable": true, "runtime_lease_id": null, "system_default": false, "target_generation": "target_generation_c90adc415c694336bb0bfba23ba8ad33", "target_namespace": "gateway", "workspace_id": "gw_remote_1"}','2026-11-04T12:53:15.528079+00:00');
CREATE TABLE guest_tracking (
        guest_id TEXT PRIMARY KEY,
        tracking_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
CREATE TABLE native_directory_roots (
        gateway_id TEXT NOT NULL,
        directory_id TEXT NOT NULL,
        native_root TEXT NOT NULL,
        PRIMARY KEY (gateway_id, directory_id),
        UNIQUE (gateway_id, native_root)
    );
CREATE TABLE registry_apply_journal (
        apply_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        base_revision INTEGER NOT NULL,
        target_revision INTEGER,
        state TEXT NOT NULL CHECK (
            state IN ('applying', 'committed', 'failed', 'recovery_required')
        ),
        payload_digest TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
INSERT INTO "registry_apply_journal" VALUES('registry_apply_fcab2ea652764a28ac310ff66b4f0162','manual',0,1,'committed','469268685704014bd64412bf0790eecf67ce9db043a826ffab7f8582bbd7f350',NULL,'2026-11-04T12:53:15.467725+00:00','2026-11-04T12:53:15.470019+00:00');
INSERT INTO "registry_apply_journal" VALUES('registry_apply_8480c1c34ece4bb39d208dfeac17f20c','manual',1,2,'committed','96ea643a377b769035454316ec32664714deb0b780e72b5d11fc3e2abd70189e',NULL,'2026-11-04T12:53:15.474177+00:00','2026-11-04T12:53:15.476678+00:00');
INSERT INTO "registry_apply_journal" VALUES('registry_apply_2e08e645daa740d3949a67db7c585d12','manual',2,3,'committed','a7897da1a890c8d39cee907170430c70d1002d4e72e1ea73a1c7dd37f9b3508d',NULL,'2026-11-04T12:53:15.480765+00:00','2026-11-04T12:53:15.483977+00:00');
INSERT INTO "registry_apply_journal" VALUES('registry_apply_e4acb6138ad34ce9a47be87b626afec8','system',3,4,'committed','0dca34984d2fd3811d5889c21b51e8d939ed49a40afde8fcf28f793aff62c827',NULL,'2026-11-04T12:53:15.486792+00:00','2026-11-04T12:53:15.490195+00:00');
INSERT INTO "registry_apply_journal" VALUES('registry_apply_0cfa242cafb143da95ee9ebb2861a1bf','remote_projection',4,5,'committed','abd69e1826cde401209e43e4184ff3ebd034c549a6b6f7d08cd175af509b5139',NULL,'2026-11-04T12:53:15.502948+00:00','2026-11-04T12:53:15.505731+00:00');
INSERT INTO "registry_apply_journal" VALUES('registry_apply_cec54956b78a437c8d1bbc77ba063e4e','remote_projection',5,6,'committed','32045204bc6281dc310ecf4c7b6e783af1d36109b1032d57dfe0e073f398f330',NULL,'2026-11-04T12:53:15.508418+00:00','2026-11-04T12:53:15.510957+00:00');
INSERT INTO "registry_apply_journal" VALUES('registry_apply_10258afdcbcb482da344ce81f144207f','remote_projection',6,7,'committed','c3fdc2364262ace3e3977729e7af1330cd589905a09cf80434a95fa786ab4ca8',NULL,'2026-11-04T12:53:15.513554+00:00','2026-11-04T12:53:15.516207+00:00');
INSERT INTO "registry_apply_journal" VALUES('registry_apply_63b751c072984145ba376266afbd3925','remote_projection',7,8,'committed','cb5758b91cfadd23d4ccfd214476892fb58f06340dc32b0554b56d91737867de',NULL,'2026-11-04T12:53:15.518800+00:00','2026-11-04T12:53:15.521735+00:00');
INSERT INTO "registry_apply_journal" VALUES('registry_apply_f88481483a8745cbb3b7fe2486557df2','manual_crud',8,9,'committed','587a351fffa754bcb6d9933adcc1ac499a1454d58b1cac76a5d076d9f98a0cab',NULL,'2026-11-04T12:53:15.525419+00:00','2026-11-04T12:53:15.528186+00:00');
CREATE TABLE registry_meta (
        registry_key TEXT PRIMARY KEY,
        revision INTEGER NOT NULL
    );
INSERT INTO "registry_meta" VALUES('workspace',9);
CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
INSERT INTO "schema_migrations" VALUES(1,'2026-11-04T12:53:15.443276+00:00');
INSERT INTO "schema_migrations" VALUES(2,'2026-11-04T12:53:15.443980+00:00');
INSERT INTO "schema_migrations" VALUES(3,'2026-11-04T12:53:15.444158+00:00');
INSERT INTO "schema_migrations" VALUES(4,'2026-11-04T12:53:15.444810+00:00');
INSERT INTO "schema_migrations" VALUES(5,'2026-11-04T12:53:15.445189+00:00');
INSERT INTO "schema_migrations" VALUES(6,'2026-11-04T12:53:15.445421+00:00');
INSERT INTO "schema_migrations" VALUES(7,'2026-11-04T12:53:15.445828+00:00');
INSERT INTO "schema_migrations" VALUES(8,'2026-11-04T12:53:15.447607+00:00');
INSERT INTO "schema_migrations" VALUES(9,'2026-11-04T12:53:15.447829+00:00');
INSERT INTO "schema_migrations" VALUES(10,'2026-11-04T12:53:15.448042+00:00');
INSERT INTO "schema_migrations" VALUES(11,'2026-11-04T12:53:15.448784+00:00');
INSERT INTO "schema_migrations" VALUES(12,'2026-11-04T12:53:15.450970+00:00');
INSERT INTO "schema_migrations" VALUES(13,'2026-11-04T12:53:15.451245+00:00');
INSERT INTO "schema_migrations" VALUES(14,'2026-11-04T12:53:15.451683+00:00');
INSERT INTO "schema_migrations" VALUES(15,'2026-11-04T12:53:15.452512+00:00');
INSERT INTO "schema_migrations" VALUES(16,'2026-11-04T12:53:15.458369+00:00');
INSERT INTO "schema_migrations" VALUES(17,'2026-11-04T12:53:15.458428+00:00');
INSERT INTO "schema_migrations" VALUES(18,'2026-11-04T12:53:15.458654+00:00');
CREATE TABLE user_access_lease (
        user_id TEXT PRIMARY KEY REFERENCES user_account(user_id) ON DELETE CASCADE,
        lease_generation INTEGER NOT NULL,
        access_session_id TEXT NOT NULL UNIQUE,
        client_label TEXT,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
INSERT INTO "user_access_lease" VALUES('user_fixture',1,'access-NHwd5ygtskPxuLfMZjN8cX_MrISkxJnl','fixture-generator','2026-11-04T12:53:15.493917+00:00','2026-11-04T12:53:15.493917+00:00','2026-11-04T12:54:00.493917+00:00');
CREATE TABLE user_account (
        user_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
INSERT INTO "user_account" VALUES('user_fixture','迁移用户','2026-11-04T12:53:15.491888+00:00',NULL);
CREATE TABLE user_view_state (
        user_id TEXT NOT NULL REFERENCES user_account(user_id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_anchor TEXT,
        scroll_offset REAL NOT NULL DEFAULT 0,
        follow_latest INTEGER NOT NULL DEFAULT 0,
        projection_version INTEGER NOT NULL DEFAULT 1,
        tool_details_expanded INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, workspace_id, session_id)
    );
INSERT INTO "user_view_state" VALUES('user_fixture','gw_local_0','session_fixture','正文必须保留 gw_local_0',123.5,0,7,1,'2026-11-04T12:53:15.496255+00:00');
CREATE INDEX config_events_relay_idx
        ON config_events(relay_state, relay_next_attempt_at, event_seq);
CREATE INDEX registry_apply_journal_state_idx
        ON registry_apply_journal(state, updated_at);
CREATE INDEX config_event_relay_delivery_due_idx
        ON config_event_relay_delivery(consumer_id, state, next_attempt_at, event_id);
CREATE UNIQUE INDEX config_events_idempotency_result_unique
        ON config_events(config_domain, idempotency_key, result)
        WHERE idempotency_key IS NOT NULL;
CREATE INDEX config_apply_journal_domain_idx
        ON config_apply_journal(config_domain, state, updated_at);
CREATE INDEX gateway_runtime_generation_domain_idx
        ON gateway_runtime_generation(config_domain, state, updated_at);
CREATE INDEX gateway_restart_intent_gateway_idx
        ON gateway_restart_intent(gateway_id, state, updated_at);
DELETE FROM "sqlite_sequence";
COMMIT;
