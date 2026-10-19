"""真实 Coordinator 消费成员变化；PrefixEpoch 另由 MCP/Saver 用例验证。"""

from dataclasses import replace

import pytest

from app.domain.itemized.resource_activation import (
    ResourceActivationPolicySnapshot,
    ResourceMemberBinding,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from tests.unit.services.orchestration.resource_activation.test_resource_activation import (
    _admission,
    _coordinator,
    _FakeSaver,
)


@pytest.fixture
def published_catalog():
    member = ResourceMemberBinding(
        member_id="tool-a", member_kind="mcp", schema_hash="sha256:" + "a" * 64,
        server_id="server-a", revision="catalog-a",
    )
    return ResourceSnapshot(
        resource_id="mcp:catalog", resource_kind="mcp_tool_catalog", facet="activation",
        display_uri=None, revision="guidance-1", payload={"content": "稳定指引"},
        source_lineage=(("catalog-source", "source-1"),), generation=3, members=(member,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"member_id": "tool-b"},
    {"member_kind": "custom", "server_id": None},
    {"schema_hash": "sha256:" + "b" * 64},
    {"server_id": "server-b"},
    {"revision": "catalog-b"},
], ids=("member-id", "member-kind", "schema-hash", "server-id", "member-revision"))
async def test_only_member_detail_changes_neither_v1_content_hash(published_catalog, changes):
    registry = ResourceRegistry()
    registry.publish(published_catalog)
    saver = _FakeSaver()
    coordinator = _coordinator(registry, saver)
    parent = await coordinator.freeze_execution(
        admission=_admission("turn-epoch-neutral"),
        policy=ResourceActivationPolicySnapshot(
            default_boundary="turn", boundaries={"mcp_tool_catalog": "model_call"},
        ),
    )
    original = await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=parent, model_call_id="before-members",
    ))
    updated = replace(published_catalog, members=(replace(published_catalog.members[0], **changes),))
    registry.publish(updated)
    changed = await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=parent, model_call_id="after-members",
    ))

    assert original.parent is changed.parent is parent
    assert original.bindings[0].members == published_catalog.members
    assert changed.bindings[0].members == updated.members != published_catalog.members
    # 两次都走 Registry capture 和 Coordinator；除成员与 typed 正文引用外父行不变。
    assert replace(changed.bindings[0], members=original.bindings[0].members,
                   detail_ref=original.bindings[0].detail_ref) == original.bindings[0]
    assert changed.registry_generation == original.registry_generation == published_catalog.generation
    assert changed.bindings_hash == original.bindings_hash
    assert changed.activation_provenance_hash == original.activation_provenance_hash
    assert saver.snapshots == [parent, original, changed]
