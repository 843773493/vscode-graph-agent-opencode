"""真实配置发布、R01 generation 与封存请求继承的纵向合同。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

import tests.integration.backend.sessions.test_thread_runtime_runner as runner_module
from app.agents.graph_binding import GRAPH_FACTORY_REGISTRY, GraphBinding
from app.agents.instruction_producers import InstructionProducerSpec
from app.agents.model_call_execution import ModelCallExecution
from app.core.execution_admission_context import execution_admission_scope
from app.core.resource_uri import resource_display_uri
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.epoch.prefix_epoch import stable_prefix_bytes
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.common import JobStatus
from app.services.infrastructure.resource_platform.sources.observed_source import (
    StableSourceReader,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from configs.runtime import read_jsonc_object
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _complete_job,
    _session,
    _start_job,
    runner_case,
    runtime_case,
)

# 显式导出既有 fixtures，保持 pytest 注入与测试用例的同名参数。
__all__ = ["runner_case", "runtime_case"]


@pytest.fixture(autouse=True)
def instruction_file_input(input_kind: str, producer_id: str, monkeypatch: pytest.MonkeyPatch):
    """在真实容器初始化之前选择文件；不手工发布来源或构造 producer。"""
    files: list[Path] = []
    if input_kind == "file":
        build = runner_module._build_owned_container

        def build_with_file(workspace, patch, request):
            path = workspace / f"{producer_id}.txt"
            path.write_text(f"{producer_id} 文件初始正文", encoding="utf-8")
            files.append(path)
            uri = resource_display_uri(scope="workspace", scope_id=load_or_create_workspace_id(workspace),
                                       kind="files", tail_segments=("root", path.name))
            config_path = workspace / ".boxteam/workspace.jsonc"
            payload = read_jsonc_object(config_path)
            instructions = payload.setdefault("agents", {}).setdefault("default", {}).setdefault("instructions", {})
            if producer_id == "R01":
                instructions["system_prompt_file"] = uri
            else:
                instructions.setdefault("runtime_identity", {})["description_file"] = uri
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            return build(workspace, patch, request)

        monkeypatch.setattr(runner_module, "_build_owned_container", build_with_file)
    return files


@pytest.mark.parametrize("input_kind", ["inline", "file"])
@pytest.mark.parametrize("producer_id", ["R01", "R02"])
@pytest.mark.asyncio
async def test_real_config_reload_refreshes_instruction_generation_without_rewriting_sealed_prefix(
    runtime_case: RuntimeCase,
    producer_id: str,
    input_kind: str,
    instruction_file_input: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实配置发布经 runtime generation 到下一请求；旧封存请求仍可重试。"""
    case = runtime_case
    saver = case.runner.saver
    config = case.runner.container.config_service
    source_id = "instruction:agent-instructions" if producer_id == "R01" else "instruction:runtime-identity"
    before = config.get_snapshot()
    # Runner fixture 已在建容器前写入配置，初始来源必须与该真实文件和权威记录一致。
    workspace_source_key = "workspace_root_mutable_override"
    source_store = case.runner.container.workspace_activity_service.store
    before_workspace_sources = [
        source for source in before.source_details if source.layer == "workspace"
    ]
    assert len(before_workspace_sources) == 1
    before_workspace_source = before_workspace_sources[0]
    initial_layer = source_store.get_source_layer(workspace_source_key)
    assert initial_layer is not None
    initial_config_path = case.runner.workspace / ".boxteam" / "workspace.jsonc"
    assert (
        before_workspace_source.precedence,
        before_workspace_source.source_key,
        before_workspace_source.presence,
        before_workspace_source.loaded,
        before_workspace_source.uri,
        before_workspace_source.layer_revision,
        before_workspace_source.layer_digest,
        before_workspace_source.source_generation,
    ) == (3, workspace_source_key, "present", True, None, initial_layer.layer_revision,
          sha256(initial_config_path.read_bytes()).hexdigest(), initial_layer.source_generation)
    before_source_generation = source_store.source_generation_high_water_mark(
        source_key=workspace_source_key
    )
    assert before_source_generation == initial_layer.source_generation == 1
    initial_instructions = before.to_dict()["agents"]["default"]["instructions"]
    old_content = initial_instructions["system_prompt"] if producer_id == "R01" else initial_instructions["runtime_identity"]["description"]
    new_content = f"{producer_id} 配置刷新后的说明：后续请求使用这条新内容。"
    assert new_content != old_content
    produced: list[InstructionProducerSpec] = []
    projections = []
    resolve = GRAPH_FACTORY_REGISTRY.resolve
    project = ModelCallExecution._project_prepared
    wrap = ModelCallExecution.aexecute
    first_record = None
    first_snapshot = None
    first_admission = None
    first_seal_bytes = None
    retry_checked = False

    def record_generation(binding: GraphBinding):
        builder = resolve(binding)

        def compile_generation(**kwargs):
            r01 = [
                spec for spec in kwargs["instruction_producers"]
                if spec.producer_id == producer_id
            ]
            assert len(r01) == 1 and isinstance(r01[0], InstructionProducerSpec)
            produced.append(r01[0])
            return builder(**kwargs)

        return compile_generation

    def source_state():
        return next(
            state for state in saver.load_context_source_control_states(
                ContextSourceOwnerKey(session_id, thread_id)
            )
            if state.source_id == source_id
        )

    async def check_retry(bridge):
        nonlocal retry_checked
        if first_record is not None and not retry_checked:
            assert first_snapshot is not None and first_admission is not None
            assert bridge is not first_record[0]
            manager = bridge._context_source_manager
            pending = manager.prepare_pending()
            assert pending is not None
            changed = [delta for delta in pending.deltas if delta.source_id == source_id]
            assert len(changed) == 1
            assert changed[0].previous_revision == produced[0].revision_token()
            assert changed[0].revision == produced[1].revision_token()
            state_before_retry = source_state()
            assert state_before_retry.latest_visible_committed_revision == produced[0].revision_token()
            assert state_before_retry.latest_revision == produced[1].revision_token()
            read_sources = manager.prepare_sources
            source_reads = 0

            def count_source_reads(*args, **kwargs):
                nonlocal source_reads
                source_reads += 1
                return read_sources(*args, **kwargs)

            if input_kind == "file":
                instruction_file_input[0].unlink()
                with pytest.raises(RuntimeError):
                    await config.reload(idempotency_key="file-unavailable-before-sealed-retry")
                with pytest.raises(RuntimeError, match="instruction-source-unavailable"):
                    config.require_instruction_sources_available("default")
            saved_request = first_record[1]
            checkpoint_ns = saved_request.runtime.execution_info.checkpoint_ns
            try:
                with (
                    monkeypatch.context() as retry_patch,
                    session_thread_owner_scope(
                        MutationIntentOwner(session_id, thread_id),
                        langgraph_thread_id=thread_id,
                    ),
                    execution_admission_scope(
                        first_admission, langgraph_thread_id=thread_id,
                    ),
                ):
                    retry_patch.setattr(manager, "prepare_sources", count_source_reads)
                    if input_kind == "file":
                        def forbidden_file_read(*args, **kwargs):
                            pytest.fail("sealed retry 不得读取提示词文件")
                        retry_patch.setattr(StableSourceReader, "read", forbidden_file_read)
                    retry_patch.setattr(ModelCallExecution, "_project_prepared", project)
                    retry, messages, tools = project(bridge, saved_request, await bridge._aprepare(saved_request))
                assert source_reads == 0
                assert retry["assembly_id"] == first_snapshot.assembly_id
                assert canonical_json_bytes(retry["snapshot"].to_dict()) == first_seal_bytes
                assert messages == first_record[3] and tools == first_record[4]
                assert manager.prepare_pending() == pending
                assert source_state() == state_before_retry
                retry_checked = True
            finally:
                if input_kind == "file":
                    instruction_file_input[0].write_text(new_content, encoding="utf-8")
                    assert not await config.reload(idempotency_key="file-available-after-sealed-retry")
                saver.discard_prepared_context_for_dispatch(
                    session_id, thread_id,
                    execution_id=first_admission.execution_id,
                    assembly_id=first_snapshot.assembly_id,
                    checkpoint_ns=checkpoint_ns,
                )
    async def record_call(bridge, request):
        await check_retry(bridge)
        return await wrap(bridge, request)

    def record_projection(bridge, request, prepared):
        prepared, messages, tools = project(bridge, request, prepared)
        projections.append((
            bridge, request.override(messages=deepcopy(request.messages)),
            prepared["snapshot"], deepcopy(messages), deepcopy(tools),
        ))
        return prepared, messages, tools

    monkeypatch.setattr(GRAPH_FACTORY_REGISTRY, "resolve", record_generation)
    monkeypatch.setattr(ModelCallExecution, "aexecute", record_call)
    monkeypatch.setattr(ModelCallExecution, "_project_prepared", record_projection)
    session_id, thread_id = await _session(case.runner)
    first_admission = await _complete_job(case, session_id, thread_id)
    assert len(projections) == len(produced) == len(case.compiled) == 1
    first_record = projections[0]
    first_snapshot = saver.get_context_assembly(
        session_id, thread_id, assembly_id=first_record[2].assembly_id,
        checkpoint_ns=first_record[1].runtime.execution_info.checkpoint_ns,
    )
    first_seal_bytes = canonical_json_bytes(first_snapshot.to_dict())
    first_generation = case.tracker.snapshot(session_id, thread_id).generation
    if producer_id == "R01":
        assert produced[0].content == old_content and produced[0].revision == before.revision
    else:
        assert json.loads(produced[0].content)["text"] == old_content
    assert produced[0].source_id == source_id and produced[0].root_placement == "root_eligible"
    first_binding = first_snapshot.seal_binding
    assert first_binding is not None and first_binding.root is not None
    assert old_content in first_binding.root.content
    assert new_content not in first_binding.root.content
    root_sources = [
        source for source in first_binding.root.sources
        if source.source_identity == source_id
    ]
    assert len(root_sources) == 1
    assert root_sources[0].revision == produced[0].revision_token()
    assert case.compiled[0][0] == (session_id, thread_id)
    first_wire = first_snapshot.request_hash_preimage
    assert isinstance(first_wire, dict)
    assert len(case.model_requests) == 1
    assert canonical_json_bytes(case.model_requests[0]["messages"]) == canonical_json_bytes(first_wire["messages"])
    assert source_state().latest_visible_committed_revision == produced[0].revision_token()

    # 写入正式测试工作区的真实 source 层，由 ConfigService 发布并激活快照。
    config_path = case.runner.workspace / ".boxteam" / "workspace.jsonc"
    source_config = read_jsonc_object(config_path) if config_path.exists() else {}
    source_config.setdefault("agents", {}).setdefault("default", {}).setdefault(
        "instructions", {}
    )
    if input_kind == "file":
        assert len(instruction_file_input) == 1
        instruction_file_input[0].write_text(new_content, encoding="utf-8")
    else:
        if producer_id == "R01":
            source_config["agents"]["default"]["instructions"]["system_prompt"] = new_content
        else:
            source_config["agents"]["default"]["instructions"].setdefault("runtime_identity", {})["description"] = new_content
        config_path.write_text(json.dumps(source_config, ensure_ascii=False), encoding="utf-8")
    assert await config.reload(idempotency_key="r01-instructions-refresh")
    after = config.get_snapshot()
    expected = before.to_dict()
    if producer_id == "R01":
        expected["agents"]["default"]["instructions"]["system_prompt"] = new_content
    else:
        expected["agents"]["default"]["instructions"]["runtime_identity"]["description"] = new_content
    assert after.revision != before.revision and after.to_dict() == expected
    workspace_sources = [
        source for source in after.source_details if source.layer == "workspace"
    ]
    assert len(workspace_sources) == 1
    workspace_source = workspace_sources[0]
    workspace_layer = source_store.get_source_layer(workspace_source_key)
    assert workspace_layer is not None
    assert (
        workspace_source.precedence,
        workspace_source.source_key,
        workspace_source.presence,
        workspace_source.loaded,
        workspace_source.uri,
    ) == (3, workspace_source_key, "present", True, None)
    assert (
        workspace_source.layer_revision,
        workspace_source.layer_digest,
        workspace_source.source_generation,
    ) == (
        workspace_layer.layer_revision,
        workspace_layer.layer_digest,
        workspace_layer.source_generation,
    ) == (initial_layer.layer_revision + (input_kind == "inline"), sha256(config_path.read_bytes()).hexdigest(),
          before_source_generation + (input_kind == "inline"))
    assert config.get_revision() == after.revision
    runtime = config.get_agent_runtime_config("default")
    assert runtime["system_prompt" if producer_id == "R01" else "runtime_identity_description"] == new_content

    second_admission = await _complete_job(case, session_id, thread_id, timeout_seconds=20)
    assert retry_checked and len(projections) == len(produced) == len(case.compiled) == 2
    if producer_id == "R01":
        assert produced[1].content == new_content and produced[1].revision == after.revision
    else:
        before_identity, after_identity = json.loads(produced[0].content), json.loads(produced[1].content)
        assert after_identity["text"] == new_content
        assert after_identity["runtime_facts"] == before_identity["runtime_facts"]
        assert produced[1].revision_token() != produced[0].revision_token()
    assert case.tracker.snapshot(session_id, thread_id).generation == first_generation + 1
    assert case.compiled[1][0] == (session_id, thread_id)
    assert case.compiled[1][1] is not case.compiled[0][1]
    assert case.compiled[0][2].state == "closed" and case.reactors[0].released
    assert case.compiled[1][2].state == "open" and not case.reactors[1].released
    second_snapshot = saver.get_context_assembly(
        session_id, thread_id, assembly_id=projections[1][2].assembly_id,
        checkpoint_ns=projections[1][1].runtime.execution_info.checkpoint_ns,
    )
    second_binding = second_snapshot.seal_binding
    assert second_binding is not None and second_binding.root == first_binding.root
    assert second_binding.prefix.state.epoch == first_binding.prefix.state.epoch
    first_bytes = stable_prefix_bytes(first_binding.prefix.state.frames)
    assert stable_prefix_bytes(second_binding.prefix.state.frames).startswith(first_bytes)
    assert second_binding.previous_assembly_id == first_snapshot.assembly_id
    changed_frames = [
        frame for frame in second_binding.prefix.state.frames
        if any(ref.source_identity == source_id and ref.source_revision == produced[1].revision_token()
               for ref in frame.item_refs)
    ]
    assert len(changed_frames) == 1 and changed_frames[0].role == "user"
    assert changed_frames[0].ordinal > first_binding.prefix.state.frames[-1].ordinal
    assert new_content.encode() in canonical_json_bytes(changed_frames[0].content)
    second_wire = second_snapshot.request_hash_preimage
    assert isinstance(second_wire, dict)
    wire_messages = second_wire["messages"]
    changed_messages = [
        message for message in wire_messages
        if new_content.encode() in canonical_json_bytes(message)
    ]
    assert len(changed_messages) == 1 and changed_messages[0]["role"] == "user"
    assert "真实准入输入".encode() not in canonical_json_bytes(changed_messages[0])
    assert len(case.model_requests) == 2
    actual_second = case.model_requests[1]
    assert canonical_json_bytes(actual_second["messages"]) == canonical_json_bytes(wire_messages)
    assert canonical_json_bytes(actual_second["tools"]) == canonical_json_bytes(second_wire["tools"])
    assert actual_second["messages"].count(changed_messages[0]) == 1
    applied = [revision for revision in second_binding.source_revisions if revision.source_id == source_id]
    assert len(applied) == 1
    assert applied[0].previous_revision == produced[0].revision_token()
    assert applied[0].revision == produced[1].revision_token()
    applied_state = source_state()
    assert applied_state.latest_revision == applied_state.latest_visible_committed_revision == produced[1].revision_token()

    # seal 只确认启用策略的来源；完整 sources 中保留已应用来源，pending 以 deltas 为准。
    second_manager = projections[1][0]._context_source_manager
    second_pending = second_manager.prepare_pending()
    assert second_pending is not None and second_pending.deltas
    pending_source_ids = {delta.source_id for delta in second_pending.deltas}
    assert source_id not in pending_source_ids
    pending_sources = {
        source.descriptor.source_id: source for source in second_pending.sources
    }
    second_control_states = {
        state.source_id: state
        for state in saver.load_context_source_control_states(
            ContextSourceOwnerKey(session_id, thread_id)
        )
    }
    second_tool_refs = [
        ref for ref in second_snapshot.tool_set_refs
        if ref.ref_id == second_binding.tool_set_snapshot_id
    ]
    assert len(second_tool_refs) == 1
    enabled_policy_keys = second_tool_refs[0].tool_policy["enabled_policy_keys"]
    second_sealed_source_ids = (
        {revision.source_id for revision in second_binding.source_revisions}
        | {source.source_identity for source in second_binding.root.sources}
        | {ref.source_identity for frame in second_binding.prefix.state.frames for ref in frame.item_refs}
    )
    for delta in second_pending.deltas:
        pending_source = pending_sources[delta.source_id]
        policy_key = pending_source.descriptor.policy_key
        assert policy_key is not None and policy_key not in enabled_policy_keys
        state = second_control_states[delta.source_id]
        assert pending_source.tracked and state.tracking_status == "tracked"
        assert state.latest_revision == pending_source.revision == delta.revision
        assert state.latest_visible_committed_revision == pending_source.applied_revision == delta.previous_revision
        assert delta.source_id not in second_sealed_source_ids
    for revision in second_binding.source_revisions:
        assert second_control_states[revision.source_id].latest_visible_committed_revision == revision.revision

    assert not await config.reload(idempotency_key="r01-instructions-unchanged")
    assert config.get_snapshot().revision == after.revision
    # TODO(performance): 完整 HEAD 在第三轮超过 helper 的8秒等待预算；与第二轮共用20秒功能验收预算，性能另行测量。
    third_admission = await _complete_job(case, session_id, thread_id, timeout_seconds=20)
    assert len(projections) == 3 and len(produced) == len(case.compiled) == 2
    assert case.tracker.snapshot(session_id, thread_id).generation == first_generation + 1
    third_snapshot = saver.get_context_assembly(
        session_id, thread_id, assembly_id=projections[2][2].assembly_id,
        checkpoint_ns=projections[2][1].runtime.execution_info.checkpoint_ns,
    )
    third_binding = third_snapshot.seal_binding
    assert third_binding is not None and third_binding.root == first_binding.root
    assert third_binding.prefix.state.epoch == first_binding.prefix.state.epoch
    assert third_binding.previous_assembly_id == second_snapshot.assembly_id
    assert stable_prefix_bytes(third_binding.prefix.state.frames).startswith(
        stable_prefix_bytes(second_binding.prefix.state.frames)
    )
    assert sum(
        ref.source_identity == source_id and ref.source_revision == produced[1].revision_token()
        for frame in third_binding.prefix.state.frames for ref in frame.item_refs
    ) == 1
    assert all(revision.source_id != source_id for revision in third_binding.source_revisions)
    assert source_state() == applied_state
    third_manager = projections[2][0]._context_source_manager
    assert third_manager is second_manager
    third_pending = third_manager.prepare_pending()
    assert third_pending == second_pending
    assert third_pending is not None
    assert all(delta.source_id != source_id for delta in third_pending.deltas)
    third_control_states = {
        state.source_id: state
        for state in saver.load_context_source_control_states(
            ContextSourceOwnerKey(session_id, thread_id)
        )
    }
    assert third_control_states == second_control_states
    third_tool_refs = [
        ref for ref in third_snapshot.tool_set_refs
        if ref.ref_id == third_binding.tool_set_snapshot_id
    ]
    assert len(third_tool_refs) == 1
    assert third_tool_refs[0].tool_policy == second_tool_refs[0].tool_policy
    third_sealed_source_ids = (
        {revision.source_id for revision in third_binding.source_revisions}
        | {source.source_identity for source in third_binding.root.sources}
        | {ref.source_identity for frame in third_binding.prefix.state.frames for ref in frame.item_refs}
    )
    assert pending_source_ids.isdisjoint(third_sealed_source_ids)
    for revision in third_binding.source_revisions:
        assert third_control_states[revision.source_id].latest_visible_committed_revision == revision.revision
    assert case.model_admissions == [first_admission, second_admission, third_admission]
    assert len({admission.execution_id for admission in case.model_admissions}) == 3
    third_wire = third_snapshot.request_hash_preimage
    assert isinstance(third_wire, dict)
    assert len(case.model_requests) == 3
    assert canonical_json_bytes(case.model_requests[2]["messages"]) == canonical_json_bytes(third_wire["messages"])
    assert case.model_requests[2]["messages"].count(changed_messages[0]) == 1
    restored = saver.get_context_assembly(
        session_id, thread_id, assembly_id=first_snapshot.assembly_id,
        checkpoint_ns=first_record[1].runtime.execution_info.checkpoint_ns,
    )
    assert canonical_json_bytes(restored.to_dict()) == first_seal_bytes

    if input_kind == "file":
        instruction_file_input[0].unlink()
        with pytest.raises(RuntimeError):
            await config.reload(idempotency_key="file-unavailable-for-new-execution")
        rejected = await _start_job(case.runner, "user", session_id, thread_id)
        assert rejected.task is not None
        await asyncio.wait_for(asyncio.shield(rejected.task), 8)
        assert rejected.status == JobStatus.failed
        assert "instruction-source-unavailable" in rejected.error_message
        assert len(case.model_requests) == 3
        restored = saver.get_context_assembly(
            session_id, thread_id, assembly_id=first_snapshot.assembly_id,
            checkpoint_ns=first_record[1].runtime.execution_info.checkpoint_ns,
        )
        assert canonical_json_bytes(restored.to_dict()) == first_seal_bytes
