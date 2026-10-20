"""共享唯一 Backend 的独立 Gateway Skill 来源、普通 Job 与冷恢复。"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from threading import Event

import pytest

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.services.business.session_context_resource import session_resource_uri
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_resource_navigation import _tracking_counts
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _completed,
    _prepare_workspace,
)
from tests.integration.stubs.http_stubs import HTTPStubState, openai_chat_stub
from tests.support import communication_federation
from tests.support.api_helpers import create_session
from tests.support.communication_federation import (
    CommunicationNode,
    open_communication_triad,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import start_backend_process

SKILL_NAME = "owner-proof"
OBSERVATION_APP = "tests.support.skill_gateway_observations:app"


def _tool(name: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
    return {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}]}


async def _until(predicate: Callable[[], bool], seconds: float = 30) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)


@dataclass
class SkillGatewayCase:
    nodes: dict[str, CommunicationNode]
    model: HTTPStubState
    context: TestRunContext
    snapshots: dict[str, dict[str, object]]

    def record(self, name: str, data: object) -> None:
        (self.context.artifacts_dir / (name + ".json")).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )

    def observations(self) -> list[dict[str, object]]:
        return [json.loads(line)
                for path in sorted((self.context.artifacts_dir / "skill-request-identity").glob("*.jsonl"))
                for line in path.read_text().splitlines()]

    def source_responses(self, gateway: str) -> list[dict[str, object]]:
        node = self.nodes[gateway]
        path = f"/api/gateway/skill-source-connections/{node.gateway_id}/workspaces/{node.workspace_id}"
        return [row for row in self.observations()
                if row["event"] == "workspace_response" and row["path"] == path]

    def assert_exchange(self, row: dict[str, object], gateway: str, request_id: str | None) -> None:
        node = self.nodes[gateway]
        assert row["sent_request_id"] == row["context_request_id"] == request_id
        transport_url = row["url"]
        assert isinstance(transport_url, str)
        assert transport_url.startswith(f"http://127.0.0.1:{self.nodes['a'].gateway.port}/")
        assert row["pid"] == node.backend.process.pid
        returned_id = row["response_request_id"]
        assert isinstance(returned_id, str) and returned_id.strip()
        if request_id is not None:
            assert returned_id == request_id
        if row["status"] == 304:
            assert not row["has_body"] and row["envelope_request_id"] is None
            assert row["if_none_match"] == row["etag"]
        else:
            assert row["status"] == 200 and row["envelope_request_id"] == returned_id
        kind = "navigation" if row["method"] == "POST" else "snapshot"
        owner, = [receipt for receipt in self.observations()
                  if receipt["event"] == "owner_receipt" and receipt["request_id"] == returned_id
                  and receipt["owner_gateway_id"] == node.gateway_id and receipt["kind"] == kind]
        assert owner["request_gateway_id"] == owner["result_gateway_id"] == node.gateway_id
        assert owner["request_workspace_id"] == owner["result_workspace_id"] == node.workspace_id
        assert owner["result_request_id"] == returned_id and owner["status"] == row["status"]
        assert owner["pid"] == node.gateway.process.pid and owner["if_none_match"] == row["if_none_match"]
        if kind == "snapshot":
            assert owner["etag"] == row["etag"]

    async def assert_first_snapshot(self, gateway: str, request_id: str, *, backend_pid: int | None = None) -> None:
        def matches() -> list[dict[str, object]]:
            return [row for row in self.source_responses(gateway)
                    if row["sent_request_id"] == request_id and row["status"] == 200
                    and (backend_pid is None or row["pid"] == backend_pid)]

        await _until(lambda: bool(matches()))
        row, = matches()
        self.assert_exchange(row, gateway, request_id)

    async def send(self, gateway: str, session_id: str, content: str, request_id: str) -> dict[str, object]:
        response = await self.nodes[gateway].client.post(
            f"/api/v1/sessions/{session_id}/messages", headers={"X-Request-ID": request_id},
            json={"message": {"content": content}, "run": {"mode": "single_agent", "agent_id": "default"}},
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        return response.json()["data"]

    async def assert_created_owner(self, session_id: str, job_id: str, gateway: str, request_id: str) -> None:
        response = await self.nodes["a"].client.get(f"/api/v1/sessions/{session_id}/traces", params={"limit": 200})
        assert response.status_code == 200, response.text
        created, = [event["raw"]["payload"] for event in response.json()["data"]["items"]
                    if event["type"] == "job_created" and event["job_id"] == job_id]
        assert (created["gateway_id"], created["request_id"]) == (self.nodes[gateway].gateway_id, request_id)

    async def restart_backend(self) -> dict[str, int]:
        source, peer = self.nodes["a"], self.nodes["b"]
        previous = source.backend
        assert previous is peer.backend
        await asyncio.to_thread(source.restart_backend, "kill")
        backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(source.workspace), port=previous.port,
            log_name="skill-owner-cold-recovery", refresh_user_configuration=False,
            env_unset=("BOXTEAM_TEST_COMMUNICATION_BARRIER_DIR", "BOXTEAM_TEST_COMMUNICATION_BARRIER_STAGE"),
            env_overrides={
                "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{source.gateway.port}",
                "BOXTEAM_TEST_SKILL_OBSERVATION_DIR": str(self.context.artifacts_dir / "skill-request-identity"),
                "BOXTEAM_TEST_SKILL_OBSERVATION_ROLE": "backend",
            }, asgi_app=OBSERVATION_APP,
        )
        source.backend = peer.backend = backend
        assert backend.process.pid != previous.process.pid
        return {"before_pid": previous.process.pid, "after_pid": backend.process.pid}

    def skill_reply(
        self, payload: dict[str, object], gateway: str, call_id: str, *, allow_history: bool = False,
    ) -> dict[str, object]:
        wire = json.dumps(payload, ensure_ascii=False)
        other = "b" if gateway == "a" else "a"
        assert f"skill-gateway-owner-{gateway}" in wire
        if not allow_history:
            assert f"skill-gateway-owner-{other}" not in wire
        results = [json.loads(message["content"]) for message in payload["messages"]
                   if message["role"] == "tool" and message.get("name") == "skill_load"]
        if not results:
            return _tool("skill_load", {"name": SKILL_NAME, "mode": "snapshot"}, call_id)
        result = results[-1]
        assert result["error"] is None, result
        assert result["display_uri"] == (
            f"boxteam://gateway/{self.nodes[gateway].gateway_id}/resources/skills/{SKILL_NAME}/SKILL.md"
        )
        assert f"private-skill-owner-{gateway}" in wire
        assert f"private-skill-owner-{other}" not in wire
        return {"content": "Skill owner " + gateway + " 已验证"}


@pytest.fixture
async def skill_gateways(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    base = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    run_node = base.runtime_root_for_node(request.node.nodeid).relative_to(base.output_root)
    context = replace(base, workspace_root=base.workspace_root / run_node, artifacts_dir=base.artifacts_dir / run_node).prepare()
    ports = integration_port_block_for_file(context.test_file)
    barrier = getattr(request, "param", None)
    observation_environment = {"BOXTEAM_TEST_SKILL_OBSERVATION_DIR": str(context.artifacts_dir / "skill-request-identity")}
    environment = {**observation_environment, "BOXTEAM_TEST_SKILL_OBSERVATION_ROLE": "backend"}
    if barrier is not None:
        environment.update({"BOXTEAM_TEST_COMMUNICATION_BARRIER_DIR": str(context.artifacts_dir),
                            "BOXTEAM_TEST_COMMUNICATION_BARRIER_STAGE": barrier})
    start_gateway = communication_federation.start_gateway_process

    def observed_gateway(**kwargs):
        return start_gateway(**kwargs, asgi_app=OBSERVATION_APP)

    monkeypatch.setattr(communication_federation, "start_gateway_process", observed_gateway)
    with openai_chat_stub(ports.port(10)) as model:
        async with open_communication_triad(
            context, _prepare_workspace, ports=ports, same_backend_routes=True,
            backend_asgi_app=OBSERVATION_APP,
            backend_environment=environment,
            gateway_environment={**observation_environment, "BOXTEAM_TEST_SKILL_OBSERVATION_ROLE": "gateway"},
        ) as nodes:
            snapshots = {}
            for name, node in nodes.items():
                skill = node.gateway_home / "skills" / SKILL_NAME / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text(
                    f"---\nname: {SKILL_NAME}\ndescription: skill-gateway-owner-{name}\n---\nprivate-skill-owner-{name}\n",
                )
                async with asyncio.timeout(10):
                    while True:
                        response = await node.client.get(f"/api/gateway/skill-sources/{node.gateway_id}/workspaces/{node.workspace_id}")
                        assert response.status_code == 200, response.text
                        snapshot = response.json()["data"]
                        if snapshot["entries"]:
                            assert snapshot["entries"][0]["content"] == skill.read_text()
                            snapshots[name] = snapshot
                            break
                        await asyncio.sleep(0.02)
            case = SkillGatewayCase(nodes, model, context, snapshots)
            try:
                yield case
            finally:
                case.record("provider-requests", model.requests)
                case.record("topology", {name: {
                    "gateway_id": node.gateway_id, "gateway_pid": node.gateway.process.pid,
                    "backend_pid": node.backend.process.pid, "workspace_id": node.workspace_id,
                    "gateway_home": str(node.gateway_home), "channels": node.channels(),
                } for name, node in nodes.items()})


async def test_shared_backend_routes_snapshot_navigation_and_concurrent_messages_to_true_owner(skill_gateways):
    case = skill_gateways
    source, target = case.nodes["a"], case.nodes["b"]
    own_path = f"/api/gateway/skill-sources/{target.gateway_id}/workspaces/{target.workspace_id}"
    rejected = await source.client.get(own_path)
    assert rejected.status_code == 409 and rejected.json()["detail"] == "Gateway Skill source owner mismatch"
    connected_path = own_path.replace("/skill-sources/", "/skill-source-connections/")
    connected = await source.client.get(connected_path, headers={"X-Request-ID": "req-skill-connected-B"})
    assert connected.status_code == 200, connected.text
    assert connected.json()["data"] == case.snapshots["b"]
    assert connected.headers["X-Request-ID"] == connected.json()["request_id"] == "req-skill-connected-B"
    conditional = await source.client.get(connected_path, headers={
        "If-None-Match": connected.headers["ETag"], "X-Request-ID": "req-skill-conditional-B",
    })
    assert conditional.status_code == 304 and conditional.headers["ETag"] == connected.headers["ETag"]
    assert conditional.headers["X-Request-ID"] == "req-skill-conditional-B" and not conditional.content
    uri = f"boxteam://gateway/{target.gateway_id}/resources/skills/{SKILL_NAME}/SKILL.md"
    navigation = await source.client.post(connected_path + "/navigation", headers={"X-Request-ID": "req-skill-direct-navigation-B"},
                                          json={"uri": uri, "operation": "read"})
    assert navigation.status_code == 200, navigation.text
    assert navigation.headers["X-Request-ID"] == navigation.json()["request_id"] == "req-skill-direct-navigation-B"
    assert navigation.json()["data"]["node"]["uri"] == uri
    assert navigation.json()["data"]["content"] == case.snapshots["b"]["entries"][0]["content"]
    # 真实 Workspace 导航必须先由 B 请求绑定B；同一 Backend 的连接URL仍是A。
    product_path = f"/api/v1/workspaces/{target.workspace_id}/resources/navigation"
    product_navigation = await target.client.post(product_path, headers={"X-Request-ID": "req-skill-product-navigation-B"},
                                                  json={"uri": uri, "operation": "read"})
    assert product_navigation.status_code == 200, product_navigation.text
    assert product_navigation.headers["X-Request-ID"] == product_navigation.json()["request_id"] == "req-skill-product-navigation-B"
    assert product_navigation.json()["data"] == navigation.json()["data"]
    own_uri = uri.replace(target.gateway_id, source.gateway_id)
    own_navigation = await source.client.post(product_path, headers={"X-Request-ID": "req-skill-product-navigation-A"},
                                             json={"uri": own_uri, "operation": "read"})
    assert own_navigation.status_code == 200, own_navigation.text
    assert own_navigation.headers["X-Request-ID"] == own_navigation.json()["request_id"] == "req-skill-product-navigation-A"
    for name in case.nodes:
        request_id = "req-skill-product-navigation-" + name.upper()
        row, = [row for row in case.observations()
                if row["event"] == "workspace_response" and row["sent_request_id"] == request_id]
        case.assert_exchange(row, name, request_id)
    wrong_context = await source.client.post(product_path, json={"uri": uri, "operation": "read"})
    assert wrong_context.status_code == 403, wrong_context.text
    assert wrong_context.json()["detail"]["code"] == "scope_mismatch"
    outside = await source.client.get(connected_path.replace(target.workspace_id, "outside-workspace"))
    assert outside.status_code == 403
    unknown = await source.client.get(connected_path.replace(target.gateway_id, "gateway_" + "0" * 32))
    assert unknown.status_code == 409

    def reply(payload):
        wire = json.dumps(payload, ensure_ascii=False)
        for name in case.nodes:
            if "owner-run-" + name in wire:
                return case.skill_reply(payload, name, "call_owner_" + name)
        return {"content": "Skill 来源验证"}

    case.model.response_callback = reply
    sessions = {name: await create_session(node.client, "独立 owner " + name) for name, node in case.nodes.items()}
    accepted = await asyncio.gather(*(case.send(name, sessions[name], "owner-run-" + name, "req-owner-run-" + name) for name in case.nodes))
    completed = await asyncio.gather(*(_completed(case.nodes[name].client, job["job_id"]) for name, job in zip(case.nodes, accepted, strict=True)))
    assert len({job["job_id"] for job in completed}) == 2
    for name, job in zip(case.nodes, accepted, strict=True):
        await case.assert_created_owner(sessions[name], job["job_id"], name, "req-owner-run-" + name)
        await case.assert_first_snapshot(name, "req-owner-run-" + name)

    for name in case.nodes:
        await _until(lambda name=name: any(row["sent_request_id"] is None and row["status"] == 304
                                          for row in case.source_responses(name)))
    for name, node in case.nodes.items():
        skill = node.gateway_home / "skills" / SKILL_NAME / "SKILL.md"
        skill.write_text(skill.read_text() + "独立观察新 revision " + name + "\n")
    observer_rows = []
    for name in case.nodes:
        def refreshed(name=name):
            return [row for row in case.source_responses(name)
                    if row["sent_request_id"] is None and row["status"] == 200]

        await _until(lambda: bool(refreshed()))
        revision = refreshed()[-1]["etag"]
        await _until(lambda name=name, revision=revision: any(
            row["sent_request_id"] is None and row["status"] == 304 and row["etag"] == revision
            for row in case.source_responses(name)
        ))
        rows = [row for row in case.source_responses(name) if row["sent_request_id"] is None]
        assert len(rows) >= 3 and {row["status"] for row in rows} == {200, 304}
        for row in rows:
            assert row["task"] == "workspace-gateway-skill-sources:" + case.nodes[name].gateway_id
            case.assert_exchange(row, name, None)
        observer_rows.extend(rows)
    observer_ids = [row["response_request_id"] for row in observer_rows]
    assert len(set(observer_ids)) == len(observer_ids)
    assert not set(observer_ids) & {"req-owner-run-a", "req-owner-run-b"}
    case.record("request-identity-exchanges", case.observations())
    case.record("connected-owner-results", {"snapshot": connected.json(), "navigation": navigation.json(), "jobs": completed})


async def test_remote_skill_navigation_is_lightweight_encoded_and_fixed_to_owner(skill_gateways: SkillGatewayCase):
    case = skill_gateways
    source, target = case.nodes["a"], case.nodes["b"]
    for name, node in case.nodes.items():
        root = node.gateway_home / "skills" / SKILL_NAME
        (root / "references/deep").mkdir(parents=True)
        (root / ".notes").mkdir()
        (root / "scripts").mkdir()
        (root / "references/deep/中文 文档.v2.md").write_text(f"多层正文 owner-{name} Ω\n")
        (root / ".notes/100%.txt").write_text(f"点号目录 owner-{name}\n")
        (root / "%2e%2e.txt").write_text(f"字面百分号 owner-{name}\n")
        (root / "scripts/non-text.bin").write_bytes(b"\xff")
        (root / "scripts/media %.PNG").write_bytes(b"\xff" + f"media-owner-{name}".encode() * 300)
        (root / "scripts/read-range.txt").write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\ngamma\n")
        for index in range(257):
            (root / f"references/deep/bulk-needle-{index:03}.txt").write_text(f"未追踪 owner-{name}\n")

    connected_path = f"/api/gateway/skill-source-connections/{target.gateway_id}/workspaces/{target.workspace_id}"
    product_path = f"/api/v1/workspaces/{target.workspace_id}/resources/navigation"
    root_uri = resource_display_uri(scope="gateway", scope_id=target.gateway_id, kind="skills", tail_segments=(SKILL_NAME,))
    receipts = []

    def persistent_facts() -> dict[str, object]:
        sessions = target.workspace / ".boxteam/sessions"
        return {
            "tracking": _tracking_counts(case.context.workspace_root),
            "session_files": {str(path.relative_to(sessions)): sha256(path.read_bytes()).hexdigest()
                              for path in sorted(sessions.rglob("*")) if path.is_file()},
        }

    before = persistent_facts()
    publication = await source.client.get(connected_path)
    assert publication.status_code == 200, publication.text
    assert publication.json()["data"] == case.snapshots["b"]

    async def navigate(uri: str, operation: str, *, route: str = "connection", status: int = 200, **extra: object) -> dict:
        request_id = f"req-remote-navigation-{len(receipts):03}"
        client, path = (source.client, connected_path + "/navigation") if route == "connection" else (target.client, product_path)
        response = await client.post(path, headers={"X-Request-ID": request_id}, json={"uri": uri, "operation": operation, **extra})
        body = response.json()
        receipts.append({"route": route, "uri": uri, "operation": operation, "status": response.status_code,
                         "header_request_id": response.headers["X-Request-ID"], "body": body})
        case.record("remote-navigation-receipts", receipts)
        assert response.status_code == status, response.text
        assert response.headers["X-Request-ID"] == request_id
        for private in (str(target.workspace), *(str(node.gateway_home) for node in case.nodes.values()), "local-dev-token"):
            assert private not in response.text
        if status != 422:
            assert body["request_id"] == request_id
            owner, = [row for row in case.observations()
                      if row["event"] == "owner_receipt" and row["request_id"] == request_id and row["kind"] == "navigation"]
            assert owner["pid"] == target.gateway.process.pid
            assert owner["owner_gateway_id"] == owner["request_gateway_id"] == owner["result_gateway_id"] == target.gateway_id
            assert owner["request_workspace_id"] == owner["result_workspace_id"] == target.workspace_id
            assert owner["result_request_id"] == request_id and owner["status"] == status
        else:
            assert body["detail"][0]["loc"] == ["body", "operation"]
            assert body["detail"][0]["input"] == operation
            assert not any(row["event"] == "owner_receipt" and row["request_id"] == request_id for row in case.observations())
        if status != 200:
            return body
        data = body["data"]
        assert set(data) == {"node", "entries", "content", "truncated", "next_cursor", "truncation_reason", "view",
                             "content_encoding", "media_type", "mime_type", "line_offset"}
        assert data["view"] == "live"
        assert data["node"]["uri"] == parse_resource_uri(uri, local_gateway_id=target.gateway_id).display_uri
        for node in (data["node"], *data["entries"]):
            assert set(node) == {"uri", "resource_shape", "operations", "child_path"}
            assert node["uri"].startswith(root_uri)
        if route == "workspace":
            row, = [row for row in case.observations()
                    if row["event"] == "workspace_response" and row["sent_request_id"] == request_id]
            case.assert_exchange(row, "b", request_id)
        return data

    for route in ("connection", "workspace"):
        root = await navigate(root_uri, "describe", route=route)
        assert root["node"] == {"uri": root_uri, "resource_shape": "directory", "operations": ["describe", "list", "search"], "child_path": "file_segments"}
        listed = await navigate(root_uri, "list", route=route)
        entries = {node["uri"].rsplit("/", 1)[1]: node for node in listed["entries"]}
        skill_uri = entries["SKILL.md"]["uri"]
        assert entries["SKILL.md"]["operations"] == ["describe", "read"] and entries["SKILL.md"]["child_path"] is None
        assert (await navigate(skill_uri, "read", route=route))["content"] == case.snapshots["b"]["entries"][0]["content"]
        explicit = resource_display_uri(scope="gateway", scope_id=target.gateway_id, kind="skills",
                                        tail_segments=(SKILL_NAME, "SKILL.md"), gateway_authority=target.gateway_id)
        assert (await navigate(explicit, "read", route=route))["node"]["uri"] == skill_uri
        references = await navigate(entries["references"]["uri"], "list", route=route)
        deep, = references["entries"]
        assert deep["resource_shape"] == "directory" and deep["uri"].endswith("/references/deep")
        found = await navigate(deep["uri"], "search", route=route, query="中文 文档")
        child, = found["entries"]
        assert child["uri"] == root_uri + "/references/deep/%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.v2.md"
        assert (await navigate(child["uri"], "read", route=route))["content"] == "多层正文 owner-b Ω\n"
        bounded = await navigate(child["uri"], "read", route=route, max_bytes=4)
        assert bounded["content"] == "多" and bounded["truncated"]
        dot = await navigate(entries[".notes"]["uri"], "list", route=route)
        literal_percent, = dot["entries"]
        assert literal_percent["uri"] == root_uri + "/.notes/100%25.txt"
        assert (await navigate(literal_percent["uri"], "read", route=route))["content"] == "点号目录 owner-b\n"
        assert entries["%252e%252e.txt"]["uri"] == root_uri + "/%252e%252e.txt"
        assert (await navigate(entries["%252e%252e.txt"]["uri"], "read", route=route))["content"] == "字面百分号 owner-b\n"
        media = await navigate(root_uri + "/scripts/media%20%25.PNG", "read", route=route, read_format="auto", max_bytes=8192, max_response_bytes=16384)
        assert media["content_encoding"] == "base64" and media["media_type"] == "image" and media["mime_type"] == "image/png"
        assert base64.b64decode(media["content"]) == b"\xff" + b"media-owner-b" * 300
        assert not media["truncated"]
        rejected = await navigate(root_uri + "/scripts/media%20%25.PNG", "read", route=route, status=400, read_format="auto", max_bytes=1)
        assert rejected["detail"]["owner"] == "resource_file_operations" and rejected["detail"]["reason"] == "file_byte_limit"
        ranged = await navigate(root_uri + "/scripts/read-range.txt", "read", route=route, line_offset=2, max_lines=1)
        assert ranged["content"] == "beta\r\n" and ranged["line_offset"] == 2
        assert ranged["truncated"] and ranged["truncation_reason"] == "line_limit"
        bulk = await navigate(root_uri, "search", route=route, query="bulk-needle", limit=7)
        assert len(bulk["entries"]) == 7 and bulk["truncated"]
        assert all(node["resource_shape"] == "file" for node in bulk["entries"])
        assert (await navigate(bulk["entries"][0]["uri"], "read", route=route))["content"] == "未追踪 owner-b\n"

    # 同名文件分别属于 A/B；固定 B 的路径不能读取 A，也不能把读能力提升为目录或写能力。
    for uri, operation, code in (
        (root_uri.replace(target.gateway_id, source.gateway_id), "list", "scope_mismatch"),
        (root_uri, "read", "capability_denied"),
        (root_uri + "/SKILL.md", "list", "capability_denied"),
        (root_uri + "/SKILL.md/child", "read", "capability_denied"),
    ):
        denied = await navigate(uri, operation, status=403)
        assert denied["detail"]["owner"] == "resource_uri_resolver" and denied["detail"]["code"] == code
    for suffix, code in (("/%2e%2e", "dot_segment"), ("/references%2Fdeep", "invalid_character")):
        denied = await navigate(root_uri + suffix, "read", status=400)
        assert denied["detail"]["owner"] == "resource_uri_grammar" and denied["detail"]["code"] == code
    await navigate(root_uri + "/SKILL.md", "write", status=422)
    invalid_content = await navigate(root_uri + "/scripts/non-text.bin", "read", status=400)
    assert invalid_content["detail"] == {"owner": "resource_file_operations", "code": "invalid_encoding",
                                         "reason": "utf8", "message": "文件内容不是有效的 UTF-8 文本"}
    outside = case.context.workspace_root / "remote-private-proof.txt"
    outside.write_text("私有根外正文\n")
    link = target.gateway_home / "skills" / SKILL_NAME / "references/escape"
    link.symlink_to(outside)
    denied = await navigate(root_uri + "/references/escape", "read", status=403)
    assert denied["detail"]["code"] == "capability_denied"
    link.unlink()

    after = persistent_facts()
    assert after == before
    current = await source.client.get(connected_path)
    assert current.status_code == 200 and current.json()["data"] == publication.json()["data"]
    assert current.headers["ETag"] == publication.headers["ETag"]
    assert not case.model.requests
    assert not case.source_responses("a") and not case.source_responses("b")
    case.record("remote-navigation-lightweight", {"before": before, "after": after, "publication": current.json(),
                                                  "backend_pid": target.backend.process.pid, "owner_pid": target.gateway.process.pid})


async def test_model_ls_and_read_gateway_skill_children_without_activation(skill_gateways: SkillGatewayCase):
    case = skill_gateways
    target = case.nodes["b"]
    for name, node in case.nodes.items():
        directory = node.gateway_home / "skills" / SKILL_NAME / "references" / "scripts"
        directory.mkdir(parents=True)
        (directory / f"owner-{name}-中文 %.txt").write_text(f"read-body-owner-{name}\n")
    root_uri = resource_display_uri(scope="gateway", scope_id=target.gateway_id,
                                    kind="skills", tail_segments=(SKILL_NAME,))
    session = await create_session(target.client, "模型目录导航")
    before = None
    listed = []

    def tracking_facts():
        counts = _tracking_counts(case.context.workspace_root)
        # 每次真实模型请求都会保存 assembly binding；核其仍指向原 activation，而非禁止正常装配记录。
        counts = {key: value for key, value in counts.items() if not key.endswith(":resource_activation_assembly_bindings")}
        assembly_owners = {}
        for database in case.context.workspace_root.rglob("index.sqlite"):
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                owners = connection.execute("""SELECT DISTINCT activation_snapshot_id, bindings_hash,
                    activation_provenance_hash FROM resource_activation_assembly_bindings
                    ORDER BY activation_snapshot_id, bindings_hash, activation_provenance_hash""").fetchall()
            assembly_owners[str(database.relative_to(case.context.workspace_root))] = owners
        return {"counts": counts, "assembly_owners": assembly_owners}

    def reply(payload):
        nonlocal before
        results = [json.loads(message["content"]) for message in payload["messages"]
                   if message["role"] == "tool" and message.get("name") == "ls"]
        if not results:
            # 本轮指令来源已由真实 preparation 登记；只比较普通工具执行前后。
            assert before is None
            before = tracking_facts()
            return _tool("ls", {"path": root_uri}, "call_model_ls_root")
        result = results[-1]
        if not listed or listed[-1] != result:
            listed.append(result)
        assert result["node"]["uri"].startswith(root_uri)
        assert not result["truncated"] and result["view"] == "live"
        if len(results) < 3:
            directory = "references" if len(results) == 1 else "scripts"
            child, = [entry for entry in result["entries"] if entry["uri"].endswith("/" + directory)]
            return _tool("ls", {"path": child["uri"]}, "call_model_ls_" + directory)
        child, = result["entries"]
        assert child["uri"] == root_uri + "/references/scripts/owner-b-%E4%B8%AD%E6%96%87%20%25.txt"
        reads = [message for message in payload["messages"] if message["role"] == "tool" and message.get("name") == "read_file"]
        if not reads:
            return _tool("read_file", {"path": child["uri"]}, "call_model_read_child")
        read, = reads
        assert "1\tread-body-owner-b" in read["content"] and "read-body-owner-a" not in read["content"]
        wire = json.dumps(payload, ensure_ascii=False)
        assert "owner-a-" not in wire and "private-skill-owner-" not in wire
        return {"content": "已验证目标 Gateway 的多层目录和实际文件正文"}

    case.model.response_callback = reply
    accepted = await case.send("b", session, "浏览目标 Skill 目录", "req-model-ls-navigation")
    await _completed(target.client, accepted["job_id"])
    assert len(listed) == 3
    rows = [row for row in case.observations() if row["event"] == "workspace_response"
            and row["method"] == "POST" and row["sent_request_id"] == "req-model-ls-navigation"]
    assert len(rows) == 4
    for row in rows:
        assert row["sent_request_id"] == row["context_request_id"] == row["response_request_id"] == row["envelope_request_id"] == "req-model-ls-navigation"
        assert row["status"] == 200 and row["pid"] == target.backend.process.pid
    assert {row["uri"] for row in rows} == {result["node"]["uri"] for result in listed} | {listed[-1]["entries"][0]["uri"]}
    owners = [row for row in case.observations() if row["event"] == "owner_receipt"
              and row["request_id"] == "req-model-ls-navigation" and row["kind"] == "navigation"]
    assert len(owners) == 4
    for row in owners:
        assert row["pid"] == target.gateway.process.pid
        assert row["owner_gateway_id"] == row["request_gateway_id"] == row["result_gateway_id"] == target.gateway_id
        assert row["request_workspace_id"] == row["result_workspace_id"] == target.workspace_id
        assert row["result_request_id"] == "req-model-ls-navigation" and row["status"] == 200
    after = tracking_facts()
    assert before is not None
    assert before == after
    case.record("model-ls-navigation", {"results": listed, "before": before, "after": after, "receipts": rows})


async def test_queued_job_cold_restore_keeps_frozen_gateway_and_request_id(skill_gateways):
    case = skill_gateways
    started, release = Event(), Event()
    first_request = True

    def reply(payload):
        nonlocal first_request
        wire = json.dumps(payload, ensure_ascii=False)
        if "owner-pending-b" in wire:
            return case.skill_reply(payload, "b", "call_pending_owner_b", allow_history=True)
        if "owner-active-hold" in wire and first_request:
            first_request = False
            started.set()
            assert release.wait(45), "测试未解除 Provider 屏障"
        return {"content": "原活跃执行结束"}

    case.model.response_callback = reply
    session_id = await create_session(case.nodes["a"].client, "冷恢复 owner 队列")
    try:
        await case.send("a", session_id, "owner-active-hold", "req-owner-active-a")
        await _until(started.is_set)
        pending = await case.send("b", session_id, "owner-pending-b", "req-owner-pending-b")
        queued = await case.nodes["a"].client.get(f"/api/v1/sessions/{session_id}/pending-requests")
        assert queued.status_code == 200, queued.text
        entry, = queued.json()["data"]["requests"]
        assert entry["job_id"] == pending["job_id"]
        assert (entry["gateway_id"], entry["request_id"]) == (case.nodes["b"].gateway_id, "req-owner-pending-b")
        # 原 active 在后端停止后才解除；待处理输入仍由真实队列 owner 恢复。
        await asyncio.to_thread(case.nodes["a"].restart_backend, "kill")
        release.set()
        previous = case.nodes["a"].backend
        backend = await asyncio.to_thread(case.nodes["a"].backend_start)
        case.nodes["a"].backend = case.nodes["b"].backend = backend
        assert backend.process.pid != previous.process.pid
        restored = await case.nodes["a"].client.get(f"/api/v1/sessions/{session_id}/pending-requests")
        assert restored.status_code == 200, restored.text
        job = await _completed(case.nodes["a"].client, pending["job_id"])
        await case.assert_created_owner(session_id, pending["job_id"], "b", "req-owner-pending-b")
        await case.assert_first_snapshot("b", "req-owner-pending-b", backend_pid=backend.process.pid)
        case.record("pending-cold-owner", {"queued": queued.json(), "job": job, "old_pid": previous.process.pid, "new_pid": backend.process.pid})
    finally:
        release.set()


@pytest.mark.parametrize("skill_gateways", ["after_wakeup"], indirect=True)
async def test_runtime_wakeup_cold_restore_uses_frozen_owner_without_request_context(skill_gateways):
    case = skill_gateways
    source, target = case.nodes["a"], case.nodes["b"]
    source_session = await create_session(source.client, "来源 A")
    target_session = await create_session(target.client, "目标 B 冷恢复")

    def reply(payload):
        human = next((str(message["content"]) for message in reversed(payload["messages"])
                      if message["role"] == "user" and message["content"]), "")
        if "owner-wakeup-b" in human:
            return case.skill_reply(payload, "b", "call_wakeup_owner_b")
        if "owner-send-to-b" in human and not any(message.get("name") == "send_message_to_session" for message in payload["messages"]):
            return _tool("send_message_to_session", {
                "target_session_id": session_resource_uri(target.workspace_id, target_session, gateway_authority=target.gateway_id),
                "content": "owner-wakeup-b",
            }, "call_owner_source_send")
        return {"content": "来源发送已接受"}

    case.model.response_callback = reply
    source_job = await case.send("a", source_session, "owner-send-to-b", "req-owner-wakeup-source")
    marker = case.context.artifacts_dir / "barrier-after_wakeup.json"
    await _until(marker.exists)
    frozen = json.loads(marker.read_text())["wakeup"]
    assert frozen["gateway_id"] == target.gateway_id
    assert frozen["request_id"] == "req-owner-wakeup-source"
    target_node = await target.session_node(target_session)
    database = target_node / "rollout/index.sqlite"

    def row():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            return connection.execute(
                "SELECT job_id,gateway_id,request_id,state FROM runtime_execution_wakeups",
            ).fetchone()

    assert row() == (frozen["job_id"], target.gateway_id, frozen["request_id"], "pending")
    await _completed(source.client, source_job["job_id"])
    restart = await case.restart_backend()
    completed = await _completed(source.client, frozen["job_id"])
    await case.assert_created_owner(target_session, frozen["job_id"], "b", frozen["request_id"])
    await case.assert_first_snapshot("b", frozen["request_id"], backend_pid=restart["after_pid"])
    assert row() == (frozen["job_id"], target.gateway_id, frozen["request_id"], "dispatched")
    case.record("runtime-wakeup-cold-owner", {"frozen": frozen, "recovered": row(), "restart": restart, "job": completed})
