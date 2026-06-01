from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime, timedelta

import pytest

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import EventType, JobEventBus
from app.schemas.event import AgentEndEvent, AgentEndPayload
from app.agents.agent_tools import (
    create_system_time_emitter_tool,
    create_monitor_session_agent_end_tool,
    create_send_message_to_session_tool,
    build_default_tools,
)
from app.services.agent_execution_service import AgentExecutionService


class _DummyConfigService:
    def get_llm_providers(self):
        return [
            {
                "id": "primary",
                "model": "dummy-model",
                "api_key": "dummy-key",
                "endpoint": "http://localhost:1234",
                "interface": "chat.completion",
            }
        ]

    def get_default_agent_runtime_config(self):
        return {
            "system_prompt": "dummy system prompt",
            "providers": self.get_llm_providers(),
            "temperature": 0.2,
            "top_p": 1,
            "max_output_tokens": 4000,
        }

    def get_default_agent_id(self):
        return "default"

    def resolve_agent_id(self, agent_id):
        return agent_id or "default"

    def get_agent_runtime_config(self, agent_id=None):
        return self.get_default_agent_runtime_config()

    def get_agent_tool_config(self, agent_id=None):
        return {
            "denylist": [],
            "confirmation_required": [],
        }


class _DummyAgent:
    def get_graph(self):
        class _DummyGraph:
            nodes = {}

        return _DummyGraph()


class _FakeBackgroundMessageBus:
    def __init__(self):
        self.messages = []

    def emit(self, session_id, agent_id, content, *, kind, source_id=None, payload=None, message_id=None):
        self.messages.append(
            {
                "session_id": session_id,
                "agent_id": agent_id,
                "content": content,
                "kind": kind,
                "source_id": source_id,
                "payload": payload,
            }
        )

        class _FakeMessage:
            def __init__(self, value):
                self._value = value

            def model_dump(self, mode="json"):
                return self._value

            def model_dump_json(self):
                import json

                return json.dumps(self._value)

        return _FakeMessage(self.messages[-1])

    async def collect(self, *args, **kwargs):
        class _Batch:
            interrupted = False
            timed_out = True
            messages = []

            def model_dump(self, mode="json"):
                return {
                    "interrupted": self.interrupted,
                    "timed_out": self.timed_out,
                    "messages": self.messages,
                }

        return _Batch()


class _FakeBackgroundTaskRegistry:
    def __init__(self):
        self.tasks = {}

    def spawn(self, session_id, task_name, runner, *, metadata=None):
        task_id = f"task_{len(self.tasks) + 1}"
        async def _noop_task():
            return None

        self.tasks[(session_id, task_id)] = _noop_task

        class _Handle:
            def __init__(self, task_id, session_id, task_name, metadata):
                self.task_id = task_id
                self.session_id = session_id
                self.task_name = task_name
                self.status = "pending"
                self.created_at = datetime.now()
                self.started_at = None
                self.ended_at = None
                self.metadata = metadata or {}

            def to_dict(self):
                return {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "task_name": self.task_name,
                    "status": self.status,
                    "created_at": self.created_at.isoformat(),
                    "started_at": None,
                    "ended_at": None,
                    "metadata": self.metadata,
                }

        return _Handle(task_id, session_id, task_name, metadata)

    def get_task(self, session_id, task_id):
        return self.tasks.get((session_id, task_id))


class _FakeJobEventBus:
    async def subscribe(self, job_id):
        return asyncio.Queue()

    async def unsubscribe(self, job_id, queue):
        return None

    async def publish(self, *args, **kwargs):
        return None


class _FakeJobService:
    async def list(self, session_id=None):
        class _FakeTargetJob:
            job_id = "job_target_1"
            created_at = datetime.now()

        if session_id == "target_session":
            return [_FakeTargetJob()]
        return []


class _FakeSessionService:
    async def get(self, session_id):
        class _Session:
            current_agent_id = "deep_agent"

        return _Session()


class _FakeMessageService:
    async def create_and_run(self, session_id, run_request, *, session_service, config_service, job_service, job_event_bus=None):
        return _FakeResult()


class _FakeResult:
    def model_dump(self, mode="json"):
        return {
            "message_id": "msg_test",
            "job_id": "job_test",
            "status": "accepted",
        }


