import os
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException, Request

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.managed_workspaces import create_direct_managed_workspace
from app.gateway.routes._shared import (
    _directory_listing,
    _scan_local_directories,
    resolve_management_directory,
)
from app.gateway.routes.workspaces_lifecycle import list_local_directories
from app.schemas.gateway.directory import GatewayDirectoryListDTO


@pytest.fixture
def directory_request(tmp_path: Path):
    app = FastAPI()
    app.state.gateway_id = "gateway_00000000000070008000000000000001"
    app.state.gateway_state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    yield Request({"type": "http", "app": app})
    app.state.gateway_state.close()


@pytest.fixture
def directory_root(tmp_path: Path) -> Path:
    root = tmp_path / "目录 % Ω"
    root.mkdir()
    (root / "Zulu").mkdir()
    (root / "alpha").mkdir()
    (root / "README.md").write_text("not a directory", encoding="utf-8")
    return root


def test_scan_local_directories_only_returns_sorted_directories(directory_root: Path) -> None:
    entries, truncated = _scan_local_directories(directory_root, limit=120)
    assert [entry.name for entry in entries] == ["alpha", "Zulu"]
    assert truncated is False


def test_scan_local_directories_reports_truncation(directory_root: Path) -> None:
    entries, truncated = _scan_local_directories(directory_root, limit=1)
    assert len(entries) == 1
    assert truncated is True


@pytest.mark.asyncio
async def test_directory_uri_native_and_relative_input_share_target_owner(
    directory_request: Request, directory_root: Path,
) -> None:
    listing = await _directory_listing(directory_request, str(directory_root), limit=120)
    assert listing.name == directory_root.name
    assert str(directory_root.parent) not in listing.model_dump_json()
    for uri in [listing.path, listing.home_path, listing.parent_path, *(entry.path for entry in listing.entries)]:
        parsed = parse_resource_uri(uri)
        assert parsed.kind == "files" and parsed.scope_id == directory_request.app.state.gateway_id
    reopened = await _directory_listing(directory_request, listing.path, limit=120)
    relative = await _directory_listing(directory_request, "alpha", base_path=listing.path, limit=120)
    absolute = await _directory_listing(directory_request, str(directory_root / "alpha"), limit=120)
    assert reopened == listing
    assert relative == absolute
    assert resolve_management_directory(directory_request, relative.path) == directory_root / "alpha"
    parent = await _directory_listing(directory_request, listing.parent_path, limit=120)
    assert next(entry.path for entry in parent.entries if entry.name == listing.name) == listing.path
    assert not (directory_root / ".boxteam").exists()
    state = directory_request.app.state.gateway_state
    state_path = state.path
    state.close()
    directory_request.app.state.gateway_state = GatewayStateStore(path=state_path)
    assert resolve_management_directory(directory_request, listing.path) == directory_root


@pytest.mark.asyncio
async def test_absolute_input_is_never_reinterpreted_as_workspace_relative(
    directory_request: Request, directory_root: Path,
) -> None:
    external = directory_root.parent / "outside"
    external.mkdir()
    (external / "real-target").mkdir()
    decoy = directory_root / str(external).lstrip("/")
    decoy.mkdir(parents=True)
    (decoy / "wrong-target").mkdir()
    base = await _directory_listing(directory_request, str(directory_root), limit=120)
    result = await _directory_listing(directory_request, str(external), base_path=base.path, limit=120)
    assert [entry.name for entry in result.entries] == ["real-target"]
    assert resolve_management_directory(directory_request, result.path) == external


@pytest.mark.asyncio
async def test_directory_selection_rejects_foreign_owner_and_implicit_relative_base(
    directory_request: Request, directory_root: Path,
) -> None:
    listing = await _directory_listing(directory_request, str(directory_root), limit=120)
    foreign = listing.path.replace(directory_request.app.state.gateway_id, "gateway_00000000000070008000000000000002")
    with pytest.raises(ValueError, match="不属于"):
        resolve_management_directory(directory_request, foreign)
    with pytest.raises(ValueError, match="相对路径"):
        resolve_management_directory(directory_request, "alpha")
    if os.name != "nt":
        with pytest.raises(ValueError):
            resolve_management_directory(directory_request, r"C:\projects\alpha", base_path=listing.path)
    with pytest.raises(HTTPException) as error:
        await _directory_listing(directory_request, str(directory_root / "missing"), limit=120)
    assert str(directory_root) not in str(error.value.detail)


@pytest.mark.asyncio
async def test_remote_gateway_directory_browser_delegates_to_federation_api(
    monkeypatch: pytest.MonkeyPatch, directory_request: Request,
) -> None:
    class FakeRegistry:
        def remote_gateway_url(self, connection_id: str) -> str:
            assert connection_id == "rgw_remote"
            return "http://remote-gateway.test"

    uri = resource_display_uri(scope="gateway", scope_id="gateway_00000000000070008000000000000002",
                               kind="files", tail_segments=("00000000000070008000000000000003",))
    data = {"name": "projects", "path": uri, "parent_path": uri, "home_path": uri,
            "entries": [{"name": "alpha project", "path": uri + "/alpha%20project"}],
            "truncated": False, "limit": 25}
    request_remote = AsyncMock(return_value=data)
    monkeypatch.setattr("app.gateway.routes.workspaces_lifecycle.request_remote_gateway_management", request_remote)
    monkeypatch.setattr("app.gateway.routes.workspaces_lifecycle._remote_gateway_credential",
                        lambda connection_id: f"token-for-{connection_id}")
    response = await list_local_directories(
        request=directory_request, path=r"C:\projects", base_path=uri, limit=25,
        gateway_connection_id="rgw_remote", _="local-token", request_id="req_remote_directory",
        registry=FakeRegistry(),  # type: ignore[arg-type]
    )
    assert response.data == GatewayDirectoryListDTO.model_validate(data)
    assert request_remote.await_args.kwargs["path"].startswith("/api/gateway/federation/directories?")
    assert parse_qs(urlsplit(request_remote.await_args.kwargs["path"]).query) == {
        "path": [r"C:\projects"], "base_path": [uri], "limit": ["25"],
    }
    assert request_remote.await_args.kwargs["request_id"] == "req_remote_directory"


