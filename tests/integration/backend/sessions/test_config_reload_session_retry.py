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
from tests.support.api_helpers import with_message_run_operation_id
from tests.support.api_waiters import wait_for_job_done
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_ports_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
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
        context.artifacts_dir,
        runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="failed-session-retry-after-model-configuration-reload",
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
async def test_persisted_failed_session_retries_after_model_configuration_reload(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    retry_model: ModelStreamReplay,
) -> None:
    port_block = integration_ports_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    config_path = Path(integration_workspace_config_path).resolve()
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    primary_provider = config["llm"]["providers"]["primary"]
    primary_provider["defaults"] = {
        "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
        "api_key": "", "custom_llm_provider": "openai",
        "api_mode": primary_provider["defaults"]["api_mode"],
    }
    primary_provider["models"]["default"]["model"] = "e2e-stub-model"
    config["agents"]["default"]["model"] = {
        "primary": {"provider_id": "primary", "model_id": "default"},
        "fallbacks": [],
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    backend = start_backend_process(
        workspace_root=str(workspace_root),
        port=port_block.port(0),
        log_name="config-reload-session-retry",
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
            session_response = await client.post(
                "/api/v1/sessions",
                json={"title": "Config Reload Retry Integration"},
            )
            assert session_response.status_code == 200, session_response.text
            session_id = session_response.json()["data"]["session_id"]
            main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert main.status_code == 200, main.text
            thread_id = main.json()["data"]["thread_id"]
            # 首次调用明确失败；两次请求按真实 owner 匹配，不用端口启停制造模型状态。
            interactions = [build_cassette(
                asset_id="config-reload-retry", model=model_name,
                url=f"http://127.0.0.1:{port_block.port(10)}/v1/chat/completions",
                status=status,
                frames=chat_completion_frames(
                    response_id=f"chatcmpl-reload-{status}", model=model_name,
                    deltas=({"role": "assistant", "content": text},), finish_reason="stop",
                ),
            ).raw["interactions"][0] for status, model_name, text in (
                (400, "e2e-stub-model", "config-reload-initial-rejection"),
                (200, "e2e-retry-model", "CONFIG_RELOAD_RETRY_OK"),
            )]
            call_ids = retry_model.control.plan_model_calls(session_id, thread_id, interactions)
            message_response = await client.post(
                f"/api/v1/sessions/{session_id}/messages",
                json=with_message_run_operation_id({
                    "message": {"content": "请回复 CONFIG_RELOAD_RETRY_OK"},
                    "run": {"mode": "single_agent", "agent_id": "default"},
                }),
            )
            assert message_response.status_code == 200, message_response.text
            accepted = message_response.json()["data"]
            failed = await _wait_for_failed_job(client, accepted["job_id"])
            assert failed["status"] == "failed"
            assert failed["error_message"].startswith("模型执行失败: stage=execution-provider, error=")
            message_id = accepted["message_id"]

            config = commentjson.loads(config_path.read_text(encoding="utf-8"))
            config["llm"]["providers"]["primary"]["models"]["default"]["model"] = "e2e-retry-model"
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            for _ in range(100):
                current = await client.get("/api/v1/agents/default")
                assert current.status_code == 200, current.text
                if current.json()["data"]["models"][0]["model"] == "e2e-retry-model":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("等待当前格式模型配置生效超时")

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
            assert any(message["role"] == "assistant" and message["content"] == "CONFIG_RELOAD_RETRY_OK"
                       for message in history.json()["data"]["items"])
            for call_id, model_name in zip(call_ids, ("e2e-stub-model", "e2e-retry-model"), strict=True):
                record = json.loads((retry_model.artifacts / f"{call_id}.json").read_text(encoding="utf-8"))
                assert record["identity"] == {
                    "session_id": session_id, "thread_id": thread_id, "model_call_id": call_id,
                }
                assert record["request"]["stream"] is True
                assert record["request"]["model"] == model_name
            with retry_model.control._condition:
                assert sum(count for namespace, count in retry_model.control._counters.items()
                           if namespace.startswith("model_call/")) == 2
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        close_backend_process(backend)

    saved = commentjson.loads(config_path.read_text(encoding="utf-8"))
    assert saved["config_version"] == config["config_version"]
    assert saved["llm"]["providers"]["primary"]["models"]["default"]["model"] == "e2e-retry-model"
