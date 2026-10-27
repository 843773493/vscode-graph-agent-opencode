"""真实 HTTP 来源仅由 Gateway 获取；两个 Workspace 经产品入口激活。"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import commentjson
import httpx
import pytest

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
    scripted_model as scripted_model,  # noqa: PLC0414 - pytest 通过重导出的 fixture 注入。
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@dataclass
class HttpSkillServer:
    url: str
    body: bytes = (FRONTMATTER + "共享网络初始正文 Ω\n").encode()
    etag: str = '"initial"'
    requests: list[dict[str, str]] = field(default_factory=list)


@pytest.fixture
def http_skill_server(request: pytest.FixtureRequest) -> Iterator[HttpSkillServer]:
    port = integration_port_block_for_file(Path(request.node.fspath)).port(14)
    state = HttpSkillServer(f"http://127.0.0.1:{port}/private/SKILL.md")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append(dict(self.headers))
            if self.headers.get("If-None-Match") == state.etag:
                self.send_response(304)
                self.end_headers()
                return
            body = state.body
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", state.etag)
            self.send_header("Last-Modified", "Mon, 26 Oct 2026 10:00:00 GMT")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass  # 来源请求以结构化事实留在本测试收据中。

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_gateway_shared_http_skill_is_fetched_once_and_frozen_per_workspace(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str, scripted_model: ModelState, http_skill_server: HttpSkillServer,
) -> None:
    state, source = scripted_model, http_skill_server
    workspace = Path(integration_workspace_root_path)
    output, home = workspace.parent, workspace.parent / "boxteam-home"
    artifacts = output / "artifacts" / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    config_path = Path(integration_workspace_config_path)
    _configure_model(config_path, ENDPOINT)
    secondary = prepare_default_test_workspace(
        workspace_root=output / "runtime/secondary/workspace",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    # 模板身份只属于模板；新复制的工作区经真实初始化入口生成独立身份。
    (secondary / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
    install_test_workspace_config(workspace_root=secondary, config_path=config_path, schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    port = integration_port_block_for_file(Path(request.node.fspath)).port(13)
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=None, port=port, boxteam_home=home,
        artifacts_dir=artifacts / "gateway", extra_env=state.environment,
        asgi_app="tests.support.e2e_control.gateway_app:app",
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            assert (await client.get("/api/v1/workspace")).status_code == 200
            gateway_id = (await client.get("/api/gateway/health")).json()["data"]["gateway_id"]
            initial_targets = (await client.get("/api/gateway/workspaces")).json()["data"]
            first_target = initial_targets["active_workspace_id"]
            before = (await client.get("/api/gateway/config/reload-status")).json()["data"]
            path = home / "config/gateway.jsonc"
            payload = commentjson.loads(path.read_text())
            payload["resources"]["skill_sources"]["sources"] = [{
                "id": "shared-network", "name": SKILL_NAME,
                "fetch": {"protocol": "http", "url": source.url}, "updates": {"mode": "manual"},
            }]
            candidate = path.with_suffix(".candidate")
            candidate.write_text(json.dumps(payload, ensure_ascii=False))
            candidate.replace(path)
            async with asyncio.timeout(10):
                while True:
                    status = (await client.get("/api/gateway/config/reload-status")).json()["data"]
                    if status["revision"] != before["revision"]:
                        assert status["healthy"] and status["state"] == "active" and not status["restart_required"], status
                        break
                    await asyncio.sleep(0.05)
            assert len(source.requests) == 1
            added = await client.post("/api/gateway/workspaces/local", json={"root_path": str(secondary), "name": "HTTP Skill Secondary"})
            assert added.status_code == 200, added.text
            second_target = next(item["workspace_id"] for item in added.json()["data"]["items"] if item["workspace_id"] != first_target)
            sessions: list[tuple[str, str, str]] = []
            for target in (first_target, second_target):
                prefix = "/api/v1"
                headers = {"X-BoxTeam-Workspace-Id": target}
                info = await client.get(prefix + "/workspace", headers=headers)
                assert info.status_code == 200, info.text
                created = await client.post(prefix + "/sessions", headers=headers, json={"title": "Shared HTTP Skill"})
                assert created.status_code == 200, created.text
                session_id = created.json()["data"]["session_id"]
                thread_id = (await client.get(prefix + f"/sessions/{session_id}/main-thread", headers=headers)).json()["data"]["thread_id"]
                state.plan(session_id, thread_id, _skill_reply("http_skill_" + target, tracked=True), _text_reply("SHARED_HTTP_SKILL_OK"))
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={**LOCAL_TOKEN_HEADERS, **headers},
                                             cookies=client.cookies, timeout=60, trust_env=False) as routed:
                    job = await _job(routed, session_id)
                assert job["status"] in {"completed", "succeeded"}, job
                wire = json.dumps(state.requests[-1], ensure_ascii=False)
                assert "共享网络初始正文 Ω" in wire and source.url not in wire
                sessions.append((target, session_id, thread_id))
            assert len(source.requests) == 1, "Workspace 目录和 skill_load 不得访问上游"
            workspace_id = json.loads((workspace / ".boxteam/workspace-identity.json").read_text())["workspace_id"]
            raw_path = f"/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}"
            original = (await client.get(raw_path)).json()["data"]
            entry = original["entries"][0]
            assert entry["layer"] == "network" and entry["content"] == source.body.decode()
            assert source.url not in json.dumps(original)
            navigation = raw_path.replace("/skill-sources/", "/skill-source-connections/") + "/navigation"
            read = await client.post(navigation, json={"uri": entry["display_uri"], "operation": "read"})
            assert read.status_code == 200 and "共享网络初始正文 Ω" in read.text, read.text
            assert len(source.requests) == 1
            # validator 改变但正文相同，不能制造新 publication / semantic revision。
            source.etag = '"validator-only"'
            reply = await client.post("/api/gateway/skill-sources/refresh")
            assert reply.status_code == 200, reply.text
            assert (await client.get(raw_path)).json()["data"] == original
            assert source.requests[-1]["If-None-Match"] == '"initial"'
            await client.post("/api/gateway/skill-sources/refresh")
            assert source.requests[-1]["If-None-Match"] == '"validator-only"'
            assert (await client.get(raw_path)).json()["data"] == original
            source.etag, source.body = '"new"', (FRONTMATTER + "共享网络更新正文 Ω\n").encode()
            reply = await client.post("/api/gateway/skill-sources/refresh")
            assert reply.status_code == 200, reply.text
            updated = (await client.get(raw_path)).json()["data"]
            assert updated["revision"] != original["revision"]
            assert updated["entries"][0]["entry_identity"] == entry["entry_identity"]
            await asyncio.sleep(1.2)
            for target, session_id, thread_id in sessions:
                state.plan(session_id, thread_id, _text_reply("TRACKED_HTTP_UPDATE_OK"))
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={**LOCAL_TOKEN_HEADERS, "X-BoxTeam-Workspace-Id": target},
                                             cookies=client.cookies, timeout=60, trust_env=False) as routed:
                    job = await _job(routed, session_id)
                assert job["status"] in {"completed", "succeeded"}, job
                assert "共享网络更新正文 Ω" in json.dumps(state.requests[-1], ensure_ascii=False)
            assert len(source.requests) == 4
    finally:
        close_gateway_process(gateway)
        (artifacts / "http-source-requests.json").write_text(json.dumps(source.requests, indent=2))
        (artifacts / "model-requests.json").write_text(json.dumps(state.requests, ensure_ascii=False, indent=2))
