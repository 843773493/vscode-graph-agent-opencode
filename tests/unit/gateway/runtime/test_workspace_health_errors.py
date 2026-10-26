"""健康响应的原始值只进诊断，公开故障保留身份与协议阶段。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime import local_workspace
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
)
from app.gateway.runtime.workspace import WorkspaceRuntime

UUID = "00000000-0000-4000-8000-000000000001"
URL = "http://127.0.0.1:41000"


@pytest.fixture(params=["posix", "windows", "object", "missing"])
def invalid_identity(request: pytest.FixtureRequest, tmp_path: Path) -> object:
    marker = str(tmp_path / "private-health-marker")
    return {"posix": marker, "windows": "C:\\private-health-marker\\root",
            "object": {"path": marker}, "missing": None}[request.param]


@pytest.fixture
def health_payload(invalid_identity: object) -> dict[str, object]:
    return {"workspace_id": invalid_identity, "process_id": 123}


@pytest.fixture
def health_http(monkeypatch: pytest.MonkeyPatch, health_payload: dict[str, object]) -> list[httpx.Request]:
    calls = []
    client = httpx.AsyncClient

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers.get("X-BoxTeam-Workspace-Id") in (UUID, None)
        return httpx.Response(200, json=health_payload)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *, timeout, trust_env: client(
        timeout=timeout, transport=httpx.MockTransport(response), trust_env=trust_env,
    ))
    return calls


@pytest.fixture
def registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    return GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["wait", "config-wait", "pending", "list", "adopt"])
async def test_invalid_health_identity_is_rejected_before_followup_publication(
    health_http: list[httpx.Request], registry: GatewayWorkspaceRegistry,
    invalid_identity: object, entry: str, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    target = WorkspaceTarget(
        workspace_id="workspace-test", name="测试", root_path=str(tmp_path),
        backend_workspace_id=UUID, backend_url=URL, connection_kind="local",
    )
    expected = f"Workspace 健康响应 backend UUID 不匹配: expected={UUID}"
    if entry == "list":
        registry.upsert(target, runtime=WorkspaceRuntime(service_urls={"workspace_api": URL}))
        item, = await registry.list_dtos()
        assert item.status == "offline"
        assert item.services["workspace_api"].error == expected
        assert "private-health-marker" not in item.model_dump_json()
    else:
        with pytest.raises(RuntimeError) as failure:
            if entry == "wait":
                await local_workspace.wait_for_workspace_health(URL, UUID)
            elif entry == "config-wait":
                await local_workspace.wait_for_workspace_config_proof(
                    URL, None, backend_workspace_id=UUID, expected_proof={},
                )
            elif entry == "pending":
                controller = GatewayWorkspaceRuntimeController(registry=registry, project_root=tmp_path, log_dir=tmp_path)
                await controller._resolve_pending_restart(target, candidate_ref="pending", request_id="health-test")
            else:
                await local_workspace._adopt_local_node_service(service_url=URL, service_name="browser_manager", backend_workspace_id=UUID, log_dir=tmp_path / "logs")
        message = str(failure.value)
        assert "backend UUID 不匹配" in message and f"expected={UUID}" in message
        assert "actual=" not in message and "private-health-marker" not in message
    assert len(health_http) == 1, "身份错误不得重试、提交候选或探测其它协议"
    assert "actual=" in caplog.text
    if invalid_identity is not None:
        assert "private-health-marker" in caplog.text


@pytest.fixture
def backend_contract() -> ManagedBackendContract:
    return ManagedBackendContract("owner-test", "generation-test", (ManagedBackendMember("workspace-test", UUID),), URL)


@pytest.mark.parametrize("field", ["workspace_id", "backend_generation", "mount_manifest_digest", "status"])
def test_group_proof_reports_field_without_copying_bad_values(
    backend_contract: ManagedBackendContract, tmp_path: Path, field: str, caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {"workspace_id": UUID, "backend_generation": "generation-test", "mount_manifest_digest": "manifest-test", "status": "ok"}
    payload[field] = str(tmp_path / "private-health-marker")
    with pytest.raises(RuntimeError) as failure:
        local_workspace._validate_group_health(
            payload, backend_contract, MountedWorkspace(UUID, tmp_path), manifest_digest="manifest-test",
        )
    assert str(failure.value) == f"Backend 完整组健康证明不匹配: {field}"
    assert "private-health-marker" in caplog.text


@pytest.fixture
def startup_contract() -> dict[str, object]:
    return {"fencing_token": "token-test", "candidate_id": "candidate-test", "pending_revision": 7,
            "candidate_digest": "candidate-digest", "effective_digest": "effective-digest",
            "target_generation": "generation-test", "secret_binding_digest": "binding-digest"}


def test_group_config_proof_reports_field_without_copying_bad_values(
    backend_contract: ManagedBackendContract, tmp_path: Path, startup_contract: dict[str, object], caplog: pytest.LogCaptureFixture,
) -> None:
    proof = local_workspace.workspace_config_proof_expectation(startup_contract)
    proof["candidate_id"] = str(tmp_path / "private-health-marker")
    payload = {"workspace_id": UUID, "backend_generation": "generation-test", "mount_manifest_digest": "manifest-test", "status": "ok", "process_id": 123, "config_proof": proof}
    with pytest.raises(RuntimeError) as failure:
        local_workspace._validate_group_health(
            payload, backend_contract, MountedWorkspace(UUID, tmp_path), manifest_digest="manifest-test", startup_contract=startup_contract,
        )
    assert str(failure.value) == "Workspace config proof 不匹配: candidate_id"
    assert "private-health-marker" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["wait", "config-wait", "adopt"])
async def test_failed_http_body_remains_in_logs_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, entry: str,
) -> None:
    client = httpx.AsyncClient
    body = str(tmp_path / "private-health-marker")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *, timeout, trust_env: client(
        timeout=timeout, transport=httpx.MockTransport(lambda _: httpx.Response(503, text=body)), trust_env=trust_env,
    ))
    monkeypatch.setattr(local_workspace, "GATEWAY_PROCESS_READY_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(RuntimeError if entry == "adopt" else TimeoutError) as failure:
        if entry == "wait":
            await local_workspace.wait_for_workspace_health(URL, UUID, poll_interval_seconds=0.03)
        elif entry == "config-wait":
            await local_workspace.wait_for_workspace_config_proof(
                URL, None, backend_workspace_id=UUID, expected_proof={}, poll_interval_seconds=0.03,
            )
        else:
            await local_workspace._adopt_local_node_service(service_url=URL, service_name="browser_manager", backend_workspace_id=UUID, log_dir=tmp_path / "logs")
    assert "503" in str(failure.value)
    assert body not in str(failure.value) and body in caplog.text
