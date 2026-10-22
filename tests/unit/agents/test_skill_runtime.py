from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents.custom_tools import CustomToolFactoryContext
from app.agents.policy.custom_tool_spec import parse_custom_tool_spec
from app.agents.skill_runtime import (
    WorkspaceSkillsMiddleware,
    build_workspace_skill_catalog,
    resolve_bundled_skill_groups,
)
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.agents.tools.testing import create_test_tool_2
from app.agents.workspace_backend import build_workspace_backend
from app.core.path_utils import get_boxteam_home
from app.core.trace_middleware import (
    GATEWAY_ID_HEADER,
    TraceMiddleware,
    get_current_gateway_id,
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.resource_platform.virtual_resources import (
    ResolutionContext,
    parse_resource_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
    require_scope_binding,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from tests.support.gateway_skill_sources import accepted_gateway_skill_sources


def _custom_tool_context(tmp_path) -> CustomToolFactoryContext:
    return CustomToolFactoryContext(
        session_id="ses_test",
        agent_id="default",
        sender_agent_id="default",
        workspace_root=tmp_path,
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
        job_event_bus=MagicMock(),
        job_service=MagicMock(),
        session_context_query_service=MagicMock(),
        workspace_session_context_client=MagicMock(),
        session_orchestrator=MagicMock(),
        config_service=MagicMock(),
        terminal_resource_owner=MagicMock(),
        browser_resource_owner=MagicMock(),
        invocation_context=ToolInvocationContext(),
    )


def _write_runtime_manifest(root: Path) -> Path:
    manifest = root / "runtime-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "distribution": "source-development",
                "version": "0.0.2",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


# 请求级注入的真实 gateway_id 替身；gateway scope 的 scope_id 必须等于它，
# MUST NOT 是字面量 ``local``。
FAKE_GATEWAY_ID = "gateway_0123456789abcdef0123456789abcdef"


def _build_catalog(
    workspace: Path,
    *,
    registry: ResourceRegistry,
    file_registry: WorkspaceFileResourceRegistry | None = None,
    bundled_skill_groups=None,
    project_root: Path | None = None,
    gateway_sources=None,
):
    source_owner = file_registry or WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=WorkspaceFileWatchService(workspace_root=workspace),
        project_root=project_root,
    )
    gateway_id = get_current_gateway_id()
    if gateway_sources is None and gateway_id is not None:
        gateway_sources = accepted_gateway_skill_sources(
            skills_root=get_boxteam_home() / "skills", gateway_id=gateway_id,
            workspace_id=load_or_create_workspace_id(workspace), observation_channel=source_owner.observation_channel,
        )
    return build_workspace_skill_catalog(
        workspace,
        registry=registry,
        file_registry=source_owner,
        bundled_skill_groups=bundled_skill_groups,
        project_root=project_root,
        gateway_sources=gateway_sources,
    )


@pytest.fixture()
def gateway_identity():
    """在测试作用域内绑定真实 gateway_id，模拟 Gateway 按请求注入。"""
    token = set_current_gateway_id(FAKE_GATEWAY_ID)
    try:
        yield FAKE_GATEWAY_ID
    finally:
        reset_current_gateway_id(token)


def test_build_workspace_skill_catalog_empty_layers_publishes_empty_revision(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)

    registry = ResourceRegistry()
    catalog = _build_catalog(tmp_path, registry=registry)

    assert catalog.entries == ()
    assert catalog.catalog_snapshot.available
    assert catalog.catalog_snapshot.revision.startswith("sha256:")


def test_build_workspace_skill_catalog_resolves_gateway_layer(
    tmp_path,
    monkeypatch,
    gateway_identity,
):
    boxteam_home = tmp_path / "boxteam-home"
    gateway_skill = boxteam_home / "skills" / "shared"
    gateway_skill.mkdir(parents=True)
    (gateway_skill / "SKILL.md").write_text(
        "---\nname: shared\ndescription: Gateway skill\n---\n# shared\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))

    catalog = _build_catalog(tmp_path, registry=ResourceRegistry())

    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.name == "shared"
    assert entry.layer == "gateway"
    assert entry.description == "Gateway skill"
    # gateway 的 scope_id 是请求级注入的真实 gateway_id（X-BoxTeam-Gateway-Id），
    # 不再是字面量 "local"，故此处是精确断言而非弱 startswith。
    assert entry.display_uri == (
        f"boxteam://gateway/{FAKE_GATEWAY_ID}/resources/skills/shared/SKILL.md"
    )
    assert "/.boxteam/" not in entry.display_uri
    # 模型可见 metadata 不含 path/locator。
    assert entry.metadata_view() == {
        "name": "shared",
        "description": "Gateway skill",
    }
    # 模型可见挂载已移除:backend 不再暴露 gateway skill 路由。
    backend = build_workspace_backend(tmp_path)
    assert backend.read("/.boxteam/gateway-skills/shared/SKILL.md").error is not None


def test_gateway_scope_uri_passes_binding_with_injected_identity(
    tmp_path,
    monkeypatch,
    gateway_identity,
):
    """带 X-BoxTeam-Gateway-Id 的请求在 gateway scope 解析出真实 gateway_id 并通过绑定校验。

    硬门槛：注入存在时，固定目录 owner 的 scope_id 必须等于注入值
    （而非 `local`），且 `ResolutionContext(gateway_id=...)` 能通过真实的
    ``require_scope_binding``。注入缺失时同一 URI 必然 scope_mismatch。
    """
    boxteam_home = tmp_path / "boxteam-home"
    gateway_skill = boxteam_home / "skills" / "shared"
    gateway_skill.mkdir(parents=True)
    (gateway_skill / "SKILL.md").write_text(
        "---\nname: shared\ndescription: Gateway skill\n---\n# shared\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))

    catalog = _build_catalog(tmp_path, registry=ResourceRegistry())
    entry = catalog.entries[0]
    parsed = parse_resource_uri(entry.display_uri)
    assert parsed.scope == "gateway"
    assert parsed.scope_id == FAKE_GATEWAY_ID
    assert parsed.scope_id != "local"

    # 注入的 principal 与 URI scope_id 一致 → 绑定成立，显式通过校验。
    require_scope_binding(
        parsed.scope,
        parsed.scope_id,
        ResolutionContext(workspace_id="ws-1", gateway_id=FAKE_GATEWAY_ID),
    )
    # 仅有 workspace principal（无 gateway_id）→ 绑定不成立，必然拒绝。
    with pytest.raises(ResourceUriResolveError) as excinfo:
        require_scope_binding(
            parsed.scope, parsed.scope_id, ResolutionContext(workspace_id="ws-1")
        )
    assert excinfo.value.reason_code == "scope_mismatch"


def test_build_workspace_skill_catalog_fails_closed_without_gateway_identity(
    tmp_path,
    monkeypatch,
):
    """缺失 X-BoxTeam-Gateway-Id 的请求在 gateway 层有条目时 fail-closed。

    硬门槛：MUST NOT 回退字面量 `local`，MUST NOT 用进程级单例补齐。
    """
    boxteam_home = tmp_path / "boxteam-home"
    gateway_skill = boxteam_home / "skills" / "shared"
    gateway_skill.mkdir(parents=True)
    (gateway_skill / "SKILL.md").write_text(
        "---\nname: shared\ndescription: Gateway skill\n---\n# shared\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))

    assert get_current_gateway_id() is None
    channel = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path, watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
    ).observation_channel
    remote = accepted_gateway_skill_sources(
        skills_root=boxteam_home / "skills", gateway_id=FAKE_GATEWAY_ID,
        workspace_id=load_or_create_workspace_id(tmp_path), observation_channel=channel,
    )
    with pytest.raises(RuntimeError, match="缺少请求级 Gateway 身份绑定"):
        _build_catalog(tmp_path, registry=ResourceRegistry(), gateway_sources=remote)


def test_gateway_identity_flows_from_request_header_into_catalog(
    tmp_path,
    monkeypatch,
):
    """硬门槛：Gateway 注入的 X-BoxTeam-Gateway-Id 经 TraceMiddleware 进入请求上下文。

    端到端断言请求级注入通道真实连通（而非测试内手工 set_current_gateway_id）：
    未带头 → 端点读到未绑定身份、gateway 层 fail-closed；带真头 → 端点读到该真实
    gateway_id，gateway 层条目解析出同一 scope_id 并通过 require_scope_binding。
    """
    boxteam_home = tmp_path / "boxteam-home"
    gateway_skill = boxteam_home / "skills" / "shared"
    gateway_skill.mkdir(parents=True)
    (gateway_skill / "SKILL.md").write_text(
        "---\nname: shared\ndescription: Gateway skill\n---\n# shared\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))

    file_owner = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path, watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
    )
    remote = accepted_gateway_skill_sources(
        skills_root=boxteam_home / "skills", gateway_id=FAKE_GATEWAY_ID,
        workspace_id=load_or_create_workspace_id(tmp_path), observation_channel=file_owner.observation_channel,
    )
    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    @app.get("/build")
    def _build() -> dict[str, object]:
        bound = get_current_gateway_id()
        try:
            catalog = _build_catalog(tmp_path, registry=ResourceRegistry(), gateway_sources=remote)
        except RuntimeError as error:
            return {"bound": bound, "error": str(error)}
        parsed = parse_resource_uri(catalog.entries[0].display_uri)
        require_scope_binding(
            parsed.scope,
            parsed.scope_id,
            ResolutionContext(workspace_id="ws-1", gateway_id=bound),
        )
        return {"bound": bound, "scope_id": parsed.scope_id}

    client = TestClient(app)

    missing = client.get("/build")
    assert missing.status_code == 200
    assert missing.json()["bound"] is None
    assert "缺少请求级 Gateway 身份绑定" in missing.json()["error"]

    ok = client.get("/build", headers={GATEWAY_ID_HEADER: FAKE_GATEWAY_ID})
    assert ok.status_code == 200
    assert ok.json() == {"bound": FAKE_GATEWAY_ID, "scope_id": FAKE_GATEWAY_ID}
    assert ok.headers["X-Request-ID"]


def test_build_workspace_skill_catalog_prefers_workspace_over_gateway(
    tmp_path,
    monkeypatch,
    gateway_identity,
):
    boxteam_home = tmp_path / "boxteam-home"
    for layer_root, description in (
        (boxteam_home / "skills" / "demo", "Gateway 版本"),
        (tmp_path / ".boxteam" / "skills" / "demo", "Workspace 版本"),
    ):
        layer_root.mkdir(parents=True)
        (layer_root / "SKILL.md").write_text(
            f"---\nname: demo\ndescription: {description}\n---\n# demo\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))

    catalog = _build_catalog(tmp_path, registry=ResourceRegistry())

    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.layer == "workspace"
    assert entry.description == "Workspace 版本"
    assert entry.display_uri.startswith("boxteam://workspace/")


def test_gateway_layer_build_writes_nothing_into_workspace_storage(
    tmp_path,
    monkeypatch,
    gateway_identity,
):
    """Gateway 全局 Skill catalog 解析不写工作区 Session 存储。

    OpenSpec add-context-injection-lifecycle 4.1/6.6：Gateway 负责全局
    catalog 发现，但不得写工作区 Session 状态；workspace bootstrap 身份
    （workspace-identity.json）是允许的工作区初始化副作用，不属于
    Session 存储。
    """
    boxteam_home = tmp_path / "boxteam-home"
    gateway_skill = boxteam_home / "skills" / "shared"
    gateway_skill.mkdir(parents=True)
    (gateway_skill / "SKILL.md").write_text(
        "---\nname: shared\ndescription: Gateway skill\n---\n# shared\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))

    def workspace_storage_tree():
        root = tmp_path / ".boxteam"
        if not root.exists():
            return None
        return {
            str(path.relative_to(root)): (
                path.read_bytes() if path.is_file() else None
            )
            for path in sorted(root.rglob("*"))
        }

    session_storage_paths = (
        tmp_path / ".boxteam" / "sessions",
        tmp_path / ".boxteam" / "navigation",
    )

    def session_storage_snapshot():
        return {
            str(path.relative_to(tmp_path)): (
                path.read_bytes() if path.is_file() else None
            )
            for path in sorted(tmp_path.glob(".boxteam/**/*.sqlite"))
        }

    before_sqlite = session_storage_snapshot()
    catalog = _build_catalog(tmp_path, registry=ResourceRegistry())
    assert len(catalog.entries) == 1
    assert catalog.entries[0].layer == "gateway"

    # catalog 解析读取了 gateway 层，但 Session 存储未被创建或修改。
    for session_path in session_storage_paths:
        assert not session_path.exists()
    assert session_storage_snapshot() == before_sqlite
    # 允许的 bootstrap 副作用只有 workspace 身份文件本身。
    created = workspace_storage_tree()
    assert created is not None
    assert set(created) == {"workspace-identity.json"}
    assert not (tmp_path / ".boxteam" / "sessions").exists()


def test_build_workspace_skill_catalog_uses_inline_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("BOXTEAM_DEFAULT_SKILL_GROUPS", '["gateway-context"]')
    monkeypatch.setenv(
        "BOXTEAM_RUNTIME_MANIFEST", str(_write_runtime_manifest(tmp_path))
    )

    assert resolve_bundled_skill_groups() == ("gateway-context",)
    catalog = _build_catalog(
        tmp_path,
        registry=ResourceRegistry(),
        project_root=Path.cwd(),
    )
    assert [entry.name for entry in catalog.entries] == ["gateway-context"]
    inline_entry = catalog.entries[0]
    assert inline_entry.layer == "inline"
    # scope_id 由 manifest 的 distribution + version 真实推导（非硬编码 local）。
    assert inline_entry.display_uri == (
        "boxteam://inline/source-development-0_0_2/resources/skills/"
        "gateway-context/SKILL.md"
    )
    activation = catalog.source_path(inline_entry.name)
    assert activation is not None and activation.is_file()


def test_build_workspace_skill_catalog_fails_closed_without_manifest(
    tmp_path, monkeypatch
):
    # 缺失 runtime manifest 时 MUST fail-closed，绝不回退 local 虚假默认值。
    monkeypatch.setenv("BOXTEAM_DEFAULT_SKILL_GROUPS", '["gateway-context"]')
    monkeypatch.delenv("BOXTEAM_RUNTIME_MANIFEST", raising=False)

    with pytest.raises(RuntimeError, match="BOXTEAM_RUNTIME_MANIFEST"):
        _build_catalog(
            tmp_path,
            registry=ResourceRegistry(),
            project_root=Path.cwd(),
        )


def test_build_workspace_skill_catalog_rejects_symlink_entry(tmp_path, monkeypatch):
    skills_root = tmp_path / ".boxteam" / "skills"
    skills_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (skills_root / "evil").symlink_to(outside)
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)

    with pytest.raises(RuntimeError, match="symlink") as caught:
        _build_catalog(tmp_path, registry=ResourceRegistry())
    assert str(tmp_path) not in str(caught.value)
    assert "evil" in str(caught.value)


@pytest.mark.parametrize("failure", ["invalid_utf8", "skill_symlink", "directory_io"])
def test_skill_catalog_failure_exposes_logical_source_without_native_locator(
    tmp_path, monkeypatch, failure,
):
    skill_dir = tmp_path / ".boxteam" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    skill.write_bytes(b"\xff")
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)
    if failure == "skill_symlink":
        skill.unlink()
        target = tmp_path / "private-target.md"
        target.write_text("private content", encoding="utf-8")
        skill.symlink_to(target)
    elif failure == "directory_io":
        original_iterdir = Path.iterdir

        def fail_directory(path):
            if path == skill_dir.parent:
                raise PermissionError(13, "private diagnostic", str(path))
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_directory)
    with pytest.raises(RuntimeError) as caught:
        _build_catalog(tmp_path, registry=ResourceRegistry())
    message = str(caught.value)
    assert str(tmp_path) not in message
    if failure == "invalid_utf8":
        assert "invalid_utf8" in message
        assert "/resources/skills/demo/SKILL.md" in message
    elif failure == "skill_symlink":
        assert "symlink" in message and "demo" in message
    else:
        assert "PermissionError" in message and "errno=13" in message


def test_build_workspace_skill_catalog_rejects_name_dir_mismatch(
    tmp_path,
    monkeypatch,
):
    skill_dir = tmp_path / ".boxteam" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other\ndescription: 不匹配\n---\n# body\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)

    with pytest.raises(RuntimeError, match="目录名一致"):
        _build_catalog(tmp_path, registry=ResourceRegistry())


def test_build_workspace_skill_catalog_revision_is_immutable_hash(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)
    skill_dir = tmp_path / ".boxteam" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: v1\n---\n# body\n",
        encoding="utf-8",
    )

    registry = ResourceRegistry()
    file_registry = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path,
        watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
    )
    first = _build_catalog(
        tmp_path, registry=registry, file_registry=file_registry,
    )
    unchanged = _build_catalog(
        tmp_path, registry=registry, file_registry=file_registry,
    )
    assert unchanged.catalog_revision == first.catalog_revision
    assert unchanged.entries[0].metadata_revision == first.entries[0].metadata_revision

    # 只改被忽略的 frontmatter 字段:metadata/activation facet revision 不变,
    # catalog revision 也不变;只有来源 raw revision 变化。
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: v1\nignored: 其它备注\n---\n# body\n",
        encoding="utf-8",
    )
    source_uri = first.entries[0].display_uri
    file_registry.refresh(source_uri)
    ignored_only = _build_catalog(
        tmp_path, registry=registry, file_registry=file_registry,
    )
    assert ignored_only.entries[0].metadata_revision == first.entries[0].metadata_revision
    assert ignored_only.catalog_revision == first.catalog_revision

    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: v2\n---\n# body\n",
        encoding="utf-8",
    )
    file_registry.refresh(source_uri)
    changed = _build_catalog(
        tmp_path, registry=registry, file_registry=file_registry,
    )
    assert changed.entries[0].metadata_revision != first.entries[0].metadata_revision
    assert changed.catalog_revision != first.catalog_revision


