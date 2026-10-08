"""v1 hash preimage 的成员细节负向证明；真实 PrefixEpoch 另由 MCP/Saver 用例验证。"""

from dataclasses import replace

import pytest

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationPolicySnapshot,
    ResourceMemberBinding,
)
from tests.unit.domain.itemized.test_resource_activation_contract import (
    binding,
    execution_snapshot,
)


@pytest.fixture
def parent_with_members():
    ref = replace(binding(display_uri=None), resource_id="mcp:catalog", resource_kind="mcp_tool_catalog")
    member = ResourceMemberBinding(
        member_id="tool-a", member_kind="mcp", schema_hash="sha256:" + "a" * 64,
        server_id="server-a", revision="catalog-a",
    )
    policy = ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={})
    return execution_snapshot(
        bindings=(replace(ref, members=(member,)),),
        activation_policy=policy, activation_policy_revision=policy.revision,
        activation_policy_hash=policy.hash,
    )


@pytest.mark.parametrize("changes", [
    {"member_id":"tool-b"},
    {"member_kind":"custom", "server_id":None},
    {"schema_hash":"sha256:" + "b" * 64},
    {"server_id":"server-b"},
    {"revision":"catalog-b"},
])
def test_only_member_detail_changes_neither_v1_content_hash(parent_with_members, changes):
    parent = parent_with_members
    ref = parent.bindings[0]
    changed = replace(parent, bindings=(replace(ref, members=(replace(ref.members[0], **changes),)),))
    assert changed.bindings[0].members != ref.members
    assert changed.bindings_hash == parent.bindings_hash
    assert changed.activation_provenance_hash == parent.activation_provenance_hash


def test_null_field_is_in_original_jcs_v1_preimage(parent_with_members):
    parent = parent_with_members
    ref = parent.bindings[0]
    preimage = {
        "schema":"resource-activation-bindings:v1",
        "bindings":[{
            "resource_id":ref.resource_id, "resource_kind":ref.resource_kind,
            "owner_scope":ref.owner_scope, "facet":ref.facet, "revision":ref.revision,
            "content_length":ref.content_length, "content_hash":ref.content_hash,
            "availability":ref.availability, "display_uri":None,
        }],
    }
    assert parent.bindings_hash == sha256_jcs(preimage)
    del preimage["bindings"][0]["display_uri"]
    assert parent.bindings_hash != sha256_jcs(preimage)


@pytest.mark.parametrize("changes", [
    {"resource_id":"mcp:other"},
    {"resource_kind":"skills"},
    {"revision":"semantic-revision-2"},
])
def test_parent_semantic_selection_changes_v1_bindings_hash(parent_with_members, changes):
    parent = parent_with_members
    changed = replace(parent, bindings=(replace(parent.bindings[0], **changes),))
    assert changed.bindings_hash != parent.bindings_hash
