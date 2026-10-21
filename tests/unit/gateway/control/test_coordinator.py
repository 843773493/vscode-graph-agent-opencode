from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.gateway.control.coordinator import SessionGeneratorCoordinator
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


def _response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/session-generations/capabilities":
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "type_id": "builtin.agent_prompt",
                            "supported_versions": ["1"],
                            "config_schema": {
                                "type": "object",
                                "properties": {"prompt": {"type": "string"}},
                                "required": ["prompt"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                }
            },
        )
    node_id = request.url.path.rsplit("/", maxsplit=1)[-1]
    kind = {
        "fld_wrong": "folder",
        "ses_shared": "session",
    }[node_id]
    return httpx.Response(
        200,
        json={
            "data": {
                "revision": "rev_test",
                "items": [{"node_id": node_id, "kind": kind, "name": node_id}],
            }
        },
    )


@pytest.mark.asyncio
async def test_capability_validation_rejects_folder_used_as_session() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_response)) as client:
        coordinator = SessionGeneratorCoordinator(
            registry=_Registry(),  # type: ignore[arg-type]
            store=_UnusedStore(),  # type: ignore[arg-type]
            http_client=client,
        )
        definition = _definition(
            placement={
                "kind": "session",
                "workspace_id": "gw_test",
                "session_id": "fld_wrong",
            }
        )

        with pytest.raises(ValueError, match="节点类型错误.*actual=folder"):
            await coordinator.validate_definition_capability(
                definition,
                request_id="req_wrong_kind",
            )


@pytest.mark.asyncio
async def test_duplicate_session_locators_share_one_catalog_probe() -> None:
    requests: list[str] = []

    def recording_response(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return _response(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(recording_response)
    ) as client:
        coordinator = SessionGeneratorCoordinator(
            registry=_Registry(),  # type: ignore[arg-type]
            store=_UnusedStore(),  # type: ignore[arg-type]
            http_client=client,
        )
        definition = _definition(
            placement={
                "kind": "session",
                "workspace_id": "gw_test",
                "session_id": "ses_shared",
            },
            context_source={
                "kind": "live_session",
                "workspace_id": "gw_test",
                "session_id": "ses_shared",
                "thread_id": "thr_shared",
            },
            session_strategy={
                "mode": "continue_existing",
                "target": {
                    "workspace_id": "gw_test",
                    "session_id": "ses_shared",
                    "thread_id": "thr_shared",
                },
                "concurrency": "queue",
                "report_back": "none",
            },
        )

        await coordinator.validate_definition_capability(
            definition,
            request_id="req_deduplicate",
        )

    assert requests.count(
        "/api/v1/session-catalog/breadcrumb/ses_shared"
    ) == 1
