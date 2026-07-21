from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.tools import get_tool, update_tool_selection
from app.main import app
from app.schemas.public_v2.tool import (
    ToolSelectionChange,
    ToolSelectionPatchRequest,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.infrastructure.tool_service import (
    ToolNotFoundError,
    ToolSelectionError,
    ToolService,
)


class _ToolCatalogStub:
    def get_available_tools(self, agent_id: str = "default") -> list[dict]:
        return [
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
    def get_available_tools(self, agent_id: str = "default") -> list[dict]:
        tools = super().get_available_tools(agent_id)
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
        return tools


def _service(
    tmp_path: Path,
    *,
    tool_catalog: _ToolCatalogStub | None = None,
) -> ToolService:
    return ToolService(
        tool_catalog=tool_catalog or _ToolCatalogStub(),
        selection_store=ToolSelectionStore(boxteam_root=tmp_path / ".boxteam"),
        test_supported_tools={"read_file"},
    )


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
            tool_service=_service(tmp_path),
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
                    ToolSelectionChange(tool_id="missing", enabled=False)
                ]
            )
        )

    assert not (tmp_path / ".boxteam" / "settings" / "tool_selection.json").exists()


async def test_unknown_tool_selection_maps_to_http_400(tmp_path: Path) -> None:
    request = ToolSelectionPatchRequest(
        changes=[ToolSelectionChange(tool_id="missing", enabled=False)]
    )

    with pytest.raises(HTTPException) as error:
        await update_tool_selection(
            payload=request,
            _="local-dev-token",
            request_id="req_select_missing",
            tool_service=_service(tmp_path),
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
                ToolSelectionChange(tool_id="write_file", enabled=False)
            ]
        )
    )

    assert [(tool.tool_id, tool.enabled) for tool in changed] == [
        ("write_file", False)
    ]
    assert {
        tool.tool_id: tool.enabled for tool in await service.list()
    } == {
        "read_file": True,
        "write_file": False,
    }


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
                        enabled=False,
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
                    enabled=False,
                ),
                ToolSelectionChange(tool_id="task", enabled=False),
                ToolSelectionChange(
                    tool_id="create_team_member",
                    enabled=False,
                ),
            ]
        )
    )

    assert {tool.tool_id for tool in changed if not tool.enabled} == {
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
