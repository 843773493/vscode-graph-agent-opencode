"""seal/dispatch 最小验证合同：每个闭合错误码至少一条冲突与一条放行路径。"""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    BaseDeltaRole,
    CanonicalItemStatus,
    PayloadKind,
    SelectionKind,
    SemanticKind,
)
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextContribution
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.rollout_context.assembly.seal_preflight import (
    ContextAssemblySealPreflightError,
    canonical_tool_pairings,
    validate_seal_dispatch_invariants,
)


def _canonical_ref(
    ref_id: str,
    semantic_kind: str = SemanticKind.ASSISTANT_OUTPUT.value,
    payload_kind: str = PayloadKind.STRUCTURED_CONTENT.value,
) -> ContextRef:
    return ContextRef(
        ref_type="canonical_item",
        ref_id=ref_id,
        semantic_kind=semantic_kind,
        payload_kind=payload_kind,
        status=CanonicalItemStatus.COMPLETED.value,
        source_revision=f"rev-{ref_id}",
        content_hash=sha256_jcs({"canonical": ref_id}),
        content_length=1,
        session_id="s1",
    )


def _request_ref(
    ref_id: str,
    *,
    base_delta_role: str = BaseDeltaRole.NONE.value,
    source_overlay_epoch: int | None = None,
    overlay_from_revision: str | None = None,
    overlay_to_revision: str | None = None,
    overlay_diff_hash: str | None = None,
    content_hash_override: str | None = None,
    content_length_override: int | None = None,
) -> ContextRef:
    return ContextRef(
        ref_type="request_only",
        ref_id=ref_id,
        source_revision=f"rev-{ref_id}",
        content_hash=(
            content_hash_override
            if content_hash_override is not None
            else contribution_content_hash("prompt", {"text": ref_id})
        ),
        content_length=(
            content_length_override
            if content_length_override is not None
            else len(canonical_json_bytes({"text": ref_id}))
        ),
        source_ref=f"skill-source:{ref_id}",
        base_delta_role=base_delta_role,
        source_overlay_epoch=source_overlay_epoch,
        overlay_from_revision=overlay_from_revision,
        overlay_to_revision=overlay_to_revision,
        overlay_diff_hash=overlay_diff_hash,
        session_id="s1",
        plan_id="p1",
    )


def _contribution(
    contribution_id: str,
    kind: str = "prompt",
    metadata: dict[str, object] | None = None,
) -> ContextContribution:
    body = {"text": contribution_id}
    return ContextContribution(
        contribution_id=contribution_id,
        source_kind="skill",
        source_revision=f"rev-{contribution_id}",
        contribution_kind=kind,
        body=body,
        content_hash=contribution_content_hash(kind, body),
        metadata=metadata or {},
    )


def _entry(
    plan_ordinal: int,
    ref: ContextRef,
    *,
    selection_kind: str,
    contribution_id: str | None = None,
    base_delta_role: str = BaseDeltaRole.NONE.value,
    source_overlay_epoch: int | None = None,
    overlay_from_revision: str | None = None,
    overlay_to_revision: str | None = None,
    overlay_diff_hash: str | None = None,
    source_revision: str | None = None,
    contribution_ordinal: int | None = None,
) -> ContextSelectionEntry:
    return ContextSelectionEntry(
        assembly_id="assembly-1",
        plan_ordinal=plan_ordinal,
        ref=ref,
        selection_kind=selection_kind,
        source_revision=ref.source_revision
        if source_revision is None
        else source_revision,
        contribution_id=contribution_id,
        base_delta_role=base_delta_role,
        source_overlay_epoch=source_overlay_epoch,
        overlay_from_revision=overlay_from_revision,
        overlay_to_revision=overlay_to_revision,
        overlay_diff_hash=(
            overlay_diff_hash
            if overlay_diff_hash is not None
            else ref.overlay_diff_hash
        ),
        content_length=ref.content_length,
        content_hash=ref.content_hash,
        detail_ref=(
            DetailRef(session_id="s1", assembly_id="assembly-1", detail_id=ref.ref_id)
            if ref.ref_type == "request_only"
            else None
        ),
        contribution_ordinal=contribution_ordinal,
    )