def test_custom_tool_spec_parses_independent_skill_attribution():
    spec = parse_custom_tool_spec(
        {
            "name": "test_tool_2",
            "factory": "app.agents.tools.testing:create_test_tool_2",
            "skills": ["demo-skill"],
        }
    )

    assert spec.skills == ("demo-skill",)
    assert spec.to_config()["skills"] == ["demo-skill"]


def test_custom_tool_spec_rejects_invalid_skill_attribution():
    with pytest.raises(ValueError, match="重复 Skill 名"):
        parse_custom_tool_spec(
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
                "skills": ["demo", "demo"],
            }
        )
    with pytest.raises(ValueError, match="格式无效"):
        parse_custom_tool_spec(
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
                "skills": ["Bad_Skill"],
            }
        )
    with pytest.raises(TypeError, match="字符串数组"):
        parse_custom_tool_spec(
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
                "skills": "demo",
            }
        )


def test_workspace_skills_prompt_keeps_custom_tools_out_of_skill_list(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)
    skill_dir = tmp_path / ".boxteam" / "skills" / "test-tool-2"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-tool-2\n"
        "description: Test skill for custom validation.\n"
        "allowed-tools: test_tool_2\n"
        "---\n"
        "# Test\n"
        "调用 `test_tool_2`。\n",
        encoding="utf-8",
    )
    catalog = _build_catalog(tmp_path, registry=ResourceRegistry())
    middleware = WorkspaceSkillsMiddleware(catalog=catalog)

    skill_list = middleware._format_skills_list(
        [entry.metadata_view() for entry in catalog.entries]
    )

    assert "test-tool-2" in skill_list
    assert "/.boxteam/skills/test-tool-2/SKILL.md" not in skill_list
    assert 'skill_load(name="...")' in skill_list
    assert "test_tool_2" not in skill_list
    # allowed_tools 已按 4.2 从 Skill context contract 移除。
    assert all("allowed_tools" not in view for view in [entry.metadata_view() for entry in catalog.entries])


