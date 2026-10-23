"""ResourceRegistry 与语义资源快照的身份和 CAS 合同测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace
from threading import Event

import pytest

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import ResourceMemberBinding
from app.services.infrastructure.resource_platform.derivation import (
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)


def test_registry_replaces_only_the_expected_dependency_descriptor(
    resource_registry: ResourceRegistry,
) -> None:
    original = SemanticResourceDescriptor(
        resource_id="catalog",
        resource_kind="skill",
        facet="catalog",
        display_uri=None,
        dependency_resource_ids=("first",),
    )
    resource_registry.register_descriptor(original)
    candidate = replace(original, dependency_resource_ids=("second",))
    with pytest.raises(ValueError, match="描述冲突"):
        resource_registry.register_descriptor(candidate)
    resource_registry.register_descriptor(candidate, expected=original)
    assert resource_registry.descriptor("catalog") is candidate
    with pytest.raises(ValueError, match="描述冲突"):
        resource_registry.register_descriptor(original, expected=original)
    with pytest.raises(ValueError, match="身份不可替换"):
        resource_registry.register_descriptor(
            replace(candidate, facet="metadata"), expected=candidate
        )


@pytest.fixture
def registry_catalog_member() -> ResourceMemberBinding:
    return ResourceMemberBinding(
        member_id="mcp:server:demo",
        member_kind="mcp",
        schema_hash="sha256:" + "a" * 64,
        server_id="server-demo",
        revision="catalog-revision-1",
    )


@pytest.fixture
def registry_catalog_snapshot() -> ResourceSnapshot:
    payload = {"guidance": "固定目录指引"}
    return ResourceSnapshot(
        resource_id="mcp:catalog",
        resource_kind="mcp_tool_catalog",
        facet="guidance",
        display_uri=None,
        revision=sha256_jcs(payload),
        payload=payload,
        source_lineage=(("mcp:catalog-source", "catalog-revision-1"),),
        generation=1,
    )


@pytest.mark.parametrize(
    "identity",
    [
        "/work/project/SKILL.md",
        "./skills/demo",
        "skills/demo",
        r"C:\work\demo",
        "boxteam://workspace/test/resources/skills/demo/SKILL.md",
        "https://example.com/resource",
        "owner@resource",
        "encoded%2Fpath",
        "bad\nidentity",
    ],
)
@pytest.mark.parametrize(
    "field",
    ["descriptor_id", "source_ids", "dependency_ids", "snapshot_id", "lineage_id"],
)
def test_registry_rejects_locator_shaped_identity_before_publication(
    resource_registry: ResourceRegistry,
    registry_catalog_snapshot: ResourceSnapshot,
    identity: str,
    field: str,
) -> None:
    original = resource_registry.publish(registry_catalog_snapshot)
    descriptor = resource_registry.descriptor(original.resource_id)
    before = resource_registry.capture()
    with pytest.raises(ValueError, match="ResourceIdentity"):
        if field == "descriptor_id":
            resource_registry.register_descriptor(
                replace(descriptor, resource_id=identity)
            )
        elif field == "source_ids":
            resource_registry.register_descriptor(
                replace(descriptor, source_ids=(identity,))
            )
        elif field == "dependency_ids":
            resource_registry.register_descriptor(
                replace(descriptor, dependency_resource_ids=(identity,))
            )
        elif field == "snapshot_id":
            resource_registry.publish(replace(original, resource_id=identity))
        else:
            resource_registry.publish(
                replace(original, source_lineage=((identity, "revision-2"),))
            )
    assert resource_registry.capture() == before
    assert resource_registry.snapshot(original.resource_id) is original
    assert resource_registry.last_valid_snapshot(original.resource_id) is original


@pytest.mark.parametrize(
    "identity",
    [
        "skill:demo:metadata",
        "mcp:catalog",
        "res_019abc",
        "019c390d-6a70-7c85-8709-afddc785bf93",
    ],
)
def test_registry_keeps_opaque_identity_independent_of_uri_and_revision(
    resource_registry: ResourceRegistry,
    registry_catalog_snapshot: ResourceSnapshot,
    identity: str,
) -> None:
    snapshot = replace(registry_catalog_snapshot, resource_id=identity)
    first = resource_registry.publish(snapshot)
    changed = resource_registry.publish(
        replace(snapshot, revision="next-revision", payload={"guidance": "next"})
    )
    assert changed.resource_id == first.resource_id == identity
    assert changed.display_uri is first.display_uri is None
    assert resource_registry.descriptor(identity).resource_id == identity
    assert resource_registry.publish(replace(changed)) is changed


def test_resource_snapshot_keeps_members_typed_and_separate_from_payload(
    registry_catalog_snapshot: ResourceSnapshot,
    registry_catalog_member: ResourceMemberBinding,
) -> None:
    assert registry_catalog_snapshot.members == ()
    custom = ResourceMemberBinding(
        member_id="custom:demo",
        member_kind="custom",
        schema_hash="sha256:" + "b" * 64,
        server_id=None,
        revision="custom-revision",
    )
    populated = replace(
        registry_catalog_snapshot, members=(registry_catalog_member, custom)
    )
    assert populated.members == (custom, registry_catalog_member)
    assert populated.payload is registry_catalog_snapshot.payload
    assert populated.revision == registry_catalog_snapshot.revision
    with pytest.raises(ValueError, match="typed tuple"):
        replace(registry_catalog_snapshot, members=(registry_catalog_member.to_dict(),))
    with pytest.raises(ValueError, match="member_id 必须唯一"):
        replace(
            registry_catalog_snapshot,
            members=(registry_catalog_member, registry_catalog_member),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"member_id": "mcp:server:other"},
        {"member_kind": "custom", "server_id": None},
        {"schema_hash": "sha256:" + "b" * 64},
        {"server_id": "server-other"},
        {"revision": "catalog-revision-2"},
    ],
)
def test_registry_same_semantic_revision_publishes_complete_member_changes(
    resource_registry: ResourceRegistry,
    registry_catalog_snapshot: ResourceSnapshot,
    registry_catalog_member: ResourceMemberBinding,
    changes: dict[str, object],
) -> None:
    first = resource_registry.publish(
        replace(registry_catalog_snapshot, members=(registry_catalog_member,))
    )
    changed = replace(registry_catalog_member, **changes)
    second = resource_registry.publish(replace(first, members=(changed,)))
    assert second is not first
    assert second.members == (changed,)
    assert second.revision == first.revision
    assert second.payload == first.payload
    assert resource_registry.publish(replace(second)) is second
    assert resource_registry.last_valid_snapshot(second.resource_id) is second
    removed = resource_registry.publish(replace(second, members=()))
    assert removed.members == () and removed is not second
    assert resource_registry.snapshot(removed.resource_id) is removed


def test_registry_capture_waits_for_cross_thread_descriptor_snapshot_publication(
    registry_catalog_snapshot: ResourceSnapshot,
) -> None:
    descriptor_registered = Event()
    release_publication = Event()
    capture_started = Event()

    class PausingRegistry(ResourceRegistry):
        def register_descriptor(self, descriptor: SemanticResourceDescriptor) -> None:
            super().register_descriptor(descriptor)
            descriptor_registered.set()
            assert release_publication.wait(2), "发布线程未收到释放信号"

    registry = PausingRegistry()

    def capture():
        capture_started.set()
        return registry.capture()

    with ThreadPoolExecutor(max_workers=2) as workers:
        publication = workers.submit(registry.publish, registry_catalog_snapshot)
        try:
            assert descriptor_registered.wait(1)
            captured = workers.submit(capture)
            assert capture_started.wait(1)
            # 描述已登记而完整发布尚未完成时，另一个线程不能读到半次发布。
            with pytest.raises(FutureTimeoutError):
                captured.result(timeout=0.05)
        finally:
            release_publication.set()
        assert publication.result(timeout=1) is registry_catalog_snapshot
        value = captured.result(timeout=1)

    assert value.snapshots == (registry_catalog_snapshot,)
    assert tuple(item.resource_id for item in value.descriptors) == ("mcp:catalog",)
    assert value.resource_kinds == frozenset({"mcp_tool_catalog"})
    registry.publish(replace(registry_catalog_snapshot, generation=2))
    assert value.snapshots[0].generation == 1
    assert registry.capture().snapshots[0].generation == 2
