from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.gateway.control.coordinator import (
    GeneratorCapabilityMissingError,
    SessionGeneratorCoordinator,
)
from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.registry import WorkspaceTarget
from app.schemas.gateway_control import (
    GenerationOutputDTO,
    GenerationRunDTO,
    GeneratorDefinitionCreateRequest,
)


@pytest.mark.parametrize("read_method", ["get", "list"])
def test_gateway_generation_run_cold_read_rejects_physical_locator(tmp_path: Path, read_method: str) -> None:
    run = GenerationRunDTO(
        run_id="grun_old", generator_id="gen_old", idempotency_key="old",
        status="completed", trigger_type="manual", scheduled_for=datetime.now(UTC),
        outputs=[GenerationOutputDTO(workspace_id="gw_old", session_id="ses_old")],
    )
    store = SessionGeneratorStore(root=tmp_path)
    store.write_run(run)
    path = tmp_path / "generation-runs/gen_old/grun_old.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["outputs"][0]["storage_relative_path"] = "2026/10/21/ses_old"
    path.write_text(json.dumps(record), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValidationError, match="storage_relative_path") as captured:
        if read_method == "get":
            store.get_run(run.generator_id, run.run_id)
        else:
            store.list_runs(run.generator_id)
    assert "2026/10/21/ses_old" not in str(captured.value)
    assert path.read_bytes() == before


class _Registry:
    def __init__(self) -> None:
        self.target = WorkspaceTarget(
            workspace_id="gw_test",
            name="测试工作区",
            root_path="/tmp/gw_test",
            backend_url="http://workspace.test",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
        )

    def resolve(self, workspace_id: str) -> WorkspaceTarget:
        if workspace_id == "gw_other":
            return WorkspaceTarget(
                workspace_id="gw_other", name="另一目标", root_path=None,
                backend_url="http://other.test", connection_kind="ssh",
                backend_workspace_id="00000000-0000-4000-8000-000000000002",
            )
        if workspace_id != self.target.workspace_id:
            raise KeyError(f"未知工作区: {workspace_id}")
        return self.target


class _UnusedStore:
    pass


def _definition(**updates: object) -> GeneratorDefinitionCreateRequest:
    payload: dict[str, object] = {
        "name": "定位器校验",
        "placement": {"kind": "workspace", "workspace_id": "gw_test"},
        "execution_workspace_id": "gw_test",
        "context_source": {"kind": "fresh"},
        "session_strategy": {
            "mode": "new_per_run",
            "concurrency": "queue",
            "report_back": "none",
        },
        "config": {"prompt": "执行"},
    }
    payload.update(updates)
    return GeneratorDefinitionCreateRequest.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_workspace", ["gw_test", "gw_other"])
async def test_definition_validation_resolves_each_route_identity(source_workspace: str) -> None:
    registry = _Registry()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {
            "workspace_id": registry.target.backend_workspace_id,
            "provider_available": True,
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        coordinator = SessionGeneratorCoordinator(
            registry=registry,  # type: ignore[arg-type]
            store=_UnusedStore(),  # type: ignore[arg-type]
            http_client=client,
        )
        definition = _definition(
            placement={"kind": "session", "workspace_id": "gw_test", "session_id": "ses_shared"},
            context_source={"kind": "live_session", "workspace_id": source_workspace, "session_id": "ses_shared", "thread_id": "thr_shared"},
            session_strategy={
                "mode": "continue_existing",
                "target": {"workspace_id": "gw_test", "session_id": "ses_shared", "thread_id": "thr_shared"},
            },
        )
        await coordinator.validate_definition_capability(definition, request_id="req_definition")
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v1/session-generations/validate-definition"
    assert request.headers["X-Request-ID"] == "req_definition"
    assert request.headers["X-BoxTeam-Workspace-Id"] == registry.target.backend_workspace_id
    payload = json.loads(request.content)
    assert payload["execution_workspace_id"] == registry.target.backend_workspace_id
    assert payload["placement"]["workspace_id"] == registry.target.backend_workspace_id
    assert payload["context_source"]["workspace_id"] == registry.resolve(source_workspace).backend_workspace_id
    assert payload["session_strategy"]["target"]["workspace_id"] == registry.target.backend_workspace_id
    assert payload["session_strategy"]["target"]["thread_id"] == "thr_shared"
    assert definition.execution_workspace_id == "gw_test"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "data", "error_type"), [
    (400, {"detail": "invalid config"}, ValueError),
    (404, {"detail": "missing thread"}, KeyError),
    (503, {"detail": "backend unavailable"}, httpx.HTTPStatusError),
    (200, {"data": {"workspace_id": "00000000-0000-4000-8000-000000000001", "provider_available": False, "reason": "missing provider"}}, GeneratorCapabilityMissingError),
])
async def test_definition_validation_preserves_error_category(status: int, data: dict[str, object], error_type: type[Exception]) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=data))) as client:
        coordinator = SessionGeneratorCoordinator(
            registry=_Registry(),  # type: ignore[arg-type]
            store=_UnusedStore(),  # type: ignore[arg-type]
            http_client=client,
        )
        with pytest.raises(error_type):
            await coordinator.validate_definition_capability(_definition(), request_id="req_error")
