"""真实安装、配置 owner、认证来源及模型入口验证插件 Skill 的单链路。"""

from __future__ import annotations

import asyncio
import json
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

import commentjson
import httpx
import pytest

from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2ETestControlHarness
from tests.support.model_stream.config import model_stream_replay
from tests.support.plugin_package import install_plugin_async
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.trace import get_trace_payload
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)

PACKAGE = "proof.skill-package"
PLUGIN = "instructions"
NAME = "plugin-skill-proof"
MODEL = "plugin-skill-model"
ENDPOINT = "https://opencode.ai/zen/v1"
BODIES = {"1.0.0": "PLUGIN_SKILL_FROZEN_V1 Ω\r\n", "2.0.0": "PLUGIN_SKILL_FROZEN_V2 Ω\r\n"}


@dataclass
class ModelCase:
    control: E2ETestControlHarness
    environment: dict[str, str]
    artifacts: Path
    calls: list[str] = field(default_factory=list)

    def plan(self, session_id: str, thread_id: str, *, load: bool, tool_id: str | None = None,
             mode: Literal["snapshot", "tracked", "untrack"] = "snapshot",
             skill_call_id: str = "call_plugin_skill") -> list[str]:
        replies: list[tuple[dict[str, object], str]] = [({"role": "assistant", "content": "PLUGIN_SKILL_OK"}, "stop")]
        if tool_id is not None:
            replies.insert(0, ({"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_plugin_tool", "type": "function",
                "function": {"name": "invoke_extension_tool", "arguments": json.dumps({
                    "tool_name": tool_id, "arguments": {},
                })},
            }]}, "tool_calls"))
        if load:
            replies.insert(0, ({"role": "assistant", "tool_calls": [{
                "index": 0, "id": skill_call_id, "type": "function",
                "function": {"name": "skill_load", "arguments": json.dumps({
                    "name": NAME, **({"mode": mode} if mode != "snapshot" else {}),
                })},
            }]}, "tool_calls"))
        interactions = [cast(list[dict[str, object]], build_cassette(
            asset_id="plugin-skill-response", model=MODEL, url=f"{ENDPOINT}/chat/completions",
            frames=chat_completion_frames(response_id="chatcmpl-plugin-skill", model=MODEL,
                                          deltas=(delta,), finish_reason=reason),
        ).raw["interactions"])[0] for delta, reason in replies]
        calls = self.control.plan_model_calls(session_id, thread_id, interactions)
        self.calls.extend(calls)
        return calls

    def request_text(self, call_id: str) -> str:
        return json.dumps(json.loads((self.artifacts / f"{call_id}.json").read_text())["request"], ensure_ascii=False)

    def skill_receipt(self, call_id: str, tool_call_id: str = "call_plugin_skill") -> dict:
        messages = json.loads((self.artifacts / f"{call_id}.json").read_text())["request"]["messages"]
        result, = [message for message in messages if message.get("role") == "tool"
                   and message.get("tool_call_id") == tool_call_id]
        return json.loads(result["content"])


@pytest.fixture
def plugin_model(request: pytest.FixtureRequest) -> Iterator[ModelCase]:
    context = TestRunContext.from_test_file(Path(request.node.path))
    with model_stream_replay(context.artifacts_dir,
        runtime_root=context.runtime_root_for_node(request.node.nodeid), business_assertion="plugin-skill-source-flow",
    ) as replay:
        yield ModelCase(replay.control, replay.environment, replay.artifacts)


