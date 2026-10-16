from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import litellm
import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.providers.wire_finalize import (
    chat_wire_params,
    chat_wire_profile,
    responses_wire_params,
    responses_wire_profile,
)
from app.agents.sealed_assembly_dispatch import sealed_assembly_dispatch_scope
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.runtime.model_stream import ModelStreamTransportController
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
from app.testing.model_stream import (
    ModelCallIdentity,
    ModelStreamHTTPTransport,
    StreamScenario,
    build_cassette,
    data_frame,
    done_frame,
    load_cassette_from_object,
    load_model_stream_config,
    load_scenario,
    replay_model_call,
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream.jsonc"
CHAT_BASIC_CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream_chat_basic.jsonc"
RESPONSES_CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream_responses.jsonc"
RESPONSES_PARALLEL_TOOL_CONFIG_PATH = (
    Path.cwd()
    / "configs"
    / "tests"
    / "model_stream"
    / "model_stream_responses_parallel_tool.jsonc"
)
FIXTURE_ROOT = Path.cwd() / "tests" / "fixtures" / "model_stream"


class SealedTransportOwner:
    """每个协议调用使用真实 ingress、canonical checkpoint 与 Saver 封存。"""

    def __init__(self, model, saver: RolloutCheckpointSaver, owner: MutationIntentOwner,
                 activation: Callable[..., dict[str, object]]) -> None:
        self.model = model
        self.saver = saver
        self.owner = owner
        self.activation = activation
        self.accepted: dict[str, object] | None = None
        self.root_content: object = None
        self.root_message_id = uuid4().hex
        self.call_number = 0
        self.manager = ContextSourceManager(
            owner=ContextSourceOwnerKey(owner.session_id, owner.thread_id),
            control_state_port=saver,
        )
        self.manager.register(ContextSourceDescriptor(
            source_id="instruction:transport", source_kind="agent_instructions",
            name="协议测试说明", description="真实协议测试的系统说明",
            internal_locator="instruction-producer://transport", root_placement="root_eligible",
        ), tracking_status="tracked")

    @contextmanager
    def call_scope(
        self, messages: str | list[BaseMessage], *, identity: ModelCallIdentity | None = None,
    ) -> Iterator[list[BaseMessage]]:
        original = [HumanMessage(content=messages)] if isinstance(messages, str) else deepcopy(messages)
        users = [message for message in original if isinstance(message, HumanMessage)]
        if len(users) != 1:
            raise ValueError("transport fixture 必须持有一个明确的真实 ingress")
        self.call_number += 1
        if self.accepted is None or self.root_content != users[0].content:
            self.root_message_id = uuid4().hex
            self.root_content = deepcopy(users[0].content)
            self.accepted = self.saver.accept_turn(
                self.owner.session_id, thread_id=self.owner.thread_id,
                accepted_ingress_id=self.root_message_id, payload=users[0].content,
                root_item_id="item-" + self.root_message_id,
                acceptance_idempotency_key=f"transport:accept:{self.call_number}",
                job_id=f"transport-job-{self.call_number}",
                admission_idempotency_key=f"transport:admission:{self.call_number}",
            )
        users[0].id = self.root_message_id
        users[0].response_metadata["turn_id"] = str(self.accepted["turn_id"])
        canonical = [message for message in original if not isinstance(message, SystemMessage)]
        call_id = identity.model_call_id if identity is not None else str(UUID(create_uuid_hex()))
        if identity is not None and (identity.session_id, identity.thread_id) != (
            self.owner.session_id, self.owner.thread_id,
        ):
            raise ValueError("transport replay identity 与真实 SessionThread owner 不一致")
        instruction = [message.content for message in original if isinstance(message, SystemMessage)]
        if any(not isinstance(text, str) for text in instruction):
            raise TypeError("transport fixture 的系统说明必须是文本")
        self.manager.observe("instruction:transport", "\n".join(instruction), revision="transport:instruction")
        admission = self.saver.execution_admission(
            self.owner.session_id, thread_id=self.owner.thread_id,
            execution_id=str(self.accepted["initial_execution_id"]),
        )
        with (session_thread_owner_scope(self.owner, langgraph_thread_id=self.owner.thread_id),
              execution_admission_scope(admission, langgraph_thread_id=self.owner.thread_id)):
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"messages": canonical}
            checkpoint["channel_versions"] = {"messages": str(self.call_number)}
            self.saver.put(
                build_checkpoint_config(self.owner.thread_id), checkpoint,
                {"source": "loop", "step": self.call_number, "parents": {}},
                {"messages": str(self.call_number)},
            )
            responses = isinstance(self.model, BoxteamOpenAIResponsesModel)
            profile = responses_wire_profile(self.model) if responses else chat_wire_profile(self.model)
            params = responses_wire_params(self.model, kwargs={}) if responses else chat_wire_params(
                self.model, stop=None, kwargs={}, stream=True,
            )
            creation_key = f"transport:call:{self.call_number}"
            prepared = self.saver.prepare_context_for_provider(
                self.owner.session_id, self.owner.thread_id,
                turn_id=str(self.accepted["turn_id"]), execution_id=admission.execution_id,
                plan_creation_idempotency_key=creation_key,
                seal_idempotency_key=creation_key + ":seal", request_messages=canonical,
                context_source_manager=self.manager,
                tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
                provider_version=profile.identity(), wire_profile=profile, wire_params=params,
                target_format="responses" if responses else "chat_completions",
                **self.activation(self.saver, plan_creation_idempotency_key=creation_key, model_call_id=call_id),
            )
            snapshot = prepared["snapshot"]
            assert self.saver.get_context_assembly(
                self.owner.session_id, self.owner.thread_id, assembly_id=snapshot.assembly_id,
            ) == snapshot
            native = self.saver.project_context_plan_to_native(
                self.owner.session_id, self.owner.thread_id, prepared["plan"],
            ) if responses else None
            with sealed_assembly_dispatch_scope(
                snapshot, callback_model_call_id=call_id, native_projection=native,
            ):
                yield prepared["messages"]

    async def astream(
        self, messages: str | list[BaseMessage], *, identity: ModelCallIdentity | None = None,
    ) -> AsyncIterator[AIMessageChunk]:
        with self.call_scope(messages, identity=identity) as projected:
            async for chunk in self.model.astream(projected):
                yield chunk

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        with self.call_scope(messages) as projected:
            return await self.model.ainvoke(projected)


