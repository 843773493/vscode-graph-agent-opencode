"""会话默认设置由真实 API 创建，存储损坏与 I/O 故障不公开本机位置。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
async def defaults_error_target(
    integration_client: httpx.AsyncClient, integration_workspace_root_path: str,
):
    listed = await integration_client.get("/api/v1/agents")
    assert listed.status_code == 200, listed.text
    agent = next(item for item in listed.json()["data"] if item["workspace_default"])
    provider = next(item for item in agent["providers"] if item["workspace_default"])
    agent_payload = {"agent_id": agent["agent_id"]}
    provider_payload = {"provider_id": provider["provider_id"]}
    for url, payload in [
        ("/api/v1/agents/workspace-default", agent_payload),
        (f"/api/v1/agents/{agent['agent_id']}/workspace-default-provider", provider_payload),
    ]:
        response = await integration_client.put(url, json=payload)
        assert response.status_code == 200, response.text
    root = Path(integration_workspace_root_path)
    path = root / ".boxteam/settings/session_defaults.json"
    original = path.read_bytes()
    try:
        yield root, path, original, agent_payload, provider_payload
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)
        path.write_bytes(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["shape", "version", "agent", "provider", "json", "utf8", "permission", "write"])
async def test_session_defaults_errors_preserve_original_and_recover(
    integration_client: httpx.AsyncClient, defaults_error_target,
    failure: str, request: pytest.FixtureRequest,
):
    root, path, original, agent_payload, provider_payload = defaults_error_target
    if failure in {"permission", "write"} and (os.name == "nt" or os.geteuid() == 0):
        pytest.skip("真实权限错误需要非 root POSIX 用户，Windows ACL 单独验收")
    data = json.loads(original)
    private_input = str(root / "private-input.txt")
    if failure == "shape":
        data = []
    elif failure == "version":
        data["schema_version"] = private_input
    elif failure == "agent":
        data["default_agent_id"] = private_input
    elif failure == "provider":
        data["provider_by_agent"][agent_payload["agent_id"]] = private_input
    corrupted = json.dumps(data).encode() if failure not in {"permission", "write"} else original
    if failure == "json":
        corrupted = ('{"' + private_input + '":').encode()
    elif failure == "utf8":
        corrupted = b"\xff" + private_input.encode()
    path.write_bytes(corrupted)
    if failure == "permission":
        path.chmod(0)
    elif failure == "write":
        path.parent.chmod(0o555)
    request_id = f"req_defaults_{failure}"
    headers = {"X-Request-ID": request_id}
    response = (
        await integration_client.put("/api/v1/agents/workspace-default", json=agent_payload, headers=headers)
        if failure == "write" else await integration_client.get("/api/v1/agents", headers=headers)
    )
    assert response.status_code == 500, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
    assert str(root) not in response.text and "private-input.txt" not in response.text
    assert "artifact=session_defaults" in response.text
    if failure in {"permission", "write"}:
        assert "PermissionError" in response.text and "errno=13" in response.text
    path.parent.chmod(0o755)
    path.chmod(0o644)
    assert path.read_bytes() == corrupted
    assert not path.with_suffix(".json.tmp").exists()
    path.write_bytes(original)
    repaired = await integration_client.get("/api/v1/agents")
    assert repaired.status_code == 200, repaired.text
    agent = next(item for item in repaired.json()["data"] if item["workspace_default"])
    assert agent["agent_id"] == agent_payload["agent_id"]
    assert [item["provider_id"] for item in agent["providers"] if item["workspace_default"]] == [provider_payload["provider_id"]]
    retried = await integration_client.put("/api/v1/agents/workspace-default", json=agent_payload)
    assert retried.status_code == 200, retried.text
    assert path.read_bytes() == original
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{failure}.json").write_text(
        json.dumps({"error": response.json(), "repaired": repaired.json()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