def _snapshot(
    *,
    selection: tuple[ContextSelectionEntry, ...],
    refs: tuple[ContextRef, ...],
    contributions: tuple[ContextContribution, ...] = (),
) -> ContextAssemblySnapshot:
    contributions = tuple(
        replace(contribution, assembly_id="assembly-1", contribution_ordinal=index)
        for index, contribution in enumerate(contributions)
    )
    return ContextAssemblySnapshot(
        assembly_id="assembly-1",
        session_id="s1",
        turn_id="t1",
        execution_id="e1",
        plan_id="p1",
        plan_hash="plan-hash",
        request_hash="request-hash",
        history_view_revision=0,
        source_overlay_epoch=0,
        refs=refs,
        contributions=contributions,
        tool_snapshot=(),
        compiler_version="test",
        provider_version="test",
        target_format="responses",
        selection=selection,
        sealed=True,
    )


def _minimal_snapshot() -> ContextAssemblySnapshot:
    assistant = _canonical_ref("item-assistant")
    source = _request_ref("skill-a")
    return _snapshot(
        selection=(
            _entry(0, assistant, selection_kind=SelectionKind.CANONICAL_HISTORY.value),
            _entry(
                1,
                source,
                selection_kind=SelectionKind.REQUEST_ONLY.value,
                contribution_id="skill-a",
                contribution_ordinal=0,
            ),
        ),
        refs=(assistant, source),
        contributions=(_contribution("skill-a"),),
    )


def _with_selection(
    snapshot: ContextAssemblySnapshot,
    selection: tuple[ContextSelectionEntry, ...],
) -> ContextAssemblySnapshot:
    return _snapshot(
        selection=selection,
        refs=snapshot.refs,
        contributions=snapshot.contributions,
    )


def test_minimal_assembly_passes_without_tool_pairings() -> None:
    validate_seal_dispatch_invariants(_minimal_snapshot())


def _mutate_entry(
    entry: ContextSelectionEntry, **changes: object
) -> ContextSelectionEntry:
    clone = object.__new__(ContextSelectionEntry)
    for item in fields(ContextSelectionEntry):
        object.__setattr__(
            clone, item.name, changes.get(item.name, getattr(entry, item.name))
        )
    return clone


def _clone_with(
    snapshot: ContextAssemblySnapshot, **changes: object
) -> ContextAssemblySnapshot:
    # 恢复/外部位图路径可能给出绕过 domain 构造器的快照；seal preflight
    # 用同一不变量兜底，这里用旁路构造模拟该输入。
    clone = object.__new__(ContextAssemblySnapshot)
    for item in fields(ContextAssemblySnapshot):
        object.__setattr__(
            clone, item.name, changes.get(item.name, getattr(snapshot, item.name))
        )
    return clone


def test_non_contiguous_plan_ordinal_fails_closed() -> None:
    snapshot = _minimal_snapshot()
    shifted = _entry(
        2,
        snapshot.selection[1].ref,
        selection_kind=snapshot.selection[1].selection_kind,
        contribution_id=snapshot.selection[1].contribution_id,
        contribution_ordinal=0,
    )
    with pytest.raises(ContextAssemblySealPreflightError) as error:
        validate_seal_dispatch_invariants(
            _clone_with(
                snapshot,
                selection=(snapshot.selection[0], shifted),
            )
        )
    assert error.value.code == "seal-dispatch-plan-ordinal-conflict"


