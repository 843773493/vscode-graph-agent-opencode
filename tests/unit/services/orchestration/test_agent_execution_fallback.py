from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.model_delta_context import get_current_model_delta_sink
from app.core.session_interrupt_state import SessionInterruptState
from app.services.infrastructure.message_stream_store import MessageStreamTerminalError
from app.services.orchestration.agent_event_stream_processor import (
    STREAM_JOB_ID_METADATA_KEY,
    STREAM_SESSION_ID_METADATA_KEY,
    AgentEventStreamResult,
    SuccessfulToolCall,
)
from app.services.orchestration.agent_execution_service import (
    AgentExecutionService,
    _has_valid_delegated_report,
    _has_valid_session_question_reply,
)


@pytest.fixture
def mock_dependencies():
    """创建一组共用的 mock 依赖。"""
    config_service = MagicMock()
    config_service.get_snapshot.return_value = object()
    config_service.use_snapshot.side_effect = lambda _snapshot: nullcontext()
    config_service.resolve_agent_id.return_value = "test_agent"
    config_service.get_agent_runtime_config.return_value = {
        "providers": [
            {"custom_llm_provider": "openai", "model": "primary", "api_key": "k", "endpoint": "e", "temperature": 0.7, "top_p": 1.0, "max_output_tokens": 1024},
            {"custom_llm_provider": "openai", "model": "fallback", "api_key": "k", "endpoint": "e", "temperature": 0.7, "top_p": 1.0, "max_output_tokens": 1024},
        ],
        "temperature": 0.7,
        "top_p": 1.0,
        "max_output_tokens": 1024,
        "system_prompt": "test",
        "require_delegated_report": False,
    }
    config_service.get_agent_tool_config.return_value = {"denylist": []}
    config_service.resolve_agent_tool_policy.return_value.enabled_names = frozenset(
        {"test_tool_2"}
    )

    registry = MagicMock()
    msg_bus = MagicMock()
    job_event_bus = MagicMock()
    job_event_bus.publish = AsyncMock()
    session_changes_service = MagicMock()
    tool_selection_store = MagicMock()
    tool_selection_store.execution_overrides.return_value = {}
    tool_selection_store.model_visibility_overrides.return_value = {}

    dependency_provider = MagicMock()
    dependency_provider.get_message_service.return_value = MagicMock()
    dependency_provider.get_session_service.return_value = MagicMock()
    dependency_provider.get_session_orchestrator.return_value = MagicMock()
    dependency_provider.get_checkpointer.return_value = None

    message_stream_writer = MagicMock()
    message_stream_writer.turn_stream_id = "strm_test"
    message_stream_writer.commit = AsyncMock()
    message_stream_writer.close_completed = AsyncMock()
    message_stream_writer.close_interrupted = AsyncMock()
    message_stream_writer.close_failed = AsyncMock()
    message_stream_store = MagicMock()
    message_stream_store.open = AsyncMock(return_value=message_stream_writer)
    message_stream_store.get_state = AsyncMock(
        return_value={"stream_status": "open", "interrupt_state": None}
    )

    return {
        "config_service": config_service,
        "registry": registry,
        "msg_bus": msg_bus,
        "job_event_bus": job_event_bus,
        "session_changes_service": session_changes_service,
        "tool_selection_store": tool_selection_store,
        "dependency_provider": dependency_provider,
        "message_stream_store": message_stream_store,
    }


def create_chunk(
    content="",
    tool_calls=None,
    *,
    part_id: str | None = None,
    index: int | None = None,
):
    """创建模拟的 chunk 对象。"""
    if isinstance(content, str) and content:
        if part_id is None or index is None:
            raise ValueError("模拟文本 chunk 必须显式提供 part_id/index")
        content = [{"type": "text", "text": content, "id": part_id, "index": index}]
    chunk = MagicMock()
    chunk.content = content
    chunk.message = None
    chunk.tool_calls = tool_calls or []
    chunk.tool_call_chunks = [
        {
            "index": index,
            "id": tool_call["id"],
            "name": tool_call["name"],
            "args": tool_call.get("args", {}),
        }
        for index, tool_call in enumerate(tool_calls or [])
    ]
    chunk.additional_kwargs = {}
    chunk.usage_metadata = None
    chunk.id = "test-id"
    return chunk