def _configure_model(path: Path, gateway_url: str) -> None:
    config = commentjson.loads(path.read_text())
    config["llm"]["providers"] = [{
        "id": "plugin-skill", "endpoint": ENDPOINT, "model": MODEL, "api_key": "plugin-skill-test-key",
        "custom_llm_provider": "openai",
        "api_mode": {"protocol": "chat_completions", "model_info": {"supports_function_calling": True,
            "supports_reasoning": False}, "supports_reasoning": {"reasoning_content": False}},
    }]
    for agent in config["agents"].values():
        if "model" in agent:
            agent["model"] = {"primary_provider": "plugin-skill", "fallback_providers": []}
        agent["tools"]["custom"] = [{"tool_id": "test_tool_2", "skills": [NAME]}]
    config.setdefault("runtime", {}).setdefault("gateway", {})["connection"] = {
        "url": gateway_url, "timeout_seconds": 5,
    }
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def _archive(path: Path, version: str) -> str:
    manifest = {"package_id": PACKAGE, "version": version, "name": "Plugin Skill proof", "plugins": [{
        "id": PLUGIN, "name": "Plugin Skill proof", "instance_scope": "workspace",
        "requires": [{"extension_point": "skills", "version": 1}],
        "contributes": {"skills": [{"id": "guide", "path": "guides/arbitrary-layout"}]},
    }]}
    content = f"---\r\nname: {NAME}\r\ndescription: Plugin source description\r\n---\r\n{BODIES[version]}"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("guides/arbitrary-layout/SKILL.md", content)
        archive.writestr("guides/arbitrary-layout/reference.txt", f"REFERENCE_{version}\n")
    return content


async def _data(response: httpx.Response) -> dict:
    response.raise_for_status()
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    return response.json()["data"]


async def _bind(client: httpx.AsyncClient, gateway_id: str, workspace_id: str,
                version: str | None, *, enabled: bool = True) -> None:
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    document = await _data(await client.get(path, headers=headers))
    source = commentjson.loads(document["jsonc"])
    source.setdefault("plugins", {})["runtime"] = {"entries": [] if version is None else [{
        "package_id": PACKAGE, "package_version": version, "plugin_id": PLUGIN, "enabled": enabled,
    }]}
    result = await _data(await client.put(path, headers=headers, json={
        "gateway_id": gateway_id, "workspace_id": workspace_id, "source_key": "workspace_root_mutable_override",
        "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
        "idempotency_key": f"skill-{workspace_id}-{version}-{enabled}",
    }))
    assert result["edit"]["state"] == "active", result


async def _published(client: httpx.AsyncClient, gateway_id: str, workspace_id: str,
                     version: str | None) -> dict:
    path = f"/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}"
    async with asyncio.timeout(15):
        while True:
            source = await _data(await client.get(path))
            entries = [entry for entry in source["entries"] if entry.get("plugin")]
            if (version is None and not entries) or (len(entries) == 1 and entries[0]["plugin"]["package_version"] == version):
                metadata = await _data(await client.get("/api/v1/resources/gateway-skills", headers={
                    "X-BoxTeam-Target-Gateway-Id": gateway_id, "X-BoxTeam-Workspace-Id": workspace_id,
                }))
                assert (metadata["gateway_id"], metadata["workspace_id"], metadata["revision"]) == (
                    gateway_id, workspace_id, source["revision"],
                )
                assert metadata["entries"] == [{key: entry[key] for key in (
                    "name", "display_uri", "available", "error_code", "layer", "priority", "plugin",
                )} for entry in source["entries"]]
                return source
            await asyncio.sleep(0.05)


async def _session(client: httpx.AsyncClient, workspace_id: str) -> tuple[str, str]:
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    session = await _data(await client.post("/api/v1/sessions", headers=headers, json={"title": "Plugin Skill source"}))
    session_id = session["session_id"]
    thread = await _data(await client.get(f"/api/v1/sessions/{session_id}/main-thread", headers=headers))
    return session_id, thread["thread_id"]


async def _send(client: httpx.AsyncClient, workspace_id: str, session_id: str) -> str:
    accepted = await _data(await client.post(f"/api/v1/sessions/{session_id}/messages",
        headers={"X-BoxTeam-Workspace-Id": workspace_id}, json={
            "message": {"content": "加载插件说明"}, "run": {"mode": "single_agent", "agent_id": "default"},
        }))
    return accepted["job_id"]


