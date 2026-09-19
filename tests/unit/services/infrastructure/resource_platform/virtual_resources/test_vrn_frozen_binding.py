"""VRN 冻结绑定单元测试：历史不重解、同名覆盖不改绑、provenance 路径隐藏。"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    skill_display_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    CatalogBinding,
    TrackedResourceBinding,
    VirtualResourceCatalog,
    VirtualResourceResolver,
    VrnResolveError,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    OPERATION_ACTIVATE,
    ResolutionContext,
    ResolvedResourceHandle,
    ResourceProvenance,
    SemanticResourceDescriptor,
)

_CAPS = frozenset({OPERATION_ACTIVATE})


def _binding(
    resource_id: str,
    display_uri: str,
    *,
    revision: str = "rev-1",
    snapshot_ref: str | None = "cas:sha256:aaaa",
) -> CatalogBinding:
    return CatalogBinding(
        descriptor=SemanticResourceDescriptor(
            resource_id=resource_id,
            source_id=f"src-{resource_id}",
            kind="skills",
            display_uri=display_uri,
            semantic_revision=revision,
            semantic_hash=f"hash-{revision}",
        ),
        capabilities=_CAPS,
        snapshot_ref=snapshot_ref,
    )


def _gateway_uri(skill_name: str) -> str:
    return skill_display_uri(scope="gateway", scope_id="gw-1", skill_name=skill_name)


def test_frozen_binding_survives_same_name_override() -> None:
    old_uri = _gateway_uri("code-review")
    tracked = TrackedResourceBinding(
        name="code-review", binding=_binding("res-gw-1", old_uri)
    )
    # 新 catalog：同名高优先级 workspace Skill 覆盖，display URI 与 resource 均不同。
    new_uri = skill_display_uri(
        scope="workspace", scope_id="ws-1", skill_name="code-review"
    )
    new_binding = _binding("res-ws-2", new_uri, revision="rev-2", snapshot_ref="cas:2")
    resolver = VirtualResourceResolver(
        VirtualResourceCatalog(
            bindings={new_uri: new_binding},
            name_index={("workspace", "code-review"): new_uri},
        )
    )
    # 未来名称解析指向 workspace 资源。
    handle = resolver.resolve_name(
        scope="workspace",
        logical_name="code-review",
        operation=OPERATION_ACTIVATE,
        context=ResolutionContext(workspace_id="ws-1"),
    )
    assert handle.descriptor.resource_id == "res-ws-2"
    # 既有 registration 继续绑定原 resource id，不改绑。
    frozen = tracked.resolve_frozen()
    assert frozen.descriptor.resource_id == "res-gw-1"
    assert frozen.descriptor.semantic_revision == "rev-1"
    assert frozen.descriptor.display_uri == old_uri


def test_frozen_binding_ignores_same_uri_revision_update() -> None:
    uri = _gateway_uri("code-review")
    tracked = TrackedResourceBinding(
        name="code-review", binding=_binding("res-gw-1", uri, revision="rev-1")
    )
    # 同一 display URI 当前 revision 已更新，历史恢复仍用封存 revision。
    resolver = VirtualResourceResolver(
        VirtualResourceCatalog(
            bindings={uri: _binding("res-gw-1", uri, revision="rev-9", snapshot_ref="cas:9")},
            name_index={("gateway", "code-review"): uri},
        )
    )
    frozen = tracked.resolve_frozen()
    assert frozen.descriptor.semantic_revision == "rev-1"
    assert frozen.snapshot_ref == "cas:sha256:aaaa"
    # 当前 catalog 按需解析才得到新 revision，且两者互不影响。
    current = resolver.resolve(
        uri,
        operation=OPERATION_ACTIVATE,
        context=ResolutionContext(gateway_id="gw-1"),
    )
    assert current.descriptor.semantic_revision == "rev-9"


def test_frozen_binding_missing_snapshot_fails_explicitly() -> None:
    uri = _gateway_uri("code-review")
    tracked = TrackedResourceBinding(
        name="code-review",
        binding=_binding("res-gw-1", uri, snapshot_ref=None),
    )
    with pytest.raises(VrnResolveError) as excinfo:
        tracked.resolve_frozen()
    assert excinfo.value.reason_code == "historical_snapshot_missing"


def test_resource_identity_stable_across_rename() -> None:
    old_uri = _gateway_uri("old-name")
    new_uri = _gateway_uri("new-name")
    old_descriptor = _binding("res-gw-1", old_uri).descriptor
    new_descriptor = _binding("res-gw-1", new_uri, revision="rev-2").descriptor
    assert old_descriptor.resource_id == new_descriptor.resource_id
    assert old_descriptor.display_uri != new_descriptor.display_uri


def test_provenance_hides_physical_paths() -> None:
    field_names = {field.name for field in dataclasses.fields(ResourceProvenance)}
    assert field_names == {
        "display_uri",
        "resource_id",
        "source_id",
        "semantic_revision",
        "semantic_hash",
    }
    with pytest.raises(ValueError):
        ResourceProvenance(
            display_uri="boxteam://gateway/gw-1/resources/skills/s/SKILL.md",
            resource_id="res/gw/secret",
            source_id="src-1",
            semantic_revision="rev-1",
            semantic_hash="hash-1",
        )
    with pytest.raises(ValueError):
        SemanticResourceDescriptor(
            resource_id="res-1",
            source_id="/home/hyf/.boxteams/skills/s/SKILL.md",
            kind="skills",
            display_uri="boxteam://gateway/gw-1/resources/skills/s/SKILL.md",
            semantic_revision="rev-1",
            semantic_hash="hash-1",
        )
    descriptor = _binding("res-gw-1", _gateway_uri("code-review")).descriptor
    provenance = ResourceProvenance(
        display_uri=descriptor.display_uri,
        resource_id=descriptor.resource_id,
        source_id=descriptor.source_id,
        semantic_revision=descriptor.semantic_revision,
        semantic_hash=descriptor.semantic_hash,
    )
    rendered = repr(provenance)
    assert "/home" not in rendered
    assert ".boxteams" not in rendered


def test_handle_rejects_locator_style_snapshot_ref() -> None:
    descriptor = _binding("res-gw-1", _gateway_uri("code-review")).descriptor
    provenance = ResourceProvenance(
        display_uri=descriptor.display_uri,
        resource_id=descriptor.resource_id,
        source_id=descriptor.source_id,
        semantic_revision=descriptor.semantic_revision,
        semantic_hash=descriptor.semantic_hash,
    )
    with pytest.raises(ValueError):
        ResolvedResourceHandle(
            descriptor=descriptor,
            snapshot_ref="/home/hyf/.boxteams/skills/s/SKILL.md",
            capabilities=_CAPS,
            provenance=provenance,
        )
