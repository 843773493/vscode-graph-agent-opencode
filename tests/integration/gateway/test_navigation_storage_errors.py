"""真实 Gateway 的导航读写拒绝损坏存储，修复原件后可继续操作。"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)


@pytest.mark.asyncio
async def test_navigation_corruption_rejects_without_input_echo_and_recovers(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    ports = integration_port_block_for_file(Path(request.node.fspath))
    evidence = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None,
            port=ports.port(0), artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30,
        ) as client:
            created = await client.post("/api/gateway/workspace-navigation/folders", json={"name": "原始目录"})
            assert created.status_code == 200, created.text
            folder = next(node for node in created.json()["data"]["nodes"] if node["name"] == "原始目录")
            path = context.boxteam_home / "state/gateway/navigation/workspace-tree.json"
            original = path.read_bytes()
            original_tree = created.json()["data"]
            unsafe = "/private/navigation-secret"
            corruptions: list[tuple[str, bytes, int]] = [
                ("object", b"[]", 500),
                ("nodes", json.dumps({"nodes": unsafe}).encode(), 500),
                ("json", b'{"nodes":"/private/navigation-secret",}', 400),
                ("utf8", b'\xff/private/navigation-secret', 400),
            ]
            for field in ("kind", "position", "parent_node_id"):
                payload = json.loads(original)
                next(node for node in payload["nodes"] if node["node_id"] == folder["node_id"])[field] = unsafe
                corruptions.append((field, json.dumps(payload).encode(), 400))
            try:
                for kind, content, expected_post in corruptions:
                    path.write_bytes(content)
                    for method, endpoint, expected in [
                        ("GET", "/api/gateway/workspace-navigation", 500),
                        ("POST", "/api/gateway/workspace-navigation/folders", expected_post),
                    ]:
                        options = {"json": {"name": "不得写入"}} if method == "POST" else {}
                        response = await client.request(method, endpoint, **options)
                        assert response.status_code == expected, response.text
                        assert response.json()["request_id"] == response.headers["X-Request-ID"]
                        assert str(path) not in response.text and "navigation-secret" not in response.text
                        assert path.read_bytes() == content
                        evidence[f"{kind}-{method}"] = response.json()
                    path.write_bytes(original)
                    recovered = await client.get("/api/gateway/workspace-navigation")
                    assert recovered.status_code == 200 and recovered.json()["data"] == original_tree
                    assert path.read_bytes() == original
            finally:
                path.write_bytes(original)
            updated = await client.patch(
                f"/api/gateway/workspace-navigation/nodes/{folder['node_id']}", json={"name": "修复后目录"},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["revision"] != original_tree["revision"]
            assert any(node["node_id"] == folder["node_id"] and node["name"] == "修复后目录" for node in updated.json()["data"]["nodes"])
    (context.artifacts_dir / "navigation-error-receipts.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
