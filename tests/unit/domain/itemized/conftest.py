from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.itemized.assembly_snapshot import (
    ContextAssemblySnapshot,
    context_request_hash,
)
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SelectionKind,
    SemanticKind,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry


@pytest.fixture(scope="session")
def hash_vectors() -> dict[str, object]:
    path = Path.cwd() / "tests/fixtures/itemized/hash_vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def item_matrix() -> dict[str, object]:
    path = Path.cwd() / "tests/fixtures/itemized/item_matrix.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def golden_plan(hash_vectors: dict[str, object]) -> ContextRequestPlan:
    scenario = hash_vectors["scenario"]
    item = CanonicalItemRecord.from_dict(scenario["item"])
    ref = ContextRef.canonical_item(item, session_id=scenario["session_id"], thread_id="thread-1")
    tools = ToolSetRef.from_tool_snapshot(
        snapshot_id="tools-golden",
        session_id=scenario["session_id"],
        plan_id=scenario["plan_id"],
        source_revision="tools-rev-golden",
        tools=scenario["tools"],
        tool_set_schema="example.tools",
        tool_policy={"mode": "allow"},
    )
    selection = tuple(
        ContextSelectionEntry(
            assembly_id=scenario["assembly_id"],
            plan_ordinal=ordinal,
            ref=source,
            selection_kind=kind,
            source_revision=source.source_revision,
            content_length=source.content_length,
            content_hash=source.content_hash,
        )
        for ordinal, (source, kind) in enumerate(
            (
                (ref, "canonical_history"),
                (replace(tools, assembly_id=scenario["assembly_id"]), "tool_set"),
            )
        )
    )
    return ContextRequestPlan(
        session_id=scenario["session_id"],
        plan_id=scenario["plan_id"],
        refs=(ref,),
        tool_set_refs=(tools,),
        active_view_id="view-golden",
        history_view_revision=3,
    ).seal_for_assembly(scenario["assembly_id"], selection=selection)


@pytest.fixture
def user_item() -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-user-1",
        semantic_kind=SemanticKind.USER_INPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "user",
            "producer_id": "ingress-1",
            "invocation_id": "turn-1",
        },
        payload="读取 README",
        created_at="2026-09-07T00:00:00+00:00",
        metadata={"source_revision": "rev-1"},
        turn_id="turn-1",
        turn_scope="turn_root",
        message_group_id="group-1",
        wire_role="user",
    )


@pytest.fixture
def omitted_snapshot(user_item: CanonicalItemRecord) -> ContextAssemblySnapshot:
    item_ref = ContextRef.canonical_item(user_item, session_id="session-1", thread_id="thread-1")
    omitted = ContextSelectionEntry(
        assembly_id="assembly-omitted",
        plan_ordinal=0,
        ref=item_ref,
        selection_kind=SelectionKind.CANONICAL_HISTORY,
        included=False,
        omission_reason="budget",
        loss=("budget",),
        visibility=item_ref.visibility,
        protection=item_ref.protection,
        availability=item_ref.availability,
    )
    plan = ContextRequestPlan(
        session_id="session-1",
        plan_id="plan-omitted",
        refs=(item_ref,),
    ).seal_for_assembly("assembly-omitted", selection=(omitted,))
    return ContextAssemblySnapshot(
        assembly_id="assembly-omitted",
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(
            plan,
            "provider-1",
            target_format="native",
        ),
        history_view_revision=0,
        source_overlay_epoch=0,
        refs=plan.refs,
        contributions=plan.contributions,
        tool_snapshot=(),
        compiler_version=plan.compiler_version,
        provider_version="provider-1",
        selection=plan.selection,
        sealed=True,
        plan_state="sealed",
        target_format="native",
    )
