"""真实 Gateway 注册入口不得把不可用私钥的位置复制到普通错误。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import commentjson
import httpx
import pytest

from app.gateway.federation import build_remote_gateway_connection_id
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)


@pytest.fixture
def key_path(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    path = tmp_path / "private-key-location-marker"
    if request.param == "directory":
        path.mkdir()
    return path


@pytest.fixture
def gateway(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Iterator[tuple[TestRunContext, str]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    assert context.workspace_root == Path(integration_workspace_root_path)
    # 本场景不调用模型，工作区启动不能依赖开发者的个人凭据。
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in config["llm"]["providers"]:
        provider["api_key"] = "private-key-preflight-unused-key"
        provider.pop("auth", None)
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    handle = start_gateway_process(
        workspace_root=context.workspace_root, default_backend_url=None,
        port=port, artifacts_dir=context.artifacts_dir,
    )
    try:
        yield context, f"http://127.0.0.1:{port}"
    finally:
        close_gateway_process(handle)


@pytest.mark.parametrize("key_path", ["missing", "directory"], indirect=True)
async def test_key_preflight_failure_preserves_registry_without_exposing_native_path(
    key_path: Path, gateway: tuple[TestRunContext, str],
) -> None:
    context, url = gateway
    key_existed = key_path.exists()
    connection_id = build_remote_gateway_connection_id(
        host="127.0.0.1", port=1, username="unreachable", remote_gateway_port=8014,
    )
    replies = []
    async with httpx.AsyncClient(base_url=url, headers=LOCAL_TOKEN_HEADERS, timeout=10, trust_env=False) as client:
        before = await client.get("/api/gateway/workspaces")
        assert before.status_code == 200, before.text
        before_data = before.json()["data"]
        registry_checks = []
        for attempt in range(2):
            request_id = f"req_private_key_{key_path.is_dir()}_{attempt}"
            response = await client.post("/api/gateway/remote-gateways", headers={"X-Request-ID": request_id}, json={
                "host": "127.0.0.1", "port": 1, "username": "unreachable",
                "private_key_path": str(key_path), "remote_gateway_port": 8014,
            })
            replies.append(response.json())
            assert response.status_code == 400, response.text
            assert str(key_path) not in response.text
            assert "private-key-location-marker" not in response.text
            assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
            assert response.json()["detail"] == (
                "SSH 私钥不存在或不是普通文件: "
                f"connection_id={connection_id}, stage=private_key_preflight"
            )
            after = await client.get("/api/gateway/workspaces")
            assert after.status_code == 200, after.text
            after_data = after.json()["data"]
            registry_checks.append({"before": before_data, "after": after_data})
            (context.artifacts_dir / "registry-checks.json").write_text(
                json.dumps(registry_checks, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            # GET 每次重新执行健康探测，checked_at 只记录本次探测时间。
            for old, current in zip(before_data["items"], after_data["items"], strict=True):
                assert datetime.fromisoformat(current["checked_at"]) >= datetime.fromisoformat(old["checked_at"])
                assert {**current, "checked_at": old["checked_at"]} == old
            assert {**after_data, "items": before_data["items"]} == before_data
    assert key_path.exists() == key_existed
    if key_existed:
        assert key_path.is_dir()
        assert list(key_path.iterdir()) == []
    logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert str(key_path) in logs
    assert "stage=private_key_preflight" in logs
    artifact = context.artifacts_dir / f"key-error-{'directory' if key_path.is_dir() else 'missing'}.json"
    artifact.write_text(json.dumps({"connection_id": connection_id, "responses": replies}, ensure_ascii=False, indent=2) + "\n")