def stream_metadata(
    *,
    session_id: str = "test",
    job_id: str = "job_test",
) -> dict[str, str]:
    """构造通过事件归属校验的 LangChain 测试 metadata。"""
    return {
        STREAM_SESSION_ID_METADATA_KEY: session_id,
        STREAM_JOB_ID_METADATA_KEY: job_id,
    }


def _make_service(deps):
    """用当前测试夹具构造 AgentExecutionService。"""
    return AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
        session_changes_service=deps["session_changes_service"],
        tool_selection_store=deps["tool_selection_store"],
        message_stream_store=deps["message_stream_store"],
        workspace_root=Path.cwd(),
    )


def test_agent_cache_rebuilds_after_config_revision_changes(
    mock_dependencies,
):
    service = _make_service(mock_dependencies)
    mock_dependencies["config_service"].get_revision.side_effect = [
        "revision-a",
        "revision-a",
        "revision-b",
    ]
    first_agent = object()
    second_agent = object()

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
        side_effect=[first_agent, second_agent],
    ) as build_runtime:
        assert service._get_or_create_agent("ses_test", "test_agent") is first_agent
        assert service._get_or_create_agent("ses_test", "test_agent") is first_agent
        assert service._get_or_create_agent("ses_test", "test_agent") is second_agent

    assert build_runtime.call_count == 2
    assert list(service._agent_cache) == [
        ("ses_test", "test_agent", "revision-b", (), ()),
    ]


@pytest.mark.asyncio
async def test_run_step_pins_snapshot_through_async_tool_stage(
    mock_dependencies,
):
    service = _make_service(mock_dependencies)
    snapshot = object()
    active_snapshots: list[object] = []
    mock_dependencies["config_service"].get_snapshot.return_value = snapshot

    @contextmanager
    def use_snapshot(candidate):
        active_snapshots.append(candidate)
        try:
            yield
        finally:
            active_snapshots.pop()

    mock_dependencies["config_service"].use_snapshot.side_effect = use_snapshot

    async def fake_run_step_with_snapshot(*_args, **_kwargs):
        assert active_snapshots == [snapshot]
        await asyncio.sleep(0)
        # 模拟模型返回后进入异步工具阶段，snapshot 仍必须固定。
        assert active_snapshots == [snapshot]
        return "ok"

    service._run_step_with_snapshot = fake_run_step_with_snapshot

    result = await service.run_step(
        session_id="ses_test",
        message="test",
        agent_id="test_agent",
        job_id="job_test",
        message_id="msg_test",
        message_created_at="2026-07-19T00:00:00+00:00",
    )

    assert result == "ok"
    assert active_snapshots == []


def test_delegated_report_requires_successful_parent_directed_system_message():
    parent = "ses_parent"
    assert not _has_valid_delegated_report(
        [SuccessfulToolCall("send_message_to_session", {"target_session_id": "ses_other"})],
        parent_session_id=parent,
    )
    assert not _has_valid_delegated_report(
        [
            SuccessfulToolCall(
                "send_message_to_session",
                {
                    "target_session_id": parent,
                    "simulate_user": False,
                    "kind": "progress",
                },
            )
        ],
        parent_session_id=parent,
    )
    assert not _has_valid_delegated_report(
        [
            SuccessfulToolCall(
                "send_message_to_session",
                {"target_session_id": parent, "simulate_user": True},
            )
        ],
        parent_session_id=parent,
    )
    assert _has_valid_delegated_report(
        [
            SuccessfulToolCall(
                "send_message_to_session",
                {
                    "target_session_id": parent,
                    "simulate_user": False,
                    "kind": "result",
                },
            )
        ],
        parent_session_id=parent,
    )


def test_session_question_reply_requires_matching_communication_id():
    valid = SuccessfulToolCall(
        "send_message_to_session",
        {
            "target_session_id": "ses_sender",
            "simulate_user": False,
            "kind": "reply",
            "reply_to_communication_id": "comm_question",
        },
    )
    assert _has_valid_session_question_reply(
        [valid],
        sender_session_id="ses_sender",
        communication_id="comm_question",
    )
    assert not _has_valid_session_question_reply(
        [valid],
        sender_session_id="ses_sender",
        communication_id="comm_other",
    )


