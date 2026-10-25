"""Saver seal/read/重启中 typed detail owner、用途和物理 shape 的集成合同。"""

from __future__ import annotations

import errno
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.plans.contributions import ContextContribution
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailUnavailableError,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    _project,
    projection_draft,
    projection_plan,
    projection_saver,
    projection_workspace_root,
    register_projection_draft,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)

__all__ = [
    "projection_draft",
    "projection_plan",
    "projection_saver",
    "projection_workspace_root",
]


@pytest.mark.parametrize("operation", ["read", "publish", "remove_paths"])
@pytest.mark.parametrize("damage", ["symlink_leaf", "symlink_parent", "parent_file", "target_directory"])
def test_detail_path_rejections_keep_physical_location_in_diagnostics(
    projection_saver, projection_plan, caplog, operation, damage,
):
    saver, session_id, session = projection_saver
    ref = projection_plan.selection[2].detail_ref
    target = session / detail_relative_path(ref)
    original = target.read_bytes()
    canonical = (session / "rollout/rollout.jsonl").read_bytes()
    files = saver._detail_store._files
    node = target.parent if damage in {"symlink_parent", "parent_file"} else target
    parked = node.with_name(node.name + ".private-original")
    node.rename(parked)
    if damage.startswith("symlink"):
        node.symlink_to(parked, target_is_directory=parked.is_dir())
    elif damage == "parent_file":
        node.write_bytes(b"private parent sentinel")
    else:
        node.mkdir()
    try:
        with pytest.raises(DetailUnavailableError) as failure:
            if operation == "read":
                saver.read_context_plan_detail(session_id, projection_plan.thread_id, detail_ref=ref)
            elif operation == "publish":
                files.publish(ref, original)
            else:
                files.remove_paths(ref)
        detail = str(failure.value)
        assert str(session) not in detail and str(node) not in detail
        assert ref.detail_id in detail and "stage=path" in detail
        assert any(str(node) in record.getMessage() for record in caplog.records)
        assert (session / "rollout/rollout.jsonl").read_bytes() == canonical
        body = parked / target.name if node == target.parent else parked
        assert body.read_bytes() == original
    finally:
        if damage == "target_directory":
            node.rmdir()
        else:
            node.unlink()
        parked.rename(node)
    assert files.read(ref, session_id=session_id, thread_id=projection_plan.thread_id) == original
    assert files.remove_paths(ref) == (target,)
    assert (session / "rollout/rollout.jsonl").read_bytes() == canonical


@pytest.mark.parametrize("stage", ["lstat", "mkdir", "resolve"])
@pytest.mark.parametrize("private", ["/private/detail/body", r"C:\private\detail\body", r"\\host\private\detail\body"])
def test_detail_path_native_failures_keep_original_cause_and_allow_retry(
    projection_saver, projection_plan, monkeypatch, caplog, stage, private,
):
    saver, _session_id, session = projection_saver
    ref = projection_plan.selection[2].detail_ref
    if stage == "mkdir":
        ref = replace(ref, assembly_id="new-assembly")
    target = session / detail_relative_path(ref)
    native_path = target.parent if stage == "mkdir" else target
    original_error = PermissionError(errno.EACCES, f"private native failure: {private}")
    original_method = getattr(Path, stage)

    def fail(path, *args, **kwargs):
        if path == native_path:
            raise original_error
        return original_method(path, *args, **kwargs)

    before = (session / "rollout/rollout.jsonl").read_bytes()
    with monkeypatch.context() as failure:
        failure.setattr(Path, stage, fail)
        with pytest.raises(DetailUnavailableError) as caught:
            saver._detail_store._files.path(ref, create=stage == "mkdir")
    detail = str(caught.value)
    assert private not in detail and str(session) not in detail
    assert ref.detail_id in detail and "stage=path" in detail
    assert "PermissionError" in detail and f"errno={errno.EACCES}" in detail
    assert caught.value.__cause__ is original_error
    assert any(record.exc_info and record.exc_info[1] is original_error for record in caplog.records)
    assert private in caplog.text
    assert (session / "rollout/rollout.jsonl").read_bytes() == before
    if stage == "mkdir":
        assert not target.parent.exists()
    assert saver._detail_store._files.path(ref, create=stage == "mkdir") == target


