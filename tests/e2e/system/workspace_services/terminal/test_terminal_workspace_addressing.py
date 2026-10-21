"""两个真实 Terminal 进程在 HTTP、WS 与持久 PTY 事实 上的显式 UUID 隔离。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
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
from tests.harness.python.process_output import spawn_logged_process
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.processes.workspace import terminate_process, wait_for_http_ok
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def terminal_mounts(
    request: pytest.FixtureRequest, e2e_backend_port: int
) -> Iterator[list[tuple[str, int, Path]]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    mounts = []
    with ExitStack() as lifetime:
        for index, name in enumerate(("a", "b")):
            root = context.workspace_root / name
            prepare_default_test_workspace(
                workspace_root=root,
                template_root=Path.cwd()
                / "tests/fixtures/workspaces/default_test_workspace",
            )
            (root / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
            workspace_id = load_or_create_workspace_id(root)
            backend_port, frontend_port = terminal_ports_with_offset(
                e2e_backend_port, 40 + index * 2
            )
            process = start_terminal_backend(
                backend_port=backend_port, workspace_root=str(root),
                artifacts_dir=context.artifacts_dir,
            )
            lifetime.callback(terminate_process, process)
            node = shutil.which("node")
            assert node is not None
            frontend, frontend_logs = spawn_logged_process(
                [
                    node,
                    "server.js",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(frontend_port),
                    "--backend-url",
                    f"http://127.0.0.1:{backend_port}",
                    "--workspace-root",
                    str(root),
                    "--workspace-id",
                    workspace_id,
                    "--asset-root",
                    str(Path.cwd()),
                ],
                cwd=Path.cwd() / "src/workspace-services/terminal/client",
                env=os.environ.copy(),
                artifacts_dir=context.artifacts_dir,
                log_name=f"terminal-{name}-frontend",
            )
            lifetime.callback(terminate_process, frontend)
            try:
                wait_for_http_ok(
                    f"http://127.0.0.1:{frontend_port}/health", frontend,
                    timeout_seconds=30,
                )
            except BaseException as error:
                for path in frontend_logs:
                    error.add_note(f"Terminal 测试进程日志：{path}")
                raise
            mounts.append((workspace_id, backend_port, root))
        yield mounts


@pytest.mark.asyncio
async def test_two_terminal_mounts_require_identity_for_empty_lists_http_and_websocket(
    terminal_mounts,
):
    (owner_a, port_a, root_a), (owner_b, port_b, root_b) = terminal_mounts
    assert owner_a != owner_b
    async with (
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port_a}",
            headers={
                "X-BoxTeam-Workspace-Id": owner_a,
                "X-BoxTeam-Resource-Owner": "workspace-terminal",
            },
        ) as client_a,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port_b}",
            headers={
                "X-BoxTeam-Workspace-Id": owner_b,
                "X-BoxTeam-Resource-Owner": "workspace-terminal",
            },
        ) as client_b,
    ):
        for identity, port in ((owner_a, port_a), (owner_b, port_b)):
            frontend_health = (
                await client_a.get(f"http://127.0.0.1:{port + 1}/health")
            ).json()
            assert frontend_health["workspace_id"] == identity
            assert "workspace_root" not in frontend_health
            config = (await client_a.get(f"http://127.0.0.1:{port + 1}/config.js")).text
            assert f'window.BOXTEAM_WORKSPACE_ID = "{identity}"' in config
        for client in (client_a, client_b):
            empty = await client.get("/api/terminals")
            assert empty.status_code == 200 and empty.json()["data"] == []
        for header, expected in (("", 400), ("not-a-uuid", 400), (owner_b, 404)):
            response = await client_a.get(
                "/api/terminals", headers={"X-BoxTeam-Workspace-Id": header}
            )
            assert response.status_code == expected, response.text
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port_a}") as unscoped:
            assert (await unscoped.get("/api/terminals")).status_code == 403
            assert (
                await unscoped.get(
                    "/api/terminals",
                    headers={"X-BoxTeam-Resource-Owner": "workspace-terminal"},
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
                    f"ws://127.0.0.1:{port_a}/terminal{suffix}"
                ):
                    raise AssertionError("非法身份不得完成 WS upgrade")
            assert rejection.value.response.status_code == status
        for transport, port, owner, terminal_id in (
            (client_a, port_a, owner_a, terminals[0]),
            (client_b, port_b, owner_b, terminals[1]),
        ):
            grant_response = await transport.post(
                f"/api/terminals/{terminal_id}/transport-grant",
                json={"owner_revision": 1, "association": None},
            )
            grant_response.raise_for_status()
            grant_token = grant_response.json()["data"]["grant_token"]
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/terminal?workspace_id={owner}"
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
