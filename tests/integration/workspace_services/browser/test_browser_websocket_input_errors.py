"""真实 Node WebSocket 的输入拒绝、原始诊断及同连接恢复；不经过 Gateway/Web。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import websockets

from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.browser_manager import (
    BrowserManagerProcesses,
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def browser_service(
    request: pytest.FixtureRequest, integration_backend_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[BrowserManagerProcesses, TestRunContext, Path]]:
    with TestRunContext.from_test_file(Path(request.path)) as run:
        runtime = run.runtime_root_for_node(request.node.nodeid)
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TMP", "TEMP"):
            monkeypatch.setenv(name, str(runtime))
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        handle = start_browser_manager_processes(
            workspace_root=root, artifacts_dir=run.artifacts_dir,
            backend_port=integration_backend_port + 60,
            frontend_port=integration_backend_port + 61, backend_host="127.0.0.1",
        )
        run.add_cleanup("Browser 进程", lambda: close_browser_manager_processes(handle))
        yield handle, run, root


async def test_rejected_inputs_keep_connection_usable_without_creating_resources(
    browser_service: tuple[BrowserManagerProcesses, TestRunContext, Path],
) -> None:
    handle, run, root = browser_service
    workspace_id = load_or_create_workspace_id(root)
    base_url = f"http://127.0.0.1:{handle.backend_port}"
    client = BrowserManagerClient(workspace_id=workspace_id, backend_url=base_url)
    browser_id = f"browser_{uuid4().hex}"
    paths = ("/private/ws-profile", r"C:\private\ws-profile", r"\\host\share\ws-profile")
    receipts = []
    async with websockets.connect(
        f"ws://127.0.0.1:{handle.backend_port}/browser?workspace_id={workspace_id}",
    ) as socket:
        for native_path in paths:
            cases = [
                (native_path, "WebSocket JSON 请求解析失败: stage=websocket_json, error=SyntaxError"),
                ({"type": native_path}, "未知消息类型"),
                ({"type": "pointer", "action": native_path}, "未知 pointer action"),
                ({"type": "pointer", "action": "down", "x": 1, "y": 2, "button": native_path}, "未知 pointer button"),
                ({"type": "key", "action": native_path}, "未知 key action"),
                ({"type": "deviceProfile", "profileId": "desktop", "orientation": native_path}, "未知浏览器设备方向"),
                ({"type": "deviceSettings", "settings": {"orientation": native_path}}, "未知浏览器设备方向"),
                ({"type": "debugSnapshot", "panel": native_path}, "未知 debugSnapshot.panel"),
                ({"type": "selectElement", "x": 1, "y": 2, "mode": native_path}, "未知元素选择模式"),
                ({"type": "command", "name": native_path}, "未知 command name"),
            ]
            for message, expected in cases:
                wire = message if isinstance(message, str) else json.dumps({"browserId": browser_id, **message})
                await socket.send(wire)
                response = await asyncio.wait_for(socket.recv(), timeout=3)
                assert isinstance(response, str)
                receipt = json.loads(response)
                assert receipt["type"] == "error" and receipt["message"] == expected
                assert "ws-profile" not in response
                receipts.append(receipt)
        assert await client.list_resources() == []
        async with httpx.AsyncClient(base_url=base_url, timeout=3, trust_env=False) as http:
            response = await http.get("/health")
            response.raise_for_status()
            health = response.json()
        assert health["browser_count"] == 0
        assert health["browser_runtime"] == {"connected": False, "context_count": 0, "generation": 0}

        # 真实产品入口创建用户指定的本地页面，原连接随后使用真实授权附加/分离。
        page = root / "user-opened.html"
        page.write_text("<title>WebSocket 恢复</title><p>原生页面</p>", encoding="utf-8")
        created = await client.create_browser(
            browser_id=browser_id, owner_revision=1, title="WebSocket 恢复", url=page.as_uri(),
        )
        assert created["browser_id"] == browser_id and created["url"] == page.as_uri()
        grant = await client.issue_transport_grant(browser_id, owner_revision=1, association=None)
        for message, expected in (
            ({"type": "attach", "browserId": browser_id, "grantToken": grant["grant_token"]}, "attached"),
            ({"type": "detach"}, "detached"),
        ):
            await socket.send(json.dumps(message))
            async with asyncio.timeout(8):
                while True:
                    response = await socket.recv()
                    if isinstance(response, str):
                        receipt = json.loads(response)
                        assert receipt["type"] != "error", receipt
                        if receipt["type"] == expected:
                            break
            assert receipt["browserId"] == browser_id
            receipts.append(receipt)
        await client.delete_resource(browser_id)
    diagnostics = "\n".join(path.read_text(encoding="utf-8") for path in run.artifacts_dir.glob("browser-backend-*.stderr.log"))
    assert "websocket_json" in diagnostics and "SyntaxError" in diagnostics
    assert "websocket_validate" in diagnostics
    for native_path in paths:
        assert native_path.replace("\\", "\\\\") in diagnostics
    run.write_result("passed", {
        "scope": "真实 Node WebSocket/HTTP、Chromium、本地页面与同连接恢复；不代表 Gateway/Web、Python owner 或进程重启",
        "receipts": receipts, "health_after_rejection": health,
    })
