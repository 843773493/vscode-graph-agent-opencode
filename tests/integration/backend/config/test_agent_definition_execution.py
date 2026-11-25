"""来源删除不扫描历史、不改绑会话，并保留已开始执行的配置快照。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import main
from app.schemas.internal_v2.common import JobStatus
from app.services.infrastructure.config.source_document import parse_source_text
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    _plan_model_call,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414 - 工作区及产物沿当前测试路径。
)
from tests.support.api_helpers import with_message_run_operation_id
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


async def test_removed_agent_keeps_running_snapshot_and_requires_explicit_session_choice(
    mounted_case: MountedCase, inprocess_model_replay, monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay, controller = inprocess_model_replay
    entered, release = asyncio.Event(), asyncio.Event()
    planned: list[str] = []

    async def observe_model_response(call_id: str, phase: str) -> None:
        assert phase == "model.response_ready" and call_id in planned
        if call_id == planned[0]:
            entered.set()
            await release.wait()

    monkeypatch.setattr("app.testing.model_stream.transport.observe_phase", observe_model_response)
    async with main.lifespan(main.app) as lifespan:
        await lifespan["after_http_startup"]()
        container = mounted_case.containers[0]
        await container.config_service.stop_watching()
        base = f"/api/v1/workspaces/{container.workspace_id}"
        source = base + "/config/sources/workspace_root_mutable_override"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://workspace.test", headers=LOCAL_TOKEN_HEADERS) as client:
            async def edit_agent(*, remove: bool, key: str) -> None:
                # 此进程内场景没有 Gateway；来源协议的发起身份不能冒充执行的 Gateway 绑定。
                headers = {"X-BoxTeam-Gateway-Id": "gateway_" + "8" * 32}
                response = await client.get(source, headers=headers)
                assert response.status_code == 200, response.text
                document = response.json()["data"]
                payload = parse_source_text(document["jsonc"])
                agents = payload["settings"]["workspace"].setdefault("agents", {})
                if remove:
                    agents.pop("source_agent")
                else:
                    agents["source_agent"] = {
                        "name": "执行中的来源 Agent",
                        "instructions": {"system_prompt": "source-agent-frozen-prompt"},
                        "model": {"primary": {"provider_id": "primary", "model_id": "default"}, "fallbacks": []},
                    }
                request = {name: document[name] for name in ("gateway_id", "workspace_id", "source_key")}
                request.update({
                    "jsonc": json.dumps(payload, ensure_ascii=False), "idempotency_key": key,
                    "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
                    "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
                    "expected_active_digest": document["active_digest"],
                })
                preflight = await client.post(source + "/preflight", json=request, headers=headers)
                assert preflight.status_code == 200, preflight.text
                saved = await client.put(source, json=request, headers=headers)
                assert saved.status_code == 200, saved.text
                assert saved.headers["X-Request-ID"] == saved.json()["request_id"]
                assert saved.json()["data"]["edit"]["state"] == "active"

            await edit_agent(remove=False, key="create-source-agent")
            created = await client.post(base + "/sessions", json={"title": "删除定义不改绑历史", "agent_id": "source_agent"})
            assert created.status_code == 200, created.text
            session = created.json()["data"]
            session_url = base + f'/sessions/{session["session_id"]}'
            frozen = container.config_service.get_snapshot()
            planned.append(_plan_model_call(replay, container, session["session_id"], session["thread_id"]))
            accepted = await client.post(session_url + "/messages", json=with_message_run_operation_id({
                "message": {"content": "保持已经开始的执行"}, "run": {},
            }))
            assert accepted.status_code == 200, accepted.text
            job = container.job_service._jobs[accepted.json()["data"]["job_id"]]
            try:
                await asyncio.wait_for(entered.wait(), 30)
                await edit_agent(remove=True, key="remove-source-agent")
                assert "source_agent" not in container.config_service.list_agents()
                current = await client.get(session_url)
                assert current.status_code == 200, current.text
                assert current.json()["data"]["current_agent_id"] == "source_agent"
                with container.config_service.use_snapshot(frozen):
                    assert container.config_service.get_agent_runtime_config("source_agent")["system_prompt"] == "source-agent-frozen-prompt"
            finally:
                release.set()
                assert job.task is not None
                await asyncio.wait_for(asyncio.shield(job.task), 30)
                await container.job_service.drain_finalizations()
            assert job.status == JobStatus.completed, job.error_message
            assert job.agent_id == "source_agent" and controller.transport.call_count == 1
            capture = json.loads((replay.artifacts / (planned[0] + ".json")).read_text(encoding="utf-8"))
            assert "source-agent-frozen-prompt" in json.dumps(capture["request"]["messages"])

            rejected = await client.post(session_url + "/messages", json=with_message_run_operation_id({
                "message": {"content": "不能偷偷使用默认 Agent"}, "run": {},
            }))
            assert rejected.status_code == 400, rejected.text
            assert "agent source_agent 不存在" in rejected.text
            assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"]
            assert controller.transport.call_count == 1
            assert (await client.get(session_url)).json()["data"]["current_agent_id"] == "source_agent"

            selected = await client.patch(session_url, json={"agent_id": "default"})
            assert selected.status_code == 200, selected.text
            assert selected.json()["data"]["current_agent_id"] == "default"
            planned.append(_plan_model_call(replay, container, session["session_id"], session["thread_id"]))
            resumed = await client.post(session_url + "/messages", json=with_message_run_operation_id({
                "message": {"content": "用户明确选择后继续"}, "run": {},
            }))
            assert resumed.status_code == 200, resumed.text
            next_job = container.job_service._jobs[resumed.json()["data"]["job_id"]]
            assert next_job.task is not None
            await asyncio.wait_for(asyncio.shield(next_job.task), 30)
            await container.job_service.drain_finalizations()
            assert next_job.status == JobStatus.completed, next_job.error_message
            assert next_job.agent_id == "default" and controller.transport.call_count == 2
