from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.agents.policy import ToolPolicyResolver
from app.api.tools import get_tool, list_tools, update_tool_selection
from app.main import app
from app.schemas.internal_v2.tool import (
    ToolSelectionChange,
    ToolSelectionPatchRequest,
)
from app.services.business.agent_tool_settings.service import (
    AgentToolSettingsService,
    ToolNotFoundError,
    ToolSelectionError,
)
from app.services.infrastructure.config import ConfigSnapshot, build_config_snapshot
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


class _ToolCatalogStub:
    def __init__(self) -> None:
        self.agent_ids: list[str | None] = []

    def list_tool_definitions(self, agent_id: str | None = None) -> tuple[ConfigSnapshot, list[dict]]:
        self.agent_ids.append(agent_id)
        return build_config_snapshot({}, source_paths=()), [
            {
                "id": "read_file",
                "name": "read_file",
                "description": "读取文件",
                "parameters": {"type": "object"},
            },
            {
                "id": "write_file",
                "name": "write_file",
                "description": "写入文件",
                "parameters": {"type": "object"},
            },
        ]


class _DependencyToolCatalogStub(_ToolCatalogStub):
    def list_tool_definitions(self, agent_id: str | None = None) -> tuple[ConfigSnapshot, list[dict]]:
        snapshot, tools = super().list_tool_definitions(agent_id)
        tools.extend(
            {
                "id": tool_id,
                "name": tool_id,
                "description": tool_id,
                "parameters": {"type": "object"},
            }
            for tool_id in (
                "send_message_to_session",
                "task",
                "create_team_member",
            )
        )
        return snapshot, tools


class _DebuggingToolCatalogStub(_ToolCatalogStub):
    def list_tool_definitions(self, agent_id: str | None = None) -> tuple[ConfigSnapshot, list[dict]]:
        snapshot, tools = super().list_tool_definitions(agent_id)
        tools.append(
            {
                "id": "start_debugging",
                "name": "start_debugging",
                "description": "启动调试",
                "parameters": {"type": "object"},
                "group_id": "debugging",
                "group_name": "扩展工具 · Source Debugging",
                "kind": "debugging",
            }
        )
        return snapshot, tools


def _service(
    tmp_path: Path,
    *,
    tool_catalog: _ToolCatalogStub | None = None,
    default_agent_id: str = "default",
) -> AgentToolSettingsService:
    config_service = MagicMock()
    config_service.get_active_snapshot.return_value = build_config_snapshot({}, source_paths=())
    config_service.validate_agent_id.side_effect = (
        lambda agent_id: default_agent_id if agent_id is None else agent_id
    )
    config_service.get_tool_policy_resolver.return_value = ToolPolicyResolver(
        policy_defaults={
            "execution_enabled": True,
            "model_visible": True,
            "confirmation_required": False,
        },
        policy_rules={
            "by_origin": {},
            "by_kind": {"debugging": {"model_visible": False}},
            "by_group": {},
            "by_tool": {},
        },
        restrictions={},
    )
    return AgentToolSettingsService(
        tool_catalog=tool_catalog or _ToolCatalogStub(),
        selection_store=ToolSelectionStore(boxteam_root=tmp_path / ".boxteam"),
        config_service=config_service,
        test_supported_tools={"read_file"},
    )


@pytest.fixture
def published_tools_case(tmp_path: Path, empty_workspace_scope):
    config = ConfigService(config_dir=Path.cwd() / "configs", config_path=tmp_path / "workspace.jsonc")
    payload = config.get_snapshot().to_dict()
    payload["agents"]["coder"] = deepcopy(payload["agents"]["default"])
    old = build_config_snapshot(payload, source_paths=())
    payload["default_agent"] = "coder"
    current = build_config_snapshot(payload, source_paths=())
    with config.publication_lock:
        config._snapshot_store.initialize(current)

    class PublishedCatalog(_ToolCatalogStub):
        def list_tool_definitions(self, agent_id: str | None = None) -> tuple[ConfigSnapshot, list[dict]]:
            _, definitions = super().list_tool_definitions(agent_id)
            return config.get_active_snapshot(), definitions

    catalog = PublishedCatalog()
    store = ToolSelectionStore(boxteam_root=empty_workspace_scope[1] / ".boxteam")
    service = AgentToolSettingsService(tool_catalog=catalog, config_service=config,
        selection_store=store, test_supported_tools=set())
    return SimpleNamespace(service=service, config=config, store=store, catalog=catalog, old=old, current=current)


async def test_missing_tool_uses_the_catalog_agent_not_outer_pinned_config(published_tools_case):
    case = published_tools_case
    with case.config.use_snapshot(case.old), pytest.raises(ToolNotFoundError, match="Agent 'coder' 不存在工具 'missing'"):
        await case.service.get("missing")


