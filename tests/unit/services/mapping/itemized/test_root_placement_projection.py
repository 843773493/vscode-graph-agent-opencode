"""E1 projector 接线合同:request-only 贡献按 root 资格投影。

root_eligible 编入唯一 system root;tail_only 永不进入 system root,
按独立 user-role item 在 plan 顺序位置追加。同时验证 envelope 往返
保留 root_placement 声明。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serde.registry import parse_contribution
from app.services.mapping.itemized.langchain import project_context_plan

SESSION = "session-root-placement"
PLAN = "plan-root-placement"
ASSEMBLY = "assembly-root-placement"
BODY = {"text": "受控来源正文"}


def _contribution(root_placement: str) -> ContextContribution:
    return ContextContribution(
        contribution_id=f"contribution-{root_placement}",
        source_kind="context_source",
        source_revision="rev-root-placement",
        content_hash=contribution_content_hash("prompt", BODY),
        body=BODY,
        content_length=len(canonical_json_bytes(BODY)),
        source_ordinal=0,
        root_placement=root_placement,
    )


def _sealed_plan(contribution: ContextContribution) -> ContextRequestPlan:
    request_ref = ContextRef.request_only_ref(
        contribution.contribution_id,
        session_id=SESSION,
        plan_id=PLAN,
        source_revision=contribution.source_revision,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        content_length=contribution.content_length,
        content_hash_value=contribution.content_hash,
        source_ref="root-placement-probe",
    )
    request_selection = ContextSelectionEntry(
        assembly_id=ASSEMBLY,
        plan_ordinal=0,
        ref=request_ref,
        selection_kind="request_only",
        source_revision=request_ref.source_revision,
        content_length=request_ref.content_length,
        content_hash=request_ref.content_hash,
        visibility=request_ref.visibility,
        protection=request_ref.protection,
        availability=request_ref.availability,
        detail_ref=DetailRef(SESSION, ASSEMBLY, "detail-root-placement-probe"),
        contribution_id=contribution.contribution_id,
        contribution_ordinal=0,
    )
    return ContextRequestPlan(
        session_id=SESSION,
        plan_id=PLAN,
        refs=(request_ref,),
        contributions=(contribution,),
    ).seal_for_assembly(ASSEMBLY, selection=(request_selection,))


@pytest.mark.parametrize("root_placement", ["root_eligible", "tail_only"])
def test_root_placement_envelope_round_trip(root_placement: str) -> None:
    contribution = _contribution(root_placement)
    raw = _sealed_plan(contribution).to_dict()["contributions"][0]
    restored = parse_contribution(raw, sealed=False)
    assert restored.root_placement == root_placement
    with pytest.raises(ItemSchemaError):
        parse_contribution({**raw, "root_placement": "root"}, sealed=False)


def test_root_eligible_contribution_compiles_into_system_root() -> None:
    contribution = _contribution("root_eligible")
    messages = project_context_plan(
        _sealed_plan(contribution),
        (),
        request_only_content={contribution.contribution_id: BODY},
    )
    system_messages = [m for m in messages if isinstance(m, SystemMessage)]
    assert len(system_messages) == 1
    assert messages == system_messages
    selection = system_messages[0].response_metadata["selection"]
    assert selection[0]["context_contribution_id"] == contribution.contribution_id


def test_tail_only_contribution_never_enters_system_root() -> None:
    contribution = _contribution("tail_only")
    messages = project_context_plan(
        _sealed_plan(contribution),
        (),
        request_only_content={contribution.contribution_id: BODY},
    )
    assert not any(isinstance(m, SystemMessage) for m in messages)
    assert len(messages) == 1
    user_item = messages[0]
    assert isinstance(user_item, HumanMessage)
    # 结构化 dict 正文包成单元素 content block。
    assert user_item.content == [BODY]
    assert user_item.response_metadata["wire_role"] == "user"
    assert user_item.response_metadata["root_placement"] == "tail_only"
    assert (
        user_item.response_metadata["context_contribution_id"]
        == contribution.contribution_id
    )
