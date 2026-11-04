from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.core.path_utils import get_gateway_root
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER, load_proxy_gateway_id
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 复用真实日志 I/O 故障。
)
from tests.support.resource_owners.sessions import (
    SESSION_A,
    THREAD_A,
    WORKSPACE_ID,
)


@pytest.fixture
def target(tmp_path: Path) -> WorkspaceTarget:
    return WorkspaceTarget(
        gateway_id=load_proxy_gateway_id(), workspace_id=WORKSPACE_ID,
        name="测试工作区", root_path=str(tmp_path / "catalog"),
        backend_url="http://workspace.test", connection_kind="local", managed=True,
    )


@pytest.fixture
def registry(tmp_path: Path, target: WorkspaceTarget) -> Iterator[GatewayWorkspaceRegistry]:
    result = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=target.gateway_id)
    result.upsert(target, activate=False)
    try:
        yield result
    finally:
        result.close()


@pytest.fixture
def resource_record() -> dict[str, object]:
    return {
        "resource_id": "browser_owned", "workspace_id": WORKSPACE_ID, "kind": "browser",
        "retention_scope": "workspace", "source_thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
        "thread_associations": [], "owner_revision": 3, "status": "running",
        "reconciliation_status": "verified", "unattached_since": None, "reclaim_at": None,
        "available_actions": ["stop", "delete"], "metadata": {"title": "持久浏览器"},
        "created_at": "2026-10-05T00:00:00Z", "updated_at": "2026-10-05T00:00:00Z", "error": None,
    }


