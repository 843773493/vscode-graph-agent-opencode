"""SkillCatalog 三层权威与 ResourceRegistry 发布合同测试(OpenSpec 4.1/4.1-A)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.skill_runtime import build_workspace_skill_catalog
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.virtual_resources import parse_vrn


def _write_skill(root: Path, name: str, description: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


@pytest.fixture()
def layered_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """构造 workspace 与 gateway 两层同名/异名 Skill 的隔离环境。"""
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)
    _write_skill(
        tmp_path / "boxteam-home" / "skills",
        "shared",
        "Gateway 版本",
        "gateway body   \n",
    )
    _write_skill(
        tmp_path / "boxteam-home" / "skills",
        "gateway-only",
        "仅 Gateway",
        "# gateway-only\n",
    )
    _write_skill(
        tmp_path / ".boxteam" / "skills",
        "shared",
        "Workspace 版本",
        "workspace body\n",
    )
    _write_skill(
        tmp_path / ".boxteam" / "skills",
        "local-only",
        "仅 Workspace",
        "# local-only\n",
    )
    return tmp_path


def _build(tmp_path: Path, registry: ResourceRegistry):
    return build_workspace_skill_catalog(tmp_path, registry=registry)


def test_catalog_publishes_unique_entry_with_fixed_priority(layered_workspace):
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry)

    by_name = {entry.name: entry for entry in catalog.entries}
    assert set(by_name) == {"shared", "gateway-only", "local-only"}
    # workspace > gateway-global:同名解析唯一 entry,origin 保留在 identity。
    assert by_name["shared"].layer == "workspace"
    assert by_name["shared"].entry_identity == "skill-entry:workspace:shared"
    assert by_name["gateway-only"].layer == "gateway"
    assert by_name["local-only"].layer == "workspace"


def test_catalog_revision_is_immutable_and_registry_cas_is_idempotent(
    layered_workspace,
):
    registry = ResourceRegistry()
    first = _build(layered_workspace, registry)
    second = _build(layered_workspace, registry)

    assert first.catalog_revision == second.catalog_revision
    assert first.catalog_snapshot is second.catalog_snapshot
    by_name = {entry.name: entry for entry in first.entries}
    for entry in by_name.values():
        metadata = registry.snapshot(entry.metadata_resource_id)
        assert isinstance(metadata, ResourceSnapshot)
        assert metadata.available
        assert registry.publish(metadata) is metadata


def test_catalog_binding_payload_contains_registry_owned_refs(layered_workspace):
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry)
    payload = catalog.catalog_snapshot.payload
    assert isinstance(payload, dict)

    entries = payload["entries"]
    assert len(entries) == len(catalog.entries)
    for item in entries:
        assert item["metadata_revision"] == item["metadata_revision"]
        metadata = registry.snapshot(item["metadata_resource_id"])
        assert metadata.revision == item["metadata_revision"]
        # display URI 必须通过 C4 VRN grammar,且不是 identity/dedupe key。
        parsed = parse_vrn(item["display_uri"])
        assert parsed.kind == "skills"
        assert item["entry_identity"].startswith(f"skill-entry:{item['layer']}:")


def test_activation_facet_uses_exact_bytes_after_frontmatter(layered_workspace):
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry)
    by_name = {entry.name: entry for entry in catalog.entries}

    activation = registry.snapshot(by_name["shared"].activation_resource_id)
    assert activation.available
    # workspace 覆盖后 activation 正文精确保留 frontmatter 之后的 bytes,
    # 不做任何 BOM/换行/空白规范化。
    assert activation.payload == {"body": "workspace body\n"}
    assert by_name["shared"].activation_revision == activation.revision

    # 被同名高优先级覆盖的 entry 不是有效 catalog entry,不发布语义快照;
    # 曾生效过的旧快照由 registry 保留,供 tracked binding 继续消费。
    with pytest.raises(KeyError):
        registry.snapshot("skill-entry:gateway:shared:activation")
    gateway_only = registry.snapshot(
        by_name["gateway-only"].activation_resource_id
    )
    assert gateway_only.available
    assert gateway_only.payload == {"body": "# gateway-only\n"}


def test_metadata_facet_payload_hides_locator_and_ignored_fields(layered_workspace):
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry)
    entry = next(item for item in catalog.entries if item.name == "shared")

    metadata = registry.snapshot(entry.metadata_resource_id)
    assert metadata.payload == {
        "name": "shared",
        "description": "Workspace 版本",
        "display_uri": entry.display_uri,
        "entry_identity": entry.entry_identity,
    }
    assert metadata.source_lineage[0][0] == "skill-source:workspace:shared"


def test_published_snapshot_does_not_reresolve_current_file(layered_workspace):
    """已发布快照是冻结事实：当前 URI 指向的文件变化不得影响旧 snapshot。

    OpenSpec add-context-injection-lifecycle 6.6：历史 snapshot 不按当前
    URI 重新解析正文；URI 是 locator/provenance，不是内容来源。
    """
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry)
    entry = next(item for item in catalog.entries if item.name == "shared")
    frozen = registry.snapshot(entry.activation_resource_id)
    frozen_revision = frozen.revision
    frozen_payload = frozen.payload

    # 当前 display URI 指向的文件被整篇改写。
    skill_file = layered_workspace / ".boxteam" / "skills" / "shared" / "SKILL.md"
    skill_file.write_text(
        "---\nname: shared\ndescription: 改写版本\n---\nrewritten body\n",
        encoding="utf-8",
    )

    # 不重新 build/发布时，registry 只返回已发布快照；
    # 不得按当前 URI 重新读取正文（对象同一性 + revision + payload 全部不变）。
    reread = registry.snapshot(entry.activation_resource_id)
    assert reread is frozen
    assert reread.revision == frozen_revision
    assert reread.payload == frozen_payload

    # 只有显式 rebuild 才发布新 revision；先前持有的 snapshot 对象保持冻结。
    rebuilt = _build(layered_workspace, registry)
    rebuilt_entry = next(item for item in rebuilt.entries if item.name == "shared")
    assert rebuilt_entry.activation_revision != frozen_revision
    assert frozen.revision == frozen_revision
    assert frozen.payload == frozen_payload