@pytest.mark.asyncio
async def test_custom_tool_invoker_dispatches_configured_tool_without_skill_activation(tmp_path):
    custom_tool = create_test_tool_2(_custom_tool_context(tmp_path))
    invoker = create_extension_tool_invoker_tool(
        [custom_tool],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [custom_tool]
        ),
    )

    result = await invoker.ainvoke(
        {
            "tool_name": "test_tool_2",
            "arguments": {},
        }
    )

    assert invoker.name == EXTENSION_TOOL_INVOKER_NAME
    assert set(invoker.args) == {"tool_name", "arguments"}
    assert result == "4568"


@pytest.mark.asyncio
async def test_custom_tool_invoker_rechecks_effective_execution_policy(tmp_path):
    custom_tool = create_test_tool_2(_custom_tool_context(tmp_path))
    invoker = create_extension_tool_invoker_tool(
        [custom_tool],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [custom_tool]
        ),
        is_tool_execution_enabled=lambda _tool: False,
    )

    # 权限撤销在执行点拒绝，并按 envelope 合同返回真实 paired 错误结果。
    denied_result = await invoker.ainvoke(
        {"tool_name": "test_tool_2", "arguments": {}},
    )
    assert isinstance(denied_result, str)
    assert "已被策略禁用" in denied_result


