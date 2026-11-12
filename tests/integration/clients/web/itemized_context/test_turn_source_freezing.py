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
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    skill_catalog_from_activation,
)
from app.services.mapping.itemized.resource_activation import (
    project_sealed_resource_refs,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from configs.runtime import read_jsonc_object
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
SKILL_BODIES = ("WEB_MIXED_SKILL_A\n", "WEB_MIXED_SKILL_B é\n", "WEB_MIXED_SKILL_C 🧭\n")
SKILL_NAME = "web-mixed-boundary"
MIXED_CURRENT_AGENTS = "WEB_MIXED_CURRENT_D_IS_NOT_HISTORY\n"


def skill_file_content(body: str) -> str:
    return f"---\nname: {SKILL_NAME}\ndescription: 固定metadata的混合来源\n---\n{body}"


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
    skill_source: Path | None = None,
    policy_update: Path | None = None,
) -> None:
    base_url, workspace, replay, _ = prefix_runtime
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Turn 来源冻结验收需要真实 Chromium"
    async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60) as client:
        registry = await client.get("/api/gateway/workspaces")
        assert registry.status_code == 200, registry.text
        active_owner = registry.json()["data"]["active_workspace"]
        client.headers["X-BoxTeam-Target-Gateway-Id"] = active_owner["gateway_id"]
        client.headers["X-BoxTeam-Workspace-Id"] = active_owner["workspace_id"]
        created = await client.post("/api/v1/sessions", json={"title": "工具循环中更新来源", "agent_id": "default"})
        assert created.status_code == 200, created.text
        session = created.json()["data"]
        assert session["workspace_id"] == active_owner["workspace_id"]
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
    if skill_source is not None:
        assert boundary == "turn"
        responses = [
            ([{"role": "assistant", "tool_calls": [{"index": 0, "id": f"mixed-load-{index}", "type": "function",
                "function": {"name": "skill_load", "arguments": json.dumps({"name": SKILL_NAME})}}]}], "tool_calls")
            if index in (0, 1, 3) else ([{"role": "assistant", "content": text}], "stop")
            for index, text in enumerate(("", "", first_reply, "", "下一轮采用新的来源版本。"))
        ]
    cassettes = [build_cassette(
        asset_id="web-turn-source-freezing", model="browser-chat-tool-loop-stub",
        url=f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
        frames=chat_completion_frames(response_id=f"turn-source-{index}", model="browser-chat-tool-loop-stub",
            deltas=deltas, finish_reason=finish),
    ).raw for index, (deltas, finish) in enumerate(responses)]
    policy_change = None
    if policy_update is not None:
        assert boundary == "turn" and skill_source is None
        config = read_jsonc_object(policy_update)
        context_config = config.setdefault("context", {})
        assert isinstance(context_config, dict)
        policy = context_config.setdefault("resource_activation", {})
        assert isinstance(policy, dict)
        policy["default_boundary"] = "model_call"
        policy_change = {"path": str(policy_update), "content": json.dumps(config, ensure_ascii=False, indent=2) + "\n"}
    environment = os.environ | replay.environment | {
        "BOXTEAM_BROWSER_BASE_URL": base_url,
        "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(artifacts),
        "BOXTEAM_BROWSER_TEST_OWNER": str(request.node.path),
        "BOXTEAM_BROWSER_OWNER": json.dumps({"gatewayId": active_owner["gateway_id"], "workspaceId": session["workspace_id"],
            "sessionId": session_id, "threadId": thread_id}),
        "BOXTEAM_BROWSER_CONTEXT_RESOURCE": json.dumps({key: session[key] for key in ("resource_identity", "uri", "scope")}),
        "BOXTEAM_BROWSER_AGENTS_FILE": str(agents_source),
        "BOXTEAM_BROWSER_AGENTS_VERSIONS": json.dumps(AGENTS_VERSIONS),
        "BOXTEAM_BROWSER_AGENTS_BOUNDARY": boundary,
        "BOXTEAM_BROWSER_FIRST_REPLY": first_reply,
        "BOXTEAM_BROWSER_MODEL_CASSETTES": json.dumps(cassettes),
        **({"BOXTEAM_BROWSER_MIXED_SKILL": json.dumps({"path": str(skill_source),
            "uri": f"boxteam://workspace/{session['workspace_id']}/resources/skills/{SKILL_NAME}/SKILL.md",
            "currentAgents": MIXED_CURRENT_AGENTS,
            "versions": [skill_file_content(body) for body in SKILL_BODIES]})} if skill_source is not None else {}),
        **({"BOXTEAM_BROWSER_POLICY_UPDATE": json.dumps(policy_change)} if policy_change is not None else {}),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    command = ["node", "tests/integration/clients/web/itemized_context/turn_source_freezing.mjs"]
    with (
        chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary,
        (artifacts / "browser.log").open("x", encoding="utf-8") as output,
        capture_test_processes(),
    ):
        process = subprocess.Popen(command, env=environment | temporary,
            stdout=output, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        managed = ManagedProcess(process=process, log_file=output)
        register_test_process(managed, managed.close)
        # 三调用场景原预算不变；五调用与五份完整UI检查按相同单位工作量计预算。
        returncode = await asyncio.to_thread(process.wait, timeout=80 * len(responses))
    (artifacts / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": returncode}, indent=2))
    assert returncode == 0, (artifacts / "browser.log").read_text()
    facts = json.loads((artifacts / "turn-source-result.json").read_text())
    assert facts["phase"] == "complete" and facts["pageErrors"] == []
    assert facts["boundary"] == boundary
    assert facts["coldAssemblies"] == facts["assemblies"]
    assert agents_source.read_bytes() == (MIXED_CURRENT_AGENTS if skill_source is not None else AGENTS_VERSIONS[2]).encode()
    calls = facts["modelCalls"]
    assert len(calls) == len(set(calls)) == (5 if skill_source is not None else 3)
    captures = {call: json.loads((replay.artifacts / f"{call}.json").read_text()) for call in calls}
    assert all(record["identity"] == {"session_id": session_id, "thread_id": thread_id, "model_call_id": call}
               for call, record in captures.items())
    wires = [captures[call]["request"]["messages"] for call in calls]
    first, followup, successor = wires[0], wires[1], wires[-1]
    assert followup[:len(first)] == first
    tail = followup[len(first):]
    assert len(tail) == (2 if boundary == "turn" and skill_source is None else 3)
    assistant, result = tail[:2]
    assert [assistant["role"], result["role"]] == ["assistant", "tool"]
    assert assistant["tool_calls"][0]["id"] == result["tool_call_id"] == (
        "frozen-turn-read" if skill_source is None else "mixed-load-0")
    if skill_source is None:
        assert "# 统一测试工作区" in result["content"]
    assert ("WEB_TURN_SOURCE_B" in json.dumps(followup, ensure_ascii=False)) == (boundary == "model_call")
    if boundary == "model_call":
        assert tail[2]["role"] == "user"
        assert "WEB_TURN_SOURCE_B" in json.dumps(tail[2], ensure_ascii=False)
    assert ("WEB_TURN_SOURCE_B" if skill_source is None else "WEB_TURN_SOURCE_C_CURRENT_IS_NOT_HISTORY") in json.dumps(successor, ensure_ascii=False)
    assert successor[0] == first[0]
    if skill_source is None:
        assert all("WEB_TURN_SOURCE_C_CURRENT_IS_NOT_HISTORY" not in json.dumps(record["request"], ensure_ascii=False)
                   for record in captures.values())

    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
    node = resolver.resolve_thread_node(session_id, thread_id)
    audit = audit_sealed_requests(node / "rollout/index.sqlite",
        (resolver.resolve_session_node(session_id) / "logs/llm_requests").glob("*.json"), captures)
    reports = audit["requests"]
    assert isinstance(reports, list) and len(reports) == len(calls)
    by_call = {report["model_call_id"]: report for report in reports}
    revisions = [sha256_jcs({"content": content}) for content in AGENTS_VERSIONS]
    frozen = []
    version_indexes = (0, 0, 0, 2, 2) if skill_source is not None else (0, 0, 1) if boundary == "turn" else (0, 1, 1)
    metadata = []
    receipts = []
    catalogs = []
    agent_bindings = []
    policies = []
    with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as cold:
        for ordinal, call in enumerate(calls):
            assembly_id = by_call[call]["assembly_id"]
            snapshot = cold.get_context_assembly(session_id, thread_id, assembly_id=assembly_id)
            projection = cold.read_context_assembly_history(session_id, thread_id, snapshot.assembly_id)
            activation = projection.resource_activation
            assert activation is not None
            policies.append(activation.activation_policy.to_dict())
            expected_boundary = "model_call" if policy_update is not None and ordinal == 2 else boundary
            agents = [item for item in activation.bindings if item.resource_kind == "workspace_agents"]
            agent_bindings.append(agents)
            others = [item for item in activation.bindings if item.resource_kind != "workspace_agents"
                      and not (skill_source is not None and item.resource_kind == "skill")]
            assert others and all(item.effective_boundary == (
                activation.activation_policy.effective_boundary(item.resource_kind) if policy_update is not None else "turn"
            ) for item in others)
            assert sorted(item.facet for item in agents) == ["chain", "raw-file"]
            chain = next(item for item in agents if item.facet == "chain")
            raw = next(item for item in agents if item.facet == "raw-file")
            assert chain.resource_id == WORKSPACE_AGENTS_RESOURCE_ID and chain.display_uri is None
            assert raw.display_uri == f"boxteam://workspace/{session['workspace_id']}/resources/agent-spec/root/AGENTS.md"
            assert chain.source_lineage_ref.sources == ((raw.resource_id, raw.revision),)
            for binding in agents:
                expected_version = version_indexes[ordinal]
                assert (binding.revision, binding.effective_boundary) == (revisions[expected_version], expected_boundary)
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
            assert len(updates) == int(version_indexes[ordinal] > 0)
            if updates:
                update, = updates
                assert (update.revision, update.base_revision, update.operation, update.representation, update.replaces) == (
                    revisions[version_indexes[ordinal]], revisions[0], "update", "unified_diff", (SourceVersionRef(WORKSPACE_AGENTS_SOURCE_ID, revisions[0]),))
            if skill_source is not None:
                catalog = skill_catalog_from_activation(activation, lambda snapshot, binding:
                    cold.read_resource_activation_payload(snapshot, binding, storage_owner=(session_id, thread_id)))
                assert catalog is not None
                catalogs.append(catalog)
                assert catalog.entries[SKILL_NAME].body == SKILL_BODIES[min(ordinal, 2)]
                skill_refs = [item for item in activation.bindings if item.resource_kind == "skill"
                              and item.display_uri == catalog.entries[SKILL_NAME].display_uri]
                assert {item.facet for item in skill_refs} == {"metadata", "activation"}
                assert all(item.effective_boundary == "model_call" for item in skill_refs)
                metadata.append(next(item for item in skill_refs if item.facet == "metadata"))
                if ordinal in (1, 2, 4):
                    previous = wires[ordinal - 1]
                    assert wires[ordinal][:len(previous)] == previous
                    tool_tail = wires[ordinal][len(previous):]
                    assert [row["role"] for row in tool_tail[:2]] == ["assistant", "tool"]
                    tool_call = tool_tail[0]["tool_calls"][0]["id"]
                    assert tool_tail[1]["tool_call_id"] == tool_call == f"mixed-load-{ordinal - 1}"
                    receipt = json.loads(tool_tail[1]["content"])
                    assert receipt["status"] == "loaded" and receipt["error"] is None and not receipt["tracked"]
                    assert receipt["revision"] == catalogs[ordinal - 1].entries[SKILL_NAME].activation_revision
                    assert SKILL_BODIES[len(receipts)].strip() in json.dumps(tool_tail, ensure_ascii=False)
                    receipts.append(receipt)
            frozen.append(canonical_json_bytes(activation.to_dict()).decode())
    if policy_update is not None:
        assert policies[0]["default_boundary"] == "turn"
        assert policies == [policies[0], policies[0], {**policies[0], "default_boundary": "model_call"}]
        assert facts["policyReload"]["after"]["revision"] != facts["policyReload"]["before"]["revision"]
    if skill_source is not None:
        assert len({item.revision for item in metadata[:3]}) == 1
        assert len({item.source_lineage_digest for item in metadata[:3]}) == 3
        assert len({receipt["revision"] for receipt in receipts}) == 3
        assert not skill_source.exists()
        assert agent_bindings[0] == agent_bindings[1] == agent_bindings[2]
        assert agent_bindings[3] == agent_bindings[4] != agent_bindings[0]
    else:
        assert (frozen[0] == frozen[1]) == (boundary == "turn")
    assert frozen[2] != frozen[0]
    (artifacts / "sealed-request-audit.json").write_text(json.dumps({"audit": audit, "activation": frozen}, ensure_ascii=False, indent=2))
