"""用手写 Chat Completions SSE cassette 重建 rollout 历史 fixture。"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import litellm
import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler
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

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.core.checkpoint_config import build_checkpoint_config
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.core.session_paths import SessionPathResolver
from app.testing.model_stream import (
    ModelStreamHTTPTransport,
    StreamScenario,
    load_cassette_from_object,
    load_scenario,
)

TARGET_WORKSPACE = PROJECT_ROOT / "asset" / "custom_tool_test_workspace"
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


def _reasoning_frame(frame: dict[str, object], *, marker: str) -> None:
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return
    choice = choices[0]
    if not isinstance(choice, dict):
        return
    delta = choice.get("delta")
    if not isinstance(delta, dict) or not delta.get("reasoning_content"):
        return
    delta["thinking_blocks"] = [
        {"type": "redacted_thinking", "data": f"redacted-{marker}"}
    ]
    delta["model_extra"] = {
        "reasoning_items": [
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": f"summary-{marker}"}
                ],
            }
        ]
    }


def _enhance_reasoning_interaction(
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
    for frame in frames:
        if isinstance(frame, dict):
            _reasoning_frame(frame, marker=marker)


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
    simple = copy.deepcopy(
        load_scenario(FIXTURE_ROOT, "reasoning-stream").cassette.raw
    )
    tool = copy.deepcopy(
        load_scenario(FIXTURE_ROOT, "reasoning-tool").cassette.raw
    )
    large = copy.deepcopy(tool)

    simple_interaction = simple["interactions"][0]
    tool_interactions = tool["interactions"]
    large_interactions = large["interactions"]
    if not isinstance(simple_interaction, dict) or not isinstance(tool_interactions, list):
        raise TypeError("手写 SSE cassette 结构非法")
    if not isinstance(large_interactions, list):
        raise TypeError("手写 SSE large cassette 结构非法")

    _set_model(simple_interaction, "handwritten-stream")
    _enhance_reasoning_interaction(simple_interaction, marker="stream")
    for interaction in tool_interactions:
        if not isinstance(interaction, dict):
            raise TypeError("手写 SSE tool interaction 结构非法")
        _set_model(interaction, "handwritten-tool")
        _enhance_reasoning_interaction(interaction, marker="tool")
    for interaction in large_interactions:
        if not isinstance(interaction, dict):
            raise TypeError("手写 SSE large interaction 结构非法")
        _set_model(interaction, "handwritten-large")
        _enhance_reasoning_interaction(interaction, marker="large")
    _large_tool_interaction(large_interactions[0], marker="turn-0000")

    combined = {
        "schema_version": 1,
        "kind": "model_stream_cassette",
        "metadata": {
            "source": "handwritten",
            "asset_id": "handwritten-rollout-history",
            "protocol": "openai_chat_sse",
            "provider": "openai_compatible",
            "model": "handwritten-rollout",
        },
        "interactions": [
            simple_interaction,
            *tool_interactions,
            *large_interactions,
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
    model: BoxteamLiteLLMChatModel,
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


def _stamp_message(
    message: AIMessage,
    *,
    message_id: str,
    turn: int,
    turn_id: str,
    provider_id: str,
    phase: str,
) -> AIMessage:
    metadata = dict(message.response_metadata or {})
    stamp = _stamp(turn)
    metadata.update(
        {
            "created_at": stamp,
            "updated_at": stamp,
            "message_metadata": {"turn_id": turn_id, "job_id": turn_id},
            "provider_id": provider_id,
            "model_provider": "handwritten_sse",
            "model": "handwritten-stream",
            "api_mode": "chat_completions",
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
            "model_provider": "handwritten_sse",
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
    model: BoxteamLiteLLMChatModel,
    tool_model: BoxteamLiteLLMChatModel,
    session_id: str,
    title: str,
    turn_count: int,
    tool_every: int,
    large_tools: bool,
    compaction_points: tuple[int, ...],
    provider_ids: tuple[str, ...],
    checkpoint_prefix: str = "checkpoint",
    turn_id_prefix: str = "job",
    fork_source: tuple[str, str] | None = None,
) -> None:
    session_dir = resolver.allocate_session_dir(session_id=session_id, title=title)
    _write_session_manifest(
        session_dir,
        session_id=session_id,
        title=title,
        provider_id=provider_ids[0],
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

    system = SystemMessage(content="你是用于历史回放的本地测试模型。只输出简短结论。")
    history: list[BaseMessage] = []
    config: RunnableConfig = build_checkpoint_config(session_id)
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
        provider_id = provider_ids[(turn - 1) % len(provider_ids)]
        use_tool = tool_every > 0 and turn % tool_every == 0
        if use_tool:
            first = await _invoke(tool_model, [system, user])
            first_id = f"assistant-tool-{turn:04d}"
            first = _stamp_message(
                first,
                message_id=first_id,
                turn=turn,
                turn_id=turn_id,
                provider_id=provider_id,
                phase="tool_call",
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
                provider_id=provider_id,
            )
            history.append(tool)
            final = await _invoke(tool_model, [system, user, first, tool])
            final = _stamp_message(
                final,
                message_id=f"assistant-final-{turn:04d}",
                turn=turn,
                turn_id=turn_id,
                provider_id=provider_id,
                phase="final_answer",
            )
            history.append(final)
            final_message_id = str(final.id)
        else:
            final = await _invoke(model, [system, user])
            final = _stamp_message(
                final,
                message_id=f"assistant-final-{turn:04d}",
                turn=turn,
                turn_id=turn_id,
                provider_id=provider_id,
                phase="final_answer",
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
                provider_id,
                event,
            ),
            {
                "source": "handwritten-sse-rollout-fixture",
                "turn": turn,
                "provider_id": provider_id,
                "api_mode": "chat_completions",
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
                "fixture_version": 4,
                "description": "由 tests/fixtures/model_stream/handwritten 重建的 rollout 历史 fixture",
                "source": "handwritten_sse",
                "source_assets": [
                    "openai_chat/reasoning-stream.json",
                    "openai_chat/reasoning-tool.json",
                ],
                "history_semantics": [
                    "user_message",
                    "reasoning_content",
                    "reasoning_items",
                    "redacted_thinking",
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
        simple_model = BoxteamLiteLLMChatModel(
            model="openai/handwritten-stream",
            api_key="handwritten-sse-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
        )
        tool_model = BoxteamLiteLLMChatModel(
            model="openai/handwritten-tool",
            api_key="handwritten-sse-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
        )
        large_model = BoxteamLiteLLMChatModel(
            model="openai/handwritten-large",
            api_key="handwritten-sse-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
        )
        saver = RolloutCheckpointSaver(sessions_dir)
        await _generate_session(
            resolver=resolver,
            saver=saver,
            model=simple_model,
            tool_model=tool_model,
            session_id=STATIC_SESSION_ID,
            title="自定义工具工作区：128 Turn 历史压测（handwritten SSE）",
            turn_count=128,
            tool_every=8,
            large_tools=False,
            compaction_points=(32, 64, 96),
            provider_ids=("handwritten_sse_primary", "handwritten_sse_backup"),
            checkpoint_prefix="checkpoint",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            model=simple_model,
            tool_model=tool_model,
            session_id=REAL_SESSION_ID,
            title="手写 SSE 128 Turn block carrier 压测",
            turn_count=128,
            tool_every=8,
            large_tools=False,
            compaction_points=(32, 64, 96),
            provider_ids=("handwritten_sse_chat_a", "handwritten_sse_chat_b"),
            checkpoint_prefix="real-checkpoint",
            turn_id_prefix="real-turn",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            model=large_model,
            tool_model=large_model,
            session_id=LARGE_SESSION_ID,
            title="大型工具调用：128 Turn 历史投影压测（handwritten SSE）",
            turn_count=128,
            tool_every=8,
            large_tools=True,
            compaction_points=(32, 64, 96),
            provider_ids=("handwritten_sse_large",),
            checkpoint_prefix="checkpoint",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            model=tool_model,
            tool_model=tool_model,
            session_id=COMPACTION_SESSION_ID,
            title="上下文压缩与摘要示例（handwritten SSE）",
            turn_count=24,
            tool_every=1,
            large_tools=False,
            compaction_points=(8, 16),
            provider_ids=("handwritten_sse_tool",),
            checkpoint_prefix="checkpoint",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            model=tool_model,
            tool_model=tool_model,
            session_id=TOOL_SESSION_ID,
            title="多工具调用和大输出示例（handwritten SSE）",
            turn_count=12,
            tool_every=4,
            large_tools=True,
            compaction_points=(6,),
            provider_ids=("handwritten_sse_tool",),
            checkpoint_prefix="checkpoint",
        )
        await _generate_session(
            resolver=resolver,
            saver=saver,
            model=simple_model,
            tool_model=tool_model,
            session_id=FORK_SESSION_ID,
            title="独立历史分支示例（handwritten SSE）",
            turn_count=7,
            tool_every=0,
            large_tools=False,
            compaction_points=(),
            provider_ids=("handwritten_sse_fork",),
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