@pytest.fixture
def transport_dispatch(tmp_path: Path, frozen_provider_activation):
    """多个并发 owner 共用 fixture catalog；每个 owner 独立 admission 与封存。"""
    def create(model, *, identity: ModelCallIdentity | None = None) -> SealedTransportOwner:
        session_id = identity.session_id if identity is not None else "ses_" + create_uuid_hex()
        thread_id = identity.thread_id if identity is not None else "thr_" + create_uuid_hex()
        sessions = tmp_path / ".boxteam/sessions"
        seed_catalog_session_bundle(sessions, session_id, main_thread_id=thread_id)
        saver = RolloutCheckpointSaver(sessions)
        return SealedTransportOwner(model, saver, MutationIntentOwner(session_id, thread_id), frozen_provider_activation)

    return create


def _model() -> BoxteamLiteLLMChatModel:
    return BoxteamLiteLLMChatModel(
        model="openai/big-pickle",
        api_key="test-key",
        api_base="https://opencode.ai/zen/v1",
        custom_llm_provider="openai",
        streaming=True,
    )


def _chat_tool_model() -> BoxteamLiteLLMChatModel:
    return BoxteamLiteLLMChatModel(
        model="openai/big-pickle",
        api_key="test-key",
        api_base="https://opencode.ai/zen/v1",
        custom_llm_provider="openai",
        provider_id="primary",
        streaming=True,
    )


def _sequence_model() -> BoxteamLiteLLMChatModel:
    return BoxteamLiteLLMChatModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://provider.example/v1",
        custom_llm_provider="openai",
        streaming=True,
    )


