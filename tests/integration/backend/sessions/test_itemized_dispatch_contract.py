"""经真实 LangChain dispatch、Provider TCP HTTP 和后端 history API 的合同。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.structured_output import (
    AutoStrategy,
    ProviderStrategy,
    ToolStrategy,
)
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.runtime import ExecutionInfo, Runtime
from pydantic import BaseModel

from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.providers.wire_finalize import chat_wire_profile, responses_wire_profile
from app.agents.sealed_assembly_dispatch import read_sealed_assembly_dispatch
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import current_execution_admission
from app.core.identifier import create_uuid_hex
from app.domain.itemized.assembly_snapshot import context_request_hash
from app.domain.itemized.epoch.prefix_epoch import (
    ProviderProfileChangeRequiresRebuildError,
)
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.serialization import normalize_wire_request
from app.schemas.internal_v2.tool_test import ToolTestStartRequest
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from app.tool_testing.definitions import PreparedToolTest, ToolTestEvaluation
from app.tool_testing.registry import ToolTestRegistry
from app.tool_testing.service import ToolTestService
from app.tool_testing.store import ToolTestStore
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    EMPTY_TOOL_SET,
    create_dispatch_agent,
    dispatch_execution_scope,
    dispatch_saver,
    invoke_provider_dispatch,
    native_dispatch_workspace,
    native_http_server,
    seed_dispatch_history,
    seed_dispatch_overlay,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    register_projection_draft,
)
from tests.support.workspaces import prepare_default_test_workspace

__all__ = ["dispatch_saver", "native_dispatch_workspace", "native_http_server"]


@tool
def inspect_file(path: str) -> str:
    """返回文件检查目标，用于验证工具调用合同。"""
    return f"inspected:{path}"


@pytest.fixture
def dispatch_bridge(dispatch_saver, registered_tool_set_producer_factory):
    saver, _session_id, _turn_id, _sessions = dispatch_saver

    def create(*, session_id, instruction, checkpointer, tools=()):
        thread = saver._storage.main_thread_id(session_id)
        manager = ContextSourceManager(
            owner=ContextSourceOwnerKey(session_id, thread), control_state_port=saver
        )
        manager.register(
            ContextSourceDescriptor(
                source_id="instruction:native-contract",
                source_kind="agent_instructions",
                name="native contract instruction",
                description="图注册的说明来源",
                internal_locator="instruction-producer://native-contract",
                root_placement="root_eligible",
            ),
            tracking_status="tracked",
        )
        manager.observe(
            "instruction:native-contract", instruction, revision="native-contract:1"
        )
        producer = registered_tool_set_producer_factory(tools)
        return SealedAssemblyDispatchBridge(
            checkpointer=checkpointer,
            session_id=session_id,
            thread_id=thread,
            context_source_manager=manager,
            tool_set_producer=producer,
        )

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_native_dispatch_sends_sealed_selection_over_real_http(
    dispatch_saver, native_http_server, mode, dispatch_bridge
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=state.api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
    )
    agent = create_dispatch_agent(
        model,
        tools=[inspect_file],
        middleware=[
            dispatch_bridge(
                session_id=session_id,
                instruction="HTTP system prompt",
                checkpointer=saver,
                tools=(inspect_file,),
            )
        ],
        checkpointer=saver,
    )
    with dispatch_execution_scope(saver, session_id, turn_id):
        if mode == "sync":
            result = agent.invoke(
                {"messages": []},
                build_checkpoint_config(saver._storage.main_thread_id(session_id)),
            )
        else:
            result = await agent.ainvoke(
                {"messages": []},
                build_checkpoint_config(saver._storage.main_thread_id(session_id)),
            )
    assert "native-http-result" in json.dumps(result["messages"][-1].content)
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None
    assert len(state.requests) == 1
    (snapshot,) = saver.list_context_assemblies(
        session_id, saver._storage.main_thread_id(session_id)
    )
    with RolloutCheckpointSaver(sessions) as restarted:
        restored = restarted.get_context_assembly(
            session_id,
            restarted._storage.main_thread_id(session_id),
            assembly_id=snapshot.assembly_id,
        )
        projection = restarted.project_context_plan_to_native(
            session_id,
            restarted._storage.main_thread_id(session_id),
            restored.as_sealed_plan(),
        )
    body = state.requests[0]["body"]
    for entry, encoded in zip(restored.selection, projection["selection"], strict=True):
        assert "detail_ref" not in encoded["ref"]
        if entry.detail_ref is not None:
            assert isinstance(entry.detail_ref, DetailRef)
            entry.detail_ref.require_owner(
                session_id, restored.thread_id, restored.assembly_id
            )
            assert encoded["detail_ref"] == entry.detail_ref.to_dict()
    assert body["input"] == projection["request"]["input"]
    assert body["tools"] == projection["request"]["tools"]
    # compiled root 独立记录 provenance；其余 wire source 忠实消费同一 selection。
    root = restored.seal_binding.root
    assert root.content == "HTTP system prompt"
    root_sources = tuple(
        source.source_binding.source_ref
        for source in restored.contributions
        if source.root_placement == "root_eligible"
    )
    assert tuple(source.source_identity for source in root.sources) == root_sources
    root_contribution_ids = {
        source.contribution_id
        for source in restored.contributions
        if source.root_placement == "root_eligible"
    }
    assert [source["plan_ordinal"] for source in projection["wire_sources"]] == [
        entry.plan_ordinal
        for entry in snapshot.selection
        if entry.included
        and entry.ref.ref_type != "tool_set"
        and entry.contribution_id not in root_contribution_ids
    ]
    assert body["input"][0] == {
        "role": "system",
        "content": [{"type": "input_text", "text": root.content}],
    }
    assert sum(item.get("role") == "system" for item in body["input"]) == 1
    assert [item["content"][0]["text"] for item in body["input"][-2:]] == [
        "HTTP overlay base",
        "HTTP overlay delta",
    ]
    assert body["tools"][0]["name"] == "inspect_file"
    assert body["tools"][0]["parameters"]["type"] == "object"
    assert body["tools"][0]["parameters"]["properties"]["path"]["type"] == "string"
    assert (
        not {"selection", "plan_hash", "assembly_id", "native_projection"} & body.keys()
    )


@pytest.mark.asyncio
async def test_concurrent_native_dispatch_does_not_share_context(
    dispatch_saver, native_http_server, session_bundle_factory, dispatch_bridge
) -> None:
    saver, first_session, turn_id, sessions = dispatch_saver
    second_session = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, second_session)
    seed_dispatch_history(saver, second_session)
    seed_dispatch_overlay(saver, second_session)
    state, endpoint = native_http_server

    async def dispatch(session_id: str) -> None:
        model = BoxteamOpenAIResponsesModel(
            model="openai/gpt-4.1",
            api_key=state.api_key,
            api_base=endpoint,
            custom_llm_provider="openai",
            streaming=True,
            max_retries=0,
        )
        agent = create_dispatch_agent(
            model,
            middleware=[
                dispatch_bridge(
                    session_id=session_id,
                    instruction=session_id,
                    checkpointer=saver,
                )
            ],
            checkpointer=saver,
        )
        with dispatch_execution_scope(saver, session_id, turn_id):
            await agent.ainvoke(
                {"messages": []},
                build_checkpoint_config(saver._storage.main_thread_id(session_id)),
            )

    await asyncio.gather(dispatch(first_session), dispatch(second_session))
    assert len(state.requests) == 2
    for session_id in (first_session, second_session):
        (snapshot,) = saver.list_context_assemblies(
            session_id, saver._storage.main_thread_id(session_id)
        )
        expected = saver.project_context_plan_to_native(
            session_id,
            saver._storage.main_thread_id(session_id),
            snapshot.as_sealed_plan(),
        )
        matching = [
            request["body"]
            for request in state.requests
            if session_id in json.dumps(request["body"], ensure_ascii=False)
        ]
        assert len(matching) == 1
        assert matching[0]["input"] == expected["request"]["input"]
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.asyncio
async def test_native_http_tool_loop_replays_canonical_call_and_result(
    dispatch_saver, native_http_server, dispatch_bridge
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    state.call_tool = True
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=state.api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
    )
    agent = create_dispatch_agent(
        model,
        tools=[inspect_file],
        middleware=[
            dispatch_bridge(
                session_id=session_id,
                instruction="HTTP tool loop",
                checkpointer=saver,
                tools=(inspect_file,),
            )
        ],
        checkpointer=saver,
    )
    with dispatch_execution_scope(saver, session_id, turn_id):
        result = await agent.ainvoke(
            {"messages": []},
            build_checkpoint_config(saver._storage.main_thread_id(session_id)),
        )
    assert "native-http-result" in json.dumps(result["messages"][-1].content)
    assert len(state.requests) == 2
    with RolloutCheckpointSaver(sessions) as restarted:
        snapshots = restarted.list_context_assemblies(
            session_id, restarted._storage.main_thread_id(session_id)
        )
        assert len(snapshots) == 2
        projections = [
            restarted.project_context_plan_to_native(
                session_id,
                restarted._storage.main_thread_id(session_id),
                snapshot.as_sealed_plan(),
            )
            for snapshot in snapshots
        ]
    for request in state.requests:
        assert request["body"]["input"] in [
            projection["request"]["input"] for projection in projections
        ]
    second_input = state.requests[1]["body"]["input"]
    (call,) = [item for item in second_input if item.get("type") == "function_call"]
    (output,) = [
        item for item in second_input if item.get("type") == "function_call_output"
    ]
    assert call["call_id"] == output["call_id"] == "call-native"
    assert json.loads(call["arguments"]) == {"path": "README.md"}
    assert output["output"] == "inspected:README.md"
    assert second_input.index(call) < second_input.index(output)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_missing_saver_owner_fails_before_provider_http(
    dispatch_saver, native_http_server, mode, dispatch_bridge
) -> None:
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=state.api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
    )
    # 缺少强制 owner 是错误配置，不以替身伪造 prepare/supports 成功。
    agent = create_dispatch_agent(
        model,
        middleware=[
            dispatch_bridge(
                session_id=session_id, instruction="missing owner", checkpointer=None
            )
        ],
        checkpointer=saver,
    )
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(TypeError, match="Saver prepare/supports"),
    ):
        if mode == "sync":
            agent.invoke(
                {"messages": []},
                build_checkpoint_config(saver._storage.main_thread_id(session_id)),
            )
        else:
            await agent.ainvoke(
                {"messages": []},
                build_checkpoint_config(saver._storage.main_thread_id(session_id)),
            )
    assert state.requests == []
    assert (
        saver.list_context_assemblies(
            session_id, saver._storage.main_thread_id(session_id)
        )
        == ()
    )
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.asyncio
async def test_provider_parameters_cannot_override_sealed_input(
    dispatch_saver, native_http_server, dispatch_bridge
) -> None:
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=state.api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
        model_kwargs={"instructions": "不能覆盖 sealed prompt"},
    )
    agent = create_dispatch_agent(
        model,
        middleware=[
            dispatch_bridge(
                session_id=session_id,
                instruction="sealed prompt",
                checkpointer=saver,
            )
        ],
        checkpointer=saver,
    )
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(ValueError, match="source-mismatch.*instructions"),
    ):
        await agent.ainvoke(
            {"messages": []},
            build_checkpoint_config(saver._storage.main_thread_id(session_id)),
        )
    assert state.requests == []
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("changed_field", ["instance_temperature", "request_temperature"])
async def test_new_seals_bind_effective_parameters_and_profile_over_real_http(
    dispatch_saver, native_http_server, dispatch_bridge, target_format, changed_field,
) -> None:
    """同 epoch 两次新 seal 使用相同来源与历史，实际正文和 hash 绑定有效参数。"""
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    thread = saver._storage.main_thread_id(session_id)
    checkpoint_id = saver.get_tuple(build_checkpoint_config(thread)).checkpoint["id"]
    bridge = dispatch_bridge(
        session_id=session_id, instruction="相同的已登记说明", checkpointer=saver,
        tools=[inspect_file],
    )
    snapshots = []

    async def send_actual_request(request: ModelRequest) -> ModelResponse:
        # 真实 bind_tools 和 SDK/TCP 发送；不把模型回答写回历史，保持两次 selection 相同。
        bound = request.model.bind_tools(request.tools)
        answer = await bound.ainvoke(request.messages, **request.model_settings)
        return ModelResponse(result=[answer])

    for ordinal, temperature in enumerate((0.1, 0.8)):
        model_type = {
            "chat_completions": BoxteamLiteLLMChatModel,
            "responses": BoxteamOpenAIResponsesModel,
        }[target_format]
        model = model_type(
            model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
            custom_llm_provider="openai", streaming=True, max_retries=0,
            temperature=temperature if changed_field == "instance_temperature" else None,
            image_input_replay=False,
        )
        if ordinal == 0:
            create_dispatch_agent(model, tools=[inspect_file], middleware=[bridge], checkpointer=saver)
        request = ModelRequest(
            model=model, messages=[], tools=[inspect_file],
            model_settings={"temperature": temperature} if changed_field == "request_temperature" else {},
            runtime=Runtime(execution_info=ExecutionInfo(
                checkpoint_id=checkpoint_id, checkpoint_ns="", thread_id=thread,
                task_id=f"wire-profile-contract:{changed_field}:{ordinal}",
            )),
        )
        prior_ids = {snapshot.assembly_id for snapshot in saver.list_context_assemblies(session_id, thread)}
        with dispatch_execution_scope(saver, session_id, turn_id):
            await bridge.awrap_model_call(request, send_actual_request)
        (snapshot,) = [
            snapshot for snapshot in saver.list_context_assemblies(session_id, thread)
            if snapshot.assembly_id not in prior_ids
        ]
        snapshots.append(snapshot)
        assert len(state.requests) == ordinal + 1
        assert snapshot.request_hash_preimage == normalize_wire_request(state.requests[-1]["body"])
        assert state.requests[-1]["body"]["temperature"] == temperature
        assert snapshot.request_hash_preimage["temperature"] == temperature

    first, second = snapshots
    assert first.assembly_id != second.assembly_id
    assert first.history_view_revision == second.history_view_revision
    assert first.source_overlay_epoch == second.source_overlay_epoch
    assert [
        (entry.ref.ref_id, entry.included, entry.content_hash, entry.source_revision, entry.plan_ordinal)
        for entry in first.selection
    ] == [
        (entry.ref.ref_id, entry.included, entry.content_hash, entry.source_revision, entry.plan_ordinal)
        for entry in second.selection
    ]
    assert first.request_hash != second.request_hash
    assert first.provider_version == second.provider_version
    assert (first.projector_id, first.projector_version) == (second.projector_id, second.projector_version)
    # 固定同一个 plan 排除新 assembly/detail 身份造成的假阳性，单独校验参数/profile 的 hash 影响。
    original_hash = context_request_hash(
        first.as_sealed_plan(), first.provider_version,
        projector_id=first.projector_id, projector_version=first.projector_version,
        target_format=target_format, wire_request=first.request_hash_preimage,
    )
    changed_hash = context_request_hash(
        first.as_sealed_plan(), second.provider_version,
        projector_id=first.projector_id, projector_version=first.projector_version,
        target_format=target_format, wire_request=second.request_hash_preimage,
    )
    assert original_hash == first.request_hash
    assert changed_hash != original_hash
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_ns", ["", "model:namespace-contract"])
@pytest.mark.parametrize("missing_assembly", [False, True])
async def test_dispatch_reads_committed_owner_namespace_before_real_provider_http(
    dispatch_saver, native_http_server, dispatch_bridge, monkeypatch,
    checkpoint_ns, missing_assembly,
) -> None:
    """真实 prepare/getter/send 共享 namespace；已提交记录缺失时发网前明确失败。"""
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    thread = saver._storage.main_thread_id(session_id)
    checkpoint_id = saver.get_tuple(build_checkpoint_config(thread)).checkpoint["id"]
    bridge = dispatch_bridge(
        session_id=session_id, instruction="namespace owner contract", checkpointer=saver,
    )
    model = BoxteamLiteLLMChatModel(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
    )
    create_dispatch_agent(model, middleware=[bridge], checkpointer=saver)
    request = ModelRequest(
        model=model, messages=[], tools=[], model_settings={},
        runtime=Runtime(execution_info=ExecutionInfo(
            checkpoint_id=checkpoint_id, checkpoint_ns=checkpoint_ns, thread_id=thread,
            task_id="namespace-contract",
        )),
    )
    original_getter = saver.get_context_assembly
    missing = KeyError("injected committed assembly lookup loss")
    seen_namespaces = []

    def checked_getter(*args, **kwargs):
        seen_namespaces.append(kwargs["checkpoint_ns"])
        if missing_assembly:
            raise missing
        return original_getter(*args, **kwargs)

    monkeypatch.setattr(saver, "get_context_assembly", checked_getter)

    async def send_actual_request(prepared: ModelRequest) -> ModelResponse:
        return ModelResponse(result=[await prepared.model.ainvoke(prepared.messages)])

    with dispatch_execution_scope(saver, session_id, turn_id):
        if missing_assembly:
            with pytest.raises(ValueError, match="source-mismatch.*assembly.*namespace") as error:
                await bridge.awrap_model_call(request, send_actual_request)
            assert error.value.__cause__ is missing
        else:
            await bridge.awrap_model_call(request, send_actual_request)
    assert seen_namespaces == [checkpoint_ns]
    (snapshot,) = saver.list_context_assemblies(session_id, thread)
    with RolloutCheckpointSaver(sessions) as restarted:
        restored = restarted.get_context_assembly(
            session_id, thread, assembly_id=snapshot.assembly_id, checkpoint_ns=checkpoint_ns,
        )
    assert restored == snapshot
    assert len(state.requests) == (0 if missing_assembly else 1)
    if not missing_assembly:
        assert snapshot.request_hash_preimage == normalize_wire_request(state.requests[0]["body"])
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("image_input_replay", [False, True])
async def test_profile_capability_is_bound_and_same_epoch_change_rejected_over_real_http(
    dispatch_saver, native_http_server, dispatch_bridge, target_format, image_input_replay,
) -> None:
    """两种能力各自首次封存发网；同 epoch 换 profile 必须重建且零额外 HTTP。"""
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    thread = saver._storage.main_thread_id(session_id)
    checkpoint_id = saver.get_tuple(build_checkpoint_config(thread)).checkpoint["id"]
    bridge = dispatch_bridge(
        session_id=session_id, instruction="相同的已登记说明", checkpointer=saver,
        tools=[inspect_file],
    )
    model_type = {
        "chat_completions": BoxteamLiteLLMChatModel,
        "responses": BoxteamOpenAIResponsesModel,
    }[target_format]
    profile = chat_wire_profile if target_format == "chat_completions" else responses_wire_profile
    model = model_type(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
        image_input_replay=image_input_replay,
    )
    changed_model = model_type(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
        image_input_replay=not image_input_replay,
    )
    create_dispatch_agent(model, tools=[inspect_file], middleware=[bridge], checkpointer=saver)

    async def send_actual_request(request: ModelRequest) -> ModelResponse:
        bound = request.model.bind_tools(request.tools)
        return ModelResponse(result=[await bound.ainvoke(request.messages, **request.model_settings)])

    def request_for(selected_model, ordinal):
        return ModelRequest(
            model=selected_model, messages=[], tools=[inspect_file], model_settings={},
            runtime=Runtime(execution_info=ExecutionInfo(
                checkpoint_id=checkpoint_id, checkpoint_ns="", thread_id=thread,
                task_id=f"wire-profile-capability:{image_input_replay}:{ordinal}",
            )),
        )

    with dispatch_execution_scope(saver, session_id, turn_id):
        await bridge.awrap_model_call(request_for(model, 0), send_actual_request)
    (snapshot,) = saver.list_context_assemblies(session_id, thread)
    assert len(state.requests) == 1
    assert snapshot.provider_version == profile(model).identity()
    assert snapshot.request_hash_preimage == normalize_wire_request(state.requests[0]["body"])
    original_hash = context_request_hash(
        snapshot.as_sealed_plan(), snapshot.provider_version,
        projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
        target_format=target_format, wire_request=snapshot.request_hash_preimage,
    )
    changed_hash = context_request_hash(
        snapshot.as_sealed_plan(), profile(changed_model).identity(),
        projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
        target_format=target_format, wire_request=snapshot.request_hash_preimage,
    )
    assert original_hash == snapshot.request_hash
    assert profile(changed_model).identity() != snapshot.provider_version
    assert changed_hash != original_hash
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(ProviderProfileChangeRequiresRebuildError, match="provider-profile-change-requires-rebuild"),
    ):
        await bridge.awrap_model_call(request_for(changed_model, 1), send_actual_request)
    assert len(state.requests) == 1
    assert saver.list_context_assemblies(session_id, thread) == (snapshot,)
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
async def test_agent_without_dispatch_binding_fails_before_provider_http(
    dispatch_saver, native_http_server, target_format, mode,
) -> None:
    """真实 Agent 缺少封存桥时，adapter 必须拒绝，不能退回普通消息发送。"""
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    model_type = {
        "chat_completions": BoxteamLiteLLMChatModel,
        "responses": BoxteamOpenAIResponsesModel,
    }[target_format]
    model = model_type(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
    )
    # 保留真实 Saver 与 admission；只漏装无状态 dispatch bridge。
    agent = create_agent(model, checkpointer=saver)
    thread = saver._storage.main_thread_id(session_id)
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(ValueError, match="source-mismatch.*binding"),
    ):
        if mode == "sync":
            agent.invoke({"messages": []}, build_checkpoint_config(thread))
        else:
            await agent.ainvoke({"messages": []}, build_checkpoint_config(thread))
    assert state.requests == []
    assert saver.list_context_assemblies(session_id, thread) == ()
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("changed_field", ["temperature", "image_input_replay"])
async def test_provider_drift_after_committed_seal_fails_before_http(
    dispatch_saver, native_http_server, dispatch_bridge, monkeypatch, target_format, changed_field,
) -> None:
    """先由真实 Saver 提交封存，再改实际模型；不得把发送漂移误当成新 seal。"""
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    thread = saver._storage.main_thread_id(session_id)
    model_type = {
        "chat_completions": BoxteamLiteLLMChatModel,
        "responses": BoxteamOpenAIResponsesModel,
    }[target_format]
    model = model_type(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
        temperature=0.1, image_input_replay=False,
    )
    sealed_ids = []
    prepare = saver.prepare_context_for_provider

    def seal_then_change_model(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        sealed_ids.append(prepared["assembly_id"])
        if changed_field == "temperature":
            model.temperature = 0.8
        else:
            model.image_input_replay = True
        return prepared

    monkeypatch.setattr(saver, "prepare_context_for_provider", seal_then_change_model)
    agent = create_dispatch_agent(
        model, tools=[inspect_file], checkpointer=saver,
        middleware=[dispatch_bridge(
            session_id=session_id, instruction="已封存后不得改变模型参数",
            checkpointer=saver, tools=[inspect_file],
        )],
    )
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(ValueError, match="source-mismatch"),
    ):
        await agent.ainvoke({"messages": []}, build_checkpoint_config(thread))
    assert len(sealed_ids) == 1
    (snapshot,) = saver.list_context_assemblies(session_id, thread)
    assert snapshot.assembly_id == sealed_ids[0]
    assert snapshot.request_hash_preimage["temperature"] == 0.1
    assert state.requests == []
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "port", ["provider", "native", "messages", "history", "bind", "seal"]
)
def test_foreign_session_plan_is_rejected_without_io(
    dispatch_saver, session_bundle_factory, monkeypatch, empty, port
) -> None:
    saver, source_id, turn_id, sessions = dispatch_saver
    target_id = f"ses_{create_uuid_hex()}"
    target = session_bundle_factory(sessions, target_id)
    source_thread = saver._storage.main_thread_id(source_id)
    target_thread = saver._storage.main_thread_id(target_id)
    draft = saver.compose_committed_context_plan(
        source_id,
        source_thread,
        plan_id=f"guard-{uuid4().hex}",
        tool_set=EMPTY_TOOL_SET,
    )
    if empty:
        draft = replace(draft, refs=(), contributions=(), tool_set_refs=())
    if port not in {"bind", "seal"}:
        draft = register_projection_draft(saver, source_id, draft)
        draft = saver.seal_context_plan(
            source_id,
            source_thread,
            draft,
            wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{draft.plan_id}",
            turn_id=turn_id,
            execution_id=saver.execution_for_turn(
                source_id, turn_id=turn_id, thread_id=source_thread
            ),
            provider_version="guard-contract",
        ).as_sealed_plan()
    before = {
        str(path.relative_to(target)): path.read_bytes() if path.is_file() else None
        for path in target.rglob("*")
    }
    source_assemblies = saver.list_context_assemblies(source_id, source_thread)
    io_calls = []

    def reject_io(*args, **kwargs):
        io_calls.append((args, kwargs))
        raise AssertionError("跨 session owner guard 前不得读写 storage/detail")

    # 先经真实 Saver seal/read 取得 plan，再在 I/O 边界设置哨兵；错误 owner
    # 必须在 source lookup 前拒绝，不能只依靠目标文件最终未变化来推断。
    with monkeypatch.context() as io_patch:
        for owner, names in (
            (
                saver._storage,
                (
                    "get_context_assembly",
                    "read_items",
                    "get_context_plan_detail",
                    "register_context_plan_detail",
                ),
            ),
            (saver._detail_store, ("read", "write", "remove")),
        ):
            for name in names:
                io_patch.setattr(owner, name, reject_io)
        with pytest.raises(ValueError, match="source-mismatch.*session/thread owner"):
            if port == "provider":
                saver.project_context_plan_to_provider(
                    target_id, target_thread, draft, target_format="responses"
                )
            elif port == "native":
                saver.project_context_plan_to_native(target_id, target_thread, draft)
            elif port == "messages":
                saver.project_context_plan_to_messages(target_id, target_thread, draft)
            elif port == "history":
                saver.project_context_plan_to_history_with_diagnostics(
                    target_id, target_thread, draft
                )
            elif port == "bind":
                saver._bind_request_detail_refs(
                    target_id, target_thread, "", draft, assembly_id="foreign-assembly"
                )
            else:
                saver.seal_context_plan(
                    target_id,
                    target_thread,
                    draft,
                    wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{draft.plan_id}",
                    turn_id=turn_id,
                    execution_id="foreign-execution",
                    provider_version="guard-contract",
                )
    assert {
        str(path.relative_to(target)): path.read_bytes() if path.is_file() else None
        for path in target.rglob("*")
    } == before
    assert io_calls == []
    assert not (target / "rollout/index.sqlite").exists()
    assert saver.list_context_assemblies(source_id, source_thread) == source_assemblies


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_content", "delta_content"),
    [
        pytest.param(
            {"policy": "基础规则"}, {"policy": "本轮规则变化"}, id="original-dicts",
        ),
        pytest.param(
            [{"type": "text", "text": "已知部分"}, {"policy": "基础规则"}],
            None, id="partially-known-list",
        ),
        pytest.param(
            [{"type": "text", "text": "基础规则"}],
            [{"type": "text", "text": 42}], id="malformed-delta",
        ),
        pytest.param(
            {"type": "text", "text": "基础规则", "policy": "不得丢弃字段"},
            None, id="unknown-text-field",
        ),
    ],
)
async def test_chat_rejects_unknown_overlay_before_assembly_and_provider_http(
    dispatch_saver, native_http_server, dispatch_bridge, session_bundle_factory,
    base_content, delta_content,
) -> None:
    saver, _seeded_session, _seeded_turn, sessions = dispatch_saver
    # 此来源必须在首个seal前验证，不能混入fixture已登记的其它overlay。
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    turn_id = seed_dispatch_history(saver, session_id)
    thread = saver._storage.main_thread_id(session_id)
    state, endpoint = native_http_server
    saver.register_source_overlay(
        SimpleNamespace(
            session_id=session_id, thread_id=thread, checkpoint_ns="",
            overlay_id="unsupported-chat-overlay", source_kind="workspace_policy",
            source_revision="policy-v2", source_overlay_epoch=0,
            base_ref="unsupported-chat-base",
            delta_ref=None if delta_content is None else "unsupported-chat-delta",
            base_source_revision="policy-v1",
            delta_source_revision=None if delta_content is None else "policy-v2",
            delta_from_revision=None if delta_content is None else "policy-v1",
            delta_to_revision=None if delta_content is None else "policy-v2",
            delta_diff_hash=None if delta_content is None else sha256_jcs(delta_content),
            status="active", idempotency_key="unsupported-chat-overlay",
        ),
        session_id=session_id, thread_id=thread,
        base_content=base_content, delta_content=delta_content,
    )
    bridge = dispatch_bridge(
        session_id=session_id, instruction="HTTP required instruction", checkpointer=saver,
    )
    manager = bridge._context_source_manager
    pending = manager.prepare_pending()
    control_before = saver.load_context_source_control_states(manager._owner)
    canonical_before = saver._storage.jsonl_path(session_id).read_bytes()
    with saver._storage._connect(thread, "", read_only=True, session_id=session_id) as connection:
        overlay_before = connection.execute(
            "SELECT * FROM source_overlays WHERE overlay_id = 'unsupported-chat-overlay'"
        ).fetchall()
        sources_before = connection.execute(
            "SELECT * FROM context_contributions "
            "WHERE contribution_id LIKE 'overlay:unsupported-chat-overlay:%' ORDER BY contribution_id"
        ).fetchall()
        available_before = connection.execute(
            "SELECT COUNT(*) FROM context_plan_details WHERE availability = 'available'"
        ).fetchone()
    model = BoxteamLiteLLMChatModel(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
    )
    agent = create_dispatch_agent(model, middleware=[bridge], checkpointer=saver)
    with dispatch_execution_scope(saver, session_id, turn_id), pytest.raises(
        ValueError, match="source-mismatch: Chat source frame 包含无法编码",
    ):
        await agent.ainvoke({"messages": []}, build_checkpoint_config(thread))
    assert state.requests == []
    assert saver.list_context_assemblies(session_id, thread) == ()
    assert manager.prepare_pending() == pending
    assert saver.load_context_source_control_states(manager._owner) == control_before
    assert saver._storage.jsonl_path(session_id).read_bytes() == canonical_before
    with saver._storage._connect(thread, "", read_only=True, session_id=session_id) as connection:
        assert connection.execute(
            "SELECT * FROM source_overlays WHERE overlay_id = 'unsupported-chat-overlay'"
        ).fetchall() == overlay_before
        assert connection.execute(
            "SELECT * FROM context_contributions "
            "WHERE contribution_id LIKE 'overlay:unsupported-chat-overlay:%' ORDER BY contribution_id"
        ).fetchall() == sources_before
        assert connection.execute(
            "SELECT COUNT(*) FROM context_plan_details WHERE availability = 'available'"
        ).fetchone() == available_before
        for table in ("context_assemblies", "context_assembly_selections", "context_assembly_contributions"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM context_plans WHERE plan_state = 'sealed'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_commits WHERE commit_kind = 'assembly_sealed'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT error_code FROM context_plan_seal_failures"
        ).fetchall() == [("source-mismatch",)]
        removed = connection.execute(
            "SELECT relative_path FROM context_plan_details WHERE availability = 'unavailable'"
        ).fetchall()
    thread_root = saver._storage._path_resolver.resolve_thread_node(session_id, thread)
    assert removed
    assert all(not (thread_root / path).exists() for (path,) in removed)


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_new_process_restores_overlay_for_real_provider_http_dispatch(
    dispatch_saver, native_http_server, target_format,
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    await invoke_provider_dispatch(
        saver, session_id, turn_id, endpoint, state.api_key, target_format=target_format,
    )
    thread = saver._storage.main_thread_id(session_id)
    (first,) = saver.list_context_assemblies(session_id, thread)
    restart_code = """import asyncio,sys
