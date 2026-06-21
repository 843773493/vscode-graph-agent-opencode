from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.orchestration.agent_execution_service import AgentExecutionService


@pytest.fixture
def mock_dependencies():
    """创建一组共用的 mock 依赖。"""
    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "test_agent"
    config_service.get_agent_runtime_config.return_value = {
        "providers": [
            {"interface": "openai", "model": "primary", "api_key": "k", "endpoint": "e", "temperature": 0.7, "top_p": 1.0, "max_output_tokens": 1024},
            {"interface": "openai", "model": "fallback", "api_key": "k", "endpoint": "e", "temperature": 0.7, "top_p": 1.0, "max_output_tokens": 1024},
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
    dependency_provider.get_system_reminder_trigger_registry.return_value = MagicMock()
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


def create_chunk(content: str = "", tool_calls=None, kind="text"):
    """创建模拟的 chunk 对象。"""
    chunk = MagicMock()
    chunk.content = content
    chunk.message = None
    chunk.tool_calls = tool_calls or []
    chunk.additional_kwargs = {"kind": kind}
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

    with patch("app.runtime.agent_runtime.build_session_agent_runtime") as mock_build:
        async def mock_events(*args, **kwargs):
            yield {"event": "on_chat_model_stream", "name": "ChatOpenAI", "data": {"chunk": create_chunk("主模型成功")}, "metadata": {}}

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

    with patch("app.runtime.agent_runtime.build_session_agent_runtime") as mock_build:
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
                    yield {"event": "on_chat_model_stream", "name": "ChatOpenAI", "data": {"chunk": create_chunk("fallback 成功")}, "metadata": {}}
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

    with patch("app.runtime.agent_runtime.build_session_agent_runtime") as mock_build:
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

    with patch("app.runtime.agent_runtime.build_session_agent_runtime") as mock_build:
        call_count = 0
        
        def create_mock_agent(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_agent = MagicMock()
            
            if call_count == 1:
                async def mock_events(*args, **kwargs):
                    raise Exception("主模型失败")
                mock_agent.astream_events = mock_events
            else:
                async def mock_events(*args, **kwargs):
                    yield {"event": "on_chat_model_stream", "name": "ChatOpenAI", "data": {"chunk": create_chunk("fallback")}, "metadata": {}}
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
    publish_calls = [c for c in deps["job_event_bus"].publish.call_args_list if c.kwargs.get("event_type") == "AGENT_START"]
    assert len(publish_calls) >= 2