@pytest.mark.asyncio
async def test_custom_tool_invoker_rejects_unknown_envelope_and_target_arguments(
    tmp_path,
):
    custom_tool = create_test_tool_2(_custom_tool_context(tmp_path))
    invoker = create_extension_tool_invoker_tool(
        [custom_tool],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [custom_tool]
        ),
    )

    result = await invoker.ainvoke(
        {
            "tool_name": "test_tool_2",
            "arguments": {"unexpected": True},
        }
    )
    assert isinstance(result, str)
    assert result == "扩展工具 test_tool_2 包含未知参数: unexpected"

    with pytest.raises(ValidationError):
        invoker.args_schema.model_validate(
            {
                "tool_name": "test_tool_2",
                "arguments": {},
                "session_id": "must-not-be-accepted",
            }
        )


def test_custom_tool_invoker_description_is_fixed_and_contains_no_target_schema():
    from langchain_core.tools import tool

    @tool
    def visible_extension(value: str) -> str:
        """可见扩展工具。"""
        return value

    @tool
    def hidden_extension(secret: str) -> str:
        """隐藏扩展工具。"""
        return secret

    invoker = create_extension_tool_invoker_tool(
        [visible_extension, hidden_extension],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [visible_extension, hidden_extension]
        ),
    )

    assert "visible_extension" not in invoker.description
    assert "可见扩展工具" not in invoker.description
    assert "hidden_extension" not in invoker.description
    assert "隐藏扩展工具" not in invoker.description
    assert set(invoker.args) == {"tool_name", "arguments"}