@pytest.mark.asyncio
async def test_delegated_report_is_not_enforced_by_default(mock_dependencies):
    service = _make_service(mock_dependencies)
    stream_result = AgentEventStreamResult(
        final_text="普通文本结果",
        latest_model_content_blocks=(),
        last_tool_result_text="",
    )

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            new=AsyncMock(return_value=stream_result),
        ) as process,
    ):
        result = await service.run_step(
            session_id="ses_child",
            message="委派任务",
            agent_id="test_agent",
            job_id="job_child",
            message_id="msg_child",
            message_created_at="2026-07-16T00:00:00+00:00",
            message_metadata={
                "source": "session_subagent_delegation",
                "parent_session_id": "ses_parent",
            },
        )

    assert result == "普通文本结果"
    process.assert_awaited_once()


@pytest.mark.asyncio
async def test_delegated_first_turn_fails_after_two_missing_tool_reports(
    mock_dependencies,
):
    mock_dependencies["config_service"].get_agent_runtime_config.return_value[
        "require_delegated_report"
    ] = True
    service = _make_service(mock_dependencies)
    stream_results = [
        AgentEventStreamResult(
            final_text=f"普通文本 {index}",
            latest_model_content_blocks=(),
            last_tool_result_text="",
        )
        for index in range(3)
    ]

    with (  # noqa: SIM117 - 保持异常断言的作用域清晰。
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            new=AsyncMock(side_effect=stream_results),
        ) as process,
    ):
        with pytest.raises(RuntimeError, match="没有通过 send_message_to_session"):
            await service.run_step(
                session_id="ses_child",
                message="委派任务",
                agent_id="test_agent",
                job_id="job_child",
                message_id="msg_child",
                message_created_at="2026-07-16T00:00:00+00:00",
                message_metadata={
                    "source": "session_subagent_delegation",
                    "parent_session_id": "ses_parent",
                },
            )

    assert process.await_count == 3
    assert (
        process.await_args_list[0].kwargs["config"]["recursion_limit"]
        == 9999
    )


@pytest.mark.asyncio
async def test_delegated_progress_only_cannot_replace_final_result(
    mock_dependencies,
):
    mock_dependencies["config_service"].get_agent_runtime_config.return_value[
        "require_delegated_report"
    ] = True
    service = _make_service(mock_dependencies)
    progress_call = SuccessfulToolCall(
        "send_message_to_session",
        {
            "target_session_id": "ses_parent",
            "simulate_user": False,
            "kind": "progress",
        },
    )
    stream_results = [
        AgentEventStreamResult(
            final_text=f"普通结论 {index}",
            latest_model_content_blocks=(),
            last_tool_result_text="accepted",
            successful_tool_calls=(progress_call,),
        )
        for index in range(3)
    ]

    with (  # noqa: SIM117 - 保持异常断言的作用域清晰。
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            new=AsyncMock(side_effect=stream_results),
        ),
    ):
        with pytest.raises(RuntimeError, match="没有通过 send_message_to_session"):
            await service.run_step(
                session_id="ses_child",
                message="委派任务",
                agent_id="test_agent",
                job_id="job_child",
                message_id="msg_child",
                message_created_at="2026-07-16T00:00:00+00:00",
                message_metadata={
                    "source": "session_subagent_delegation",
                    "parent_session_id": "ses_parent",
                },
            )


@pytest.mark.asyncio
async def test_cross_session_question_retries_until_correlated_tool_reply(
    mock_dependencies,
):
    service = _make_service(mock_dependencies)
    input_messages: list[object] = []
    stream_results = [
        AgentEventStreamResult(
            final_text="普通回答",
            latest_model_content_blocks=(),
            last_tool_result_text="",
        ),
        AgentEventStreamResult(
            final_text="已通过会话工具回复",
            latest_model_content_blocks=(),
            last_tool_result_text="accepted",
            successful_tool_calls=(
                SuccessfulToolCall(
                    "send_message_to_session",
                    {
                        "target_session_id": "ses_questioner",
                        "simulate_user": False,
                        "kind": "reply",
                        "reply_to_communication_id": "comm_question",
                    },
                ),
            ),
        ),
    ]

    async def process_side_effect(*, input_payload, **_kwargs):
        input_messages.append(input_payload["messages"][0])
        return stream_results.pop(0)

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            side_effect=process_side_effect,
        ),
    ):
        result = await service.run_step(
            session_id="ses_answerer",
            message="跨会话问题",
            agent_id="test_agent",
            job_id="job_answer",
            message_id="msg_question",
            message_created_at="2026-07-16T00:00:00+00:00",
            message_metadata={
                "source": "send_message_to_session",
                "kind": "question",
                "reply_required": True,
                "sender_session_id": "ses_questioner",
                "communication_id": "comm_question",
            },
        )

    assert result == "已通过会话工具回复"
    assert len(input_messages) == 2
    assert (
        input_messages[1].response_metadata["source"]
        == "session_question_reply_retry"
    )


