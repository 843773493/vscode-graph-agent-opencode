"""SkillCatalog 三层权威与 ResourceRegistry 发布合同测试(OpenSpec 4.1/4.1-A)。"""

from __future__ import annotations

import asyncio
import hashlib
from contextvars import Context
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.agents.skill_catalog import (
    build_workspace_skill_catalog,
    start_workspace_skill_catalog_publication,
)
from app.core.lifecycle import LifetimeScope
from app.core.path_utils import get_boxteam_home
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.services.business.resource_derivations.skills import SkillFacetLoader
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
from tests.support.gateway_skill_sources import accepted_gateway_skill_sources


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
        gateway_sources=accepted_gateway_skill_sources(
            skills_root=get_boxteam_home() / "skills", gateway_id=FAKE_GATEWAY_ID,
            workspace_id=load_or_create_workspace_id(tmp_path), observation_channel=file_registry.observation_channel,
        ),
    )


async def test_background_catalog_publications_keep_their_frozen_gateway_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)
    gateway_b = "gateway_fedcba9876543210fedcba9876543210"
    roots = {FAKE_GATEWAY_ID: tmp_path / "home-a/skills", gateway_b: tmp_path / "home-b/skills"}
    for gateway_id, root in roots.items():
        _write_skill(root, "shared", "owner " + gateway_id, "body " + gateway_id + "\n")
    files = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path, watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
    )
    workspace_id = load_or_create_workspace_id(tmp_path)
    sources = accepted_gateway_skill_sources(
        skills_root=roots[FAKE_GATEWAY_ID], gateway_id=FAKE_GATEWAY_ID,
        workspace_id=workspace_id, observation_channel=files.observation_channel,
    )

    def publication(gateway_id: str):
        producer = GatewaySkillSourceOwner(gateway_id=gateway_id, skills_root=roots[gateway_id])
        producer._publish()
        return producer.snapshot(workspace_id=workspace_id)

    sources._accept_snapshot(publication(gateway_b), gateway_id=gateway_b)
    registries = {gateway_id: ResourceRegistry() for gateway_id in roots}
    scopes = {gateway_id: LifetimeScope("catalog " + gateway_id) for gateway_id in roots}
    catalogs = {
        gateway_id: build_workspace_skill_catalog(
            tmp_path, registry=registries[gateway_id], file_registry=files,
            gateway_id=gateway_id, gateway_sources=sources,
        )
        for gateway_id in roots
    }
    for gateway_id, catalog in catalogs.items():
        assert catalog.gateway_id == gateway_id
        assert catalog.entries[0].display_uri == f"boxteam://gateway/{gateway_id}/resources/skills/shared/SKILL.md"
        # 后台任务继承空请求 Context，必须只依赖各 catalog 冻结的 owner。
        Context().run(
            start_workspace_skill_catalog_publication, catalog,
            registry=registries[gateway_id], file_registry=files,
            lifetime_scope=scopes[gateway_id], gateway_sources=sources,
        )
    catalog_id = catalogs[gateway_b].catalog_snapshot.resource_id
    initial_a = registries[FAKE_GATEWAY_ID].snapshot(catalog_id)
    initial_b = registries[gateway_b].snapshot(catalog_id)

    async def wait_for_b(predicate) -> None:
        async with asyncio.timeout(1):
            while not predicate(registries[gateway_b].snapshot(catalog_id)):
                await asyncio.sleep(0)

    try:
        sources._state(gateway_b).client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(403, json={"detail": "B 已断开"}),
        ))
        token = set_current_request_id("req-background-owner-b-disconnect")
        try:
            await sources.reconcile(gateway_id=gateway_b)
        finally:
            reset_current_request_id(token)
        await wait_for_b(lambda snapshot: not snapshot.available)
        assert registries[FAKE_GATEWAY_ID].snapshot(catalog_id) is initial_a
        assert registries[gateway_b].last_valid_snapshot(catalog_id) is initial_b

        skill = roots[gateway_b] / "shared/SKILL.md"
        skill.write_text("---\nname: shared\ndescription: B 已更新\n---\nB 新正文\n")
        sources._accept_snapshot(publication(gateway_b), gateway_id=gateway_b)
        await wait_for_b(lambda snapshot: snapshot.available and snapshot.revision != initial_b.revision)
        assert registries[FAKE_GATEWAY_ID].snapshot(catalog_id) is initial_a
        updated_b = registries[gateway_b].snapshot(catalog_id)
        assert updated_b.payload["entries"][0]["display_uri"] == catalogs[gateway_b].entries[0].display_uri
        activation = registries[gateway_b].snapshot(catalogs[gateway_b].entries[0].activation_resource_id)
        assert activation.payload == {"body": "B 新正文\n"}
        assert all(gateway_b in source_id for source_id, _revision in activation.source_lineage)
        original_activation = registries[FAKE_GATEWAY_ID].snapshot(catalogs[FAKE_GATEWAY_ID].entries[0].activation_resource_id)
        assert original_activation.payload == {"body": "body " + FAKE_GATEWAY_ID + "\n"}
    finally:
        for scope in scopes.values():
            await scope.close()
        await sources.close()


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


