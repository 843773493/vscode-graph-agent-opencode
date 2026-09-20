from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.agents.custom_tools import CustomToolFactoryContext
from app.agents.policy.custom_tool_spec import parse_custom_tool_spec
from app.agents.skill_runtime import (
    WORKSPACE_AGENTS_URI,
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
from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)


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
        terminal_manager_client=MagicMock(),
        browser_manager_client=MagicMock(),
        invocation_context=ToolInvocationContext(),
    )


def test_build_workspace_skill_catalog_empty_layers_publishes_empty_revision(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)

    registry = ResourceRegistry()
    catalog = build_workspace_skill_catalog(tmp_path, registry=registry)

    assert catalog.entries == ()
    assert catalog.catalog_snapshot.available
    assert catalog.catalog_snapshot.revision.startswith("sha256:")


def test_build_workspace_skill_catalog_resolves_gateway_layer(tmp_path, monkeypatch):
    boxteam_home = tmp_path / "boxteam-home"
    gateway_skill = boxteam_home / "skills" / "shared"
    gateway_skill.mkdir(parents=True)
    (gateway_skill / "SKILL.md").write_text(
        "---\nname: shared\ndescription: Gateway skill\n---\n# shared\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))

    catalog = build_workspace_skill_catalog(tmp_path, registry=ResourceRegistry())

    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.name == "shared"
    assert entry.layer == "gateway"
    assert entry.description == "Gateway skill"
    assert entry.display_uri.startswith("boxteam://gateway/")
    assert "/.boxteam/" not in entry.display_uri
    # 模型可见 metadata 不含 path/locator。
    assert entry.metadata_view() == {
        "name": "shared",
        "description": "Gateway skill",
        "display_uri": entry.display_uri,
    }
    # 模型可见挂载已移除:backend 不再暴露 gateway skill 路由。
    backend = build_workspace_backend(tmp_path)
    assert backend.read("/.boxteam/gateway-skills/shared/SKILL.md").error is not None


def test_build_workspace_skill_catalog_prefers_workspace_over_gateway(
    tmp_path,
    monkeypatch,
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

    catalog = build_workspace_skill_catalog(tmp_path, registry=ResourceRegistry())

    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.layer == "workspace"
    assert entry.description == "Workspace 版本"
    assert entry.display_uri.startswith("boxteam://workspace/")


def test_gateway_layer_build_writes_nothing_into_workspace_storage(
    tmp_path,
    monkeypatch,
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
    catalog = build_workspace_skill_catalog(tmp_path, registry=ResourceRegistry())
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


def test_build_workspace_skill_catalog_uses_bundled_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("BOXTEAM_DEFAULT_SKILL_GROUPS", '["gateway-context"]')

    assert resolve_bundled_skill_groups() == ("gateway-context",)
    catalog = build_workspace_skill_catalog(
        tmp_path,
        registry=ResourceRegistry(),
        project_root=Path.cwd(),
    )
    assert [entry.name for entry in catalog.entries] == ["gateway-context"]
    bundled_entry = catalog.entries[0]
    assert bundled_entry.layer == "bundled"
    assert bundled_entry.display_uri.startswith("boxteam://builtin/")
    activation = catalog.source_path(bundled_entry.name)
    assert activation is not None and activation.is_file()


def test_build_workspace_skill_catalog_rejects_symlink_entry(tmp_path, monkeypatch):
    skills_root = tmp_path / ".boxteam" / "skills"
    skills_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (skills_root / "evil").symlink_to(outside)
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)

    with pytest.raises(RuntimeError, match="symlink"):
        build_workspace_skill_catalog(tmp_path, registry=ResourceRegistry())


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
        build_workspace_skill_catalog(tmp_path, registry=ResourceRegistry())


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
    first = build_workspace_skill_catalog(tmp_path, registry=registry)
    unchanged = build_workspace_skill_catalog(tmp_path, registry=registry)
    assert unchanged.catalog_revision == first.catalog_revision
    assert unchanged.entries[0].metadata_revision == first.entries[0].metadata_revision

    # 只改被忽略的 frontmatter 字段:metadata/activation facet revision 不变,
    # catalog revision 也不变;只有来源 raw revision 变化。
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: v1\nignored: 其它备注\n---\n# body\n",
        encoding="utf-8",
    )
    ignored_only = build_workspace_skill_catalog(tmp_path, registry=registry)
    assert ignored_only.entries[0].metadata_revision == first.entries[0].metadata_revision
    assert ignored_only.catalog_revision == first.catalog_revision

    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: v2\n---\n# body\n",
        encoding="utf-8",
    )
    changed = build_workspace_skill_catalog(tmp_path, registry=registry)
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
    catalog = build_workspace_skill_catalog(tmp_path, registry=ResourceRegistry())
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


