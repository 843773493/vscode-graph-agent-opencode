"""真实 run.mode 配置发布驱动 R03 工具注册、generation 与 ToolSet hard rebase。"""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256

import pytest
from langchain_core.messages import messages_to_dict

from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
from app.core.execution_admission_context import (
    execution_admission_scope,
    require_current_execution_admission,
)
from app.core.path_utils import workspace_context
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.prefix_epoch import EpochReason
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.mcp import (
    MCP_GUIDANCE_SOURCE_ID,
    MCP_TOOL_CATALOG_RESOURCE_ID,
    mcp_guidance_content,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.team.activation import (
    TEAM_LEDGER_SOURCE_ID,
    TEAM_RESOURCE_IDS,
    TEAM_SOURCE_IDS,
    team_guidance_content,
)
from configs.runtime import read_jsonc_object
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _complete_job,
    _session,
    runner_case,
    runtime_case,
)

__all__ = ["runner_case", "runtime_case"]


@pytest.mark.asyncio
async def test_real_run_mode_reload_rebuilds_r03_registration_and_preserves_old_seals(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只沿用外部模型响应桩；团队工具真实注册，但模型不调用创建团队业务。"""
    case = runtime_case
    saver = case.runner.saver
    config = case.runner.container.config_service
    source_store = case.runner.container.workspace_activity_service.store
    config_path = case.runner.workspace / ".boxteam" / "workspace.jsonc"
    r03_source_id = "instruction:team-coordination-policy"
    session_id = None
    thread_id = None
    first_generation = None
    observed_calls = []
    sealed_records = []
    retry_checks = []
    wrap = SealedAssemblyDispatchBridge.awrap_model_call
    project = SealedAssemblyDispatchBridge._prepare_projection

    def retry_sealed_requests(bridge, admission, records, *, pending):
        manager = bridge._context_source_manager
        owner = ContextSourceOwnerKey(admission.session_id, admission.thread_id)
        control = saver.load_context_source_control_states(owner)
        selection_reader = bridge._tool_set_producer._selection_reader
        assert selection_reader is case.runner.container.agent_execution_service._tool_selection_store
        chosen = selection_reader.snapshot("default")
        reads = {"sources": 0, "rebuild_sources": 0, "tool_selection": 0}
        prepare_sources = manager.prepare_sources
        prepare_rebuild_sources = manager.prepare_rebuild_sources
        selection_snapshot = selection_reader.snapshot

        def count_sources(*args: object, **kwargs: object):
            reads["sources"] += 1
            return prepare_sources(*args, **kwargs)

        def count_rebuild_sources(*args: object, **kwargs: object):
            reads["rebuild_sources"] += 1
            return prepare_rebuild_sources(*args, **kwargs)

        def count_tool_selection(*args: object, **kwargs: object):
            reads["tool_selection"] += 1
            return selection_snapshot(*args, **kwargs)

        with monkeypatch.context() as retry_patch:
            retry_patch.setattr(manager, "prepare_sources", count_sources)
            retry_patch.setattr(manager, "prepare_rebuild_sources", count_rebuild_sources)
            retry_patch.setattr(selection_reader, "snapshot", count_tool_selection)
            for old_bridge, old_request, old_admission, old_snapshot, old_bytes, old_messages, old_tools in records:
                assert bridge is not old_bridge
                assert admission.execution_id != old_admission.execution_id
                checkpoint_ns = old_request.runtime.execution_info.checkpoint_ns
                try:
                    with execution_admission_scope(
                        old_admission, langgraph_thread_id=old_admission.thread_id,
                    ):
                        retry, messages, tools = project(bridge, old_request)
                    assert retry["assembly_id"] == old_snapshot.assembly_id
                    assert canonical_json_bytes(retry["snapshot"].to_dict()) == old_bytes
                    assert canonical_json_bytes(messages_to_dict(messages)) == old_messages
                    assert canonical_json_bytes(tools) == old_tools
                    retry_checks.append((admission.execution_id, old_admission.execution_id))
                finally:
                    # 只回收本次旧请求重试产生的精确 Saver prepared handle。
                    saver.discard_prepared_context_for_dispatch(
                        old_admission.session_id, old_admission.thread_id,
                        execution_id=old_admission.execution_id,
                        assembly_id=old_snapshot.assembly_id,
                        checkpoint_ns=checkpoint_ns,
                    )
        assert reads == {"sources": 0, "rebuild_sources": 0, "tool_selection": 0}
        assert manager.prepare_pending() == pending
        assert saver.load_context_source_control_states(owner) == control
        assert selection_reader.snapshot("default") == chosen

    async def record_call(bridge, request, handler):
        admission = require_current_execution_admission()
        saved_request = request.override(messages=deepcopy(request.messages))
        assert (admission.session_id, admission.thread_id) == (session_id, thread_id)
        if sealed_records:
            pending = bridge._context_source_manager.prepare_pending()
            assert pending is not None
            assert any(
                delta.source_id == "instruction:agent-instructions"
                for delta in pending.deltas
            )
            # 新 generation 的首个真实 dispatch 前只重试紧邻的上一请求。
            retry_sealed_requests(bridge, admission, sealed_records[-1:], pending=pending)
        # handler 只记录真正投影出的请求；正文、参数及返回值原样通过生产调用。
        projected_payloads = []

        async def record_handler(actual_request):
            projected_payloads.append((
                canonical_json_bytes(messages_to_dict(actual_request.messages)),
                canonical_json_bytes(actual_request.tools),
            ))
            return await handler(actual_request)

        response = await wrap(bridge, request, record_handler)
        assert len(projected_payloads) == 1
        observed_calls.append((bridge, saved_request, admission, *projected_payloads[0]))
        return response

    monkeypatch.setattr(SealedAssemblyDispatchBridge, "awrap_model_call", record_call)
    for step, mode in enumerate(("team", "single_agent", "team")):
        before = config.get_snapshot()
        assert before.to_dict()["runtime"]["agent"]["run"]["mode"] != mode
        source_config = read_jsonc_object(config_path) if config_path.exists() else {}
        source_config.setdefault("runtime", {}).setdefault("agent", {}).setdefault(
            "run", {}
        )["mode"] = mode
        config_path.write_text(json.dumps(source_config, ensure_ascii=False), encoding="utf-8")
        assert await config.reload(idempotency_key=f"r03-mode-generation-{step}")
        after = config.get_snapshot()
        expected = before.to_dict()
        expected["runtime"]["agent"]["run"]["mode"] = mode
        assert after.revision != before.revision and after.to_dict() == expected
        assert config.get_agent_run_mode() == mode
        assert config.get_revision() == after.revision
        workspace_sources = [source for source in after.source_details if source.layer == "workspace"]
        assert len(workspace_sources) == 1
        workspace_source = workspace_sources[0]
        workspace_layer = source_store.get_source_layer("workspace_root_mutable_override")
        assert workspace_layer is not None
        assert (
            workspace_source.source_key, workspace_source.precedence,
            workspace_source.presence, workspace_source.loaded, workspace_source.uri,
        ) == ("workspace_root_mutable_override", 3, "present", True, None)
        assert (
            workspace_source.layer_revision,
            workspace_source.layer_digest,
            workspace_source.source_generation,
        ) == (
            workspace_layer.layer_revision,
            workspace_layer.layer_digest,
            workspace_layer.source_generation,
        ) == (step + 1, sha256(config_path.read_bytes()).hexdigest(), step + 1)

        if step == 0:
            session_id, thread_id = await _session(case.runner)
        assert session_id is not None and thread_id is not None
        admission = await _complete_job(case, session_id, thread_id)
        assert len(observed_calls) == len(case.compiled) == len(case.model_requests) == step + 1
        assert case.compiled[step][0] == (session_id, thread_id)
        bridge, saved_request, actual_admission, projected_messages, projected_tools = observed_calls[step]
        assert actual_admission == admission == case.model_admissions[step]
        if step > 1:
            # 更早的封存请求在真实 Job 完成后重试，保留同一 bridge 与原准入身份。
            with (
                workspace_context(
                    case.runner.container.workspace_service.workspace_id,
                    case.runner.workspace,
                    session_catalog=case.runner.container.session_catalog,
                ),
                session_thread_owner_scope(
                    MutationIntentOwner(session_id, thread_id),
                    langgraph_thread_id=thread_id,
                ),
                execution_admission_scope(admission, langgraph_thread_id=thread_id),
            ):
                retry_sealed_requests(
                    bridge, admission, sealed_records[:-1],
                    pending=bridge._context_source_manager.prepare_pending(),
                )
        assert case.compiled[step][2].state == "open" and not case.reactors[step].released
        graph_tools = extract_agent_tools_by_name(case.compiled[step][1])
        team_enabled = mode == "team"
        assert ("create_team" in graph_tools) is team_enabled
        descriptors = {
            descriptor.source_id: descriptor
            for descriptor in bridge._context_source_manager.descriptors()
        }
        assert descriptors[r03_source_id].policy_key == "team_coordination"

        assemblies = saver.list_context_assemblies(session_id, thread_id)
        assert len(assemblies) == step + 1
        snapshot = assemblies[-1]
        assert snapshot.execution_id == admission.execution_id
        assert snapshot.turn_id == admission.turn_id
        binding = snapshot.seal_binding
        assert binding is not None and binding.root is not None
        root_source_ids = {source.source_identity for source in binding.root.sources}
        assert (r03_source_id in root_source_ids) is team_enabled
        tool_refs = [ref for ref in snapshot.tool_set_refs if ref.ref_id == binding.tool_set_snapshot_id]
        assert len(tool_refs) == 1
        enabled_policy_keys = set(tool_refs[0].tool_policy["enabled_policy_keys"])
        assert ("team_coordination" in enabled_policy_keys) is team_enabled
        wire = snapshot.request_hash_preimage
        assert isinstance(wire, dict)
        wire_names = {schema["function"]["name"] for schema in wire["tools"]}
        assert ("create_team" in wire_names) is team_enabled
        assert wire_names <= set(graph_tools)
        assert canonical_json_bytes(tool_refs[0].tools) == canonical_json_bytes(wire["tools"])
        actual = case.model_requests[step]
        assert canonical_json_bytes(actual["messages"]) == canonical_json_bytes(wire["messages"])
        assert canonical_json_bytes(actual["tools"]) == canonical_json_bytes(wire["tools"])
        assert projected_tools == canonical_json_bytes(wire["tools"])
        assert binding.root.epoch == binding.prefix.state.epoch
        assert binding.root.tool_compatibility_key == binding.prefix.state.tool_compatibility_key
        assert not any(
            frame.item_ref.source_identity == r03_source_id
            for frame in binding.prefix.state.frames if frame.role == "user"
        )

        generation = case.tracker.snapshot(session_id, thread_id).generation
        if step == 0:
            first_generation = generation
            assert binding.prefix.state.epoch.reason == EpochReason.INITIAL
        else:
            assert first_generation is not None and generation == first_generation + step
            previous_snapshot = sealed_records[-1][3]
            previous = previous_snapshot.seal_binding
            assert previous is not None and previous.root is not None
            assert binding.prefix.state.epoch.ordinal == previous.prefix.state.epoch.ordinal + 1
            assert binding.prefix.state.epoch.reason == EpochReason.TOOLSET_CHANGED
            assert binding.previous_assembly_id == previous_snapshot.assembly_id
            assert binding.root != previous.root and binding.root.content != previous.root.content
            assert case.compiled[step][1] is not case.compiled[step - 1][1]
            assert case.compiled[step - 1][2].state == "closed" and case.reactors[step - 1].released
        assert len(retry_checks) == sum(range(step + 1))

        # 条件未启用来源仍可 desired；剩余 pending 不代表它们已进入当前 seal。
        pending = bridge._context_source_manager.prepare_pending()
        if pending is not None:
            pending_sources = {source.descriptor.source_id: source for source in pending.sources}
            for delta in pending.deltas:
                required_policy = pending_sources[delta.source_id].descriptor.policy_key
                assert required_policy is not None and required_policy not in enabled_policy_keys
                assert delta.source_id not in root_source_ids
        owner = ContextSourceOwnerKey(session_id, thread_id)
        control = {state.source_id: state for state in saver.load_context_source_control_states(owner)}
        activation = saver.load_sealed_resource_activation(
            session_id, thread_id=thread_id, assembly_id=snapshot.assembly_id,
            plan_hash=snapshot.plan_hash, request_hash=snapshot.request_hash,
            checkpoint_ns=saved_request.runtime.execution_info.checkpoint_ns,
        )
        for revision in binding.source_revisions:
            if revision.source_id == MCP_GUIDANCE_SOURCE_ID:
                parents = [entry for entry in activation.bindings
                           if entry.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID]
                assert len(parents) == 1 and parents[0].revision == revision.revision
                content = mcp_guidance_content(saver.read_resource_activation_payload(
                    activation, parents[0],
                    checkpoint_ns=saved_request.runtime.execution_info.checkpoint_ns,
                ))
                frames = [frame for frame in binding.prefix.state.frames
                          if frame.item_ref.source_identity == revision.source_id
                          and frame.item_ref.source_revision == revision.revision]
                assert len(frames) == 1 and frames[0].role == "user"
                assert frames[0].content == content
                assert revision.source_id not in control
            elif revision.source_id in TEAM_SOURCE_IDS.values():
                facet = next(facet for facet, source_id in TEAM_SOURCE_IDS.items()
                             if source_id == revision.source_id)
                parents = [entry for entry in activation.bindings
                           if entry.resource_id == TEAM_RESOURCE_IDS[facet]]
                if parents:
                    assert len(parents) == 1
                    parent = parents[0]
                    assert parent.resource_kind == "team_state" and parent.facet == facet
                    assert parent.source_lineage_ref is not None
                    # 本例只切换真实工具策略，没有提交任何团队 mutation。
                    assert dict(parent.source_lineage_ref.sources)[TEAM_LEDGER_SOURCE_ID] == "0"
                    payload = saver.read_resource_activation_payload(
                        activation, parent,
                        checkpoint_ns=saved_request.runtime.execution_info.checkpoint_ns,
                    )
                    assert parent.revision == parent.content_hash == sha256_jcs(payload)
                    content = team_guidance_content(payload)
                else:
                    assert not team_enabled
                    content = ""
                assert revision.revision == sha256_jcs({"content": content})
                assert revision.expected_control_revision is None
                frames = [frame for frame in binding.prefix.state.frames
                          if frame.item_ref.source_identity == revision.source_id
                          and frame.item_ref.source_revision == revision.revision]
                assert len(frames) == 1 and frames[0].role == "user"
                assert frames[0].content == content
                assert revision.source_id not in control
            else:
                assert control[revision.source_id].latest_visible_committed_revision == revision.revision
        for _old_bridge, old_request, _old_admission, old_snapshot, old_bytes, _old_messages, _old_tools in sealed_records:
            restored = saver.get_context_assembly(
                session_id, thread_id, assembly_id=old_snapshot.assembly_id,
                checkpoint_ns=old_request.runtime.execution_info.checkpoint_ns,
            )
            assert canonical_json_bytes(restored.to_dict()) == old_bytes
        sealed_records.append((
            bridge, saved_request, admission, snapshot, canonical_json_bytes(snapshot.to_dict()),
            projected_messages, projected_tools,
        ))

    assert len(case.compiled) == len(sealed_records) == len(case.model_requests) == 3
    assert len(retry_checks) == 3
    assert len({admission.execution_id for admission in case.model_admissions}) == 3
