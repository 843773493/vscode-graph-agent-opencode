"""Workspace 控制面状态库去重 SQL 常量。

四条常量收敛同一语义 SQL 的逐字重复，供 apply / snapshot / restart 三族 mixin 共享；
常量单独成模块避免各族打补丁式复制。"""

__all__ = [
    "_CONFIG_APPLY_CLAIM_UPSERT",
    "_CONFIG_APPLY_JOURNAL_INSERT",
    "_PENDING_CANDIDATE_SNAPSHOT_SELECT",
    "_SOURCE_LAYER_BASELINE_SELECT",
]


# 以下常量收敛同一语义 SQL 的逐字重复：begin/acquire 两条 apply 入口共用候选
# 快照读取，start/begin 两处共用 apply journal 首插，prepare/acquire 两处共用
# apply claim upsert，begin/discard 两处共用 source layer 完整基线读取。
_PENDING_CANDIDATE_SNAPSHOT_SELECT = """
SELECT pending_revision, state, base_active_revision
FROM config_pending_candidate
WHERE config_domain = ? AND candidate_id = ?
"""

_SOURCE_LAYER_BASELINE_SELECT = """
SELECT config_key, vrn, presence, layer_revision,
       layer_digest, source_generation
FROM config_source_layers
"""

_CONFIG_APPLY_JOURNAL_INSERT = """
INSERT INTO config_apply_journal(
    config_domain, apply_id, candidate_id, attempt_id, owner,
    base_active_revision, pending_revision, source_baseline_json,
    active_baseline_json, registry_revision, side_effects_json,
    state, last_error, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 'applying', NULL, ?, ?)
"""

_CONFIG_APPLY_CLAIM_UPSERT = """
INSERT INTO config_apply_claim(
    config_domain, candidate_id, attempt_id, apply_id, owner,
    base_active_revision, target_generation, lease_expires_at,
    fencing_token, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(config_domain) DO UPDATE SET
    candidate_id=excluded.candidate_id,
    attempt_id=excluded.attempt_id,
    apply_id=excluded.apply_id,
    owner=excluded.owner,
    base_active_revision=excluded.base_active_revision,
    target_generation=excluded.target_generation,
    lease_expires_at=excluded.lease_expires_at,
    fencing_token=excluded.fencing_token,
    updated_at=excluded.updated_at
"""