@pytest.mark.asyncio
async def test_custom_tool_invoker_executes_mcp_style_target_and_validates_schema():
    from langchain_core.tools import tool

    @tool
    def mcp_status(value: str) -> str:
        """MCP 状态查询 stub。"""
        return f"status:{value}"

    mcp_status = mcp_status.model_copy(
        update={"metadata": {"mcp_server_id": "tui-mcp"}}
    )
    invoker = create_extension_tool_invoker_tool(
        [mcp_status],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools([mcp_status]),
    )

    result = await invoker.ainvoke(
        {
            "tool_name": "mcp_status",
            "arguments": {"value": "ready"},
        }
    )

    assert result == "status:ready"


@pytest.mark.asyncio
async def test_custom_tool_invoker_uses_ainvoke_for_mcp_style_base_tool():
    from langchain_core.tools import BaseTool

    class AinvokeOnlyMcpTool(BaseTool):
        name: str = "mcp_async_status"
        description: str = "只暴露 ainvoke 的 MCP stub。"

        def _run(self, value: str) -> str:
            raise AssertionError("该 stub 不应走同步 _run")

        async def ainvoke(self, input, config=None, **kwargs):
            return f"async-status:{input['value']}"

    ainvoke_only = AinvokeOnlyMcpTool()
    invoker = create_extension_tool_invoker_tool(
        [ainvoke_only],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [ainvoke_only]
        ),
    )

    result = await invoker.ainvoke(
        {
            "tool_name": "mcp_async_status",
            "arguments": {"value": "ready"},
        }
    )

    assert result == "async-status:ready"


@pytest.mark.asyncio
async def test_custom_tool_invoker_preserves_container_injected_invocation_context(
    tmp_path,
) -> None:
    from langchain_core.tools import tool

    context = _custom_tool_context(tmp_path)

    @tool
    def context_aware() -> str:
        """读取由统一执行中间件注入到容器的调用 ID。"""
        return context.invocation_context.require_tool_call_id()

    invoker = create_extension_tool_invoker_tool(
        [context_aware],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [context_aware]
        ),
    )
    token = context.invocation_context.set_tool_call_id("call_from_outer_invoker")
    try:
        result = await invoker.ainvoke(
            {
                "tool_name": "context_aware",
                "arguments": {},
            }
        )
    finally:
        context.invocation_context.reset_tool_call_id(token)

    assert result == "call_from_outer_invoker"
