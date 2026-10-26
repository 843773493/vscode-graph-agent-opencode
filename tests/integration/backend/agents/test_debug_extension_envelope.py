"""固定扩展信封与 sealed 目录 binding 的真实后端验收。

本文件复用 ``test_debug_prompt_flow`` 的确定性模型回放和真实后端 fixture，
避免把 ``ainvoke`` 当成主证据。模型只能看到 invoke_extension_tool，调试目标
通过封存 binding 执行；随后从 debug action、ToolSetRef 和 assembly 读取事实。
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME, TOOL_DISCOVERY_NAME
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import (
    ParentAssemblyRef,
    verify_stable_prefix,
)
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.agents import test_debug_prompt_flow as debug_case
from tests.integration.backend.agents.test_debug_prompt_flow import (
    ScriptedModelState,
    _plan_debug_calls,
    _session_main_thread_id,
    _write_debug_fixture,
)
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.trace import get_trace_payload

# 显式登记共享 fixture；测试模块同时被收集时不依赖 pytest_plugins 的全局注册。
scripted_model_replay = debug_case.scripted_model_replay
scripted_debug_backend_port = debug_case.scripted_debug_backend_port
scripted_backend_process = debug_case.scripted_backend_process
scripted_client = debug_case.scripted_client


def _tool_names(request: dict[str, Any]) -> set[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return set()
    names: set[str] = set()
    for item in tools:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
        elif isinstance(item.get("name"), str):
            names.add(item["name"])
    return names


def _binding_fingerprint(action: dict[str, Any]) -> tuple[str, str, str, int, str, str]:
    binding = action.get("extension_catalog_binding")
    assert isinstance(binding, dict), action
    return (
        str(binding["binding_id"]),
        str(binding["binding_hash"]),
        str(binding["catalog_revision"]),
        int(binding["generation"]),
        str(binding["provider_binding_identity"]),
        str(binding["target_schema_hash"]),
    )


@pytest.mark.asyncio
async def test_fixed_extension_envelope_seals_binding_and_preserves_toolset_prefix(
    scripted_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
    scripted_model_replay: ScriptedModelState,
    scripted_backend_process: object,
) -> None:
    del scripted_backend_process
    state = scripted_model_replay
    workspace_root = Path(integration_workspace_root_path).resolve()
    fixture_path, worker_path = _write_debug_fixture(workspace_root)
    state.fixture_path = fixture_path.relative_to(workspace_root).as_posix()
    state.worker_path = worker_path.relative_to(workspace_root).as_posix()
    state.working_directory = "."

    create_response = await scripted_client.post(
        "/api/v1/sessions", json={"title": "sealed debug envelope"}
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]
    main_thread_id = await _session_main_thread_id(scripted_client, session_id)
    _plan_debug_calls(state, session_id, main_thread_id, discover=True)
    # 本项在执行后核 sealed binding；Skill 正文屏障由同组 prompt 测试单独验收。
    state.control.release(state.call_ids[1], "model.response_ready")
    prompt = (
        "请先使用 skill_load(name=debugging)，然后按 Skill 通过固定信封真实执行调试动作；"
        "完成后只回复 envelope acceptance。"
    )
    message_response = await scripted_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200, message_response.text
    job_id = message_response.json()["data"]["job_id"]
    job = await wait_for_job_done(scripted_client, job_id, max_attempts=120)
    assert job["status"] in {"completed", "succeeded"}, job

    messages = (
        await scripted_client.get(f"/api/v1/sessions/{session_id}/messages")
    ).json()["data"]["items"]
    assert "DEBUG_PROMPT_FLOW_OK" in last_assistant_message(messages)

    requests = state.requests
    assert requests
    provider_tool_sets = [
        json.dumps(request["tools"], ensure_ascii=False, sort_keys=True)
        for request in requests
        if isinstance(request.get("tools"), list)
    ]
    assert provider_tool_sets
    assert len(set(provider_tool_sets)) == 1
    exposed = _tool_names(requests[0])
    assert EXTENSION_TOOL_INVOKER_NAME in exposed
    assert TOOL_DISCOVERY_NAME in exposed
    discovered_message, = (
        message for message in requests[2]["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "debug-e2e-call-1"
    )
    discovered = json.loads(discovered_message["content"])
    assert discovered["results"] and discovered["next_cursor"] is None
    assert all(entry["status"] == "available" and entry["input_schema"]
               and entry["effective_policy"]["execution_enabled"]
               and not entry["effective_policy"]["model_visible"]
               and entry["provenance"]["activation_snapshot_id"]
               and entry["provenance"]["revision"] for entry in discovered["results"])
    assert {entry["tool_id"] for entry in discovered["results"]} == {
        "list_debug_configurations", "list_breakpoints", "create_debug_configuration",
        "add_breakpoint", "start_debugging", "evaluate_expression", "continue_execution",
    }
    assert not {
        "list_debug_configurations",
        "create_debug_configuration",
        "start_debugging",
        "add_breakpoint",
        "evaluate_expression",
        "continue_execution",
    } & exposed

    traces = (
        await scripted_client.get(f"/api/v1/sessions/{session_id}/traces")
    ).json()["data"]["items"]
    debug_target_names = {
        "list_debug_configurations",
        "list_breakpoints",
        "create_debug_configuration",
        "add_breakpoint",
        "start_debugging",
        "evaluate_expression",
        "continue_execution",
    }
    starts = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") in debug_target_names
    ]
    assert starts
    assert all(
        item.get("invocation_tool_name") == EXTENSION_TOOL_INVOKER_NAME
        for item in starts
    )

    debug_response = await scripted_client.get(
        "/api/v1/debug/node",
        params={"session_id": session_id},
    )
    assert debug_response.status_code == 200, debug_response.text
    debug_state = debug_response.json()["data"]
    assert debug_state["thread_id"] == main_thread_id
    actions = debug_state["actions"]
    audited = [
        action
        for action in actions
        if action.get("actor") == "ai" and action.get("tool_name") in debug_target_names
    ]
    assert audited
    fingerprints = {_binding_fingerprint(action) for action in audited}
    binding_identities = {item[:5] for item in fingerprints}
    assert len(binding_identities) == 1
    assert all(item[0].startswith("ext-catalog:v3:") for item in fingerprints)
    assert all(item[1].startswith("sha256:") for item in fingerprints)
    assert all(item[2].startswith("sha256:") for item in fingerprints)
    assert all(item[5].startswith("sha256:") for item in fingerprints)
    assert all(
        item[4] == "invoke_extension_tool@v1:tool_name:str+arguments:object"
        for item in fingerprints
    )

    saver = RolloutCheckpointSaver(
        sessions_dir=workspace_root / ".boxteam" / "sessions"
    )
    assemblies = saver.list_context_assemblies(session_id, main_thread_id)
    assert assemblies
    tool_refs = [
        ref
        for assembly in assemblies
        for ref in assembly.tool_set_refs
        if ref.assembly_id == assembly.assembly_id
    ]
    assert tool_refs
    assert len({ref.content_hash for ref in tool_refs}) == 1
    assert len(
        {
            json.dumps(ref.tools, ensure_ascii=False, sort_keys=True)
            for ref in tool_refs
        }
    ) == 1

    assert len(assemblies) == len(requests)
    same_epoch: dict[int, list[ContextAssemblySnapshot]] = {}
    for assembly, request in zip(assemblies, requests, strict=True):
        wire = normalize_wire_request(request)
        assert assembly.request_hash_preimage == wire
        assert assembly.request_hash == context_request_hash(
            assembly.as_sealed_plan(), assembly.provider_version,
            projector_id=assembly.projector_id, projector_version=assembly.projector_version,
            target_format=assembly.target_format, wire_request=wire,
        )
        binding = assembly.seal_binding
        assert binding is not None
        same_epoch.setdefault(binding.prefix.state.epoch.ordinal, []).append(assembly)
    assert any(len(epoch_assemblies) > 1 for epoch_assemblies in same_epoch.values())
    for epoch_assemblies in same_epoch.values():
        for previous, current in pairwise(epoch_assemblies):
            previous_binding, current_binding = previous.seal_binding, current.seal_binding
            assert previous_binding is not None and current_binding is not None
            previous_prefix, current_prefix = previous_binding.prefix, current_binding.prefix
            parent = ParentAssemblyRef(
                assembly_id=previous.assembly_id,
                epoch_ordinal=previous_prefix.state.epoch.ordinal,
                prefix_byte_length=previous_prefix.prefix_byte_length,
                prefix_hash=previous_prefix.prefix_hash,
            )
            assert verify_stable_prefix(parent, current_prefix.state.frames) == len(
                previous_prefix.state.frames
            )
            assert current_binding.root == previous_binding.root
            assert current_binding.applied_toolset_revision == previous_binding.applied_toolset_revision
            assert current_prefix.state.tool_compatibility_key == previous_prefix.state.tool_compatibility_key
            assert current_prefix.state.provider_profile == previous_prefix.state.provider_profile
