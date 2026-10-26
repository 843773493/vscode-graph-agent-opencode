"""经真实 LangChain dispatch、Provider TCP HTTP 和后端 history API 的合同。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing, nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from typing import Annotated, cast
from uuid import UUID, uuid4

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest
from langchain.agents.structured_output import (
    AutoStrategy,
    ProviderStrategy,
    ToolStrategy,
)
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.runtime import ExecutionInfo, Runtime
from pydantic import BaseModel

from app.agents.model_call_execution import ModelCallExecution
from app.agents.model_capability_routing import (
    CapabilityRoutingMiddleware,
    ProviderModelCandidate,
    current_selected_provider,
)
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.providers.wire_finalize import chat_wire_profile, responses_wire_profile
from app.agents.sealed_assembly_dispatch import (
    read_sealed_assembly_dispatch,
    sealed_assembly_dispatch_scope,
)
from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import current_execution_admission
from app.core.identifier import create_uuid_hex
from app.core.path_utils import get_session_catalog_components
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.prefix_epoch import (
    EpochReason,
    ProviderProfileChangeRequiresRebuildError,
    stable_prefix_bytes,
)
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.domain.itemized.serialization import normalize_wire_request
from app.schemas.internal_v2.tool_test import ToolTestStartRequest
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    acheckpoint_messages,
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import preparation
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
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.testing.model_stream import build_cassette
from app.tool_testing.definitions import PreparedToolTest, ToolTestEvaluation
from app.tool_testing.registry import ToolTestRegistry
from app.tool_testing.service import ToolTestService
from app.tool_testing.store import ToolTestStore
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    EMPTY_TOOL_SET,
    chat_reply_frames,
    create_dispatch_agent,
    dispatch_execution_scope,
    dispatch_saver,
    invoke_provider_dispatch,
    native_dispatch_workspace,
    native_http_server,
    responses_reply_frames,
    seed_dispatch_history,
    seed_dispatch_overlay,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    register_projection_draft,
)
from tests.integration.workspace_services.mcp.schema_server import (
    RESULT_AUDIO_BASE64,
    RESULT_IMAGE_BASE64,
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
        thread = saver._storage.storage.main_thread_id(session_id)
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
        return ModelCallExecution(
            context_read_admission=nullcontext,
            context_control_admission=lambda _operation, _payload, _ref: nullcontext(),
            checkpointer=checkpointer,
            session_id=session_id,
            thread_id=thread,
            context_source_manager=manager,
            tool_set_producer=producer,
            resource_activation=ResourceActivationCoordinator(
                registry=ResourceRegistry(), saver=saver,
                body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
            ),
        )

    return create


@pytest.fixture
def emitting_identity_inspection():
    context = ToolInvocationContext()
    observations: list[tuple[str, str]] = []

    @tool("inspect_file")
    def inspect_with_identity(path: str) -> str:
        """核验工具执行实际消费的 emitting model-call 身份。"""
        observations.append((context.require_tool_call_id(), context.require_model_call_id()))
        return f"inspected:{path}"

    return context, inspect_with_identity, observations


@pytest.fixture
def media_inspection_tool():
    @tool("inspect_file")
    def inspect_media(path: str, tool_call_id: Annotated[str, InjectedToolCallId]):
        """返回含媒体与资源引用的检查结果。"""
        return ToolMessage(content=[
            {"type": "text", "text": path},
            {"type": "image", "base64": RESULT_IMAGE_BASE64, "mime_type": "image/png"},
            {"type": "audio", "base64": RESULT_AUDIO_BASE64, "mime_type": "audio/wav"},
            {"type": "file", "url": "https://example.invalid/report", "mime_type": "text/plain"},
        ], artifact={"structured_content": {"path": path}}, tool_call_id=tool_call_id, name="inspect_file")
    return inspect_media


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_media_tool_loop_preserves_actual_http_body_hash_and_cold_state(
    dispatch_saver, native_http_server, dispatch_bridge, media_inspection_tool, mode, protocol,
):
    saver, session, turn, sessions = dispatch_saver
    state, endpoint = native_http_server
    state.call_tool = True
    model_type = BoxteamLiteLLMChatModel if protocol == "chat_completions" else BoxteamOpenAIResponsesModel
    model = model_type(model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
                       custom_llm_provider="openai", streaming=True, max_retries=0,
                       image_input_replay=True)
    agent = create_dispatch_agent(
        model, tools=[media_inspection_tool], checkpointer=saver,
        execution=dispatch_bridge(session_id=session, instruction="读取媒体", checkpointer=saver,
                                  tools=(media_inspection_tool,)),
    )
    with dispatch_execution_scope(saver, session, turn) as thread:
        config = cast(RunnableConfig, build_checkpoint_config(thread))
        if mode == "sync":
            await asyncio.to_thread(agent.invoke, {"messages": []}, config)
        else:
            await agent.ainvoke({"messages": []}, config)
    assert len(state.requests) == 2
    body = state.requests[-1]["body"]
    if protocol == "chat_completions":
        result, = [message for message in body["messages"] if message.get("role") == "tool"]
        assert [block["type"] for block in result["content"]] == ["text", "text"]
        assert "https://example.invalid/report" in result["content"][1]["text"]
        media = body["messages"][-1]
        assert media["role"] == "user"
        assert [block["type"] for block in media["content"]] == ["text", "image_url", "input_audio"]
        assert "call-native" in media["content"][0]["text"]
        assert media["content"][2]["input_audio"] == {"data": RESULT_AUDIO_BASE64, "format": "wav"}
    else:
        result, = [item for item in body["input"] if item.get("type") == "function_call_output"]
        assert "1 个图片媒体" in result["output"] and "1 个音频媒体" in result["output"]
        assert "https://example.invalid/report" in result["output"]
    with RolloutCheckpointSaver(sessions) as cold:
        snapshots = cold.list_context_assemblies(session, thread)
        for request in state.requests:
            matches = [snapshot for snapshot in snapshots
                       if snapshot.request_hash_preimage == normalize_wire_request(request["body"])]
            assert len(matches) == 1
            matches[0].validate_hashes()
        with session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread):
            messages = await acheckpoint_messages(cold, await cold.aget_tuple(config))
        restored, = [message for message in messages if isinstance(message, ToolMessage)]
        assert [block["type"] for block in restored.content] == ["text", "image", "audio", "file"]
        assert restored.artifact == {"structured_content": {"path": "README.md"}}


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
        execution=dispatch_bridge(
                session_id=session_id,
                instruction="HTTP system prompt",
                checkpointer=saver,
                tools=(inspect_file,),
            ),
        checkpointer=saver,
    )
    with dispatch_execution_scope(saver, session_id, turn_id):
        if mode == "sync":
            result = await asyncio.to_thread(
                agent.invoke,
                {"messages": []},
                cast(RunnableConfig, build_checkpoint_config(saver._storage.storage.main_thread_id(session_id))),
            )
        else:
            result = await agent.ainvoke(
                {"messages": []},
                cast(RunnableConfig, build_checkpoint_config(saver._storage.storage.main_thread_id(session_id))),
            )
    assert "native-http-result" in json.dumps(result["messages"][-1].content)
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None
    assert len(state.requests) == 1
    (snapshot,) = saver.list_context_assemblies(
        session_id, saver._storage.storage.main_thread_id(session_id)
    )
    with RolloutCheckpointSaver(sessions) as restarted:
        restored = restarted.get_context_assembly(
            session_id,
            restarted._storage.storage.main_thread_id(session_id),
            assembly_id=snapshot.assembly_id,
        )
        projection = restarted.project_context_plan_to_native(
            session_id,
            restarted._storage.storage.main_thread_id(session_id),
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
    assert [(block.source_identity, block.body) for block in parse_source_blocks(root.content)] == [("instruction:native-contract", "HTTP system prompt")]
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
    overlay_frame = restored.seal_binding.prefix.state.frames[-1]
    assert body["input"][-1] == {"role": "user", "content": [{"type": "input_text", "text": overlay_frame.content}]}
    assert [(ref.ref_id, block.body) for ref, block in zip(overlay_frame.item_refs, parse_source_blocks(overlay_frame.content), strict=True)] == [
        ("z-base", [{"type": "text", "text": "HTTP overlay base"}]),
        ("a-delta", [{"type": "text", "text": "HTTP overlay delta"}]),
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
            execution=dispatch_bridge(
                    session_id=session_id,
                    instruction=session_id,
                    checkpointer=saver,
                ),
            checkpointer=saver,
        )
        with dispatch_execution_scope(saver, session_id, turn_id):
            await agent.ainvoke(
                {"messages": []},
                cast(RunnableConfig, build_checkpoint_config(saver._storage.storage.main_thread_id(session_id))),
            )

    await asyncio.gather(dispatch(first_session), dispatch(second_session))
    assert len(state.requests) == 2
    for session_id in (first_session, second_session):
        (snapshot,) = saver.list_context_assemblies(
            session_id, saver._storage.storage.main_thread_id(session_id)
        )
        expected = saver.project_context_plan_to_native(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
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
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_native_http_tool_loop_replays_canonical_call_and_result(
    dispatch_saver, native_http_server, dispatch_bridge, emitting_identity_inspection, mode,
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    state.call_tool = True
    context, inspection_tool, observations = emitting_identity_inspection
    state.reasoning_item = {
        "id": "rs-native", "type": "reasoning", "status": "completed",
        "summary": [{"index": 0, "type": "summary_text", "text": ""}],
        "content": [{"type": "reasoning_text", "text": "先核验工具调用身份。"}],
        "encrypted_content": "encrypted-native-reasoning",
        "provider_extension": {"trace_group": "native-group"},
    }
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
        tools=[inspection_tool],
        execution=dispatch_bridge(
                session_id=session_id,
                instruction="HTTP tool loop",
                checkpointer=saver,
                tools=(inspection_tool,),
            ), middleware=[ToolInvocationContextMiddleware(context)],
        checkpointer=saver,
    )
    with dispatch_execution_scope(saver, session_id, turn_id):
        config = cast(RunnableConfig, build_checkpoint_config(saver._storage.storage.main_thread_id(session_id)))
        if mode == "sync":
            result = await asyncio.to_thread(agent.invoke, {"messages": []}, config)
        else:
            result = await agent.ainvoke({"messages": []}, config)
    assert "native-http-result" in json.dumps(result["messages"][-1].content)
    assert len(state.requests) == 2
    with RolloutCheckpointSaver(sessions) as restarted:
        snapshots = restarted.list_context_assemblies(
            session_id, restarted._storage.storage.main_thread_id(session_id)
        )
        assert len(snapshots) == 2
        projections = [
            restarted.project_context_plan_to_native(
                session_id,
                restarted._storage.storage.main_thread_id(session_id),
                snapshot.as_sealed_plan(),
            )
            for snapshot in snapshots
        ]
        config = build_checkpoint_config(restarted._storage.storage.main_thread_id(session_id))
        if mode == "sync":
            restored = await asyncio.to_thread(restarted.get_tuple, config)
            assert restored is not None
            restored_messages = await asyncio.to_thread(checkpoint_messages, restarted, restored)
        else:
            restored = await restarted.aget_tuple(config)
            assert restored is not None
            restored_messages = await acheckpoint_messages(restarted, restored)
        assert [message.id for message in restored_messages] == [message.id for message in result["messages"]]
        (assistant,) = [message for message in restored_messages if isinstance(message, AIMessage) and message.tool_calls]
        (tool_result,) = [message for message in restored_messages if isinstance(message, ToolMessage)]
        codec = LangChainMessageCodec()
        assert assistant.id.startswith("resp_")
        assert assistant.content == [{"type": "reasoning_items", "reasoning_items": [state.reasoning_item]}]
        assert codec.model_call_id(assistant) == codec.model_call_id(tool_result) == snapshots[0].model_call_id
        assert observations == [("call-native", snapshots[0].model_call_id)]
        (reasoning_carrier,) = [
            item for item in restarted.read_canonical_items(session_id, restarted._storage.storage.main_thread_id(session_id))
            if item.semantic_kind == "reasoning" and item.payload_kind == "extension"
            and item.payload["wire_type"] == "reasoning_items"
        ]
        assert reasoning_carrier.payload["value"] == assistant.content[0]
        assert reasoning_carrier.projection_identity.model_call_id == snapshots[0].model_call_id
        assert f"{reasoning_carrier.item_id}:reasoning/extension" in projections[1]["losses"]
        index = restarted._storage.storage._path_resolver.resolve_thread_node(
            session_id, restarted._storage.storage.main_thread_id(session_id),
        ) / "rollout/index.sqlite"
        with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) as connection:
            pairs = connection.execute(
                "SELECT semantic_kind,tool_call_id,projection_manifest_json FROM item_catalog "
                "WHERE semantic_kind IN ('tool_call','tool_result') ORDER BY item_sequence",
            ).fetchall()
        assert [kind for kind, _, _ in pairs] == ["tool_call", "tool_result"]
        assert all(call_id == "call-native" for _, call_id, _ in pairs)
        assert all(
            json.loads(manifest)["projection_identity"]["model_call_id"] == snapshots[0].model_call_id
            for _, _, manifest in pairs
        )
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
    # 缺少强制 owner 是错误配置，不以替身伪造 prepare 成功。
    agent = create_dispatch_agent(
        model,
        execution=dispatch_bridge(
                session_id=session_id, instruction="missing owner", checkpointer=None
            ),
        checkpointer=saver,
    )
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(TypeError, match="itemized context 缺少必需的 Saver prepare 端口"),
    ):
        if mode == "sync":
            await asyncio.to_thread(
                agent.invoke,
                {"messages": []},
                cast(RunnableConfig, build_checkpoint_config(saver._storage.storage.main_thread_id(session_id))),
            )
        else:
            await agent.ainvoke(
                {"messages": []},
                cast(RunnableConfig, build_checkpoint_config(saver._storage.storage.main_thread_id(session_id))),
            )
    assert state.requests == []
    assert (
        saver.list_context_assemblies(
            session_id, saver._storage.storage.main_thread_id(session_id)
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
        execution=dispatch_bridge(
                session_id=session_id,
                instruction="sealed prompt",
                checkpointer=saver,
            ),
        checkpointer=saver,
    )
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(ValueError, match="source-mismatch.*instructions"),
    ):
        await agent.ainvoke(
            {"messages": []},
            cast(RunnableConfig, build_checkpoint_config(saver._storage.storage.main_thread_id(session_id))),
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
    thread = saver._storage.storage.main_thread_id(session_id)
    checkpoint_id = saver.get_tuple(build_checkpoint_config(thread)).checkpoint["id"]
    bridge = dispatch_bridge(
        session_id=session_id, instruction="相同的已登记说明", checkpointer=saver,
        tools=[inspect_file],
    )
    snapshots = []

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
            create_dispatch_agent(model, tools=[inspect_file], execution=bridge, checkpointer=saver)
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
            await bridge.aexecute(request)
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
@pytest.mark.parametrize(
    "checkpoint_ns, owner_namespace", [("", ""), ("model:namespace-contract", "")]
)
@pytest.mark.parametrize("missing_assembly", [False, True])
async def test_cold_dispatch_retry_reads_committed_owner_namespace_before_real_provider_http(
    dispatch_saver, native_http_server, dispatch_bridge, monkeypatch,
    checkpoint_ns, owner_namespace, missing_assembly,
) -> None:
    """真实 sealed retry 经 authority reader 冷恢复；失败时不得发送第二个 HTTP。"""
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    thread = saver._storage.storage.main_thread_id(session_id)
    checkpoint_id = saver.get_tuple(build_checkpoint_config(thread)).checkpoint["id"]
    bridge = dispatch_bridge(
        session_id=session_id, instruction="namespace owner contract", checkpointer=saver,
    )
    model = BoxteamLiteLLMChatModel(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
    )
    create_dispatch_agent(model, execution=bridge, checkpointer=saver)
    request = ModelRequest(
        model=model, messages=[], tools=[], model_settings={},
        runtime=Runtime(execution_info=ExecutionInfo(
            checkpoint_id=checkpoint_id, checkpoint_ns=checkpoint_ns, thread_id=thread,
            task_id="namespace-contract",
        )),
    )
    original_reader = saver._storage.assemblies._read_context_assembly_connection
    missing = KeyError(f"injected committed assembly lookup loss: namespace={owner_namespace!r}")
    seen_owners = []

    def checked_reader(
        connection, owner_session_id, owner_thread_id, *, assembly_id, checkpoint_ns,
        registration=None,
    ):
        seen_owners.append((owner_session_id, owner_thread_id, checkpoint_ns, assembly_id))
        if missing_assembly:
            raise missing
        return original_reader(
            connection, owner_session_id, owner_thread_id,
            assembly_id=assembly_id, checkpoint_ns=checkpoint_ns, registration=registration,
        )

    # 首次 seal 的 request-local snapshot 完成真实 HTTP dispatch；随后同一 attempt 的
    # cold retry 必须通过 authority reader 重新验证持久事实。
    with dispatch_execution_scope(saver, session_id, turn_id):
        await bridge.aexecute(request)
    assert len(state.requests) == 1

    with monkeypatch.context() as cold_read_patch:
        cold_read_patch.setattr(
            saver._storage.assemblies,
            "_read_context_assembly_connection",
            checked_reader,
        )
        with dispatch_execution_scope(saver, session_id, turn_id):
            if missing_assembly:
                with pytest.raises(KeyError, match="committed assembly lookup loss") as error:
                    await bridge.aexecute(request)
                assert error.value is missing
            else:
                await bridge.aexecute(request)

    (snapshot,) = saver.list_context_assemblies(session_id, thread)
    assert seen_owners
    assert set(seen_owners) == {(session_id, thread, owner_namespace, snapshot.assembly_id)}
    with RolloutCheckpointSaver(sessions) as restarted:
        restored = restarted.get_context_assembly(
            session_id, thread, assembly_id=snapshot.assembly_id, checkpoint_ns=checkpoint_ns,
        )
    assert restored == snapshot
    assert len(state.requests) == (1 if missing_assembly else 2)
    assert snapshot.request_hash_preimage == normalize_wire_request(state.requests[-1]["body"])
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("failure", ["http", "stream"])
async def test_cross_protocol_fallback_keeps_frozen_context_through_tool_loop_and_restart(
    dispatch_saver, native_http_server, dispatch_bridge, mode, failure,
) -> None:
    """真实候选失败→换协议→工具→再次换候选，不能被旧 profile 锁死。"""
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    state.responses_failure = failure
    state.call_tool = True
    options = {"model": "openai/gpt-4.1", "api_key": state.api_key, "api_base": endpoint,
               "custom_llm_provider": "openai", "streaming": True, "max_retries": 0}
    primary = BoxteamOpenAIResponsesModel(**options)
    backup = BoxteamLiteLLMChatModel(**options)
    routing = CapabilityRoutingMiddleware([
        ProviderModelCandidate("responses-first", "gpt-4.1", primary, frozenset({"text_input"})),
        ProviderModelCandidate("chat-backup", "gpt-4.1", backup, frozenset({"text_input"})),
    ])
    agent = create_dispatch_agent(
        primary, tools=[inspect_file], checkpointer=saver, middleware=[routing],
        execution=dispatch_bridge(session_id=session_id, instruction="fallback 必须保留的原始说明",
                                  checkpointer=saver, tools=(inspect_file,)),
    )
    thread = saver._storage.storage.main_thread_id(session_id)
    with dispatch_execution_scope(saver, session_id, turn_id):
        config = cast(RunnableConfig, build_checkpoint_config(thread))
        result = await asyncio.to_thread(agent.invoke, {"messages": []}, config) if mode == "sync" else await agent.ainvoke({"messages": []}, config)
    assert result["messages"][-1].text == "native-http-result"
    tool_results = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(tool_results) == 1
    assert tool_results[0].text == "inspected:README.md"
    assert [request["path"] for request in state.requests] == [
        "/v1/responses", "/v1/chat/completions", "/v1/responses", "/v1/chat/completions",
    ]
    snapshots = saver.list_context_assemblies(session_id, thread)
    assert len(snapshots) == 4
    assert snapshots[0].seal_binding.prefix.state.epoch.reason == EpochReason.INITIAL
    assert [snapshot.seal_binding.prefix.state.epoch.ordinal for snapshot in snapshots] == [1, 2, 3, 4]
    for index, snapshot in enumerate(snapshots):
        assert snapshot.request_hash_preimage == normalize_wire_request(state.requests[index]["body"])
        assert snapshot.request_hash == context_request_hash(
            snapshot.as_sealed_plan(), snapshot.provider_version,
            projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
            target_format=snapshot.target_format, wire_request=snapshot.request_hash_preimage,
        )
        assert [(block.source_identity, block.body) for block in parse_source_blocks(snapshot.seal_binding.root.content)] == [("instruction:native-contract", "fallback 必须保留的原始说明")]
        assert snapshot.seal_binding.root.sources == snapshots[0].seal_binding.root.sources
        if index:
            old = snapshots[index - 1]
            assert snapshot.seal_binding.previous_assembly_id == old.assembly_id
            assert snapshot.seal_binding.prefix.state.epoch.reason == EpochReason.PROVIDER_CHANGED
            assert snapshot.seal_binding.prefix.parent is None
            assert snapshot.seal_binding.prefix.consumed_transition is None
            old_frames = old.seal_binding.prefix.state.frames
            new_frames = snapshot.seal_binding.prefix.state.frames[:len(old_frames)]
            assert [(frame.role, frame.payload_kind, frame.content) for frame in new_frames] == [
                (frame.role, frame.payload_kind, frame.content) for frame in old_frames
            ]
    assert snapshots[0].plan_hash == snapshots[1].plan_hash
    assert snapshots[0].request_hash != snapshots[1].request_hash
    with RolloutCheckpointSaver(sessions) as restarted:
        assert restarted.list_context_assemblies(session_id, thread) == snapshots
        for snapshot in snapshots:
            assert restarted.get_context_assembly(session_id, thread, assembly_id=snapshot.assembly_id) == snapshot
    assert current_selected_provider() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("image_input_replay", [False, True])
async def test_profile_capability_is_bound_and_same_epoch_change_rejected_over_real_http(
    dispatch_saver, native_http_server, dispatch_bridge, target_format, image_input_replay,
) -> None:
    """两种能力各自首次封存发网；同 epoch 换 profile 必须重建且零额外 HTTP。"""
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    thread = saver._storage.storage.main_thread_id(session_id)
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
    create_dispatch_agent(model, tools=[inspect_file], execution=bridge, checkpointer=saver)

    def request_for(selected_model, ordinal):
        return ModelRequest(
            model=selected_model, messages=[], tools=[inspect_file], model_settings={},
            runtime=Runtime(execution_info=ExecutionInfo(
                checkpoint_id=checkpoint_id, checkpoint_ns="", thread_id=thread,
                task_id=f"wire-profile-capability:{image_input_replay}:{ordinal}",
            )),
        )

    with dispatch_execution_scope(saver, session_id, turn_id):
        await bridge.aexecute(request_for(model, 0))
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
        await bridge.aexecute(request_for(changed_model, 1))
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
    thread = saver._storage.storage.main_thread_id(session_id)
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(ValueError, match="source-mismatch.*binding"),
    ):
        if mode == "sync":
            agent.invoke({"messages": []}, cast(RunnableConfig, build_checkpoint_config(thread)))
        else:
            await agent.ainvoke({"messages": []}, cast(RunnableConfig, build_checkpoint_config(thread)))
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
    thread = saver._storage.storage.main_thread_id(session_id)
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
        execution=dispatch_bridge(
            session_id=session_id, instruction="已封存后不得改变模型参数",
            checkpointer=saver, tools=[inspect_file],
        ),
    )
    with (
        dispatch_execution_scope(saver, session_id, turn_id),
        pytest.raises(ValueError, match="source-mismatch"),
    ):
        await agent.ainvoke({"messages": []}, cast(RunnableConfig, build_checkpoint_config(thread)))
    assert len(sealed_ids) == 1
    (snapshot,) = saver.list_context_assemblies(session_id, thread)
    assert snapshot.assembly_id == sealed_ids[0]
    assert snapshot.request_hash_preimage["temperature"] == 0.1
    assert state.requests == []
    assert read_sealed_assembly_dispatch(invocation_purpose="tool_test") is None


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "port", ["dispatch", "provider", "native", "messages", "history", "bind", "seal"]
)
def test_foreign_session_plan_is_rejected_without_io(
    dispatch_saver, session_bundle_factory, monkeypatch, empty, port
) -> None:
    saver, source_id, turn_id, sessions = dispatch_saver
    target_id = f"ses_{create_uuid_hex()}"
    target = session_bundle_factory(sessions, target_id)
    source_thread = saver._storage.storage.main_thread_id(source_id)
    target_thread = saver._storage.storage.main_thread_id(target_id)
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
    expected_owner_error = (
        "source-mismatch: target rollout is not materialized; "
        "frozen history scope cannot be authorized"
        if port in {"dispatch", "provider", "native", "messages", "history"}
        else "source-mismatch.*session/thread owner"
    )
    with monkeypatch.context() as io_patch:
        for owner, names in (
            (
                saver._storage.assemblies,
                (
                    "get_context_assembly",
                    "get_context_plan_detail",
                    "register_context_plan_detail",
                ),
            ),
            (saver._storage.storage, ("read_items",)),
            (saver._detail_store, ("read", "write", "remove")),
        ):
            for name in names:
                io_patch.setattr(owner, name, reject_io)
        with pytest.raises(ValueError, match=expected_owner_error):
            if port == "dispatch":
                saver.project_context_plan_for_dispatch(
                    target_id, target_thread, draft, target_format="responses"
                )
            elif port == "provider":
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
    thread = saver._storage.storage.main_thread_id(session_id)
    state, endpoint = native_http_server
    thread_root = saver._storage.storage._path_resolver.resolve_thread_node(session_id, thread)
    detail_dirs = (
        thread_root / "rollout" / "context-plan-details",
    )
    detail_files_before = {
        path.relative_to(thread_root): path.read_bytes()
        for root in detail_dirs for path in root.rglob("*") if path.is_file()
    }
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
    canonical_before = saver._storage.storage.jsonl_path(session_id).read_bytes()
    with saver._storage.storage._connect(thread, "", read_only=True, session_id=session_id) as connection:
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
        details_before = connection.execute(
            "SELECT * FROM context_plan_details ORDER BY assembly_id, detail_id"
        ).fetchall()
    model = BoxteamLiteLLMChatModel(
        model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
    )
    agent = create_dispatch_agent(model, execution=bridge, checkpointer=saver)
    with dispatch_execution_scope(saver, session_id, turn_id), pytest.raises(
        ValueError, match="source-mismatch: Chat source frame 包含无法编码",
    ):
        await agent.ainvoke({"messages": []}, cast(RunnableConfig, build_checkpoint_config(thread)))
    assert state.requests == []
    assert saver.list_context_assemblies(session_id, thread) == ()
    assert manager.prepare_pending() == pending
    assert saver.load_context_source_control_states(manager._owner) == control_before
    assert saver._storage.storage.jsonl_path(session_id).read_bytes() == canonical_before
    with saver._storage.storage._connect(thread, "", read_only=True, session_id=session_id) as connection:
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
        assert connection.execute(
            "SELECT * FROM context_plan_details ORDER BY assembly_id, detail_id"
        ).fetchall() == details_before
    # prepare 只冻结内存正文；投影拒绝发生在发布前，不应遗留 detail 或 tombstone。
    assert {
        path.relative_to(thread_root): path.read_bytes()
        for root in detail_dirs for path in root.rglob("*") if path.is_file()
    } == detail_files_before


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
    thread = saver._storage.storage.main_thread_id(session_id)
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
                    projection["request"]["input"][index]
                    for index in dict.fromkeys(source["input_index"] for source in sources)
                ])
            assert overlay_inputs[0] == overlay_inputs[1]
            assert [item["content"][0]["text"] for item in overlay_inputs[0]] == [
                frame.content for frame in first.seal_binding.prefix.state.frames
                if any(ref.ref_id in {"z-base", "a-delta"} for ref in frame.item_refs)
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
                assert [messages[index].id for index in positions] == ["z-base"]
                overlay_inputs.append([wire[index] for index in positions])
                assert wire[0] == {"role": "system", "content": snapshot.seal_binding.root.content}
            assert overlay_inputs[0] == overlay_inputs[1] == [
                {"role": "user", "content": frame.content}
                for frame in first.seal_binding.prefix.state.frames
                if any(ref.ref_id in {"z-base", "a-delta"} for ref in frame.item_refs)
            ]
        assert [(ref.ref_id, block.body)
                for frame in first.seal_binding.prefix.state.frames
                if frame.role == "user" and frame.source_protocol is not None
                for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
                if ref.ref_id in {"z-base", "a-delta"}] == [
            ("z-base", [{"type": "text", "text": "HTTP overlay base"}]),
            ("a-delta", [{"type": "text", "text": "HTTP overlay delta"}]),
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
                session_id, saver._storage.storage.main_thread_id(session_id)
            )
            expected = saver.project_context_plan_to_history(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
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
        session_id, saver._storage.storage.main_thread_id(session_id)
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
        model, tools=[inspect_file], execution=bridge, response_format=response_format,
        checkpointer=saver,
    )
    thread = saver._storage.storage.main_thread_id(session_id)
    with dispatch_execution_scope(saver, session_id, turn_id):
        result = await agent.ainvoke({"messages": []}, cast(RunnableConfig, build_checkpoint_config(thread)))
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
        model, tools=[inspect_file], execution=bridge,
        response_format=ProviderStrategy(schema), checkpointer=saver,
    )
    thread = saver._storage.storage.main_thread_id(session_id)
    with dispatch_execution_scope(saver, session_id, turn_id):
        result = await agent.ainvoke({"messages": []}, cast(RunnableConfig, build_checkpoint_config(thread)))
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


@tool
def inspect_folder(folder: str) -> str:
    """真实图注册目录的第二个工具。"""
    return f"folder:{folder}"


@tool
def search_notes(query: str) -> str:
    """真实图注册目录的第三个工具。"""
    return f"notes:{query}"


def _build_toolset_dispatch_case(
    dispatch_saver, dispatch_bridge, target_format, monkeypatch, *, endpoint, api_key,
):
    """两种传输共用真实 ToolSelectionStore、producer 与 Bridge/Saver 设置。"""
    saver, session, turn, sessions = dispatch_saver
    thread = saver._storage.storage.main_thread_id(session)
    checkpoint_id = saver.get_tuple(build_checkpoint_config(thread)).checkpoint["id"]
    tools = (inspect_file, inspect_folder, search_notes)
    model_type = {
        "responses": BoxteamOpenAIResponsesModel,
        "chat_completions": BoxteamLiteLLMChatModel,
    }[target_format]
    model = model_type(
        model="openai/gpt-4.1", api_key=api_key, api_base=endpoint,
        custom_llm_provider="openai", streaming=True, max_retries=0,
    )
    bridge = dispatch_bridge(
        session_id=session, instruction="ToolSet sealed owner 合同",
        checkpointer=saver, tools=tools,
    )
    create_dispatch_agent(model, tools=list(tools), execution=bridge, checkpointer=saver)
    producer = bridge._tool_set_producer
    selection = producer._selection_reader
    assert isinstance(selection, ToolSelectionStore)
    selection.apply_changes(
        agent_id="dispatch-test",
        changes={item.name: (item.name == "inspect_file", item.name == "inspect_file") for item in tools},
    )
    dispatches = []

    async def dispatch(task_id, *, messages=()):
        incoming = ModelRequest(
            model=model, messages=list(messages), tools=list(tools), model_settings={},
            runtime=Runtime(execution_info=ExecutionInfo(
                thread_id=thread, checkpoint_ns="", checkpoint_id=checkpoint_id,
                task_id=task_id, node_attempt=1,
            )),
        )
        outbound = []

        original_invoke = bridge._ainvoke

        async def send_actual(prepared, messages, tools):
            # handler 只读已成功提交的 assembly，真实 SDK 还会联校 finalized wire/hash。
            handle = read_sealed_assembly_dispatch(invocation_purpose="agent")
            assert handle is not None
            if target_format == "responses":
                native, snapshot = handle.native_projection, handle.snapshot
                assert (native["session_id"], native["assembly_id"], native["plan_hash"]) == (
                    snapshot.session_id, snapshot.assembly_id, snapshot.plan_hash,
                )
                assert native["selection"] == [entry.to_dict() for entry in snapshot.selection]
            dispatches.append(handle)
            outbound.append(handle.snapshot)
            return await original_invoke(prepared, messages, tools)

        with dispatch_execution_scope(saver, session, turn), monkeypatch.context() as dispatch_patch:
            dispatch_patch.setattr(bridge, "_ainvoke", send_actual)
            await bridge.aexecute(incoming)
        assert len(outbound) == 1
        return outbound[0]

    return SimpleNamespace(
        saver=saver, session=session, turn=turn, thread=thread, sessions=sessions,
        target_format=target_format, bridge=bridge, producer=producer,
        selection=selection, manager=bridge._context_source_manager,
        tools=tools, dispatch=dispatch, dispatches=dispatches,
        model=model,
    )


@pytest.fixture
def native_toolset_dispatch_case(
    dispatch_saver, native_http_server, dispatch_bridge, target_format, monkeypatch,
):
    """真实网络的失败重试与 in-flight 专项保留 socket 和原始请求记录。"""
    state, endpoint = native_http_server
    case = _build_toolset_dispatch_case(
        dispatch_saver, dispatch_bridge, target_format, monkeypatch,
        endpoint=endpoint, api_key=state.api_key,
    )
    actual_dispatch = case.dispatch

    async def dispatch(task_id, *, messages=()):
        snapshot = await actual_dispatch(task_id, messages=messages)
        assert snapshot.request_hash_preimage == normalize_wire_request(state.requests[-1]["body"])
        return snapshot

    case.state, case.dispatch = state, dispatch
    return case


@pytest.fixture
async def toolset_dispatch_case(
    dispatch_saver, inprocess_model_replay, dispatch_bridge, target_format, monkeypatch,
):
    """普通封存/控制面集成使用共享精确回放；不伪造 HTTP 请求状态。"""
    replay, controller = inprocess_model_replay
    endpoint = "https://toolset-replay.invalid/v1"
    protocol = "openai_responses_sse" if target_format == "responses" else "openai_chat_sse"
    suffix = "/responses" if target_format == "responses" else "/chat/completions"
    frames = responses_reply_frames if target_format == "responses" else chat_reply_frames
    cassette = build_cassette(
        asset_id="toolset-reply", url=endpoint + suffix, model="gpt-4.1",
        protocol=protocol, frames=frames(1),
    )
    interaction = {**cassette.raw["interactions"][0], "protocol": protocol}
    case = _build_toolset_dispatch_case(
        dispatch_saver, dispatch_bridge, target_format, monkeypatch,
        endpoint=endpoint, api_key="toolset-replay-key",
    )
    planned = set()
    actual_dispatch = case.dispatch

    def read_request(snapshot):
        # Bridge 重试保留 sealed ID，传输身份来自本次真实 callback，不能读到旧产物。
        reference = next(handle.reference for handle in reversed(case.dispatches)
                         if handle.snapshot.assembly_id == snapshot.assembly_id)
        reference.validate_snapshot(snapshot)
        artifact = json.loads((replay.artifacts / f"{reference.callback_model_call_id}.json").read_text(encoding="utf-8"))
        assert artifact["identity"] == {
            "session_id": case.session, "thread_id": case.thread,
            "model_call_id": reference.callback_model_call_id,
        }
        assert snapshot.request_hash_preimage == normalize_wire_request(artifact["request"])
        return artifact["request"]

    async def dispatch(task_id, *, messages=()):
        namespace = f"model_call/{case.session}/{case.thread}"
        next_call = str(UUID(hex=replay.control.next_uuid(namespace)))
        if next_call not in planned:
            planned.update(replay.control.plan_model_calls(case.session, case.thread, [interaction]))
        before = controller.transport.call_count
        snapshot = await actual_dispatch(task_id, messages=messages)
        assert controller.transport.call_count == before + 1
        assert controller.transport.request_urls[-1] == endpoint + suffix
        read_request(snapshot)
        return snapshot

    controller.ensure_installed()
    case.controller, case.request = controller, read_request
    case.artifacts, case.dispatch = replay.artifacts.parent, dispatch
    return case


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_typed_dispatch_reference_blocks_each_binding_mismatch_before_http(
    toolset_dispatch_case, target_format,
):
    case = toolset_dispatch_case
    snapshot = await case.dispatch("reference-source")
    reference = case.dispatches[-1].reference
    native = case.dispatches[-1].native_projection
    changes = {
        "session_id": "foreign-session", "thread_id": "foreign-thread",
        "execution_id": "foreign-execution", "attempt_key": "foreign-attempt",
        "checkpoint_ns": "foreign-namespace", "assembly_id": "foreign-assembly",
        "callback_model_call_id": str(uuid4()), "provider_version": "foreign-provider",
        "prefix_epoch": "foreign-epoch", "epoch_reason": "foreign-reason",
        "prefix_hash": "foreign-prefix", "frame_hashes": ("foreign-frame",),
        "tool_set_refs": (), "tool_policy_hash": "foreign-policy",
    }
    for field, value in changes.items():
        with (
            dispatch_execution_scope(case.saver, case.session, case.turn),
            pytest.raises(ValueError, match="sealed-assembly binding violation"),
            sealed_assembly_dispatch_scope(replace(reference, **{field: value}), snapshot, resource_refs=(), native_projection=native),
        ):
            await case.model.ainvoke([])
        assert case.controller.transport.call_count == 1, field
    with (
        pytest.raises(TypeError, match="dispatch reference"),
        # 有意向类型边界注入缺失引用，证明真实发送前拒绝。
        sealed_assembly_dispatch_scope(None, snapshot, resource_refs=(), native_projection=native),  # pyright: ignore[reportArgumentType]
    ):
        await case.model.ainvoke([])
    assert case.controller.transport.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_cold_dispatch_reference_reads_exact_payload_without_preparation(
    toolset_dispatch_case, monkeypatch, target_format,
):
    case = toolset_dispatch_case
    snapshot = await case.dispatch("reference-cold-retry")
    reference = case.dispatches[-1].reference
    original_request = case.request(snapshot)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("只读 dispatch 不得重取 CSM/ToolSet 或重新准备")

    monkeypatch.setattr(case.manager, "prepare_sources", unexpected)
    monkeypatch.setattr(case.selection, "snapshot", unexpected)
    with RolloutCheckpointSaver(case.sessions) as restarted:
        monkeypatch.setattr(restarted, "prepare_context_for_provider", unexpected)
        payload = restarted.read_context_for_dispatch(reference)
        assert payload["snapshot"] == snapshot
        assert payload["execution_id"] == snapshot.execution_id
        assert restarted._prepared_dispatches == {}
        with (
            dispatch_execution_scope(restarted, case.session, case.turn),
            sealed_assembly_dispatch_scope(reference, payload["snapshot"], resource_refs=payload["resource_refs"], native_projection=payload.get("native_projection")),
        ):
            await case.model.bind_tools(payload["tools"]).ainvoke(payload["messages"])
        assert restarted.get_context_assembly(case.session, case.thread, assembly_id=snapshot.assembly_id) == snapshot
    assert case.controller.transport.call_count == 2
    assert case.request(snapshot) == original_request
    assert sorted(case.controller.transport.hit_counts.values()) == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
@pytest.mark.parametrize("callback_mismatch", [False, True], ids=["matched-callback", "stale-callback"])
async def test_sealed_retry_callback_reference_reaches_real_step_adapter(
    native_toolset_dispatch_case, target_format, callback_mismatch, monkeypatch,
):
    """真实 HTTP 失败后重试同一 payload；每次 callback 精确消费自己的引用。"""
    case = native_toolset_dispatch_case
    admission = current_execution_admission(langgraph_thread_id=case.thread)
    assert admission is not None
    consumed_ids = []
    consume = case.saver.consume_prepared_context_for_dispatch

    def consume_actual(*args, **kwargs):
        consumed_ids.append(kwargs["model_call_id"])
        return consume(*args, **kwargs)

    monkeypatch.setattr(case.saver, "consume_prepared_context_for_dispatch", consume_actual)
    adapter = StepModelCallAdapter(
        checkpointer=case.saver, session_id=case.session, thread_id=case.thread,
        job_id=admission.job_id, checkpoint_ns="",
    )
    references = []
    callback_ids = []
    outcomes = []
    reject_retry_callback = False

    def record_bindings():
        (case.state.artifacts / "step-callback-bindings.json").write_text(json.dumps({
            "references": [asdict(reference) for reference in references],
            "callback_model_call_ids": callback_ids, "consumed_model_call_ids": consumed_ids,
            "outcomes": outcomes, "http_request_count": len(case.state.requests),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    class StepCallbacks(AsyncCallbackHandler):
        raise_error = True

        async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
            dispatch = read_sealed_assembly_dispatch(invocation_purpose="agent")
            assert dispatch is not None
            reference = dispatch.reference
            assert str(run_id) == reference.callback_model_call_id
            callback_ids.append(str(run_id))
            references.append(reference)
            call_id = callback_ids[0] if reject_retry_callback else str(run_id)
            execution_id = await adapter.register(call_id, len(references), case.model.model)
            assert execution_id == reference.execution_id
            assert case.saver._prepared_dispatches == {}

        async def on_llm_error(self, error, *, run_id, **kwargs):
            outcomes.append((str(run_id), "failed"))
            await adapter.update_outcome(str(run_id), "failed")

        async def on_llm_end(self, response, *, run_id, **kwargs):
            outcomes.append((str(run_id), "completed"))
            await adapter.update_outcome(str(run_id), "completed")

    case.model.callbacks = [StepCallbacks()]
    # 两次失败后再成功，确保下一次冷恢复仍引用最初封存的 model-call，
    # 而持久 model_calls 链逐次指向上一个真实 callback。
    case.state.response_status = 503
    with pytest.raises(Exception, match="503|ServiceUnavailable|InternalServerError"):
        await case.dispatch("reference-step-retry")
    if callback_mismatch:
        reject_retry_callback = True
        with pytest.raises(RuntimeError, match="callback 没有唯一 prepared reference"):
            await case.dispatch("reference-step-retry")
        record_bindings()
        assert len(case.state.requests) == 1
        assert len(set(callback_ids)) == 2
        assert consumed_ids == [callback_ids[0], callback_ids[0]]
        pending = [prepared for queue in case.saver._prepared_dispatches.values() for prepared in queue]
        assert [prepared["dispatch_ref"] for prepared in pending] == [references[-1]]
        with case.saver._storage.storage._connect(case.thread, session_id=case.session, read_only=True) as connection:
            rows = connection.execute("SELECT model_call_id, outcome FROM model_calls").fetchall()
        assert rows == [(callback_ids[0], "failed")]
        return
    with pytest.raises(Exception, match="503|ServiceUnavailable|InternalServerError"):
        await case.dispatch("reference-step-retry")
    case.state.response_status = 200
    snapshot = await case.dispatch("reference-step-retry")
    record_bindings()
    assert len(references) == len(case.state.requests) == 3
    assert len(set(callback_ids)) == 3
    assert consumed_ids == callback_ids
    assert outcomes == list(zip(callback_ids, ["failed", "failed", "completed"], strict=True))
    assert {reference.sealed_model_call_id for reference in references} == {snapshot.model_call_id}
    assert callback_ids[0] == snapshot.model_call_id
    for reference in references:
        reference.validate_snapshot(snapshot)
        assert (reference.assembly_id, reference.request_hash) == (snapshot.assembly_id, snapshot.request_hash)
    assert all(request["body"] == case.state.requests[0]["body"] for request in case.state.requests)
    with case.saver._storage.storage._connect(case.thread, session_id=case.session, read_only=True) as connection:
        rows = connection.execute(
            "SELECT model_call_id, retry_of_model_call_id, assembly_id, execution_id, attempt, outcome "
            "FROM model_calls WHERE session_id=? AND thread_id=? ORDER BY attempt",
            (case.session, case.thread),
        ).fetchall()
    assert rows == [
        (call_id, callback_ids[index - 1] if index else None, snapshot.assembly_id,
         snapshot.execution_id, index + 1, outcomes[index][1])
        for index, call_id in enumerate(callback_ids)
    ]
    assert case.saver.list_context_assemblies(case.session, case.thread) == (snapshot,)


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_real_bridge_dispatch_uses_seal_projection_without_post_ack_reread(
    toolset_dispatch_case, monkeypatch, target_format,
):
    """真实 Bridge 和 Provider 复用本次 seal 收据，ack 后不再冷读 assembly。"""
    case = toolset_dispatch_case
    storage = case.saver._storage.storage
    original_ack = case.manager.acknowledge_sealed_pending
    original_open = storage.open_read_snapshot
    original_cold_project = case.saver.project_context_plan_for_dispatch
    acknowledged, post_ack_snapshots, cold_projection_calls = [], [], []

    def acknowledge(*args, **kwargs):
        receipt = original_ack(*args, **kwargs)
        acknowledged.append(receipt.assembly_id)
        return receipt

    def open_snapshot(*args, **kwargs):
        snapshot = original_open(*args, **kwargs)
        if acknowledged:
            post_ack_snapshots.append(snapshot)
        return snapshot

    def cold_project(*args, **kwargs):
        cold_projection_calls.append((args, kwargs))
        return original_cold_project(*args, **kwargs)

    monkeypatch.setattr(case.manager, "acknowledge_sealed_pending", acknowledge)
    monkeypatch.setattr(storage, "open_read_snapshot", open_snapshot)
    monkeypatch.setattr(case.saver, "project_context_plan_for_dispatch", cold_project)
    snapshot = await case.dispatch("single-seal-receipt")
    (handle,) = case.dispatches
    assert acknowledged == [snapshot.assembly_id]
    assert cold_projection_calls == []
    assert post_ack_snapshots == []
    assert case.controller.transport.call_count == 1
    body = case.request(snapshot)
    assert snapshot.request_hash_preimage == normalize_wire_request(body)
    if target_format == "responses":
        native = handle.native_projection
        assert native is not None
        assert native["selection"] == [entry.to_dict() for entry in snapshot.selection]
        assert (native["session_id"], native["assembly_id"], native["plan_hash"]) == (
            snapshot.session_id, snapshot.assembly_id, snapshot.plan_hash,
        )
        assert (body["input"], body["tools"]) == (native["request"]["input"], native["request"]["tools"])
    else:
        assert handle.native_projection is None


def _select_contract_tools(case, *names):
    """只修改真实 desired 控制面，不调用任何 applied writer。"""
    selected = frozenset(names)
    case.selection.apply_changes(
        agent_id="dispatch-test",
        changes={item.name: (item.name in selected, item.name in selected) for item in case.tools},
    )
    return case.selection.snapshot("dispatch-test")


@pytest.fixture
def preparation_session_service(toolset_dispatch_case):
    """沿原 dispatch owner 注入正式 Session gate 与持久 context-control lease。"""
    case = toolset_dispatch_case
    components = get_session_catalog_components(case.sessions)
    service = SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=case.sessions),
        workspace_id=components.resolver.workspace_id,
        path_resolver=components.resolver, creation_service=components.creation_service,

    )
    case.bridge._context_read_admission = lambda: service.session_read_admission(case.session)
    case.bridge._context_control_admission = lambda operation, payload, ref: service.session_context_control_admission(
        case.session, case.thread, operation_id=operation, payload=payload, recovery_ref=ref,
    )
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["chat_completions"])
@pytest.mark.parametrize("stage", ["read", "boundary", "activation_boundary", "commit"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_real_preparation_worker_keeps_loop_and_admission_until_settled(
    toolset_dispatch_case, preparation_session_service, monkeypatch, stage, cancelled, target_format,
):
    """真实 Bridge/Saver worker 暂停时 loop 可运行；取消不能早退原 gate/lease。"""
    del target_format
    case, service = toolset_dispatch_case, preparation_session_service
    loop, loop_thread = asyncio.get_running_loop(), get_ident()
    entered, released = asyncio.Event(), Event()
    settled, queued_ready = Event(), asyncio.Event()
    method_name = {
        "read": "find_sealed_context_for_provider_creation",
        "boundary": "prepare_model_call_boundary",
        "activation_boundary": "prepare_model_call_boundary",
        "commit": "prepare_context_for_provider",
    }[stage]
    actual = getattr(case.saver, method_name)
    observed_admissions = []
    boundary_calls = 0
    operations = []
    admit = case.bridge._context_control_admission

    def record_admission(operation, payload, ref):
        operations.append(operation)
        return admit(operation, payload, ref)

    monkeypatch.setattr(case.bridge, "_context_control_admission", record_admission)

    def pause_worker(*args, **kwargs):
        nonlocal boundary_calls
        boundary_calls += 1
        if stage == "activation_boundary" and boundary_calls == 1:
            return actual(*args, **kwargs)
        assert get_ident() != loop_thread, "同步 Saver preparation 仍在 backend loop 中执行"
        observed_admissions.append(current_execution_admission())
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(10), "preparation worker 未收到有限屏障释放"
        try:
            return actual(*args, **kwargs)
        finally:
            settled.set()

    monkeypatch.setattr(case.saver, method_name, pause_worker)
    running = asyncio.create_task(case.dispatch(f"worker-settlement-{stage}-{cancelled}"))

    async def queue_next_turn():
        queued_ready.set()
        async with service.session_write_admission(case.session):
            assert settled.is_set(), "queued admission 早于仍在提交的 preparation worker"
            return case.saver.accept_turn(
                case.session, thread_id=case.thread,
                accepted_ingress_id="queued-after-worker", acceptance_idempotency_key="queued-after-worker-accept",
                job_id="queued-after-worker-job", admission_idempotency_key="queued-after-worker-admit",
                payload="worker 结算后接受的队列输入", turn_id="queued-after-worker-turn",
            )

    queued = None
    control_path = service.path_resolver.resolve_session_node(case.session) / "session-control.sqlite"
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert observed_admissions and observed_admissions[0].thread_id == case.thread
        queued = asyncio.create_task(queue_next_turn())
        await asyncio.wait_for(queued_ready.wait(), 5)
        if cancelled:
            for _ in range(3):
                running.cancel()
                await asyncio.sleep(0)
        assert not running.done()
        assert not queued.done()
        assert not settled.is_set()
        with closing(SessionControlStore(control_path)) as control:
            context_leases = [lease for lease in control.list_non_terminal_leases() if lease.operation_kind == "context_control"]
            assert len(context_leases) == (0 if stage == "read" else 1)
            assert all(lease.state == "settling" for lease in context_leases)
    finally:
        released.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 20)
        else:
            await asyncio.wait_for(running, 20)
        if queued is not None:
            await asyncio.wait_for(queued, 20)
    assert settled.is_set()
    assert all(admission == observed_admissions[0] for admission in observed_admissions)
    assert case.controller.transport.call_count == (0 if cancelled else 1)
    with closing(SessionControlStore(control_path)) as control:
        assert not [lease for lease in control.list_non_terminal_leases() if lease.operation_kind == "context_control"]
        for operation in operations:
            assert control.find_lease_by_operation(f"context-control:{operation}").state == "completed"
    assemblies = case.saver.list_context_assemblies(case.session, case.thread)
    assert len(assemblies) == (0 if cancelled and stage != "commit" else 1)
    for assembly in assemblies:
        assert "worker 结算后接受的队列输入" not in json.dumps(assembly.request_hash_preimage, ensure_ascii=False)


def _sealed_contract_rows(case):
    """保存已提交 SQLite 原字符串，证明旧 assembly 持久字节没有被更新。"""
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        return tuple(tuple(row) for row in snapshot.connection.execute(
            "SELECT assembly_id, snapshot_json, plan_hash, request_hash "
            "FROM context_assemblies WHERE status IN ('sealed','terminal') ORDER BY rowid"
        ))


def _latest_contract_assembly(case):
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        return case.saver._storage.assemblies.latest_context_assembly(snapshot)


def _contract_wire_tool_names(case, body):
    return tuple(
        entry["name"] if case.target_format == "responses" else entry["function"]["name"]
        for entry in body.get("tools", ())
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_real_request_prefix_chain_keeps_tool_group_full_delta_and_retry_bytes(
    toolset_dispatch_case, target_format,
):
    """正常 Provider 链逐字节继承 root/canonical/full/delta；仅工具切换重建 epoch。"""
    del target_format
    case = toolset_dispatch_case
    initial = await case.dispatch("prefix-chain-initial")
    case.manager.register(ContextSourceDescriptor(
        source_id="prefix-chain:tail", source_kind="workspace_policy",
        name="前缀来源", description="工具组之后的完整正文与增量",
        internal_locator="instruction-producer://prefix-chain-tail", root_placement="tail_only",
    ), tracking_status="tracked")
    case.manager.observe("prefix-chain:tail", "完整来源 A\n", revision="tail:A")
    call = AIMessage(content="读取两个文件", id="prefix-chain-call", tool_calls=[
        {"name": "inspect_file", "args": {"path": path}, "id": identity}
        for path, identity in (("README.md", "prefix-call-a"), ("AGENTS.md", "prefix-call-b"))
    ])
    results = [ToolMessage(content=body, id=f"prefix-result-{index}", tool_call_id=identity,
                           name="inspect_file")
               for index, (identity, body) in enumerate((("prefix-call-a", "README 结果"), ("prefix-call-b", "AGENTS 结果")))]
    messages = (call, *results)
    full = await case.dispatch("prefix-chain-full", messages=messages)
    case.manager.observe("prefix-chain:tail", "完整来源 B\n", revision="tail:B")
    delta = await case.dispatch("prefix-chain-delta", messages=messages)
    expected_delta = "--- 前缀来源@tail:A\n+++ 前缀来源@tail:B\n@@ -1 +1 @@\n-完整来源 A\n+完整来源 B\n"
    for previous, current in ((initial, full), (full, delta)):
        old = previous.seal_binding.prefix.state
        new = current.seal_binding.prefix.state
        assert new.epoch == old.epoch
        assert new.tool_compatibility_key == old.tool_compatibility_key
        assert stable_prefix_bytes(new.frames[:len(old.frames)]) == stable_prefix_bytes(old.frames)
        assert current.seal_binding.root == initial.seal_binding.root
    frames = delta.seal_binding.prefix.state.frames
    tail = [(frame, block) for frame in frames if frame.role == "user" and frame.source_protocol is not None
            for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
            if ref.source_identity == "prefix-chain:tail"]
    assert [(frame.role, block.body) for frame, block in tail] == [
        ("user", "完整来源 A\n"), ("user", expected_delta),
    ]
    full_tail_index = frames.index(tail[0][0])
    assert [frame.role for frame in frames[full_tail_index - 3:full_tail_index]] == [
        "assistant", "tool", "tool",
    ]
    assert frames[0].role == "system"
    assert any(frame.role == "user" and "HTTP canonical user" in str(frame.content) for frame in frames)
    delta_request = case.request(delta)
    before = _sealed_contract_rows(case)
    retry = await case.dispatch("prefix-chain-delta", messages=messages)
    assert retry == delta
    assert case.request(retry) == delta_request
    assert _sealed_contract_rows(case) == before
    assert case.dispatches[-1].reference.sealed_model_call_id == delta.model_call_id
    assert case.dispatches[-1].reference.callback_model_call_id != delta.model_call_id
    assert sorted(case.controller.transport.hit_counts.values()) == [1, 1, 1, 1]
    _select_contract_tools(case, "inspect_folder")
    rebuilt = await case.dispatch("prefix-chain-rebase", messages=messages)
    assert rebuilt.seal_binding.prefix.state.epoch.ordinal == initial.seal_binding.prefix.state.epoch.ordinal + 1
    assert rebuilt.seal_binding.prefix.state.epoch.reason == EpochReason.TOOLSET_CHANGED
    assert rebuilt.seal_binding.prefix.state.tool_compatibility_key != delta.seal_binding.prefix.state.tool_compatibility_key
    assert _sealed_contract_rows(case)[:-1] == before
    # 实际 Provider 正文必须维持两个独立 user 来源块，不能由 adapter 合并。
    body = case.request(delta)
    wire_items = body["input"] if case.target_format == "responses" else body["messages"]
    assert [entry for entry in wire_items if entry.get("role") == "user"][-2:] == [
        {"role": "user", "content": [{"type": "input_text", "text": text}]
         if case.target_format == "responses" else text}
        for text in (frame.content for frame, _block in tail)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
@pytest.mark.parametrize("dispatch_saver", [
    ResourceActivationPolicySnapshot(default_boundary="model_call", boundaries={}),
], indirect=True)
async def test_unpaired_tool_group_does_not_capture_model_call_resources(
    toolset_dispatch_case, preparation_session_service, target_format, monkeypatch,
):
    """未闭合协议组必须先失败，不能提前消费 Registry 或持久化下一次 snapshot。"""
    del target_format
    case = toolset_dispatch_case
    control_operations = []
    admit = case.bridge._context_control_admission

    def record_admission(operation, payload, ref):
        control_operations.append(operation)
        return admit(operation, payload, ref)

    monkeypatch.setattr(case.bridge, "_context_control_admission", record_admission)
    await case.dispatch("activation-protocol-initial")
    activation = case.bridge._resource_activation
    original_capture = activation.capture_model_call
    captures = []

    async def record_capture(**kwargs):
        captures.append(kwargs["model_call_id"])
        return await original_capture(**kwargs)

    monkeypatch.setattr(activation, "capture_model_call", record_capture)

    def frozen_ids():
        with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
            return tuple(snapshot.connection.execute(
                "SELECT activation_snapshot_id FROM resource_activation_snapshots "
                "WHERE snapshot_kind='model_call' ORDER BY rowid",
            ))

    before = frozen_ids()
    assert len(before) == 1, "首次真实请求必须冻结独立的 model_call snapshot"
    call = AIMessage(content="", id="activation-unpaired-call", tool_calls=[
        {"name": "inspect_file", "args": {"path": "README.md"}, "id": "activation-call"},
    ])
    with pytest.raises(ToolProtocolBoundaryConflict):
        await case.dispatch("activation-protocol-next", messages=(call,))
    rejected_operation = control_operations[-1]
    control_path = preparation_session_service.path_resolver.resolve_session_node(case.session) / "session-control.sqlite"
    with closing(SessionControlStore(control_path)) as control:
        assert control.find_lease_by_operation(f"context-control:{rejected_operation}").state == "completed"
    # 相同不完整输入也可以重入；不能被 failed lease 提前挡住。
    with pytest.raises(ToolProtocolBoundaryConflict):
        await case.dispatch("activation-protocol-next", messages=(call,))
    assert control_operations[-1] == rejected_operation
    assert captures == []
    assert frozen_ids() == before
    assert case.controller.transport.call_count == 1
    result = ToolMessage(content="完整结果", id="activation-paired-result", tool_call_id="activation-call",
                         name="inspect_file")
    await case.dispatch("activation-protocol-next", messages=(call, result))
    assert len(captures) == 1
    assert len(frozen_ids()) == len(before) + 1
    assert case.controller.transport.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
@pytest.mark.parametrize("dispatch_saver", [
    ResourceActivationPolicySnapshot(default_boundary="model_call", boundaries={}),
], indirect=True)
async def test_registry_wait_rechecks_protocol_before_committing_activation(
    toolset_dispatch_case, preparation_session_service, monkeypatch, target_format,
):
    """来源等待不占 gate；期间新 open group 必须先于 snapshot/source/seal 被拒绝。"""
    del target_format
    case = toolset_dispatch_case
    await case.dispatch("activation-race-initial")
    before_rows = _sealed_contract_rows(case)
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        before_ids = tuple(snapshot.connection.execute(
            "SELECT activation_snapshot_id FROM resource_activation_snapshots ORDER BY rowid",
        ))
    entered, released = asyncio.Event(), asyncio.Event()
    activation = case.bridge._resource_activation
    capture = activation.capture_model_call

    async def wait_for_registry(**kwargs):
        pending = await capture(**kwargs)
        entered.set()
        await released.wait()
        return pending

    monkeypatch.setattr(activation, "capture_model_call", wait_for_registry)
    calls = {"source": 0, "seal": 0}

    def count(name, actual):
        def counted(*args, **kwargs):
            calls[name] += 1
            return actual(*args, **kwargs)
        return counted

    monkeypatch.setattr(case.manager, "prepare_sources", count("source", case.manager.prepare_sources))
    from app.services.infrastructure.rollout_context.checkpoint.seal import preparation
    monkeypatch.setattr(preparation, "seal_registered_plan", count("seal", preparation.seal_registered_plan))
    running = asyncio.create_task(case.dispatch("activation-race-next"))
    call = AIMessage(content="", id="activation-race-call", tool_calls=[
        {"name": "inspect_file", "args": {"path": "README.md"}, "id": "activation-race-tool"},
    ])
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with preparation_session_service.session_write_admission(case.session):
            with dispatch_execution_scope(case.saver, case.session, case.turn):
                admission = current_execution_admission()
                assert admission is not None
                case.saver.ensure_request_items(
                    case.session, case.thread, turn_id=case.turn, execution_id=admission.execution_id,
                    messages=(call,),
                )
    finally:
        released.set()
        with pytest.raises(ToolProtocolBoundaryConflict):
            await asyncio.wait_for(running, 20)
    assert calls == {"source": 0, "seal": 0}
    assert case.controller.transport.call_count == 1
    assert _sealed_contract_rows(case) == before_rows
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        assert tuple(snapshot.connection.execute(
            "SELECT activation_snapshot_id FROM resource_activation_snapshots ORDER BY rowid",
        )) == before_ids
    result = ToolMessage(content="完整结果", id="activation-race-result", tool_call_id="activation-race-tool",
                         name="inspect_file")
    await case.dispatch("activation-race-next", messages=(call, result))
    assert calls == {"source": 1, "seal": 1}
    assert case.controller.transport.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_real_inflight_selection_change_keeps_sealed_retry_and_applies_next_call(
    native_toolset_dispatch_case, monkeypatch, target_format,
):
    """真实 HTTP in-flight 中改 desired；旧 retry 零新来源/零新 seal。"""
    del target_format
    case = native_toolset_dispatch_case
    case.state.release_response.clear()
    in_flight = asyncio.create_task(case.dispatch("toolset-inflight-first"))
    try:
        assert await asyncio.to_thread(case.state.request_ready.wait, 10)
        (first,) = case.saver.list_context_assemblies(case.session, case.thread)
        before = _sealed_contract_rows(case)
        desired = _select_contract_tools(case, "inspect_folder")
        case.manager.observe(
            "instruction:native-contract", "下一次 safe boundary 的说明",
            revision="native-contract:next-call",
        )
        assert _sealed_contract_rows(case) == before
        assert _latest_contract_assembly(case).seal_binding == first.seal_binding
        assert first.seal_binding.selection_revision != desired.revision
        assert _contract_wire_tool_names(case, case.state.requests[0]["body"]) == ("inspect_file",)
    finally:
        case.state.release_response.set()
    assert await asyncio.wait_for(in_flight, 20) == first

    def no_live_read_or_seal(*_args, **_kwargs):
        pytest.fail("sealed retry 不得读取新 desired/source 或再次 seal")

    with monkeypatch.context() as patch:
        patch.setattr(case.producer, "snapshot", no_live_read_or_seal)
        patch.setattr(case.manager, "prepare_sources", no_live_read_or_seal)
        patch.setattr(case.manager, "prepare_rebuild_sources", no_live_read_or_seal)
        patch.setattr(case.saver, "seal_context_plan", no_live_read_or_seal)
        retry = await case.dispatch("toolset-inflight-first")
    assert retry == first
    assert normalize_wire_request(case.state.requests[1]["body"]) == first.request_hash_preimage
    assert _sealed_contract_rows(case) == before

    changed = await case.dispatch("toolset-inflight-next")
    assert changed.seal_binding.selection_revision == desired.revision
    assert changed.seal_binding.applied_toolset_revision != first.seal_binding.applied_toolset_revision
    assert changed.seal_binding.tool_set_snapshot_id != first.seal_binding.tool_set_snapshot_id
    assert changed.seal_binding.prefix.state.epoch.ordinal == 2
    assert changed.seal_binding.previous_assembly_id == first.assembly_id
    assert _contract_wire_tool_names(case, case.state.requests[2]["body"]) == ("inspect_folder",)
    assert [(block.source_identity, block.body) for block in parse_source_blocks(changed.seal_binding.root.content)] == [("instruction:native-contract", "下一次 safe boundary 的说明")]
    assert _sealed_contract_rows(case)[0] == before[0]
    assert _latest_contract_assembly(case).seal_binding == changed.seal_binding


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_real_control_switches_coalesce_and_each_call_can_open_an_epoch_in_one_turn(
    toolset_dispatch_case, monkeypatch, target_format,
):
    """多个 desired revision 只在下一 safe call 读取最终快照，同 Turn 可多次 rebase。"""
    del target_format
    case = toolset_dispatch_case
    first = await case.dispatch("toolset-coalesce-initial")
    before = _sealed_contract_rows(case)
    intermediate = _select_contract_tools(case, "inspect_folder")
    restored = _select_contract_tools(case, "inspect_file")
    final = _select_contract_tools(case, "search_notes")
    assert len({intermediate.revision, restored.revision, final.revision}) == 3
    assert _sealed_contract_rows(case) == before
    produced = []
    original_snapshot = case.producer.snapshot

    def record_snapshot(*args, **kwargs):
        actual = original_snapshot(*args, **kwargs)
        produced.append(actual)
        return actual

    monkeypatch.setattr(case.producer, "snapshot", record_snapshot)
    second = await case.dispatch("toolset-coalesce-final")
    assert len(produced) == 1
    assert produced[0].selection_revision == final.revision
    assert produced[0].tool_names == ("search_notes",)
    assert second.seal_binding.applied_toolset_revision == produced[0].revision
    assert second.seal_binding.tool_set_snapshot_id == produced[0].snapshot_id
    assert _contract_wire_tool_names(case, case.request(second)) == ("search_notes",)

    third_selection = _select_contract_tools(case, "inspect_folder")
    third = await case.dispatch("toolset-coalesce-third")
    assert len(produced) == 2
    assert produced[1].selection_revision == third_selection.revision
    assert third.seal_binding.applied_toolset_revision == produced[1].revision
    assert first.turn_id == second.turn_id == third.turn_id == case.turn
    assert [entry.seal_binding.prefix.state.epoch.ordinal for entry in (first, second, third)] == [1, 2, 3]
    assert [entry.seal_binding.previous_assembly_id for entry in (second, third)] == [first.assembly_id, second.assembly_id]
    assert first.history_view_revision == second.history_view_revision == third.history_view_revision
    assert case.saver.list_context_assemblies(case.session, case.thread) == (first, second, third)
    assert _sealed_contract_rows(case)[0] == before[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
@pytest.mark.parametrize("terminal_status", ["success", "error"])
async def test_real_bridge_waits_for_paired_terminal_result_before_sources_or_seal(
    toolset_dispatch_case, monkeypatch, target_format, terminal_status,
):
    """真实 request codec 先提交 call；缺 terminal result 时零来源准备/零 seal/零 Provider 请求。"""
    del target_format
    case = toolset_dispatch_case
    first = await case.dispatch("toolset-protocol-initial")
    before = _sealed_contract_rows(case)
    _select_contract_tools(case, "inspect_folder")
    case.manager.observe(
        "instruction:native-contract", "工具组闭合后的说明", revision="native-contract:closed-group",
    )
    pending = case.manager.prepare_pending()
    calls = {"sources": 0, "rebuild": 0, "seal": 0}
    originals = {
        "sources": case.manager.prepare_sources,
        "rebuild": case.manager.prepare_rebuild_sources,
        "seal": preparation.seal_registered_plan,
    }

    def record_sources(*args, **kwargs):
        calls["sources"] += 1
        return originals["sources"](*args, **kwargs)

    def record_rebuild(*args, **kwargs):
        calls["rebuild"] += 1
        return originals["rebuild"](*args, **kwargs)

    def record_seal(*args, **kwargs):
        calls["seal"] += 1
        return originals["seal"](*args, **kwargs)

    monkeypatch.setattr(case.manager, "prepare_sources", record_sources)
    monkeypatch.setattr(case.manager, "prepare_rebuild_sources", record_rebuild)
    monkeypatch.setattr(preparation, "seal_registered_plan", record_seal)
    call = AIMessage(
        content="", id="toolset-outstanding-call",
        tool_calls=[{"name": "inspect_file", "args": {"path": "README.md"}, "id": "toolset-call"}],
    )
    with pytest.raises(ToolProtocolBoundaryConflict) as error:
        await case.dispatch("toolset-protocol-next", messages=(call,))
    assert error.value.code == "tool-protocol-boundary-conflict"
    assert calls == {"sources": 0, "rebuild": 0, "seal": 0}
    assert case.controller.transport.call_count == 1
    assert _sealed_contract_rows(case) == before
    assert _latest_contract_assembly(case).seal_binding == first.seal_binding
    # 只读 pending 容器，避免测试自身调用 prepare_sources 污染边界计数。
    assert tuple(case.manager._pending.values()) == pending.deltas
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        assert snapshot.connection.execute(
            "SELECT status, result_message_sequence FROM tool_calls WHERE tool_call_id = 'toolset-call'"
        ).fetchone() == ("pending", None)
        call_item_id = snapshot.connection.execute(
            "SELECT item_id FROM item_catalog WHERE semantic_kind = 'tool_call'"
        ).fetchone()[0]

    result = ToolMessage(
        content="真实工具结果" if terminal_status == "success" else "真实工具执行失败",
        id="toolset-terminal-result", tool_call_id="toolset-call",
        name="inspect_file", status=terminal_status,
    )
    changed = await case.dispatch("toolset-protocol-next", messages=(call, result))
    assert calls == {"sources": 1, "rebuild": 0, "seal": 1}
    assert case.controller.transport.call_count == 2
    assert changed.seal_binding.prefix.state.epoch.ordinal == 2
    assert changed.seal_binding.previous_assembly_id == first.assembly_id
    assert _contract_wire_tool_names(case, case.request(changed)) == ("inspect_folder",)
    assert _sealed_contract_rows(case)[0] == before[0]
    assert call_item_id in {entry.ref.ref_id for entry in changed.selection if entry.included}
    assert "item-toolset-terminal-result" in {entry.ref.ref_id for entry in changed.selection if entry.included}
    body = case.request(changed)
    if case.target_format == "responses":
        old_calls = [entry for entry in body["input"] if entry.get("type") == "function_call"]
        old_results = [entry for entry in body["input"] if entry.get("type") == "function_call_output"]
        assert old_calls[0]["call_id"] == old_results[0]["call_id"] == "toolset-call"
        assert old_calls[0]["name"] == "inspect_file"
        assert body["input"].index(old_calls[0]) < body["input"].index(old_results[0])
    else:
        old_calls = [call for entry in body["messages"] for call in entry.get("tool_calls", ())]
        old_results = [entry for entry in body["messages"] if entry.get("role") == "tool"]
        assert old_calls[0]["id"] == old_results[0]["tool_call_id"] == "toolset-call"
        assert old_calls[0]["function"]["name"] == "inspect_file"
    assert _latest_contract_assembly(case).seal_binding == changed.seal_binding


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_real_bridge_seal_transaction_failure_keeps_applied_and_retry_commits_once(
    toolset_dispatch_case, target_format,
):
    """真实 seal 已写 header 后在索引写入处失败；只成功提交能成为 applied。"""
    del target_format
    case = toolset_dispatch_case
    first = await case.dispatch("toolset-rollback-initial")
    before = _sealed_contract_rows(case)
    _select_contract_tools(case, "inspect_folder")
    case.manager.observe(
        "instruction:native-contract", "重试后才能 applied 的说明", revision="native-contract:retry-only",
    )
    pending = case.manager.prepare_pending()
    with case.saver._storage.storage._connect(case.thread, "", session_id=case.session) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_toolset_refs BEFORE INSERT ON assembly_item_refs "
            "BEGIN SELECT RAISE(ABORT, 'toolset-ref-seal-injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="toolset-ref-seal-injected"):
        await case.dispatch("toolset-rollback-next")
    assert case.controller.transport.call_count == 1
    assert _sealed_contract_rows(case) == before
    assert _latest_contract_assembly(case).seal_binding == first.seal_binding
    assert case.manager.prepare_pending() == pending
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        assert snapshot.connection.execute(
            "SELECT count(*) FROM context_assemblies WHERE status IN ('sealed','terminal')"
        ).fetchone()[0] == 1
        assert snapshot.connection.execute(
            "SELECT count(*) FROM assembly_item_refs WHERE assembly_id <> ?", (first.assembly_id,),
        ).fetchone()[0] == 0
        assert snapshot.connection.execute(
            "SELECT count(*) FROM storage_commits WHERE commit_kind = 'assembly_sealed' AND status = 'committed'"
        ).fetchone()[0] == 1
    with case.saver._storage.storage._connect(case.thread, "", session_id=case.session) as connection:
        connection.execute("DROP TRIGGER refuse_toolset_refs")
    changed = await case.dispatch("toolset-rollback-next")
    assert changed.seal_binding.prefix.state.epoch.ordinal == 2
    assert changed.seal_binding.previous_assembly_id == first.assembly_id
    assert changed.seal_binding.applied_toolset_revision != first.seal_binding.applied_toolset_revision
    assert [(block.source_identity, block.body) for block in parse_source_blocks(changed.seal_binding.root.content)] == [("instruction:native-contract", "重试后才能 applied 的说明")]
    assert _contract_wire_tool_names(case, case.request(changed)) == ("inspect_folder",)
    assert _sealed_contract_rows(case)[0] == before[0]
    assert _latest_contract_assembly(case).seal_binding == changed.seal_binding
    assert case.saver.list_context_assemblies(case.session, case.thread) == (first, changed)
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        assert snapshot.connection.execute(
            "SELECT count(*) FROM storage_commits WHERE commit_kind = 'assembly_sealed' AND status = 'committed'"
        ).fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
@pytest.mark.parametrize("manifest_change", ["policy_version", "wire_tool_order"])
async def test_complete_snapshot_supplier_policy_and_order_changes_bind_real_bridge_seals(
    toolset_dispatch_case, monkeypatch, target_format, manifest_change,
):
    """仅验证完整 supplier snapshot 的 seal 合同，不代表控制面可修改 policy/version/order。"""
    del target_format
    case = toolset_dispatch_case
    _select_contract_tools(case, "inspect_file", "inspect_folder", "search_notes")
    first = await case.dispatch("toolset-manifest-initial")
    before = _sealed_contract_rows(case)
    original_snapshot = case.producer.snapshot
    first_set = original_snapshot()
    injected = []

    def supply_complete_manifest(*args, **kwargs):
        # 保留正式 producer 的注册 schema 与真实 selection；仅在 supplier 值边界注入变体。
        desired = original_snapshot(*args, **kwargs)
        changed = ToolSetSnapshot(
            selection_revision=desired.selection_revision,
            tools=tuple(reversed(desired.tools)) if manifest_change == "wire_tool_order" else desired.tools,
            policy_keys=desired.policy_keys,
            policy_version="v2" if manifest_change == "policy_version" else desired.policy_version,
        )
        injected.append(changed)
        return changed

    monkeypatch.setattr(case.producer, "snapshot", supply_complete_manifest)
    changed = await case.dispatch("toolset-manifest-changed")
    assert len(injected) == 1
    changed_set = injected[0]
    assert first_set.selection_revision == changed_set.selection_revision
    assert first_set.tools == changed_set.tools
    assert first_set.policy_keys == changed_set.policy_keys
    assert first_set.revision != changed_set.revision
    assert changed.seal_binding.selection_revision == first.seal_binding.selection_revision
    assert changed.seal_binding.applied_toolset_revision == changed_set.revision
    assert changed.seal_binding.tool_set_snapshot_id == changed_set.snapshot_id
    assert changed.seal_binding.prefix.state.epoch.ordinal == 2
    assert changed.seal_binding.previous_assembly_id == first.assembly_id
    assert changed.tool_set_refs[0].tool_policy_version == changed_set.policy_version
    assert tuple(changed.tool_set_refs[0].tool_policy["wire_tool_order"]) == changed_set.wire_tool_order
    assert _contract_wire_tool_names(case, case.request(changed)) == changed_set.wire_tool_order
    assert _sealed_contract_rows(case)[0] == before[0]
    assert _latest_contract_assembly(case).seal_binding == changed.seal_binding


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
@pytest.mark.parametrize("canonical_status", ["partial", "failed"])
async def test_real_prepare_rejects_noncompleted_unknown_canonical_tool_result(
    toolset_dispatch_case, monkeypatch, target_format, canonical_status,
):
    """合法非 completed 结果经真实 canonical sink 提交，仍不能闭合 prepare 边界。"""
    del target_format
    case = toolset_dispatch_case
    first = await case.dispatch("toolset-noncompleted-unknown-initial")
    before = _sealed_contract_rows(case)
    _select_contract_tools(case, "inspect_folder")
    case.manager.observe(
        "instruction:native-contract", "工具终态闭合后才应用",
        revision="native-contract:noncompleted-unknown",
    )
    pending = tuple(case.manager._pending.values())
    model_call_id = f"terminal-scope-{canonical_status}"
    scoped_call_id = f"{model_call_id}:tool-call:unknown-result-call"
    with dispatch_execution_scope(case.saver, case.session, case.turn):
        admission = current_execution_admission()
        assert admission is not None
        accumulator = CanonicalBlockAccumulator(
            turn_id=case.turn, execution_id=admission.execution_id,
            producer_id="terminal-scope-probe", model_call_id=model_call_id,
        )
        accumulator.accept({
            "block_id": "terminal-scope-call", "carrier_type": "tool_call",
            "name": "inspect_file", "args": {"path": "README.md"},
            "tool_call_id": scoped_call_id,
        })
        (call,) = accumulator.finalize(first_item_sequence=1, status="completed")
        accumulator.accept({
            "block_id": "terminal-scope-result", "carrier_type": "tool_result",
            "name": "inspect_file", "tool_call_id": scoped_call_id,
            "tool_attempt_id": f"tool-attempt:{model_call_id}",
            "text": "工具结果尚未完整收敛，执行结果未知。",
        })
        (result,) = accumulator.finalize(
            first_item_sequence=1, status=canonical_status,
        )
        sink = CanonicalItemSink(
            case.saver, session_id=case.session, thread_id=case.thread,
            checkpoint_ns="",
        )
        await sink.append((call, result))
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        committed = case.saver._context_reader.read_canonical_items(
            snapshot, item_ids=(call.item_id, result.item_id),
        )
    committed_call, committed_result = committed
    assert committed_call.status == "completed"
    assert committed_result.status == canonical_status
    assert committed_result.payload["tool_outcome"] == "unknown"
    assert committed_result.payload["execution_confirmed"] is False
    assert committed_result.payload == result.payload
    assert _sealed_contract_rows(case) == before
    assert _latest_contract_assembly(case).seal_binding == first.seal_binding
    projected_result = LangChainMessageCodec().project_message((committed_result,))
    assert projected_result["type"] == "tool"
    assert projected_result["data"]["status"] == "error"
    projected_knowledge = projected_result["data"]["response_metadata"]
    assert projected_knowledge["canonical_item_status"] == canonical_status
    assert projected_knowledge["tool_outcome"] == "unknown"
    assert projected_knowledge["execution_confirmed"] is False
    (case.artifacts / f"noncompleted-unknown-{canonical_status}-{case.target_format}.json").write_text(
        json.dumps({
            "canonical_status": committed_result.status,
            "tool_outcome": committed_result.payload["tool_outcome"],
            "execution_confirmed": committed_result.payload["execution_confirmed"],
            "checkpoint_tool_message_status": projected_result["data"]["status"],
            "expected_prepare_boundary": "tool-protocol-boundary-conflict",
            "previous_assembly_id": first.assembly_id,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    counts = {"sources": 0, "rebuild": 0, "seal": 0}

    def count_call(name, method):
        def counted(*args, **kwargs):
            counts[name] += 1
            return method(*args, **kwargs)
        return counted

    monkeypatch.setattr(
        case.manager, "prepare_sources", count_call("sources", case.manager.prepare_sources),
    )
    monkeypatch.setattr(
        case.manager, "prepare_rebuild_sources",
        count_call("rebuild", case.manager.prepare_rebuild_sources),
    )
    monkeypatch.setattr(
        preparation, "seal_registered_plan",
        count_call("seal", preparation.seal_registered_plan),
    )
    with pytest.raises(ToolProtocolBoundaryConflict) as error:
        await case.dispatch("toolset-noncompleted-unknown-next")
    assert error.value.code == "tool-protocol-boundary-conflict"
    assert counts == {"sources": 0, "rebuild": 0, "seal": 0}
    assert case.controller.transport.call_count == 1
    assert case.saver.list_context_assemblies(case.session, case.thread) == (first,)
    assert _sealed_contract_rows(case) == before
    assert _latest_contract_assembly(case).seal_binding == first.seal_binding
    assert _latest_contract_assembly(case).seal_binding.prefix == first.seal_binding.prefix
    assert tuple(case.manager._pending.values()) == pending
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        assert case.saver._context_reader.read_canonical_items(
            snapshot, item_ids=(committed_result.item_id,),
        ) == [committed_result]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_real_prepare_observes_completed_unknown_canonical_terminal_notification(
    toolset_dispatch_case, monkeypatch, target_format,
):
    """completed 终态通知保持 unknown/false；真实 codec/Bridge 投影都不得伪成功。"""
    del target_format
    case = toolset_dispatch_case
    first = await case.dispatch("toolset-completed-unknown-initial")
    before = _sealed_contract_rows(case)
    desired = _select_contract_tools(case, "inspect_folder")
    model_call_id = "terminal-scope-completed-unknown"
    provider_call_id = "unknown-terminal-call"
    scoped_call_id = f"{model_call_id}:tool-call:{provider_call_id}"
    content = "最终通知：工具执行结果未知，未确认执行。"
    with dispatch_execution_scope(case.saver, case.session, case.turn):
        admission = current_execution_admission()
        assert admission is not None
        accumulator = CanonicalBlockAccumulator(
            turn_id=case.turn, execution_id=admission.execution_id,
            producer_id="terminal-scope-probe", model_call_id=model_call_id,
        )
        accumulator.accept({
            "block_id": "terminal-scope-call", "carrier_type": "tool_call",
            "name": "inspect_file", "args": {"path": "README.md"},
            "tool_call_id": scoped_call_id,
        })
        (call,) = accumulator.finalize(first_item_sequence=1, status="completed")
        accumulator.accept({
            "block_id": "terminal-scope-result", "carrier_type": "tool_result",
            "name": "inspect_file", "tool_call_id": scoped_call_id,
            "tool_attempt_id": f"tool-attempt:{model_call_id}", "text": content,
        })
        (unknown_draft,) = accumulator.finalize(first_item_sequence=1, status="partial")
        # accumulator 的 completed 分支固定 success；合法 completed+unknown
        # 由正式领域工厂分别表达终态记录与未确认执行知识，再经同一真实 sink 提交。
        result = CanonicalItemRecord.create(
            item_sequence=unknown_draft.item_sequence, item_id=unknown_draft.item_id,
            semantic_kind=unknown_draft.semantic_kind, payload_kind=unknown_draft.payload_kind,
            status="completed", producer_ref=unknown_draft.producer_ref,
            payload=unknown_draft.payload, created_at=unknown_draft.created_at,
            projection_identity=unknown_draft.projection_identity,
            turn_id=unknown_draft.turn_id, turn_scope=unknown_draft.turn_scope,
            message_group_id=unknown_draft.message_group_id, wire_role=unknown_draft.wire_role,
        )
        sink = CanonicalItemSink(
            case.saver, session_id=case.session, thread_id=case.thread,
            checkpoint_ns="",
        )
        await sink.append((call, result))
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        committed_call, committed_result = case.saver._context_reader.read_canonical_items(
            snapshot, item_ids=(call.item_id, result.item_id),
        )
    assert committed_call.status == committed_result.status == "completed"
    assert committed_result.payload["tool_outcome"] == "unknown"
    assert committed_result.payload["execution_confirmed"] is False
    assert committed_result.payload == result.payload
    assert _sealed_contract_rows(case) == before
    assert _latest_contract_assembly(case).seal_binding == first.seal_binding
    counts = {"sources": 0, "rebuild": 0, "seal": 0}

    def count_call(name, method):
        def counted(*args, **kwargs):
            counts[name] += 1
            return method(*args, **kwargs)
        return counted

    monkeypatch.setattr(
        case.manager, "prepare_sources", count_call("sources", case.manager.prepare_sources),
    )
    monkeypatch.setattr(
        case.manager, "prepare_rebuild_sources",
        count_call("rebuild", case.manager.prepare_rebuild_sources),
    )
    monkeypatch.setattr(
        preparation, "seal_registered_plan",
        count_call("seal", preparation.seal_registered_plan),
    )
    projected_tool_results = []
    projected_message_lists = []
    original_projection = case.bridge._project_prepared

    def record_projection(request, prepared):
        actual = original_projection(request, prepared)
        projected_message_lists.append(list(actual[1]))
        projected_tool_results.extend(
            message for message in actual[1] if isinstance(message, ToolMessage)
        )
        return actual

    monkeypatch.setattr(case.bridge, "_project_prepared", record_projection)
    changed = await case.dispatch("toolset-completed-unknown-next")
    projected_tool_result, = projected_tool_results
    projected_messages, = projected_message_lists
    assert projected_tool_result.status == "error"
    assert projected_tool_result.tool_call_id == provider_call_id
    assert projected_tool_result.response_metadata["canonical_item_status"] == "completed"
    assert projected_tool_result.response_metadata["tool_outcome"] == "unknown"
    assert projected_tool_result.response_metadata["execution_confirmed"] is False
    # 真实 Bridge 返回值经唯一 codec 回写，终态与执行知识仍分别保留。
    codec = LangChainMessageCodec()
    assert committed_result.projection_identity is not None
    result_message_index = projected_messages.index(projected_tool_result)
    inferred_model_call_id = codec.tool_message_model_call_id(
        projected_tool_result, projected_messages[:result_message_index],
    )
    assert inferred_model_call_id == model_call_id
    recoded_result, = codec.items_for_message(
        projected_tool_result,
        item_sequence=committed_result.item_sequence,
        message_id=codec.message_id(projected_tool_result, 0),
        turn_id=committed_result.turn_id,
        execution_id=admission.execution_id,
        timestamp=committed_result.created_at,
        model_call_id=inferred_model_call_id,
    )
    assert recoded_result.status == "completed"
    assert recoded_result.payload["tool_outcome"] == "unknown"
    assert recoded_result.payload["execution_confirmed"] is False
    assert recoded_result.payload["content"] == committed_result.payload["content"] == content
    assert recoded_result.projection_identity is not None
    assert recoded_result.projection_identity.model_call_id == model_call_id
    assert counts == {"sources": 1, "rebuild": 0, "seal": 1}
    assert case.controller.transport.call_count == 2
    assert changed.seal_binding.selection_revision == desired.revision
    assert changed.seal_binding.prefix.state.epoch.ordinal == 2
    assert changed.seal_binding.previous_assembly_id == first.assembly_id
    assert _contract_wire_tool_names(case, case.request(changed)) == ("inspect_folder",)
    assert {committed_call.item_id, committed_result.item_id} <= {
        entry.ref.ref_id for entry in changed.selection if entry.included
    }
    assert _sealed_contract_rows(case)[0] == before[0]
    assert _latest_contract_assembly(case).seal_binding == changed.seal_binding
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        assert case.saver._context_reader.read_canonical_items(
            snapshot, item_ids=(committed_result.item_id,),
        ) == [committed_result]
    body = case.request(changed)
    if case.target_format == "responses":
        wire_call, = (entry for entry in body["input"] if entry.get("type") == "function_call")
        wire_result, = (entry for entry in body["input"] if entry.get("type") == "function_call_output")
        assert wire_call["call_id"] == wire_result["call_id"] == provider_call_id
        assert wire_call["name"] == "inspect_file"
        assert wire_result["output"] == content
        assert body["input"].index(wire_call) < body["input"].index(wire_result)
    else:
        wire_call, = (
            call for message in body["messages"] for call in message.get("tool_calls", ())
        )
        wire_result, = (message for message in body["messages"] if message.get("role") == "tool")
        assert wire_call["id"] == wire_result["tool_call_id"] == provider_call_id
        assert wire_call["function"]["name"] == "inspect_file"
        assert wire_result["content"] == content
    projected_result = LangChainMessageCodec().project_message((committed_result,))
    assert projected_result["type"] == "tool"
    assert projected_result["data"]["status"] == "error"
    projected_knowledge = projected_result["data"]["response_metadata"]
    assert projected_knowledge["canonical_item_status"] == "completed"
    assert projected_knowledge["tool_outcome"] == "unknown"
    assert projected_knowledge["execution_confirmed"] is False
    # canonical 与真实 codec/Bridge 传输知识分别落证，完整终态仍可闭合。
    (case.artifacts / f"completed-unknown-terminal-{case.target_format}.json").write_text(
        json.dumps({
            "canonical_status": committed_result.status,
            "tool_outcome": committed_result.payload["tool_outcome"],
            "execution_confirmed": committed_result.payload["execution_confirmed"],
            "checkpoint_tool_message_status": projected_result["data"]["status"],
            "provider_wire_result": wire_result,
            "provider_tool_message_status": projected_tool_result.status,
            "provider_tool_outcome": projected_tool_result.response_metadata["tool_outcome"],
            "provider_execution_confirmed": projected_tool_result.response_metadata["execution_confirmed"],
            "checkpoint_inferred_model_call_id": inferred_model_call_id,
            "checkpoint_inverse_status": recoded_result.status,
            "checkpoint_inverse_tool_outcome": recoded_result.payload["tool_outcome"],
            "checkpoint_inverse_execution_confirmed": recoded_result.payload["execution_confirmed"],
            "source_prepare_calls": counts,
            "previous_assembly_id": first.assembly_id,
            "assembly_id": changed.assembly_id,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
