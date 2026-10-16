"""当前 schema 的真实 assembly seal/registry/重启读取合同。

写入调用 assembly storage 的事务 port，读取经过重启 Saver；不替代
checkpoint 的公开 seal 调用链验收，也不安装测试 DDL 或回填旧 registry。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.enums import DetailProtection
from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    ProviderProfileItemFrame,
    append_items,
    initial_epoch_state,
    seal_assembly,
)
from app.domain.itemized.epoch.seal_binding import ContextSealBinding
from app.domain.itemized.epoch.tool_set import tool_set_manifest_compatibility_key
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.refs import ContextRef, ToolSetIdentityRef, ToolSetRef
from app.domain.itemized.request_plan import (
    ContextContribution,
    ContextRequestPlan,
    SourceBinding,
)
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.rollout_context.assembly.manifest import (
    ContextAssemblyManifestMixin,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_SCHEMA_VERSION,
)
from tests.harness.python.run_context import TestRunContext


@dataclass(frozen=True)
class AssemblyCase:
    saver: RolloutCheckpointSaver
    sessions: Path
    session_id: str
    thread_id: str
    turn_id: str
    execution_id: str
    connection: sqlite3.Connection
    jsonl_path: Path


@pytest.fixture
def assembly_case(
    request: pytest.FixtureRequest,
    session_bundle_factory: Callable[[Path, str], Path],
) -> Iterator[AssemblyCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    saver = RolloutCheckpointSaver(sessions)
    accepted = saver.accept_turn(
        session_id,
        accepted_ingress_id="manifest-ingress",
        acceptance_idempotency_key="manifest-acceptance",
        job_id="manifest-job",
        admission_idempotency_key="manifest-admission",
        payload="assembly 恢复必须使用同一 plan registry",
        payload_kind="text",
    )
    with saver._storage.storage._connect(
        saver._storage.storage.main_thread_id(session_id),
        "",
        read_only=True,
        session_id=session_id,
    ) as connection:
        database_path = connection.execute("PRAGMA database_list").fetchone()[2]
    # 默认关闭 FK 的独立诊断连接仅用于显式损坏注入，不改变生产连接约束。
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT schema_version FROM database_meta WHERE singleton_id = 1"
        ).fetchone() == (ROLLOUT_SCHEMA_VERSION,)
        yield AssemblyCase(
            saver,
            sessions,
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            str(accepted["turn_id"]),
            str(accepted["initial_execution_id"]),
            connection,
            saver._storage.storage.jsonl_path(session_id),
        )
    finally:
        connection.close()


def _candidate(
    case: AssemblyCase, plan_id: str, *, admission_root: bool = False,
    extra_source_ids: tuple[str, ...] = (),
) -> ContextAssemblySnapshot:
    contributions = tuple(
        ContextContribution(
            contribution_id=name,
            source_kind="workspace_instructions",
            source_revision=f"{plan_id}-revision",
            body={"instructions": f"{plan_id}/{name} 的独立正文"},
            content_hash=contribution_content_hash(
                "prompt",
                {"instructions": f"{plan_id}/{name} 的独立正文"},
            ),
            extensions={"boxteam.test.annotation": {"extension_version": "1", "protection": "public", "value": {'owner': plan_id}}} , source_binding=SourceBinding(source_ref=name),
            source_ordinal=ordinal,
        )
        for ordinal, name in enumerate(
            ("included-source", "omitted-source", *extra_source_ids)
        )
    )
    refs = tuple(
        ContextRef.request_only_ref(
            f"{item.contribution_id}-ref",
            session_id=case.session_id,
            thread_id=case.thread_id,
            plan_id=plan_id,
            source_revision=item.source_revision,
            source_ref=item.contribution_id,
            content_hash_value=item.content_hash,
            content_length=item.content_length,
        )
        for item in contributions
    )
    active_view_id = None
    omitted_ref_ids = [
        "omitted-source-ref", "omitted-tools",
        *(f"{source_id}-ref" for source_id in extra_source_ids),
    ]
    if admission_root:
        root_id = case.connection.execute(
            "SELECT root_input_item_id FROM turn_records "
            "WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
            (case.session_id, case.thread_id, case.turn_id),
        ).fetchone()[0]
        root_item = case.saver._storage.storage.read_items(
            case.session_id, case.thread_id, item_ids=(root_id,),
        )[0]
        active_view_id = case.connection.execute(
            "SELECT branch.head_view_id FROM checkpoint_namespace_state AS namespace "
            "JOIN branches AS branch ON branch.branch_id = namespace.active_branch_id "
            "WHERE namespace.checkpoint_ns = ''"
        ).fetchone()[0]
        refs = (
            ContextRef.canonical_item(
                root_item, session_id=case.session_id, thread_id=case.thread_id,
            ),
            *refs,
        )
        omitted_ref_ids.append(root_id)
    plan = ContextRequestPlan(
        session_id=case.session_id,
        thread_id=case.thread_id,
        plan_id=plan_id,
        plan_creation_idempotency_key=f"create-{plan_id}",
        refs=refs,
        contributions=contributions,
        tool_set_refs=tuple(
            ToolSetRef.from_tool_snapshot(
                session_id=case.session_id,
                thread_id=case.thread_id,
                plan_id=plan_id,
                snapshot_id=name,
                source_revision=sha256_jcs(f"{plan_id}-tools"),
                tools=({"name": name, "parameters": {"type": "object"}},),
                tool_policy={"mode": "auto"},
            )
            for name in ("included-tools", "omitted-tools")
        ),
        active_view_id=active_view_id,
        selection_policy="compaction_selection" if admission_root else "active_view",
    )
    registered = case.saver.create_context_plan(case.session_id, plan)
    assembly_id = f"assembly-{plan_id}"
    detail = ContextPlanDetailStore(case.sessions).write(
        session_id=case.session_id,
        thread_id=case.thread_id,
        assembly_id=assembly_id,
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail=contributions[0].body,
        source_revision=contributions[0].source_revision,
    )
    case.saver._storage.assemblies.register_context_plan_detail(detail)
    return ContextPlanComposer().assembly(
        plan=registered.draft,
        assembly_id=assembly_id,
        session_id=case.session_id,
        thread_id=case.thread_id,
        turn_id=case.turn_id,
        execution_id=case.execution_id,
        provider_version="schema4-manifest-contract",
        request_detail_refs={"included-source-ref": detail.detail_ref},
        omitted_ref_ids=tuple(omitted_ref_ids),
    )


def _with_admission_root_binding(
    snapshot: ContextAssemblySnapshot, case: AssemblyCase,
) -> ContextAssemblySnapshot:
    admission = case.saver.execution_admission(case.session_id, thread_id=case.thread_id, execution_id=case.execution_id)
    notice_ref, _selection = case.saver._storage.executions.runtime_notice_acceptance(admission, connection=case.connection)
    tool_ref = next(
        entry.ref
        for entry in snapshot.selection
        if entry.included and isinstance(entry.ref, ToolSetRef)
    )
    compatibility_key = tool_set_manifest_compatibility_key(tool_ref)
    state = initial_epoch_state(
        provider_profile=snapshot.provider_version,
        tool_compatibility_key=compatibility_key,
        turn_id=snapshot.turn_id,
    )
    source_entry = next(
        entry
        for entry in snapshot.selection
        if entry.included and entry.ref.ref_type == "request_only"
    )
    contribution = next(
        item for item in snapshot.contributions
        if item.contribution_id == source_entry.contribution_id
    )
    source_binding = contribution.source_binding
    assert source_binding is not None and source_binding.source_ref is not None
    frame = ProviderProfileItemFrame(
        ordinal=1,
        provider_profile=state.provider_profile,
        role="user",
        payload_kind="structured_content",
        content=contribution.body,
        item_ref=AppendedItemRef(
            "source_item",
            source_entry.ref.ref_id,
            source_binding.source_ref,
            source_entry.ref.source_revision,
            "tracked",
        ),
    )
    state = append_items(
        state,
        (frame,),
        desired_provider_profile=state.provider_profile,
        desired_tool_compatibility_key=state.tool_compatibility_key,
    )
    prefix = seal_assembly(
        state,
        assembly_id=snapshot.assembly_id,
    )
    return replace(
        snapshot,
        seal_binding=ContextSealBinding(
            prefix=prefix,
            root=None,
            tool_set_snapshot_id=tool_ref.ref_id,
            applied_toolset_revision=tool_ref.source_revision,
            selection_revision=sha256_jcs({"plan_id": snapshot.plan_id}),
            previous_assembly_id=None,
            notice_acceptance=notice_ref,
        ),
    )


def _seal(case: AssemblyCase, plan_id: str) -> ContextAssemblySnapshot:
    snapshot = _candidate(case, plan_id)
    _seal_snapshot(case, snapshot)
    return snapshot


def _seal_snapshot(case: AssemblyCase, snapshot: ContextAssemblySnapshot) -> None:
    case.saver._storage.assemblies.seal_context_assembly(
        case.session_id, case.thread_id, snapshot,
        seal_idempotency_key=f"seal-{snapshot.plan_id}",
        seal_input_hash=sha256_jcs("storage-seal-test-input"),
    )


def test_admission_root_uses_visible_canonical_membership_outside_selection(assembly_case):
    case = assembly_case
    snapshot = _with_admission_root_binding(
        _candidate(case, "admission-root-visible", admission_root=True), case
    )
    root_id = case.connection.execute(
        "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
        (case.turn_id,),
    ).fetchone()[0]
    root_membership = case.connection.execute(
        "SELECT visible, source_kind FROM context_view_items "
        "WHERE view_id = ? AND item_id = ?",
        (snapshot.active_view_id, root_id),
    ).fetchone()
    root_selection = next(
        entry for entry in snapshot.selection if entry.ref.ref_id == root_id
    )
    assert root_membership == (1, "canonical")
    assert root_selection.ref.ref_type == "canonical_item" and not root_selection.included

    _seal_snapshot(case, snapshot)
    assert case.connection.execute(
        "SELECT source_cut_position_kind, source_cut_item_id "
        "FROM context_assembly_selections WHERE assembly_id = ? "
        "AND ref_id = 'included-source-ref'",
        (snapshot.assembly_id,),
    ).fetchone() == ("start", None)
    _validate(case, snapshot)


def test_seal_rejects_unknown_snapshot_turn_without_writes(assembly_case):
    case = assembly_case
    snapshot = _with_admission_root_binding(
        _candidate(case, "unknown-snapshot-turn", admission_root=True), case
    )
    foreign_turn_snapshot = replace(snapshot, turn_id="foreign-turn")

    before = _state(case)
    with pytest.raises(KeyError, match="assembly Turn 不存在: foreign-turn"):
        _seal_snapshot(case, foreign_turn_snapshot)
    assert _state(case) == before
    assert case.connection.execute(
        "SELECT count(*) FROM context_assemblies WHERE assembly_id = ?",
        (snapshot.assembly_id,),
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "corruption",
    ["missing_root", "wrong_turn_owner", "wrong_root_owner", "invisible", "noncanonical"],
)
def test_seal_rejects_missing_foreign_or_noncanonical_admission_root(
    assembly_case, corruption,
):
    case = assembly_case
    snapshot = _with_admission_root_binding(
        _candidate(case, f"invalid-admission-root-{corruption}", admission_root=True), case
    )
    root_id = case.connection.execute(
        "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
        (case.turn_id,),
    ).fetchone()[0]
    with case.connection:
        if corruption == "missing_root":
            case.connection.execute(
                "UPDATE turn_records SET root_input_item_id = ? WHERE turn_id = ?",
                ("missing-admission-root", case.turn_id),
            )
        elif corruption == "wrong_turn_owner":
            case.connection.execute(
                "UPDATE turn_records SET session_id = 'foreign-session' WHERE turn_id = ?",
                (case.turn_id,),
            )
        elif corruption == "wrong_root_owner":
            case.connection.execute(
                "UPDATE item_catalog SET turn_id = 'foreign-turn' WHERE item_id = ?",
                (root_id,),
            )
        elif corruption == "invisible":
            case.connection.execute(
                "UPDATE context_view_items SET visible = 0 "
                "WHERE view_id = ? AND item_id = ?",
                (snapshot.active_view_id, root_id),
            )
        else:
            case.connection.execute(
                "UPDATE context_view_items SET source_kind = 'derived' "
                "WHERE view_id = ? AND item_id = ?",
                (snapshot.active_view_id, root_id),
            )

    before = _state(case)
    expected_message = (
        "source-mismatch: canonical item 的 Turn/execution 历史 owner 不一致"
        if corruption == "wrong_turn_owner"
        else "source-cut-frontier-conflict|assembly Turn 不存在"
    )
    with pytest.raises(
        (KeyError, RuntimeError, ValueError),
        match=expected_message,
    ):
        _seal_snapshot(case, snapshot)
    assert _state(case) == before
    assert case.connection.execute(
        "SELECT count(*) FROM context_assemblies WHERE assembly_id = ?",
        (snapshot.assembly_id,),
    ).fetchone() == (0,)


def _state(case: AssemblyCase) -> tuple[bytes, tuple[str, ...]]:
    return case.jsonl_path.read_bytes(), tuple(case.connection.iterdump())


def _validate(case: AssemblyCase, snapshot: ContextAssemblySnapshot) -> None:
    ContextAssemblyManifestMixin()._validate_context_assembly_manifest(
        case.connection,
        snapshot,
        registration=read_registration(
            case.connection, case.session_id, case.thread_id, snapshot.plan_id,
        ),
    )


def _assert_recovery_rejected(
    case: AssemblyCase, snapshot: ContextAssemblySnapshot, *, match: str
) -> None:
    before = _state(case)
    with pytest.raises((KeyError, RuntimeError, ValueError), match=match):
        _validate(case, snapshot)
    restarted = RolloutCheckpointSaver(case.sessions)
    with pytest.raises((KeyError, RuntimeError, ValueError), match=match):
        restarted.get_context_assembly(
            case.session_id, case.thread_id, assembly_id=snapshot.assembly_id
        )
    assert _state(case) == before


def test_cold_manifest_matches_refs_with_linear_selection_work(
    assembly_case, monkeypatch: pytest.MonkeyPatch,
):
    case = assembly_case
    snapshot = _candidate(
        case, "linear-manifest",
        extra_source_ids=tuple(f"extra-source-{ordinal}" for ordinal in range(62)),
    )
    _seal_snapshot(case, snapshot)
    before = _state(case)
    original_validate = ContextAssemblyManifestMixin._validate_context_assembly_manifest
    original_getattribute = ContextSelectionEntry.__getattribute__
    ref_reads: list[int] = []

    def counted_manifest(owner, connection, persisted_snapshot, **kwargs):
        selected_ids = {id(entry) for entry in persisted_snapshot.selection}
        reads = 0

        def counted_ref(entry, name):
            nonlocal reads
            if name == "ref" and id(entry) in selected_ids:
                reads += 1
            return original_getattribute(entry, name)

        with monkeypatch.context() as guard:
            guard.setattr(ContextSelectionEntry, "__getattribute__", counted_ref)
            original_validate(owner, connection, persisted_snapshot, **kwargs)
        ref_reads.append(reads)

    monkeypatch.setattr(
        ContextAssemblyManifestMixin, "_validate_context_assembly_manifest",
        counted_manifest,
    )
    restored = RolloutCheckpointSaver(case.sessions).get_context_assembly(
        case.session_id, case.thread_id, assembly_id=snapshot.assembly_id,
    )

    # 经真实注册、封存和冷恢复检查工作量，避免把逐 ref 扫描藏在 helper 内。
    assert len(ref_reads) == 1
    assert 0 < ref_reads[0] <= 12 * len(snapshot.selection)
    assert restored.to_dict() == snapshot.to_dict()
    assert _state(case) == before


def test_plan_scoped_sources_restore_without_session_registry(assembly_case):
    case = assembly_case
    before_jsonl = case.jsonl_path.read_bytes()
    snapshots = tuple(_seal(case, plan_id) for plan_id in ("first", "second"))
    assert case.connection.execute(
        "SELECT plan_id, contribution_id FROM context_plan_contributions ORDER BY 1, 2"
    ).fetchall() == [
        (plan, source)
        for plan in ("first", "second")
        for source in ("included-source", "omitted-source")
    ]

    def deny_old_registry(action, table, _column, _database, _trigger):
        if action == sqlite3.SQLITE_READ and table == "context_contributions":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    stable = _state(case)
    case.connection.set_authorizer(deny_old_registry)
    try:
        for snapshot in snapshots:
            _validate(case, snapshot)
    finally:
        case.connection.set_authorizer(None)
    restarted = RolloutCheckpointSaver(case.sessions)
    for snapshot in snapshots:
        restored = restarted.get_context_assembly(
            case.session_id, case.thread_id,
            assembly_id=snapshot.assembly_id,
        )
        assert restored.to_dict() == snapshot.to_dict()
        assert [entry.plan_ordinal for entry in restored.selection] == [0, 1, 2, 3]
        assert [entry.included for entry in restored.selection] == [
            True,
            False,
            True,
            False,
        ]
        included, omitted, tools, omitted_tools = restored.selection
        assert included.contribution_id == "included-source"
        assert included.contribution_ordinal == 0 and included.detail_ref is not None
        assert restarted.read_context_plan_detail(
            case.session_id, case.thread_id,
            detail_ref=included.detail_ref,
        )["detail"] == {
            "instructions": f"{snapshot.plan_id}/included-source 的独立正文"
        }
        assert tools.ref.ref_type == "tool_set" and tools.detail_ref is None
        for entry in (omitted, omitted_tools):
            assert entry.omission_reason == "selection_omitted"
            assert entry.loss == ("selection_omitted",)
            assert entry.detail_ref is None and entry.contribution_ordinal is None
        assert "tools" not in omitted_tools.to_dict()["ref"]
        assert "tool_policy" not in omitted_tools.to_dict()["ref"]
    assert case.jsonl_path.read_bytes() == before_jsonl
    assert _state(case) == stable


def test_missing_plan_is_rejected_before_assembly_indices_without_repair(assembly_case):
    case = assembly_case
    snapshot = _seal(case, "missing")
    with case.connection:
        for table in (
            "context_plan_refs",
            "context_plan_contributions",
            "tool_set_snapshots",
            "context_plans",
        ):
            case.connection.execute(f"DELETE FROM {table}")
    statements: list[str] = []
    case.connection.set_trace_callback(statements.append)
    try:
        _assert_recovery_rejected(case, snapshot, match="context plan 不存在")
    finally:
        case.connection.set_trace_callback(None)
    # iterdump 的引号查询不计入被测 manifest 的读取顺序。
    assert not any("FROM assembly_item_refs WHERE" in sql for sql in statements)
    assert case.connection.execute("SELECT count(*) FROM context_plans").fetchone() == (
        0,
    )


def test_unsealed_registration_cannot_authorize_assembly_manifest(assembly_case):
    case = assembly_case
    candidate = _candidate(case, "unsealed")
    before = _state(case)
    with pytest.raises(RuntimeError, match="plan 尚未 sealed"):
        _validate(case, candidate)
    assert _state(case) == before
    assert case.connection.execute(
        "SELECT count(*) FROM context_assemblies"
    ).fetchone() == (0,)


def test_registration_cannot_authorize_another_assembly(assembly_case):
    case = assembly_case
    snapshot = _seal(case, "bound")
    # 用真实 domain composer 生成同 plan 的另一份合法 candidate，不伪造对象字段。
    registered = case.saver.get_context_plan_registration(
        case.session_id, thread_id=case.thread_id, plan_id="bound"
    )
    other = ContextPlanComposer().assembly(
        plan=registered.draft,
        assembly_id="wrong-assembly",
        session_id=case.session_id,
        thread_id=case.thread_id,
        turn_id=case.turn_id,
        execution_id=case.execution_id,
        provider_version="schema4-manifest-contract",
        omitted_ref_ids=tuple(ref.ref_id for ref in registered.draft.refs),
    )
    before = _state(case)
    with pytest.raises(RuntimeError, match="registry binding 不一致"):
        _validate(case, other)
    assert _state(case) == before
    _validate(case, snapshot)


@pytest.mark.parametrize(
    "table", ["context_plan_refs", "context_plan_contributions", "tool_set_snapshots"]
)
def test_missing_plan_indices_fail_closed(assembly_case, table):
    case = assembly_case
    snapshot = _seal(case, "missing-index")
    with case.connection:
        case.connection.execute(f"DELETE FROM {table}")
    _assert_recovery_rejected(case, snapshot, match="source-mismatch")
    assert case.connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize("tamper", ["seal_hash", "snapshot_hash", "source_ordinal"])
def test_registry_hash_and_metadata_are_exact_without_legacy_tolerance(
    assembly_case, tamper
):
    case = assembly_case
    snapshot = _seal(case, "exact")
    with case.connection:
        if tamper == "seal_hash":
            case.connection.execute("UPDATE context_plans SET seal_hash = 'forged'")
        elif tamper == "snapshot_hash":
            raw = snapshot.to_dict()
            raw["provider_version"] = "forged-provider"
            case.connection.execute(
                "UPDATE context_assemblies SET snapshot_json = ?",
                (canonical_json_bytes(raw).decode(),),
            )
        else:
            raw = json.loads(
                case.connection.execute(
                    "SELECT manifest_json FROM context_plan_contributions WHERE contribution_id = 'included-source'"
                ).fetchone()[0]
            )
            raw["source_ordinal"] = 999
            case.connection.execute(
                "UPDATE context_plan_contributions SET manifest_json = ? WHERE contribution_id = 'included-source'",
                (canonical_json_bytes(raw).decode(),),
            )
    _assert_recovery_rejected(case, snapshot, match="source-mismatch|request_hash")


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("assembly_item_refs", "ref_ordinal", 9),
        ("assembly_item_refs", "source_revision", "changed"),
        ("assembly_item_refs", "contribution_id", "changed"),
        ("context_assembly_contributions", "contribution_ordinal", 9),
        ("context_assembly_contributions", "content_length", 999),
        ("context_assembly_contributions", "binding_manifest_json", '{"extensions":{},"source_binding":null,"root_placement":"root_eligible"}'),
        ("context_assembly_selections", "plan_ordinal", 9),
        ("context_assembly_selections", "content_hash", "changed"),
        ("context_assembly_selections", "detail_ref", "changed"),
        ("context_assembly_selections", "loss_json", '["changed"]'),
    ],
)
def test_assembly_indices_keep_fieldwise_validation(
    assembly_case, table, column, value
):
    case = assembly_case
    snapshot = _seal(case, "index-fields")
    with case.connection:
        case.connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE rowid = (SELECT min(rowid) FROM {table})",
            (value,),
        )
    _assert_recovery_rejected(case, snapshot, match="context assembly .* manifest")


@pytest.mark.parametrize("ref_id", ["included-tools", "omitted-tools"])
def test_tool_identity_corruption_is_checked_by_plan_registry(assembly_case, ref_id):
    case = assembly_case
    snapshot = _seal(case, "tool-identity")
    with case.connection:
        case.connection.execute(
            "UPDATE tool_set_snapshots SET source_revision = 'changed' WHERE tool_set_snapshot_id = ?",
            (ref_id,),
        )
    _assert_recovery_rejected(
        case, snapshot, match="plan ToolSetSnapshot registry 不一致"
    )


def test_manifest_does_not_read_omitted_candidate_tools(assembly_case, monkeypatch):
    case = assembly_case
    snapshot = _seal(case, "omitted-body")
    omitted = tuple(
        entry.ref
        for entry in snapshot.selection
        if not entry.included and isinstance(entry.ref, ToolSetIdentityRef)
    )
    assert tuple(ref.ref_id for ref in omitted) == ("omitted-tools",)
    assert all(not isinstance(ref, ToolSetRef) for ref in omitted)
    assert tuple(ref.ref_id for ref in snapshot.tool_set_refs) == ("included-tools",)
    guarded = {id(ref) for ref in omitted}
    original = ToolSetIdentityRef.__getattribute__

    def reject_definition_read(ref, name):
        if id(ref) in guarded and name in {"tools", "tool_policy"}:
            pytest.fail(f"manifest 读取了 omitted candidate 正文: {name}")
        return original(ref, name)

    before = _state(case)
    with monkeypatch.context() as guard:
        guard.setattr(ToolSetIdentityRef, "__getattribute__", reject_definition_read)
        _validate(case, snapshot)
    assert _state(case) == before


def test_domain_enum_snapshot_uses_canonical_json_boundary(assembly_case):
    case = assembly_case
    original = _candidate(case, "enum-boundary")
    snapshot = replace(
        original,
        selection=tuple(
            replace(entry, protection=DetailProtection.PUBLIC)
            for entry in original.selection
        ),
    )
    assert all(
        type(entry.protection) is DetailProtection for entry in snapshot.selection
    )
    snapshot.validate_hashes()
    before_jsonl = case.jsonl_path.read_bytes()
    case.saver._storage.assemblies.seal_context_assembly(
        case.session_id, case.thread_id, snapshot,
        seal_idempotency_key="seal-enum-boundary",
        seal_input_hash=sha256_jcs("storage-seal-test-input"),
    )
    _validate(case, snapshot)
    rows = case.connection.execute(
        "SELECT protection FROM context_assembly_selections ORDER BY plan_ordinal"
    ).fetchall()
    assert rows == [("public",)] * 4
    assert all(type(row[0]) is str for row in rows)
    restored = RolloutCheckpointSaver(case.sessions).get_context_assembly(
        case.session_id, case.thread_id,
        assembly_id=snapshot.assembly_id,
    )
    assert restored.to_dict() == snapshot.to_dict()
    assert all(type(entry.protection) is str for entry in restored.selection)
    assert case.jsonl_path.read_bytes() == before_jsonl


@pytest.mark.parametrize(
    "contribution_id", ["omitted-source", "unknown-source", "included-source"]
)
def test_omitted_existing_contribution_binding_uses_plan_owner(
    assembly_case, contribution_id
):
    case = assembly_case
    snapshot = _candidate(case, "omitted-binding")
    draft = case.saver.get_context_plan_registration(
        case.session_id,
        thread_id=case.thread_id,
        plan_id=snapshot.plan_id,
    ).draft
    selection = tuple(
        replace(entry, contribution_id=contribution_id)
        if entry.ref.ref_id == "omitted-source-ref"
        else entry
        for entry in snapshot.selection
    )
    if contribution_id == "included-source":
        before = _state(case)
        with pytest.raises(ValueError, match="plan-order-integrity"):
            draft.seal_for_assembly(snapshot.assembly_id, selection=selection)
        assert _state(case) == before
        return
    sealed = draft.seal_for_assembly(snapshot.assembly_id, selection=selection)
    snapshot = replace(
        snapshot,
        selection=selection,
        plan_hash=sealed.plan_hash(),
        request_hash=context_request_hash(
            sealed,
            snapshot.provider_version,
            projector_id=snapshot.projector_id,
            projector_version=snapshot.projector_version,
            target_format=snapshot.target_format,
        ),
    )
    snapshot.validate_hashes()
    before = _state(case)
    if contribution_id != "omitted-source":
        with pytest.raises(ValueError, match="plan-order-integrity|source-mismatch"):
            case.saver._storage.assemblies.seal_context_assembly(
                case.session_id, case.thread_id, snapshot,
                seal_idempotency_key="seal-omitted-binding",
                seal_input_hash=sha256_jcs("storage-seal-test-input"),
            )
        assert _state(case) == before
        return
    case.saver._storage.assemblies.seal_context_assembly(
        case.session_id, case.thread_id, snapshot,
        seal_idempotency_key="seal-omitted-binding",
        seal_input_hash=sha256_jcs("storage-seal-test-input"),
    )
    restored = RolloutCheckpointSaver(case.sessions).get_context_assembly(
        case.session_id, case.thread_id,
        assembly_id=snapshot.assembly_id,
    )
    entry = restored.selection[1]
    assert not entry.included and entry.contribution_id == "omitted-source"
    assert entry.detail_ref is None and entry.contribution_ordinal is None
    assert entry.omission_reason == "selection_omitted" and entry.loss == (
        "selection_omitted",
    )
    assert case.jsonl_path.read_bytes() == before[0]
