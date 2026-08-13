from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests.integration.stubs.http_stubs import generation_target_stub
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file


def _definition_payload(
    *,
    name: str,
    placement_workspace_id: str,
    execution_workspace_id: str,
) -> dict[str, object]:
    return {
        "name": name,
        "generator_type": {
            "type_id": "builtin.agent_prompt",
            "version": "1",
        },
        "enabled": True,
        "trigger": {"type": "manual", "timezone": "UTC"},
        "placement": {
            "kind": "workspace",
            "workspace_id": placement_workspace_id,
        },
        "execution_workspace_id": execution_workspace_id,
        "context_source": {"kind": "fresh"},
        "naming": {
            "title_template": "{generator.name}-{session.title}",
            "path_template": [
                "自动化",
                "{generated_at:yyyy-MM-dd}",
                "{session.title}",
            ],
        },
        "session_strategy": {
            "mode": "new_per_run",
            "concurrency": "queue",
            "report_back": "none",
        },
        "config": {
            "prompt": "只回复 E2E generator control",
            "session_title": "控制面运行",
        },
    }


async def _wait_for_completed_run(
    client: httpx.AsyncClient,
    generator_id: str,
    run_id: str,
) -> dict[str, object]:
    for _ in range(50):
        response = await client.get(
            f"/api/gateway/session-generators/{generator_id}/runs"
        )
        assert response.status_code == 200, response.text
        run = next(
            item
            for item in response.json()["data"]["items"]
            if item["run_id"] == run_id
        )
        if run["status"] == "completed":
            return run
        assert run["status"] in {"dispatching", "running", "reporting"}, run
        await asyncio.sleep(0.1)
    pytest.fail(f"生成运行未在 5 秒内完成: {run_id}")


