"""thread-qualified ref identity 在整个 domain 判定链路上必须唯一且一致。

OpenSpec add-itemized-rollout-context design §509/§617/§748 与 tasks 8 要求
context ref 按 (session_id, thread_id) 定位；同一 Session 下两个 sibling
thread 各自拥有独立的 canonical context，同名 ref 不是同一 identity。

本文件锁定的是：domain 内所有「身份 / 去重 / 查找 / 排序 / 哈希范围选择」
都必须走唯一的 selection_ref_identity，不得在某一层退回 (ref_type, ref_id)
二元组；否则 sibling thread 的同名 ref 会被误判为重复或互相覆盖。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import (
    ContextRef,
    ToolSetRef,
    ref_identity,
    selection_ref_identity,
    unique_ref_identities,
)
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serialization import ordered_selection


def _canonical_ref(
    item: CanonicalItemRecord, *, thread_id: str
) -> ContextRef:
    return ContextRef.canonical_item(item, session_id="session-1", thread_id=thread_id)


def _selection(ref: ContextRef, ordinal: int) -> ContextSelectionEntry:
    return ContextSelectionEntry(
        assembly_id="assembly-sibling",
        plan_ordinal=ordinal,
        ref=ref,
        selection_kind="canonical_history",
        source_revision=ref.source_revision,
        content_length=ref.content_length,
        content_hash=ref.content_hash,
        visibility=ref.visibility,
        protection=ref.protection,
        availability=ref.availability,
    )


@pytest.fixture
def sibling_refs(
    user_item: CanonicalItemRecord,
) -> tuple[ContextRef, ContextRef]:
    # 同一 item_id 在两个 sibling thread 中各自的 local identity；不是同一 ref。
    main_ref = _canonical_ref(user_item, thread_id="thread-main")
    child_ref = _canonical_ref(user_item, thread_id="thread-child")
    assert main_ref.ref_id == child_ref.ref_id
    assert main_ref != child_ref
    return main_ref, child_ref


def test_selection_ref_identity_matches_registry_identity_for_context_ref(
    sibling_refs: tuple[ContextRef, ContextRef],
) -> None:
    # selection 层与 registry 层必须共用同一 thread-qualified 口径。
    main_ref, child_ref = sibling_refs
    assert selection_ref_identity(main_ref) == ref_identity(main_ref)
    assert selection_ref_identity(child_ref) == ref_identity(child_ref)
    assert selection_ref_identity(main_ref) != selection_ref_identity(child_ref)


def test_selection_ref_identity_fails_closed_without_thread_id() -> None:
    # 缺失 thread_id 时不得静默退化成二元组，必须显式失败。
    class _ForgedRef:
        ref_type = "canonical_item"
        ref_id = "item-1"

    with pytest.raises(ItemSchemaError, match="thread_id"):
        selection_ref_identity(_ForgedRef())


def test_selection_ref_identity_keys_tool_set_by_ref_id(
    user_item: CanonicalItemRecord,
) -> None:
    # ToolSetRef 尚无 thread_id，其 plan/assembly 内唯一性由 ref_id 表达；
    # 不得用 session_id 冒充 thread，也不得据此声称已 thread 化。
    tool_ref = ToolSetRef.from_tool_snapshot(
        snapshot_id="tool-set-sibling",
        session_id="session-1",
        plan_id="plan-sibling",
        tools=[],
        source_revision="tools-rev",
    )
    assert selection_ref_identity(tool_ref) == ("tool_set", "tool-set-sibling")
    with pytest.raises(ItemSchemaError, match="ToolSetRef.ref_id"):
        selection_ref_identity(replace(tool_ref, ref_id=""))


def test_ordered_selection_accepts_sibling_thread_refs_once(
    sibling_refs: tuple[ContextRef, ContextRef],
) -> None:
    # 修复前：二元组去重把两个仅 thread 不同的 ref 判为重复引用而误拒。
    main_ref, child_ref = sibling_refs
    plan = ContextRequestPlan(
        session_id="session-1",
        plan_id="plan-sibling",
        refs=(main_ref, child_ref),
    ).seal_for_assembly(
        "assembly-sibling",
        selection=(_selection(main_ref, 0), _selection(child_ref, 1)),
    )
    assert len(plan.selection) == 2

    with pytest.raises(ItemSchemaError, match="不得重复引用同一个 ref"):
        ordered_selection((_selection(main_ref, 0), _selection(main_ref, 1)))


def test_sibling_thread_registry_is_rejected_only_on_true_duplicate(
    sibling_refs: tuple[ContextRef, ContextRef],
) -> None:
    main_ref, child_ref = sibling_refs
    assert unique_ref_identities((main_ref, child_ref)) == (main_ref, child_ref)
    with pytest.raises(ItemSchemaError, match="thread_id"):
        unique_ref_identities((main_ref, main_ref))


def test_sealed_plan_hash_is_stable_across_sibling_registry_order(
    sibling_refs: tuple[ContextRef, ContextRef],
) -> None:
    # 两个 sibling ref 都进入 hash scope 时，排序必须是全序；否则 registry
    # 迭代顺序会改写已提交 plan 的 plan_hash。
    main_ref, child_ref = sibling_refs
    selection = (_selection(main_ref, 0), _selection(child_ref, 1))
    registry_order = (main_ref, child_ref)
    base = ContextRequestPlan(
        session_id="session-1",
        plan_id="plan-sibling",
        refs=registry_order,
    )
    plan = base.seal_for_assembly("assembly-sibling", selection=selection)
    reversed_registry = replace(base, refs=tuple(reversed(registry_order)))
    reordered = reversed_registry.seal_for_assembly(
        "assembly-sibling",
        selection=selection,
    )
    assert reordered.plan_hash() == plan.plan_hash()

    # 两个 sibling ref 都必须留在 hash scope 内，不能被二元组键静默漏掉。
    assert len(plan.refs) == 2
    assert [entry.ref.thread_id for entry in plan.selection] == [
        "thread-main",
        "thread-child",
    ]


def test_snapshot_accepts_sibling_thread_refs_and_rejects_true_duplicate(
    sibling_refs: tuple[ContextRef, ContextRef],
) -> None:
    main_ref, child_ref = sibling_refs
    base_kwargs: dict[str, object] = {
        "assembly_id": "assembly-sibling",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "execution_id": "execution-1",
        "plan_id": "plan-sibling",
        "plan_hash": "sha256:jcs:v1:" + "0" * 64,
        "request_hash": "sha256:jcs:v1:" + "1" * 64,
        "history_view_revision": 0,
        "source_overlay_epoch": 0,
        "contributions": (),
        "tool_snapshot": (),
        "compiler_version": "itemized-context-v1",
        "provider_version": "provider-1",
        "sealed": True,
    }
    snapshot = ContextAssemblySnapshot(refs=(main_ref, child_ref), **base_kwargs)
    assert len(snapshot.refs) == 2

    with pytest.raises(ItemSchemaError, match="重复 identity"):
        ContextAssemblySnapshot(refs=(main_ref, main_ref), **base_kwargs)
