from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.tools import StructuredTool

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.policy.tool_capability import metadata_for_tool
from app.agents.policy.tool_groups import DEBUGGING_TOOL_NAMES
from app.agents.tools.extension_invocation import (
    create_extension_catalog_resolver_from_tools,
)
from app.services.business.agent_tool_settings.catalog import AgentToolCatalog
from app.services.infrastructure.config import build_config_snapshot
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.mcp.catalog_owner import McpCatalogPublication
from app.services.infrastructure.mcp.tool_definitions import definition_from_tool


@pytest.fixture
def catalog_case(tmp_path: Path, empty_workspace_scope):
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(json.dumps({"agents": {"default": {"tools": {"custom": [
        {"tool_id": "test_tool_2", "description": "配置中的说明"},
        {"tool_id": "read_context"},
        *({"name": name, "factory": "app.agents.tools.debugging:create_debugging_tool",
           "options": {"tool_name": name}} for name in sorted(DEBUGGING_TOOL_NAMES)),
    ]}}}}), encoding="utf-8")
    config = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    config.get_snapshot()
    publication = McpCatalogPublication(
        binding_ref=create_extension_catalog_resolver_from_tools([])(),
        descriptors=(), definitions={}, tools={}, status="available",
    )
    dependencies = MagicMock()
    catalog = AgentToolCatalog(
        config_service=config, dependency_provider=dependencies,
        background_task_registry=MagicMock(), background_mailbox=MagicMock(),
        job_event_bus=MagicMock(), workspace_root=empty_workspace_scope[1],
    )
    case = SimpleNamespace(catalog=catalog, config=config, publication=publication, dependencies=dependencies)
    dependencies.get_mcp_catalog_owner.return_value.publication_snapshot.side_effect = lambda: case.publication
    return case


def test_current_definitions_include_real_custom_schema_and_policy_identity(catalog_case):
    snapshot, definitions = catalog_case.catalog.list_tool_definitions()
    by_id = {item["id"]: item for item in definitions}
    assert snapshot is catalog_case.config.get_active_snapshot()
    assert {"read_file", "write_todos", "compact_conversation", "read_compaction_history", "skill_load", "search_tools"} <= by_id.keys()
    assert "invoke_extension_tool" not in by_id
    custom = by_id["test_tool_2"]
    assert custom["origin"] == "custom"
    assert custom["description"] == "配置中的说明"
    assert custom["group_id"] == "extension:testing"
    assert by_id["read_context"]["group_id"] == "agent-collaboration"
    assert by_id["read_context"]["parameters"]["properties"]
    for name in DEBUGGING_TOOL_NAMES:
        definition = by_id[name]
        assert definition["origin"] == "custom"
        assert definition["group_id"] == "debugging"
        assert definition["parameters"]["additionalProperties"] is False
    assert set(by_id["start_debugging"]["parameters"]["properties"]) == {
        "fileFullPath", "workingDirectory", "testName", "configurationName", "debugConfigurationId",
    }
    catalog_case.dependencies.get_checkpointer.assert_not_called()
    catalog_case.dependencies.get_session_service.assert_not_called()


def test_warm_read_isolated_and_invalidated_by_published_config(catalog_case, monkeypatch):
    catalog, config = catalog_case.catalog, catalog_case.config
    original = catalog._build_definitions
    builds = []

    def build(agent, publication):
        builds.append(agent)
        return original(agent, publication)

    monkeypatch.setattr(catalog, "_build_definitions", build)
    old, first = catalog.list_tool_definitions()
    first[0]["parameters"]["properties"]["corrupted"] = {}
    _, second = catalog.list_tool_definitions()
    assert "corrupted" not in second[0]["parameters"]["properties"]
    assert builds == ["default"]
    payload = old.to_dict()
    payload["agents"]["default"]["tools"]["custom"][0]["description"] = "已更新"
    current = build_config_snapshot(payload, source_paths=())
    with config.publication_lock:
        config._snapshot_store.initialize(current)
    # 当前管理目录不能把外层旧 pinned 配置与当前 MCP publication 拼接。
    with config.use_snapshot(old):
        observed, third = catalog.list_tool_definitions()
    assert observed is current
    assert next(item for item in third if item["id"] == "test_tool_2")["description"] == "已更新"
    assert builds == ["default", "default"]
    assert len(catalog._cache) == 1


def test_publication_can_advance_while_factory_is_running(catalog_case, monkeypatch):
    entered, finish = Event(), Event()
    catalog, config, publication = catalog_case.catalog, catalog_case.config, catalog_case.publication
    original = catalog._build_definitions
    pairs = []

    def build(agent, captured):
        pairs.append((config.get_snapshot().revision, captured.binding_ref.generation))
        entered.set()
        assert finish.wait(5)
        return original(agent, captured)

    monkeypatch.setattr(catalog, "_build_definitions", build)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(copy_context().run, catalog.list_tool_definitions)
        assert entered.wait(5)
        try:
            with config.publication_lock:
                payload = config.get_active_snapshot().to_dict()
                payload["agents"]["default"]["instructions"]["system_prompt"] = "new"
                current = build_config_snapshot(payload, source_paths=())
                config._snapshot_store.initialize(current)
                catalog_case.publication = replace(publication, binding_ref=replace(publication.binding_ref, generation=publication.binding_ref.generation + 1))
        finally:
            finish.set()
        old, _ = result.result(5)
    catalog.list_tool_definitions()
    assert pairs[0][0] == old.revision
    assert pairs[1] == (current.revision, catalog_case.publication.binding_ref.generation)


def test_mcp_uses_published_schema_and_current_readiness(catalog_case, monkeypatch):
    target = StructuredTool(name="mcp__example__query", description="查询", args_schema={
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }, metadata={"mcp_server_id": "example"})
    definition = definition_from_tool(target, metadata_for_tool(target, origin="mcp"))
    publication = catalog_case.publication
    catalog_case.publication = replace(publication, definitions={target.name: definition})
    _, definitions = catalog_case.catalog.list_tool_definitions()
    item = next(item for item in definitions if item["id"] == target.name)
    assert item["parameters"] == export_model_tool_json_schema(target)
    assert item["origin"] == "mcp"
    assert item["availability"] == "ready"
    monkeypatch.setattr(catalog_case.catalog, "_build_definitions", lambda *_: pytest.fail("状态变化不重建 schema"))
    catalog_case.publication = replace(catalog_case.publication, definitions={target.name: replace(definition, readiness="disconnected")})
    _, definitions = catalog_case.catalog.list_tool_definitions()
    assert next(item for item in definitions if item["id"] == target.name)["availability"] == "disconnected"


@pytest.mark.parametrize("result", ["invalid", StructuredTool(name="different", description="错误名称", args_schema={"type": "object"})])
def test_invalid_factory_fails_without_models_or_success_cache(catalog_case, monkeypatch, result):
    monkeypatch.setattr("app.agents.custom_tools.load_custom_tool_factory", lambda _: lambda context: result)
    with pytest.raises((TypeError, ValueError), match="test_tool_2"):
        catalog_case.catalog.list_tool_definitions()
    assert not catalog_case.catalog._cache


def test_duplicate_registration_is_not_overwritten(catalog_case, monkeypatch):
    tool = StructuredTool(name="same", description="重复工具", args_schema={"type": "object"})
    monkeypatch.setattr("app.agents.default_tools.build_default_tools", lambda **_: [tool, tool])
    with pytest.raises(RuntimeError, match="命名冲突"):
        catalog_case.catalog.list_tool_definitions()