@pytest.mark.asyncio
async def test_catalog_loads_unassociated_workspace_browser_without_source_session(
    target: WorkspaceTarget, resource_record: dict[str, object], registry: GatewayWorkspaceRegistry,
) -> None:
    calls: list[str] = []
    def request(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
        return httpx.Response(200, json={"data": {"workspace_id": WORKSPACE_ID, "items": [resource_record]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        service=GatewayResourceCatalogService(registry=registry, http_client=client)
        resources, errors=await service._load_workspace(target, request_id="req_resources")
    assert errors == []
    assert len(resources) == 1
    assert resources[0].gateway_id == target.gateway_id
    assert resources[0].workspace_id == target.workspace_id == WORKSPACE_ID
    assert resources[0].gateway_connection_id is None
    assert resources[0].resource.workspace_id == WORKSPACE_ID
    assert resources[0].resource.thread_associations == []
    assert resources[0].resource.retention_scope == "workspace"
    assert calls == ["http://workspace.test/api/v1/resources"]


@pytest.mark.asyncio
async def test_catalog_owner_failure_exposes_workspace_error(target: WorkspaceTarget, registry: GatewayWorkspaceRegistry) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        service=GatewayResourceCatalogService(registry=registry, http_client=client)
        resources, errors=await service._load_workspace(target, request_id="req_failed")
    assert resources == []
    assert len(errors) == 1
    assert errors[0].scope_key == f"{target.gateway_id}/{target.workspace_id}"
    assert "503" in errors[0].message


@pytest.mark.asyncio
async def test_catalog_log_failure_keeps_healthy_resource_and_original_error(
    target: WorkspaceTarget, resource_record: dict[str, object],
    diagnostic_sink: DiagnosticFailure, registry: GatewayWorkspaceRegistry, tmp_path: Path,
) -> None:
    healthy = replace(target, workspace_id="00000000-0000-4000-8000-000000000002", name="健康工作区",
                      root_path=str(tmp_path / "healthy"), backend_url="http://healthy.test")
    registry.upsert(healthy, activate=False)
    original = httpx.ReadError("目录读取失败: /private/catalog-marker/state.sqlite")
    diagnostic_sink.primary = original
    calls: list[httpx.Request] = []
    failed = True

    def request(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "workspace.test" and failed:
            raise original
        workspace_id = target.workspace_id if request.url.host == "workspace.test" else healthy.workspace_id
        assert request.headers["X-BoxTeam-Workspace-Id"] == workspace_id
        return httpx.Response(200, json={"data": {"workspace_id": workspace_id, "items": [
            {**resource_record, "workspace_id": workspace_id},
        ]}})

    before = original.args
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        service = GatewayResourceCatalogService(registry=registry, http_client=client)
        rejected = await service.list(request_id="req_catalog_double_failure")
        item, = rejected.items
        assert item.workspace_id == healthy.workspace_id
        assert item.resource.model_dump() == {**resource_record, "workspace_id": healthy.workspace_id}
        error, = rejected.errors
        assert error.scope_key == f"{target.gateway_id}/{target.workspace_id}"
        assert error.message == "工作区资源目录读取失败: ReadError, diagnostic_sink_error=NotADirectoryError"
        assert "catalog-marker" not in rejected.model_dump_json()
        assert str(diagnostic_sink.destination) not in rejected.model_dump_json()
        record, = diagnostic_sink.records
        assert record.exc_info is not None and record.exc_info[1] is original
        assert record.__dict__["workspace_id"] == f"{target.gateway_id}/{target.workspace_id}"
        assert record.__dict__["request_id"] == "req_catalog_double_failure"
        secondary, = diagnostic_sink.errors
        assert str(secondary) in "\n".join(original.__notes__)
        assert original.args == before
        assert len(calls) == 2

        failed = False
        restored = await service.list(request_id="req_catalog_double_failure")
        assert restored.errors == [] and len(calls) == 4
        assert len(restored.items) == 2
        assert {(item.gateway_id, item.workspace_id, item.gateway_connection_id) for item in restored.items} == {
            (target.gateway_id, target.workspace_id, None), (healthy.gateway_id, healthy.workspace_id, None),
        }
        assert {item.workspace_id: item.resource.model_dump() for item in restored.items} == {
            owner.workspace_id: {**resource_record, "workspace_id": owner.workspace_id} for owner in (target, healthy)
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("native_path", [
    "/private/catalog-marker/state.sqlite", r"C:\private\catalog-marker\state.sqlite",
    r"\\server\private\catalog-marker\state.sqlite",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("failure", ["envelope", "validation", "transport", "http-status"])
async def test_catalog_failure_preserves_scope_and_diagnostics_without_raw_payload(
    target: WorkspaceTarget, native_path: str, failure: str, caplog: pytest.LogCaptureFixture,
    registry: GatewayWorkspaceRegistry, tmp_path: Path,
) -> None:
    healthy = replace(target, workspace_id="00000000-0000-4000-8000-000000000002", name="健康工作区",
                      root_path=str(tmp_path / "healthy"), backend_url="http://healthy.test")
    registry.upsert(healthy, activate=False)
    calls: list[httpx.Request] = []
    transport_error = httpx.ReadError(f"目录传输失败: {native_path}")
    failed = True

    def request(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["X-Request-ID"] == "req_catalog_failure"
        workspace_id = target.workspace_id if request.url.host == "workspace.test" else healthy.workspace_id
        assert request.headers["X-BoxTeam-Workspace-Id"] == workspace_id
        if request.url.host == "workspace.test" and failed:
            if failure == "envelope":
                return httpx.Response(200, json={"message": f"目录读取失败: {native_path}"})
            if failure == "validation":
                return httpx.Response(200, json={"data": {"workspace_id": WORKSPACE_ID, "items": native_path}})
            if failure == "transport":
                raise transport_error
            return httpx.Response(503, extensions={"reason_phrase": native_path.encode()})
        return httpx.Response(200, json={"data": {"workspace_id": workspace_id, "items": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        service = GatewayResourceCatalogService(registry=registry, http_client=client)
        result = await service.list(request_id="req_catalog_failure")
        assert result.items == []
        error, = result.errors
        assert error.scope_key == f"{target.gateway_id}/{target.workspace_id}"
        assert error.label == "本机 Gateway · 测试工作区"
        assert "catalog-marker" not in result.model_dump_json()
        expected_type = {"envelope": "ValueError", "validation": "ValidationError", "transport": "ReadError", "http-status": "HTTPStatusError"}[failure]
        assert expected_type in error.message
        if failure == "http-status":
            assert "503" in error.message
        diagnostic, = [record for record in caplog.records if record.name == "app.gateway.control.resource_catalog"]
        assert diagnostic.exc_info is not None
        original = diagnostic.exc_info[1]
        assert original is not None
        if isinstance(original, ValidationError):
            assert original.errors()[0]["input"] == native_path
        else:
            assert native_path in str(original)
        assert diagnostic.__dict__["workspace_id"] == f"{target.gateway_id}/{target.workspace_id}"
        assert diagnostic.__dict__["request_id"] == "req_catalog_failure"
        assert diagnostic.__dict__["stage"] == "gateway-resource-catalog"
        if failure == "transport":
            assert original is transport_error
        if isinstance(original, httpx.HTTPStatusError):
            assert original.response.status_code == 503
            assert original.response.extensions["reason_phrase"] == native_path.encode()
        assert {call.url.host for call in calls} == {"workspace.test", "healthy.test"}
        assert len(calls) == 2

        failed = False
        recovered = await service.list(request_id="req_catalog_failure")
        assert recovered.items == [] and recovered.errors == []
        assert len(calls) == 4


@pytest.fixture(params=["local", "remote_gateway"])
def owner_target(
    request: pytest.FixtureRequest, target: WorkspaceTarget,
    registry: GatewayWorkspaceRegistry,
) -> tuple[WorkspaceTarget, GatewayWorkspaceRegistry, str | None]:
    token = None
    if request.param == "remote_gateway":
        registry.remove(target.key)
        target = replace(target, gateway_id="gateway_" + "c" * 32, connection_kind="remote_gateway",
                         root_path=None, managed=False, owner="remote_projection", target_namespace="gateway-remote",
                         remote_gateway_connection_ids=("rgw_owner",))
        connection = RemoteGatewayConnection(
            connection_id="rgw_owner", name="远端 Gateway", host="peer.test", port=22,
            username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
            remote_gateway_id=target.gateway_id, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
        )
        registry.apply_remote_projection_snapshot(
            connection=connection, projections=(target,),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://peer.test"}),
        )
        target = registry.resolve(target.key)
        credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
            connection_id="rgw_owner", peer_gateway_id=load_proxy_gateway_id(),
        )
        token = credential.token
    return target, registry, token


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["envelope-empty", "envelope-with-data", "item-after-valid"])
@pytest.mark.parametrize("foreign_owner", [
    "00000000-0000-4000-8000-000000000099", "/private/catalog-owner-marker/state.sqlite",
    r"C:\private\catalog-owner-marker\state.sqlite", r"\\server\catalog-owner-marker\state.sqlite",
], ids=["other-workspace", "posix", "drive", "unc"])
async def test_catalog_rejects_foreign_owner_before_publishing_any_target_items(
    owner_target: tuple[WorkspaceTarget, GatewayWorkspaceRegistry, str | None], resource_record: dict[str, object],
    position: str, foreign_owner: str, caplog: pytest.LogCaptureFixture,
) -> None:
    target, registry, token = owner_target
    original_target = asdict(target)
    failed = True
    calls = []

    def request(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["X-Request-ID"] == "req_catalog_owner"
        assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
        if token:
            assert request.headers["X-BoxTeam-Federation-Token"] == token
            assert request.headers[TARGET_GATEWAY_ID_HEADER] == "gateway_" + "c" * 32
            assert request.headers["X-BoxTeam-Gateway-Id"] == load_proxy_gateway_id()
            assert "X-Local-Token" not in request.headers
        else:
            assert TARGET_GATEWAY_ID_HEADER not in request.headers
            assert "X-BoxTeam-Federation-Token" not in request.headers
        if not failed:
            data = {"workspace_id": WORKSPACE_ID, "items": [resource_record]}
        elif position == "item-after-valid":
            data = {"workspace_id": WORKSPACE_ID, "items": [resource_record, {**resource_record, "resource_id": "browser_foreign", "workspace_id": foreign_owner}]}
        else:
            data = {"workspace_id": foreign_owner, "items": [] if position == "envelope-empty" else [resource_record]}
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        service = GatewayResourceCatalogService(registry=registry, http_client=client)
        rejected = await service.list(request_id="req_catalog_owner")
        assert rejected.items == []
        error, = rejected.errors
        assert error.scope_key == f"{target.gateway_id}/{target.workspace_id}"
        assert error.message == "工作区资源目录读取失败: ValueError"
        assert "catalog-owner-marker" not in rejected.model_dump_json()
        diagnostic, = [record for record in caplog.records if record.name == "app.gateway.control.resource_catalog"]
        assert diagnostic.exc_info is not None
        assert isinstance(diagnostic.exc_info[1], ValueError)
        assert foreign_owner in str(diagnostic.exc_info[1])
        assert WORKSPACE_ID in str(diagnostic.exc_info[1])
        assert len(calls) == 1 and asdict(target) == original_target

        failed = False
        recovered = await service.list(request_id="req_catalog_owner")
        item, = recovered.items
        assert recovered.errors == [] and len(calls) == 2
        assert item.gateway_id == target.gateway_id
        assert item.gateway_connection_id == ("rgw_owner" if token else None)
        assert item.workspace_id == target.workspace_id
        assert item.resource.model_dump() == resource_record
        assert asdict(target) == original_target
