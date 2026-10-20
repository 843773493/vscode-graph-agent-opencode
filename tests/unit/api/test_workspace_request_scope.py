from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from app import container as container_module
from app.api.deps import workspace_container_scope
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import (
    get_session_catalog_components,
    get_workspace_id,
    get_workspace_root,
)


@pytest.mark.asyncio
async def test_concurrent_requests_keep_workspace_context_through_child_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = [tmp_path / "workspace-a", tmp_path / "workspace-b"]
    workspace_ids = [str(uuid4()), str(uuid4())]
    mounts = []
    for workspace_id, root in zip(workspace_ids, roots, strict=True):
        root.mkdir()
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