async def _completed(client: httpx.AsyncClient, workspace_id: str, job_id: str,
                     *, failed: bool = False) -> dict:
    async with asyncio.timeout(40):
        while True:
            job = await _data(await client.get(f"/api/v1/jobs/{job_id}", headers={"X-BoxTeam-Workspace-Id": workspace_id}))
            if job["status"] in {"completed", "succeeded", "failed", "cancelled", "timed_out"}:
                assert job["status"] in ({"failed"} if failed else {"completed", "succeeded"}), job
                return job
            await asyncio.sleep(0.05)


async def test_plugin_skill_install_binding_navigation_frozen_call_and_uninstall(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str, plugin_model: ModelCase,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path))
    artifacts, workspace = context.artifacts_dir, Path(integration_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(15)
    base_url = f"http://127.0.0.1:{port}"
    _configure_model(Path(integration_workspace_config_path), base_url)
    second = prepare_default_test_workspace(workspace_root=workspace / "second-project",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True)
    install_test_workspace_config(workspace_root=second, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    _configure_model(second / ".boxteam/workspace.jsonc", base_url)
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=artifacts / "gateway",
        asgi_app="tests.support.e2e_control.gateway_app:app", extra_env=plugin_model.environment)
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=75, trust_env=False) as client:
            await _data(await client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}))
            gateway_id = (await _data(await client.get("/api/gateway/health")))["gateway_id"]
            await _data(await client.get("/api/v1/workspace"))
            workspace_id = json.loads((workspace / ".boxteam/workspace-identity.json").read_text())["workspace_id"]
            managed = await _data(await client.post("/api/gateway/managed-workspaces", json={
                "root_path": str(second), "name": "Plugin Skill second", "create_directory": False,
            }))
            second_id = next(item["workspace_id"] for item in managed["items"] if item["name"] == "Plugin Skill second")
            documents = {}
            for version in BODIES:
                archive = artifacts / f"skill-{version}.zip"
                documents[version] = _archive(archive, version)
                await install_plugin_async(
                    lambda path, payload: client.post(path, json=payload),
                    gateway_id=gateway_id,
                    path=str(archive.resolve()),
                )
            await _published(client, gateway_id, workspace_id, None)
            await _bind(client, gateway_id, workspace_id, "1.0.0")
            first = await _published(client, gateway_id, workspace_id, "1.0.0")
            entry, = [entry for entry in first["entries"] if entry.get("plugin")]
            assert entry["content"] == documents["1.0.0"]
            assert entry["plugin"]["workspace_id"] == workspace_id
            assert str(context.boxteam_home_for_node(request.node.nodeid)) not in json.dumps(first)
            await _published(client, gateway_id, second_id, None)
            source_path = f"/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}/navigation"
            sibling_path = source_path.replace(workspace_id, second_id)
            assert (await client.post(sibling_path, json={"uri": entry["display_uri"], "operation": "read"})).status_code == 404
            root_uri = entry["display_uri"].removesuffix("/SKILL.md")
            timings = []
            for operation, extra in (("read", {}), ("list", {}), ("search", {"query": "REFERENCE", "search_mode": "content"})):
                started = time.monotonic()
                nav = await _data(await client.post(source_path, json={
                    "uri": entry["display_uri"] if operation == "read" else root_uri, "operation": operation, **extra,
                }))
                timings.append({"operation": operation, "elapsed_ms": (time.monotonic() - started) * 1000})
                if operation == "read":
                    assert nav["content"] == documents["1.0.0"]
                elif operation == "list":
                    assert {item["uri"].rsplit("/", 1)[1] for item in nav["entries"]} == {"SKILL.md", "reference.txt"}
                else:
                    assert nav["matches"][0]["text"] == "REFERENCE_1.0.0"
            status = await _data(await client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_id}))
            slot, = [item for item in status["plugins"] if item["binding"]["plugin_id"] == PLUGIN]
            assert slot["declarative"] and slot["instance"] is None and slot["binding"]["enabled"]
            session_id, thread_id = await _session(client, workspace_id)
            calls = plugin_model.plan(session_id, thread_id, load=True, tool_id="test_tool_2")
            plugin_model.control.arm(calls[0], "model.response_ready")
            job_id = await _send(client, workspace_id, session_id)
            try:
                await asyncio.to_thread(plugin_model.control.acknowledged, calls[0], "model.response_ready")
                await _bind(client, gateway_id, workspace_id, "2.0.0")
                updated = await _published(client, gateway_id, workspace_id, "2.0.0")
                assert updated["revision"] != first["revision"]
            finally:
                plugin_model.control.release(calls[0], "model.response_ready")
            await _completed(client, workspace_id, job_id)
            assert "Plugin source description" in plugin_model.request_text(calls[0])
            assert BODIES["1.0.0"].strip() in plugin_model.request_text(calls[1])
            assert BODIES["2.0.0"].strip() not in plugin_model.request_text(calls[1])
            traces = await _data(await client.get(f"/api/v1/sessions/{session_id}/traces",
                headers={"X-BoxTeam-Workspace-Id": workspace_id}))
            tool_events = [item for item in traces["items"] if item["type"] in {"tool_call_start", "tool_call_end"}
                and get_trace_payload(item).get("tool_name") == "test_tool_2"]
            assert [item["type"] for item in tool_events] == ["tool_call_start", "tool_call_end"]
            assert all(item["skill_names"] == [NAME] for item in tool_events)
            assert get_trace_payload(tool_events[-1])["result"] == "4568"
            # 原调用缺少工具时，由原信封返回错误，归因读取不能抢先中止执行流。
            unknown_session, unknown_thread = await _session(client, workspace_id)
            plugin_model.plan(unknown_session, unknown_thread, load=False, tool_id="unknown_plugin_tool")
            await _completed(client, workspace_id, await _send(client, workspace_id, unknown_session))
            unknown_traces = await _data(await client.get(f"/api/v1/sessions/{unknown_session}/traces",
                headers={"X-BoxTeam-Workspace-Id": workspace_id}))
            unknown_end, = [get_trace_payload(item) for item in unknown_traces["items"]
                if item["type"] == "tool_call_end" and get_trace_payload(item).get("tool_name") == "unknown_plugin_tool"]
            assert unknown_end["status"] == "error"
            new_session, new_thread = await _session(client, workspace_id)
            new_calls = plugin_model.plan(new_session, new_thread, load=True)
            await _completed(client, workspace_id, await _send(client, workspace_id, new_session))
            assert BODIES["2.0.0"].strip() in plugin_model.request_text(new_calls[1])
            await _bind(client, gateway_id, second_id, "1.0.0")
            sibling = await _published(client, gateway_id, second_id, "1.0.0")
            assert sibling["entries"][0]["display_uri"] != entry["display_uri"]
            await _bind(client, gateway_id, workspace_id, "2.0.0", enabled=False)
            await _published(client, gateway_id, workspace_id, None)
            current_entry = next(item for item in updated["entries"] if item.get("plugin"))
            assert (await client.post(source_path, json={"uri": current_entry["display_uri"], "operation": "read"})).status_code == 404
            blocked = await client.post("/api/gateway/service-plugins/uninstall", json={
                "gateway_id": gateway_id, "package_id": PACKAGE, "package_version": "2.0.0",
            })
            assert blocked.status_code == 409 and "configuration" in blocked.text
            await _bind(client, gateway_id, workspace_id, None)
            await _data(await client.post("/api/gateway/service-plugins/uninstall", json={
                "gateway_id": gateway_id, "package_id": PACKAGE, "package_version": "2.0.0",
            }))
            retained_calls = plugin_model.plan(new_session, new_thread, load=False)
            await _completed(client, workspace_id, await _send(client, workspace_id, new_session))
            assert BODIES["2.0.0"].strip() in plugin_model.request_text(retained_calls[0])
            # 通过产品生成的原调用冷读封存来源；不依赖已卸载包或当前目录。
            cold = RolloutCheckpointSaver(workspace / ".boxteam/sessions")
            restored = cold.load_tool_call_skill_catalog(new_session, thread_id=new_thread,
                tool_call_id="call_plugin_skill", model_call_id=new_calls[0], skill_name=NAME)
            tool_catalog = cold.load_tool_call_extension_catalog(session_id, thread_id=thread_id,
                tool_call_id="call_plugin_tool", model_call_id=calls[1], tool_ids=frozenset({"test_tool_2"}))
            tool_definition, = tool_catalog.definitions
            association, = tool_definition.skill_associations
            assert association.status == "available" and association.skill_name == NAME
            assert association.entry_identity == entry["entry_identity"]
            assert association.display_uri == entry["display_uri"]
            frozen = restored.entries[NAME]
            assert frozen.body == BODIES["2.0.0"]
            assert frozen.plugin is not None and frozen.plugin.package_version == "2.0.0"
            assert frozen.plugin.package_id == PACKAGE and frozen.plugin.contribution_id == "guide"
            assert frozen.plugin.workspace_id == workspace_id
            (artifacts / "skill-navigation-timings.json").write_text(json.dumps(timings, indent=2) + "\n")
    finally:
        close_gateway_process(gateway)


