"""两个真实 Terminal 进程在 HTTP、WS 与持久 PTY 事实 上的显式 UUID 隔离。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
import websockets
from websockets.exceptions import InvalidStatus

from app.core.identifier import create_uuid_hex
from app.core.workspace_identity import load_or_create_workspace_id
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    terminal_ports_with_offset,
    wait_process_gone,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import (
    TerminalPluginService,
    start_terminal_plugin_service,
    stop_terminal_plugin_service,
)
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
def terminal_mounts(
    request: pytest.FixtureRequest, e2e_backend_port: int
) -> Iterator[list[tuple[str, TerminalPluginService, Path]]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    mounts = []
    with ExitStack() as lifetime:
        for index, name in enumerate(("a", "b")):
            root = context.workspace_root / name
            prepare_test_workspace(
                workspace_root=root,
                template_root=Path.cwd()
                / "tests/fixtures/workspaces/default_test_workspace",
                template_items=(),
            )
            workspace_id = load_or_create_workspace_id(root)
            backend_port, _frontend_port = terminal_ports_with_offset(
                e2e_backend_port, 40 + index * 2
            )
            service = start_terminal_plugin_service(
                backend_port=backend_port, workspace_root=str(root),
                artifacts_dir=context.artifacts_dir,
            )
            lifetime.callback(stop_terminal_plugin_service, service)
            mounts.append((workspace_id, service, root))
        yield mounts


@pytest.mark.asyncio
async def test_two_terminal_mounts_require_identity_for_empty_lists_http_and_websocket(
    terminal_mounts,
):
    (owner_a, service_a, root_a), (owner_b, service_b, root_b) = terminal_mounts
    assert owner_a != owner_b
    async with (
        httpx.AsyncClient(
            base_url=service_a.http_url,
            headers=service_a.headers,
        ) as client_a,
        httpx.AsyncClient(
            base_url=service_b.http_url,
            headers=service_b.headers,
        ) as client_b,
    ):
        for client in (client_a, client_b):
            empty = await client.get("/api/terminals")
            assert empty.status_code == 200 and empty.json()["data"] == []
        for header, expected in (("", 400), ("not-a-uuid", 400), (owner_b, 404)):
            response = await client_a.get(
                "/api/terminals", headers={"X-BoxTeam-Workspace-Id": header}
            )
            assert response.status_code == expected, response.text
        async with httpx.AsyncClient(base_url=service_a.http_url) as unscoped:
            assert (await unscoped.get("/api/terminals")).status_code == 401
            assert (
                await unscoped.get(
                    "/api/terminals",
                    headers={"X-Local-Token": service_a.token},
                )
            ).status_code == 400
        assert (
            await client_a.get("/api/terminals", params={"workspace_id": owner_b})
        ).status_code == 400
        assert (
            await client_a.get(
                "/api/terminals",
                params=[("workspace_id", owner_a), ("workspace_id", owner_a)],
            )
        ).status_code == 400

        terminals = []
        pids = []
        for client, owner, root in (
            (client_a, owner_a, root_a),
            (client_b, owner_b, root_b),
        ):
            created = await client.post(
                "/api/terminals",
                json={
                    "workspace_id": owner,
                    "terminal_id": f"term_{create_uuid_hex()}",
                    "owner_revision": 1,
                    "cwd": str(root),
                },
            )
            assert created.status_code == 200, created.text
            terminal = created.json()["data"]
            assert terminal["workspace_id"] == owner
            assert not {
                "session_id",
                "source_thread_id",
                "retention_scope",
                "thread_associations",
            }.intersection(terminal)
            terminals.append(terminal["terminal_id"])
            pids.append(int(terminal["os_pid"]))
            listed = (await client.get("/api/terminals")).json()["data"]
            assert [item["terminal_id"] for item in listed] == [terminal["terminal_id"]]
            assert (
                await client.post(f"/api/terminals/{terminal['terminal_id']}/read")
            ).status_code == 200
        assert terminals[0] != terminals[1]
        assert (await client_a.get(f"/api/terminals/{terminals[1]}")).status_code == 404
        assert (
            await client_b.post(f"/api/terminals/{terminals[0]}/kill", json={})
        ).status_code == 404
        conflict = await client_a.post(
            "/api/terminals",
            json={
                "workspace_id": owner_b,
                "terminal_id": f"term_{create_uuid_hex()}",
                "owner_revision": 1,
            },
        )
        assert conflict.status_code == 400
        legacy = await client_a.post(
            "/api/terminals", json={"workspace_id": owner_a, "session_id": "legacy"}
        )
        assert legacy.status_code == 400 and "session_id" in legacy.text

        for suffix, status in (
            ("", 400),
            ("?workspace_id=invalid", 400),
            (f"?workspace_id={owner_b}", 404),
        ):
            with pytest.raises(InvalidStatus) as rejection:
                async with websockets.connect(
                    service_a.websocket_url + suffix,
                    additional_headers={"X-Local-Token": service_a.token},
                ):
                    raise AssertionError("非法身份不得完成 WS upgrade")
            assert rejection.value.response.status_code == status
        for transport, service, owner, terminal_id in (
            (client_a, service_a, owner_a, terminals[0]),
            (client_b, service_b, owner_b, terminals[1]),
        ):
            grant_response = await transport.post(
                f"/api/terminals/{terminal_id}/transport-grant",
                json={"owner_revision": 1, "association": None},
            )
            grant_response.raise_for_status()
            grant_token = grant_response.json()["data"]["grant_token"]
            async with websockets.connect(
                service.websocket_url + f"?workspace_id={owner}",
                additional_headers=service.headers,
            ) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "type": "attach",
                            "terminalId": terminal_id,
                            "grantToken": grant_token,
                            "cols": 80,
                            "rows": 24,
                        }
                    )
                )
                while True:
                    message = json.loads(
                        await asyncio.wait_for(socket.recv(), timeout=5)
                    )
                    if message["type"] == "attached":
                        assert message["snapshot"]["workspace_id"] == owner
                        break
        for client, terminal_id, pid in zip(
            (client_a, client_b), terminals, pids, strict=True
        ):
            killed = await client.post(
                f"/api/terminals/{terminal_id}/kill", json={"reason": "model_requested"}
            )
            assert killed.status_code == 200, killed.text
            assert killed.json()["data"]["terminal"]["status"] == "terminated"
            wait_process_gone(pid)
            assert (
                await client.delete(f"/api/terminals/{terminal_id}")
            ).status_code == 200
