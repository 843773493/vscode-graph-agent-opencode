from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services.orchestration.agent_execution_service import AgentExecutionService


@pytest.fixture
def mock_dependencies():
    """创建一组共用的 mock 依赖。"""
    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "test_agent"
    config_service.get_agent_runtime_config.return_value = {
        "providers": [
            {"interface": "chat.completion", "model": "primary", "api_key": "k", "endpoint": "e", "temperature": 0.7, "top_p": 1.0, "max_output_tokens": 1024},
            {"interface": "chat.completion", "model": "fallback", "api_key": "k", "endpoint": "e", "temperature": 0.7, "top_p": 1.0, "max_output_tokens": 1024},
        ],
        "temperature": 0.7,
        "top_p": 1.0,
        "max_output_tokens": 1024,
        "system_prompt": "test",
    }
    config_service.get_agent_tool_config.return_value = {"denylist": []}

    registry = MagicMock()
    msg_bus = MagicMock()
    job_event_bus = MagicMock()
    job_event_bus.publish = AsyncMock()

    dependency_provider = MagicMock()
    dependency_provider.get_message_service.return_value = MagicMock()
    dependency_provider.get_session_service.return_value = MagicMock()
    dependency_provider.get_session_orchestrator.return_value = MagicMock()
    dependency_provider.get_checkpointer.return_value = None

    return {
        "config_service": config_service,
        "registry": registry,
        "msg_bus": msg_bus,
        "job_event_bus": job_event_bus,
        "dependency_provider": dependency_provider,
    }


def create_chunk(content="", tool_calls=None):
    """创建模拟的 chunk 对象。"""
    chunk = MagicMock()
    chunk.content = content
    chunk.message = None
    chunk.tool_calls = tool_calls or []
    chunk.additional_kwargs = {}
    chunk.id = "test-id"
    return chunk


@pytest.mark.asyncio
async def test_primary_success_no_fallback(mock_dependencies):
    """测试：主模型成功时，不使用 fallback。"""
    deps = mock_dependencies
    service = AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
    )

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
                yield {
                    "event": "on_chat_model_stream",
                    "name": "BoxteamLiteLLMChatModel",
                    "data": {"chunk": create_chunk("主模型成功")},
                    "metadata": {},
                }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
        )

        assert result == "主模型成功"
        assert mock_build.call_count == 1


@pytest.mark.asyncio
async def test_reasoning_stream_not_mixed_into_final_text(mock_dependencies):
    """reasoning 流应只作为 reasoning 事件，不应污染正式回复。"""
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value = {
        "providers": [
            {
                "interface": "chat.completion",
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
    service = AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
    )

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": create_chunk(
                        [{"type": "reasoning", "reasoning": "先判断用户只要 OK。"}],
                    )
                },
                "metadata": {},
            }
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {"chunk": create_chunk([{"type": "text", "text": "OK"}])},
                "metadata": {},
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
        )

    assert result == "OK"
    text_delta_payloads = [
        call.kwargs["payload"]
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") == "text_delta"
    ]
    assert text_delta_payloads == [
        {"text": "先判断用户只要 OK。", "kind": "reasoning"},
        {"text": "OK", "kind": "text"},
    ]