@pytest.mark.parametrize("incoming_kind", ["reply", "progress", "result"])
@pytest.mark.asyncio
async def test_delegated_child_relays_cross_session_updates_to_its_parent(
    mock_dependencies,
    incoming_kind,
):
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value[
        "require_delegated_report"
    ] = True
    delegated_session = MagicMock()
    delegated_session.delegation.parent_session_id = "ses_parent"
    session_service = MagicMock()
    session_service.get = AsyncMock(return_value=delegated_session)
    deps["dependency_provider"].get_session_service.return_value = session_service
    service = _make_service(deps)
    outgoing_kind = "progress" if incoming_kind == "progress" else "result"
    stream_result = AgentEventStreamResult(
        final_text="继续执行并完成",
        latest_model_content_blocks=(),
        last_tool_result_text="accepted",
        successful_tool_calls=(
            SuccessfulToolCall(
                "send_message_to_session",
                {
                    "target_session_id": "ses_parent",
                    "simulate_user": False,
                    "kind": outgoing_kind,
                },
            ),
        ),
    )

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            new=AsyncMock(return_value=stream_result),
        ),
    ):
        result = await service.run_step(
            session_id="ses_child",
            message="父会话答复",
            agent_id="test_agent",
            job_id="job_continue",
            message_id="msg_reply",
            message_created_at="2026-07-16T00:00:00+00:00",
            message_metadata={
                "source": "send_message_to_session",
                "kind": incoming_kind,
                "sender_session_id": "ses_parent",
                "communication_id": "comm_reply",
                "reply_to_communication_id": "comm_question",
            },
        )

    assert result == "继续执行并完成"
    session_service.get.assert_awaited_once_with("ses_child")


@pytest.mark.asyncio
async def test_primary_success_no_fallback(mock_dependencies):
    """测试：主模型成功时，不使用 fallback。"""
    deps = mock_dependencies
    deps["tool_selection_store"].execution_overrides.return_value = {
        "apply_patch": False,
        "test_tool_2": False,
    }
    service = _make_service(deps)

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
                yield {
                    "event": "on_chat_model_stream",
                    "name": "BoxteamLiteLLMChatModel",
                    "data": {
                        "chunk": create_chunk(
                            "主模型成功",
                            part_id="part_primary",
                            index=0,
                        )
                    },
                    "metadata": stream_metadata(),
                }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
        )

        assert result == "主模型成功"
        assert mock_build.call_count == 1
        assert mock_build.call_args.kwargs["execution_overrides"] == {
            "apply_patch": False,
            "test_tool_2": False,
        }


@pytest.mark.asyncio
async def test_reasoning_stream_not_mixed_into_final_text(mock_dependencies):
    """reasoning 流只参与最终聚合，不应污染正式回复或旧实时事件。"""
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value = {
        "providers": [
            {
                "custom_llm_provider": "openai",
                "model": "primary",
                "api_key": "k",
                "endpoint": "e",
            },
        ],
        "temperature": 0.7,
        "top_p": 1.0,
        "max_output_tokens": 1024,
        "system_prompt": "test",
    }
    service = _make_service(deps)

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": create_chunk(
                        [
                            {
                                "type": "reasoning",
                                "reasoning": "先判断用户只要 OK。",
                                "id": "part_reasoning",
                                "index": 0,
                            }
                        ],
                    )
                },
                "metadata": stream_metadata(),
            }
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": create_chunk(
                        [
                            {
                                "type": "text",
                                "text": "OK",
                                "id": "part_answer",
                                "index": 1,
                            }
                        ]
                    )
                },
                "metadata": stream_metadata(),
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
        )

    assert result == "OK"
    assert not [
        call
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") == "text_delta"
    ]


@pytest.mark.asyncio
async def test_model_event_stream_does_not_recreate_legacy_text_events(mock_dependencies):
    """模型事件流不应重新创建消息流已经替代的旧文本事件。"""
    deps = mock_dependencies
    service = _make_service(deps)

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": create_chunk(
                        "第一段\n\n",
                        part_id="part_markdown",
                        index=0,
                    )
                },
                "metadata": stream_metadata(),
            }
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": create_chunk(
                        "第二段",
                        part_id="part_markdown",
                        index=0,
                    )
                },
                "metadata": stream_metadata(),
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
        )

    assert result == "第一段\n\n第二段"
    assert not [
        call
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") in {"text_start", "text_delta", "text_end"}
    ]


