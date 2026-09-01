"""用手写多 Provider SSE cassette 重建 rollout 历史 fixture。"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import httpx
import litellm
import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langchain_core.runnables import RunnableConfig
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

PROJECT_ROOT = Path.cwd().resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.providers.anthropic_messages import BoxteamAnthropicMessagesModel
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.core.checkpoint_config import build_checkpoint_config
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.core.session_paths import SessionPathResolver
from app.testing.model_stream import (
    ModelStreamHTTPTransport,
    StreamScenario,
    load_cassette_from_object,
    load_scenario,
)

TARGET_WORKSPACE = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "workspaces"
    / "custom_tool_test_workspace"
)
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "model_stream"
BASE_TIME = datetime(2026, 8, 27, tzinfo=UTC)
LARGE_PAYLOAD_BYTES = 64 * 1024

STATIC_SESSION_ID = "ses_a1b2c3d4e5f6478899aabbccddeeff00"
REAL_SESSION_ID = "ses_8128d7f0a4b64aa0b3f1c9e7d2a65018"
LARGE_SESSION_ID = "ses_9f4e2c7a1b6d4830a5e8f2c1d7b90436"
COMPACTION_SESSION_ID = "ses_4c0a1d6e7f8b49a2b5c6d7e8f9012345"
TOOL_SESSION_ID = "ses_7e5d3c1b9a8f4762b4d6e8f0a1c23579"
FORK_SESSION_ID = "ses_6b2d4f8a0c1e4937b5d9f1a3c7e24680"

TOPICS = (
    "会话历史加载",
    "自定义工具说明",
    "SQLite 索引",
    "浏览器资源管理",
    "TypeScript 状态更新",
    "Gateway 路由",
    "测试隔离工作区",
    "上下文压缩",
)


@dataclass(frozen=True, slots=True)
class HandwrittenProvider:
    """一个真实 ModelCall 的 provider 配置和对应模型实例。"""

    provider_id: str
    model_provider: str
    api_mode: str
    model: BaseChatModel
    tool_model: BaseChatModel
    large_model: BaseChatModel

    def model_for_call(self, *, use_tool: bool, large_tools: bool) -> BaseChatModel:
        if large_tools:
            return self.large_model
        return self.tool_model if use_tool else self.model


def _stamp(turn: int) -> str:
    return (BASE_TIME + timedelta(minutes=turn)).isoformat()


def _large_payload(marker: str, size: int = LARGE_PAYLOAD_BYTES) -> str:
    header = f"{marker}_BEGIN\n"
    footer = f"\n{marker}_END"
    line = f"{marker}|record=000000|status=ok|source=handwritten_sse\n"
    body_size = size - len(header) - len(footer)
    if body_size <= 0:
        raise ValueError("大工具 fixture payload 尺寸过小")
    body = (line * ((body_size // len(line)) + 1))[:body_size]
    return f"{header}{body}{footer}"


def _set_model(interaction: dict[str, object], model: str) -> None:
    request = interaction.get("request")
    if not isinstance(request, dict):
        raise TypeError("手写 SSE interaction 缺少 request")
    match = request.get("match")
    if not isinstance(match, dict):
        raise TypeError("手写 SSE interaction 缺少 request.match")
    match["model"] = model


def _large_tool_interaction(
    interaction: dict[str, object],
    *,
    marker: str,
) -> None:
    response = interaction.get("response")
    if not isinstance(response, dict):
        raise TypeError("手写 SSE interaction 缺少 response")
    frames = response.get("frames")
    if not isinstance(frames, list):
        raise TypeError("手写 SSE interaction 缺少 response.frames")
    args = {
        "tool_name": "large_test_output",
        "arguments": {
            "lines": 768,
            "marker": marker,
            "output_bytes": LARGE_PAYLOAD_BYTES,
            "query_context": _large_payload(f"LARGE_CALL {marker}"),
        },
    }
    replaced = False
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            continue
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        tool_calls = delta.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        call = tool_calls[0]
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        function["name"] = "invoke_custom_tool"
        function["arguments"] = json.dumps(args, ensure_ascii=False)
        replaced = True
        break
    if not replaced:
        raise RuntimeError("手写 SSE large tool interaction 未找到 tool_call frame")
    # 原始 cassette 把 tool arguments 拆成两帧；完整参数已放入第一帧，
    # 第二帧清空，避免把 JSON 重复拼接成非法参数。
    cleared_first = False
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            continue
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        tool_calls = delta.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        if not cleared_first:
            cleared_first = True
            continue
        delta["tool_calls"] = []


def _cassette() -> StreamScenario:
    """合并三种 provider 的原始 cassette，但保持 interaction 协议隔离。"""

    def interactions_from(
        scenario_id: str,
        *,
        model: str | None = None,
    ) -> list[dict[str, object]]:
        source = load_scenario(FIXTURE_ROOT, scenario_id).cassette
        protocol = source.metadata.get("protocol")
        if not isinstance(protocol, str) or protocol == "mixed":
            raise TypeError(f"手写 SSE scenario 缺少单一 protocol: {scenario_id}")
        raw_interactions = source.raw.get("interactions")
        if not isinstance(raw_interactions, list):
            raise TypeError(f"手写 SSE scenario interactions 结构非法: {scenario_id}")
        result: list[dict[str, object]] = []
        for raw_interaction in raw_interactions:
            if not isinstance(raw_interaction, dict):
                raise TypeError(f"手写 SSE interaction 结构非法: {scenario_id}")
            interaction = copy.deepcopy(raw_interaction)
            interaction["protocol"] = protocol
            if protocol == "openai_responses_sse":
                request = interaction.get("request")
                if isinstance(request, dict):
                    match = request.get("match")
                    if isinstance(match, dict):
                        # LiteLLM 是否把 stream 写入 Responses 请求体取决于
                        # provider/model capability；SSE 读取本身由 HTTPX
                        # 的 stream 参数控制，因此不能把它作为身份匹配键。
                        match.pop("stream", None)
            if model is not None:
                _set_model(interaction, model)
            result.append(interaction)
        return result

    chat_simple = interactions_from("reasoning-stream", model="handwritten-stream")
    chat_tool = interactions_from("reasoning-tool", model="handwritten-tool")
    chat_large = interactions_from("reasoning-tool", model="handwritten-large")
    _large_tool_interaction(chat_large[0], marker="turn-0000")

    responses_simple = interactions_from("responses-reasoning-text")
    responses_tool = interactions_from(
        "responses-reasoning-tool",
        model="gpt-4o",
    )
    responses_tool[0]["request"]["match"]["input_types"] = [
        "message",
        "message",
    ]
    responses_tool[1]["request"]["match"]["input_types"] = [
        "message",
        "message",
        "function_call",
        "function_call_output",
    ]
    anthropic_simple = interactions_from("anthropic-reasoning-stream")
    anthropic_tool = interactions_from(
        "anthropic-reasoning-tool",
        model="claude-handwritten-tool",
    )

    combined = {
        "schema_version": 1,
        "kind": "model_stream_cassette",
        "metadata": {
            "source": "handwritten",
            "asset_id": "handwritten-rollout-history",
            "protocol": "mixed",
            "provider": "provider_specific",
            "model": "handwritten-rollout",
        },
        "interactions": [
            *chat_simple,
            *chat_tool,
            *chat_large,
            *responses_simple,
            *responses_tool,
            *anthropic_simple,
            *anthropic_tool,
        ],
    }
    cassette = load_cassette_from_object(combined)
    return StreamScenario(
        scenario_id="handwritten-rollout-history",
        asset_path=Path("<handwritten-sse-generated>"),
        cassette=cassette,
        business_assertion="handwritten-sse-rollout",
        raw=combined,
    )


def _install_transport(
    scenario: StreamScenario,
) -> tuple[httpx.AsyncClient, httpx.AsyncClient | None, object]:
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="replay",
        replay_policy="request_reusable",
        artifact_root=PROJECT_ROOT / "out" / "tests" / "temp" / "handwritten-sse-rollout",
    )
    client = httpx.AsyncClient(transport=transport)
    previous_session = litellm.aclient_session
    if previous_session is not None:
        raise RuntimeError("生成手写 SSE fixture 前 LiteLLM async client 已存在")
    injected_handler = AsyncHTTPHandler.__new__(AsyncHTTPHandler)
    injected_handler.timeout = None
    injected_handler.event_hooks = None
    injected_handler.client_alias = "boxteam-handwritten-sse-rollout"
    injected_handler.client = client
    previous_factory = litellm_http_handler.get_async_httpx_client

    def get_injected_async_client(*_args: object, **_kwargs: object) -> AsyncHTTPHandler:
        return injected_handler

    litellm_http_handler.get_async_httpx_client = get_injected_async_client
    litellm.aclient_session = client
    return client, previous_session, previous_factory


def _restore_transport(
    client: httpx.AsyncClient,
    previous_session: httpx.AsyncClient | None,
    previous_factory: object,
) -> None:
    litellm_http_handler.get_async_httpx_client = previous_factory
    if litellm.aclient_session is client:
        litellm.aclient_session = previous_session


async def _invoke(
    model: BaseChatModel,
    messages: Sequence[BaseMessage],
) -> AIMessage:
    combined: AIMessageChunk | None = None
    async for chunk in model.astream(list(messages)):
        if not isinstance(chunk, AIMessageChunk):
            raise TypeError(f"手写 SSE 模型流返回了非 AIMessageChunk: {type(chunk).__name__}")
        combined = chunk if combined is None else combined + chunk
    if combined is None:
        raise RuntimeError("手写 SSE 模型流没有返回 chunk")
    message = message_chunk_to_message(combined)
    if not isinstance(message, AIMessage):
        raise TypeError(f"手写 SSE 聚合结果不是 AIMessage: {type(message).__name__}")
    return message


def _model_name(model: BaseChatModel) -> str:
    value = getattr(model, "model_name", None) or getattr(model, "model", None)
    if not isinstance(value, str) or not value:
        raise TypeError(f"手写 SSE provider 缺少模型名: {type(model).__name__}")
    return value


def _stamp_message(
    message: AIMessage,
    *,
    message_id: str,
    turn: int,
    turn_id: str,
    provider_id: str,
    phase: str,
    model_provider: str,
    api_mode: str,
    model_name: str,
) -> AIMessage:
    metadata = dict(message.response_metadata or {})
    stamp = _stamp(turn)
    metadata.update(
        {
            "created_at": stamp,
            "updated_at": stamp,
            "message_metadata": {"turn_id": turn_id, "job_id": turn_id},
            "provider_id": provider_id,
            "model_provider": model_provider,
            "model": model_name,
            "api_mode": api_mode,
            "phase": phase,
            "stream_source": "tests/fixtures/model_stream/handwritten",
        }
    )
    return message.model_copy(update={"id": message_id, "response_metadata": metadata})


def _tool_message(
    *,
    turn: int,
    turn_id: str,
    tool_call_id: str,
    content: str,
    name: str,
    provider_id: str,
    model_provider: str,
    api_mode: str,
) -> ToolMessage:
    stamp = _stamp(turn)
    return ToolMessage(
        id=f"tool-result-{turn:04d}-{tool_call_id}",
        content=content,
        name=name,
        tool_call_id=tool_call_id,
        response_metadata={
            "created_at": stamp,
            "updated_at": stamp,
            "message_metadata": {"turn_id": turn_id, "job_id": turn_id},
            "provider_id": provider_id,
            "model_provider": model_provider,
            "api_mode": api_mode,
            "phase": "tool_result",
            "stream_source": "tests/fixtures/model_stream/handwritten",
        },
    )


def _checkpoint(
    checkpoint_id: str,
    messages: list[BaseMessage],
    turn: int,
    provider_id: str,
    event: dict[str, object] | None,
) -> dict[str, object]:
    from langgraph.checkpoint.base import empty_checkpoint

    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    values: dict[str, object] = {
        "messages": messages,
        "model_provider": provider_id,
        "turn_count": turn,
    }
    versions = {
        "messages": f"{turn:032d}.handwritten-sse",
        "model_provider": f"{turn:032d}.handwritten-sse",
        "turn_count": f"{turn:032d}.handwritten-sse",
    }
    updated = ["messages", "model_provider", "turn_count"]
    if event is not None:
        values["_summarization_event"] = event
        versions["_summarization_event"] = f"{turn:032d}.handwritten-sse"
        updated.append("_summarization_event")
    checkpoint["channel_values"] = values
    checkpoint["channel_versions"] = versions
    checkpoint["updated_channels"] = updated
    checkpoint["versions_seen"] = {"agent": {"messages": str(turn)}}
    checkpoint["pending_sends"] = []
    return checkpoint


def _write_session_manifest(
    session_dir: Path,
    *,
    session_id: str,
    title: str,
    provider_id: str,
) -> None:
    stamp = _stamp(0)
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "created_at": stamp,
                "updated_at": stamp,
                "session_id": session_id,
                "workspace_id": "ws_custom_tool_fixture",
                "title": title,
                "title_source": "user",
                "current_agent_id": "default",
                "current_provider_id": provider_id,
                "parent_session_id": None,
                "context_source_session_id": None,
                "kind": "normal",
                "delegation": None,
                "generation_origin": {
                    "generator_id": "handwritten-sse-rollout-fixture",
                    "run_id": "handwritten-rollout-history",
                    "idempotency_key": f"handwritten-sse:{session_id}",
                    "generator_type_id": "handwritten_sse",
                    "generator_type_version": "1",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def _generate_session(
    *,
    resolver: SessionPathResolver,
    saver: RolloutCheckpointSaver,
    providers: tuple[HandwrittenProvider, ...],
    session_id: str,
    title: str,
    turn_count: int,
    tool_every: int,
    large_tools: bool,
    compaction_points: tuple[int, ...],
    checkpoint_prefix: str = "checkpoint",
    turn_id_prefix: str = "job",
    fork_source: tuple[str, str] | None = None,
) -> None:
    session_dir = resolver.allocate_session_dir(session_id=session_id, title=title)
    _write_session_manifest(
        session_dir,
        session_id=session_id,
        title=title,
        provider_id=providers[0].provider_id,
    )
    resolver.register_session(session_id, session_dir)
    if fork_source is not None:
        saver.record_fork_origin(
            target_thread_id=session_id,
            source_session_id=fork_source[0],
            source_checkpoint_id=fork_source[1],
            source_view_id=None,
            fork_mode="reference",
            relationship="detached",
        )

    system = SystemMessage(
        content=(
            "你是用于历史回放的本地测试模型。请按流式阶段输出完整的可复查思考摘要、"
            "工具调用和最终结论。"
        )
    )
    history: list[BaseMessage] = []
    config: RunnableConfig = build_checkpoint_config(session_id)
    model_call_index = 0
    segment_start = 0
    boundaries = set(compaction_points) | {turn_count}
    for turn in range(1, turn_count + 1):
        turn_id = f"{turn_id_prefix}-{turn:04d}"
        topic = TOPICS[(turn - 1) % len(TOPICS)]
        user = HumanMessage(
            id=f"user-{turn:04d}",
            content=f"手写 SSE 历史样本第 {turn} 轮：请检查{topic}，并保留可复查的工具证据。",
            response_metadata={
                "created_at": _stamp(turn),
                "updated_at": _stamp(turn),
                "message_metadata": {"turn_id": turn_id, "job_id": turn_id},
                "stream_source": "handwritten_sse",
            },
        )
        history.append(user)
        use_tool = tool_every > 0 and turn % tool_every == 0
        if use_tool:
            first_provider = providers[model_call_index % len(providers)]
            model_call_index += 1
            first_model = first_provider.model_for_call(
                use_tool=True,
                large_tools=large_tools,
            )
            first = await _invoke(first_model, [system, user])
            first_id = f"assistant-tool-{turn:04d}"
            first = _stamp_message(
                first,
                message_id=first_id,
                turn=turn,
                turn_id=turn_id,
                provider_id=first_provider.provider_id,
                phase="tool_call",
                model_provider=first_provider.model_provider,
                api_mode=first_provider.api_mode,
                model_name=_model_name(first_model),
            )
            if not first.tool_calls:
                raise RuntimeError(f"手写 SSE tool fixture 没有生成 tool_call: turn={turn}")
            call = first.tool_calls[0]
            call_id = str(call["id"])
            tool_name = str(call.get("name") or "read_file")
            if large_tools:
                call["name"] = "invoke_custom_tool"
                call["args"] = {
                    "tool_name": "large_test_output",
                    "arguments": {
                        "lines": 768,
                        "marker": f"turn-{turn:04d}",
                        "output_bytes": LARGE_PAYLOAD_BYTES,
                        "query_context": _large_payload(f"LARGE_CALL turn-{turn:04d}"),
                    },
                }
                tool_name = "invoke_custom_tool"
                tool_result = _large_payload(f"LARGE_RESULT turn-{turn:04d}")
            else:
                tool_result = json.dumps(
                    {"turn": turn, "result": f"handwritten-sse-tool-ok:{topic}"},
                    ensure_ascii=False,
                )
            history.append(first)
            tool = _tool_message(
                turn=turn,
                turn_id=turn_id,
                tool_call_id=call_id,
                content=tool_result,
                name=tool_name,
                provider_id=first_provider.provider_id,
                model_provider=first_provider.model_provider,
                api_mode=first_provider.api_mode,
            )
            history.append(tool)
            final_provider = providers[model_call_index % len(providers)]
            model_call_index += 1
            final_model = final_provider.model_for_call(
                use_tool=True,
                large_tools=large_tools,
            )
            final = await _invoke(final_model, [system, user, first, tool])
            final = _stamp_message(
                final,
                message_id=f"assistant-final-{turn:04d}",
                turn=turn,
                turn_id=turn_id,
                provider_id=final_provider.provider_id,
                phase="final_answer",
                model_provider=final_provider.model_provider,
                api_mode=final_provider.api_mode,
                model_name=_model_name(final_model),
            )
            history.append(final)
            final_message_id = str(final.id)
        else:
            final_provider = providers[model_call_index % len(providers)]
            model_call_index += 1
            final_model = final_provider.model_for_call(
                use_tool=False,
                large_tools=large_tools,
            )
            final = await _invoke(final_model, [system, user])
            final = _stamp_message(
                final,
                message_id=f"assistant-final-{turn:04d}",
                turn=turn,
                turn_id=turn_id,
                provider_id=final_provider.provider_id,
                phase="final_answer",
                model_provider=final_provider.model_provider,
                api_mode=final_provider.api_mode,
                model_name=_model_name(final_model),
            )
            history.append(final)
            final_message_id = str(final.id)

        event = None
        if turn in boundaries:
            event = {
                "event_id": f"compaction-{turn:04d}",
                "strategy": "cache_preserving",
                "cutoff_index": segment_start,
                "cache_prefix_messages": [],
                "summary_message": HumanMessage(
                    id=f"summary-{turn:04d}",
                    content=f"第 {turn} 轮前完成上下文压缩，保留主题和工具结论。",
                    additional_kwargs={"lc_source": "summarization"},
                ),
                "file_path": f".boxteam/context-history/compaction-{turn:04d}.json",
            }
            segment_start = len(history)
        config = saver.put(
            config,
            _checkpoint(
                f"{checkpoint_prefix}-{turn:04d}",
                history,
                turn,
                final_provider.provider_id,
                event,
            ),
            {
                "source": "handwritten-sse-rollout-fixture",
                "turn": turn,
                "provider_id": final_provider.provider_id,
                "api_mode": final_provider.api_mode,
                "stream_source": "tests/fixtures/model_stream/handwritten",
                "semantic_boundary": "compaction" if event else "turn",
            },
            {
                "messages": f"{turn:032d}.handwritten-sse",
                "model_provider": f"{turn:032d}.handwritten-sse",
                "turn_count": f"{turn:032d}.handwritten-sse",
            },
        )
        saver.finalize_turn(
            session_id=session_id,
            turn_id=turn_id,
            final_message_id=final_message_id,
        )


def _delete_all_sessions(resolver: SessionPathResolver) -> list[str]:
    deleted: list[str] = []
    for node in sorted(resolver.list_nodes(refresh=True), key=lambda item: item.node_id):
        resolver.delete_session_subtree(node.node_id)
        deleted.append(node.node_id)
    return deleted


def _write_fixture_manifest(workspace_root: Path) -> None:
    sessions = [
        (STATIC_SESSION_ID, 128, "自定义工具工作区：128 Turn 历史压测（handwritten SSE）"),
        (REAL_SESSION_ID, 128, "手写 SSE 128 Turn block carrier 压测"),
        (LARGE_SESSION_ID, 128, "大型工具调用：128 Turn 历史投影压测（handwritten SSE）"),
        (COMPACTION_SESSION_ID, 24, "上下文压缩与摘要示例（handwritten SSE）"),
        (TOOL_SESSION_ID, 12, "多工具调用和大输出示例（handwritten SSE）"),
        (FORK_SESSION_ID, 7, "独立历史分支示例（handwritten SSE）"),
    ]
    (workspace_root / "rollout-fixture.json").write_text(
        json.dumps(
            {
                "fixture_version": 5,
                "description": "由 tests/fixtures/model_stream/handwritten 重建的 rollout 历史 fixture",
                "source": "handwritten_sse",
                "source_assets": [
                    "openai_chat/reasoning-stream.json",
                    "openai_chat/reasoning-tool.json",
                    "openai_responses/reasoning-text.json",
                    "openai_responses/reasoning-tool.json",
                    "anthropic_messages/reasoning-stream.json",
                    "anthropic_messages/reasoning-tool.json",
                ],
                "provider_message_cycle": [
                    "chat_completions",
                    "responses",
                    "anthropic_messages",
                ],
                "history_semantics": [
                    "user_message",
                    "provider_scoped_reasoning_block",
                    "tool_call",
                    "tool_result",
                    "final_response",
                    "compaction_boundary",
                    "fork_origin",
                ],
                "large_tool_payload": {
                    "tool_name": "large_test_output",
                    "argument_bytes": LARGE_PAYLOAD_BYTES,
                    "result_bytes": LARGE_PAYLOAD_BYTES,
                    "stored_as": "inline JSONL record",
                    "detail_behavior": "bounded detail with detail_truncated",
                },
                "sessions": [
                    {
                        "session_id": session_id,
                        "turn_count": turn_count,
                        "title": title,
                        "kind": "handwritten_sse_snapshot",
                    }
                    for session_id, turn_count, title in sessions
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def _generate(workspace_root: Path) -> None:
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    resolver = SessionPathResolver(sessions_dir)
    resolver.initialize()
    deleted = _delete_all_sessions(resolver)
    print(f"deleted sessions: {deleted}")

    scenario = _cassette()
    client, previous_session, previous_factory = _install_transport(scenario)
    try:
        chat_model = BoxteamLiteLLMChatModel(
            model="openai/handwritten-stream",
            api_key="handwritten-sse-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
        )
        chat_tool_model = BoxteamLiteLLMChatModel(
            model="openai/handwritten-tool",
            api_key="handwritten-sse-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
        )
        chat_large_model = BoxteamLiteLLMChatModel(
            model="openai/handwritten-large",
            api_key="handwritten-sse-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
        )
        responses_model = BoxteamOpenAIResponsesModel(
            model="gpt-5.6-luna",
            api_key="handwritten-sse-key",
            api_base="https://www.cctq.ai/v1",
            custom_llm_provider="openai",
            streaming=True,
            provider_id="handwritten_responses",
            responses_include=["reasoning.encrypted_content"],
            responses_store=False,
        )
        responses_tool_model = BoxteamOpenAIResponsesModel(
            model="gpt-4o",
            api_key="handwritten-sse-key",
            api_base="https://www.cctq.ai/v1",
            custom_llm_provider="openai",
            streaming=True,
            provider_id="handwritten_responses",
            responses_include=["reasoning.encrypted_content"],
            responses_store=False,
        )
        anthropic_model = BoxteamAnthropicMessagesModel(
            model_name="claude-handwritten",
            api_key="handwritten-sse-key",
            base_url="https://anthropic.handwritten",
            streaming=True,
            thinking={"type": "enabled", "budget_tokens": 128},
            provider_id="handwritten_anthropic",
        )
        anthropic_tool_model = BoxteamAnthropicMessagesModel(
            model_name="claude-handwritten-tool",
            api_key="handwritten-sse-key",
            base_url="https://anthropic.handwritten",
            streaming=True,
            thinking={"type": "enabled", "budget_tokens": 128},
            provider_id="handwritten_anthropic",
        )
        for model in (anthropic_model, anthropic_tool_model):
            model.__dict__["_async_client"] = anthropic.AsyncAnthropic(
                api_key="handwritten-sse-key",
                base_url="https://anthropic.handwritten",
                http_client=client,
            )
        providers = (
            HandwrittenProvider(
                provider_id="handwritten_chat",
                model_provider="openai",
                api_mode="chat_completions",
                model=chat_model,
                tool_model=chat_tool_model,
                large_model=chat_large_model,
            ),
            HandwrittenProvider(
                provider_id="handwritten_responses",
                model_provider="openai",
                api_mode="responses",
                model=responses_model,
                tool_model=responses_tool_model,
                large_model=responses_tool_model,
            ),
            HandwrittenProvider(
                provider_id="handwritten_anthropic",
                model_provider="anthropic",
                api_mode="anthropic_messages",
                model=anthropic_model,
                tool_model=anthropic_tool_model,
                large_model=anthropic_tool_model,
            ),
        )
        saver = RolloutCheckpointSaver(sessions_dir)
        await _generate_session(
            resolver=resolver,
            saver=saver,
            providers=providers,
            session_id=STATIC_SESSION_ID,
            title="自定义工具工作区：128 Turn 历史压测（handwritten SSE）",
            turn_count=128,
            tool_every=8,
            large_tools=False,
            compaction_points=(32, 64, 96),
            checkpoint_prefix="checkpoint",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            providers=providers,
            session_id=REAL_SESSION_ID,
            title="手写 SSE 128 Turn block carrier 压测",
            turn_count=128,
            tool_every=8,
            large_tools=False,
            compaction_points=(32, 64, 96),
            checkpoint_prefix="real-checkpoint",
            turn_id_prefix="real-turn",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            providers=providers,
            session_id=LARGE_SESSION_ID,
            title="大型工具调用：128 Turn 历史投影压测（handwritten SSE）",
            turn_count=128,
            tool_every=8,
            large_tools=True,
            compaction_points=(32, 64, 96),
            checkpoint_prefix="checkpoint",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            providers=providers,
            session_id=COMPACTION_SESSION_ID,
            title="上下文压缩与摘要示例（handwritten SSE）",
            turn_count=24,
            tool_every=1,
            large_tools=False,
            compaction_points=(8, 16),
            checkpoint_prefix="checkpoint",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            providers=providers,
            session_id=TOOL_SESSION_ID,
            title="多工具调用和大输出示例（handwritten SSE）",
            turn_count=12,
            tool_every=4,
            large_tools=True,
            compaction_points=(6,),
            checkpoint_prefix="checkpoint",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            providers=providers,
            session_id=FORK_SESSION_ID,
            title="独立历史分支示例（handwritten SSE）",
            turn_count=7,
            tool_every=0,
            large_tools=False,
            compaction_points=(),
            checkpoint_prefix="checkpoint",
            fork_source=(STATIC_SESSION_ID, "checkpoint-0064"),
        )
    finally:
        _restore_transport(client, previous_session, previous_factory)
        await client.aclose()
    resolver.refresh()
    _write_fixture_manifest(workspace_root)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=TARGET_WORKSPACE)
    args = parser.parse_args()
    asyncio.run(_generate(args.workspace.resolve()))


if __name__ == "__main__":
    main()
