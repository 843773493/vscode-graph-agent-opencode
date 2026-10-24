"""只读核对真实Provider日志、transport产物与原owner的封存事实。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from app.agents.providers.wire_finalize import (
    finalize_chat_wire,
    provider_wire_params,
    wire_request_params,
)
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.item_reader import (
    RolloutCanonicalReadMixin,
)
from app.services.infrastructure.rollout_context.storage.checkpoint_state_values import (
    read_state_value,
)


def logged_provider_wire(attempt: dict[str, object], *, target_format: str) -> dict[str, object]:
    """只用日志冻结的协议与参数，复用真实发送编码，不访问当前配置。"""
    logged = attempt["request"]
    assert isinstance(logged, dict)
    if target_format == "chat_completions":
        params = wire_request_params(
            SimpleNamespace(custom_llm_provider=attempt["provider"]),
            {key: value for key, value in logged.items() if key not in {"messages", "tools"}},
        )
        logged = finalize_chat_wire(messages=logged["messages"], tools=logged.get("tools"), params=params)
    else:
        assert target_format == "responses", target_format
        logged = provider_wire_params(logged)
    return normalize_wire_request(logged)


def audit_sealed_requests(
    index: Path, log_paths: Iterable[Path], captures: Mapping[str, dict[str, object]],
) -> dict[str, object]:
    """不读当前配置、不补业务状态；输入路径由调用方的catalog resolver提供。"""
    with (
        closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as connection,
        index.with_name("rollout.jsonl").open("rb") as stream,
    ):
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        session, thread, state = connection.execute(
            "SELECT session_id,thread_id,database_state FROM database_meta WHERE singleton_id=1",
        ).fetchone()
        assert state == "active"
        expected_calls = {call for call, record in captures.items()
                          if (record["identity"]["session_id"], record["identity"]["thread_id"]) == (session, thread)}
        assert expected_calls, f"缺少真实transport请求: {session}/{thread}"
        proofs = []
        canonical = {}
        for path in sorted(log_paths):
            log = json.loads(path.read_text(encoding="utf-8"))
            assert log["session_id"] == session
            for attempt in log["upstream"]["attempts"]:
                ref = attempt["sealed_dispatch"]
                assert ref is not None, f"Agent Provider日志缺少封存引用: {path}"
                if (ref["session_id"], ref["thread_id"]) != (session, thread):
                    continue
                call = ref["callback_model_call_id"]
                registration = read_registration(connection, session, thread, ref["attempt_key"])
                snapshot = registration.sealed_snapshot
                assert snapshot is not None and snapshot.seal_binding is not None
                expected_ref = SealedContextDispatchRef.from_snapshot(
                    snapshot, checkpoint_ns=ref["checkpoint_ns"],
                    attempt_key=ref["attempt_key"], callback_model_call_id=call,
                )
                assert canonical_json_bytes(ref) == canonical_json_bytes(asdict(expected_ref)), path
                model = connection.execute(
                    "SELECT m.execution_id,m.assembly_id,e.job_id FROM model_calls m "
                    "JOIN executions e USING(execution_id) WHERE m.model_call_id=? "
                    "AND m.session_id=? AND m.thread_id=?", (call, session, thread),
                ).fetchone()
                assert model == (ref["execution_id"], ref["assembly_id"], log["job_id"]), path
                transport = normalize_wire_request(captures[call]["request"])
                assert canonical_json_bytes(transport) == canonical_json_bytes(snapshot.request_hash_preimage), path
                assert canonical_json_bytes(logged_provider_wire(attempt, target_format=snapshot.target_format)) == canonical_json_bytes(transport), path
                for entry in snapshot.selection:
                    source = entry.ref
                    if not entry.included or source.ref_type != "canonical_item":
                        continue
                    require_historical_scope(connection, source.session_id, source.thread_id)
                    if source.ref_id not in canonical:
                        row = connection.execute(
                            "SELECT i.item_sequence,i.jsonl_offset,i.jsonl_length,i.payload_length,"
                            "i.source_revision,i.content_hash,e.session_id,e.thread_id FROM item_catalog i "
                            "JOIN executions e USING(execution_id) WHERE i.item_id=?", (source.ref_id,),
                        ).fetchone()
                        assert row is not None, source.ref_id
                        require_historical_scope(connection, row[6], row[7])
                        item = RolloutCanonicalReadMixin._read_v2_item_at(
                            connection, stream, sequence=row[0], offset=row[1], length=row[2],
                        )
                        assert item.item_id == source.ref_id
                        canonical[source.ref_id] = (row[0], row[3], row[4], row[5])
                    assert canonical[source.ref_id] == (
                        source.item_sequence, source.content_length, source.source_revision, source.content_hash,
                    ), source.ref_id
                proofs.append({
                    "model_call_id": call, "assembly_id": snapshot.assembly_id,
                    "request_hash": snapshot.request_hash, "plan_hash": snapshot.plan_hash,
                    "prefix_epoch": ref["prefix_epoch"], "epoch_reason": ref["epoch_reason"],
                    "prefix_hash": ref["prefix_hash"], "frame_hashes": ref["frame_hashes"],
                    "selection_revision": snapshot.seal_binding.selection_revision,
                    "applied_toolset_revision": snapshot.seal_binding.applied_toolset_revision,
                    "selection_hash": sha256_jcs([entry.to_dict() for entry in snapshot.selection]),
                    "log_file": path.name,
                })
        assert {proof["model_call_id"] for proof in proofs} == expected_calls, (session, thread)
        # checkpoint沿用的非消息状态仍须有精确owner/version和有效正文，不能只验assembly。
        values = connection.execute(
            "SELECT DISTINCT c.value_id,c.channel_name,c.channel_version,p.checkpoint_ns,"
            "c.value_session_id,c.value_thread_id FROM checkpoint_channels c "
            "JOIN checkpoints p USING(checkpoint_id) WHERE c.storage_kind='sqlite_value' AND c.value_state='present'",
        ).fetchall()
        for value_id, channel, version, namespace, value_session, value_thread in values:
            read_state_value(connection, value_id, channel=channel, version=version,
                             checkpoint_ns=namespace, session_id=value_session, thread_id=value_thread)
        assert connection.execute(
            "SELECT count(*) FROM checkpoint_channels c LEFT JOIN context_views v ON v.view_id=c.context_view_id "
            "WHERE c.storage_kind='rollout_view' AND c.value_state IN ('snapshot','delta','present') AND v.view_id IS NULL",
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM checkpoint_context_source_snapshots c LEFT JOIN context_assemblies a "
            "ON a.assembly_id=c.source_assembly_id WHERE c.source_assembly_id IS NOT NULL AND a.assembly_id IS NULL",
        ).fetchone() == (0,)
        return {"session_id": session, "thread_id": thread, "requests": proofs,
                "canonical_items": len(canonical), "state_versions": len(values),
                "checkpoint_count": connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0]}