def test_duplicate_included_identity_fails_closed() -> None:
    snapshot = _minimal_snapshot()
    duplicated = _entry(
        2,
        snapshot.selection[1].ref,
        selection_kind=snapshot.selection[1].selection_kind,
        contribution_id=snapshot.selection[1].contribution_id,
        contribution_ordinal=0,
    )
    with pytest.raises(ContextAssemblySealPreflightError) as error:
        validate_seal_dispatch_invariants(
            _with_selection(
                snapshot,
                (snapshot.selection[0], snapshot.selection[1], duplicated),
            )
        )
    assert error.value.code == "seal-dispatch-source-identity-conflict"


def test_selection_ref_manifest_mismatch_fails_closed() -> None:
    # domain 构造器只拦截部分字段漂移；seal 边界必须独立兜底（例如恢复的
    # 旧快照 content_hash 与 ref manifest 不一致）。
    snapshot = _minimal_snapshot()
    drifted = _mutate_entry(
        snapshot.selection[0], content_hash="sha256:jcs:v1:" + "0" * 64
    )
    with pytest.raises(ContextAssemblySealPreflightError) as error:
        validate_seal_dispatch_invariants(
            _with_selection(snapshot, (drifted, snapshot.selection[1]))
        )
    assert error.value.code == "seal-dispatch-source-identity-conflict"


def _overlay_pair_snapshot() -> ContextAssemblySnapshot:
    base_ref = _request_ref(
        "skill-base",
        base_delta_role=BaseDeltaRole.BASE.value,
        source_overlay_epoch=1,
        content_hash_override=contribution_content_hash(
            "overlay_base", {"text": "skill-base"}
        ),
        content_length_override=len(canonical_json_bytes({"text": "skill-base"})),
    )
    delta_ref = _request_ref(
        "skill-delta",
        base_delta_role=BaseDeltaRole.DELTA.value,
        source_overlay_epoch=1,
        overlay_from_revision="rev-skill-base",
        overlay_to_revision="rev-skill-delta",
        overlay_diff_hash=sha256_jcs({"overlay": "skill-base:skill-delta"}),
        content_hash_override=contribution_content_hash(
            "overlay_delta", {"text": "skill-delta"}
        ),
        content_length_override=len(canonical_json_bytes({"text": "skill-delta"})),
    )
    base_selection = _entry(
        0,
        base_ref,
        selection_kind=SelectionKind.OVERLAY_BASE.value,
        contribution_id="skill-base",
        contribution_ordinal=0,
        base_delta_role=BaseDeltaRole.BASE.value,
        source_overlay_epoch=1,
    )
    delta_selection = _entry(
        1,
        delta_ref,
        selection_kind=SelectionKind.OVERLAY_DELTA.value,
        contribution_id="skill-delta",
        contribution_ordinal=1,
        base_delta_role=BaseDeltaRole.DELTA.value,
        source_overlay_epoch=1,
        overlay_from_revision="rev-skill-base",
        overlay_to_revision=delta_ref.overlay_to_revision,
        overlay_diff_hash=delta_ref.overlay_diff_hash,
    )
    return _snapshot(
        selection=(base_selection, delta_selection),
        refs=(base_ref, delta_ref),
        contributions=(
            _contribution(
                "skill-base",
                kind="overlay_base",
                metadata={
                    "overlay_id": "skill-a-overlay",
                    "overlay_ref": "skill-base",
                    "overlay_role": "base",
                    "source_overlay_epoch": 1,
                },
            ),
            _contribution(
                "skill-delta",
                kind="overlay_delta",
                metadata={
                    "overlay_id": "skill-a-overlay",
                    "overlay_ref": "skill-delta",
                    "overlay_role": "delta",
                    "source_overlay_epoch": 1,
                },
            ),
        ),
    )


def test_overlay_delta_with_wrong_from_revision_fails_closed() -> None:
    snapshot = _overlay_pair_snapshot()
    drifted_delta = _mutate_entry(
        snapshot.selection[1], overlay_from_revision="rev-other"
    )
    with pytest.raises(ContextAssemblySealPreflightError) as error:
        validate_seal_dispatch_invariants(
            _clone_with(snapshot, selection=(snapshot.selection[0], drifted_delta))
        )
    assert error.value.code == "seal-dispatch-overlay-order-conflict"


