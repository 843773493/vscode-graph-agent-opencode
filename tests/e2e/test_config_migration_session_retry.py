from __future__ import annotations

import asyncio
import json
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.e2e.http_stubs import openai_chat_stub
from tests.e2e.ports import e2e_port_block_for_file
from tests.e2e.processes import close_backend_process, start_backend_process
from tests.e2e.utils import wait_for_job_done


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
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
) -> None:
    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(e2e_workspace_root_path).resolve()
    config_path = Path(e2e_workspace_config_path).resolve()
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
            "api_mode": "chat_completions",
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
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend.port}",
            headers={"X-Local-Token": "local-dev-token"},
            timeout=30,
        ) as client:
            session_response = await client.post(
                "/api/v1/sessions",
                json={"title": "Config Migration Retry E2E"},
            )
            assert session_response.status_code == 200, session_response.text
            session_id = session_response.json()["data"]["session_id"]
            message_response = await client.post(
                f"/api/v1/sessions/{session_id}/messages",
                json={
                    "message": {"content": "请回复 MIGRATION_RETRY_OK"},
                    "run": {"mode": "single_agent", "agent_id": "default"},
                },
            )
            assert message_response.status_code == 200, message_response.text
            accepted = message_response.json()["data"]
            await _wait_for_failed_job(client, accepted["job_id"])
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
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            for _ in range(100):
                migrated = commentjson.loads(
                    config_path.read_text(encoding="utf-8")
                )
                if migrated.get("config_version") == 4:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("等待旧配置热迁移超时")

            with openai_chat_stub(port_block.port(10)):
                replay_response = await client.post(
                    f"/api/v1/sessions/{session_id}/messages/{message_id}/replay",
                    json={
                        "action": "retry_failed",
                        "acknowledge_context_only": True,
                    },
                )
                assert replay_response.status_code == 200, replay_response.text
                replayed = replay_response.json()["data"]
                completed = await wait_for_job_done(client, replayed["job_id"])
                assert completed["status"] in {"completed", "succeeded"}
    finally:
        close_backend_process(backend)

    migrated = commentjson.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["config_version"] == 4
    assert {item.get("tool_id") for item in migrated["agents"]["default"]["tools"]["custom"]} >= {
        "read_context",
        "search_context",
    }