@pytest.mark.asyncio
async def test_reasoning_only_response_retries_with_system_reminder(mock_dependencies):
    """reasoning-only 空响应应继续请求模型产生工具调用或最终正文。"""
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value = {
        "providers": [
            {
                "custom_llm_provider": "openai",
                "model": "primary",
                "api_key": "k",
                "endpoint": "e",
            },
        ],
        "temperature": 0.7,
        "top_p": 1.0,
        "max_output_tokens": 1024,
        "system_prompt": "test",
    }
    service = _make_service(deps)

    input_messages: list[object] = []

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(input_payload, *args, **kwargs):
            input_messages.append(input_payload["messages"][0])
            if len(input_messages) == 1:
                yield {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "data": {
                        "chunk": create_chunk(
                            [
                                {
                                    "type": "reasoning",
                                    "reasoning": "我应该继续。",
                                    "id": "part_retry_reasoning",
                                    "index": 0,
                                }
                            ],
                        )
                    },
                    "metadata": stream_metadata(),
                }
                return
            assert "<system_reminder>" in input_payload["messages"][0].content
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": create_chunk(
                        [
                            {
                                "type": "text",
                                "text": "OK",
                                "id": "part_retry_answer",
                                "index": 0,
                            }
                        ]
                    )
                },
                "metadata": stream_metadata(),
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
        )

    assert result == "OK"
    assert len(input_messages) == 2
    assert input_messages[1].response_metadata["source"] == "empty_response_retry"


@pytest.mark.asyncio
async def test_requested_custom_tool_missing_result_retries_with_system_reminder(mock_dependencies):
    """用户点名配置 custom tool 但模型只输出正文时，应继续要求真实工具调用。"""
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value = {
        "providers": [
            {
                "custom_llm_provider": "openai",
                "model": "primary",
                "api_key": "k",
                "endpoint": "e",
            },
        ],
        "temperature": 0.7,
        "top_p": 1.0,
        "max_output_tokens": 1024,
        "system_prompt": "test",
    }
    deps["config_service"].get_agent_tool_config.return_value = {
        "denylist": [],
        "custom": [
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
            }
        ],
    }
    service = _make_service(deps)

    input_messages: list[object] = []
    stream_results = [
        AgentEventStreamResult(
            final_text="根据 AG",
            latest_model_content_blocks=(
                {
                    "type": "text",
                    "text": "根据 AG",
                    "id": "part_first",
                    "index": 0,
                },
            ),
            last_tool_result_text="",
            completed_custom_tool_names=(),
        ),
        AgentEventStreamResult(
            final_text="4568",
            latest_model_content_blocks=(
                {
                    "type": "text",
                    "text": "4568",
                    "id": "part_second",
                    "index": 0,
                },
            ),
            last_tool_result_text="4568",
            completed_custom_tool_names=("test_tool_2",),
        ),
    ]

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
        ) as mock_build,
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream"
        ) as mock_process,
    ):
        mock_build.return_value = MagicMock()

        async def process_side_effect(*, input_payload, **kwargs):
            input_messages.append(input_payload["messages"][0])
            return stream_results.pop(0)

        mock_process.side_effect = process_side_effect

        result = await service.run_step(
            session_id="test",
            message="请调用 test_tool_2",
            agent_id="test_agent",
            job_id="job_test",
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
        )

    assert result == "4568"
    assert len(input_messages) == 2
    assert input_messages[1].response_metadata["source"] == "missing_custom_tool_retry"
    assert "test_tool_2" in input_messages[1].response_metadata["missing_tools"]


@pytest.mark.asyncio
async def test_standard_content_blocks_stream_split_reasoning_and_text(mock_dependencies):
    """标准 content blocks 流应拆分为 reasoning/text 两种事件。"""
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value = {
        "providers": [
            {
                "custom_llm_provider": "openai",
                "model": "primary",
                "api_key": "k",
                "endpoint": "e",
            },
        ],
        "temperature": 0.7,
        "top_p": 1.0,
        "max_output_tokens": 1024,
        "system_prompt": "test",
    }
    service = _make_service(deps)

    content_blocks = [
        {
            "type": "reasoning",
            "reasoning": "先判断用户只要 OK。",
            "id": "part_reasoning",
            "index": 0,
        },
        {"type": "text", "text": "OK", "id": "part_answer", "index": 1},
    ]

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {"chunk": create_chunk(content_blocks)},
                "metadata": stream_metadata(),
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
        )

    assert result == "OK"
    assert not [
        call
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") == "text_delta"
    ]


