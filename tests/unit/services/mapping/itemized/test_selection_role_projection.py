"""typed selection_role/replacement_policy 的持久化与同义 flag 隔离边界。

两个字段已在规范定稿为 typed core：`selection_role=direct|backing_only` 替代
自由 `selection_only` flag，`replacement_policy=immutable|replaceable` 替代自由
`replaceable_source` flag。这里只固定 typed 字段的序列化往返、取值闭集，以及
同名 metadata key 不得再生效；registry/ledger 消费点在各自 owner 测试中覆盖。
"""

from __future__ import annotations

import pytest

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import contribution_content_hash
from app.domain.itemized.request_plan import ContextContribution
from app.domain.itemized.serde.registry import parse_contribution

BODY = "selection role typed body"


def _contribution(
    *,
    selection_role: str = "direct",
    replacement_policy: str = "immutable",
    metadata: dict | None = None,
) -> ContextContribution:
    return ContextContribution(
        contribution_id="contribution-role",
        source_kind="workspace_instructions",
        source_revision="rev-role",
        content_hash=contribution_content_hash("prompt", BODY),
        request_only=True,
        metadata=metadata or {},
        contribution_kind="prompt",
        body=BODY,
        source_ordinal=0,
        selection_role=selection_role,
        replacement_policy=replacement_policy,
    )


@pytest.mark.parametrize("selection_role", ["direct", "backing_only"])
@pytest.mark.parametrize("replacement_policy", ["immutable", "replaceable"])
def test_typed_fields_round_trip(
    selection_role: str, replacement_policy: str
) -> None:
    contribution = _contribution(
        selection_role=selection_role, replacement_policy=replacement_policy
    )
    raw = contribution_to_manifest(contribution)
    restored = parse_contribution(raw, sealed=False)
    assert restored.selection_role == selection_role
    assert restored.replacement_policy == replacement_policy


def contribution_to_manifest(contribution: ContextContribution) -> dict:
    from dataclasses import replace

    from app.domain.itemized.serde.plan import unsealed_context_plan_from_dict
    from app.domain.itemized.request_plan import ContextRequestPlan

    plan = ContextRequestPlan(
        session_id="session-role",
        plan_id="plan-role",
        refs=(),
        contributions=(replace(contribution, body=None),),
    )
    manifest = plan.to_dict()
    # 往返前必须能通过完整 domain parser。
    unsealed_context_plan_from_dict(manifest)
    return manifest["contributions"][0]


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("selection_role", "manifest"),
        ("selection_role", True),
        ("replacement_policy", "mutable"),
        ("replacement_policy", 1),
    ],
)
def test_unknown_typed_values_fail_closed(field: str, bad: object) -> None:
    raw = contribution_to_manifest(_contribution())
    with pytest.raises(ItemSchemaError, match=field):
        parse_contribution({**raw, field: bad}, sealed=False)


def test_same_name_metadata_keys_never_drive_typed_fields() -> None:
    """metadata 中的 selection_role/replacement_policy 同名 key 不改变 typed 事实。"""
    raw = contribution_to_manifest(
        _contribution(
            selection_role="direct",
            replacement_policy="immutable",
            metadata={"selection_role": "backing_only", "replacement_policy": "replaceable"},
        )
    )
    restored = parse_contribution(raw, sealed=False)
    assert restored.selection_role == "direct"
    assert restored.replacement_policy == "immutable"


def test_legacy_replaceable_source_key_is_rejected_as_unknown() -> None:
    """旧 replaceable_source 顶层字段必须报未知字段，不能作为兼容别名。"""
    raw = contribution_to_manifest(_contribution())
    with pytest.raises(ItemSchemaError, match="未知|不属于"):
        parse_contribution({**raw, "replaceable_source": True}, sealed=False)