async def _visible_text(model: SealedTransportOwner, prompt: str, *, identity: ModelCallIdentity | None = None) -> str:
    chunks = []
    async for chunk in model.astream(prompt, identity=identity):
        content = chunk.content
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
    return "".join(chunks)


async def _responses_reasoning_and_tool(
    model: SealedTransportOwner,
) -> tuple[str, str]:
    reasoning: list[str] = []
    tool_names: list[str] = []
    async for chunk in model.astream(
        [
            SystemMessage(content="测试系统消息"),
            HumanMessage(content="请读取 README.md"),
        ]
    ):
        content = chunk.content
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "reasoning":
                    nested = block.get("content")
                    if isinstance(nested, list):
                        reasoning.extend(
                            item["text"]
                            for item in nested
                            if isinstance(item, dict)
                            and item.get("type") in {"reasoning_text", "text"}
                            and isinstance(item.get("text"), str)
                        )
                    elif isinstance(block.get("reasoning"), str):
                        reasoning.append(block["reasoning"])
        for tool_call in chunk.tool_call_chunks:
            name = tool_call.get("name")
            if isinstance(name, str) and name not in tool_names:
                tool_names.append(name)
    return "".join(reasoning), "".join(tool_names)


async def _responses_parallel_tool_calls(
    model: SealedTransportOwner,
) -> dict[str, tuple[str, str]]:
    tool_calls: dict[str, dict[str, str]] = {}
    async for chunk in model.astream(
        [
            SystemMessage(content="测试系统消息"),
            HumanMessage(content="请并行读取 README.md 和 pyproject.toml"),
        ]
    ):
        for raw_tool_call in chunk.tool_call_chunks:
            call_id = raw_tool_call.get("id")
            index = raw_tool_call.get("index")
            if not isinstance(call_id, str) or not call_id:
                raise RuntimeError(
                    f"Responses 并发工具 chunk 缺少 call_id: chunk={raw_tool_call!r}"
                )
            if not isinstance(index, int):
                raise TypeError(
                    f"Responses 并发工具 chunk 缺少 index: chunk={raw_tool_call!r}"
                )
            current = tool_calls.setdefault(call_id, {"name": "", "args": ""})
            name = raw_tool_call.get("name")
            if isinstance(name, str):
                current["name"] = name
            args = raw_tool_call.get("args")
            if isinstance(args, str):
                current["args"] += args
    return {
        call_id: (tool_call["name"], tool_call["args"])
        for call_id, tool_call in tool_calls.items()
    }


def _responses_model() -> BoxteamOpenAIResponsesModel:
    return BoxteamOpenAIResponsesModel(
        model="gpt-5.6-luna",
        api_key="test-key",
        api_base="https://www.cctq.ai/v1",
        custom_llm_provider="openai",
        provider_id="backup_3",
        responses_store=False,
        streaming=True,
    )


def _call_identity(owner: int, call: int) -> ModelCallIdentity:
    return ModelCallIdentity(
        f"ses_000000004000700080000000000000{owner:02x}",
        f"thr_000000004000700080000000000001{owner:02x}",
        f"00000000-4000-7000-8000-000000000{owner:01x}{call:02x}",
    )


def _sequence_scenario() -> StreamScenario:
    interactions: list[object] = []
    for step, text in enumerate(("第一轮", "第二轮")):
        cassette = build_cassette(
            asset_id=f"sequence-{step}",
            url="https://provider.example/v1/chat/completions",
            model="test-model",
            frames=(
                data_frame(
                    {
                        "id": f"sequence-{step}",
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": text},
                                "finish_reason": None,
                            }
                        ],
                    }
                ),
                data_frame(
                    {
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"}
                        ]
                    }
                ),
                done_frame(),
            ),
        )
        interaction = dict(cassette.raw["interactions"][0])
        interaction["identity"] = _call_identity(1, step).to_dict()
        interactions.append(interaction)
    second_owner = [dict(entry, identity=_call_identity(2, call).to_dict())
                    for call, entry in enumerate(interactions)]
    interactions.extend(second_owner)
    raw = {
        "schema_version": 1,
        "kind": "model_stream_cassette",
        "metadata": {
            "source": "handwritten",
            "asset_id": "sequence-test",
            "protocol": "openai_chat_sse",
        },
        "interactions": interactions,
    }
    return StreamScenario(
        scenario_id="sequence-test",
        asset_path=Path("<memory>"),
        cassette=load_cassette_from_object(raw),
        business_assertion="sequence-text",
        raw=raw,
    )


