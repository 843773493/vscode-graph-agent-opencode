"""SkillCatalog 三层权威与 ResourceRegistry 发布合同测试(OpenSpec 4.1/4.1-A)。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from app.agents.skill_runtime import build_workspace_skill_catalog
from app.core.trace_middleware import (
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
    WorkspaceFileResourceSnapshot,
)
from app.services.infrastructure.resource_platform.virtual_resources import (
    parse_resource_uri,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)


def _write_skill(root: Path, name: str, description: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


FAKE_GATEWAY_ID = "gateway_0123456789abcdef0123456789abcdef"


@pytest.fixture()
def gateway_identity():
    """在测试作用域内绑定真实 gateway_id，模拟 Gateway 按请求注入该身份。

    gateway 层有条目时 MUST 绑定真实身份；缺失即 fail-closed，故所有触发 gateway
    层的用例都要显式绑定，不得依赖字面量回退。
    """
    token = set_current_gateway_id(FAKE_GATEWAY_ID)
    try:
        yield FAKE_GATEWAY_ID
    finally:
        reset_current_gateway_id(token)


@pytest.fixture()
def layered_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_identity: str,
) -> Path:
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


@pytest.fixture
def file_registry(layered_workspace: Path) -> WorkspaceFileResourceRegistry:
    return WorkspaceFileResourceRegistry(
        workspace_root=layered_workspace,
        watch_service=WorkspaceFileWatchService(workspace_root=layered_workspace),
        project_root=Path.cwd(),
    )


def _build(
    tmp_path: Path,
    registry: ResourceRegistry,
    file_registry: WorkspaceFileResourceRegistry,
):
    return build_workspace_skill_catalog(
        tmp_path,
        registry=registry,
        file_registry=file_registry,
    )


def test_catalog_publishes_unique_entry_with_fixed_priority(
    layered_workspace, file_registry,
):
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry, file_registry)

    by_name = {entry.name: entry for entry in catalog.entries}
    assert set(by_name) == {"shared", "gateway-only", "local-only"}
    # workspace > gateway-global:同名解析唯一 entry,origin 保留在 identity。
    assert by_name["shared"].layer == "workspace"
    assert by_name["shared"].entry_identity == "skill-entry:workspace:shared"
    assert by_name["gateway-only"].layer == "gateway"
    assert by_name["local-only"].layer == "workspace"


def test_catalog_revision_is_immutable_and_registry_cas_is_idempotent(
    layered_workspace, file_registry,
):
    registry = ResourceRegistry()
    first = _build(layered_workspace, registry, file_registry)
    second = _build(layered_workspace, registry, file_registry)

    assert first.catalog_revision == second.catalog_revision
    assert first.catalog_snapshot is second.catalog_snapshot
    by_name = {entry.name: entry for entry in first.entries}
    for entry in by_name.values():
        metadata = registry.snapshot(entry.metadata_resource_id)
        assert isinstance(metadata, ResourceSnapshot)
        assert metadata.available
        assert registry.publish(metadata) is metadata


def test_catalog_binding_payload_contains_registry_owned_refs(
    layered_workspace, file_registry,
):
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry, file_registry)
    assert catalog.catalog_snapshot.display_uri is None
    payload = catalog.catalog_snapshot.payload
    assert isinstance(payload, dict)

    entries = payload["entries"]
    assert len(entries) == len(catalog.entries)
    for item in entries:
        assert item["metadata_revision"] == item["metadata_revision"]
        metadata = registry.snapshot(item["metadata_resource_id"])
        assert metadata.revision == item["metadata_revision"]
        # display URI 必须通过 C4 URI grammar,且不是 identity/dedupe key。
        parsed = parse_resource_uri(item["display_uri"])
        assert parsed.kind == "skills"
        assert item["entry_identity"].startswith(f"skill-entry:{item['layer']}:")


def test_activation_facet_uses_exact_bytes_after_frontmatter(
    layered_workspace, file_registry,
):
    workspace_skill = (
        layered_workspace / ".boxteam" / "skills" / "shared" / "SKILL.md"
    )
    content = (
        "---\r\nname: shared\r\ndescription: Workspace 版本\r\n---\r\n"
        "workspace body  \r\nline é\r\n"
    )
    workspace_skill.write_bytes(content.encode("utf-8"))
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry, file_registry)
    by_name = {entry.name: entry for entry in catalog.entries}

    activation = registry.snapshot(by_name["shared"].activation_resource_id)
    assert activation.available
    # CRLF、尾部空格与 Unicode 正文均来自解析 metadata 时使用的同一快照。
    expected_body = "workspace body  \r\nline é\r\n"
    assert activation.payload == {"body": expected_body}
    assert by_name["shared"].activation_revision == activation.revision
    expected_raw_revision = "sha256:" + hashlib.sha256(
        workspace_skill.read_bytes()
    ).hexdigest()
    accepted_snapshot = file_registry.snapshot(by_name["shared"].display_uri)
    assert accepted_snapshot.revision == expected_raw_revision
    assert accepted_snapshot.content == content
    metadata = registry.snapshot(by_name["shared"].metadata_resource_id)
    assert metadata.payload["description"] == "Workspace 版本"
    assert metadata.source_lineage == (
        ("skill-source:workspace:shared", expected_raw_revision),
    )
    assert activation.source_lineage == metadata.source_lineage

    # 覆盖只改变 effective catalog；原 Gateway 来源仍发布 exact facets 供 tracked 冻结。
    shadowed = registry.snapshot("skill-entry:gateway:shared:activation")
    assert shadowed.available and shadowed.payload == {"body": "gateway body   \n"}
    assert shadowed.display_uri != by_name["shared"].display_uri
    assert by_name["shared"].layer == "workspace"
    gateway_only = registry.snapshot(
        by_name["gateway-only"].activation_resource_id
    )
    assert gateway_only.available
    assert gateway_only.payload == {"body": "# gateway-only\n"}


def test_required_unavailable_skill_snapshot_never_uses_retained_content(
    layered_workspace,
    file_registry,
    monkeypatch,
):
    stale_snapshot = WorkspaceFileResourceSnapshot(
        uri="boxteam://gateway/gateway_0123456789abcdef0123456789abcdef/resources/skills/shared/SKILL.md",
        revision="sha256:" + "a" * 64,
        content=(
            "---\nname: shared\ndescription: stale retained metadata\n---\n"
            "stale retained body\n"
        ),
        available=False,
        error="source changed during read",
    )
    monkeypatch.setattr(
        file_registry,
        "_read_via_reconciler",
        lambda uri: replace(stale_snapshot, uri=uri),
    )

    with pytest.raises(RuntimeError, match="Skill 文件来源不可用"):
        build_workspace_skill_catalog(
            layered_workspace,
            registry=ResourceRegistry(),
            file_registry=file_registry,
            bundled_skill_groups=(),
        )


def test_metadata_facet_payload_hides_locator_and_ignored_fields(
    layered_workspace, file_registry,
):
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry, file_registry)
    entry = next(item for item in catalog.entries if item.name == "shared")

    metadata = registry.snapshot(entry.metadata_resource_id)
    assert metadata.payload == {
        "name": "shared",
        "description": "Workspace 版本",
        "display_uri": entry.display_uri,
        "entry_identity": entry.entry_identity,
    }
    assert metadata.source_lineage[0][0] == "skill-source:workspace:shared"


def test_published_snapshot_does_not_reresolve_current_file(
    layered_workspace, file_registry,
):
    """已发布快照是冻结事实：当前 URI 指向的文件变化不得影响旧 snapshot。

    OpenSpec add-context-injection-lifecycle 6.6：历史 snapshot 不按当前
    URI 重新解析正文；URI 是 locator/provenance，不是内容来源。
    """
    registry = ResourceRegistry()
    catalog = _build(layered_workspace, registry, file_registry)
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
    file_registry.refresh(entry.display_uri)

    # 不重新 build/发布时，registry 只返回已发布快照；
    # 不得按当前 URI 重新读取正文（对象同一性 + revision + payload 全部不变）。
    reread = registry.snapshot(entry.activation_resource_id)
    assert reread is frozen
    assert reread.revision == frozen_revision
    assert reread.payload == frozen_payload

    # 只有显式 rebuild 才发布新 revision；先前持有的 snapshot 对象保持冻结。
    rebuilt = _build(layered_workspace, registry, file_registry)
    rebuilt_entry = next(item for item in rebuilt.entries if item.name == "shared")
    assert rebuilt_entry.activation_revision != frozen_revision
    assert frozen.revision == frozen_revision
    assert frozen.payload == frozen_payload
