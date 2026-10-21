from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import commentjson
import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.support.api_waiters import wait_for_job_done
from tests.support.model_stream_config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
def retry_model(request: pytest.FixtureRequest) -> Iterator[ModelStreamReplay]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with model_stream_replay(
        context.output_root,
        runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="failed-session-retry-after-current-tools-migration",
    ) as replay:
        yield replay


async def _wait_for_failed_job(
    client: httpx.AsyncClient,
    job_id: str,
) -> dict[str, object]:
    for _ in range(60):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()["data"]
        if job["status"] in {"failed", "timed_out"}:
            return job
        if job["status"] in {"completed", "succeeded", "cancelled"}:
            pytest.fail(f"预期 Job 失败，实际状态: {job['status']}")
        await asyncio.sleep(0.1)
    pytest.fail(f"等待失败 Job 超时: {job_id}")


@pytest.mark.asyncio
async def test_persisted_failed_session_retries_with_migrated_current_tools(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    retry_model: ModelStreamReplay,
) -> None:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    config_path = Path(integration_workspace_config_path).resolve()
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    primary_provider = next(
        provider
        for provider in config["llm"]["providers"]
        if provider["id"] == "primary"
    )
    primary_provider.update(
        {
            "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
            "model": "e2e-stub-model",
            "api_key": "e2e-local-model-key",
            "custom_llm_provider": "openai",
        }
    )
    config["agents"]["default"]["model"] = {
        "primary_provider": "primary",
        "fallback_providers": [],
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    backend = start_backend_process(
        workspace_root=str(workspace_root),
        port=port_block.port(0),
        log_name="config-migration-session-retry",
        asgi_app="tests.support.e2e_control.backend_app:app",
        env_overrides={
            **retry_model.environment,
            "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{port_block.port(1)}",
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        },
    )
    gateway = None
    try:
        gateway = await asyncio.to_thread(
            start_gateway_process, workspace_root=workspace_root, port=port_block.port(1),
            default_backend_url=f"http://127.0.0.1:{backend.port}",
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers={**LOCAL_TOKEN_HEADERS, "X-Workspace-ID": backend.workspace_id},
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            session_response = await client.post(
                "/api/v1/sessions",
                json={"title": "Config Migration Retry E2E"},
            )
            assert session_response.status_code == 200, session_response.text
            session_id = session_response.json()["data"]["session_id"]
            main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert main.status_code == 200, main.text
            thread_id = main.json()["data"]["thread_id"]
            # 首次调用明确失败；两次请求按真实 owner 匹配，不用端口启停制造模型状态。
            interactions = [build_cassette(
                asset_id="config-migration-retry", model="e2e-stub-model",
                url=f"http://127.0.0.1:{port_block.port(10)}/v1/chat/completions",
                status=status,
                frames=chat_completion_frames(
                    response_id=f"chatcmpl-migration-{status}", model="e2e-stub-model",
                    deltas=({"role": "assistant", "content": text},), finish_reason="stop",
                ),
            ).raw["interactions"][0] for status, text in (
                (400, "config-migration-initial-rejection"), (200, "MIGRATION_RETRY_OK"),
            )]
            call_ids = retry_model.control.plan_model_calls(session_id, thread_id, interactions)
            message_response = await client.post(
                f"/api/v1/sessions/{session_id}/messages",
                json={
                    "message": {"content": "请回复 MIGRATION_RETRY_OK"},
                    "run": {"mode": "single_agent", "agent_id": "default"},
                },
            )
            assert message_response.status_code == 200, message_response.text
            accepted = message_response.json()["data"]
            failed = await _wait_for_failed_job(client, accepted["job_id"])
            assert failed["status"] == "failed"
            assert "config-migration-initial-rejection" in failed["error_message"]
            message_id = accepted["message_id"]

            config = commentjson.loads(config_path.read_text(encoding="utf-8"))
            config.pop("config_version")
            config["agents"]["default"]["tools"]["custom"] = [
                (
                    {
                        "name": "read_context",
                        "factory": (
                            "app.agents.tools.session_history:"
                            "create_read_session_recent_text_messages_tool"
                        ),
                    }
                    if item.get("tool_id") == "read_context"
                    else item
                )
                for item in config["agents"]["default"]["tools"]["custom"]
            ]
            # 旧单域配置从 boxteam.jsonc 迁入 Workspace 域 v1；现行 workspace.jsonc
            # 不再采用旧单域的 v4 版本，也不接受已经移除的 factory。
            legacy_path = config_path.with_name("boxteam.jsonc")
            legacy_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            config_path.unlink()
            for _ in range(100):
                if config_path.is_file() and not legacy_path.exists():
                    migrated = commentjson.loads(config_path.read_text(encoding="utf-8"))
                    if migrated["config_version"] == 1:
                        break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("等待旧配置热迁移超时")

            replay_response = await client.post(
                f"/api/v1/sessions/{session_id}/messages/{message_id}/replay",
                json={"action": "retry_failed", "acknowledge_context_only": True},
            )
            assert replay_response.status_code == 200, replay_response.text
            replayed = replay_response.json()["data"]
            assert replayed["job_id"] != accepted["job_id"]
            completed = await wait_for_job_done(client, replayed["job_id"])
            assert completed["status"] in {"completed", "succeeded"}
            history = await client.get(f"/api/v1/sessions/{session_id}/messages")
            assert history.status_code == 200, history.text
            assert any(message["role"] == "assistant" and message["content"] == "MIGRATION_RETRY_OK"
                       for message in history.json()["data"]["items"])
            for call_id in call_ids:
                record = json.loads((retry_model.artifacts / f"{call_id}.json").read_text(encoding="utf-8"))
                assert record["identity"] == {
                    "session_id": session_id, "thread_id": thread_id, "model_call_id": call_id,
                }
                assert record["request"]["stream"] is True
            with retry_model.control._condition:
                assert sum(count for namespace, count in retry_model.control._counters.items()
                           if namespace.startswith("model_call/")) == 2
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        close_backend_process(backend)

    migrated = commentjson.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["config_version"] == 1
    assert not legacy_path.exists()
    assert {item.get("tool_id") for item in migrated["agents"]["default"]["tools"]["custom"]} >= {
        "read_context",
        "search_context",
    }
