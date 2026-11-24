"""已发布选项经真实配置 owner 和 HTTP 序列化读取，不走草稿预检。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import config as config_api
from app.api.deps import get_config_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.config import ConfigSnapshot
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace

GATEWAY = "gateway_" + "b" * 32


@dataclass
class InspectionCase:
    service: ConfigService
    client: AsyncClient


@pytest.fixture
async def inspection_case(request: pytest.FixtureRequest) -> AsyncIterator[InspectionCase]:
    scope = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_test_workspace(workspace_root=scope.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",))
    config_root = scope.boxteam_home_for_node(request.node.nodeid) / "config"
    install_user_configuration(config_root=config_root, profile="default", workspace_root=workspace)
    with WorkspaceStateStore(workspace_root=workspace) as store:
        service = ConfigService(config_dir=config_root, config_path=config_root / "workspace.jsonc",
            workspace_root=workspace, workspace_state_store=store)
        try:
            service.validate_workspace_config()
            app = FastAPI()
            app.add_middleware(TraceMiddleware)
            app.include_router(config_api.router, prefix="/api/v1")
            app.dependency_overrides[verify_local_token] = lambda: "test-token"
            app.dependency_overrides[get_config_service] = lambda: service
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-BoxTeam-Gateway-Id": GATEWAY, "X-Request-ID": "active-inspection"}) as client:
                yield InspectionCase(service, client)
        finally:
            await service.close()


async def test_active_options_are_frozen_without_source_reads_or_credential_expansion(
    inspection_case: InspectionCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = inspection_case
    snapshot = case.service.get_active_snapshot()
    document = case.service.get_source_document(source_key="workspace_root_mutable_override", gateway_id=GATEWAY)
    snapshot.graph.entry.path.write_text("{invalid on-disk source", encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("active inspection 不得读取文件、解密或复制整份工作区配置")

    source_files = set(snapshot.graph.source_locations.values())
    original_open = Path.open

    def guard_source_open(path: Path, *args: object, **kwargs: object):
        if path.resolve() in source_files:
            forbidden()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard_source_open)
    monkeypatch.setattr(ConfigSnapshot, "to_dict", forbidden)
    monkeypatch.setattr(case.service, "prepare_llm_credentials", forbidden)
    monkeypatch.setattr(case.service._llm_resolution, "_expand_model_credentials", forbidden)
    for _ in range(2):
        response = await case.client.get("/api/v1/config/llm/inspection",
            params={"provider_id": "primary", "model_id": "default"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["request_id"] == response.headers["X-Request-ID"] == "active-inspection"
        assert response.headers["Cache-Control"] == "no-store"
        data = body["data"]
        assert data["gateway_id"] == GATEWAY
        assert data["workspace_id"] == snapshot.graph.owner_id
        assert data["active_digest"] == snapshot.revision and data["active_revision"] > 0
        assert data["source_baseline"] == {key: value.model_dump() for key, value in document.source_baseline.items()}
        assert data["model_options"]["model_ref"] == {"provider_id": "primary", "model_id": "default"}
        assert data["provider_options"]["provider_id"] == "primary"
        assert {node["source_key"] for node in data["model_options"]["chain"]} <= data["source_baseline"].keys()
        assert "document" not in data and "candidate" not in data


async def test_equal_digest_with_different_frozen_source_identity_is_rejected(
    inspection_case: InspectionCase,
) -> None:
    service = inspection_case.service
    original = service.get_active_snapshot()
    source = original.source_details[0]
    changed = replace(source, source_generation=(source.source_generation or 0) + 1)
    divergent = replace(original, source_details=(changed, *original.source_details[1:]))
    assert divergent.revision == original.revision
    with service.publication_lock:
        service._snapshot_store._snapshot = divergent
        try:
            with pytest.raises(ConfigConflictError, match="来源身份"):
                service.get_active_options_inspection(gateway_id=GATEWAY, provider_id="primary")
        finally:
            service._snapshot_store._snapshot = original


async def test_unknown_model_is_rejected_without_substituting_another_model(inspection_case: InspectionCase) -> None:
    response = await inspection_case.client.get("/api/v1/config/llm/inspection",
        params={"provider_id": "primary", "model_id": "missing"})
    assert response.status_code == 422
    assert "missing" in response.json()["detail"]


async def test_options_projection_does_not_hold_the_event_loop_or_publication_lock(
    inspection_case: InspectionCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = inspection_case
    entered, release = Event(), Event()
    inspect = case.service._llm_resolution.inspect_model_reference

    def delayed(*args: object, **kwargs: object):
        entered.set()
        assert release.wait(5), "测试未能在选项投影期间继续读取已发布快照"
        return inspect(*args, **kwargs)

    monkeypatch.setattr(case.service._llm_resolution, "inspect_model_reference", delayed)
    pending = asyncio.create_task(case.client.get("/api/v1/config/llm/inspection",
        params={"provider_id": "primary", "model_id": "default"}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert not pending.done(), "选项投影不能阻塞整个事件循环"
        snapshot = await asyncio.wait_for(asyncio.to_thread(case.service.get_active_snapshot), timeout=1)
        assert snapshot.revision
    finally:
        release.set()
        response = await pending
    assert response.status_code == 200, response.text
    assert response.json()["data"]["active_digest"] == snapshot.revision