async def test_product_detail_rejection_preserves_history_and_repaired_source_can_fork(
    production_fork_container, fork_execution_message, request: pytest.FixtureRequest, caplog,
):
    container = production_fork_container
    source = await container.session_service.create(SessionCreateRequest(title="历史详情文件故障恢复"))
    accepted = await fork_execution_message(
        container, source.session_id, content="生成用于详情冷读的历史",
    )
    saver = container.checkpointer
    assembly, = saver._storage.assemblies.list_context_assemblies(source.session_id, accepted.thread_id)
    ref = next(entry.detail_ref for entry in assembly.selection if entry.detail_ref is not None)
    original = saver.read_context_plan_detail(
        source.session_id, accepted.thread_id, detail_ref=ref, include_sensitive=True,
    )
    node = container.session_service.path_resolver.resolve_thread_node(source.session_id, accepted.thread_id)
    canonical = (node / "rollout/rollout.jsonl").read_bytes()
    target = node / detail_relative_path(ref)
    raw = target.read_bytes()
    parked = target.with_name(target.name + ".private-original")
    target.rename(parked)
    target.symlink_to(parked)
    try:
        with pytest.raises(DetailUnavailableError) as failure:
            saver.read_context_plan_detail(source.session_id, accepted.thread_id, detail_ref=ref, include_sensitive=True)
        detail = str(failure.value)
        assert str(node) not in detail and "stage=path" in detail and ref.detail_id in detail
        assert any(str(target) in record.getMessage() for record in caplog.records)
        assert parked.read_bytes() == raw and (node / "rollout/rollout.jsonl").read_bytes() == canonical
    finally:
        target.unlink()
        parked.rename(target)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        assert cold.read_context_plan_detail(
            source.session_id, accepted.thread_id, detail_ref=ref, include_sensitive=True,
        ) == original
    context = _production_fork_context(container)
    fork_request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    copied = await context.forks.fork(fork_request)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        assert cold.read_context_plan_detail(
            copied.session.session_id, copied.thread_id, detail_ref=ref, include_sensitive=True,
        ) == original
        restored = cold._storage.assemblies.get_context_assembly(
            copied.session.session_id, copied.thread_id, assembly_id=assembly.assembly_id,
        )
        assert restored.request_hash == assembly.request_hash
        assert next(entry.detail_ref for entry in restored.selection if entry.detail_ref is not None) == ref
    assert target.read_bytes() == raw and (node / "rollout/rollout.jsonl").read_bytes() == canonical
    artifacts = TestRunContext.from_test_file(Path(request.node.path)).prepare().artifacts_dir
    (artifacts / "product-detail-recovery.json").write_text(json.dumps({
        "failure": detail, "source": source.session_id, "target": copied.session.session_id,
        "detail_ref": ref.to_dict(), "request_hash": assembly.request_hash,
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("change_identity", [False, True])
def test_plain_request_recovery_requires_explicit_source_identity(
    projection_saver, projection_draft, change_identity
) -> None:
    saver, session_id, _ = projection_saver
    draft, bodies = projection_draft
    draft = replace(draft, tool_set_refs=())
    draft = register_projection_draft(saver, session_id, draft)
    first = saver.seal_context_plan(session_id, saver._storage.storage.main_thread_id(session_id), draft, wire_profile=None, wire_params=None, seal_idempotency_key=f'seal:{draft.plan_id}', turn_id='turn-1', execution_id=saver.execution_for_turn(session_id, thread_id=saver._storage.storage.main_thread_id(session_id), turn_id='turn-1'), provider_version='contract-provider', request_only_content=bodies)
    # 新 plan 的 source locator 指向旧 selection，最终 detail 则必须重新分配。
    source = (
        DetailRef(
            session_id, draft.thread_id, "unrelated-assembly", "unrelated-detail"
        )
        if change_identity
        else first.selection[2].detail_ref
    )
    next_draft = replace(
        draft,
        plan_id="plain-recovery-next",
        refs=tuple(
            replace(
                ref,
                plan_id="plain-recovery-next",
                source_ref=source,
                ref_id="new-plain-ref",
            )
            if ref.ref_id == "plain-ref"
            else replace(ref, plan_id="plain-recovery-next")
            if ref.ref_type == "request_only"
            else ref
            for ref in draft.refs
        ),
    )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        next_draft = register_projection_draft(restarted, session_id, next_draft)
        arguments = {
            "seal_idempotency_key": f"seal:{next_draft.plan_id}",
            "turn_id": "turn-1",
            "execution_id": restarted.execution_for_turn(session_id, thread_id=restarted._storage.storage.main_thread_id(session_id), turn_id="turn-1"),
            "provider_version": "contract-provider",
        }
        if change_identity:
            with pytest.raises(
                DetailUnavailableError, match="detail-unavailable.*new-plain-ref"
            ):
                restarted.seal_context_plan(session_id, restarted._storage.storage.main_thread_id(session_id), next_draft, wire_profile=None, wire_params=None, **arguments)
            return
        second = restarted.seal_context_plan(session_id, restarted._storage.storage.main_thread_id(session_id), next_draft, wire_profile=None, wire_params=None, **arguments)
        entry = second.selection[2]
        assert entry.ref.source_ref == first.selection[2].detail_ref
        assert entry.detail_ref != entry.ref.source_ref
        entry.detail_ref.require_owner(
            session_id, second.thread_id, second.assembly_id
        )
        assert (
            _project(restarted, session_id, first.assembly_id)["native"]["request"]
            == _project(restarted, session_id, second.assembly_id)["native"]["request"]
        )
    # 再次打开 owner：source_ref 仍是旧 source，恢复只能读新 selection detail。
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        assert (
            _project(restarted, session_id, second.assembly_id)["native"]["selection"][
                2
            ]["detail_ref"]
            == entry.detail_ref.to_dict()
        )


def test_sealed_detail_shape_is_typed_and_not_part_of_context_ref(
    projection_saver, projection_plan
) -> None:
    saver, session_id, session = projection_saver
    snapshot = projection_plan
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        rows = connection.execute(
            "SELECT detail_ref FROM context_plan_details WHERE assembly_id = ?",
            (snapshot.assembly_id,),
        ).fetchall()
    refs = {detail_ref_from_key(row[0]) for row in rows}
    included = [entry for entry in snapshot.selection if entry.detail_ref is not None]
    assert refs == {entry.detail_ref for entry in included}
    assert len(included) == 2
    for entry in included:
        assert not hasattr(entry.ref, "detail_ref")
        assert "detail_ref" not in entry.ref.to_dict()
        ref = entry.detail_ref
        ref.require_owner(session_id, snapshot.thread_id, snapshot.assembly_id)
        assert set(ref.to_dict()) == {
            "session_id", "thread_id", "assembly_id", "detail_id"
        }
        path = session / detail_relative_path(ref)
        assert path.name == ref.detail_id and path.suffix == ""
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["detail_ref"] == ref.to_dict()
        assert not {"gc_after", "content_length"} & payload.keys()
        record = saver._storage.assemblies.get_context_plan_detail(session_id, saver._storage.storage.main_thread_id(session_id), detail_ref=ref)
        assert record["detail_kind"] == "request_source"
        assert record["retention_class"] == "request_replay"
        assert record["visibility"] == entry.visibility
        assert record["length"] == entry.content_length
        assert record["detail_id"] == ref.detail_id
        assert record["detail_ref"] == ref
        assert "expires_at" in record


@pytest.mark.parametrize(
    "shape", ["bare_id", "physical_path", "dict"]
)
def test_detail_read_rejects_untyped_input_before_storage(
    projection_saver, projection_plan, monkeypatch, shape
) -> None:
    saver, session_id, session = projection_saver
    detail = projection_plan.selection[1].detail_ref
    invalid = {
        "bare_id": detail.detail_id,
        "physical_path": str(session / detail_relative_path(detail)),
        "dict": detail.to_dict(),
    }[shape]
    calls = []

    def reject_lookup(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("detail owner/shape 必须在 registry lookup 前校验")

    with monkeypatch.context() as patch:
        patch.setattr(saver._storage.assemblies, "get_context_plan_detail", reject_lookup)
        with pytest.raises((TypeError, ValueError), match="source-mismatch"):
            saver.read_context_plan_detail(session_id, saver._storage.storage.main_thread_id(session_id), detail_ref=invalid)
    assert calls == []


@pytest.mark.parametrize("owner_field", ["session_id", "thread_id"])
def test_detail_read_rejects_unbound_historical_scope_before_body(
    projection_saver, projection_plan, monkeypatch, owner_field
) -> None:
    saver, session_id, _ = projection_saver
    detail = replace(
        projection_plan.selection[1].detail_ref,
        **{owner_field: f"foreign-{owner_field}"},
    )
    calls = []

    def reject_body_read(**kwargs):
        calls.append(kwargs)
        raise AssertionError("历史 scope 必须在正文读取前通过本地冻结 binding 校验")

    monkeypatch.setattr(saver._detail_store, "read", reject_body_read)
    with pytest.raises(ValueError, match="source-mismatch.*本地冻结 snapshot"):
        saver.read_context_plan_detail(
            session_id, projection_plan.thread_id, detail_ref=detail
        )
    assert calls == []


@pytest.mark.parametrize("owner_field", ["session_id", "assembly_id", "detail_id"])
def test_corrupt_typed_selection_binding_is_rejected_after_restart(
    projection_saver, projection_plan, owner_field
) -> None:
    saver, session_id, session = projection_saver
    original = projection_plan.selection[1].detail_ref
    changed = replace(original, **{owner_field: f"wrong-{owner_field}"})
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        connection.execute(
            "UPDATE context_assembly_selections SET detail_ref = ? "
            "WHERE assembly_id = ? AND plan_ordinal = 1",
            (detail_ref_key(changed), projection_plan.assembly_id),
        )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        for projector in (
            restarted.project_context_plan_to_messages,
            restarted.project_context_plan_to_native,
            restarted.project_context_plan_to_history,
        ):
            with pytest.raises(
                (RuntimeError, ValueError), match="mismatch|不一致|detail"
            ):
                projector(session_id, projection_plan.thread_id, projection_plan.as_sealed_plan())


@pytest.mark.parametrize(
    "port,projection_saver", [("seal", None), ("prepare", "protected")],
    indirect=["projection_saver"],
)
def test_seal_failure_keeps_prepared_details_out_of_durable_storage(frozen_provider_activation,
    frozen_provider_wire, projection_saver, monkeypatch, port
) -> None:
    saver, session_id, session = projection_saver
    thread = saver._storage.storage.main_thread_id(session_id)
    activation = (
        frozen_provider_activation(saver, plan_creation_idempotency_key='create:cleanup-prepare')
        if port == 'prepare' else {}
    )

    body = [{"type": "text", "text": "typed cleanup source"}]
    contribution = ContextContribution(
        contribution_id="cleanup-source",
        source_kind="prompt",
        source_revision="source-v1",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
    )
    saver.register_context_contribution(session_id, thread, contribution, request_content=body)
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session_id, thread), control_state_port=saver,
    )
    manager.register(ContextSourceDescriptor(
        source_id="agents:cleanup-source", source_kind="workspace_agents",
        name="cleanup source", description="详情清理来源",
        internal_locator="boxteam://workspace/test/resources/agent-spec/root/AGENTS.md",
    ), tracking_status="tracked")
    manager.observe("agents:cleanup-source", json.dumps(body), revision="source-v1")
    selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
    tables = (
        "item_catalog", "item_projections", "context_source_control_states", "context_epoch_transitions",
        "context_plan_details", "context_assemblies", "assembly_item_refs",
        "context_assembly_contributions", "context_assembly_selections", "storage_commits",
        "database_meta", "model_calls", "checkpoint_context_source_snapshots", "checkpoint_context_source_states",
    )
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        contribution_ids = tuple(row[0] for row in connection.execute(
            "SELECT contribution_id FROM context_contributions ORDER BY contribution_id"
        ).fetchall())
        plan_ids = tuple(row[0] for row in connection.execute(
            "SELECT plan_id FROM context_plans ORDER BY plan_id"
        ).fetchall())
    contribution_placeholders = ",".join("?" for _ in contribution_ids)
    plan_placeholders = ",".join("?" for _ in plan_ids)

    def durable_facts():
        with sqlite3.connect(session / "rollout/index.sqlite") as connection:
            return (
                tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables),
                connection.execute(
                    f"SELECT * FROM context_contributions WHERE contribution_id IN ({contribution_placeholders}) "
                    "ORDER BY contribution_id", contribution_ids,
                ).fetchall(),
                tuple(connection.execute(
                    f"SELECT * FROM {table} WHERE plan_id IN ({plan_placeholders}) ORDER BY rowid", plan_ids,
                ).fetchall() for table in (
                    "context_plans", "context_plan_refs", "context_plan_contributions", "tool_set_snapshots",
                )),
                tuple((key, tuple(item["assembly_id"] for item in queue))
                      for key, queue in sorted(saver._prepared_dispatches.items())),
                tuple((path.relative_to(session).as_posix(), path.read_bytes())
                      for path in sorted(session.rglob("*")) if path.is_file() and (
                          "context-plan-details" in path.parts
                      )),
            )

    before = durable_facts()

    def fail_seal(*args, **kwargs):
        raise RuntimeError("injected assembly commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(saver._storage.assemblies, "seal_context_assembly", fail_seal)
        arguments = {
            "turn_id": "turn-1",
            "provider_version": "typed-contract",
            "execution_id": saver.execution_for_turn(session_id, thread_id=saver._storage.storage.main_thread_id(session_id), turn_id="turn-1"),
        }
        with pytest.raises(RuntimeError, match="injected assembly commit failure"):
            if port == "seal":
                draft = saver.compose_committed_context_plan(
                    session_id, thread, plan_id="cleanup-plan", tool_set=selected,
                )
                draft = register_projection_draft(saver, session_id, draft)
                saver.seal_context_plan(
                    session_id, thread,
                    draft,
                    wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{draft.plan_id}",
                    **arguments,
                )
            else:
                saver.prepare_context_for_provider(
                    session_id, thread,
                    plan_creation_idempotency_key="create:cleanup-prepare",
                    seal_idempotency_key="seal:cleanup-prepare",
                    turn_id="turn-1",
                    execution_id=str(arguments["execution_id"]),
                    context_source_manager=manager, tool_set_supplier=lambda: selected,
                    **frozen_provider_wire("typed-contract", "responses"),

                    **activation,
                )
    assert durable_facts() == before
    assert saver.list_context_assemblies(session_id, saver._storage.storage.main_thread_id(session_id)) == ()
    assert manager.prepare_pending() is not None
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        plans = connection.execute(
            f"SELECT plan_id, plan_state, assembly_id, seal_idempotency_key, seal_hash, seal_input_hash "
            f"FROM context_plans WHERE plan_id NOT IN ({plan_placeholders}) ORDER BY plan_id", plan_ids,
        ).fetchall()
        assert len(plans) == 1
        assert plans[0][1:] == ("unsealed", None, None, None, None)
        assert connection.execute(
            "SELECT assembly_id FROM tool_set_snapshots WHERE plan_id=?", (plans[0][0],),
        ).fetchall() == [(None,)]
        additions = connection.execute(
            f"SELECT request_only, assembly_id, contribution_ordinal, binding_manifest_json "
            f"FROM context_contributions WHERE contribution_id NOT IN ({contribution_placeholders})",
            contribution_ids,
        ).fetchall()
        for request_only, assembly_id, contribution_ordinal, manifest in additions:
            assert (request_only, assembly_id, contribution_ordinal) == (1, None, None)
            assert set(json.loads(manifest)) == {"root_placement", "source_binding", "extensions"}
