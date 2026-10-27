"""私有网络 Skill 经真实配置、HTTP owner、工具执行和冷恢复保持唯一链路。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.integration.backend.agents.test_gateway_http_skill_flow import (
    HttpSkillServer,
)
from tests.integration.backend.agents.test_gateway_http_skill_flow import (
    http_skill_server as http_skill_server,  # noqa: PLC0414 - pytest 显式重导出共享 HTTP fixture。
)
from tests.integration.backend.agents.test_gateway_skill_source_flow import (
    ENDPOINT,
    FRONTMATTER,
    SKILL_NAME,
    ModelState,
    _configure_model,
    _job,
    _skill_reply,
    _text_reply,
)
from tests.integration.backend.agents.test_gateway_skill_source_flow import (
    scripted_model as scripted_model,  # noqa: PLC0414 - 复用唯一 model_stream 回放入口。
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)


async def _reload(client: httpx.AsyncClient, path: Path, config: dict) -> dict:
    before = (await client.get("/api/v1/config/reload-status")).json()["data"]
    candidate = path.with_suffix(".candidate")
    candidate.write_text(json.dumps(config, ensure_ascii=False))
    candidate.replace(path)
    async with asyncio.timeout(10):
        while True:
            reply = await client.get("/api/v1/config/reload-status")
            assert reply.status_code == 200, reply.text
            assert reply.headers["X-Request-ID"] == reply.json()["request_id"]
            status = reply.json()["data"]
            if status["revision"] != before["revision"]:
                assert status["healthy"] and status["state"] == "active" and not status["restart_required"], status
                assert not status["deferred_paths"]
                return status
            await asyncio.sleep(0.05)


async def test_private_http_skill_updates_rebinds_explicitly_and_replays_frozen_history(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str, scripted_model: ModelState, http_skill_server: HttpSkillServer,
) -> None:
    state, source = scripted_model, http_skill_server
    workspace = Path(integration_workspace_root_path)
    artifacts = workspace.parent / "artifacts" / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    config_path = Path(integration_workspace_config_path)
    _configure_model(config_path, ENDPOINT)
    config = commentjson.loads(config_path.read_text())
    sources = [{"id": "private-one", "name": SKILL_NAME, "fetch": {"protocol": "http", "url": source.url},
                "updates": {"mode": "manual"}}]
    config.setdefault("resources", {}).setdefault("skill_sources", {})["sources"] = sources
    config_path.write_text(json.dumps(config, ensure_ascii=False))
    home = workspace.parent / "boxteam-home"
    port = integration_port_block_for_file(Path(request.node.fspath)).port(13)
    base_url = f"http://127.0.0.1:{port}"

    def start(label: str):
        return start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port, boxteam_home=home,
            artifacts_dir=artifacts / label, extra_env=state.environment,
            asgi_app="tests.support.e2e_control.gateway_app:app",
        )

    gateway = start("gateway-first")
    receipts: dict[str, object] = {}
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            info = await client.get("/api/v1/workspace")
            assert info.status_code == 200, info.text
            assert len(source.requests) == 1
            workspace_id = json.loads((workspace / ".boxteam/workspace-identity.json").read_text())["workspace_id"]
            created = await client.post("/api/v1/sessions", json={"title": "Private HTTP Skill"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            thread_id = (await client.get(f"/api/v1/sessions/{session_id}/main-thread")).json()["data"]["thread_id"]
            state.plan(session_id, thread_id, _skill_reply("private_initial", tracked=True), _text_reply("PRIVATE_INITIAL_OK"))
            job = await _job(client, session_id)
            assert job["status"] in {"completed", "succeeded"}, job
            assert "共享网络初始正文 Ω" in json.dumps(state.requests[-1], ensure_ascii=False)
            uri = f"boxteam://workspace/{workspace_id}/resources/skills/http_private-one/SKILL.md"
            read = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "read"})
            assert read.status_code == 200 and "共享网络初始正文 Ω" in read.text, read.text
            assert len(source.requests) == 1, "目录、导航与 skill_load 只消费 accepted 快照"

            config["resources"]["skill_sources"]["gateway_updates"] = {"mode": "manual"}
            receipts["unrelated_reload"] = await _reload(client, config_path, config)
            assert len(source.requests) == 1, "无关配置发布不得刷新 manual 来源"
            source.body, source.etag = (FRONTMATTER + "私有来源手动更新 Ω\n").encode(), '"manual-update"'
            refresh_path = "/api/v1/resources/skill-sources/refresh"
            async with httpx.AsyncClient(base_url=base_url, headers={"X-Local-Token": "wrong"}, timeout=10, trust_env=False) as denied:
                assert (await denied.post(refresh_path)).status_code == 401
            assert len(source.requests) == 1
            refreshed = await client.post(refresh_path)
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.headers["X-Request-ID"] == refreshed.json()["request_id"]
            assert refreshed.json()["data"]["workspace_id"] == workspace_id
            assert refreshed.json()["data"]["available"]
            receipts["refresh"] = refreshed.json()
            state.plan(session_id, thread_id, _text_reply("PRIVATE_MANUAL_OK"))
            job = await _job(client, session_id)
            assert job["status"] in {"completed", "succeeded"}, job
            assert "私有来源手动更新 Ω" in json.dumps(state.requests[-1], ensure_ascii=False)

            sources[0]["updates"] = {"mode": "poll", "poll_interval": "1m"}
            receipts["poll_reload"] = await _reload(client, config_path, config)
            count = len(source.requests)
            source.body, source.etag = (FRONTMATTER + "私有来源分钟轮询 Ω\n").encode(), '"poll-update"'
            async with asyncio.timeout(66):
                while len(source.requests) == count:
                    await asyncio.sleep(0.1)
            # 确认接受发布已完成，而不只观察上游收到请求。
            async with asyncio.timeout(5):
                while "私有来源分钟轮询 Ω" not in (await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "read"})).text:
                    await asyncio.sleep(0.05)
            state.plan(session_id, thread_id, _text_reply("PRIVATE_POLL_OK"))
            job = await _job(client, session_id)
            assert job["status"] in {"completed", "succeeded"}, job
            assert "私有来源分钟轮询 Ω" in json.dumps(state.requests[-1], ensure_ascii=False)

            higher_url = source.url.replace("/private/", "/higher/")
            source.variants["/higher/SKILL.md"] = ((FRONTMATTER + "更高优先级封存正文 Ω\n").encode(), '"higher"')
            sources[0]["updates"] = {"mode": "manual"}
            sources.append({"id": "higher-two", "name": SKILL_NAME, "priority": 5,
                            "fetch": {"protocol": "http", "url": higher_url}, "updates": {"mode": "manual"}})
            receipts["priority_reload"] = await _reload(client, config_path, config)
            state.plan(session_id, thread_id, _text_reply("ORIGINAL_TRACKING_OK"))
            job = await _job(client, session_id)
            assert job["status"] in {"completed", "succeeded"}, job
            assert "更高优先级封存正文 Ω" not in json.dumps(state.requests[-1], ensure_ascii=False), "目录优先级变化不得改绑已有 tracked 来源"
            state.plan(session_id, thread_id, _skill_reply("private_explicit_rebind"), _text_reply("PRIVATE_REBOUND_OK"))
            job = await _job(client, session_id)
            assert job["status"] in {"completed", "succeeded"}, job
            assert "更高优先级封存正文 Ω" in json.dumps(state.requests[-1], ensure_ascii=False)
            frozen_messages = state.requests[-1]["messages"]
            source.variants["/higher/SKILL.md"] = ((FRONTMATTER + "当前上游不得代替历史 Ω\n").encode(), '"latest"')
            assert (await client.post(refresh_path)).status_code == 200

        close_gateway_process(gateway)
        gateway = start("gateway-restarted")
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            state.plan(session_id, thread_id, _text_reply("PRIVATE_COLD_REPLAY_OK"))
            job = await _job(client, session_id)
            assert job["status"] in {"completed", "succeeded"}, job
            request_body = state.requests[-1]
            assert request_body["messages"][:len(frozen_messages)] == frozen_messages
            assert "当前上游不得代替历史 Ω" not in json.dumps(request_body, ensure_ascii=False)
            assert source.url not in json.dumps(state.requests) and higher_url not in json.dumps(state.requests)
            receipts["model_calls"] = len(state.requests)
    finally:
        close_gateway_process(gateway)
        (artifacts / "receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2))
        (artifacts / "http-source-requests.json").write_text(json.dumps(source.requests, indent=2))
        (artifacts / "model-requests.json").write_text(json.dumps(state.requests, ensure_ascii=False, indent=2))