@pytest.mark.asyncio
async def test_tool_events_use_tool_start_input_and_tool_message_content(mock_dependencies):
    """工具卡片应使用 on_tool_start 的完整参数和 ToolMessage.content。"""
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value = {
        "providers": [
            {
                "custom_llm_provider": "openai",
                "model": "primary",
                "api_key": "k",
                "endpoint": "e",
            },
        ],
        "temperature": 0.7,
        "top_p": 1.0,
        "max_output_tokens": 1024,
        "system_prompt": "test",
    }
    service = _make_service(deps)

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
            tool_chunk = create_chunk(
                "",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "python_exec",
                        "args": {},
                    }
                ],
            )
            delta_sink = get_current_model_delta_sink()
            if delta_sink is None:
                raise AssertionError("测试模型流缺少消息 delta sink")
            await delta_sink.accept_message_chunk(tool_chunk)
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": tool_chunk,
                },
                "metadata": stream_metadata(),
            }
            yield {
                "event": "on_tool_start",
                "run_id": "run_python_exec",
                "name": "python_exec",
                "data": {"input": {"code": "print('LC_BLOCK_OK_2')"}},
                "metadata": stream_metadata(),
            }
            yield {
                "event": "on_tool_end",
                "run_id": "run_python_exec",
                "name": "python_exec",
                "data": {
                    "output": ToolMessage(
                        content='{"stdout":"LC_BLOCK_OK_2\\n"}',
                        tool_call_id="call_1",
                        name="python_exec",
                    )
                },
                "metadata": stream_metadata(),
            }
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": create_chunk(
                        "完成",
                        part_id="part_after_tool",
                        index=0,
                    )
                },
                "metadata": stream_metadata(),
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_agent.get_graph = None
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
        )

    assert result == "完成"
    tool_start_payloads = [
        call.kwargs["payload"]
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") == "tool_call_start"
    ]
    tool_end_payloads = [
        call.kwargs["payload"]
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") == "tool_call_end"
    ]
    assert tool_start_payloads == [
        {
            "part_id": "run_python_exec",
            "execution_id": "run_python_exec",
            "tool_name": "python_exec",
            "args": {"code": "print('LC_BLOCK_OK_2')"},
            "agent_id": "test_agent",
        }
    ]
    assert tool_end_payloads == [
        {
            "part_id": "run_python_exec",
            "execution_id": "run_python_exec",
            "tool_call_id": "call_1",
            "tool_name": "python_exec",
            "result": '{"stdout":"LC_BLOCK_OK_2\\n"}',
            "status": "success",
            "failed": False,
            "agent_id": "test_agent",
        }
    ]
    assert "ToolMessage" not in tool_end_payloads[0]["result"]
    assert "content=" not in tool_end_payloads[0]["result"]


def test_extract_final_text_uses_visible_text_from_standard_blocks(mock_dependencies):
    """从最终消息提取正文时不能把 reasoning block 拼进回复。"""
    deps = mock_dependencies
    service = _make_service(deps)
    result = {
        "messages": [
            HumanMessage(content="只回复 OK"),
            AIMessage(
                content=[
                    {"type": "reasoning", "reasoning": "用户只要 OK。"},
                    {"type": "text", "text": "OK"},
                ],
            ),
        ],
    }

    assert service._extract_final_text(result) == "OK"