async def test_selection_resolves_default_from_catalog_snapshot(published_tools_case):
    case = published_tools_case
    with case.config.use_snapshot(case.old):
        changed = await case.service.update_selection(ToolSelectionPatchRequest(changes=[
            ToolSelectionChange(tool_id="write_file", execution_enabled=False, model_visible=False),
        ]))
    assert [(tool.tool_id, tool.execution_enabled) for tool in changed] == [("write_file", False)]
    assert case.store.snapshot("coder").execution_overrides == {"write_file": False}
    assert case.store.snapshot("default").execution_overrides == {}


async def test_selection_rejects_config_change_before_commit_without_writing(published_tools_case, monkeypatch):
    case = published_tools_case
    payload = case.current.to_dict()
    payload["tooling"]["restrictions"]["execution_disabled"] = ["write_file"]
    newer = build_config_snapshot(payload, source_paths=())
    original = case.store.snapshot

    def read_and_publish(agent_id):
        selection = original(agent_id)
        with case.config.publication_lock:
            case.config._snapshot_store.initialize(newer)
        return selection

    monkeypatch.setattr(case.store, "snapshot", read_and_publish)
    with pytest.raises(ToolSelectionError, match="配置已更新"):
        await case.service.update_selection(ToolSelectionPatchRequest(agent_id="coder", changes=[
            ToolSelectionChange(tool_id="write_file", execution_enabled=True, model_visible=True),
        ]))
    assert original("coder").execution_overrides == {}


async def test_source_debugging_is_model_hidden_by_default(tmp_path: Path) -> None:
    service = _service(tmp_path, tool_catalog=_DebuggingToolCatalogStub())

    debugging_tool = await service.get("start_debugging")

    assert debugging_tool.execution_enabled is True
    assert debugging_tool.model_visible is False

    changed = await service.update_selection(
        ToolSelectionPatchRequest(
            changes=[
                ToolSelectionChange(
                    tool_id="start_debugging",
                    execution_enabled=True,
                    model_visible=True,
                )
            ]
        )
    )

    assert [(tool.tool_id, tool.model_visible) for tool in changed] == [
        ("start_debugging", True)
    ]


async def test_get_unknown_tool_raises_domain_error(tmp_path: Path) -> None:
    with pytest.raises(
        ToolNotFoundError,
        match="Agent 'default' 不存在工具 'missing'",
    ):
        await _service(tmp_path).get("missing")


