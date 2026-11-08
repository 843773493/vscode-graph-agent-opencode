from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app import container as container_module
from app.api.deps import get_workspace_container, workspace_container_scope
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import (
    get_session_catalog_components,
    get_workspace_id,
    get_workspace_root,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture(params=["/private/workspace-identity-secret", r"C:\private\workspace-identity-secret", r"\\server\private\workspace-identity-secret"])
def invalid_workspace_identity(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=["header", "path", "path_and_header"])
def invalid_workspace_carrier(
    request: pytest.FixtureRequest, invalid_workspace_identity: str,
) -> Request:
    value = invalid_workspace_identity
    headers = [] if request.param == "path" else [(b"x-boxteam-workspace-id", value.encode())]
    path_params = {} if request.param == "header" else {
        "workspace_id": value if request.param == "path" else "00000000-0000-4000-8000-000000000001",
    }
    return Request({"type": "http", "headers": headers, "path_params": path_params})


def test_invalid_identity_carriers_never_echo_paths_or_lookup_a_container(
    invalid_workspace_carrier: Request, invalid_workspace_identity: str,
) -> None:
    # Request 未装配 app/registry，越过身份拒绝进入服务图查找会直接使本例失败。
    with pytest.raises(HTTPException) as caught:
        get_workspace_container(invalid_workspace_carrier)
    assert caught.value.status_code == 400
    assert invalid_workspace_identity not in str(caught.value.detail)
    assert caught.value.detail == "Workspace 后端工作区 ID 必须是标准 UUID"


@pytest.fixture
def workspace_roots(request: pytest.FixtureRequest) -> tuple[Path, Path]:
    output_workspace = TestRunContext.from_test_file(Path(request.node.path)).workspace_root
    template_root = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    roots = (output_workspace / "workspace-a", output_workspace / "workspace-b")
    for root in roots:
        prepare_test_workspace(
            workspace_root=root,
            template_root=template_root,
            template_items=(),
        )
    return roots


@pytest.mark.asyncio
async def test_concurrent_requests_keep_workspace_context_through_child_tasks(
    monkeypatch: pytest.MonkeyPatch,
    workspace_roots: tuple[Path, Path],
) -> None:
    roots = workspace_roots
    workspace_ids = [str(uuid4()), str(uuid4())]
    mounts = []
    for workspace_id, root in zip(workspace_ids, roots, strict=True):
        mounts.append(
            MountedWorkspace(workspace_id, root).to_payload()
        )
    mounted = MountedWorkspaceRegistry.from_payload(mounts)

    def build_container(*, workspace_root: Path, **_: object) -> SimpleNamespace:
        mount = next(
            item
            for item in mounted.mounts
            if item.workspace_root == workspace_root
        )
        return SimpleNamespace(
            workspace_id=mount.workspace_id,
            workspace_root=mount.workspace_root,
            session_catalog=get_session_catalog_components(
                mount.workspace_root / ".boxteam" / "sessions", workspace_id=mount.workspace_id,
            ),
        )

    monkeypatch.setattr(container_module, "build_app_container", build_container)
    with WorkspaceContainerRegistry(mounted_workspaces=mounted) as registry:
        app = FastAPI()
        app.state.workspace_container_registry = registry

        @app.get("/scope", dependencies=[Depends(workspace_container_scope)])
        async def scope_probe() -> dict[str, str]:
            async def read_child_context() -> tuple[str, str]:
                await asyncio.sleep(0)
                return get_workspace_id(), str(get_workspace_root())

            root_before_yield = str(get_workspace_root())
            await asyncio.sleep(0)
            child_workspace_id, child_root = await asyncio.create_task(
                read_child_context()
            )
            return {
                "workspace_id": get_workspace_id(),
                "root_before_yield": root_before_yield,
                "root_after_yield": str(get_workspace_root()),
                "child_workspace_id": child_workspace_id,
                "child_root": child_root,
            }

        @app.get(
            "/workspaces/{workspace_id}/scope",
            dependencies=[Depends(workspace_container_scope)],
        )
        async def path_scope_probe(workspace_id: str) -> dict[str, str]:
            return {"workspace_id": get_workspace_id(), "path_id": workspace_id}

        @app.websocket(
            "/workspaces/{workspace_id}/scope",
            dependencies=[Depends(workspace_container_scope)],
        )
        async def websocket_scope_probe(websocket: WebSocket) -> None:
            await websocket.accept()

            async def read_child_context() -> dict[str, str]:
                await asyncio.sleep(0)
                return {"workspace_id": get_workspace_id(), "root": str(get_workspace_root())}

            await websocket.send_json(await asyncio.create_task(read_child_context()))
            await websocket.close()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://workspace.test",
        ) as client:
            requests = [
                client.get(
                    "/scope",
                    headers={"X-BoxTeam-Workspace-Id": workspace_ids[index % 2]},
                )
                for index in range(40)
            ]
            responses = await asyncio.gather(*requests)

            assert all(response.status_code == 200 for response in responses)
            for index, response in enumerate(responses):
                expected_index = index % 2
                payload = response.json()
                assert payload == {
                    "workspace_id": workspace_ids[expected_index],
                    "root_before_yield": str(roots[expected_index]),
                    "root_after_yield": str(roots[expected_index]),
                    "child_workspace_id": workspace_ids[expected_index],
                    "child_root": str(roots[expected_index]),
                }

            path_and_header_mismatch = await client.get(
                f"/workspaces/{workspace_ids[0]}/scope",
                headers={"X-BoxTeam-Workspace-Id": workspace_ids[1]},
            )
            missing_identity = await client.get("/scope")

        assert path_and_header_mismatch.status_code == 400
        assert missing_identity.status_code == 400

        with TestClient(app) as websocket_client:
            for workspace_id, root in zip(workspace_ids, roots, strict=True):
                with websocket_client.websocket_connect(
                    f"/workspaces/{workspace_id}/scope",
                    headers={"X-BoxTeam-Workspace-Id": workspace_id},
                ) as websocket:
                    assert websocket.receive_json() == {"workspace_id": workspace_id, "root": str(root)}
            with (
                pytest.raises(WebSocketDenialResponse) as denied,
                websocket_client.websocket_connect(
                    f"/workspaces/{workspace_ids[0]}/scope",
                    headers={"X-BoxTeam-Workspace-Id": workspace_ids[1]},
                ),
            ):
                pytest.fail("归属冲突的 WebSocket 不得进入路由")
            assert denied.value.status_code == 400