from pathlib import Path
from tests.support.session_catalog_owner import fixture_catalog_scope
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from tests.integration.backend.sessions.itemized_dispatch_helpers import invoke_provider_dispatch
with fixture_catalog_scope(), RolloutCheckpointSaver(Path(sys.argv[1])) as saver:
    asyncio.run(invoke_provider_dispatch(saver, *sys.argv[2:-1], target_format=sys.argv[-1]))
print('restarted-dispatch-ok')
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", restart_code, str(sessions), session_id, turn_id,
         endpoint, state.api_key, target_format],
        cwd=Path.cwd(), capture_output=True, text=True, check=False, timeout=45,
    )
    (state.artifacts / "restart.stdout.log").write_text(result.stdout, encoding="utf-8")
    (state.artifacts / "restart.stderr.log").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "restarted-dispatch-ok" in result.stdout
    assert len(state.requests) == 2
    model = BoxteamLiteLLMChatModel(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
    )
    with RolloutCheckpointSaver(sessions) as restarted:
        snapshots = restarted.list_context_assemblies(session_id, thread)
        (second,) = [snapshot for snapshot in snapshots if snapshot.assembly_id != first.assembly_id]
        assert second.history_view_revision > first.history_view_revision
        assert second.source_overlay_epoch == first.source_overlay_epoch
        assert restarted.get_context_assembly(
            session_id, thread, assembly_id=first.assembly_id,
        ) == first
        for snapshot, request in zip((first, second), state.requests, strict=True):
            # 独立 TCP 桩记录实际 HTTP 正文；封存必须绑定完整 wire，而非 LangChain 消息包装。
            assert snapshot.request_hash_preimage == normalize_wire_request(request["body"])
        if target_format == "responses":
            original_projection = restarted.project_context_plan_to_native(
                session_id, thread, first.as_sealed_plan(),
            )
            expected = restarted.project_context_plan_to_native(
                session_id, thread, second.as_sealed_plan(),
            )
            assert state.requests[0]["body"]["input"] == original_projection["request"]["input"]
            assert state.requests[1]["body"]["input"] == expected["request"]["input"]
            overlay_inputs = []
            for projection in (original_projection, expected):
                sources = [
                    source for source in projection["wire_sources"]
                    if source["ref_id"] in {"z-base", "a-delta"}
                ]
                assert [source["ref_id"] for source in sources] == ["z-base", "a-delta"]
                overlay_inputs.append([
                    projection["request"]["input"][source["input_index"]] for source in sources
                ])
            assert overlay_inputs[0] == overlay_inputs[1]
            assert [item["content"][0]["text"] for item in overlay_inputs[0]] == [
                "HTTP overlay base", "HTTP overlay delta",
            ]
        else:
            overlay_inputs = []
            for snapshot, request in zip((first, second), state.requests, strict=True):
                assert request["path"] == "/v1/chat/completions"
                messages, tools, losses = restarted.project_context_plan_to_provider(
                    session_id, thread, snapshot.as_sealed_plan(),
                    target_format="chat_completions",
                )
                assert tools == [] and losses == ()
                wire = model._convert_messages_to_dicts(messages)
                assert request["body"]["messages"] == wire
                positions = [
                    index for index, message in enumerate(messages) if message.id in {"z-base", "a-delta"}
                ]
                assert [messages[index].id for index in positions] == ["z-base", "a-delta"]
                overlay_inputs.append([wire[index] for index in positions])
                assert wire[0] == {"role": "system", "content": snapshot.seal_binding.root.content}
            assert overlay_inputs[0] == overlay_inputs[1] == [
                {"role": "user", "content": [{"type": "text", "text": text}]}
                for text in ("HTTP overlay base", "HTTP overlay delta")
            ]
        old_overlay = [entry for entry in first.selection if entry.base_delta_role != "none"]
        new_overlay = [entry for entry in second.selection if entry.base_delta_role != "none"]
        for old, new in zip(old_overlay, new_overlay, strict=True):
            assert old.ref.ref_id == new.ref.ref_id
            assert old.contribution_ordinal == new.contribution_ordinal
            assert old.content_hash == new.content_hash
            assert old.detail_ref != new.detail_ref
            old_body = restarted.read_context_plan_detail(
                session_id, thread, detail_ref=old.detail_ref,
            )["detail"]
            new_body = restarted.read_context_plan_detail(
                session_id, thread, detail_ref=new.detail_ref,
            )["detail"]
            assert old_body == new_body == [
                {"type": "text", "text": "HTTP overlay base" if old.base_delta_role == "base" else "HTTP overlay delta"}
            ]
            assert old.content_hash == contribution_content_hash(
                f"overlay_{old.base_delta_role}", old_body,
            )
        assert [frame.frame_bytes() for frame in second.seal_binding.prefix.state.frames[:len(first.seal_binding.prefix.state.frames)]] == [
            frame.frame_bytes() for frame in first.seal_binding.prefix.state.frames
        ]