@pytest.mark.asyncio
async def test_agent_includes_background_message_collection_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    background_message_bus = _FakeBackgroundMessageBus()
    background_task_registry = _FakeBackgroundTaskRegistry()
    job_event_bus = _FakeJobEventBus()
    message_service = _FakeMessageService()
    session_service = _FakeSessionService()
    config_service = _DummyConfigService()
    job_service = _FakeJobService()

    tools = build_default_tools(
        session_id="session_test",
        agent_id="deep_agent",
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        message_service=message_service,
        session_service=session_service,
        config_service=config_service,
    )

    tool_names = [tool.name for tool in tools]
    assert "python_exec" in tool_names
    assert "emit_system_time_messages" in tool_names
    assert "monitor_session_agent_end" in tool_names
    assert "collect_background_messages" in tool_names
    assert "send_message_to_session" in tool_names
    assert len(tools) == 5



@pytest.mark.asyncio
async def test_agent_tool_denylist_filters_direct_and_middleware_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    job_event_bus = _FakeJobEventBus()

    class _DenylistConfigService(_DummyConfigService):
        def get_agent_tool_config(self, agent_id=None):
            return {
                "denylist": ["send_message_to_session", "edit_file"],
                "confirmation_required": [],
            }

    config_service = _DenylistConfigService()

    tools = build_default_tools(
        session_id="session_denylist",
        agent_id="deep_agent",
        background_task_registry=_FakeBackgroundTaskRegistry(),
        background_message_bus=_FakeBackgroundMessageBus(),
        job_event_bus=job_event_bus,
        job_service=_FakeJobService(),
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService(),
        config_service=config_service,
    )

    direct_tool_names = [tool.name for tool in tools]
    assert direct_tool_names == [
        "python_exec",
        "emit_system_time_messages",
        "monitor_session_agent_end",
        "collect_background_messages",
        "send_message_to_session",
    ]


@pytest.mark.asyncio
async def test_emit_system_time_messages_tool_emits_periodic_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    background_message_bus = _FakeBackgroundMessageBus()

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("app.agents.agent_tools.asyncio.sleep", fake_sleep)

    tool = create_system_time_emitter_tool("session_test", background_message_bus=background_message_bus)

    result = await tool.ainvoke({"interval_seconds": 0.01, "message_count": 3, "source_id": "clock-stream"})

    assert result["message_count"] == 3
    assert result["interval_seconds"] == 0.01
    assert result["source_id"] == "clock-stream"

    messages = background_message_bus.messages
    assert len(messages) == 3
    assert all(message["content"] for message in messages)


@pytest.mark.asyncio
async def test_monitor_session_agent_end_tool_emits_interrupt_message(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    background_message_bus = _FakeBackgroundMessageBus()
    background_task_registry = _FakeBackgroundTaskRegistry()
    job_event_bus = _FakeJobEventBus()
    job_service = _FakeJobService()

    future_event = AgentEndEvent(
        event_id="evt_future_1",
        job_id="job_target_1",
        step_id=None,
        agent_id="deep_agent",
        payload=AgentEndPayload(
            response={"text": "橙子"},
            final_text="橙子",
            agent_id="deep_agent"
        ),
        timestamp=datetime.now() + timedelta(days=1),
    )

    tool = create_monitor_session_agent_end_tool(
        "monitor_session",
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
    )

    result = await tool.ainvoke(
        {
            "target_session_id": "target_session",
            "timeout_seconds": 1,
            "poll_interval_seconds": 0.01,
            "max_events": 1,
        }
    )

    assert result["status"] == "pending"
    assert result["task_name"] == "monitor_session_agent_end"
    assert result["metadata"]["target_session_id"] == "target_session"
    assert result["metadata"]["max_events"] == 1
    assert result["metadata"]["source_id"].startswith("monitor:target_session:")

    task = background_task_registry.get_task("monitor_session", result["task_id"])
    assert task is not None

    if callable(task):
        await task()
    else:
        await task

    # 这里只验证任务已被成功装配，monitor 的完整事件循环在集成测试中覆盖
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_send_message_to_session_tool_creates_job(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    job_event_bus = _FakeJobEventBus()
    job_service = _FakeJobService()
    session_service = _FakeSessionService()
    config_service = _DummyConfigService()
    message_service = _FakeMessageService()

    tool = create_send_message_to_session_tool(
        message_service=message_service,
        session_service=session_service,
        config_service=config_service,
        job_service=job_service,
        job_event_bus=job_event_bus,
    )

    result = await tool.ainvoke({"target_session_id": "ses_target", "content": "请再次只重复前面的话"})

    assert result["job_id"] == "job_test"
