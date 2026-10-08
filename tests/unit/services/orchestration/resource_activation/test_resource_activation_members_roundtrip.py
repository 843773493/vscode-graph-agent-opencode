"""MCP NULL 父行与完整 typed members 在冻结和领域恢复中保持精确事实。"""

from dataclasses import replace

import pytest

from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceMemberBinding,
)
from tests.unit.services.orchestration.resource_activation.test_resource_activation import (
    _admission,
    _coordinator,
    _FakeSaver,
    _policy,
    _registry,
)


@pytest.fixture
def members_registry():
    registry = _registry()
    snapshot = registry.snapshot("mcp:catalog")
    members = (
        ResourceMemberBinding(member_id="z-mcp", member_kind="mcp", schema_hash="sha256:" + "a" * 64, server_id="server-a", revision="catalog-a"),
        ResourceMemberBinding(member_id="a-custom", member_kind="custom", schema_hash="sha256:" + "b" * 64, server_id=None, revision="catalog-custom"),
        ResourceMemberBinding(member_id="m-mcp", member_kind="mcp", schema_hash="sha256:" + "c" * 64, server_id="server-b", revision="catalog-b"),
    )
    registry.publish(replace(snapshot, generation=snapshot.generation + 1, members=members))
    return registry


@pytest.mark.asyncio
async def test_freeze_serde_retry_resume_keep_null_parent_and_all_members(members_registry):
    saver = _FakeSaver()
    coordinator = _coordinator(members_registry, saver)
    policy = _policy()
    admission = _admission("same-user-turn")
    execution = await coordinator.freeze_execution(admission=admission, policy=policy)
    frozen = await coordinator.prepare_model_call(parent=execution, model_call_id="first-call", policy=policy)
    parent = frozen.bindings[-1]
    published = members_registry.snapshot("mcp:catalog")
    assert parent.display_uri is None
    assert [member.member_id for member in parent.members] == ["a-custom", "m-mcp", "z-mcp"]
    assert parent.members == published.members
    restored = ResourceActivationSnapshotRef.from_dict(frozen.to_dict(), parent=execution)
    assert restored.to_dict() == frozen.to_dict()
    assert restored.bindings[-1].to_dict() == parent.to_dict()
    assert restored.bindings[-1].members == parent.members
    assert [binding.effective_boundary for binding in frozen.bindings] == ["turn", "model_call"]
    members_registry.publish(replace(
        published, generation=published.generation + 1,
        members=(replace(published.members[0], schema_hash="sha256:" + "d" * 64), *published.members[1:]),
    ))
    retry = await coordinator.prepare_model_call(parent=execution, model_call_id="first-call", policy=policy)
    assert retry.to_dict() == frozen.to_dict()
    resumed = await coordinator.freeze_execution(
        admission=_admission("same-user-turn", execution_id="resumed-execution"), policy=policy,
    )
    assert resumed.execution_id == "resumed-execution"
    assert resumed.activation_snapshot_id != execution.activation_snapshot_id
    assert resumed.to_dict() == {
        **execution.to_dict(),
        "execution_id":resumed.execution_id,
        "activation_snapshot_id":resumed.activation_snapshot_id,
    }
    future = await coordinator.prepare_model_call(parent=resumed, model_call_id="second-call", policy=policy)
    assert future.bindings[-1].members != parent.members
    assert future.bindings[:-1] == execution.bindings