def test_overlay_chain_in_order_passes() -> None:
    snapshot = _overlay_pair_snapshot()
    validate_seal_dispatch_invariants(snapshot)


def _tool_pair_snapshot() -> ContextAssemblySnapshot:
    call = _canonical_ref(
        "call-1", SemanticKind.TOOL_CALL.value, PayloadKind.TOOL_CALL.value
    )
    result = _canonical_ref(
        "result-1", SemanticKind.TOOL_RESULT.value, PayloadKind.TOOL_RESULT.value
    )
    return _snapshot(
        selection=(
            _entry(0, call, selection_kind=SelectionKind.CANONICAL_HISTORY.value),
            _entry(1, result, selection_kind=SelectionKind.CANONICAL_HISTORY.value),
        ),
        refs=(call, result),
    )


def test_tool_entries_without_pairings_fail_closed() -> None:
    with pytest.raises(ContextAssemblySealPreflightError) as error:
        validate_seal_dispatch_invariants(_tool_pair_snapshot())
    assert error.value.code == "seal-dispatch-tool-pairing-conflict"


def test_tool_entries_with_valid_pairing_pass() -> None:
    validate_seal_dispatch_invariants(
        _tool_pair_snapshot(),
        tool_pairings=(("call-1", "result-1"),),
    )


def test_unpaired_tool_result_fails_closed() -> None:
    with pytest.raises(ContextAssemblySealPreflightError) as error:
        validate_seal_dispatch_invariants(
            _tool_pair_snapshot(),
            tool_pairings=(("call-1", "missing-result"),),
        )
    assert error.value.code == "seal-dispatch-tool-pairing-conflict"


def _canonical_record(
    item_id: str,
    *,
    semantic_kind: str,
    payload_kind: str,
    payload: object,
) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=1,
        item_id=item_id,
        semantic_kind=semantic_kind,
        payload_kind=payload_kind,
        status=CanonicalItemStatus.COMPLETED.value,
        producer_ref={"producer_kind": "provider", "producer_id": item_id},
        payload=payload,
        turn_id="turn-pairing",
        turn_scope="turn_member",
        message_group_id=f"message-{item_id}",
        wire_role="assistant",
    )


def test_canonical_tool_pairings_derive_from_explicit_ids() -> None:
    call = _canonical_record(
        "call-item-1",
        semantic_kind=SemanticKind.TOOL_CALL.value,
        payload_kind=PayloadKind.TOOL_CALL.value,
        payload={"tool_calls": [{"id": "tc-1", "name": "tool", "args": {}}]},
    )
    result = _canonical_record(
        "result-item-1",
        semantic_kind=SemanticKind.TOOL_RESULT.value,
        payload_kind=PayloadKind.TOOL_RESULT.value,
        payload={"tool_call_id": "tc-1", "result_id": "res-1", "content": "ok"},
    )

    assert canonical_tool_pairings((call, result)) == (
        ("call-item-1", "result-item-1"),
    )


def test_canonical_tool_pairings_ignore_non_tool_items() -> None:
    plain = _canonical_record(
        "text-item-1",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT.value,
        payload_kind=PayloadKind.TEXT.value,
        payload="正文",
    )

    assert canonical_tool_pairings((plain,)) == ()


def test_canonical_tool_pairings_fail_closed_without_explicit_link() -> None:
    # 正向构造合法 result，再旁路剥离显式 tool_call_id，模拟绕过 domain
    # 构造器恢复/外部写入的脏 item：preflight 必须独立 fail closed。
    result = _canonical_record(
        "result-item-1",
        semantic_kind=SemanticKind.TOOL_RESULT.value,
        payload_kind=PayloadKind.TOOL_RESULT.value,
        payload={"tool_call_id": "tc-1", "result_id": "res-1", "content": "ok"},
    )
    object.__setattr__(result, "payload", {"content": "缺少 tool_call_id"})

    with pytest.raises(ContextAssemblySealPreflightError) as error:
        canonical_tool_pairings((result,))
    assert error.value.code == "seal-dispatch-tool-pairing-conflict"


