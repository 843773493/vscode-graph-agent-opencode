"""真实工具开关 HTTP 的存储错误不公开本机路径，修复后可用原入口重试。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import portalocker
import pytest

from tests.harness.python.run_context import TestRunContext


async def test_disabled_builtin_stays_in_management_catalog_and_can_be_reenabled(
    integration_client: httpx.AsyncClient, request: pytest.FixtureRequest,
) -> None:
    """真实目录生成与持久选择串联，禁用回执不能在提交之后因定义消失报错。"""
    client = integration_client
    listed = await client.get("/api/v1/tools")
    assert listed.status_code == 200, listed.text
    original = next(tool for tool in listed.json()["data"] if tool["tool_id"] == "test_tool")
    assert original["execution_enabled"] is True
    expected = {**original, "execution_enabled": False, "model_visible": False}
    request_id = "req_disable_builtin"
    disabled = await client.patch("/api/v1/tools/selection", json={
        "agent_id": "default", "changes": [
            {key: expected[key] for key in ("tool_id", "execution_enabled", "model_visible")},
        ],
    }, headers={"X-Request-ID": request_id})
    assert disabled.status_code == 200, disabled.text
    assert disabled.headers["X-Request-ID"] == disabled.json()["request_id"] == request_id
    assert disabled.json()["data"] == [expected]
    after = await client.get("/api/v1/tools")
    assert after.status_code == 200, after.text
    assert {tool["tool_id"] for tool in after.json()["data"]} == {tool["tool_id"] for tool in listed.json()["data"]}
    assert next(tool for tool in after.json()["data"] if tool["tool_id"] == "test_tool") == expected
    detail = await client.get("/api/v1/tools/test_tool")
    assert detail.status_code == 200 and detail.json()["data"] == expected, detail.text
    enabled = await client.patch("/api/v1/tools/selection", json={
        "agent_id": "default", "changes": [
            {key: original[key] for key in ("tool_id", "execution_enabled", "model_visible")},
        ],
    })
    assert enabled.status_code == 200 and enabled.json()["data"] == [original], enabled.text
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "disabled-builtin.json").write_text(json.dumps({
        "before": listed.json(), "disabled": disabled.json(), "after": after.json(), "reenabled": enabled.json(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    log_path = root / ".boxteam/logs/integration-backend.stderr.log"
    log_offset = log_path.stat().st_size
    response = (
        await integration_client.patch("/api/v1/tools/selection", json=payload, headers=headers)
        if failure == "write" else await integration_client.get("/api/v1/tools", headers=headers)
    )
    assert response.status_code == (400 if failure in {"json", "utf8"} else 500), response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
    assert str(root) not in response.text and "private-input.txt" not in response.text
    if failure in {"json", "utf8"}:
        assert "artifact=tool_selection" in response.text
    else:
        error_type = "PermissionError, errno=13" if failure in {"permission", "write"} else "TypeError"
        assert response.json()["message"] == f"请求处理失败: stage=http_request, error={error_type}"
        with log_path.open("rb") as log:
            log.seek(log_offset)
            diagnostics = log.read().decode()
        assert "artifact=tool_selection" in diagnostics
        assert f"request_id={request_id} response_started=false" in diagnostics
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
    log_path = root / ".boxteam/logs/integration-backend.stderr.log"
    log_offset = log_path.stat().st_size
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
    assert response.json()["message"] == "请求处理失败: stage=http_request, error=TimeoutError, errno=None"
    with log_path.open("rb") as log:
        log.seek(log_offset)
        diagnostics = log.read().decode()
    assert "timeout_seconds=10" in diagnostics and "artifact=tool_selection" in diagnostics
    assert f"request_id={request_id} response_started=false" in diagnostics
    assert str(root) not in response.text
    assert path.read_bytes() == original
    repaired = await integration_client.get("/api/v1/tools")
    assert repaired.status_code == 200, repaired.text
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "lock-timeout.json").write_text(
        json.dumps({"error": response.json(), "repaired": repaired.json()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
