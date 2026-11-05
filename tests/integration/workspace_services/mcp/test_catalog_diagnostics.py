"""真实后端 HTTP 与本地 stdio MCP 协议失败；不替换目录 owner。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from tests.support.mcp.configuration import write_mcp_config


@pytest.fixture(scope="module")
def catalog_artifacts(integration_workspace_root_path: str) -> Path:
    artifacts = Path(integration_workspace_root_path).parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts


@pytest.fixture(scope="module")
def integration_config_path(integration_workspace_root_path: str, catalog_artifacts: Path) -> str:
    project = Path.cwd()
    return write_mcp_config(
        workspace_root=Path(integration_workspace_root_path),
        servers={"mini": {
            "enabled": True, "transport": "stdio", "command": sys.executable,
            "args": [
                str(project / "tests/integration/workspace_services/mcp/catalog_failure_server.py"),
                str(catalog_artifacts / "failure.json"),
                str(catalog_artifacts / "protocol.jsonl"),
            ],
            "cwd": str(project),
        }},
        allowed_mcp_tools=["mcp__mini__echo"],
    )


async def test_stdio_catalog_errors_are_facts_and_http_recovers(
    integration_client: httpx.AsyncClient, catalog_artifacts: Path,
    integration_workspace_root_path: str,
) -> None:
    before = await integration_client.get("/api/v1/mcp/servers")
    assert before.status_code == 200
    original = before.json()["data"]
    assert original[0]["server_id"] == "mini"
    assert original[0]["tools"][0]["tool_id"] == "mcp__mini__echo"
    evidence: list[dict[str, object]] = []
    failure_path = catalog_artifacts / "failure.json"
    protocol_path = catalog_artifacts / "protocol.jsonl"
    protocol_before = protocol_path.read_bytes()
    for _ in range(3):
        passive = await integration_client.get("/api/v1/mcp/servers", params={"refresh": "false"})
        assert passive.status_code == 200
        assert passive.json()["data"] == original
        assert passive.json()["catalog_revision"] == before.json()["catalog_revision"]
        assert passive.json()["catalog_generation"] == before.json()["catalog_generation"]
        assert passive.headers["x-request-id"] == passive.json()["request_id"]
        assert protocol_path.read_bytes() == protocol_before
    native_paths = [
        "/private/mcp/catalog.json", r"C:\private\mcp\catalog.json",
        r"\\server\private\mcp\catalog.json", r"\\?\UNC\server\private\mcp\catalog.json",
    ]
    try:
        for native_path in native_paths:
            message = f"private protocol failure: {native_path}"
            failure_path.write_text(json.dumps({"message": message, "path": native_path}), encoding="utf-8")
            previous_lists = len(protocol_path.read_text().splitlines())
            response = await integration_client.get("/api/v1/mcp/servers")
            evidence.append({"status": response.status_code, "body": response.json()})
            assert response.status_code == 503
            public_error = response.json()["detail"]
            assert "server_id=mini" in public_error
            assert "stage=relist" in public_error
            assert "McpError" in public_error
            assert "rpc_code=-32603" in public_error
            assert "private" not in response.text
            assert response.headers["x-request-id"] == response.json()["request_id"]
            protocol = [json.loads(line) for line in protocol_path.read_text().splitlines()]
            assert len(protocol) == previous_lists + 1
            assert protocol[-1] == {"method": "tools/list", "failure": {"message": message, "path": native_path}}
            protocol_before = protocol_path.read_bytes()
            for _ in range(2):
                passive = await integration_client.get("/api/v1/mcp/servers", params={"refresh": "false"})
                evidence.append({"phase": "passive-unavailable", "status": passive.status_code, "body": passive.json()})
                assert passive.status_code == 503
                assert passive.json()["detail"] == public_error
                assert "private" not in passive.text
                assert passive.headers["x-request-id"] == passive.json()["request_id"]
                assert protocol_path.read_bytes() == protocol_before
            log_root = Path(integration_workspace_root_path) / ".boxteam/logs"
            logs = "\n".join(path.read_text(encoding="utf-8") for path in log_root.glob("integration-backend.*.log"))
            assert message in logs
            failure_path.unlink()
            passive = await integration_client.get("/api/v1/mcp/servers", params={"refresh": "false"})
            evidence.append({"phase": "passive-before-recovery", "status": passive.status_code, "body": passive.json()})
            assert passive.status_code == 503
            assert passive.json()["detail"] == public_error
            assert passive.headers["x-request-id"] == passive.json()["request_id"]
            assert protocol_path.read_bytes() == protocol_before
            recovered = await integration_client.get("/api/v1/mcp/servers", params={"refresh": "true"})
            evidence.append({"phase": "active-recovery", "status": recovered.status_code, "body": recovered.json()})
            assert recovered.status_code == 200
            assert recovered.json()["data"] == original
            assert recovered.headers["x-request-id"] == recovered.json()["request_id"]
            protocol = [json.loads(line) for line in protocol_path.read_text().splitlines()]
            assert len(protocol) == previous_lists + 2
            assert protocol[-1] == {"method": "tools/list", "failure": None}
            protocol_before = protocol_path.read_bytes()
            passive = await integration_client.get("/api/v1/mcp/servers", params={"refresh": "false"})
            assert passive.status_code == 200
            assert passive.json()["data"] == original
            assert passive.json()["catalog_revision"] == recovered.json()["catalog_revision"]
            assert passive.json()["catalog_generation"] == recovered.json()["catalog_generation"]
            assert passive.headers["x-request-id"] == passive.json()["request_id"]
            assert protocol_path.read_bytes() == protocol_before
    finally:
        failure_path.unlink(missing_ok=True)
        (catalog_artifacts / "http-failures.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