def test_directory_receipt_rejects_native_response_paths() -> None:
    with pytest.raises(ValueError):
        GatewayDirectoryListDTO(name="legacy", path="/srv/projects", home_path="/home/user", entries=[], limit=25)


@pytest.mark.asyncio
async def test_registration_http_consumes_selected_directory_uri(
    directory_request: Request, directory_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from app.core.trace_middleware import get_request_id
    from app.gateway.auth import verify_gateway_token
    from app.gateway.routes._shared import get_registry
    from app.gateway.routes.workspaces_lifecycle import router

    listing = await _directory_listing(directory_request, str(directory_root), limit=120)
    app = directory_request.app
    registry = AsyncMock()
    registry.active_workspace_id = None
    registry.list_dtos.return_value = []
    app.include_router(router)
    app.dependency_overrides[verify_gateway_token] = lambda: "local-token"
    app.dependency_overrides[get_request_id] = lambda: "req-register-uri"
    app.dependency_overrides[get_registry] = lambda: registry
    create = AsyncMock()
    monkeypatch.setattr("app.gateway.routes.workspaces_lifecycle.create_direct_managed_workspace", create)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        response = await client.post("/api/gateway/workspaces/local", json={"root_path": listing.path})
        assert response.status_code == 200, response.text
        assert create.await_args.kwargs["root_path"] == str(directory_root)
        assert create.await_args.kwargs["create_directory"] is False
        foreign = listing.path.replace(app.state.gateway_id, "gateway_00000000000070008000000000000002")
        rejected = await client.post("/api/gateway/workspaces/local", json={"root_path": foreign})
        assert rejected.status_code == 400
        assert create.await_count == 1
        gone = directory_root / "gone"
        gone.mkdir()
        selected = await _directory_listing(directory_request, str(gone), limit=120)
        gone.rmdir()
        failed = await client.post("/api/gateway/workspaces/local", json={"root_path": selected.path})
        assert failed.status_code == 400 and str(directory_root) not in failed.text
        assert create.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("federated", [False, True])
async def test_managed_registration_http_uses_target_directory_binding(
    directory_request: Request, directory_root: Path, monkeypatch: pytest.MonkeyPatch, federated: bool,
) -> None:
    import httpx

    from app.core.trace_middleware import get_request_id
    from app.gateway.auth import verify_federation_token, verify_gateway_token
    from app.gateway.routes import federation, workspaces_managed
    from app.gateway.routes._shared import get_registry
    from app.gateway.server.port_forwarding import get_port_forward_manager
    from app.schemas.gateway.workspaces import GatewayManagedWorkspaceListDTO

    listing = await _directory_listing(directory_request, str(directory_root), limit=120)
    module = federation if federated else workspaces_managed
    app = directory_request.app
    app.include_router(module.router)
    app.dependency_overrides[verify_gateway_token] = lambda: "local-token"
    app.dependency_overrides[verify_federation_token] = lambda: "federation-token"
    app.dependency_overrides[federation.require_managed_workspace_admin] = lambda: None
    app.dependency_overrides[get_request_id] = lambda: "req-managed-uri"
    app.dependency_overrides[get_registry] = lambda: AsyncMock()
    app.dependency_overrides[get_port_forward_manager] = lambda: AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(module, "create_direct_managed_workspace", create)
    monkeypatch.setattr(module, "_managed_workspace_list", AsyncMock(return_value=GatewayManagedWorkspaceListDTO(
        gateway_id=app.state.gateway_id, gateway_name="target", connection_kind="local", items=[],
    )))
    path = "/api/gateway/federation/managed-workspaces" if federated else "/api/gateway/managed-workspaces"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        for value in [listing.path, str(directory_root)]:
            response = await client.post(path, json={"root_path": value, "create_directory": False})
            assert response.status_code == 200, response.text
            assert create.await_args.kwargs["root_path"] == str(directory_root)
        foreign = listing.path.replace(app.state.gateway_id, "gateway_00000000000070008000000000000002")
        rejected = await client.post(path, json={"root_path": foreign})
        assert rejected.status_code == 400 and create.await_count == 2
        monkeypatch.setattr(module, "create_direct_managed_workspace", create_direct_managed_workspace)
        gone = directory_root / "gone"
        gone.mkdir()
        selected = await _directory_listing(directory_request, str(gone), limit=120)
        gone.rmdir()
        for value in (str(gone), selected.path):
            failed = await client.post(path, json={"root_path": value, "create_directory": False})
            assert failed.status_code == 400, failed.text
            assert "目录不存在" in failed.text and str(directory_root) not in failed.text
        gone.write_text("目录在选中后被替换为文件", encoding="utf-8")
        failed = await client.post(path, json={"root_path": selected.path, "create_directory": False})
        assert failed.status_code == 400, failed.text
        assert "不是目录" in failed.text and str(directory_root) not in failed.text
