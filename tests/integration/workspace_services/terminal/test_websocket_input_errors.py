"""真实 Terminal WebSocket 的输入错误边界及同连接 PTY 交互。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import websockets

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import (
    TerminalPluginService,
    start_terminal_plugin_service,
    stop_terminal_plugin_service,
)
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def terminal_service(
    request: pytest.FixtureRequest, integration_backend_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TerminalPluginService, int, Path, TestRunContext]]:
    with TestRunContext.from_test_file(Path(request.path)) as run:
        runtime = run.runtime_root_for_node(request.node.nodeid)
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TMP", "TEMP"):
            monkeypatch.setenv(name, str(runtime))
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        port = integration_backend_port + 62
        process = start_terminal_plugin_service(
            backend_port=port, workspace_root=str(root), artifacts_dir=run.artifacts_dir,
        )
        run.add_cleanup("Terminal 插件进程", lambda: stop_terminal_plugin_service(process))
        yield process, port, root, run


async def test_rejected_inputs_preserve_same_connection_and_native_shell(
    terminal_service: tuple[TerminalPluginService, int, Path, TestRunContext],
) -> None:
    plugin, _port, root, run = terminal_service
    paths = ("/private/terminal-ws", r"C:\private\terminal-ws", r"\\host\share\terminal-ws")
    receipts: list[dict[str, object]] = []
    mismatches: list[dict[str, object]] = []
    terminal_id = f"term_{uuid4().hex}"
    workspace_id = plugin.workspace_id
    client = httpx.AsyncClient(base_url=plugin.http_url, headers=plugin.headers, timeout=10, trust_env=False)
    async with AsyncExitStack() as cleanup, websockets.connect(
        f"{plugin.websocket_url}?workspace_id={workspace_id}", additional_headers=plugin.headers,
    ) as socket:
        cleanup.push_async_callback(client.aclose)
        async def expect_error(raw: str, expected: str) -> None:
            await socket.send(raw)
            async with asyncio.timeout(3):
                while True:
                    response = await socket.recv()
                    assert isinstance(response, str)
                    receipt = json.loads(response)
                    if receipt["type"] == "error":
                        break
            receipts.append(receipt)
            if receipt["message"] != expected or "terminal-ws" in response:
                mismatches.append({"expected": expected, "actual": receipt})

        for native_path in paths:
            await expect_error(native_path, "WebSocket JSON 请求解析失败: stage=websocket_json, error=SyntaxError")
            for message in (
                {"type": "resize", "cols": native_path, "rows": 2},
                {"type": "resize", "cols": 2, "rows": native_path},
                {"type": "attach", "terminalId": terminal_id, "cols": native_path},
                {"type": "attach", "terminalId": terminal_id, "rows": native_path},
                {"type": "attach", "terminalId": terminal_id, "afterSequence": native_path},
                {"type": "ack", "sequence": native_path},
            ):
                expected = f"Terminal WebSocket 消息不符合协议: type={message['type']}, field=payload, stage=websocket_schema, error=Error"
                await expect_error(json.dumps(message), expected)
        # 未附加时仍保持原授权顺序，不提前执行未知消息。
        await expect_error(json.dumps({"type": paths[0]}), "尚未 attach 到终端")
        listed = await client.get("/api/terminals")
        listed.raise_for_status()
        assert listed.json()["data"] == []

        created_response = await client.post("/api/terminals", json={
            "workspace_id": workspace_id, "terminal_id": terminal_id, "owner_revision": 1,
            "title": "WebSocket 恢复", "cwd": str(root),
        })
        created_response.raise_for_status()
        created = created_response.json()["data"]
        cleanup.push_async_callback(client.delete, f"/api/terminals/{terminal_id}")
        grant_response = await client.post(f"/api/terminals/{terminal_id}/transport-grant", json={
            "owner_revision": 1, "association": None,
        })
        grant_response.raise_for_status()
        grant = grant_response.json()["data"]
        await socket.send(json.dumps({
            "type": "attach", "terminalId": terminal_id, "grantToken": grant["grant_token"],
        }))
        async with asyncio.timeout(8):
            while True:
                attached = json.loads(await socket.recv())
                assert attached["type"] != "error", attached
                if attached["type"] == "attached":
                    break
        assert attached["terminalId"] == terminal_id
        for native_path in paths:
            await expect_error(json.dumps({"type": native_path}), "未知 WebSocket 消息类型: field=type")
        before_input = (await client.get(f"/api/terminals/{terminal_id}")).json()["data"]
        assert before_input["os_pid"] == created["os_pid"] and before_input["client_count"] == 1
        marker = f"ws_{uuid4().hex}"
        command = f"echo {marker} > ws-input-proof.txt\r"
        await socket.send(json.dumps({"type": "input", "data": command}))
        proof = root / "ws-input-proof.txt"
        async with asyncio.timeout(8):
            while not proof.is_file():
                await asyncio.sleep(0.02)
        assert proof.read_text(encoding="utf-8").strip() == marker
        after_input = (await client.get(f"/api/terminals/{terminal_id}")).json()["data"]
        assert after_input["last_input"] == command.strip()
        assert after_input["os_pid"] == created["os_pid"] and after_input["client_count"] == 1
        await socket.send('{"type":"detach"}')
        async with asyncio.timeout(5):
            while True:
                detached = json.loads(await socket.recv())
                assert detached["type"] != "error", detached
                if detached["type"] == "detached":
                    break
        assert detached["terminalId"] == terminal_id
        assert (await client.get(f"/api/terminals/{terminal_id}")).json()["data"]["client_count"] == 0
        (run.artifacts_dir / "websocket-receipts.json").write_text(
            json.dumps({"errors": receipts, "mismatches": mismatches}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        assert mismatches == []
    diagnostics = "\n".join(path.read_text(encoding="utf-8") for path in run.artifacts_dir.glob("terminal-plugin-*.stderr.log"))
    assert "websocket_json" in diagnostics and "websocket_schema" in diagnostics
    assert "SyntaxError" in diagnostics and "TerminalResize.cols" in diagnostics
    rejection_lines = [line for line in diagnostics.splitlines() if "WebSocket" in line and "[ERROR]" in line]
    assert len(rejection_lines) == 24
    assert all(f'workspace_id="{workspace_id}"' in line for line in rejection_lines)
    for native_path in paths:
        assert native_path in diagnostics or native_path.replace("\\", "\\\\") in diagnostics
    run.write_result("passed", {
        "scope": "真实Node WebSocket/HTTP、PTY、原生shell及同连接恢复；不代表Gateway/Web、Python业务owner、进程重启或Windows实机",
        "terminal_id": terminal_id, "error_count": len(receipts), "original_pid": created["os_pid"],
    })