async def test_get_unknown_tool_maps_to_http_404(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as error:
        await get_tool(
            tool_id="missing",
            agent_id="default",
            _="local-dev-token",
            request_id="req_get_missing",
            agent_tool_settings_service=_service(tmp_path),
        )

    assert error.value.status_code == 404
    assert "不存在工具" in error.value.detail


async def test_update_selection_rejects_unknown_tool_without_writing(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    with pytest.raises(ToolSelectionError, match="后端不支持的工具: missing"):
        await service.update_selection(
            ToolSelectionPatchRequest(
                changes=[
                    ToolSelectionChange(
                        tool_id="missing",
                        execution_enabled=False,
                        model_visible=False,
                    )
                ]
            )
        )

    assert not (tmp_path / ".boxteam" / "settings" / "tool_selection.json").exists()


async def test_unknown_tool_selection_maps_to_http_400(tmp_path: Path) -> None:
    request = ToolSelectionPatchRequest(
        changes=[
            ToolSelectionChange(
                tool_id="missing",
                execution_enabled=False,
                model_visible=False,
            )
        ]
    )

    with pytest.raises(HTTPException) as error:
        await update_tool_selection(
            payload=request,
            _="local-dev-token",
            request_id="req_select_missing",
            agent_tool_settings_service=_service(tmp_path),
        )

    assert error.value.status_code == 400
    assert "后端不支持的工具" in error.value.detail


async def test_update_selection_persists_and_reports_changed_tool(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    changed = await service.update_selection(
        ToolSelectionPatchRequest(
            changes=[
                ToolSelectionChange(
                    tool_id="write_file",
                    execution_enabled=False,
                    model_visible=False,
                )
            ]
        )
    )

    assert [
        (tool.tool_id, tool.execution_enabled, tool.model_visible)
        for tool in changed
    ] == [
        ("write_file", False, False)
    ]
    assert {
        tool.tool_id: (tool.execution_enabled, tool.model_visible)
        for tool in await service.list()
    } == {
        "read_file": (True, True),
        "write_file": (False, False),
    }


async def test_omitted_agent_id_uses_catalog_default_for_selection(
    tmp_path: Path,
) -> None:
    catalog = _ToolCatalogStub()
    service = _service(
        tmp_path,
        tool_catalog=catalog,
        default_agent_id="coder",
    )

    changed = await service.update_selection(
        ToolSelectionPatchRequest(
            changes=[
                ToolSelectionChange(
                    tool_id="write_file",
                    execution_enabled=False,
                    model_visible=False,
                )
            ]
        )
    )

    assert [(tool.tool_id, tool.execution_enabled) for tool in changed] == [
        ("write_file", False)
    ]
    assert catalog.agent_ids == [None]
    assert service._config_service.validate_agent_id.call_args_list[0].args == (None,)
    assert service._selection_store.snapshot("coder").execution_overrides == {
        "write_file": False
    }
    assert service._selection_store.snapshot("default").execution_overrides == {}


async def test_tool_list_api_passes_omitted_agent_to_default_resolution(
    tmp_path: Path,
) -> None:
    catalog = _ToolCatalogStub()
    service = _service(
        tmp_path,
        tool_catalog=catalog,
        default_agent_id="coder",
    )

    response = await list_tools(
        _="local-dev-token",
        request_id="req_tools_default_agent",
        agent_tool_settings_service=service,
    )

    assert [tool.tool_id for tool in response.data] == ["read_file", "write_file"]
    assert catalog.agent_ids == [None]  # 默认 Agent 由捕获配置的 catalog 解析。


async def test_update_selection_maps_unknown_agent_to_http_400(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    def validate_agent_id(agent_id: str | None) -> str:
        if agent_id == "missing":
            raise ValueError(f"agent {agent_id} 不存在")
        return "default" if agent_id is None else agent_id

    service._config_service.validate_agent_id.side_effect = validate_agent_id
    request = ToolSelectionPatchRequest(
        agent_id="missing",
        changes=[
            ToolSelectionChange(
                tool_id="write_file",
                execution_enabled=False,
                model_visible=False,
            )
        ],
    )

    with pytest.raises(HTTPException) as error:
        await update_tool_selection(
            payload=request,
            _="local-dev-token",
            request_id="req_selection_missing_agent",
            agent_tool_settings_service=service,
        )

    assert error.value.status_code == 400
    assert "agent missing 不存在" in error.value.detail
    assert not (
        tmp_path / ".boxteam" / "settings" / "tool_selection.json"
    ).exists()


async def test_update_selection_rejects_dependency_conflict_atomically(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        tool_catalog=_DependencyToolCatalogStub(),
    )

    with pytest.raises(
        ToolSelectionError,
        match="create_team_member, task.*依赖 send_message_to_session",
    ):
        await service.update_selection(
            ToolSelectionPatchRequest(
                changes=[
                    ToolSelectionChange(
                        tool_id="send_message_to_session",
                        execution_enabled=False,
                        model_visible=False,
                    )
                ]
            )
        )

    assert not (tmp_path / ".boxteam" / "settings" / "tool_selection.json").exists()


async def test_update_selection_accepts_consistent_dependency_changes(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        tool_catalog=_DependencyToolCatalogStub(),
    )

    changed = await service.update_selection(
        ToolSelectionPatchRequest(
            changes=[
                ToolSelectionChange(
                    tool_id="send_message_to_session",
                    execution_enabled=False,
                    model_visible=False,
                ),
                ToolSelectionChange(
                    tool_id="task",
                    execution_enabled=False,
                    model_visible=False,
                ),
                ToolSelectionChange(
                    tool_id="create_team_member",
                    execution_enabled=False,
                    model_visible=False,
                ),
            ]
        )
    )

    assert {
        tool.tool_id for tool in changed if not tool.execution_enabled
    } == {
        "send_message_to_session",
        "task",
        "create_team_member",
    }


def test_public_api_does_not_expose_context_free_tool_invoke() -> None:
    invoke_paths = {
        route.path
        for route in app.routes
        if "POST" in getattr(route, "methods", set())
        and route.path.endswith("/invoke")
    }

    assert invoke_paths == set()
    assert not any(
        path.endswith("/invoke") for path in app.openapi()["paths"]
    )


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_cold_list_does_not_block_or_overlap_catalog_builds(
    tmp_path: Path, cancel_first: bool,
) -> None:
    started, release = Event(), Event()
    calls: list[str] = []

    class BlockingCatalog(_ToolCatalogStub):
        def list_tool_definitions(self, agent_id: str | None = None) -> tuple[ConfigSnapshot, list[dict]]:
            calls.append(agent_id)
            if len(calls) == 1:
                started.set()
                assert release.wait(5), "事件循环应能继续运行并释放构建"
            return super().list_tool_definitions(agent_id)

    service = _service(tmp_path, tool_catalog=BlockingCatalog())
    first = asyncio.create_task(service.list("default"))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        if cancel_first:
            first.cancel()
        second = asyncio.create_task(service.list("coder"))
        await asyncio.sleep(0)
        assert calls == ["default"]
    finally:
        release.set()
        if cancel_first:
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            assert len(await first) == 2
        if second is not None:
            assert len(await second) == 2
    assert calls == ["default", "coder"]
