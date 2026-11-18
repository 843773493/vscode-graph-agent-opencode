"""工具发现的真实 MCP 发布目录、结果存储和调用上下文装配。"""

from pathlib import Path

import pytest
from langchain_core.tools import StructuredTool

from app.agents.policy.tool_capability import ToolPolicyResolver
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.tool_discovery import create_search_tools_tool
from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogResourcePublisher
from app.services.infrastructure.mcp.frozen_catalog import (
    restore_frozen_tool_catalog,
)
from app.services.infrastructure.mcp.tool_definitions import (
    SkillToolSource,
    ToolDefinition,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.tool_output_store import ToolOutputStore
from tests.harness.python.run_context import TestRunContext
from tests.support.mcp.session_doubles import MutableSchemaSessionFactory


@pytest.fixture
async def catalog_runtime(catalog_workspace, extension_catalog_policy_resolver, request, monkeypatch):
    factory = MutableSchemaSessionFactory()
    factory.session.description = "查找 CI 失败记录与日志"
    owner = McpCatalogOwner(
        leases=ExternalResourceLeaseLedger(),
        raw_config={"servers": {"mini": {
            "enabled": True, "transport": "streamable_http", "url": "http://127.0.0.1:1/mini/mcp",
        }}},
        workspace_root=catalog_workspace, event_service=EventChannelService(), session_factory=factory,
    )
    calls = []

    def execute(**arguments):
        calls.append(arguments)
        return arguments

    schema = {
        "type": "object", "$defs": {"run": {"enum": ["failed", "passed"]}},
        "properties": {"run": {"$ref": "#/$defs/run"}}, "required": ["run"],
        "additionalProperties": {"type": "string"},
    }
    custom = tuple(StructuredTool.from_function(
        func=execute, name=name, description=description,
        args_schema={**schema, **({"$defs": {"run": {"enum": ["failed", "passed"], "description": "完整大定义" * 6000}}} if name == "huge" else {})},
    ) for name, description in (
        ("ci_logs", "读取 CI 失败日志"), ("ci_runs", "查找 CI 失败执行"),
        ("notes", "读取笔记"), ("huge", "读取大定义"),
        ("hidden_ci", "CI 失败日志硬隐藏"),
    ))
    await owner.start()
    registry = ResourceRegistry(publication_lock=owner.publication_lock, capture_validator=owner.validate_resource_capture)
    scope = LifetimeScope("tool-discovery")
    # 历史读取测试从 publisher 首次登记起生成 v2 资产，避免改写已登记的 descriptor。
    with monkeypatch.context() as historical_writer:
        if getattr(request, "param", None) == "v2":
            from app.services.infrastructure.mcp import catalog_publication
            historical_writer.setattr(catalog_publication, "EXTENSION_CATALOG_SOURCE_ID", "extension-catalog:v2")
        if getattr(request, "param", None) == "four-field-resource":
            historical_writer.setattr(ToolDefinition, "resource_payload", property(lambda definition: definition.contract_payload))
        policies_for = extension_catalog_policy_resolver
        if isinstance(getattr(request, "param", None), dict):
            resolver = ToolPolicyResolver(policy_defaults={"model_visible": False}, policy_rules={}, restrictions=request.param)

            def configured_policies(metadata):
                return {entry.tool_id: resolver.resolve(entry) for entry in metadata}

            policies_for = configured_policies
        skill_sources = (SkillToolSource(
            name="CI Guide", source_id="workspace-skills:v1", entry_identity="workspace-skill:ci-guide:v1",
            display_uri="boxteam://workspace/test/resources/skills/CI%20Guide/SKILL.md",
        ),) if getattr(request, "param", None) == "with-skill-association" else ()
        publisher = McpCatalogResourcePublisher(
            registry=registry, custom_tools=custom, policy_resolver=policies_for,
            skill_sources=skill_sources,
            custom_skill_bindings={name: ("CI Guide",) for name in ("ci_logs", "huge")} if skill_sources else {},
        )
        scope.register(owner.subscribe_publication(publisher.publish), label="discovery-publication")
    try:
        yield owner, factory, registry, scope
        assert calls == [], "发现不得执行候选"
    finally:
        await scope.close()
        await owner.shutdown()


@pytest.fixture
def discovery_store(request: pytest.FixtureRequest):
    # 与 registry_db 的真实会话使用同一测试工作区，避免大结果物化访问另一份 catalog。
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    return ToolOutputStore(workspace_root=context.workspace_root)


@pytest.fixture
async def discovery_tool_factory(discovery_store, registry_db):
    _saver, session, _connection, _accepted = registry_db
    invocation_context = ToolInvocationContext()
    token = invocation_context.set_tool_call_id("discovery-fixture")

    def build(*, catalog_resolver, policy_resolver, response_byte_limit=None):
        return create_search_tools_tool(
            catalog_resolver=catalog_resolver, policy_resolver=policy_resolver,
            tool_output_store=discovery_store, session_id=session,
            invocation_context=invocation_context,
            response_byte_limit=discovery_store.inline_byte_limit if response_byte_limit is None else response_byte_limit,
        )

    try:
        yield build
    finally:
        invocation_context.reset_tool_call_id(token)


@pytest.fixture
def discovery_case(prepare_tool_context, registry_db, discovery_tool_factory):
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    _wire, frozen = prepare_tool_context("discovery:initial", boundary="turn")

    def read(snapshot, binding):
        return saver.read_resource_activation_payload(snapshot, binding, storage_owner=(session, thread))

    catalog = restore_frozen_tool_catalog(frozen, read)
    policy_resolver = ToolPolicyResolver(
        policy_defaults={"model_visible": False}, policy_rules={},
        restrictions={"execution_disabled": ["notes"], "model_hidden": ["hidden_ci"]},
    )
    policies = {definition.metadata.tool_id: policy_resolver.resolve(definition.metadata) for definition in catalog.definitions}
    tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda _: policies)
    return tool, catalog, policies, frozen, read
