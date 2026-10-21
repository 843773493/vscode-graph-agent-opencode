"""真实工具开关 HTTP 的存储错误不公开本机路径，修复后可用原入口重试。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import portalocker
import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
async def selection_error_target(
    integration_client: httpx.AsyncClient, integration_workspace_root_path: str,
):
    root = Path(integration_workspace_root_path)
    listed = await integration_client.get("/api/v1/tools")
    assert listed.status_code == 200, listed.text
    tool = next(item for item in listed.json()["data"] if item["tool_id"] == "read_file")
    change = {key: tool[key] for key in ("tool_id", "execution_enabled", "model_visible")}
    payload = {"agent_id": "default", "changes": [change]}
    updated = await integration_client.patch("/api/v1/tools/selection", json=payload)
    assert updated.status_code == 200, updated.text
    path = root / ".boxteam/settings/tool_selection.json"
    original = path.read_bytes()
    try:
        yield root, path, original, payload, updated.json()["data"]
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)
        path.write_bytes(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["shape", "agent", "overrides", "json", "utf8", "permission", "write"])
async def test_tool_selection_storage_errors_preserve_original_and_recover(
    integration_client: httpx.AsyncClient, selection_error_target,
    failure: str, request: pytest.FixtureRequest,
):
    root, path, original, payload, expected = selection_error_target
    if failure in {"permission", "write"} and (os.name == "nt" or os.geteuid() == 0):
        pytest.skip("真实权限错误需要非 root POSIX 用户，Windows ACL 单独验收")
    private_input = str(root / "private-input.txt")
    corrupted = {
        "shape": b"[]\n",
        "agent": json.dumps({private_input: []}).encode(),
        "overrides": json.dumps({private_input: {"execution_overrides": []}}).encode(),
        "json": ('{"' + private_input + '":').encode(),
        "utf8": b"\xff" + private_input.encode(),
    }.get(failure, original)
    path.write_bytes(corrupted)
    if failure == "permission":
        path.chmod(0)
    elif failure == "write":
        path.parent.chmod(0o555)
    request_id = f"req_selection_{failure}"
    headers = {"X-Request-ID": request_id}
    response = (
        await integration_client.patch("/api/v1/tools/selection", json=payload, headers=headers)
        if failure == "write" else await integration_client.get("/api/v1/tools", headers=headers)
    )
    assert response.status_code == (400 if failure in {"json", "utf8"} else 500), response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
    assert str(root) not in response.text and "private-input.txt" not in response.text
    assert "artifact=tool_selection" in response.text
    if failure in {"permission", "write"}:
        assert "PermissionError" in response.text and "errno=13" in response.text
    path.parent.chmod(0o755)
    path.chmod(0o644)
    assert path.read_bytes() == corrupted
    assert not path.with_suffix(".json.tmp").exists()
    path.write_bytes(original)
    repaired = await integration_client.get("/api/v1/tools")
    assert repaired.status_code == 200, repaired.text
    actual = next(item for item in repaired.json()["data"] if item["tool_id"] == "read_file")
    assert actual == expected[0]
    retried = await integration_client.patch("/api/v1/tools/selection", json=payload)
    assert retried.status_code == 200 and retried.json()["data"] == expected, retried.text
    assert path.read_bytes() == original
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{failure}.json").write_text(
        json.dumps({"error": response.json(), "repaired": repaired.json()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_tool_selection_real_lock_timeout_is_bounded_and_recoverable(
    integration_client: httpx.AsyncClient, selection_error_target,
    request: pytest.FixtureRequest,
):
    root, path, original, _payload, _expected = selection_error_target
    request_id = "req_selection_lock_timeout"
    with path.with_suffix(".lock").open("a+b") as lock_file:
        portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            response = await integration_client.get(
                "/api/v1/tools", headers={"X-Request-ID": request_id},
            )
        finally:
            portalocker.unlock(lock_file)
    assert response.status_code == 500, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
    assert "TimeoutError" in response.text and "timeout_seconds=10" in response.text
    assert "artifact=tool_selection" in response.text
    assert str(root) not in response.text
    assert path.read_bytes() == original
    repaired = await integration_client.get("/api/v1/tools")
    assert repaired.status_code == 200, repaired.text
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "lock-timeout.json").write_text(
        json.dumps({"error": response.json(), "repaired": repaired.json()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
