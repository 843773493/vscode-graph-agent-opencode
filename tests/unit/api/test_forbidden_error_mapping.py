"""冻结 ``ForbiddenError`` 家族不再泄漏 5xx / 内部字典 repr。

触发源唯一：``app/core/path_utils.py`` 的 ``safe_join`` 路径越界抛
``ForbiddenError``。该类继承 ``HTTPException`` 但基类硬编码 ``status_code=500``，
且 ``str()`` 返回 ``"500: {'code': 403000, ...}"``。适配层漏接会冒泡成 500，
已捕获时 ``detail=str(error)`` 又把内部字典 repr 当契约下发。

本文件覆盖两类入口：
- node_debug 的配置/启动/动作入口：原先 500 + 内部字典；
- workspace 的文件读取/写入/浏览入口：原先 403 但 detail 泄漏字典 repr，或 500。

按 tests/unit/api 规范：用依赖注入隔离服务，文件系统用 pytest 临时目录。
workspace 入口使用真实 ``WorkspaceService``，只改工作区磁盘状态。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi import HTTPException

from app.api import workspace as workspace_api
from app.api.errors import client_error_message, forbidden_http_error
from app.api.node_debug import (
    apply_node_debug_action,
    create_node_debug_configuration,
    get_node_debug_configuration,
    start_node_debug,
)
from app.core.exceptions import ForbiddenError
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugSetBreakpointActionRequest,
    NodeDebugSetBreakpointParams,
    NodeDebugStartRequest,
)
from app.schemas.internal_v2.workspace import WorkspaceFileCreateRequest
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_service import WorkspaceService

_TRAVERSAL = "../../../../etc/passwd"


def _workspace_config_service_mock() -> Mock:
    config_service = Mock(spec=ConfigService)
    config_service.get_workspace_file_default_limit.return_value = 500
    config_service.get_workspace_preview_max_bytes.return_value = 1024 * 1024
    config_service.get_workspace_preview_binary_sample_bytes.return_value = 8192
    return config_service


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "real.txt").write_text("hello\n", encoding="utf-8")
    return root


@pytest.fixture
def workspace_service(workspace_root: Path) -> WorkspaceService:
    return WorkspaceService(
        config_service=_workspace_config_service_mock(),
        workspace_root=workspace_root,
    )


# --- 映射层单元：ForbiddenError -> 403 + 消息本体 -----------------------------


def test_forbidden_http_error_maps_to_403_without_dict_repr() -> None:
    error = ForbiddenError("Path traversal detected")

    mapped = forbidden_http_error(error)

    assert mapped.status_code == 403
    assert mapped.detail == "Path traversal detected"
    assert "{" not in str(mapped.detail)
    assert "403000" not in str(mapped.detail)
    assert "500" not in str(mapped.detail)


def test_client_error_message_extracts_forbidden_message_body() -> None:
    error = ForbiddenError("Path traversal detected")

    assert client_error_message(error) == "Path traversal detected"
    # 未泛化前 str(error) 会带上状态码前缀与内部字典 repr。
    assert client_error_message(error) != str(error)


# --- node_debug：9 处入口 ---------------------------------------------------


def _node_debug_service_raising(error: Exception) -> MagicMock:
    service = MagicMock()
    service.create_configuration = AsyncMock(side_effect=error)
    service.get_configuration = Mock(side_effect=error)
    service.start = AsyncMock(side_effect=error)
    service.apply_action = AsyncMock(side_effect=error)
    return service


@pytest.mark.asyncio
async def test_node_debug_create_configuration_traversal_is_403() -> None:
    service = _node_debug_service_raising(ForbiddenError("Path traversal detected"))

    with pytest.raises(HTTPException) as raised:
        await create_node_debug_configuration(
            payload=NodeDebugConfigurationCreateRequest(
                session_id="ses_x", thread_id="main", name="t",
                script_path=_TRAVERSAL,
            ),
            _="local", request_id="req", node_debug_service=service,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal detected"


@pytest.mark.asyncio
async def test_node_debug_get_configuration_traversal_is_403() -> None:
    service = _node_debug_service_raising(ForbiddenError("Path traversal detected"))

    with pytest.raises(HTTPException) as raised:
        await get_node_debug_configuration(
            session_id="ses_x", configuration_id="dbgcfg_x", thread_id="main",
            _="local", request_id="req", node_debug_service=service,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal detected"


@pytest.mark.asyncio
async def test_node_debug_start_traversal_is_403() -> None:
    service = _node_debug_service_raising(ForbiddenError("Path traversal detected"))

    with pytest.raises(HTTPException) as raised:
        await start_node_debug(
            payload=NodeDebugStartRequest(
                session_id="ses_x", thread_id="main", path=_TRAVERSAL,
            ),
            _="local", request_id="req", node_debug_service=service,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal detected"


@pytest.mark.asyncio
async def test_node_debug_action_traversal_is_403() -> None:
    service = _node_debug_service_raising(ForbiddenError("Path traversal detected"))

    with pytest.raises(HTTPException) as raised:
        await apply_node_debug_action(
            payload=NodeDebugSetBreakpointActionRequest(
                session_id="ses_x", thread_id="main", action="set_breakpoint",
                params=NodeDebugSetBreakpointParams(path=_TRAVERSAL, line=1),
            ),
            _="local", request_id="req", node_debug_service=service,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal detected"


# --- workspace：符号链接逃逸（真实 WorkspaceService） ------------------------


def _make_escape_link(workspace_root: Path) -> None:
    link = workspace_root / "escape_link"
    if not link.is_symlink():
        link.symlink_to("/etc/passwd")


@pytest.mark.asyncio
async def test_workspace_file_content_symlink_escape_is_403_without_repr(
    workspace_root: Path,
    workspace_service: WorkspaceService,
) -> None:
    _make_escape_link(workspace_root)

    with pytest.raises(HTTPException) as raised:
        await workspace_api.get_workspace_file_content(
            path="escape_link", scope="workspace",
            _="local", request_id="req", workspace_service=workspace_service,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal detected"


@pytest.mark.asyncio
async def test_workspace_file_raw_symlink_escape_is_403_without_repr(
    workspace_root: Path,
    workspace_service: WorkspaceService,
) -> None:
    _make_escape_link(workspace_root)

    with pytest.raises(HTTPException) as raised:
        await workspace_api.get_workspace_raw_file(
            path="escape_link", scope="workspace",
            _="local", workspace_service=workspace_service,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal detected"


@pytest.mark.asyncio
async def test_workspace_list_files_symlink_escape_is_403(
    workspace_root: Path,
    workspace_service: WorkspaceService,
) -> None:
    link = workspace_root / "escape_dir"
    if not link.is_symlink():
        link.symlink_to("/etc")

    with pytest.raises(HTTPException) as raised:
        await workspace_api.list_workspace_files(
            path="escape_dir", scope="workspace", limit=None, cursor=None,
            _="local", request_id="req", workspace_service=workspace_service,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal detected"


@pytest.mark.asyncio
async def test_workspace_create_entry_symlink_escape_is_403(
    workspace_root: Path,
    workspace_service: WorkspaceService,
) -> None:
    link = workspace_root / "escape_dir"
    if not link.is_symlink():
        link.symlink_to("/etc")

    with pytest.raises(HTTPException) as raised:
        await workspace_api.create_workspace_file_entry(
            payload=WorkspaceFileCreateRequest(name="a", kind="file"),
            path="escape_dir", scope="workspace",
            _="local", request_id="req", workspace_service=workspace_service,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal detected"
