"""固定扩展信封与 sealed 目录 binding 的真实后端验收。

本文件复用 ``test_debug_prompt_flow`` 的确定性模型服务器和真实后端 fixture，
避免把 ``ainvoke`` 当成主证据。模型只能看到 invoke_extension_tool，调试目标
通过封存 binding 执行；随后从 debug action、ToolSetRef 和 assembly 读取事实。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest_plugins = (
    "tests.integration.backend.agents.test_debug_prompt_flow",
)

from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.agents.test_debug_prompt_flow import (
    ScriptedModelState,
    _write_debug_fixture,
)
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.trace import get_trace_payload


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
    scripted_model_server: tuple[ScriptedModelState, str],
    scripted_backend_process: object,
) -> None:
    del scripted_backend_process
    state, _endpoint = scripted_model_server
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

    with state.lock:
        requests = list(state.requests)
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
        params={"session_id": session_id, "thread_id": "main"},
    )
    assert debug_response.status_code == 200, debug_response.text
    actions = debug_response.json()["data"]["actions"]
    audited = [
        action
        for action in actions
        if action.get("actor") == "ai" and action.get("tool_name") in debug_target_names
    ]
    assert audited
    fingerprints = {_binding_fingerprint(action) for action in audited}
    binding_identities = {item[:5] for item in fingerprints}
    assert len(binding_identities) == 1
    assert all(item[0].startswith("ext-catalog:v1:") for item in fingerprints)
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
    assemblies = saver.list_context_assemblies(session_id)
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

    same_epoch = {}
    for assembly in assemblies:
        if assembly.request_hash_preimage is None:
            continue
        same_epoch.setdefault(assembly.source_overlay_epoch, []).append(
            assembly.request_hash_preimage
        )
    for wire_values in same_epoch.values():
        if len(wire_values) > 1:
            assert wire_values[0] == wire_values[-1]