@pytest.mark.asyncio
async def test_execution_delegates_model_fallback_to_single_agent(mock_dependencies):
    """模型 fallback 由请求中间件完成，执行服务不应重建 Agent。"""
    deps = mock_dependencies
    service = _make_service(deps)

    with patch(  # noqa: SIM117 - 两个运行时入口必须共享同一个 mock。
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        with patch("app.runtime.agent_runtime.build_session_agent_runtime", mock_build):
            def create_mock_agent(*args, **kwargs):
                mock_agent = MagicMock()

                async def mock_events(*args, **kwargs):
                    yield {
                        "event": "on_chat_model_stream",
                        "name": "ChatOpenAI",
                        "data": {
                            "chunk": create_chunk(
                                "fallback 成功",
                                part_id="part_fallback_success",
                                index=0,
                            )
                        },
                        "metadata": stream_metadata(),
                    }

                mock_agent.astream_events = mock_events

                return mock_agent

            mock_build.side_effect = create_mock_agent

            result = await service.run_step(
                session_id="test",
                message="test",
                agent_id="test_agent",
                job_id="job_test",
                message_id="msg_test",
                message_created_at="2026-07-14T00:00:00+00:00",
            )

            assert result == "fallback 成功"
            assert mock_build.call_count == 1
            assert "override_model" not in mock_build.call_args.kwargs


@pytest.mark.asyncio
async def test_all_models_fail(mock_dependencies):
    """测试：所有模型都失败时，抛出异常。"""
    deps = mock_dependencies
    service = _make_service(deps)

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        mock_build.side_effect = Exception("所有模型都失败")

        with pytest.raises(Exception, match="所有模型都失败"):
            await service.run_step(
                session_id="test",
                message="test",
                agent_id="test_agent",
                job_id="job_test",
                message_id="msg_test",
                message_created_at="2026-07-14T00:00:00+00:00",
            )


@pytest.mark.asyncio
async def test_model_fallback_does_not_republish_agent_start(mock_dependencies):
    """模型中间件内部 fallback 不应伪装成一次新的 Agent 启动。"""
    deps = mock_dependencies
    service = _make_service(deps)

    with patch(  # noqa: SIM117 - 两个运行时入口必须共享同一个 mock。
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        with patch("app.runtime.agent_runtime.build_session_agent_runtime", mock_build):
            def create_mock_agent(*args, **kwargs):
                mock_agent = MagicMock()

                async def mock_events(*args, **kwargs):
                    yield {
                        "event": "on_chat_model_stream",
                        "name": "ChatOpenAI",
                        "data": {
                            "chunk": create_chunk(
                                "fallback",
                                part_id="part_fallback",
                                index=0,
                            )
                        },
                        "metadata": stream_metadata(),
                    }

                mock_agent.astream_events = mock_events

                return mock_agent

            mock_build.side_effect = create_mock_agent

            await service.run_step(
                session_id="test",
                message="test",
                agent_id="test_agent",
                job_id="job_test",
                message_id="msg_test",
                message_created_at="2026-07-14T00:00:00+00:00",
            )

    publish_calls = [
        c
        for c in deps["job_event_bus"].publish.call_args_list
        if c.kwargs.get("event_type") == "agent_start"
    ]
    assert len(publish_calls) == 1


@pytest.mark.asyncio
async def test_agent_loop_retry_keeps_model_attempt_boundaries(
    mock_dependencies,
):
    service = _make_service(mock_dependencies)
    stream_results = [
        AgentEventStreamResult(
            final_text="",
            latest_model_content_blocks=(),
            last_tool_result_text="",
        ),
        AgentEventStreamResult(
            final_text="最终答案",
            latest_model_content_blocks=(),
            last_tool_result_text="",
        ),
    ]
    attempt = 0

    async def process_with_retry(*, message_stream_runtime, **_kwargs):
        nonlocal attempt
        attempt += 1
        model_call_id = f"model_{attempt}"
        await message_stream_runtime.start_model(model_call_id, "primary")
        await message_stream_runtime.accept_message_chunk(
            create_chunk(
                "中间内容" if attempt == 1 else "最终答案",
                part_id=f"part_{attempt}",
                index=0,
            )
        )
        await message_stream_runtime.finish_model()
        return stream_results.pop(0)

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            side_effect=process_with_retry,
        ),
    ):
        result = await service.run_step(
            session_id="ses_retry",
            message="重试",
            agent_id="test_agent",
            job_id="job_retry",
            message_id="msg_retry",
            message_created_at="2026-07-20T00:00:00+00:00",
        )

    assert result == "最终答案"
    committed_types = [
        call.args[0]
        for call in mock_dependencies["message_stream_store"].open.return_value.commit.await_args_list
    ]
    assert committed_types.count("model.started") == 2
    assert committed_types.count("model.completed") == 2
    assert committed_types.count("model.retrying") == 1
    mock_dependencies["message_stream_store"].open.return_value.close_completed.assert_awaited_once()
    completed_outcomes = [
        call.args[1]["outcome"]
        for call in mock_dependencies["message_stream_store"].open.return_value.commit.await_args_list
        if call.args[0] == "model.completed"
    ]
    assert completed_outcomes == ["validation_failed", "accepted"]