def _workspace_agents_stack(tmp_path, monkeypatch):
    # 构建唯一 CSM 消费链：registry + CSM + reactor + 统一中间件。
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("BOXTEAM_DEFAULT_SKILL_GROUPS", raising=False)
    watch_service = WorkspaceFileWatchService(workspace_root=tmp_path)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path,
        watch_service=watch_service,
    )
    manager = ContextSourceManager()
    lifetime_scope = LifetimeScope("d3b-test")
    reactor = ContextSourceReactor(
        sources=registry,
        context_sources=manager,
        lifetime_scope=lifetime_scope,
        reactor_id="d3b-test",
    )
    catalog = build_workspace_skill_catalog(tmp_path, registry=ResourceRegistry())
    middleware = WorkspaceSkillsMiddleware(
        catalog=catalog,
        context_source_manager=manager,
        source_registry=registry,
        context_source_reactor=reactor,
    )
    return middleware, registry, manager, reactor


def test_workspace_agents_first_activation_flows_through_unique_csm_chain(
    tmp_path, monkeypatch
):
    (tmp_path / "AGENTS.md").write_text("# 指令\n\n使用新规则。\n", encoding="utf-8")
    middleware, _registry, manager, _reactor = _workspace_agents_stack(
        tmp_path, monkeypatch
    )

    middleware.before_agent({}, MagicMock(), None)
    update = middleware.before_model({}, MagicMock())

    assert update is not None
    messages = update["messages"]
    assert len(messages) == 1
    message = messages[0]
    assert "使用新规则。" in message.text
    assert message.response_metadata["context_source_id"] == "agents:workspace"
    assert message.response_metadata["context_source_kind"] == "workspace_agents"
    assert message.response_metadata["context_wire_role"] == "user"
    # 唯一消费链：提交后 pending 清空，不再重复注入。
    assert manager.pending_observation_count() == 0
    assert middleware.before_model({}, MagicMock()) is None


def test_workspace_agents_change_is_typed_observation_not_direct_injection(
    tmp_path, monkeypatch
):
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("# 指令\n\n使用旧规则。\n", encoding="utf-8")
    middleware, registry, manager, reactor = _workspace_agents_stack(
        tmp_path, monkeypatch
    )
    middleware.before_agent({}, MagicMock(), None)
    assert middleware.before_model({}, MagicMock()) is not None

    agents_path.write_text("# 指令\n\n使用新规则。\n", encoding="utf-8")
    registry.refresh(WORKSPACE_AGENTS_URI)

    # 来源变化只形成 typed observation；正文经权威内存快照进入 CSM pending。
    observations = list(reactor.drain())
    assert [o.source_id for o in observations] == ["agents:workspace"]
    for observation in observations:
        manager.observe(
            observation.source_id,
            reactor.content_for(observation),
            revision=observation.revision,
        )
    batch = manager.prepare_pending()
    assert batch is not None
    delta = batch.deltas[0]
    assert delta.source_kind == "workspace_agents"
    assert delta.kind == "delta"
    assert "+使用新规则。" in delta.content
    assert "-使用旧规则。" in delta.content

    # 中间件消费同一 pending，产出带 provenance 的上下文 item。
    update = middleware.before_model({}, MagicMock())
    assert update is not None
    message = update["messages"][0]
    assert "+使用新规则。" in message.text
    assert message.response_metadata["context_source_id"] == "agents:workspace"
    assert middleware.before_model({}, MagicMock()) is None


def test_workspace_agents_request_boundary_avoids_file_reads_and_legacy_chain(
    tmp_path, monkeypatch
):
    (tmp_path / "AGENTS.md").write_text("# 指令\n\n使用规则。\n", encoding="utf-8")
    middleware, registry, _manager, _reactor = _workspace_agents_stack(
        tmp_path, monkeypatch
    )
    middleware.before_agent({}, MagicMock(), None)
    assert middleware.before_model({}, MagicMock()) is not None

    import app.agents.skill_runtime as skill_runtime_module

    # 旧注入入口已物理删除。
    assert not hasattr(skill_runtime_module, "WorkspaceAgentsMiddleware")
    assert not hasattr(skill_runtime_module, "WorkspaceAgentsState")

    # 请求边界零文件读取：无 pending 时 before_model 不触碰 registry 快照。
    snapshot_calls = []
    original_snapshot = registry.snapshot

    def _counting_snapshot(uri):
        snapshot_calls.append(uri)
        return original_snapshot(uri)

    monkeypatch.setattr(registry, "snapshot", _counting_snapshot)
    assert middleware.before_model({}, MagicMock()) is None
    assert snapshot_calls == []

    # AGENTS 内容不再进入 system prompt：modify_request 只处理 skills metadata。
    request = MagicMock()
    request.state = {"skills_metadata": []}
    request.system_message = None
    request.override.side_effect = lambda **kwargs: kwargs
    assert middleware.modify_request(request) is request


def test_workspace_agents_source_late_appearance_still_uses_csm_chain(
    tmp_path, monkeypatch
):
    middleware, registry, _manager, _reactor = _workspace_agents_stack(
        tmp_path, monkeypatch
    )
    middleware.before_agent({}, MagicMock(), None)
    # AGENTS.md 尚不存在：无首帧注入。
    assert middleware.before_model({}, MagicMock()) is None

    (tmp_path / "AGENTS.md").write_text("# 迟到指令\n", encoding="utf-8")
    registry.refresh(WORKSPACE_AGENTS_URI)
    update = middleware.before_model({}, MagicMock())
    assert update is not None
    message = update["messages"][0]
    assert "迟到指令" in message.text
    assert message.response_metadata["context_source_kind"] == "workspace_agents"
