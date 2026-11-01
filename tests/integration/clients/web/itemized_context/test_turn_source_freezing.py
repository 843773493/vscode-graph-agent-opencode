"""真实 Composer 工具循环的来源冻结与冷检查；模型使用精确回放。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.epoch.source_protocol import (
    SourceVersionRef,
    parse_source_blocks,
)
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.services.business.resource_derivations.agents import (
    WORKSPACE_AGENTS_RESOURCE_ID,
    WORKSPACE_AGENTS_SOURCE_ID,
    agents_content,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.mapping.itemized.resource_activation import (
    project_sealed_resource_refs,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.itemized_context.test_fork_prefix_recovery import (
    prefix_runtime as prefix_runtime,  # noqa: PLC0414 — 复用装具，资源按本测试路径隔离。
)
from tests.integration.clients.web.itemized_context.test_fork_prefix_recovery import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 — prefix_runtime 的依赖。
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.sealed_request_audit import audit_sealed_requests

AGENTS_VERSIONS = (
    "WEB_TURN_SOURCE_A\n保持工具结果的精确身份。\n",
    "WEB_TURN_SOURCE_B 🧭\r\n保留末尾空白  \n保持工具结果的精确身份。\n",
    "WEB_TURN_SOURCE_C_CURRENT_IS_NOT_HISTORY\n",
)


@pytest.fixture(scope="module", autouse=True)
def agents_source(integration_workspace_root_path: str) -> Path:
    """只准备真实来源文件；执行、checkpoint 与 assembly 均由产品创建。"""
    path = Path(integration_workspace_root_path) / "AGENTS.md"
    path.write_bytes(AGENTS_VERSIONS[0].encode())
    return path


async def test_tool_loop_freezes_agents_until_next_turn_and_cold_inspection(
    request: pytest.FixtureRequest,
    prefix_runtime: tuple[str, Path, ModelStreamReplay, int],
    agents_source: Path,
) -> None:
    await verify_agents_source_boundary(request, prefix_runtime, agents_source, boundary="turn")


async def verify_agents_source_boundary(
    request: pytest.FixtureRequest,
    prefix_runtime: tuple[str, Path, ModelStreamReplay, int],
    agents_source: Path,
    *, boundary: Literal["turn", "model_call"],
) -> None:
    base_url, workspace, replay, _ = prefix_runtime
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Turn 来源冻结验收需要真实 Chromium"
    async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60) as client:
        guest = await client.post("/api/gateway/users/guest", json={"tracking": {"source": "turn-source-freezing"}})
        assert guest.status_code == 200, guest.text
        registry = await client.get("/api/gateway/workspaces")
        assert registry.status_code == 200, registry.text
        route_id = registry.json()["data"]["active_workspace_id"]
        client.headers["X-BoxTeam-Workspace-Id"] = route_id
        created = await client.post("/api/v1/sessions", json={"title": "工具循环中更新来源", "agent_id": "default"})
        assert created.status_code == 200, created.text
        session = created.json()["data"]
        session_id, thread_id = session["session_id"], session["thread_id"]
        updated = await client.patch(f"/api/v1/sessions/{session_id}", json={"provider_id": "primary"})
        assert updated.status_code == 200 and updated.json()["data"]["current_provider_id"] == "primary", updated.text
    ports = integration_port_block_for_file(Path(request.node.path))
    first_reply = "本轮继续沿用冻结来源。" if boundary == "turn" else "本轮采用更新来源。"
    responses = [
        ([{"role": "assistant", "tool_calls": [{"index": 0, "id": "frozen-turn-read", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'}}]}], "tool_calls"),
        ([{"role": "assistant", "content": first_reply}], "stop"),
        ([{"role": "assistant", "content": "下一轮采用新的来源版本。"}], "stop"),
    ]
    cassettes = [build_cassette(
        asset_id="web-turn-source-freezing", model="browser-chat-tool-loop-stub",
        url=f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
        frames=chat_completion_frames(response_id=f"turn-source-{index}", model="browser-chat-tool-loop-stub",
            deltas=deltas, finish_reason=finish),
    ).raw for index, (deltas, finish) in enumerate(responses)]
    environment = os.environ | replay.environment | {
        "BOXTEAM_BROWSER_BASE_URL": base_url,
        "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(artifacts),
        "BOXTEAM_BROWSER_TEST_OWNER": str(request.node.path),
        "BOXTEAM_BROWSER_OWNER": json.dumps({"routeId": route_id, "workspaceId": session["workspace_id"],
            "sessionId": session_id, "threadId": thread_id}),
        "BOXTEAM_BROWSER_CONTEXT_RESOURCE": json.dumps({key: session[key] for key in ("resource_identity", "uri", "scope")}),
        "BOXTEAM_BROWSER_AGENTS_FILE": str(agents_source),
        "BOXTEAM_BROWSER_AGENTS_VERSIONS": json.dumps(AGENTS_VERSIONS),
        "BOXTEAM_BROWSER_AGENTS_BOUNDARY": boundary,
        "BOXTEAM_BROWSER_FIRST_REPLY": first_reply,
        "BOXTEAM_BROWSER_MODEL_CASSETTES": json.dumps(cassettes),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    command = ["node", "tests/integration/clients/web/itemized_context/turn_source_freezing.mjs"]
    with (
        chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary,
        (artifacts / "browser.log").open("x", encoding="utf-8") as output,
        capture_test_processes(),
    ):
        process = await asyncio.to_thread(subprocess.Popen, command, env=environment | temporary,
            stdout=output, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        managed = ManagedProcess(process=process, log_file=output)
        register_test_process(managed, managed.close)
        returncode = await asyncio.to_thread(process.wait, timeout=240)
    (artifacts / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": returncode}, indent=2))
    assert returncode == 0, (artifacts / "browser.log").read_text()
    facts = json.loads((artifacts / "turn-source-result.json").read_text())
    assert facts["phase"] == "complete" and facts["pageErrors"] == []
    assert facts["boundary"] == boundary
    assert facts["coldAssemblies"] == facts["assemblies"]
    assert agents_source.read_bytes() == AGENTS_VERSIONS[2].encode()
    calls = facts["modelCalls"]
    assert len(calls) == len(set(calls)) == 3
    captures = {call: json.loads((replay.artifacts / f"{call}.json").read_text()) for call in calls}
    assert all(record["identity"] == {"session_id": session_id, "thread_id": thread_id, "model_call_id": call}
               for call, record in captures.items())
    first, followup, successor = [captures[call]["request"]["messages"] for call in calls]
    assert followup[:len(first)] == first
    tail = followup[len(first):]
    assert len(tail) == (2 if boundary == "turn" else 3)
    assistant, result = tail[:2]
    assert [assistant["role"], result["role"]] == ["assistant", "tool"]
    assert assistant["tool_calls"][0]["id"] == result["tool_call_id"] == "frozen-turn-read"
    assert "# 统一测试工作区" in result["content"]
    assert ("WEB_TURN_SOURCE_B" in json.dumps(followup, ensure_ascii=False)) == (boundary == "model_call")
    if boundary == "model_call":
        assert tail[2]["role"] == "user"
        assert "WEB_TURN_SOURCE_B" in json.dumps(tail[2], ensure_ascii=False)
    assert "WEB_TURN_SOURCE_B" in json.dumps(successor, ensure_ascii=False)
    assert successor[0] == first[0]
    assert all("WEB_TURN_SOURCE_C_CURRENT_IS_NOT_HISTORY" not in json.dumps(record["request"], ensure_ascii=False)
               for record in captures.values())

    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
    node = resolver.resolve_thread_node(session_id, thread_id)
    audit = audit_sealed_requests(node / "rollout/index.sqlite",
        (resolver.resolve_session_node(session_id) / "logs/llm_requests").glob("*.json"), captures)
    reports = audit["requests"]
    assert isinstance(reports, list) and len(reports) == 3
    by_call = {report["model_call_id"]: report for report in reports}
    revisions = [sha256_jcs({"content": content}) for content in AGENTS_VERSIONS]
    frozen = []
    version_indexes = (0, 0, 1) if boundary == "turn" else (0, 1, 1)
    with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as cold:
        for ordinal, call in enumerate(calls):
            assembly_id = by_call[call]["assembly_id"]
            snapshot = cold.get_context_assembly(session_id, thread_id, assembly_id=assembly_id)
            projection = cold.project_context_plan_to_history_with_diagnostics(session_id, thread_id, snapshot.as_sealed_plan())
            activation = projection.resource_activation
            assert activation is not None
            agents = [item for item in activation.bindings if item.resource_kind == "workspace_agents"]
            others = [item for item in activation.bindings if item.resource_kind != "workspace_agents"]
            assert others and all(item.effective_boundary == "turn" for item in others)
            assert sorted(item.facet for item in agents) == ["chain", "raw-file"]
            chain = next(item for item in agents if item.facet == "chain")
            raw = next(item for item in agents if item.facet == "raw-file")
            assert chain.resource_id == WORKSPACE_AGENTS_RESOURCE_ID and chain.display_uri is None
            assert raw.display_uri == f"boxteam://workspace/{session['workspace_id']}/resources/agent-spec/root/AGENTS.md"
            assert chain.source_lineage_ref.sources == ((raw.resource_id, raw.revision),)
            for binding in agents:
                expected_version = version_indexes[ordinal]
                assert (binding.revision, binding.effective_boundary) == (revisions[expected_version], boundary)
                payload = cold.read_resource_activation_payload(activation, binding, storage_owner=(session_id, thread_id))
                assert agents_content(payload) == AGENTS_VERSIONS[expected_version]
            public = next(item for item in facts["assemblies"] if item["assemblyId"] == assembly_id)
            assert public["selection"] == [entry.to_dict() for entry in snapshot.selection]
            assert public["resources"] == [item.model_dump(mode="json") for item in project_sealed_resource_refs(activation)]
            seal = snapshot.seal_binding
            assert seal is not None and seal.root is not None
            root, = [block for block in parse_source_blocks(seal.root.content, expected_version=seal.root.source_protocol)
                     if block.source_identity == WORKSPACE_AGENTS_SOURCE_ID]
            assert (root.body, root.revision, root.operation, root.representation) == (AGENTS_VERSIONS[0], revisions[0], "activate", "full")
            updates = [block for frame in seal.prefix.state.frames if frame.role == "user" and frame.source_protocol is not None
                       for block in parse_source_blocks(frame.content, expected_version=frame.source_protocol)
                       if block.source_identity == WORKSPACE_AGENTS_SOURCE_ID]
            assert len(updates) == version_indexes[ordinal]
            if updates:
                update, = updates
                assert (update.revision, update.base_revision, update.operation, update.representation, update.replaces) == (
                    revisions[1], revisions[0], "update", "unified_diff", (SourceVersionRef(WORKSPACE_AGENTS_SOURCE_ID, revisions[0]),))
            frozen.append(canonical_json_bytes(activation.to_dict()).decode())
    assert (frozen[0] == frozen[1]) == (boundary == "turn")
    assert frozen[2] != frozen[0]
    (artifacts / "sealed-request-audit.json").write_text(json.dumps({"audit": audit, "activation": frozen}, ensure_ascii=False, indent=2))