@pytest.mark.asyncio
async def test_interrupt_wins_when_completion_races_with_persisted_request(
    mock_dependencies,
):
    service = _make_service(mock_dependencies)
    writer = mock_dependencies["message_stream_store"].open.return_value
    writer.close_completed.side_effect = MessageStreamTerminalError("已进入中断闸门")
    mock_dependencies["message_stream_store"].get_state.return_value = {
        "stream_status": "interrupting",
        "interrupt_state": {
            "request_id": "intr_race",
            "status": "requested",
        },
    }

    async def process_success(*, message_stream_runtime, **_kwargs):
        await message_stream_runtime.start_model("model_race", "primary")
        await message_stream_runtime.finish_model()
        return AgentEventStreamResult(
            final_text="竞态结果",
            latest_model_content_blocks=(),
            last_tool_result_text="",
        )

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            side_effect=process_success,
        ),
    ):
        result = await service.run_step(
            session_id="ses_race",
            message="竞态",
            agent_id="test_agent",
            job_id="job_race",
            message_id="msg_race",
            message_created_at="2026-07-20T00:00:00+00:00",
        )

    assert result == "竞态结果"
    writer.close_interrupted.assert_awaited_once_with("intr_race")
    writer.close_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_without_user_interrupt_closes_as_execution_cancelled(
    mock_dependencies,
):
    service = _make_service(mock_dependencies)

    async def process_cancelled(**_kwargs):
        raise asyncio.CancelledError()

    with (  # noqa: SIM117 - 取消异常需要单独断言原始 CancelledError。
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            side_effect=process_cancelled,
        ),
        patch(
            "app.services.orchestration.agent_execution_service.persist_interrupt_checkpoint",
        ) as persist_checkpoint,
    ):
        with pytest.raises(asyncio.CancelledError):
            await service.run_step(
                session_id="ses_cancelled",
                message="取消",
                agent_id="test_agent",
                job_id="job_cancelled",
                message_id="msg_cancelled",
                message_created_at="2026-07-20T00:00:00+00:00",
            )

    persist_checkpoint.assert_called_once()
    writer = mock_dependencies["message_stream_store"].open.return_value
    writer.close_failed.assert_awaited_once_with(
        code="execution_cancelled",
        message="AgentLoop 在没有用户中断请求的情况下被取消",
        resumable=False,
    )


@pytest.mark.asyncio
async def test_cancelled_after_user_interrupt_closes_as_interrupted(
    mock_dependencies,
):
    service = _make_service(mock_dependencies)

    async def process_user_cancelled(**_kwargs):
        SessionInterruptState.set(
            "ses_user_cancelled",
            interrupt_request_id="intr_user_cancelled",
            cancellation_reason="user_requested",
            user_interrupt_reminder_injected=True,
        )
        raise asyncio.CancelledError()

    with (  # noqa: SIM117 - 取消异常需要单独断言原始 CancelledError。
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            side_effect=process_user_cancelled,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await service.run_step(
                session_id="ses_user_cancelled",
                message="用户打断",
                agent_id="test_agent",
                job_id="job_user_cancelled",
                message_id="msg_user_cancelled",
                message_created_at="2026-07-20T00:00:00+00:00",
            )

    writer = mock_dependencies["message_stream_store"].open.return_value
    writer.close_interrupted.assert_awaited_once_with("intr_user_cancelled")
    writer.close_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_exception_persists_stream_failure_before_rethrow(
    mock_dependencies,
):
    service = _make_service(mock_dependencies)

    async def process_failed(**_kwargs):
        raise RuntimeError("上游异常")

    with (  # noqa: SIM117 - 异常路径需要单独断言原始执行异常。
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.agent_execution_service.process_agent_event_stream",
            side_effect=process_failed,
        ),
    ):
        with pytest.raises(RuntimeError, match="上游异常"):
            await service.run_step(
                session_id="ses_execution_error",
                message="异常",
                agent_id="test_agent",
                job_id="job_execution_error",
                message_id="msg_execution_error",
                message_created_at="2026-07-20T00:00:00+00:00",
            )

    writer = mock_dependencies["message_stream_store"].open.return_value
    writer.close_failed.assert_awaited_once_with(
        code="execution_error",
        message="上游异常",
        after_interrupt_requested=False,
        resumable=False,
    )