async def test_plugin_skill_tracked_version_switch_untrack_and_cold_uninstalled_history(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    plugin_model: ModelCase,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path))
    artifacts = context.artifacts_dir
    workspace = prepare_default_test_workspace(
        workspace_root=Path(integration_workspace_root_path) / "tracked-project",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True,
    )
    configuration = install_test_workspace_config(
        workspace_root=workspace, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
    )
    port = integration_port_block_for_file(Path(request.node.path)).port(15)
    base_url = f"http://127.0.0.1:{port}"
    _configure_model(configuration, base_url)
    home = context.boxteam_home_for_node(request.node.nodeid)

    def start(label: str):
        return start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port, boxteam_home=home,
            artifacts_dir=artifacts / label, asgi_app="tests.support.e2e_control.gateway_app:app",
            extra_env=plugin_model.environment,
        )

    gateway = start("gateway-first")
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=75, trust_env=False) as client:
            gateway_id = (await _data(await client.get("/api/gateway/health")))["gateway_id"]
            workspace_id = (await _data(await client.get("/api/v1/workspace")))["workspace_id"]
            archive = artifacts / "tracked-v1.zip"
            _archive(archive, "1.0.0")
            await install_plugin_async(lambda path, payload: client.post(path, json=payload),
                                       gateway_id=gateway_id, path=str(archive.resolve()))
            await _bind(client, gateway_id, workspace_id, "1.0.0")
            first = await _published(client, gateway_id, workspace_id, "1.0.0")
            old_entry, = [entry for entry in first["entries"] if entry.get("plugin")]
            session_id, thread_id = await _session(client, workspace_id)
            old_calls = plugin_model.plan(session_id, thread_id, load=True, mode="tracked")
            await _completed(client, workspace_id, await _send(client, workspace_id, session_id))
            old_receipt = plugin_model.skill_receipt(old_calls[1])
            assert old_receipt["tracked"] is True and old_receipt["display_uri"] == old_entry["display_uri"]
            assert BODIES["1.0.0"].strip() in plugin_model.request_text(old_calls[1])

            # 安装新包仅增加库存，不改变原绑定及追踪来源。
            archive = artifacts / "tracked-v2.zip"
            _archive(archive, "2.0.0")
            await install_plugin_async(lambda path, payload: client.post(path, json=payload),
                                       gateway_id=gateway_id, path=str(archive.resolve()))
            assert await _published(client, gateway_id, workspace_id, "1.0.0") == first
            retained = plugin_model.plan(session_id, thread_id, load=False)
            await _completed(client, workspace_id, await _send(client, workspace_id, session_id))
            assert BODIES["1.0.0"].strip() in plugin_model.request_text(retained[0])
            assert BODIES["2.0.0"].strip() not in plugin_model.request_text(retained[0])

            await _bind(client, gateway_id, workspace_id, "2.0.0")
            second = await _published(client, gateway_id, workspace_id, "2.0.0")
            new_entry, = [entry for entry in second["entries"] if entry.get("plugin")]
            assert new_entry["entry_identity"] != old_entry["entry_identity"]
            # 原来源已撤销时，必须在模型调用前失败，不能猜测同名 v2。
            before_dispatch = set(plugin_model.artifacts.glob("*.json"))
            rejected = await _completed(client, workspace_id, await _send(client, workspace_id, session_id), failed=True)
            assert "tracked-source-unavailable" in rejected["error_message"]
            assert set(plugin_model.artifacts.glob("*.json")) == before_dispatch
            untrack = await _data(await client.post(
                f"/api/v1/sessions/{session_id}/threads/{thread_id}/skills/untrack",
                headers={"X-BoxTeam-Workspace-Id": workspace_id}, json={"name": NAME},
            ))
            assert untrack["status"] == "loaded" and untrack["tracked"] is False and untrack["error"] is None
            assert untrack["revision"] == old_receipt["revision"]

            new_calls = plugin_model.plan(session_id, thread_id, load=True, mode="tracked",
                                          skill_call_id="call_plugin_skill_v2")
            await _completed(client, workspace_id, await _send(client, workspace_id, session_id))
            new_receipt = plugin_model.skill_receipt(new_calls[1], "call_plugin_skill_v2")
            assert new_receipt["tracked"] is True and new_receipt["display_uri"] == new_entry["display_uri"]
            assert BODIES["2.0.0"].strip() in plugin_model.request_text(new_calls[1])
            untrack_calls = plugin_model.plan(session_id, thread_id, load=True, mode="untrack",
                                              skill_call_id="call_plugin_skill_untrack")
            await _completed(client, workspace_id, await _send(client, workspace_id, session_id))
            frozen_receipt = plugin_model.skill_receipt(untrack_calls[1], "call_plugin_skill_untrack")
            assert frozen_receipt["tracked"] is False and frozen_receipt["append_status"] == "none"
            assert frozen_receipt["revision"] == new_receipt["revision"]
            assert BODIES["2.0.0"].strip() in plugin_model.request_text(untrack_calls[1])
            await _bind(client, gateway_id, workspace_id, None)
            await _published(client, gateway_id, workspace_id, None)
            for version in BODIES:
                await _data(await client.post("/api/gateway/service-plugins/uninstall", json={
                    "gateway_id": gateway_id, "package_id": PACKAGE, "package_version": version,
                }))
                assert not (home / "plugins/packages" / PACKAGE / version).exists()

        close_gateway_process(gateway)
        gateway = start("gateway-restarted")
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=75, trust_env=False) as client:
            retained = plugin_model.plan(session_id, thread_id, load=False)
            await _completed(client, workspace_id, await _send(client, workspace_id, session_id))
            assert BODIES["2.0.0"].strip() in plugin_model.request_text(retained[0])
            cold = RolloutCheckpointSaver(workspace / ".boxteam/sessions")
            for version, calls, tool_call_id in (("1.0.0", old_calls, "call_plugin_skill"),
                                                ("2.0.0", new_calls, "call_plugin_skill_v2")):
                frozen = cold.load_tool_call_skill_catalog(
                    session_id, thread_id=thread_id, tool_call_id=tool_call_id,
                    model_call_id=calls[0], skill_name=NAME,
                ).entries[NAME]
                assert frozen.body == BODIES[version]
                assert frozen.plugin is not None and frozen.plugin.package_version == version
            (artifacts / "tracked-version-lifecycle.json").write_text(json.dumps({
                "gateway_id": gateway_id, "workspace_id": workspace_id, "session_id": session_id,
                "thread_id": thread_id, "original": old_receipt, "replacement": new_receipt,
                "withdrawn_source_error": rejected["error_message"], "untrack": frozen_receipt,
                "cold_model_call_id": retained[0], "uninstalled_versions": list(BODIES),
            }, ensure_ascii=False, indent=2) + "\n")
    finally:
        close_gateway_process(gateway)
