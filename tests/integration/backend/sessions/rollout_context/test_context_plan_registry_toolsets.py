"""ToolSet snapshot 持久往返与真实 registry port；不代替 schema4 全链路 seal。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    validate_sealed_plan,
)
from app.services.infrastructure.rollout_context.assembly.plans.sealing import (
    bind_sealed_registration,
    preflight_seal,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    _insert_snapshot,
    registry_db,
)

__all__ = ["registry_db"]


@pytest.fixture
def run_context(request):
    # 被导入的 registry_db 同样按 request.node.path 定位，不占用主测试输出。
    return TestRunContext.from_test_file(Path(request.node.path)).prepare()


@pytest.fixture
def tool_draft(registry_db):
    _saver, session, _connection, _accepted = registry_db
    return ContextRequestPlan(
        session_id=session,
        plan_id="tool-plan",
        refs=(),
        tool_set_refs=tuple(
            ToolSetRef.from_tool_snapshot(
                session_id=session,
                plan_id="tool-plan",
                snapshot_id=name,
                source_revision=f"{name}-revision",
                tools=({"name": f"{name}_tool", "parameters": {"type": "object"}},),
                tool_policy={"mode": "auto"},
            )
            for name in ("z", "a")
        ),
        plan_creation_idempotency_key="tool-create",
    )


def _snapshot(draft, accepted, omitted):
    return ContextPlanComposer().assembly(
        plan=draft,
        assembly_id="tool-assembly",
        session_id=draft.session_id,
        turn_id=str(accepted["turn_id"]),
        execution_id=str(accepted["initial_execution_id"]),
        provider_version="registry-integration",
        omitted_ref_ids=omitted,
    )


def _jsonl_roundtrip(snapshot, run_context):
    # snapshot 的权威存储是 SQLite；此 JSONL 是生产 to_dict 的审计产物，
    # 不是伪造的 canonical rollout commit。下面单独验证 Saver 的 JSONL 不变。
    path = run_context.artifacts_dir / f"{snapshot.session_id}.snapshot.jsonl"
    path.write_bytes(canonical_json_bytes(snapshot.to_dict()) + b"\n")
    lines = path.read_bytes().splitlines()
    assert len(lines) == 1
    restored = ContextAssemblySnapshot.from_dict(json.loads(lines[0]))
    assert restored.to_dict() == snapshot.to_dict()
    return restored


@pytest.mark.parametrize("omitted", [(), ("z",), ("a",), ("z", "a")])
@pytest.mark.parametrize("reverse_rows", [False, True])
def test_toolsets_jsonl_sqlite_and_saver_restart(
    registry_db, run_context, tool_draft, omitted, reverse_rows
):
    saver, session, connection, accepted = registry_db
    canonical_path = saver._storage.root(session) / "rollout.jsonl"
    canonical_before = canonical_path.read_bytes()
    assert canonical_before
    assert all(
        isinstance(json.loads(line), dict) for line in canonical_before.splitlines()
    )
    offset = connection.execute(
        "SELECT committed_jsonl_offset FROM database_meta"
    ).fetchone()
    registered = saver.create_context_plan(session, tool_draft)
    assert [ref.ref_id for ref in registered.draft.tool_set_refs] == ["z", "a"]
    # SQLite 查询顺序不能成为 selection authority。
    connection.execute(f"PRAGMA reverse_unordered_selects = {int(reverse_rows)}")
    snapshot = _snapshot(registered.draft, accepted, omitted)
    validate_sealed_plan(registered.draft, snapshot)
    restored = _jsonl_roundtrip(snapshot, run_context)
    validate_sealed_plan(registered.draft, restored)
    assert [ref.ref_id for ref in restored.tool_set_refs] == sorted(
        {"z", "a"} - set(omitted)
    )
    assert [entry.ref.ref_id for entry in restored.selection] == ["z", "a"]
    assert [entry.plan_ordinal for entry in restored.selection] == [0, 1]
    assert [entry.to_dict() for entry in restored.selection] == [
        entry.to_dict() for entry in snapshot.selection
    ]
    for entry in restored.selection:
        assert entry.included == (entry.ref.ref_id not in omitted)
        assert entry.detail_ref is None and entry.contribution_id is None
        if not entry.included:
            assert entry.omission_reason and entry.loss
            assert "tools" not in entry.to_dict()["ref"]
            assert "tool_policy" not in entry.to_dict()["ref"]
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _insert_snapshot(connection, restored)
        bind_sealed_registration(
            connection,
            restored,
            seal_idempotency_key="tool-seal",
            detail_key=None,
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    restarted = RolloutCheckpointSaver(
        run_context.workspace_root / ".boxteam" / "sessions"
    )
    sealed = restarted.get_context_plan_registration(
        session, plan_id=tool_draft.plan_id
    )
    assert sealed.plan_state == "sealed"
    assert sealed.draft == registered.draft
    assert sealed.assembly_id == snapshot.assembly_id
    # 重启重试原内存形态和 JSON 恢复形态必须命中同一 seal identity。
    for retry in (snapshot, restored):
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            bind_sealed_registration(
                connection,
                retry,
                seal_idempotency_key="tool-seal",
                detail_key=None,
                seal_input_hash=sha256_jcs("storage-seal-test-input"),
            )
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        1,
    )
    assert canonical_path.read_bytes() == canonical_before
    assert (
        connection.execute(
            "SELECT committed_jsonl_offset FROM database_meta"
        ).fetchone()
        == offset
    )


@pytest.mark.parametrize("omitted", [("z",), ("z", "a")])
def test_omitted_comparison_never_reads_candidate_definitions(
    registry_db, tool_draft, omitted, monkeypatch
):
    saver, session, _connection, accepted = registry_db
    registered = saver.create_context_plan(session, tool_draft)
    snapshot = _snapshot(registered.draft, accepted, omitted)
    restored = ContextAssemblySnapshot.from_dict(snapshot.to_dict())
    guarded = {
        id(ref)
        for ref in (*registered.draft.tool_set_refs, *snapshot.tool_set_refs)
        if ref.ref_id in omitted
    }
    original = ToolSetRef.__getattribute__

    def reject_definition_read(ref, name):
        if id(ref) in guarded and name in {"tools", "tool_policy"}:
            pytest.fail(f"omitted ToolSetRef 正文被读取: {name}")
        return original(ref, name)

    monkeypatch.setattr(ToolSetRef, "__getattribute__", reject_definition_read)
    validate_sealed_plan(registered.draft, snapshot)
    validate_sealed_plan(registered.draft, restored)


@pytest.mark.parametrize("omitted", [(), ("z",)])
@pytest.mark.parametrize(
    "mutation", ["source_revision", "content", "policy", "schema", "missing_tag"]
)
def test_seal_rejects_self_consistent_but_different_tool_manifest(
    registry_db, run_context, tool_draft, omitted, mutation
):
    saver, session, connection, accepted = registry_db
    registered = saver.create_context_plan(session, tool_draft)
    original = tool_draft.tool_set_refs[0]
    options = {
        "snapshot_id": "unknown" if mutation == "missing_tag" else original.ref_id,
        "session_id": session,
        "plan_id": tool_draft.plan_id,
        "source_revision": "changed"
        if mutation == "source_revision"
        else original.source_revision,
        "tools": ({"name": "changed", "parameters": {"type": "object"}},)
        if mutation == "content"
        else original.tools,
        "tool_policy": {"mode": "changed"}
        if mutation == "policy"
        else original.tool_policy,
        "tool_set_schema_version": "changed"
        if mutation == "schema"
        else original.tool_set_schema_version,
    }
    changed = ToolSetRef.from_tool_snapshot(**options)
    altered_draft = replace(
        tool_draft, tool_set_refs=(changed, tool_draft.tool_set_refs[1])
    )
    altered_omission = (changed.ref_id,) if omitted else ()
    snapshot = _jsonl_roundtrip(
        _snapshot(altered_draft, accepted, altered_omission), run_context
    )
    snapshot.validate_hashes()  # 真实自洽新 manifest，不能仅靠 hash 校验挡住篡改。
    before = tuple(connection.iterdump())
    with pytest.raises(ValueError, match="plan-order-integrity"):
        preflight_seal(
            connection,
            snapshot,
            seal_idempotency_key="tool-seal",
            detail_key=None,
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    assert tuple(connection.iterdump()) == before
    assert (
        saver.get_context_plan_registration(session, plan_id=tool_draft.plan_id)
        == registered
    )


def test_omitted_tool_identity_does_not_resolve_to_request_only_tag(
    registry_db, tool_draft
):
    saver, session, connection, accepted = registry_db
    ref = ContextRef.request_only_ref(
        "z",
        session_id=session,
        plan_id=tool_draft.plan_id,
        source_revision="request-revision",
        content="另一种 tagged source",
    )
    draft = replace(
        tool_draft, refs=(ref,), tool_set_refs=(tool_draft.tool_set_refs[1],)
    )
    saver.create_context_plan(session, draft)
    # 相同裸 ID 的 request_only 不能为未注册的 tool_set 授权。
    snapshot = _snapshot(tool_draft, accepted, ("z",))
    with pytest.raises(ValueError, match="ToolSetRef 与 draft source manifest"):
        preflight_seal(
            connection,
            snapshot,
            seal_idempotency_key="tool-seal",
            detail_key=None,
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        0,
    )


@pytest.mark.parametrize("mutation", ["order", "inclusion", "omission_reason", "loss"])
def test_sealed_selection_semantics_cannot_change_after_restart(
    registry_db, run_context, tool_draft, mutation
):
    saver, session, connection, accepted = registry_db
    saver.create_context_plan(session, tool_draft)
    original = _snapshot(tool_draft, accepted, ("a",))
    if mutation == "order":
        changed = _snapshot(
            replace(
                tool_draft, tool_set_refs=tuple(reversed(tool_draft.tool_set_refs))
            ),
            accepted,
            ("a",),
        )
    elif mutation == "inclusion":
        changed = _snapshot(tool_draft, accepted, ())
    else:
        entry = original.selection[1]
        entry = replace(
            entry,
            **{"omission_reason": "other-reason"}
            if mutation == "omission_reason"
            else {"loss": (*entry.loss, "other-loss")},
        )
        selection = (original.selection[0], entry)
        plan = replace(original.as_sealed_plan(), selection=selection)
        changed = replace(
            original,
            selection=selection,
            plan_hash=plan.plan_hash(),
            request_hash=context_request_hash(
                plan,
                original.provider_version,
                projector_id=original.projector_id,
                projector_version=original.projector_version,
                target_format=original.target_format,
                wire_request=original.request_hash_preimage,
            ),
        )
    # 未 seal 时相同 registry 可供不同合法 selection；先选顺序不是 registry 排序。
    preflight_seal(
        connection,
        changed,
        seal_idempotency_key="tool-seal",
        detail_key=None,
        seal_input_hash=sha256_jcs("storage-seal-test-input"),
    )
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _insert_snapshot(connection, original)
        bind_sealed_registration(
            connection,
            original,
            seal_idempotency_key="tool-seal",
            detail_key=None,
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    restarted = RolloutCheckpointSaver(
        run_context.workspace_root / ".boxteam" / "sessions"
    )
    sealed = restarted.get_context_plan_registration(
        session, plan_id=tool_draft.plan_id
    )
    assert sealed.plan_state == "sealed"
    changed = _jsonl_roundtrip(changed, run_context)
    before = tuple(connection.iterdump())
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        preflight_seal(
            connection,
            changed,
            seal_idempotency_key="tool-seal",
            detail_key=None,
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    assert tuple(connection.iterdump()) == before
    assert (
        restarted.get_context_plan_registration(session, plan_id=tool_draft.plan_id)
        == sealed
    )


@pytest.mark.parametrize(
    "column, value",
    [
        ("tools_json", '[{"name":"injected"}]'),
        ("tool_policy_json", '{"mode":"injected"}'),
        ("source_revision", "injected"),
        ("content_length", 123456),
        ("content_hash", "sha256:jcs:v1:" + "0" * 64),
    ],
)
def test_recovery_rejects_tampered_tool_registry_rows(
    registry_db, run_context, tool_draft, column, value
):
    saver, session, connection, accepted = registry_db
    saver.create_context_plan(session, tool_draft)
    snapshot = _snapshot(tool_draft, accepted, ("z",))
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _insert_snapshot(connection, snapshot)
        bind_sealed_registration(
            connection,
            snapshot,
            seal_idempotency_key="tool-seal",
            detail_key=None,
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    with connection:
        connection.execute(
            f"UPDATE tool_set_snapshots SET {column} = ? WHERE tool_set_snapshot_id = 'z'",
            (value,),
        )
    before = tuple(connection.iterdump())
    restarted = RolloutCheckpointSaver(
        run_context.workspace_root / ".boxteam" / "sessions"
    )
    with pytest.raises(ValueError, match="source-mismatch"):
        restarted.get_context_plan_registration(session, plan_id=tool_draft.plan_id)
    assert tuple(connection.iterdump()) == before


def test_same_raw_id_keeps_request_and_tool_selection_separate(registry_db, tool_draft):
    saver, session, connection, accepted = registry_db
    request_ref = ContextRef.request_only_ref(
        "z",
        session_id=session,
        plan_id=tool_draft.plan_id,
        source_revision="request-revision",
        content="不会读取的 request-only 正文",
        availability="unavailable",
    )
    draft = replace(tool_draft, refs=(request_ref,))
    registered = saver.create_context_plan(session, draft)
    snapshot = _snapshot(registered.draft, accepted, ())
    restored = ContextAssemblySnapshot.from_dict(snapshot.to_dict())
    assert [
        (entry.ref.ref_type, entry.ref.ref_id, entry.included)
        for entry in restored.selection
    ] == [
        ("request_only", "z", False),
        ("tool_set", "z", True),
        ("tool_set", "a", True),
    ]
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _insert_snapshot(connection, restored)
        bind_sealed_registration(
            connection,
            restored,
            seal_idempotency_key="tool-seal",
            detail_key=None,
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    assert (
        saver.get_context_plan_registration(session, plan_id=draft.plan_id).plan_state
        == "sealed"
    )