def test_canonical_tool_pairings_fail_closed_on_duplicate_call_id() -> None:
    first = _canonical_record(
        "call-item-1",
        semantic_kind=SemanticKind.TOOL_CALL.value,
        payload_kind=PayloadKind.TOOL_CALL.value,
        payload={"tool_calls": [{"id": "tc-1", "name": "tool", "args": {}}]},
    )
    second = _canonical_record(
        "call-item-2",
        semantic_kind=SemanticKind.TOOL_CALL.value,
        payload_kind=PayloadKind.TOOL_CALL.value,
        payload={"tool_calls": [{"id": "tc-1", "name": "tool", "args": {}}]},
    )

    with pytest.raises(ContextAssemblySealPreflightError) as error:
        canonical_tool_pairings((first, second))
    assert error.value.code == "seal-dispatch-tool-pairing-conflict"


def test_canonical_tool_pairings_restore_scoped_stream_call_id() -> None:
    # stream carrier 保存 model-call scoped ID；result 保存 provider 原始 ID。
    stream_call = _canonical_record(
        "item-stream-call-1",
        semantic_kind=SemanticKind.TOOL_CALL.value,
        payload_kind=PayloadKind.TOOL_CALL.value,
        payload={
            "name": "tool",
            "args": {},
            "tool_call_id": "mc-1:tool-call:tc-1",
        },
    )
    object.__setattr__(stream_call, "metadata", {"model_call_id": "mc-1"})
    result = _canonical_record(
        "result-item-1",
        semantic_kind=SemanticKind.TOOL_RESULT.value,
        payload_kind=PayloadKind.TOOL_RESULT.value,
        payload={"tool_call_id": "tc-1", "result_id": "res-1", "content": "ok"},
    )

    assert canonical_tool_pairings((stream_call, result)) == (
        ("item-stream-call-1", "result-item-1"),
    )


def test_canonical_tool_pairings_cover_dual_carriers_with_one_result() -> None:
    # 同一次调用的 stream 影子 carrier 与 checkpoint carrier 共享一个 result。
    stream_call = _canonical_record(
        "item-stream-call-1",
        semantic_kind=SemanticKind.TOOL_CALL.value,
        payload_kind=PayloadKind.TOOL_CALL.value,
        payload={
            "name": "tool",
            "args": {},
            "tool_call_id": "mc-1:tool-call:tc-1",
        },
    )
    object.__setattr__(stream_call, "metadata", {"model_call_id": "mc-1"})
    checkpoint_call = _canonical_record(
        "item-checkpoint-call-1",
        semantic_kind=SemanticKind.TOOL_CALL.value,
        payload_kind=PayloadKind.TOOL_CALL.value,
        payload={"tool_calls": [{"id": "tc-1", "name": "tool", "args": {}}]},
    )
    object.__setattr__(
        checkpoint_call,
        "metadata",
        {
            "execution_confirmed": True,
            "model_call_id": "mc-1",
            "projection_group": {"ordinal": 1, "size": 2},
            "projection_message_id": "lc_run--mc-1",
        },
    )
    result = _canonical_record(
        "result-item-1",
        semantic_kind=SemanticKind.TOOL_RESULT.value,
        payload_kind=PayloadKind.TOOL_RESULT.value,
        payload={"tool_call_id": "tc-1", "result_id": "res-1", "content": "ok"},
    )

    assert canonical_tool_pairings((stream_call, checkpoint_call, result)) == (
        ("item-stream-call-1", "result-item-1"),
        ("item-checkpoint-call-1", "result-item-1"),
    )