@pytest.mark.asyncio
async def test_history_http_matches_inflight_dispatch_canonical_view(
    integration_client, integration_workspace_root_path, native_http_server
) -> None:
    response = await integration_client.post(
        "/api/v1/sessions", json={"title": "native HTTP history"}
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["data"]["session_id"]
    sessions = Path(integration_workspace_root_path) / ".boxteam/sessions"
    state, endpoint = native_http_server
    with RolloutCheckpointSaver(sessions) as saver:
        turn_id = seed_dispatch_history(saver, session_id)
        seed_dispatch_overlay(saver, session_id)
        state.release_response.clear()
        dispatch = asyncio.create_task(
            invoke_provider_dispatch(saver, session_id, turn_id, endpoint, state.api_key, target_format="responses")
        )
        try:
            assert await asyncio.to_thread(state.request_ready.wait, 20)
            (snapshot,) = saver.list_context_assemblies(
                session_id, saver._storage.main_thread_id(session_id)
            )
            expected = saver.project_context_plan_to_history(
                session_id,
                saver._storage.main_thread_id(session_id),
                snapshot.as_sealed_plan(),
            )
            response = await integration_client.post(
                f"/api/v1/sessions/{session_id}/history",
                json={
                    "turn_ids": [turn_id],
                    "include": ["user", "assistant_text", "metadata"],
                },
            )
            (state.artifacts / "history.json").write_text(
                response.text, encoding="utf-8"
            )
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"]
            page = response.json()["data"]
            (turn,) = page["items"]
            # DTO 的 source_message_ids 只标识用户输入，不是 assembly selection。
            # TODO: API 尚无 assembly selector/selection/loss，补齐后再验完整 6.5。
            assert turn["source_message_ids"] == [
                message.id for message in expected if isinstance(message, HumanMessage)
            ]
            assert turn["user_messages"][0]["content"] == "HTTP canonical user"
            assert turn["assistant_text"] == [
                message.content
                for message in expected
                if isinstance(message, AIMessage)
            ]
            assert turn["assistant_text"] == ["HTTP canonical answer"]
            assert (
                "HTTP overlay" not in response.text
                and "HTTP restart prompt" not in response.text
            )
        finally:
            state.release_response.set()
            await asyncio.wait_for(dispatch, timeout=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_field", ["session_id", "assembly_id", "detail_id"])
async def test_restart_rejects_corrupt_detail_owner_before_native_http(
    dispatch_saver, native_http_server, owner_field
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    await invoke_provider_dispatch(saver, session_id, turn_id, endpoint, state.api_key, target_format="responses")
    (snapshot,) = saver.list_context_assemblies(
        session_id, saver._storage.main_thread_id(session_id)
    )
    source = next(
        entry for entry in snapshot.selection if entry.base_delta_role == "base"
    )
    from app.core.path_utils import get_session_path_resolver

    session = get_session_path_resolver(sessions).resolve_thread_node(
        session_id, source.detail_ref.thread_id
    )
    path = session / detail_relative_path(source.detail_ref)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["detail_ref"][owner_field] = f"wrong-{owner_field}"
    path.write_bytes(canonical_json_bytes(payload))
    with (
        RolloutCheckpointSaver(sessions) as restarted,
        pytest.raises(
            (RuntimeError, ValueError), match="source-mismatch.*typed identity"
        ),
    ):
        await invoke_provider_dispatch(restarted, session_id, turn_id, endpoint, state.api_key, target_format="responses")
    assert len(state.requests) == 1
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.fixture
def nonagent_tool_probe_service(native_http_server, native_dispatch_workspace):
    """经真实默认模型构建器运行探针，独立于 Session/Saver 授权。"""
    state, endpoint = native_http_server
    state.call_tool = True
    template = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    workspace = prepare_default_test_workspace(
        workspace_root=native_dispatch_workspace / "nonagent-tool-probe",
        template_root=template,
    )
    expected = (workspace / "README.md").read_text(encoding="utf-8")
    provider = {
        "id": "nonagent-probe",
        "model": "openai/gpt-4.1",
        "endpoint": endpoint,
        "api_key": state.api_key,
        "custom_llm_provider": "openai",
        "api_mode": {
            "protocol": "responses",
            "model_info": {"supports_function_calling": True},
        },
    }

    class ProbeConfig:
        def get_agent_runtime_config(self, agent_id: str) -> dict[str, object]:
            assert agent_id == "default"
            return {"providers": [provider]}

    class ProbeCase:
        case_id = "nonagent-default-builder"
        tool_name = "inspect_file"
        title = "非 Agent 的真实工具探针"

        def prepare(
            self, *, workspace_root: Path, attempt_root: Path, asset_root: Path,
        ) -> PreparedToolTest:
            del workspace_root, asset_root
            prepare_default_test_workspace(
                workspace_root=attempt_root, template_root=template,
            )

            @tool
            def inspect_file(path: str) -> str:
                """读取隔离探针工作区中的文本文件。"""
                assert path == "README.md"
                return (attempt_root / path).read_text(encoding="utf-8")

            return PreparedToolTest(
                prompt="使用 inspect_file 读取 README.md", tool=inspect_file,
            )

        def evaluate(
            self, *, attempt_root: Path, tool_result: object,
        ) -> ToolTestEvaluation:
            del attempt_root
            return ToolTestEvaluation(
                passed=tool_result == expected, detail="校验实际读取的文件内容",
            )

    service = ToolTestService(
        config_service=ProbeConfig(),  # type: ignore[arg-type]
        registry=ToolTestRegistry(cases=[ProbeCase()]),
        store=ToolTestStore(root=workspace / ".boxteam/tool_tests"),
        workspace_root=workspace,
        asset_root=template,
    )
    return service, state, workspace


@pytest.mark.asyncio
async def test_nonagent_tool_service_uses_real_default_builder_and_http(
    nonagent_tool_probe_service,
) -> None:
    """用途门收紧后，真正的非 Agent 默认入口仍须成功而非假模型绿灯。"""
    service, state, workspace = nonagent_tool_probe_service
    assert current_execution_admission() is None
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None
    started = await service.start(
        tool_name="inspect_file", request=ToolTestStartRequest(),
    )
    try:
        await asyncio.gather(*tuple(service._tasks))
        result = service.get(started.run_id)
        assert result.status == "completed"
        assert len(result.attempts) == 1
        attempt = result.attempts[0]
        assert attempt.passed is True
        assert attempt.tool_called is True
        assert attempt.execution_succeeded is True
        assert attempt.model_calls == 1
        assert len(state.requests) == 1
        request = state.requests[0]
        assert request["path"] == "/v1/responses"
        assert request["authorization"] == f"Bearer {state.api_key}"
        assert request["body"]["tools"][0]["name"] == "inspect_file"
        assert not (workspace / ".boxteam/sessions").exists()
        assert current_execution_admission() is None
        assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None
    finally:
        await service.shutdown()


class StructuredAnswer(BaseModel):
    """图实际注册的输出 schema；名称故意排在普通工具之前。"""

    answer: str


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("strategy", ["tool", "provider", "auto_tool", "auto_provider"])
async def test_structured_output_graph_seals_registered_tools_once_over_http(
    dispatch_saver, native_http_server, dispatch_bridge, target_format, strategy,
):
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    uses_tool = strategy in {"tool", "auto_tool"}
    state.response_text = "native-http-result" if uses_tool else json.dumps({"answer": "native-http-result"})
    state.output_tool_name = "StructuredAnswer" if uses_tool else None
    model_type = {"chat_completions": BoxteamLiteLLMChatModel, "responses": BoxteamOpenAIResponsesModel}[target_format]
    model = model_type(
        model=("openai/gpt-3.5-turbo" if strategy == "auto_tool" else "openai/gpt-4.1"),
        api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
        profile={"structured_output": not uses_tool},
    )
    response_format = (
        ToolStrategy(StructuredAnswer) if strategy == "tool"
        else ProviderStrategy(StructuredAnswer) if strategy == "provider"
        else AutoStrategy(StructuredAnswer)
    )
    bridge = dispatch_bridge(
        session_id=session_id, instruction="按已注册输出 schema 回答", checkpointer=saver,
        tools=[inspect_file],
    )
    agent = create_dispatch_agent(
        model, tools=[inspect_file], middleware=[bridge], response_format=response_format,
        checkpointer=saver,
    )
    thread = saver._storage.main_thread_id(session_id)
    with dispatch_execution_scope(saver, session_id, turn_id):
        result = await agent.ainvoke({"messages": []}, build_checkpoint_config(thread))
    assert result["structured_response"] == StructuredAnswer(answer="native-http-result")
    assert len(state.requests) == 1
    (snapshot,) = saver.list_context_assemblies(session_id, thread)
    body = state.requests[0]["body"]
    assert snapshot.request_hash_preimage == normalize_wire_request(body)
    names = [item["name"] if target_format == "responses" else item["function"]["name"] for item in body["tools"]]
    assert names == (["inspect_file", "StructuredAnswer"] if uses_tool else ["inspect_file"])
    (tool_ref,) = snapshot.tool_set_refs
    assert len(tool_ref.tool_policy["wire_tool_order"]) == len(names)
    assert list(tool_ref.tool_policy["wire_tool_order"]) == names
    registered = {item["function"]["name"]: dict(item) for item in tool_ref.tools}
    expected_tools = [
        registered[name] if target_format == "chat_completions"
        else {"type": registered[name]["type"], **registered[name]["function"]}
        for name in names
    ]
    assert body["tools"] == expected_tools
    assert snapshot.seal_binding.applied_toolset_revision == tool_ref.source_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
async def test_schema_named_credentials_and_timestamp_survive_real_provider_http(
    dispatch_saver, native_http_server, dispatch_bridge, target_format,
):
    """声明名必须保留；同一已封存 plan 的 schema 类型变化必须改变哈希。"""
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    state.response_text = json.dumps({"answer": "native-http-result"})
    schema = {
        "title": "CredentialNamedAnswer", "type": "object",
        "properties": {
            "answer": {"type": "string"}, "timestamp": {"type": "string"},
            "password": {"type": "string"}, "access_token": {"type": "string"},
            "Authorization": {"type": "string"},
        },
        "required": ["answer"],
    }
    model_type = {
        "chat_completions": BoxteamLiteLLMChatModel,
        "responses": BoxteamOpenAIResponsesModel,
    }[target_format]
    model = model_type(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
        profile={"structured_output": True},
    )
    bridge = dispatch_bridge(
        session_id=session_id, instruction="只返回声明的 JSON 输出",
        checkpointer=saver, tools=[inspect_file],
    )
    agent = create_dispatch_agent(
        model, tools=[inspect_file], middleware=[bridge],
        response_format=ProviderStrategy(schema), checkpointer=saver,
    )
    thread = saver._storage.main_thread_id(session_id)
    with dispatch_execution_scope(saver, session_id, turn_id):
        result = await agent.ainvoke({"messages": []}, build_checkpoint_config(thread))
    assert result["structured_response"] == {"answer": "native-http-result"}
    assert len(state.requests) == 1
    (snapshot,) = saver.list_context_assemblies(session_id, thread)
    body = state.requests[0]["body"]
    assert snapshot.request_hash_preimage == normalize_wire_request(body)
    format_container = (
        body["response_format"]["json_schema"]
        if target_format == "chat_completions" else body["text"]["format"]
    )
    assert format_container["schema"]["properties"] == schema["properties"]
    original_hash = context_request_hash(
        snapshot.as_sealed_plan(), snapshot.provider_version,
        projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
        target_format=snapshot.target_format, wire_request=body,
    )
    assert original_hash == snapshot.request_hash
    changed_body = json.loads(json.dumps(body))
    changed_container = (
        changed_body["response_format"]["json_schema"]
        if target_format == "chat_completions" else changed_body["text"]["format"]
    )
    changed_container["schema"]["properties"]["timestamp"]["type"] = "integer"
    assert context_request_hash(
        snapshot.as_sealed_plan(), snapshot.provider_version,
        projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
        target_format=snapshot.target_format, wire_request=changed_body,
    ) != original_hash