def test_inline_gateway_workspace_priority_and_cross_workspace_availability(tmp_path, monkeypatch):
    from app.core.trace_middleware import (
        reset_current_gateway_id,
        set_current_gateway_id,
    )

    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_text(
        '{"schema_version":1,"distribution":"source-development","version":"0.0.2"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(manifest))
    project_root = tmp_path / "distribution"
    inline_root = project_root / "resources" / "skills"
    inline_root.mkdir(parents=True)
    _write_skill(inline_root, "shared", "Inline", "inline body\n")
    gateway_root = tmp_path / "boxteam-home" / "skills"
    _write_skill(gateway_root, "shared", "Gateway", "gateway body\n")

    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_inline = tmp_path / "workspace-inline"
    for workspace in (workspace_a, workspace_b, workspace_inline):
        workspace.mkdir()
        (workspace / ".boxteam" / "skills").mkdir(parents=True)
    _write_skill(workspace_a / ".boxteam" / "skills", "shared", "Workspace", "workspace body\n")

    gateway_id = "gateway_0123456789abcdef0123456789abcdef"
    gateway_token = set_current_gateway_id(gateway_id)
    try:
        catalogs = []
        for workspace in (workspace_a, workspace_b):
            files = WorkspaceFileResourceRegistry(
                workspace_root=workspace,
                watch_service=WorkspaceFileWatchService(workspace_root=workspace),
                project_root=project_root,
            )
            sources = accepted_gateway_skill_sources(
                skills_root=gateway_root, gateway_id=gateway_id,
                workspace_id=load_or_create_workspace_id(workspace),
                observation_channel=files.observation_channel,
            )
            catalogs.append(build_workspace_skill_catalog(
                workspace, registry=ResourceRegistry(), file_registry=files,
                project_root=project_root, gateway_sources=sources,
                gateway_id=gateway_id,
                bundled_skill_groups=("shared",),
            ))
    finally:
        reset_current_gateway_id(gateway_token)
    inline_files = WorkspaceFileResourceRegistry(
        workspace_root=workspace_inline,
        watch_service=WorkspaceFileWatchService(workspace_root=workspace_inline),
        project_root=project_root,
    )
    catalogs.append(build_workspace_skill_catalog(
        workspace_inline, registry=ResourceRegistry(), file_registry=inline_files,
        project_root=project_root, bundled_skill_groups=("shared",),
    ))

    assert (catalogs[0].entries[0].layer, catalogs[0].entries[0].description) == ("workspace", "Workspace")
    assert (catalogs[1].entries[0].layer, catalogs[1].entries[0].description) == ("gateway", "Gateway")
    assert (catalogs[2].entries[0].layer, catalogs[2].entries[0].description) == ("inline", "Inline")


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
    shadowed = registry.snapshot(f"skill-entry:gateway:{FAKE_GATEWAY_ID}:shared:activation")
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

    with pytest.raises(RuntimeError, match="Skill 来源不可用"):
        build_workspace_skill_catalog(
            layered_workspace,
            registry=ResourceRegistry(),
            file_registry=file_registry,
            bundled_skill_groups=(),
            gateway_sources=accepted_gateway_skill_sources(
                skills_root=get_boxteam_home() / "skills", gateway_id=FAKE_GATEWAY_ID,
                workspace_id=load_or_create_workspace_id(layered_workspace), observation_channel=file_registry.observation_channel,
            ),
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
    }
    assert registry.descriptor(entry.metadata_resource_id).display_uri == entry.display_uri
    assert metadata.source_lineage[0][0] == "skill-source:workspace:shared"


def test_metadata_facet_ignores_uri_and_identity_carriers():
    from app.services.business.resource_derivations.skills import (
        SkillDerivationEntry,
        SkillFacetLoader,
    )
    from app.services.infrastructure.resource_platform.derivation.types import (
        SemanticInput,
    )

    entry = SkillDerivationEntry(
        layer="workspace", name="shared",
        display_uri="boxteam://workspace/one/resources/skills/shared/SKILL.md",
        entry_identity="skill-entry:workspace:shared", source_id="skill-source:workspace:shared",
    )
    renamed = SkillDerivationEntry(
        layer=entry.layer, name=entry.name,
        display_uri="boxteam://workspace/two/resources/skills/shared/SKILL.md",
        entry_identity="skill-entry:workspace:renamed-carrier",
        source_id=entry.source_id,
    )
    content = "---\nname: shared\ndescription: stable\n---\nbody\n"
    inputs = {entry.source_id: SemanticInput(entry.source_id, "rev-1", 1, content)}

    assert SkillFacetLoader(entry, "metadata")(inputs) == SkillFacetLoader(renamed, "metadata")(inputs)
    assert SkillFacetLoader(entry, "metadata")(inputs).payload == {"name": "shared", "description": "stable"}


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


def test_graph_keeps_current_raw_lineage_when_ignored_fields_do_not_change_facets(
    layered_workspace, file_registry,
):
    registry = ResourceRegistry()
    first = _build(layered_workspace, registry, file_registry)
    entry = next(item for item in first.entries if item.name == "shared")
    before = registry.snapshot(entry.metadata_resource_id)
    source = layered_workspace / ".boxteam/skills/shared/SKILL.md"
    source.write_text(source.read_text().replace("---\nworkspace body", "ignored: audit\n---\nworkspace body"))
    raw = file_registry.refresh(entry.display_uri)
    current = _build(layered_workspace, registry, file_registry)
    metadata = registry.snapshot(entry.metadata_resource_id)
    activation = registry.snapshot(entry.activation_resource_id)
    assert metadata.revision == before.revision
    assert metadata.source_lineage != before.source_lineage
    assert metadata.source_lineage == (("skill-source:workspace:shared", raw.revision),)
    assert activation.source_lineage == metadata.source_lineage
    assert activation.revision == entry.activation_revision
    assert current.catalog_revision == first.catalog_revision
    catalog_spec = registry.descriptor(current.catalog_snapshot.resource_id)
    assert catalog_spec.loader_version == "skill-catalog:v2"
    assert set(catalog_spec.source_ids) == set(current.directory_ids.values())
    assert {entry.metadata_resource_id, entry.activation_resource_id} <= set(catalog_spec.dependency_resource_ids)
    assert registry.descriptor(entry.metadata_resource_id).loader_version == "skill-frontmatter:v1"


@pytest.mark.parametrize("invalid", [
    b"\xff\xfe",
    b"---\nname: shared\n---\ninvalid\n",
    b"---\nname: shared\n  private-invalid-frontmatter\n---\ninvalid\n",
])
def test_graph_failure_preserves_audit_and_recovers_identical_skill_revision(
    layered_workspace, file_registry, invalid, caplog,
):
    registry = ResourceRegistry()
    original = _build(layered_workspace, registry, file_registry)
    entry = next(item for item in original.entries if item.name == "shared")
    source = layered_workspace / ".boxteam/skills/shared/SKILL.md"
    valid_bytes = source.read_bytes()
    resource_ids = (entry.metadata_resource_id, entry.activation_resource_id, original.catalog_snapshot.resource_id)
    valid = {resource_id: registry.snapshot(resource_id) for resource_id in resource_ids}
    source.write_bytes(invalid)
    for _ in range(2):
        file_registry.refresh(entry.display_uri)
        with pytest.raises(RuntimeError) as caught:
            _build(layered_workspace, registry, file_registry)
        if invalid == b"\xff\xfe":
            assert "invalid_utf8" in str(caught.value)
            assert entry.display_uri in str(caught.value)
            assert str(layered_workspace) not in str(caught.value)
            raw_error = file_registry.snapshot(entry.display_uri).error
            assert raw_error is not None and raw_error in str(caught.value)
        elif b"private-invalid-frontmatter" in invalid:
            assert entry.display_uri in caplog.text
            assert "unsupported_structure" in caplog.text
            assert "private-invalid-frontmatter" in caplog.text
        for resource_id in resource_ids:
            current = registry.snapshot(resource_id)
            assert not current.available and current.payload is None
            assert str(layered_workspace) not in repr(current)
            assert "private-invalid-frontmatter" not in repr(current)
            assert current.retained_revision == valid[resource_id].revision
            assert registry.last_valid_snapshot(resource_id) is valid[resource_id]
    source.write_bytes(valid_bytes)
    file_registry.refresh(entry.display_uri)
    recovered = _build(layered_workspace, registry, file_registry)
    assert recovered.catalog_revision == original.catalog_revision
    for resource_id in resource_ids:
        current = registry.snapshot(resource_id)
        assert current.available and current.retained_revision is None
        assert current.revision == valid[resource_id].revision


async def test_background_catalog_loader_defect_exposes_original_error_without_snapshot_leak(
    layered_workspace, file_registry, monkeypatch,
):
    registry = ResourceRegistry()
    sources = accepted_gateway_skill_sources(
        skills_root=get_boxteam_home() / "skills", gateway_id=FAKE_GATEWAY_ID,
        workspace_id=load_or_create_workspace_id(layered_workspace), observation_channel=file_registry.observation_channel,
    )
    catalog = build_workspace_skill_catalog(
        layered_workspace, registry=registry, file_registry=file_registry, gateway_sources=sources,
    )
    valid = {item.resource_id: item for item in registry.snapshots() if item.resource_kind == "skill"}
    agents = {item.resource_id: item for item in registry.snapshots() if item.resource_kind == "workspace_agents"}
    scope = LifetimeScope("catalog loader defect")
    loop = asyncio.get_running_loop()
    prior_handler = loop.get_exception_handler()
    reported = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    source = layered_workspace / ".boxteam/skills/shared/SKILL.md"
    defect = ValueError(f"loader 程序缺陷: {source} private-invalid-frontmatter")

    def broken_loader(_loader, _inputs):
        raise defect

    try:
        start_workspace_skill_catalog_publication(
            catalog, registry=registry, file_registry=file_registry, lifetime_scope=scope, gateway_sources=sources,
        )
        monkeypatch.setattr(SkillFacetLoader, "__call__", broken_loader)
        source.write_text(source.read_text() + "新正文\n")
        uri = next(item.display_uri for item in catalog.entries if item.name == "shared")
        file_registry.refresh(uri)
        async with asyncio.timeout(1):
            while not reported:
                await asyncio.sleep(0)
        assert reported[0]["exception"] is defect
        assert len(agents) == 2
        assert all(registry.snapshot(resource_id) is previous for resource_id, previous in agents.items())
        for resource_id, previous in valid.items():
            current = registry.snapshot(resource_id)
            assert not current.available and current.payload is None
            assert current.retained_revision == previous.revision
            assert registry.last_valid_snapshot(resource_id) is previous
            assert str(source) not in repr(current)
            assert "private-invalid-frontmatter" not in repr(current)
            assert "ValueError" in current.error
    finally:
        try:
            with pytest.raises(ExceptionGroup) as caught:
                await scope.close()
            assert len(caught.value.exceptions) == 1
            assert caught.value.exceptions[0].__cause__ is defect
        finally:
            loop.set_exception_handler(prior_handler)
            await sources.close()