@pytest.mark.asyncio
async def test_litellm_replay_preserves_provider_url_and_parser_chain(transport_dispatch) -> None:
    config = load_model_stream_config(CHAT_BASIC_CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        text = await _visible_text(transport_dispatch(_model()), "测试 LiteLLM replay")

        assert text == "基础回放"
        assert controller.transport.request_urls == (
            "https://opencode.ai/zen/v1/chat/completions",
        )
        assert controller.transport.hit_counts == {0: 1}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_litellm_replay_is_safe_for_high_concurrency(transport_dispatch) -> None:
    config = load_model_stream_config(CHAT_BASIC_CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        results = await asyncio.gather(
            *(_visible_text(transport_dispatch(_model()), f"并发请求 {index}") for index in range(32))
        )

        assert results == ["基础回放"] * 32
        assert controller.transport.call_count == 32
        assert controller.transport.hit_counts == {0: 32}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_litellm_chat_replay_parses_reasoning_and_tool_loop(
    tmp_path: Path,
    transport_dispatch,
) -> None:
    config = load_model_stream_config(CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("Chat tool model stream replay controller 未安装")
    try:
        initial_messages = [
            SystemMessage(content="测试系统消息"),
            HumanMessage(content="请读取 README.md"),
        ]
        model = transport_dispatch(_chat_tool_model())
        first = await model.ainvoke(initial_messages)
        assert first.tool_calls[0]["name"] == "read_file"
        assert first.tool_calls[0]["args"] == {"path": "README.md"}
        assert first.content[0]["type"] == "reasoning_content"
        assert first.content[0]["reasoning_content"] == (
            "Chat Completions 先确认要读取的文件，再检查工具返回的证据，"
            "确认内容和当前问题一致后，再发起一次明确的 Chat Completions 工具调用。"
        )

        second = await model.ainvoke(
            [
                *initial_messages,
                first,
                ToolMessage(
                    content="README 测试结果",
                    tool_call_id=first.tool_calls[0]["id"],
                ),
            ]
        )
        assert second.content[0]["type"] == "reasoning_content"
        assert second.content[0]["reasoning_content"] == (
            "Chat Completions 工具已经返回，我先核对文件内容，"
            "确认结果和请求目标相符，再整理一份 Chat Completions 可复查的最终答复。"
        )
        assert second.content[1]["text"] == (
            "Chat Completions 会说明检查结果，再给出 Chat Completions 可复查结论。"
        )
        assert controller.transport.hit_counts == {0: 1, 1: 1}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_litellm_responses_replay_uses_real_aresponses_parser(transport_dispatch) -> None:
    config = load_model_stream_config(RESPONSES_CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("Responses model stream replay controller 未安装")
    try:
        results = await asyncio.gather(
            *(
                _responses_reasoning_and_tool(transport_dispatch(_responses_model()))
                for _ in range(12)
            )
        )

        assert results == [
            (
                (
                    "OpenAI Responses 先确认待读取的文件，再检查工具返回的证据，"
                    "确认内容和当前问题一致后，再发起一次明确的 OpenAI Responses 工具调用。"
                ),
                "read_file",
            )
        ] * 12
        assert controller.transport.request_urls == (
            "https://www.cctq.ai/v1/responses",
        ) * 12
        assert controller.transport.hit_counts == {0: 12}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_litellm_responses_replay_preserves_interleaved_parallel_tools(transport_dispatch) -> None:
    config = load_model_stream_config(RESPONSES_PARALLEL_TOOL_CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("Responses parallel tool replay controller 未安装")
    try:
        results = await asyncio.gather(
            *(
                _responses_parallel_tool_calls(transport_dispatch(_responses_model()))
                for _ in range(8)
            )
        )

        expected = {
            "call_parallel_one": ("read_file", '{"path":"README.md"}'),
                "call_parallel_two": ("read_file", '{"path":"test.md"}'),
        }
        assert results == [expected] * 8
        assert controller.transport.request_urls == (
            "https://www.cctq.ai/v1/responses",
        ) * 8
        assert controller.transport.hit_counts == {0: 8}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_different_cassettes_match_concurrently(tmp_path: Path) -> None:
    transports = [
        ModelStreamHTTPTransport(
            scenario=load_scenario(FIXTURE_ROOT, scenario_id),
            mode="replay",
            replay_policy="request_reusable",
            artifact_root=tmp_path / scenario_id,
        )
        for scenario_id in ("basic-text", "reasoning-stream")
    ]
    clients = [httpx.AsyncClient(transport=transport) for transport in transports]
    try:
        responses = await asyncio.gather(
            *(
                client.post(
                    "https://opencode.ai/zen/v1/chat/completions",
                    json={
                        "model": "big-pickle",
                        "stream": True,
                        "messages": [{"role": "user", "content": "测试"}],
                    },
                )
                for client in clients
            )
        )
        bodies = await asyncio.gather(*(response.aread() for response in responses))
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))

    assert "基础".encode() in bodies[0]
    assert "结论".encode() in bodies[1]
    assert [transport.hit_counts for transport in transports] == [{0: 1}, {0: 1}]


@pytest.mark.asyncio
async def test_transport_controller_restores_litellm_async_session() -> None:
    import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler

    previous_factory = litellm_http_handler.get_async_httpx_client
    previous_session = litellm.aclient_session
    if previous_session is not None:
        raise RuntimeError("测试开始前 LiteLLM async session 已存在")
    config = load_model_stream_config(CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")

    assert controller.client is None
    assert litellm.aclient_session is previous_session
    controller.ensure_installed()
    installed_client = controller.client
    assert installed_client is not None
    assert litellm.aclient_session is installed_client
    controller.ensure_installed()
    assert controller.client is installed_client
    await controller.aclose()
    await controller.aclose()
    assert installed_client.is_closed
    assert litellm.aclient_session is previous_session
    assert litellm_http_handler.get_async_httpx_client is previous_factory


@pytest.mark.asyncio
async def test_litellm_exact_call_identity_isolated_for_concurrent_tool_loops(
    tmp_path: Path,
    transport_dispatch,
) -> None:
    previous_session = litellm.aclient_session
    if previous_session is not None:
        raise RuntimeError("测试开始前 LiteLLM async session 已存在")
    transport = ModelStreamHTTPTransport(
        scenario=_sequence_scenario(),
        mode="replay",
        replay_policy="call_identity",
        artifact_root=tmp_path,
    )
    client = httpx.AsyncClient(transport=transport)
    litellm.aclient_session = client

    async def run_session(owner: int) -> tuple[str, str]:
        model = transport_dispatch(_sequence_model(), identity=_call_identity(owner, 0))
        with replay_model_call(_call_identity(owner, 0)):
            first = await _visible_text(model, "执行第一轮", identity=_call_identity(owner, 0))
        with replay_model_call(_call_identity(owner, 1)):
            second = await _visible_text(model, "执行第二轮", identity=_call_identity(owner, 1))
        return first, second

    try:
        results = await asyncio.gather(
            run_session(1),
            run_session(2),
        )
    finally:
        litellm.aclient_session = previous_session
        await client.aclose()

    assert results == [("第一轮", "第二轮"), ("第一轮", "第二轮")]
    assert transport.hit_counts == {0: 1, 1: 1, 2: 1, 3: 1}
