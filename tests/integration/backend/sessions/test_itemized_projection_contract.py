"""经过真实 Saver/SQLite/JSONL/detail 的跨投影与进程恢复合同。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import ContextContribution, SourceBinding
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_projection_helpers import (
    _project,
    overlay_source,
    projection_draft,
    projection_plan,
    projection_saver,
    projection_workspace,
    register_projection_draft,
)

# pytest 通过本模块导出的 fixture 注入独立的正式测试工作区。
__all__ = [
    "overlay_source",
    "projection_draft",
    "projection_plan",
    "projection_saver",
    "projection_workspace",
]


EMPTY_TOOL_SET = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())


def test_saver_seal_restores_same_selection_across_projections_and_process(
    projection_saver,
    projection_plan,
) -> None:
    saver, session_id, _ = projection_saver
    snapshot = projection_plan
    plan = snapshot.as_sealed_plan()
    messages = saver.project_context_plan_to_messages(
        session_id, saver._storage.storage.main_thread_id(session_id), plan
    )
    assert [type(message) for message in messages] == [
        AIMessage,
        SystemMessage,
        HumanMessage,
    ]
    assert [entry.ref.ref_id for entry in plan.selection[:4]] == [
        "item-answer-1",
        "plan-ref-a",
        "plain-ref",
        "item-user-1",
    ]
    assert plan.selection[1].contribution_id == "contribution-z"
    assert plan.selection[1].contribution_ordinal == 0
    assert plan.selection[2].contribution_id is None
    assert plan.selection[2].contribution_ordinal is None
    assert all(entry.detail_ref for entry in plan.selection[1:3])
    merged_metadata = messages[1].response_metadata["selection"]
    for entry, metadata in zip(plan.selection[1:3], merged_metadata, strict=True):
        assert metadata["context_ref_id"] == entry.ref.ref_id
        assert metadata["source_ref"] == entry.ref.source_ref
        assert metadata["content_hash"] == entry.content_hash
        assert metadata["content_length"] == entry.content_length
        assert metadata["detail_ref"] == entry.detail_ref.to_dict()
        assert metadata["loss"] == entry.loss
    before = _project(saver, session_id, snapshot.assembly_id)
    assert [item["role"] for item in before["native"]["request"]["input"]] == [
        "assistant",
        "system",
        "system",
        "user",
    ]
    assert before["native"]["request"]["tools"][0]["name"] == "read_file"
    assert before["native"]["selection"] == before["selection"]
    assert before["native"]["plan_hash"] == before["plan_hash"]
    assert [message["id"] for message in before["history"]] == ["answer-1", "user-1"]
    assert {item.item_id for item in saver.read_canonical_items(session_id)} == {
        "item-user-1",
        "item-answer-1",
    }
    context = TestRunContext.from_test_file(Path(__file__))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; "
                "from pathlib import Path; "
                "from tests.integration.backend.sessions.test_itemized_projection_contract import _project; "
                "from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver; "
                "from tests.support.session_catalog_owner import fixture_catalog_scope; "
                "\nwith fixture_catalog_scope():\n"
                " with RolloutCheckpointSaver(Path(sys.argv[1])) as saver:\n"
                "  print(json.dumps(_project(saver,sys.argv[2],sys.argv[3]),ensure_ascii=False))"
            ),
            str(saver._storage.storage.sessions_dir),
            session_id,
            snapshot.assembly_id,
        ],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    (context.artifacts_dir / f"{session_id}-restart.stderr.log").write_text(
        result.stderr
    )
    assert result.returncode == 0, result.stderr
    after = json.loads(result.stdout.strip().splitlines()[-1])
    (context.artifacts_dir / f"{session_id}-restart.json").write_text(
        json.dumps(after, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    assert before == after


def test_projection_evidence_is_shared_across_saver_projection_surfaces(
    projection_saver,
    projection_plan,
) -> None:
    saver, session_id, _ = projection_saver
    plan = projection_plan.as_sealed_plan()

    _, langchain_evidence = saver.project_context_plan_to_messages_with_evidence(
        session_id, saver._storage.storage.main_thread_id(session_id), plan
    )
    _, history_evidence = saver.project_context_plan_to_history_with_evidence(
        session_id, saver._storage.storage.main_thread_id(session_id), plan
    )
    native, native_evidence = saver.project_context_plan_to_native_with_evidence(
        session_id, saver._storage.storage.main_thread_id(session_id), plan
    )
    _, _, provider_evidence = saver.project_context_plan_to_provider_with_evidence(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan,
        target_format="chat_completions",
    )

    evidences = (
        langchain_evidence,
        history_evidence,
        native_evidence,
        provider_evidence,
    )
    assert {evidence.projection for evidence in evidences} == {
        "langchain",
        "web_history",
        "native",
        "provider",
    }
    assert len({evidence.selection_manifest_hash for evidence in evidences}) == 1
    assert len({evidence.plan_hash for evidence in evidences}) == 1
    assert len({evidence.source_overlay_epoch for evidence in evidences}) == 1
    assert len({evidence.history_view_revision for evidence in evidences}) == 1
    assert all(
        evidence.selection == langchain_evidence.selection for evidence in evidences
    )
    assert native["selection"] == list(langchain_evidence.selection)
    assert native["losses"] == list(native_evidence.losses)
    assert all(evidence.session_id == session_id for evidence in evidences)
    assert all(evidence.assembly_id == plan.assembly_id for evidence in evidences)


def test_unsealed_saver_plan_cannot_enter_any_projection(projection_saver) -> None:
    saver, session_id, _ = projection_saver
    draft = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id="draft-only",
        tool_set=EMPTY_TOOL_SET,
    )
    assert draft.plan_state == "unsealed" and draft.assembly_id is None
    assert draft.selection == ()
    for projector in (
        saver.project_context_plan_to_messages,
        saver.project_context_plan_to_history,
        saver.project_context_plan_to_native,
    ):
        with pytest.raises(ValueError, match="未 sealed"):
            projector(
                session_id, saver._storage.storage.main_thread_id(session_id), draft
            )


@pytest.mark.parametrize(
    "omission", ["canonical_item", "request_only", "tool_set", "all"]
)
def test_omission_keeps_selection_without_resolving_body_or_tools(
    projection_saver,
    projection_draft,
    omission: str,
) -> None:
    saver, session_id, session = projection_saver
    draft, bodies = projection_draft
    omitted = tuple(
        ref.ref_id
        for ref in (*draft.refs, *draft.tool_set_refs)
        if omission == "all" or ref.ref_type == omission
    )
    if omission in {"request_only", "all"}:
        # 普通 request-only 没有正文，只有 optional identity stub；不得读取或补空值。
        bodies = {}
        draft = replace(
            draft,
            refs=tuple(
                replace(
                    ref,
                    content_hash=None,
                    content_length=None,
                    availability="unavailable",
                )
                if ref.ref_id == "plain-ref"
                else ref
                for ref in draft.refs
            ),
        )
    draft = register_projection_draft(saver, session_id, draft)
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id="turn-1",
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="contract-provider",
        omitted_ref_ids=omitted,
        request_only_content=bodies,
    )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        result = _project(restarted, session_id, snapshot.assembly_id)
    omitted_entries = [entry for entry in snapshot.selection if not entry.included]
    assert len(omitted_entries) == len(omitted)
    assert all(
        entry.detail_ref is None and entry.contribution_ordinal is None
        for entry in omitted_entries
    )
    assert result["losses"] == result["history_losses"] == result["native"]["losses"]
    assert len(result["losses"]) == len(omitted)
    assert not set(omitted) & {
        source["ref_id"] for source in result["native"]["wire_sources"]
    }
    if omission in {"tool_set", "all"}:
        assert result["native"]["request"]["tools"] == []
    if omission in {"request_only", "all"}:
        assert not any(message["type"] == "system" for message in result["messages"])
        assert not list(
            (session / "rollout/context-plan-details" / snapshot.assembly_id).glob("*")
        )


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("context_plan_contributions", "source_revision", "wrong-source"),
        ("context_plan_contributions", "content_length", 1),
        ("context_plan_contributions", "content_hash", "sha256:jcs:v1:" + "0" * 64),
        ("context_plan_details", "source_revision", "wrong-source"),
        ("context_plan_details", "content_length", 1),
        ("context_plan_details", "detail_kind", "assembly_snapshot"),
        ("context_plan_details", "retention_class", "assembly_audit"),
        ("context_plan_details", "visibility", "private"),
        ("tool_set_snapshots", "source_revision", "wrong-source"),
        ("tool_set_snapshots", "content_length", 1),
        ("tool_set_snapshots", "content_hash", "sha256:jcs:v1:" + "0" * 64),
        ("tool_set_snapshots", "tool_policy_version", "wrong-policy"),
        ("tool_set_snapshots", "tools_json", "[]"),
        ("context_assembly_selections", "contribution_id", "wrong-contribution"),
        ("context_assembly_selections", "contribution_ordinal", 23),
        ("context_assembly_selections", "plan_ordinal", 23),
        ("context_assembly_selections", "selection_kind", "tool_set"),
    ],
)
def test_persisted_manifest_mismatch_rejects_every_projection(
    projection_saver,
    projection_plan,
    table: str,
    field: str,
    value: object,
) -> None:
    saver, session_id, session = projection_saver
    plan = projection_plan.as_sealed_plan()
    # 参数仅来自上面的固定测试矩阵；故障注入修改真实 seal 后的独立测试数据库。
    where = " WHERE plan_ordinal = 1" if table == "context_assembly_selections" else ""
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        if table == "context_plan_contributions":
            connection.execute(
                "UPDATE context_plan_contributions "
                "SET manifest_json = json_set(manifest_json, ?, ?) "
                "WHERE session_id = ? AND plan_id = ?",
                (f"$.{field}", value, session_id, plan.plan_id),
            )
        else:
            connection.execute(f"UPDATE {table} SET {field} = ?{where}", (value,))
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        for projector in (
            restarted.project_context_plan_to_messages,
            restarted.project_context_plan_to_native,
            restarted.project_context_plan_to_history,
        ):
            with pytest.raises(
                (RuntimeError, ValueError), match="不一致|mismatch|manifest"
            ):
                projector(session_id, plan.thread_id, plan)
        with pytest.raises(
            (RuntimeError, ValueError), match="不一致|mismatch|manifest"
        ):
            restarted.project_context_plan_to_provider(
                session_id, plan.thread_id, plan, target_format="chat_completions"
            )


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "unreadable",
        "no_body",
        "body",
        "hash",
        "revision",
        "length",
        "protection",
        "owner",
        "manifest",
    ],
)
def test_missing_or_changed_sealed_detail_cannot_fall_back_to_inline_body(
    projection_saver,
    projection_draft,
    damage: str,
) -> None:
    saver, session_id, session = projection_saver
    draft, bodies = projection_draft
    draft = replace(
        draft,
        refs=tuple(
            replace(ref, protection="protected") if ref.ref_id == "plain-ref" else ref
            for ref in draft.refs
        ),
    )
    draft = register_projection_draft(saver, session_id, draft)
    plan = saver.seal_context_plan(
        session_id,
        draft.thread_id,
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(session_id, turn_id="turn-1"),
        provider_version="contract-provider",
        request_only_content=bodies,
    ).as_sealed_plan()
    detail = plan.selection[2].detail_ref
    target = session / detail_relative_path(detail)
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        before_assemblies = connection.execute(
            "SELECT COUNT(*) FROM context_assemblies"
        ).fetchone()
        before_details = connection.execute(
            "SELECT detail_ref FROM context_plan_details ORDER BY detail_ref"
        ).fetchall()
    original_mode = target.stat().st_mode & 0o777
    if damage == "missing":
        target.unlink()
    elif damage == "unreadable":
        target.chmod(0)
    elif damage == "manifest":
        with sqlite3.connect(session / "rollout/index.sqlite") as connection:
            connection.execute(
                "UPDATE context_plan_details SET source_revision = ? WHERE detail_ref = ?",
                ("wrong-manifest-revision", detail_ref_key(detail)),
            )
    else:
        payload = json.loads(target.read_text(encoding="utf-8"))
        field, value = {
            "no_body": ("detail", None),
            "body": ("detail", "被覆盖的正文"),
            "hash": ("detail_content_hash", "sha256:jcs:v1:" + "0" * 64),
            "revision": ("source_revision", "different-revision"),
            "length": ("length", 1),
            "protection": ("protection", "public"),
            "owner": ("detail_ref", {**detail.to_dict(), "session_id": "other-owner"}),
        }[damage]
        payload[field] = value
        if damage == "protection":
            payload["sensitive"] = False
        target.write_bytes(canonical_json_bytes(payload))
    try:
        with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
            for projector in (
                restarted.project_context_plan_to_messages,
                restarted.project_context_plan_to_native,
            ):
                with pytest.raises((RuntimeError, ValueError), match="detail|详情"):
                    projector(session_id, plan.thread_id, plan)
            with pytest.raises((RuntimeError, ValueError), match="detail|详情"):
                restarted.project_context_plan_to_provider(
                    session_id, plan.thread_id, plan, target_format="chat_completions"
                )
            next_draft = replace(
                draft,
                plan_id=f"required-recovery-{damage}",
                contributions=(),
                tool_set_refs=(),
                refs=(
                    replace(
                        plan.selection[2].ref,
                        plan_id=f"required-recovery-{damage}",
                        ref_id="recover-required-plain",
                        source_ref=detail,
                    ),
                ),
            )
            next_draft = register_projection_draft(restarted, session_id, next_draft)
            with pytest.raises((RuntimeError, ValueError), match="detail|详情"):
                restarted.seal_context_plan(
                    session_id,
                    plan.thread_id,
                    next_draft,
                    wire_profile=None,
                    wire_params=None,
                    seal_idempotency_key=f"seal:{next_draft.plan_id}",
                    turn_id="turn-1",
                    execution_id=restarted.execution_for_turn(
                        session_id, turn_id="turn-1"
                    ),
                    provider_version="contract-provider",
                )
            assert (
                restarted._find_runtime_context_plan(
                    session_id, plan.thread_id, next_draft.plan_id, ""
                ).plan_state
                == "unsealed"
            )
            assert restarted._prepared_dispatches == {}
            with sqlite3.connect(session / "rollout/index.sqlite") as connection:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM context_assemblies"
                    ).fetchone()
                    == before_assemblies
                )
                assert (
                    connection.execute(
                        "SELECT detail_ref FROM context_plan_details ORDER BY detail_ref"
                    ).fetchall()
                    == before_details
                )
            # 正文损坏不影响只读 canonical history；registry manifest 损坏必须连 history 一起拒绝。
            if damage == "manifest":
                with pytest.raises((RuntimeError, ValueError), match="detail|详情"):
                    restarted.project_context_plan_to_history(
                        session_id, plan.thread_id, plan
                    )
            else:
                history = restarted.project_context_plan_to_history(
                    session_id, plan.thread_id, plan
                )
                assert [message.id for message in history] == ["answer-1", "user-1"]
    finally:
        if damage == "unreadable":
            target.chmod(original_mode)


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("damage", ["missing", "unreadable", "protection", "manifest"])
def test_required_detail_failure_blocks_prepared_dispatch(
    projection_saver,
    overlay_source,
    frozen_provider_wire,
    frozen_provider_activation,
    target_format: str,
    damage: str,
) -> None:
    saver, session_id, session = projection_saver
    thread = saver._storage.storage.main_thread_id(session_id)
    execution_id = saver.execution_for_turn(session_id, turn_id="turn-1")
    key = f"required-provider-{target_format}-{damage}:create"
    arguments = {
        "turn_id": "turn-1",
        "execution_id": execution_id,
        "plan_creation_idempotency_key": key,
        "seal_idempotency_key": f"seal:{key}",
        "tool_set_supplier": lambda: EMPTY_TOOL_SET,
        **frozen_provider_wire("contract-provider", target_format),
        **frozen_provider_activation(saver, plan_creation_idempotency_key=key),
    }
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session_id, thread),
        control_state_port=saver,
    )
    prepared = saver.prepare_context_for_provider(
        session_id,
        thread,
        context_source_manager=manager,
        **arguments,
    )
    snapshot = prepared["snapshot"]
    assert snapshot.seal_binding is not None
    entry = next(
        entry for entry in snapshot.selection if entry.ref.ref_type == "request_only"
    )
    target = session / detail_relative_path(entry.detail_ref)
    original_mode = target.stat().st_mode & 0o777
    before_jsonl = saver._storage.storage.jsonl_path(session_id).read_bytes()
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        before_assemblies = connection.execute(
            "SELECT COUNT(*) FROM context_assemblies"
        ).fetchone()
        before_details = connection.execute(
            "SELECT detail_ref FROM context_plan_details ORDER BY detail_ref"
        ).fetchall()
    saver.discard_prepared_context_for_dispatch(
        session_id,
        thread,
        execution_id=execution_id,
        assembly_id=snapshot.assembly_id,
    )
    assert saver._prepared_dispatches == {}
    if damage == "missing":
        target.unlink()
    elif damage == "unreadable":
        target.chmod(0)
    elif damage == "manifest":
        with sqlite3.connect(session / "rollout/index.sqlite") as connection:
            connection.execute(
                "UPDATE context_plan_details SET source_revision = ? WHERE detail_ref = ?",
                ("wrong-manifest-revision", detail_ref_key(entry.detail_ref)),
            )
    else:
        payload = json.loads(target.read_bytes())
        payload.update(protection="protected", sensitive=True)
        target.write_bytes(canonical_json_bytes(payload))
    try:
        with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
            manager = ContextSourceManager(
                owner=ContextSourceOwnerKey(session_id, thread),
                control_state_port=restarted,
            )
            # 复用真实 wire/epoch/activation binding，拒绝必须发生在 prepare 内而非 fixture 求值。
            with pytest.raises((RuntimeError, ValueError), match="detail|详情"):
                restarted.prepare_context_for_provider(
                    session_id,
                    thread,
                    context_source_manager=manager,
                    **arguments,
                )
            assert restarted._prepared_dispatches == {}
            assert (
                restarted.consume_prepared_context_for_dispatch(
                    session_id,
                    thread,
                    execution_id=execution_id,
                )
                is None
            )
            assert (
                restarted._storage.storage.jsonl_path(session_id).read_bytes()
                == before_jsonl
            )
            with sqlite3.connect(session / "rollout/index.sqlite") as connection:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM context_assemblies"
                    ).fetchone()
                    == before_assemblies
                )
                assert (
                    connection.execute(
                        "SELECT detail_ref FROM context_plan_details ORDER BY detail_ref"
                    ).fetchall()
                    == before_details
                )
    finally:
        if damage == "unreadable":
            target.chmod(original_mode)


@pytest.mark.parametrize(
    "component", ["thread_root", "rollout", "details", "assembly", "leaf"]
)
def test_detail_read_rejects_each_symlink_component(
    projection_saver,
    projection_plan,
    component: str,
) -> None:
    saver, session_id, session = projection_saver
    entry = projection_plan.selection[2]
    target = session / detail_relative_path(entry.detail_ref)
    node = {
        "thread_root": session,
        "rollout": session / "rollout",
        "details": session / "rollout/context-plan-details",
        "assembly": target.parent,
        "leaf": target,
    }[component]
    parked = node.with_name(f"{node.name}.contract-original")
    outside = parked / target.relative_to(node)
    node.rename(parked)
    before = outside.read_bytes()
    node.symlink_to(parked, target_is_directory=parked.is_dir())
    try:
        with pytest.raises((RuntimeError, ValueError)):
            saver.read_context_plan_detail(
                session_id,
                projection_plan.thread_id,
                detail_ref=entry.detail_ref,
            )
        assert outside.read_bytes() == before
    finally:
        node.unlink()
        parked.rename(node)


def test_detail_realpath_containment_rejects_resolved_escape(
    projection_saver,
    projection_plan,
    projection_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, _, session = projection_saver
    detail = projection_plan.selection[2].detail_ref
    target = session / detail_relative_path(detail)
    outside = projection_workspace / "containment-outside.json"
    outside.write_bytes(b"outside-owner-body")
    original_resolve = Path.resolve

    # 独立故障注入 realpath 结果；真实 symlink 的逐级拒绝由上面的 OS 文件测试覆盖。
    def resolve(path: Path, *args, **kwargs) -> Path:
        return outside if path == target else original_resolve(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "resolve", resolve)
            with pytest.raises(RuntimeError, match="越出 session node"):
                saver._detail_store._files.path(detail)
        assert outside.read_bytes() == b"outside-owner-body"
    finally:
        outside.unlink()


@pytest.mark.parametrize("included", [False, True])
@pytest.mark.parametrize(
    ("tag", "wrong_ref_type"),
    [
        (tag, ref_type)
        for tag, allowed in (
            ("canonical_history", "canonical_item"),
            ("request_only", "request_only"),
            ("overlay_base", "request_only"),
            ("overlay_delta", "request_only"),
            ("tool_set", "tool_set"),
        )
        for ref_type in ("canonical_item", "request_only", "tool_set")
        if ref_type != allowed
    ],
)
def test_invalid_selection_union_is_rejected_in_domain_and_restore(
    projection_saver,
    projection_draft,
    overlay_source,
    tag: str,
    wrong_ref_type: str,
    included: bool,
) -> None:
    saver, session_id, session = projection_saver
    thread = saver._storage.storage.main_thread_id(session_id)
    draft = saver.compose_committed_context_plan(
        session_id,
        thread,
        plan_id=f"union-{tag}-{wrong_ref_type}-{included}",
        tool_set=EMPTY_TOOL_SET,
    )
    role = {"overlay_base": "base", "overlay_delta": "delta"}.get(tag, "none")
    target = (
        draft.tool_set_refs[0]
        if tag == "tool_set"
        else next(
            ref
            for ref in draft.refs
            if ref.ref_type
            == ("canonical_item" if tag == "canonical_history" else "request_only")
            and ref.base_delta_role == role
        )
    )
    omitted = (
        ()
        if included
        else tuple(
            ref.ref_id for ref in draft.refs if ref.base_delta_role in {"base", "delta"}
        )
        if tag == "overlay_base"
        else (target.ref_id,)
    )
    draft = register_projection_draft(saver, session_id, draft)
    snapshot = saver.seal_context_plan(
        session_id,
        thread,
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(session_id, turn_id="turn-1"),
        provider_version="contract-provider",
        omitted_ref_ids=omitted,
    )
    entry = next(
        entry for entry in snapshot.selection if entry.ref.ref_id == target.ref_id
    )
    assert entry.included == included and entry.selection_kind == tag
    assert replace(entry) == entry
    assert _project(saver, session_id, snapshot.assembly_id)["selection"] == [
        item.to_dict() for item in snapshot.selection
    ]
    saver.project_context_plan_to_provider(
        session_id, thread, snapshot.as_sealed_plan(), target_format="chat_completions"
    )
    wrong_ref = next(
        entry.ref
        for entry in snapshot.selection
        if entry.ref.ref_type == wrong_ref_type
    )
    # 只替换 union ref，保留真实 seal 分配的必填槽；正对照已经成功构造和投影。
    with pytest.raises(
        ItemSchemaError, match="selection/ref_type/base_delta_role 不兼容"
    ):
        replace(entry, ref=wrong_ref)
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        cursor = connection.execute(
            "UPDATE context_assembly_selections SET ref_type = ? "
            "WHERE assembly_id = ? AND plan_ordinal = ?",
            (wrong_ref_type, snapshot.assembly_id, entry.plan_ordinal),
        )
        assert cursor.rowcount == 1
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        for projector in (
            restarted.project_context_plan_to_messages,
            restarted.project_context_plan_to_native,
            restarted.project_context_plan_to_history,
        ):
            with pytest.raises((RuntimeError, ValueError), match="selection manifest"):
                projector(
                    session_id,
                    thread,
                    snapshot.as_sealed_plan(),
                )
        with pytest.raises((RuntimeError, ValueError), match="selection manifest"):
            restarted.project_context_plan_to_provider(
                session_id,
                thread,
                snapshot.as_sealed_plan(),
                target_format="chat_completions",
            )


@pytest.mark.parametrize("included", [False, True])
@pytest.mark.parametrize("tag", ["canonical_history", "tool_set"])
@pytest.mark.parametrize(
    "field", ["detail_ref", "contribution_id", "contribution_ordinal"]
)
def test_canonical_and_tool_selection_forbid_detail_and_contribution_slots(
    projection_saver,
    projection_draft,
    tag: str,
    field: str,
    included: bool,
) -> None:
    saver, session_id, session = projection_saver
    thread = saver._storage.storage.main_thread_id(session_id)
    draft = saver.compose_committed_context_plan(
        session_id,
        thread,
        plan_id=f"forbidden-{tag}-{field}-{included}",
        tool_set=EMPTY_TOOL_SET,
    )
    target = (
        draft.tool_set_refs[0]
        if tag == "tool_set"
        else next(ref for ref in draft.refs if ref.ref_type == "canonical_item")
    )
    draft = register_projection_draft(saver, session_id, draft)
    snapshot = saver.seal_context_plan(
        session_id,
        thread,
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(session_id, turn_id="turn-1"),
        provider_version="contract-provider",
        omitted_ref_ids=() if included else (target.ref_id,),
    )
    entry = next(
        entry for entry in snapshot.selection if entry.ref.ref_id == target.ref_id
    )
    assert entry.included == included and replace(entry) == entry
    assert _project(saver, session_id, snapshot.assembly_id)["selection"] == [
        item.to_dict() for item in snapshot.selection
    ]
    saver.project_context_plan_to_provider(
        session_id, thread, snapshot.as_sealed_plan(), target_format="chat_completions"
    )
    values = {
        "detail_ref": next(
            entry.detail_ref
            for entry in snapshot.selection
            if entry.detail_ref is not None
        ),
        "contribution_id": "forbidden-contribution",
        "contribution_ordinal": 0,
    }
    with pytest.raises(ItemSchemaError):
        replace(entry, **{field: values[field]})
    value = detail_ref_key(values[field]) if field == "detail_ref" else values[field]
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        connection.execute(
            f"UPDATE context_assembly_selections SET {field} = ? "
            "WHERE assembly_id = ? AND plan_ordinal = ?",
            (value, snapshot.assembly_id, entry.plan_ordinal),
        )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        for projector in (
            restarted.project_context_plan_to_messages,
            restarted.project_context_plan_to_native,
            restarted.project_context_plan_to_history,
        ):
            with pytest.raises((RuntimeError, ValueError), match="selection manifest"):
                projector(
                    session_id,
                    thread,
                    snapshot.as_sealed_plan(),
                )
        with pytest.raises((RuntimeError, ValueError), match="selection manifest"):
            restarted.project_context_plan_to_provider(
                session_id,
                thread,
                snapshot.as_sealed_plan(),
                target_format="chat_completions",
            )


@pytest.mark.parametrize("field", ["source_revision", "content_length", "content_hash"])
def test_canonical_source_mismatch_is_rejected_before_seal(
    projection_saver, field: str
) -> None:
    saver, session_id, _ = projection_saver
    draft = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id="source-mismatch",
        tool_set=EMPTY_TOOL_SET,
    )
    value = 1 if field == "content_length" else "sha256:jcs:v1:" + "0" * 64
    draft = replace(
        draft, refs=(replace(draft.refs[0], **{field: value}), *draft.refs[1:])
    )
    with pytest.raises((RuntimeError, ValueError), match="不一致|mismatch"):
        draft = register_projection_draft(saver, session_id, draft)
        saver.seal_context_plan(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            draft,
            wire_profile=None,
            wire_params=None,
            seal_idempotency_key=f"seal:{draft.plan_id}",
            turn_id="turn-1",
            execution_id=saver.execution_for_turn(
                session_id,
                turn_id="turn-1",
                thread_id=saver._storage.storage.main_thread_id(session_id),
            ),
            provider_version="contract-provider",
        )
    assert (
        saver.list_context_assemblies(
            session_id, saver._storage.storage.main_thread_id(session_id)
        )
        == ()
    )


def test_overlay_reuses_source_after_history_rewind_and_restart(
    projection_saver,
    overlay_source,
    frozen_provider_wire,
    frozen_provider_activation,
) -> None:
    saver, session_id, _ = projection_saver
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "cp-tail"
    checkpoint["channel_values"] = {
        "messages": [AIMessage(content="将隐藏的历史尾部", id="tail")]
    }
    checkpoint["channel_versions"] = {"messages": "2"}
    saver.put(
        build_checkpoint_config(saver._storage.storage.main_thread_id(session_id)),
        checkpoint,
        {"source": "projection-contract"},
        {"messages": "2"},
    )
    thread = saver._storage.storage.main_thread_id(session_id)
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session_id, thread),
        control_state_port=saver,
    )
    first = saver.prepare_context_for_provider(
        session_id,
        thread,
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id="turn-1",
            thread_id=thread,
        ),
        plan_creation_idempotency_key="overlay-before:create",
        seal_idempotency_key="overlay-before:seal",
        context_source_manager=manager,
        tool_set_supplier=lambda: EMPTY_TOOL_SET,
        **frozen_provider_wire("contract-provider", "responses"),
        **frozen_provider_activation(
            saver,
            plan_creation_idempotency_key="overlay-before:create",
        ),
    )["snapshot"]
    first_result = _project(saver, session_id, first.assembly_id)
    first_overlay = [
        entry for entry in first.selection if entry.base_delta_role != "none"
    ]
    assert [entry.ref.ref_id for entry in first_overlay] == ["z-base", "a-delta"]
    assert [entry.contribution_ordinal for entry in first_overlay] == [0, 1]
    saver.rewind(
        build_checkpoint_config(saver._storage.storage.main_thread_id(session_id)),
        checkpoint_id="cp-initial",
    )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        draft = restarted.compose_committed_context_plan(
            session_id,
            restarted._storage.storage.main_thread_id(session_id),
            plan_id="overlay-after",
            tool_set=EMPTY_TOOL_SET,
        )
        assert draft.history_view_revision > first.history_view_revision
        assert draft.source_overlay_epoch == first.source_overlay_epoch == 0
        thread = restarted._storage.storage.main_thread_id(session_id)
        manager = ContextSourceManager(
            owner=ContextSourceOwnerKey(session_id, thread),
            control_state_port=restarted,
        )
        prepared = restarted.prepare_context_for_provider(
            session_id,
            thread,
            turn_id="turn-1",
            execution_id=restarted.execution_for_turn(
                session_id,
                turn_id="turn-1",
                thread_id=thread,
            ),
            plan_creation_idempotency_key="overlay-after:create",
            seal_idempotency_key="overlay-after:seal",
            context_source_manager=manager,
            tool_set_supplier=lambda: EMPTY_TOOL_SET,
            **frozen_provider_wire("contract-provider", "responses"),
            **frozen_provider_activation(
                restarted,
                plan_creation_idempotency_key="overlay-after:create",
            ),
        )
        second = prepared["snapshot"]
        second_result = _project(restarted, session_id, second.assembly_id)
        assert _project(restarted, session_id, first.assembly_id) == first_result
    second_overlay = [
        entry for entry in second.selection if entry.base_delta_role != "none"
    ]
    for old, new in zip(first_overlay, second_overlay, strict=True):
        assert old.assembly_id != new.assembly_id and old.detail_ref != new.detail_ref
        for field in (
            "source_revision",
            "content_hash",
            "content_length",
            "base_delta_role",
            "source_overlay_epoch",
            "contribution_id",
            "contribution_ordinal",
            "overlay_from_revision",
            "overlay_to_revision",
            "overlay_diff_hash",
        ):
            assert getattr(old, field) == getattr(new, field)
    assert "tail" in [message["id"] for message in first_result["history"]]
    assert "tail" not in [message["id"] for message in second_result["history"]]
    assert [
        message["content"]
        for message in first_result["messages"]
        if message["type"] == "system"
    ] == [
        message["content"]
        for message in second_result["messages"]
        if message["type"] == "system"
    ]
    assert (
        first_result["native"]["request"]["input"][-2:]
        == second_result["native"]["request"]["input"][-2:]
    )


def test_gc_tombstone_survives_file_delete_failure_and_restart(
    projection_saver, monkeypatch
) -> None:
    saver, session_id, session = projection_saver
    expired = datetime.now(UTC) + timedelta(days=1)
    # 真实 detail writer/registry 构造 seal 前已落盘但尚未被 assembly 引用的孤立详情。
    record = saver._detail_store.write(
        session_id=session_id,
        thread_id=saver._storage.storage.main_thread_id(session_id),
        assembly_id="orphan-assembly",
        detail_kind="assembly_snapshot",
        retention_class="assembly_audit",
        visibility="internal",
        detail={"source": "orphan"},
        required=False,
        sensitive=False,
        source_revision="orphan-v1",
        retention_days=0,
    )
    saver._storage.assemblies.register_context_plan_detail(record)
    target = session / record.relative_path
    original_unlink = Path.unlink

    def fail_delete(path: Path, *args, **kwargs):
        if path == target:
            with sqlite3.connect(session / "rollout/index.sqlite") as connection:
                row = connection.execute(
                    "SELECT status, availability FROM context_plan_details WHERE detail_ref = ?",
                    (detail_ref_key(record.detail_ref),),
                ).fetchone()
            assert row == ("unavailable", "unavailable")
            raise OSError("injected detail deletion failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_delete)
        with pytest.raises(OSError, match="injected detail deletion failure"):
            saver.gc_context_plan_details(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                expired_before=expired,
            )
    assert target.is_file()
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        assert restarted.gc_context_plan_details(
            session_id,
            restarted._storage.storage.main_thread_id(session_id),
            expired_before=expired,
        ) == (record.detail_ref,)
        assert not target.exists()


def test_protected_reasoning_reports_loss_in_all_projections(projection_saver) -> None:
    saver, session_id, _ = projection_saver
    opaque = CanonicalItemRecord.create(
        item_id="opaque-reasoning",
        item_sequence=3,
        semantic_kind="reasoning",
        payload_kind="opaque",
        status="completed",
        producer_ref={"producer_kind": "provider", "producer_id": "contract-provider"},
        payload={
            "encoding": "base64",
            "wire_type": "encrypted_reasoning",
            "schema_version": "v1",
            "value": "c2VjcmV0",
            "protection": {"encrypted": True},
        },
        turn_id="turn-1",
        turn_scope="turn_member",
    )
    saver.append_items(
        session_id,
        (opaque,),
        thread_id=saver._storage.storage.main_thread_id(session_id),
    )
    draft = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id="opaque-plan",
        tool_set=EMPTY_TOOL_SET,
    )
    assert "opaque-reasoning" in {ref.ref_id for ref in draft.refs}
    draft = register_projection_draft(saver, session_id, draft)
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id="turn-1",
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="contract-provider",
    )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        result = _project(restarted, session_id, snapshot.assembly_id)
    assert (
        result["losses"]
        == result["history_losses"]
        == result["native"]["losses"]
        == [
            "opaque-reasoning:reasoning/opaque",
        ]
    )
    for body in (result["messages"], result["history"], result["native"]["request"]):
        assert "c2VjcmV0" not in json.dumps(body)


@pytest.mark.parametrize("omission", ["z-base", "a-delta", "both"])
def test_overlay_omission_preserves_role_without_allocating_contribution(
    projection_saver,
    overlay_source,
    omission: str,
) -> None:
    saver, session_id, _ = projection_saver
    draft = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id=f"omit-overlay-{omission}",
        tool_set=EMPTY_TOOL_SET,
    )
    omitted = ("z-base", "a-delta") if omission == "both" else (omission,)
    draft = register_projection_draft(saver, session_id, draft)
    # 若 base 被省略，delta 必须同时省略；独立 delta 不能伪装为可应用的链。
    if omission == "z-base":
        with pytest.raises((ValueError, RuntimeError), match="overlay|base|chain|基线"):
            saver.seal_context_plan(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                draft,
                wire_profile=None,
                wire_params=None,
                seal_idempotency_key=f"seal:{draft.plan_id}",
                turn_id="turn-1",
                execution_id=saver.execution_for_turn(
                    session_id,
                    turn_id="turn-1",
                    thread_id=saver._storage.storage.main_thread_id(session_id),
                ),
                provider_version="contract-provider",
                omitted_ref_ids=omitted,
            )
        return
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id="turn-1",
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="contract-provider",
        omitted_ref_ids=omitted,
    )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        result = _project(restarted, session_id, snapshot.assembly_id)
    for entry in snapshot.selection:
        if entry.ref.ref_id in omitted:
            assert not entry.included and entry.omission_reason
            assert entry.contribution_id is None and entry.contribution_ordinal is None
            assert entry.detail_ref is None
            assert entry.base_delta_role in {"base", "delta"}
    assert result["losses"] == result["history_losses"] == result["native"]["losses"]
    assert not set(omitted) & {
        source["ref_id"] for source in result["native"]["wire_sources"]
    }


@pytest.mark.parametrize("backed", [False, True])
def test_sensitive_request_exact_detail_survives_restart(
    projection_saver,
    backed: bool,
) -> None:
    saver, session_id, session = projection_saver
    thread = saver._storage.storage.main_thread_id(session_id)
    body = [{"type": "text", "text": "protected-source-only-42"}]
    length = len(canonical_json_bytes(body))
    if backed:
        saver.register_context_contribution(
            session_id,
            thread,
            ContextContribution(
                contribution_id="protected-contribution",
                source_kind="protected_source",
                source_revision="protected-v1",
                content_hash=contribution_content_hash("prompt", body),
                content_length=length,
                protection="protected",
                extensions={},
                source_binding=SourceBinding(source_ref="source:protected"),
            ),
            request_content=body,
        )
    draft = saver.compose_committed_context_plan(
        session_id,
        thread,
        plan_id="protected-plan",
        tool_set=EMPTY_TOOL_SET,
    )
    if not backed:
        ref = ContextRef.request_only_ref(
            "protected-plan-ref",
            session_id=session_id,
            thread_id=thread,
            plan_id=draft.plan_id,
            source_revision="protected-v1",
            content_length=length,
            content_hash_value=sha256_jcs(body),
            protection="protected",
            payload_kind="structured_content",
            source_ref="source:protected",
        )
        draft = replace(draft, refs=(*draft.refs, ref))
    draft = register_projection_draft(saver, session_id, draft)
    snapshot = saver.seal_context_plan(
        session_id,
        thread,
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id="turn-1",
            thread_id=thread,
        ),
        provider_version="contract-provider",
        request_only_content={"protected-plan-ref": body} if not backed else None,
    )
    snapshot.validate_hashes()
    entry = next(
        entry for entry in snapshot.selection if entry.ref.ref_type == "request_only"
    )
    assert entry.content_hash == (
        contribution_content_hash("prompt", body) if backed else sha256_jcs(body)
    )
    assert entry.content_length == length
    assert entry.protection == "protected"
    target = session / detail_relative_path(entry.detail_ref)
    exact = json.loads(target.read_bytes())
    assert exact["format_version"] == 3
    assert exact["detail"] == body
    assert exact["detail_content_hash"] == sha256_jcs(body)
    assert exact["detail_ref"] == entry.detail_ref.to_dict()
    assert tuple(target.parent.iterdir()) == (target,)
    assert not any(
        path.name
        in {
            ".context-detail-key",
            ".context-redaction-key",
            "context-plan-details-protected",
        }
        for path in session.rglob("*")
    )
    before = _project(saver, session_id, snapshot.assembly_id)
    assert "protected-source-only-42" in json.dumps(before["native"]["request"])
    assert "protected-source-only-42" not in json.dumps(before["history"])
    assert len(before["history"]) == 2
    assert (
        b"protected-source-only-42"
        not in (session / "rollout/index.sqlite").read_bytes()
    )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        assert _project(restarted, session_id, snapshot.assembly_id) == before
        with pytest.raises(PermissionError, match="未授权读取"):
            restarted.read_context_plan_detail(
                session_id, thread, detail_ref=entry.detail_ref
            )
        assert (
            restarted.read_context_plan_detail(
                session_id,
                thread,
                detail_ref=entry.detail_ref,
                include_sensitive=True,
            )["detail"]
            == body
        )


@pytest.mark.parametrize("damage", ["content_hash", "content_length"])
def test_invalid_request_body_does_not_leave_partial_seal_details(
    projection_saver,
    projection_draft,
    damage: str,
) -> None:
    saver, session_id, session = projection_saver
    draft, bodies = projection_draft
    body = bodies["plain-ref"]
    body[0]["text"] = (
        "x" if damage == "content_length" else body[0]["text"].replace("普通", "错误")
    )
    draft = register_projection_draft(saver, session_id, draft)
    with pytest.raises(ValueError, match=f"source-mismatch: request-only {damage}"):
        saver.seal_context_plan(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            draft,
            wire_profile=None,
            wire_params=None,
            seal_idempotency_key=f"seal:{draft.plan_id}",
            turn_id="turn-1",
            execution_id=saver.execution_for_turn(
                session_id,
                turn_id="turn-1",
                thread_id=saver._storage.storage.main_thread_id(session_id),
            ),
            provider_version="contract-provider",
            request_only_content=bodies,
        )
    assert (
        saver.list_context_assemblies(
            session_id, saver._storage.storage.main_thread_id(session_id)
        )
        == ()
    )
    assert not any(
        path.is_file() for path in (session / "rollout/context-plan-details").rglob("*")
    )


def test_native_tool_calls_and_results_keep_saver_selection_order(
    projection_saver,
) -> None:
    saver, session_id, _ = projection_saver
    args = {"path": "目录/README.md"}
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "cp-tool-result"
    checkpoint["channel_values"] = {
        "messages": [
            AIMessage(
                content="",
                id="assistant-call",
                tool_calls=[
                    {
                        "id": "call-read",
                        "name": "read_file",
                        "args": args,
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="读取成功",
                tool_call_id="call-read",
                id="tool-result",
                name="read_file",
            ),
        ]
    }
    checkpoint["channel_versions"] = {"messages": "2"}
    saver.put(
        build_checkpoint_config(saver._storage.storage.main_thread_id(session_id)),
        checkpoint,
        {"source": "projection-contract"},
        {"messages": "2"},
    )
    draft = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id="native-tool-plan",
        tool_set=EMPTY_TOOL_SET,
    )
    draft = register_projection_draft(saver, session_id, draft)
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id="turn-1",
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="contract-provider",
    )
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        result = _project(restarted, session_id, snapshot.assembly_id)
    inputs = result["native"]["request"]["input"]
    call, output = inputs[-2:]
    assert call["type"] == "function_call" and json.loads(call["arguments"]) == args
    assert output == {
        "type": "function_call_output",
        "call_id": "call-read",
        "output": "读取成功",
    }
    assert call["call_id"] == output["call_id"]
    assert result["messages"][-2]["tool_calls"][0]["id"] == call["call_id"]
    assert result["messages"][-1]["tool_call_id"] == output["call_id"]
    assert result["history"] == result["messages"]
    assert [source["plan_ordinal"] for source in result["native"]["wire_sources"]] == [
        entry.plan_ordinal
        for entry in snapshot.selection
        if entry.included and entry.ref.ref_type == "canonical_item"
    ]
    assert len(snapshot.tool_set_refs) == 1 and snapshot.tool_set_refs[0].tools == ()


def test_toolset_manifest_keeps_one_plan_across_wire_formats(projection_saver) -> None:
    saver, session_id, _ = projection_saver
    function = {
        "name": "read_file",
        "description": "读取文件",
        "parameters": {"type": "object", "properties": {}},
        "strict": False,
    }
    draft = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id="toolset-wire-formats",
        tool_set=ToolSetSnapshot(
            selection_revision=sha256_jcs({"read_file": True}),
            tools=(
                {
                    "tool_id": "stable-tool-id",
                    "type": "function",
                    "function": function,
                },
            ),
        ),
    )
    draft = register_projection_draft(saver, session_id, draft)
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        draft,
        wire_profile=None,
        wire_params=None,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id="turn-1",
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="contract-provider",
    )
    plan = snapshot.as_sealed_plan()
    chat_messages, chat_tools, chat_loss = saver.project_context_plan_to_provider(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan,
        target_format="chat_completions",
    )
    response_messages, response_tools, response_loss = (
        saver.project_context_plan_to_provider(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            plan,
            target_format="responses",
        )
    )
    assert chat_tools == [{"type": "function", "function": function}]
    assert response_tools == [{"type": "function", **function}]
    assert chat_messages == response_messages and chat_loss == response_loss == ()
    native = saver.project_context_plan_to_native(
        session_id, saver._storage.storage.main_thread_id(session_id), plan
    )
    assert native["request"]["tools"] == response_tools
    assert native["plan_hash"] == snapshot.plan_hash