@pytest.mark.asyncio
async def test_generator_crud_preview_and_idempotent_manual_run(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
) -> None:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    target_port = port_block.port(10)
    with generation_target_stub(
        target_port,
        output_workspace_id="gw_stub_primary",
    ) as target_state:
        gateway = start_gateway_process(
            workspace_root=workspace_root,
            default_backend_url=f"http://127.0.0.1:{target_port}",
            port=port_block.port(11),
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}",
                headers=LOCAL_TOKEN_HEADERS,
                timeout=30,
            ) as client:
                workspaces_response = await client.get("/api/gateway/workspaces")
                assert workspaces_response.status_code == 200, workspaces_response.text
                target_workspace = workspace_root / "generator-control-target"
                target_workspace.mkdir(parents=True, exist_ok=True)
                add_target_response = await client.post(
                    "/api/gateway/workspaces/local",
                    json={
                        "root_path": str(target_workspace),
                        "name": "生成器控制面替身目标",
                        "backend_url": f"http://127.0.0.1:{target_port}",
                    },
                )
                assert add_target_response.status_code == 200, add_target_response.text
                workspace_id = next(
                    item["workspace_id"]
                    for item in add_target_response.json()["data"]["items"]
                    if Path(item["root_path"]).resolve() == target_workspace
                )

                preview_response = await client.post(
                    "/api/gateway/session-generators/preview-placement",
                    json={
                        "name": "每日审查",
                        "session_title": "依赖更新",
                        "generated_at": "2026-07-23T10:11:12Z",
                        "naming": {
                            "title_template": "{generated_at:HH-mm-ss}_{session.title}",
                            "path_template": [
                                "{generator.name}",
                                "{generated_at:yyyy-MM-dd}",
                                "{session.title}",
                            ],
                        },
                    },
                )
                assert preview_response.status_code == 200, preview_response.text
                preview = preview_response.json()["data"]
                assert preview["title"] == "10-11-12_依赖更新"
                assert preview["path_segments"] == [
                    "每日审查",
                    "2026-07-23",
                    "10-11-12_依赖更新",
                ]

                invalid_preview_response = await client.post(
                    "/api/gateway/session-generators/preview-placement",
                    json={
                        "name": "非法路径",
                        "session_title": "越界",
                        "naming": {
                            "title_template": "{session.title}",
                            "path_template": [".."],
                        },
                    },
                )
                assert invalid_preview_response.status_code == 400
                assert "非法" in invalid_preview_response.text

                create_response = await client.post(
                    "/api/gateway/session-generators",
                    json=_definition_payload(
                        name="控制面 E2E",
                        placement_workspace_id=workspace_id,
                        execution_workspace_id=workspace_id,
                    ),
                )
                assert create_response.status_code == 200, create_response.text
                definition = create_response.json()["data"]
                generator_id = definition["generator_id"]
                assert definition["status"] == "ready"
                assert definition["revision"] == 1

                update_response = await client.patch(
                    f"/api/gateway/session-generators/{generator_id}",
                    json={"name": "控制面 E2E 已更新", "enabled": False},
                )
                assert update_response.status_code == 200, update_response.text
                assert update_response.json()["data"]["status"] == "paused"
                assert update_response.json()["data"]["revision"] == 2
                resume_response = await client.patch(
                    f"/api/gateway/session-generators/{generator_id}",
                    json={"enabled": True},
                )
                assert resume_response.status_code == 200, resume_response.text
                assert resume_response.json()["data"]["status"] == "ready"

                run_response = await client.post(
                    f"/api/gateway/session-generators/{generator_id}/run",
                    json={"idempotency_key": "generator-control-e2e-key"},
                )
                assert run_response.status_code == 200, run_response.text
                first_run = run_response.json()["data"]
                assert first_run["status"] in {"running", "completed"}
                assert len(first_run["outputs"]) == 1
                completed_run = await _wait_for_completed_run(
                    client,
                    generator_id,
                    first_run["run_id"],
                )
                assert completed_run["job_id"]
                assert completed_run["message_id"]
                repeated_response = await client.post(
                    f"/api/gateway/session-generators/{generator_id}/run",
                    json={"idempotency_key": "generator-control-e2e-key"},
                )
                assert repeated_response.status_code == 200, repeated_response.text
                assert repeated_response.json()["data"]["run_id"] == first_run["run_id"]
                assert repeated_response.json()["data"]["status"] == "completed"
                assert len(
                    target_state.requests_for(
                        "POST", "/api/v1/session-generations/execute"
                    )
                ) == 1

                runs_response = await client.get(
                    f"/api/gateway/session-generators/{generator_id}/runs"
                )
                assert runs_response.status_code == 200, runs_response.text
                assert [
                    item["run_id"] for item in runs_response.json()["data"]["items"]
                ] == [completed_run["run_id"]]
                list_response = await client.get("/api/gateway/session-generators")
                assert list_response.status_code == 200, list_response.text
                assert [
                    item["generator_id"]
                    for item in list_response.json()["data"]["items"]
                ] == [generator_id]

                definition_path = (
                    workspace_root
                    / ".boxteam"
                    / "gateway"
                    / "generators"
                    / f"{generator_id}.json"
                )
                run_path = (
                    workspace_root
                    / ".boxteam"
                    / "gateway"
                    / "generation-runs"
                    / generator_id
                    / f"{completed_run['run_id']}.json"
                )
                assert json.loads(definition_path.read_text(encoding="utf-8"))[
                    "generator_id"
                ] == generator_id
                assert json.loads(run_path.read_text(encoding="utf-8"))[
                    "idempotency_key"
                ] == "generator-control-e2e-key"

                delete_response = await client.delete(
                    f"/api/gateway/session-generators/{generator_id}"
                )
                assert delete_response.status_code == 200, delete_response.text
                assert not definition_path.exists()

                fork_payload = _definition_payload(
                    name="控制面分支回报 E2E",
                    placement_workspace_id=workspace_id,
                    execution_workspace_id=workspace_id,
                )
                fork_payload["session_strategy"] = {
                    "mode": "fork_new_and_report_back",
                    "target": {
                        "workspace_id": workspace_id,
                        "session_id": "ses_stub_parent",
                    },
                    "concurrency": "queue",
                    "report_back": "continue_agent",
                }
                fork_create_response = await client.post(
                    "/api/gateway/session-generators",
                    json=fork_payload,
                )
                assert fork_create_response.status_code == 200, (
                    fork_create_response.text
                )
                fork_generator_id = fork_create_response.json()["data"][
                    "generator_id"
                ]
                fork_run_response = await client.post(
                    f"/api/gateway/session-generators/{fork_generator_id}/run",
                    json={"idempotency_key": "generator-fork-report-e2e-key"},
                )
                assert fork_run_response.status_code == 200, fork_run_response.text
                fork_run = fork_run_response.json()["data"]
                completed_fork_run = await _wait_for_completed_run(
                    client,
                    fork_generator_id,
                    fork_run["run_id"],
                )
                assert completed_fork_run["report_back_job_id"] == (
                    "job_stub_report_back"
                )
                assert any(
                    request["method"] == "GET"
                    and str(request["path"]).startswith(
                        "/api/v1/session-generations/status?"
                    )
                    for request in target_state.requests
                )
        finally:
            close_gateway_process(gateway)


