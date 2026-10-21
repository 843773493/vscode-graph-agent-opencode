"""正式 schema4 初始化的 registry/真实 SQLite 合同。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.domain.itemized import hashing
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import (
    ContextContribution,
    ContextRequestPlan,
    SourceBinding,
)
from app.services.infrastructure.rollout_context.assembly.content_store import (
    store_snapshot,
)
from app.services.infrastructure.rollout_context.assembly.plans import (
    registry as plan_registry,
)
from app.services.infrastructure.rollout_context.assembly.plans import (
    sealing as plan_sealing,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    create_registration,
    read_registration,
    revise_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.sealing import (
    bind_sealed_registration,
    preflight_seal,
    record_seal_failure,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_SCHEMA_VERSION,
)
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def registry_db(request, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    saver = RolloutCheckpointSaver(
        sessions,
    )
    accepted = saver.accept_turn(
        session_id,
        thread_id=saver._storage.storage.main_thread_id(session_id), accepted_ingress_id="registry-ingress",
        acceptance_idempotency_key="registry-root",
        job_id="registry-job",
        admission_idempotency_key="registry-admission",
        payload="独立草稿",
        payload_kind="text",
    )
    with saver._storage.storage._connect(
        saver._storage.storage.main_thread_id(session_id), "", session_id=session_id
    ) as owner_connection:
        database_path = owner_connection.execute("PRAGMA database_list").fetchone()[2]
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute(
        "SELECT schema_version, database_state FROM database_meta"
    ).fetchone() == (ROLLOUT_SCHEMA_VERSION, "active")
    try:
        yield saver, session_id, connection, accepted
    finally:
        connection.close()


@pytest.fixture
def draft(registry_db):
    saver, session, _connection, _accepted = registry_db
    thread_id = saver._storage.storage.main_thread_id(session)
    body = {"instructions": "草稿正文不能复制进 SQLite", "value": 0.0000001}
    contribution = ContextContribution(
        contribution_id="source-contribution",
        source_kind="workspace_instructions",
        source_revision="revision-1",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        extensions={} , source_binding=SourceBinding(source_ref='source-contribution'),
        source_ordinal=0,
    )
    ref = ContextRef.request_only_ref(
        "request-ref",
        session_id=session, thread_id=thread_id,
        plan_id="plan",
        source_revision="revision-1",
        content_hash_value=contribution.content_hash,
        content_length=contribution.content_length,
        source_ref=contribution.contribution_id,
    )
    tool = ToolSetRef.from_tool_snapshot(
        session_id=session,
        thread_id=thread_id,
        plan_id="plan",
        snapshot_id="tools",
        source_revision="tools-1",
        tools=({"name": "read_file", "parameters": {"type": "object"}},),
    )
    return ContextRequestPlan(
        session_id=session,
        thread_id=thread_id,
        plan_id="plan",
        refs=(ref,),
        contributions=(contribution,),
        tool_set_refs=(tool,),
        plan_creation_idempotency_key="create-key",
    )


def _create(connection, draft):
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        return create_registration(connection, draft)




def test_registry_roundtrip_has_no_early_binding_or_body(registry_db, draft, monkeypatch):
    saver, session, connection, _accepted = registry_db
    before = (saver._storage.storage.root(session) / "rollout.jsonl").read_bytes()
    committed_offset = connection.execute(
        "SELECT committed_jsonl_offset FROM database_meta"
    ).fetchone()
    def refuse_readback(*args, **kwargs):
        raise AssertionError("新注册应返回本次写入值，不冷读刚写入的 registry")

    monkeypatch.setattr(plan_registry, "read_registration", refuse_readback)
    registered = _create(connection, draft)
    assert registered.revision == 0
    assert registered.plan_state == "unsealed"
    assert registered.assembly_id is None
    assert registered.draft.selection == ()
    assert registered.draft.contributions[0].body is None
    assert registered.draft.contributions[0].contribution_ordinal is None
    assert registered.draft.tool_set_refs[0].assembly_id is None
    assert registered.draft_json == connection.execute(
        "SELECT draft_json FROM context_plans"
    ).fetchone()[0]
    assert connection.execute(
        "SELECT assembly_id FROM tool_set_snapshots"
    ).fetchall() == [(None,)]
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        0,
    )
    assert connection.execute(
        "SELECT count(*) FROM context_assembly_selections"
    ).fetchone() == (0,)
    assert "草稿正文不能复制进 SQLite" not in "\n".join(connection.iterdump())
    with saver._storage.storage._connect(
        saver._storage.storage.main_thread_id(session), "", read_only=True, session_id=session
    ) as restarted:
        assert (
            read_registration(restarted, session, draft.thread_id, draft.plan_id)
            == registered
        )
    assert (saver._storage.storage.root(session) / "rollout.jsonl").read_bytes() == before
    assert (
        connection.execute(
            "SELECT committed_jsonl_offset FROM database_meta"
        ).fetchone()
        == committed_offset
    )


def test_creation_key_reuses_original_plan_owner(registry_db, draft):
    _saver, session, connection, _accepted = registry_db
    first = _create(connection, draft)
    retry = replace(
        draft,
        plan_id="new-allocated-id",
        refs=tuple(replace(ref, plan_id="new-allocated-id") for ref in draft.refs),
        tool_set_refs=tuple(
            replace(ref, plan_id="new-allocated-id") for ref in draft.tool_set_refs
        ),
    )
    assert _create(connection, retry) == first
    assert first.draft.plan_id == "plan"
    assert connection.execute(
        "SELECT count(*) FROM context_plans WHERE session_id = ?", (session,)
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    "field", ["visibility", "source_ref", "selection_policy", "creation_key"]
)
def test_creation_conflicts_include_fields_omitted_by_plan_hash(
    registry_db, draft, field
):
    _saver, _session, connection, _accepted = registry_db
    _create(connection, draft)
    changed = (
        replace(draft, selection_policy="changed")
        if field == "selection_policy"
        else (
            replace(draft, plan_creation_idempotency_key="different-key")
            if field == "creation_key"
            else replace(
                draft,
                refs=(
                    replace(
                        draft.refs[0],
                        **{
                            field: "public" if field == "visibility" else "other-source"
                        },
                    ),
                ),
            )
        )
    )
    with pytest.raises(ValueError, match="plan-idempotency-conflict"):
        _create(connection, changed)
    assert connection.execute("SELECT count(*) FROM context_plans").fetchone() == (1,)


def test_registry_requires_owner_and_explicit_transaction(registry_db, draft):
    _saver, _session, connection, _accepted = registry_db
    with pytest.raises(RuntimeError, match="显式事务"):
        create_registration(connection, draft)
    with pytest.raises(ValueError, match="source-mismatch"):
        _create(
            connection,
            ContextRequestPlan(
                session_id="other",
                thread_id=draft.thread_id,
                plan_id="other-plan",
                refs=(),
                plan_creation_idempotency_key="other-key",
            ),
        )
    assert connection.execute("SELECT count(*) FROM context_plans").fetchone() == (0,)


@pytest.mark.parametrize(
    "table", ["context_plan_refs", "context_plan_contributions", "tool_set_snapshots"]
)
def test_recovery_rejects_missing_registry_without_repair(registry_db, draft, table):
    _saver, session, connection, _accepted = registry_db
    _create(connection, draft)
    with connection:
        connection.execute(f"DELETE FROM {table}")
    with pytest.raises(ValueError, match="source-mismatch"):
        read_registration(connection, session, draft.thread_id, draft.plan_id)
    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_draft_revision_is_cas_and_keeps_initial_creation_identity(registry_db, draft):
    _saver, session, connection, _accepted = registry_db
    first = _create(connection, draft)
    changed = replace(draft, history_view_revision=1, tool_set_refs=())
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        revised = revise_registration(connection, changed, expected_revision=0)
    assert revised.revision == 1
    assert revised.creation_hash == first.creation_hash
    assert revised.draft.tool_set_refs == ()
    assert connection.execute("SELECT count(*) FROM tool_set_snapshots").fetchone() == (
        0,
    )
    assert _create(connection, draft) == revised
    with pytest.raises(ValueError, match="plan-revision-conflict"), connection:
        connection.execute("BEGIN IMMEDIATE")
        revise_registration(connection, draft, expected_revision=0)
    assert read_registration(connection, session, draft.thread_id, draft.plan_id) == revised


def test_registry_each_operation_encodes_each_draft_read_or_write_once(
    registry_db, draft, monkeypatch
):
    _saver, session, connection, _accepted = registry_db
    encoded_revisions = []
    encode = hashing.canonical_json_bytes

    def observe(value):
        if isinstance(value, dict) and "plan_creation_idempotency_key" in value:
            encoded_revisions.append(value["history_view_revision"])
        return encode(value)

    monkeypatch.setattr(hashing, "canonical_json_bytes", observe)
    created = _create(connection, draft)
    # 新写入直接返回本次不可变值；下一次独立冷读仍完整核验。
    assert encoded_revisions == [0]
    encoded_revisions.clear()
    assert read_registration(connection, session, draft.thread_id, draft.plan_id) == created
    assert encoded_revisions == [0]
    encoded_revisions.clear()
    changed = replace(draft, history_view_revision=7)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        revised = revise_registration(connection, changed, expected_revision=0)
    # 修订前读取当前版本，CAS 后直接返回本次写入值。
    assert encoded_revisions == [0, 7]
    assert revised.revision == 1
    assert revised.creation_hash == created.creation_hash
    encoded_revisions.clear()
    assert read_registration(connection, session, draft.thread_id, draft.plan_id) == revised
    assert encoded_revisions == [7, 0]
    raw, digest = connection.execute(
        "SELECT draft_json, draft_hash FROM context_plans"
    ).fetchone()
    assert raw == encode(revised.draft.to_dict()).decode("utf-8")
    assert digest == sha256_jcs(json.loads(raw))


@pytest.mark.parametrize("failure", ["noncanonical", "digest", "typed"])
def test_registry_cold_read_keeps_manifest_error_before_typed_parser(
    registry_db, draft, failure
):
    _saver, session, connection, _accepted = registry_db
    _create(connection, draft)
    original = connection.execute("SELECT draft_json FROM context_plans").fetchone()[0]
    value = json.loads(original)
    value["selection_policy"] = None
    encoded = canonical_json_bytes(value).decode("utf-8")
    raw = " " + encoded if failure == "noncanonical" else encoded
    digest = sha256_jcs(value) if failure == "typed" else sha256_jcs("wrong")
    with connection:
        connection.execute(
            "UPDATE context_plans SET draft_json = ?, draft_hash = ?", (raw, digest)
        )
    before = connection.execute("SELECT * FROM context_plans").fetchall()
    if failure == "typed":
        with pytest.raises(ItemSchemaError, match="selection_policy"):
            read_registration(connection, session, draft.thread_id, draft.plan_id)
    else:
        with pytest.raises(ValueError, match="plan draft manifest hash 不一致") as error:
            read_registration(connection, session, draft.thread_id, draft.plan_id)
        assert type(error.value) is ValueError
    assert connection.execute("SELECT * FROM context_plans").fetchall() == before


@pytest.mark.parametrize("tamper", ["creation_hash", "creation_json"])
def test_revised_plan_still_authenticates_initial_creation_preimage(
    registry_db, draft, tamper
):
    saver, session, connection, _accepted = registry_db
    initial = saver.create_context_plan(session, draft)
    initial_json = connection.execute(
        "SELECT creation_json FROM context_plans"
    ).fetchone()[0]
    revised_input = replace(draft, history_view_revision=7)
    revised = saver.revise_context_plan(session, revised_input, expected_revision=0)
    assert revised.revision == 1
    assert revised.creation_hash == initial.creation_hash
    assert (
        connection.execute("SELECT creation_json FROM context_plans").fetchone()[0]
        == initial_json
    )
    with connection:
        if tamper == "creation_hash":
            connection.execute(
                "UPDATE context_plans SET creation_hash='forged-after-revision'"
            )
        else:
            connection.execute("UPDATE context_plans SET creation_json=draft_json")
    with pytest.raises(ValueError, match="source-mismatch"):
        saver.get_context_plan_registration(
            session, thread_id=draft.thread_id, plan_id=draft.plan_id
        )
    with pytest.raises(ValueError, match="source-mismatch"):
        saver.create_context_plan(session, revised_input)


def test_failed_registry_write_rolls_back_header_and_all_indices(registry_db, draft):
    _saver, _session, connection, _accepted = registry_db
    connection.execute(
        "CREATE TRIGGER refuse_tools BEFORE INSERT ON tool_set_snapshots BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        _create(connection, draft)
    for table in (
        "context_plans",
        "context_plan_refs",
        "context_plan_contributions",
        "tool_set_snapshots",
    ):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_failure_control_keeps_draft_correctable_without_selection(registry_db, draft):
    _saver, session, connection, _accepted = registry_db
    first = _create(connection, draft)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        failure_id = record_seal_failure(
            connection,
            session,
            draft.thread_id,
            draft.plan_id,
            seal_idempotency_key="seal-key",
            error_code="detail-unavailable",
        )
    assert failure_id.startswith("seal-failure:")
    assert read_registration(connection, session, draft.thread_id, draft.plan_id) == first
    assert connection.execute(
        "SELECT error_code FROM context_plan_seal_failures"
    ).fetchall() == [("detail-unavailable",)]
    with pytest.raises(ValueError, match="控制分类"), connection:
        connection.execute("BEGIN IMMEDIATE")
        record_seal_failure(
            connection,
            session,
            draft.thread_id,
            draft.plan_id,
            seal_idempotency_key="seal-key",
            error_code="exception with sensitive data",
        )


def test_seal_requires_current_draft_and_atomic_snapshot(registry_db, monkeypatch):
    saver, session, connection, accepted = registry_db
    draft = ContextRequestPlan(
        session_id=session,
        thread_id=saver._storage.storage.main_thread_id(session),
        plan_id="empty-plan",
        refs=(),
        plan_creation_idempotency_key="empty-key",
    )
    _create(connection, draft)
    snapshot = ContextPlanComposer().assembly(
        plan=draft,
        assembly_id="assembly",
        session_id=session,
        thread_id=draft.thread_id,
        turn_id=str(accepted["turn_id"]),
        execution_id=str(accepted["initial_execution_id"]),
        provider_version="fixture",
    )
    seal_options = {
        "seal_idempotency_key": "seal-key",
        "detail_key": None,
        "seal_input_hash": sha256_jcs("storage-seal-test-input"),
    }
    events = []

    def observe(event, operation):
        def called(*args):
            events.append(event)
            return operation(*args)

        return called

    for name, event in (
        ("runtime_seal_bytes_and_hash", "hash"),
        ("read_registration", "read"),
        ("validate_sealed_plan", "validate"),
    ):
        monkeypatch.setattr(plan_sealing, name, observe(event, getattr(plan_sealing, name)))

    jsonl_path = saver._storage.storage.jsonl_path(session)

    def preflight(candidate=snapshot, *, expected=("hash", "read", "validate"), **options):
        before = (tuple(connection.iterdump()), connection.total_changes, jsonl_path.read_bytes())
        events.clear()
        try:
            return preflight_seal(connection, candidate, **(seal_options | options))
        finally:
            assert events == list(expected)
            assert (
                tuple(connection.iterdump()), connection.total_changes, jsonl_path.read_bytes()
            ) == before

    damaged = replace(snapshot, plan_hash=sha256_jcs("damaged-plan-hash"))
    with pytest.raises(ValueError, match="seal_input_hash") as error:
        preflight(damaged, expected=("hash",), seal_input_hash="invalid-hash")
    assert type(error.value) is ValueError
    with pytest.raises(ItemSchemaError, match="context assembly plan_hash"):
        preflight(damaged)
    prepared = preflight(detail_key='引号"\\\n𐀀\ue000,"snapshot":')
    assert prepared.snapshot_json.encode() == canonical_json_bytes(snapshot.to_dict())
    with pytest.raises(ValueError, match="同事务 assembly"), connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = preflight()
        bind_sealed_registration(connection, registered)
    assert (
        read_registration(connection, session, draft.thread_id, draft.plan_id).plan_state
        == "unsealed"
    )
    with pytest.raises(RuntimeError, match="injected crash"), connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = preflight()
        _insert_snapshot(connection, snapshot)
        bind_sealed_registration(connection, registered)
        assert (
            read_registration(connection, session, draft.thread_id, draft.plan_id).plan_state
            == "sealed"
        )
        raise RuntimeError("injected crash")
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        0,
    )
    assert (
        read_registration(connection, session, draft.thread_id, draft.plan_id).plan_state
        == "unsealed"
    )
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = preflight()
        _insert_snapshot(connection, snapshot)
        bind_sealed_registration(connection, registered)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = preflight()
        bind_sealed_registration(connection, registered)
    with pytest.raises(ValueError, match="同一 plan 已封存为不同请求") as error:
        preflight(
            damaged,
            expected=("hash", "read"),
            seal_idempotency_key="different-key",
        )
    assert type(error.value) is ValueError
    for invalid_detail, message in (
        ("\ud800", "value.detail_ref.*非法 Unicode surrogate"),
        (b"\xff", "value.detail_ref 不是 JSON value: bytes"),
    ):
        with pytest.raises(ItemSchemaError, match=message):
            preflight(
                damaged, expected=("hash",), detail_key=invalid_detail,
                seal_idempotency_key="different-key",
            )
    before = (tuple(connection.iterdump()), connection.total_changes, jsonl_path.read_bytes())
    events.clear()
    for invalid_ref, error_type, message in (
        (b"\xff", TypeError, "detail_ref 必须是 DetailRef"),
        (DetailRef(session, draft.thread_id, snapshot.assembly_id, "\ud800"),
         ItemSchemaError, "value.detail_id.*非法 Unicode surrogate"),
    ):
        with pytest.raises(error_type, match=message):
            saver._storage.assemblies.seal_context_assembly(
                session, draft.thread_id, damaged, detail_ref=invalid_ref,
                seal_idempotency_key="different-key",
                seal_input_hash=seal_options["seal_input_hash"],
            )
    assert events == []
    assert (tuple(connection.iterdump()), connection.total_changes, jsonl_path.read_bytes()) == before
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"), connection:
        connection.execute("BEGIN IMMEDIATE")
        revise_registration(connection, draft, expected_revision=0)


def _insert_snapshot(connection, snapshot):
    # 本用例验证 registry 的事务 port，不冒充完整 storage commit/selection 写入。
    connection.execute(
        "INSERT INTO context_assemblies(assembly_id, session_id, thread_id, turn_id, execution_id, plan_id, "
        "plan_hash, request_hash, history_view_revision, source_overlay_epoch, snapshot_json, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sealed', 'fixture')",
        (
            snapshot.assembly_id,
            snapshot.session_id,
            snapshot.thread_id,
            snapshot.turn_id,
            snapshot.execution_id,
            snapshot.plan_id,
            snapshot.plan_hash,
            snapshot.request_hash,
            snapshot.history_view_revision,
            snapshot.source_overlay_epoch,
            store_snapshot(connection, snapshot.to_dict()),
        ),
    )


@pytest.mark.parametrize(
    "mutation", ["draft_json", "draft_hash", "header_key", "early_tool_binding"]
)
def test_recovery_rejects_tampered_manifest(registry_db, draft, mutation):
    _saver, session, connection, _accepted = registry_db
    _create(connection, draft)
    with connection:
        if mutation == "draft_json":
            raw = json.loads(
                connection.execute("SELECT draft_json FROM context_plans").fetchone()[0]
            )
            raw["selection_policy"] = "tampered"
            connection.execute(
                "UPDATE context_plans SET draft_json = ?", (json.dumps(raw),)
            )
        elif mutation == "draft_hash":
            connection.execute("UPDATE context_plans SET draft_hash = 'bad'")
        elif mutation == "header_key":
            connection.execute(
                "UPDATE context_plans SET plan_creation_idempotency_key = 'bad'"
            )
        else:
            connection.execute(
                "UPDATE tool_set_snapshots SET assembly_id = 'premature'"
            )
    with pytest.raises(ValueError, match="source-mismatch"):
        read_registration(connection, session, draft.thread_id, draft.plan_id)


def test_saver_registry_restarts_and_revises_through_one_owner(registry_db, draft):
    saver, session, _connection, _accepted = registry_db
    original = saver.create_context_plan(session, draft)
    # catalog 模式下 rollout 物理路径带日期分层，sessions 根必须取 storage
    # 的权威值，不能用 rollout 路径反向推导。
    restarted = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    assert (
        restarted.get_context_plan_registration(
            session, thread_id=draft.thread_id, plan_id=draft.plan_id
        )
        == original
    )
    changed = replace(draft, history_view_revision=1)
    current = restarted.revise_context_plan(session, changed, expected_revision=0)
    assert current.revision == 1
    assert (
        saver.get_context_plan_registration(
            session, thread_id=draft.thread_id, plan_id=draft.plan_id
        )
        == current
    )
    assert saver.create_context_plan(session, draft) == current


def test_saver_plan_body_cache_never_overwrites_another_plan(registry_db, draft):
    saver, session, _connection, accepted = registry_db
    first = saver.create_context_plan(session, draft)
    second_body = {"instructions": "第二个 plan 的同名来源"}
    contribution = replace(
        draft.contributions[0],
        body=second_body,
        content_hash=contribution_content_hash("prompt", second_body),
        content_length=None,
    )
    second = replace(
        draft,
        plan_id="second-plan",
        plan_creation_idempotency_key="second-key",
        tool_set_refs=(),
        contributions=(contribution,),
        refs=(
            replace(
                draft.refs[0],
                plan_id="second-plan",
                content_hash=contribution.content_hash,
                content_length=contribution.content_length,
            ),
        ),
    )
    second_registered = saver.create_context_plan(session, second)
    assert (
        session,
        "",
        contribution.contribution_id,
    ) not in saver._request_only_content
    for registered, expected_body in (
        (first, draft.contributions[0].body),
        (second_registered, second_body),
    ):
        snapshot = saver.seal_context_plan(
            session,
            draft.thread_id,
            registered.draft,
            turn_id=accepted["turn_id"],
            execution_id=accepted["initial_execution_id"],
            provider_version="registry-provider",
            wire_profile=None, wire_params=None,
            seal_idempotency_key=f"seal-{registered.draft.plan_id}",
        )
        detail_ref = next(
            entry.detail_ref for entry in snapshot.selection
            if entry.included and entry.ref.ref_id == "request-ref"
        )
        assert (
            saver.read_context_plan_detail(
                session, draft.thread_id, detail_ref=detail_ref
            )["detail"]
            == expected_body
        )


def test_nested_body_mutation_is_isolated_before_manifest_redaction(registry_db, draft):
    saver, session, connection, accepted = registry_db
    contribution = draft.contributions[0]
    original_body = contribution.body
    original_hash = contribution.content_hash
    draft.contributions[0].body["instructions"] = "调用方篡改了原始正文"
    assert contribution.body == original_body
    assert contribution.content_hash == original_hash
    registered = saver.create_context_plan(session, draft)
    assert registered.draft.contributions[0].body is None
    assert registered.draft.contributions[0].content_hash == original_hash
    assert connection.execute("SELECT count(*) FROM context_plans").fetchone() == (1,)
    snapshot = saver.seal_context_plan(
        session,
        draft.thread_id,
        registered.draft,
        turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        provider_version="registry-provider",
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key="seal-isolated-body",
    )
    detail_ref = next(
        entry.detail_ref for entry in snapshot.selection
        if entry.included and entry.ref.ref_id == "request-ref"
    )
    assert saver.read_context_plan_detail(
        session, draft.thread_id, detail_ref=detail_ref,
    )["detail"] == original_body
    assert "调用方篡改了原始正文" not in "\n".join(connection.iterdump())


@pytest.mark.parametrize("field", ["draft_json", "creation_json"])
def test_registry_restore_rejects_inline_body_with_matching_hash(
    registry_db, draft, field
):
    _saver, session, connection, _accepted = registry_db
    _create(connection, draft)
    inline = draft.to_dict()
    with connection:
        connection.execute(
            f"UPDATE context_plans SET {field}=?",
            (canonical_json_bytes(inline).decode(),),
        )
        if field == "draft_json":
            connection.execute(
                "UPDATE context_plans SET draft_hash=?", (sha256_jcs(inline),)
            )
    with pytest.raises(ValueError, match="registry 不得包含 inline body"):
        read_registration(connection, session, draft.thread_id, draft.plan_id)


def test_toolset_sql_owner_must_exist_even_outside_registry_api(registry_db, draft):
    _saver, session, connection, _accepted = registry_db
    original = _create(connection, draft)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), connection:
        connection.execute("UPDATE tool_set_snapshots SET plan_id='missing-parent'")
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert read_registration(connection, session, draft.thread_id, draft.plan_id) == original


def test_registry_rejects_duplicate_json_key_even_when_effective_hash_matches(
    registry_db, draft
):
    _saver, session, connection, _accepted = registry_db
    _create(connection, draft)
    original = connection.execute("SELECT draft_json FROM context_plans").fetchone()[0]
    duplicate = '{"plan_id":"forged",' + original[1:]
    assert json.loads(duplicate) == json.loads(original)
    with connection:
        connection.execute("UPDATE context_plans SET draft_json=?", (duplicate,))
    with pytest.raises(ValueError, match="source-mismatch"):
        read_registration(connection, session, draft.thread_id, draft.plan_id)


def test_sql_cannot_claim_sealed_with_null_binding(registry_db, draft):
    _saver, session, connection, _accepted = registry_db
    original = _create(connection, draft)
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute("UPDATE context_plans SET plan_state = 'sealed'")
    assert read_registration(connection, session, draft.thread_id, draft.plan_id) == original


def test_sealed_header_without_snapshot_is_not_a_dispatch_authority(registry_db, draft):
    _saver, session, connection, _accepted = registry_db
    _create(connection, draft)
    with connection:
        connection.execute(
            "UPDATE context_plans SET plan_state = 'sealed', assembly_id = 'forged', "
            "seal_idempotency_key = 'forged', seal_hash = 'forged', seal_input_hash = ?",
            (sha256_jcs("forged-input"),),
        )
        connection.execute("UPDATE tool_set_snapshots SET assembly_id = 'forged'")
    with pytest.raises(ValueError, match="缺少同一 assembly"):
        read_registration(connection, session, draft.thread_id, draft.plan_id)


def test_tool_registry_binding_is_atomic_and_plan_cannot_get_second_assembly(
    registry_db, draft
):
    _saver, session, connection, accepted = registry_db
    tools_only = replace(draft, refs=(), contributions=())
    _create(connection, tools_only)
    snapshot = ContextPlanComposer().assembly(
        plan=tools_only,
        assembly_id="tools-assembly",
        session_id=session,
        thread_id=tools_only.thread_id,
        turn_id=str(accepted["turn_id"]),
        execution_id=str(accepted["initial_execution_id"]),
        provider_version="fixture",
    )
    seal_options = {
        "seal_idempotency_key": "seal-tools",
        "detail_key": None,
        "seal_input_hash": sha256_jcs("storage-seal-test-input"),
    }
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = preflight_seal(connection, snapshot, **seal_options)
        _insert_snapshot(connection, snapshot)
        bind_sealed_registration(connection, registered)
    assert connection.execute(
        "SELECT assembly_id FROM tool_set_snapshots"
    ).fetchall() == [("tools-assembly",)]
    other = ContextPlanComposer().assembly(
        plan=tools_only,
        assembly_id="other-assembly",
        session_id=session,
        thread_id=tools_only.thread_id,
        turn_id=str(accepted["turn_id"]),
        execution_id=str(accepted["initial_execution_id"]),
        provider_version="fixture",
    )
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        preflight_seal(
            connection,
            other,
            seal_idempotency_key="seal-tools",
            detail_key=None,
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        1,
    )
