"""真实执行封存的来源 lineage 损坏时，公开检查与冷恢复必须拒绝回填。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from app.services.infrastructure.rollout_context.activation.common import (
    HASH_MISMATCH_CODE,
    LINEAGE_BODY_UNAVAILABLE_CODE,
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 — 复用按当前文件隔离的产品装具。
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_committed_lineage_corruption_rejects_public_read_and_cold_restore(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    agents = case.workspace / "AGENTS.md"
    original_source, current_source = "LINEAGE_ORIGINAL_A\n", "LINEAGE_CURRENT_B\n"
    agents.write_text(original_source, encoding="utf-8")
    session = await create_session(case.client, "lineage损坏不使用当前来源")
    first_call, = await case.plan(session, [{"content": "原来源已执行。"}])
    first = await _send(case, session, "按原来源完成本轮。")
    job_id, thread = first["job_id"], first["thread_id"]
    assert isinstance(job_id, str) and isinstance(thread, str)
    await _job(case, job_id)
    saver = case.container.checkpointer
    original, = saver.list_context_assemblies(session, thread)
    activation = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=original.assembly_id,
        plan_hash=original.plan_hash, request_hash=original.request_hash,
    )
    assert any(row.resource_kind == "workspace_agents" for row in activation.bindings)
    assert original_source.strip() in json.dumps(case.request(first_call), ensure_ascii=False)

    # 第二轮通过原 producer/CSM 使用新文件，证明当前来源确实已有可误用的不同版本。
    agents.write_text(current_source, encoding="utf-8")
    second_call, = await case.plan(session, [{"content": "新来源已执行。"}])
    second = await _send(case, session, "使用当前来源完成下一轮。")
    next_job = second["job_id"]
    assert isinstance(next_job, str)
    await _job(case, next_job)
    assemblies = saver.list_context_assemblies(session, thread)
    latest, = [row for row in assemblies if row.model_call_id == second_call]
    latest_activation = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=latest.assembly_id,
        plan_hash=latest.plan_hash, request_hash=latest.request_hash,
    )
    old_agents = [row for row in activation.bindings if row.resource_kind == "workspace_agents"]
    new_agents = [row for row in latest_activation.bindings if row.resource_kind == "workspace_agents"]
    assert {row.facet for row in old_agents} == {row.facet for row in new_agents} == {"chain", "raw-file"}
    assert all(old.revision != new.revision for old, new in zip(old_agents, new_agents, strict=True))
    assert current_source.strip() in json.dumps(case.request(second_call), ensure_ascii=False)
    originals = {row.assembly_id: canonical_json_bytes(row.to_dict()) for row in assemblies}

    query = {
        "resource_identity": str(session_resource_identity(case.container.workspace_id, session)),
        "uri": session_resource_uri(case.container.workspace_id, session), "scope": "workspace",
        "view": "assemblies", "limit": 200, "max_chars": 65536,
    }
    listing = await case.client.post("/api/v1/context/read", json=query)
    assert listing.status_code == 200, listing.text
    entry, = [row for row in listing.json()["data"]["items"] if row["data"]["assembly_id"] == original.assembly_id]
    query.update(view="assembly", assembly_ref=entry["assembly_ref"])
    healthy = await case.client.post("/api/v1/context/read", json=query)
    assert healthy.status_code == 200, healthy.text
    baseline = healthy.json()["data"]
    projected, = [row for row in baseline["items"] if row["kind"] == "assembly"]
    assert {row["facet"] for row in projected["resource_refs"] if row["resource_kind"] == "workspace_agents"} == {"chain", "raw-file"}

    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    thread_node = resolver.resolve_thread_node(session, thread)
    database = thread_node / "rollout/index.sqlite"
    canonical = thread_node / "rollout/rollout.jsonl"
    canonical_before = canonical.read_bytes()
    snapshot_id = activation.activation_snapshot_id
    key, manifest_digest = case.sql(session, thread,
        "SELECT lineage_detail_ref,lineage_manifest_digest FROM resource_activation_snapshots WHERE activation_snapshot_id=?",
        (snapshot_id,))[0]
    lineage_path = thread_node / detail_relative_path(detail_ref_from_key(key))
    lineage_bytes = lineage_path.read_bytes()
    manifest = json.loads(lineage_bytes)
    assert manifest["detail"]["lineages"]
    failures = []
    for fault in ("missing", "body_hash", "catalog_digest"):
        expected_code = HASH_MISMATCH_CODE if fault == "catalog_digest" else LINEAGE_BODY_UNAVAILABLE_CODE
        try:
            # 只对产品已提交数据注入故障；不由测试构造执行、activation 或 lineage。
            if fault == "missing":
                lineage_path.unlink()
            elif fault == "body_hash":
                tampered = json.loads(lineage_bytes)
                tampered["detail"]["lineages"][0]["source_lineage_digest"] = "sha256:jcs:v1:" + "0" * 64
                lineage_path.write_bytes(canonical_json_bytes(tampered))
            else:
                with sqlite3.connect(database) as connection:
                    connection.execute("UPDATE resource_activation_snapshots SET lineage_manifest_digest=? WHERE activation_snapshot_id=?",
                        ("sha256:jcs:v1:" + "0" * 64, snapshot_id))
            with RolloutCheckpointSaver(resolver.sessions_root) as cold:
                with pytest.raises(ResourceActivationStoreError) as caught:
                    cold.load_sealed_resource_activation(
                        session, thread_id=thread, assembly_id=original.assembly_id,
                        plan_hash=original.plan_hash, request_hash=original.request_hash,
                    )
                assert caught.value.code == expected_code
            rejected = await case.client.post("/api/v1/context/read", json=query)
            assert rejected.status_code == 500, rejected.text
            failure = rejected.json()
            assert failure["data"] is None and failure["code"] == 500
            assert "ResourceActivationStoreError" in failure["message"]
            assert rejected.headers["X-Request-ID"] == failure["request_id"]
            assert original_source.strip() not in rejected.text and current_source.strip() not in rejected.text
            assert canonical.read_bytes() == canonical_before
            assert {row.assembly_id: canonical_json_bytes(row.to_dict())
                    for row in saver.list_context_assemblies(session, thread)} == originals
            failures.append({"fault": fault, "restore_code": caught.value.code, "response": failure})
        finally:
            # 恢复原件仅用于隔离故障场景，不代表产品提供自动修复能力。
            lineage_path.write_bytes(lineage_bytes)
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE resource_activation_snapshots SET lineage_manifest_digest=? WHERE activation_snapshot_id=?",
                    (manifest_digest, snapshot_id))

    agents.unlink()
    with RolloutCheckpointSaver(resolver.sessions_root) as cold:
        restored = cold.read_context_assembly_history(session, thread, original.assembly_id)
        assert restored.resource_activation == activation
        assert canonical_json_bytes(restored.assembly.to_dict()) == originals[original.assembly_id]
    recovered = await case.client.post("/api/v1/context/read", json=query)
    assert recovered.status_code == 200 and recovered.json()["data"] == baseline, recovered.text
    captures = {call: json.loads((case.model.artifacts / f"{call}.json").read_text())
                for call in (first_call, second_call)}
    audit = audit_sealed_requests(database,
        (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures)
    assert len(case.requests()) == 2 and canonical.read_bytes() == canonical_before
    assert restored.resource_activation is not None
    case.write_actual("activation-lineage-failures", {
        "session_id": session, "thread_id": thread, "calls": [first_call, second_call],
        "snapshot_id": snapshot_id, "failures": failures, "audit": audit,
        "restored_activation": restored.resource_activation.to_dict(), "public_projection": baseline,
    })