@pytest.mark.asyncio
async def test_generator_rejects_split_execution_then_runs_on_cross_workspace_mount(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
) -> None:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    secondary_workspace = workspace_root / "generator-secondary-workspace"
    secondary_workspace.mkdir(parents=True, exist_ok=True)
    primary_port = port_block.port(20)
    secondary_port = port_block.port(21)
    with (
        generation_target_stub(
            primary_port,
            output_workspace_id="gw_stub_primary",
        ) as primary_state,
        generation_target_stub(
            secondary_port,
            output_workspace_id="gw_stub_secondary",
        ) as secondary_state,
    ):
        gateway = start_gateway_process(
            workspace_root=workspace_root,
            default_backend_url=f"http://127.0.0.1:{primary_port}",
            port=port_block.port(22),
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}",
                headers=LOCAL_TOKEN_HEADERS,
                timeout=30,
            ) as client:
                initial_response = await client.get("/api/gateway/workspaces")
                assert initial_response.status_code == 200, initial_response.text
                primary_workspace_id = initial_response.json()["data"][
                    "active_workspace_id"
                ]
                add_response = await client.post(
                    "/api/gateway/workspaces/local",
                    json={
                        "root_path": str(secondary_workspace),
                        "name": "生成执行目标",
                        "backend_url": f"http://127.0.0.1:{secondary_port}",
                    },
                )
                assert add_response.status_code == 200, add_response.text
                secondary_workspace_id = next(
                    item["workspace_id"]
                    for item in add_response.json()["data"]["items"]
                    if Path(item["root_path"]).resolve() == secondary_workspace
                )
                create_response = await client.post(
                    "/api/gateway/session-generators",
                    json=_definition_payload(
                        name="跨工作区执行 E2E",
                        placement_workspace_id=primary_workspace_id,
                        execution_workspace_id=secondary_workspace_id,
                    ),
                )
                assert create_response.status_code == 400, create_response.text
                assert "execution_workspace_id" in create_response.text
                assert not primary_state.requests_for(
                    "POST", "/api/v1/session-generations/execute"
                )
                assert not secondary_state.requests_for(
                    "POST", "/api/v1/session-generations/execute"
                )

                valid_create_response = await client.post(
                    "/api/gateway/session-generators",
                    json=_definition_payload(
                        name="跨工作区挂载 E2E",
                        placement_workspace_id=secondary_workspace_id,
                        execution_workspace_id=secondary_workspace_id,
                    ),
                )
                assert valid_create_response.status_code == 200, (
                    valid_create_response.text
                )
                generator_id = valid_create_response.json()["data"]["generator_id"]
                run_response = await client.post(
                    f"/api/gateway/session-generators/{generator_id}/run",
                    json={"idempotency_key": "cross-workspace-execution-key"},
                )
                assert run_response.status_code == 200, run_response.text
                assert not primary_state.requests_for(
                    "POST", "/api/v1/session-generations/execute"
                )
                assert len(
                    secondary_state.requests_for(
                        "POST", "/api/v1/session-generations/execute"
                    )
                ) == 1
        finally:
            close_gateway_process(gateway)