@pytest.mark.asyncio
async def test_standard_content_blocks_stream_split_reasoning_and_text(mock_dependencies):
    """标准 content blocks 流应拆分为 reasoning/text 两种事件。"""
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value = {
        "providers": [
            {
                "interface": "chat.completion",
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
    service = AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
    )

    content_blocks = [
        {"type": "reasoning", "reasoning": "先判断用户只要 OK。"},
        {"type": "text", "text": "OK"},
    ]

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {"chunk": create_chunk(content_blocks)},
                "metadata": {},
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
        )

    assert result == "OK"
    text_delta_payloads = [
        call.kwargs["payload"]
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") == "text_delta"
    ]
    assert text_delta_payloads == [
        {"text": "先判断用户只要 OK。", "kind": "reasoning"},
        {"text": "OK", "kind": "text"},
    ]
    assert all("type" not in payload["text"] for payload in text_delta_payloads)


@pytest.mark.asyncio
async def test_tool_events_use_tool_start_input_and_tool_message_content(mock_dependencies):
    """工具卡片应使用 on_tool_start 的完整参数和 ToolMessage.content。"""
    deps = mock_dependencies
    deps["config_service"].get_agent_runtime_config.return_value = {
        "providers": [
            {
                "interface": "chat.completion",
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
    service = AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
    )

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        async def mock_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {
                    "chunk": create_chunk(
                        "",
                        tool_calls=[
                            {
                                "id": "call_1",
                                "name": "python_exec",
                                "args": {},
                            }
                        ],
                    )
                },
                "metadata": {},
            }
            yield {
                "event": "on_tool_start",
                "name": "python_exec",
                "data": {"input": {"code": "print('LC_BLOCK_OK_2')"}},
                "metadata": {},
            }
            yield {
                "event": "on_tool_end",
                "name": "python_exec",
                "data": {
                    "output": ToolMessage(
                        content='{"stdout":"LC_BLOCK_OK_2\\n"}',
                        tool_call_id="call_1",
                        name="python_exec",
                    )
                },
                "metadata": {},
            }
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {"chunk": create_chunk("完成")},
                "metadata": {},
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_build.return_value = mock_agent

        result = await service.run_step(
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
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
            "tool_name": "python_exec",
            "args": {"code": "print('LC_BLOCK_OK_2')"},
            "agent_id": "test_agent",
        }
    ]
    assert tool_end_payloads == [
        {
            "tool_name": "python_exec",
            "result": '{"stdout":"LC_BLOCK_OK_2\\n"}',
            "agent_id": "test_agent",
        }
    ]
    assert "ToolMessage" not in tool_end_payloads[0]["result"]
    assert "content=" not in tool_end_payloads[0]["result"]


def test_extract_final_text_uses_visible_text_from_standard_blocks(mock_dependencies):
    """从最终消息提取正文时不能把 reasoning block 拼进回复。"""
    deps = mock_dependencies
    service = AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
    )
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
async def test_fallback_on_primary_failure(mock_dependencies):
    """测试：主模型失败时，回退到 fallback 模型成功。"""
    deps = mock_dependencies
    service = AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
    )

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        with patch("app.runtime.agent_runtime.build_session_agent_runtime", mock_build):
            call_count = 0

            def create_mock_agent(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                mock_agent = MagicMock()

                if call_count == 1:
                    async def mock_events(*args, **kwargs):
                        raise Exception("主模型失败")
                        yield  # 使其成为异步生成器（虽然永远不会执行到这里）
                    mock_agent.astream_events = mock_events
                else:
                    async def mock_events(*args, **kwargs):
                        yield {
                            "event": "on_chat_model_stream",
                            "name": "ChatOpenAI",
                            "data": {"chunk": create_chunk("fallback 成功")},
                            "metadata": {},
                        }
                    mock_agent.astream_events = mock_events

                return mock_agent

            mock_build.side_effect = create_mock_agent

            result = await service.run_step(
                session_id="test",
                message="test",
                agent_id="test_agent",
                job_id="job_test",
            )

            assert result == "fallback 成功"
            assert call_count == 2


@pytest.mark.asyncio
async def test_all_models_fail(mock_dependencies):
    """测试：所有模型都失败时，抛出异常。"""
    deps = mock_dependencies
    service = AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
    )

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
            )


@pytest.mark.asyncio
async def test_fallback_publishes_event(mock_dependencies):
    """测试：fallback 时发布 AGENT_START 事件。"""
    deps = mock_dependencies
    service = AgentExecutionService(
        config_service=deps["config_service"],
        background_task_registry=deps["registry"],
        background_message_bus=deps["msg_bus"],
        job_event_bus=deps["job_event_bus"],
        dependency_provider=deps["dependency_provider"],
    )

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        with patch("app.runtime.agent_runtime.build_session_agent_runtime", mock_build):
            call_count = 0

            def create_mock_agent(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                mock_agent = MagicMock()

                if call_count == 1:
                    async def mock_events(*args, **kwargs):
                        raise Exception("主模型失败")
                        yield  # 使其成为异步生成器（虽然永远不会执行到这里）
                    mock_agent.astream_events = mock_events
                else:
                    async def mock_events(*args, **kwargs):
                        yield {
                            "event": "on_chat_model_stream",
                            "name": "ChatOpenAI",
                            "data": {"chunk": create_chunk("fallback")},
                            "metadata": {},
                        }
                    mock_agent.astream_events = mock_events

                return mock_agent

            mock_build.side_effect = create_mock_agent

            await service.run_step(
                session_id="test",
                message="test",
                agent_id="test_agent",
                job_id="job_test",
            )

    # 验证发布过 fallback 事件（至少2次 AGENT_START：主模型启动 + fallback 启动）
    publish_calls = [
        c
        for c in deps["job_event_bus"].publish.call_args_list
        if c.kwargs.get("event_type") == "agent_start"
    ]
    assert len(publish_calls) >= 2
