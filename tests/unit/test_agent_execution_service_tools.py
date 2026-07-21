from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import EventType
from app.schemas.event import AgentEndEvent, AgentEndPayload
from app.schemas.public_v2.job import JobDispatchSnapshotDTO
from app.agents.agent_tools import (
    create_background_message_collection_tool,
    create_system_time_emitter_tool,
    create_monitor_session_agent_end_tool,
    create_send_message_to_session_tool,
    build_default_tools,
)
from app.services.infrastructure.background_task_history_store import (
    BackgroundTaskHistoryStore,
)
from app.runtime.agent_runtime import build_agent_tool_definitions
from app.agents.tool_invocation_context import ToolInvocationContext


class _DummyConfigService:
    def get_llm_providers(self):
        return [
            {
                "id": "primary",
                "model": "dummy-model",
                "api_key": "dummy-key",
                "endpoint": "http://localhost:1234",
                "custom_llm_provider": "openai",
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
                self.status = "running"
                self.created_at = datetime.now()
                self.started_at = self.created_at
                self.ended_at = None
                self.metadata = metadata or {}

            def to_dict(self):
                return {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "task_name": self.task_name,
                    "status": self.status,
                    "created_at": self.created_at.isoformat(),
                    "started_at": self.started_at.isoformat(),
                    "ended_at": None,
                    "metadata": self.metadata,
                }

        return _Handle(task_id, session_id, task_name, metadata)

    def get_task(self, session_id, task_id):
        return self.tasks.get((session_id, task_id))


class _FakeJobEventBus:
    def __init__(self):
        self.queues = {}
        self.subscribed = asyncio.Event()
        self.subscription_event_types = {}

    async def subscribe(self, job_id, *, subscriber_kind, metadata=None, event_types=None):
        queue = asyncio.Queue()
        self.queues[job_id] = queue
        self.subscription_event_types[job_id] = event_types
        self.subscribed.set()
        return queue

    async def unsubscribe(self, job_id, queue, *, reason):
        if self.queues.get(job_id) is queue:
            del self.queues[job_id]
            self.subscription_event_types.pop(job_id, None)
        return None

    async def publish(self, *args, **kwargs):
        return None

    async def emit(self, job_id, event):
        await self.queues[job_id].put(event)


class _FakeJobService:
    async def list(self, session_id=None):
        class _FakeTargetJob:
            job_id = "job_target_1"
            created_at = datetime.now()

        if session_id == "target_session":
            return [_FakeTargetJob()]
        return []


class _FakeSessionService:
    def __init__(self, *, kind: str = "normal") -> None:
        self.kind = kind

    async def get(self, session_id):
        kind = self.kind

        class _Session:
            current_agent_id = "deep_agent"

        session = _Session()
        session.kind = kind
        session.delegation = object() if kind == "delegated" else None
        return session


class _FakeMessageService:
    async def create_and_run(self, session_id, run_request, *, session_service, config_service, job_service, job_event_bus=None):
        return _FakeResult()


class _FakeSessionOrchestrator:
    def __init__(self, result=None) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result or _FakeResult()

    async def create_and_run(
        self,
        session_id: str,
        content: str,
        **kwargs,
    ):
        self.calls.append(
            {
                "session_id": session_id,
                "content": content,
                **kwargs,
            }
        )
        return self.result


class _FakeSessionSubagentService:
    async def delegate(self, **kwargs):
        raise AssertionError(f"本测试不应执行 task 工具: {kwargs}")


class _FakeTeamService:
    pass


class _FakeTerminalManagerClient:
    pass


class _FakeResult:
    message_id = "msg_test"
    job_id = "job_test"
    status = "running"
    dispatch = JobDispatchSnapshotDTO(
        session_id="ses_target",
        job_id=job_id,
        job_status="running",
        active_job_id=job_id,
        queued_jobs_ahead=0,
        queued_job_count=0,
        pending_job_count=1,
    )

    def model_dump(self, mode="json"):
        return {
            "message_id": self.message_id,
            "job_id": self.job_id,
            "status": self.status,
            "dispatch": self.dispatch.model_dump(mode="json"),
        }


class _FakeQueuedResult:
    message_id = "msg_queued"
    job_id = "job_queued"
    status = "queued"
    dispatch = JobDispatchSnapshotDTO(
        session_id="ses_target",
        job_id=job_id,
        job_status="queued",
        active_job_id="job_running",
        blocked_by_job_id="job_running",
        queued_jobs_ahead=2,
        queued_job_count=3,
        pending_job_count=4,
    )


def test_tool_catalog_uses_model_visible_schema_without_runtime_fields():
    @tool
    def runtime_aware_tool(value: str, runtime: ToolRuntime) -> str:
        """返回输入值。"""
        assert runtime.tool_call_id
        return value

    tool_node = SimpleNamespace(
        data=SimpleNamespace(tools_by_name={runtime_aware_tool.name: runtime_aware_tool})
    )
    agent = SimpleNamespace(
        get_graph=lambda: SimpleNamespace(nodes={"tools": tool_node})
    )

    definitions = build_agent_tool_definitions(agent)

    assert len(definitions) == 1
    assert definitions[0]["name"] == "runtime_aware_tool"
    assert definitions[0]["parameters"]["properties"] == {
        "value": {"title": "Value", "type": "string"}
    }
    assert definitions[0]["parameters"]["required"] == ["value"]


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
        session_orchestrator=_FakeSessionOrchestrator(),
        session_subagent_service=_FakeSessionSubagentService(),
        team_service=_FakeTeamService(),
        config_service=config_service,
        terminal_manager_client=_FakeTerminalManagerClient(),
        invocation_context=ToolInvocationContext(),
        include_test_tools=True,
    )

    tool_names = [tool.name for tool in tools]
    assert "test_tool" in tool_names
    assert "apply_patch" in tool_names
    assert "python_exec" in tool_names
    assert "emit_system_time_messages" in tool_names
    assert "monitor_session_agent_end" in tool_names
    assert "collect_background_messages" in tool_names
    assert "persistent_terminal" in tool_names
    assert "send_message_to_session" in tool_names
    assert "task" in tool_names
    assert "create_team" in tool_names
    assert "attach_team_session" in tool_names
    assert "assign_team_task" in tool_names
    assert "update_team_task" in tool_names
    assert len(tools) == 16


@pytest.mark.asyncio
async def test_agent_omits_test_tool_without_development_config(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    tools = build_default_tools(
        session_id="session_production",
        background_task_registry=_FakeBackgroundTaskRegistry(),
        background_message_bus=_FakeBackgroundMessageBus(),
        job_event_bus=_FakeJobEventBus(),
        job_service=_FakeJobService(),
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService(),
        session_orchestrator=_FakeSessionOrchestrator(),
        session_subagent_service=_FakeSessionSubagentService(),
        team_service=_FakeTeamService(),
        config_service=_DummyConfigService(),
        terminal_manager_client=_FakeTerminalManagerClient(),
        invocation_context=ToolInvocationContext(),
    )

    assert "test_tool" not in {tool.name for tool in tools}



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
        session_orchestrator=_FakeSessionOrchestrator(),
        session_subagent_service=_FakeSessionSubagentService(),
        team_service=_FakeTeamService(),
        config_service=config_service,
        terminal_manager_client=_FakeTerminalManagerClient(),
        invocation_context=ToolInvocationContext(),
        include_test_tools=True,
    )

    direct_tool_names = [tool.name for tool in tools]
    assert direct_tool_names == [
        "test_tool",
        "apply_patch",
        "python_exec",
        "emit_system_time_messages",
        "collect_background_messages",
        "persistent_terminal",
        "monitor_session_agent_end",
        "send_message_to_session",
        "task",
        "create_team",
        "list_my_teams",
        "get_team_board",
        "create_team_member",
        "attach_team_session",
        "assign_team_task",
        "update_team_task",
    ]


@pytest.mark.asyncio
async def test_emit_system_time_messages_tool_emits_periodic_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    background_message_bus = _FakeBackgroundMessageBus()
    background_task_registry = BackgroundTaskRegistry(
        history_store=BackgroundTaskHistoryStore(sessions_dir=tmp_path / ".boxteam")
    )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("app.agents.tools.background.asyncio.sleep", fake_sleep)

    tool = create_system_time_emitter_tool(
        "session_test",
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
    )

    result = await tool.ainvoke({"interval_seconds": 0.01, "message_count": 3, "source_id": "clock-stream"})

    assert result["task_name"] == "emit_system_time_messages"
    task = background_task_registry.get_task("session_test", result["task_id"])
    assert task is not None
    await task

    handle = background_task_registry.get_handle("session_test", result["task_id"])
    assert handle is not None
    assert handle.status == "completed"
    assert handle.metadata["message_count"] == 3
    assert handle.metadata["source_id"] == "clock-stream"
    assert isinstance(handle.metadata["result"], dict)

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

    tool = create_monitor_session_agent_end_tool(
        "monitor_session",
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        session_service=_FakeSessionService(),
    )

    result = await tool.ainvoke(
        {
            "target_session_id": "target_session",
            "timeout_seconds": 1,
            "poll_interval_seconds": 0.01,
            "max_events": 1,
        }
    )

    assert result["status"] == "running"
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
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_monitor_session_agent_end_accepts_zero_as_unlimited(tmp_path):
    tool = create_monitor_session_agent_end_tool(
        "monitor_session",
        background_task_registry=_FakeBackgroundTaskRegistry(),
        background_message_bus=_FakeBackgroundMessageBus(),
        job_event_bus=_FakeJobEventBus(),
        job_service=_FakeJobService(),
        session_service=_FakeSessionService(),
    )

    result = await tool.ainvoke(
        {
            "target_session_id": "target_session",
            "timeout_seconds": 0,
            "max_events": 0,
        }
    )

    assert result["metadata"]["timeout_seconds"] is None
    assert result["metadata"]["max_events"] is None


@pytest.mark.asyncio
async def test_monitor_and_collect_forward_agent_end_final_text(tmp_path):
    background_message_bus = BackgroundMessageBus()
    background_task_registry = BackgroundTaskRegistry(
        history_store=BackgroundTaskHistoryStore(sessions_dir=tmp_path / ".boxteam")
    )
    job_event_bus = _FakeJobEventBus()
    job_service = _FakeJobService()
    monitor_tool = create_monitor_session_agent_end_tool(
        "monitor_session",
        agent_id="deep_agent",
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        session_service=_FakeSessionService(),
    )
    collect_tool = create_background_message_collection_tool(
        "monitor_session",
        agent_id="deep_agent",
        background_message_bus=background_message_bus,
    )

    monitor_result = await monitor_tool.ainvoke(
        {
            "target_session_id": "target_session",
            "timeout_seconds": 1,
            "poll_interval_seconds": 0.01,
            "max_events": 1,
        }
    )
    await asyncio.wait_for(job_event_bus.subscribed.wait(), timeout=1)
    assert job_event_bus.subscription_event_types["job_target_1"] == frozenset({EventType.AGENT_END})

    collect_task = asyncio.create_task(
        collect_tool.ainvoke(
            {
                "source_id": monitor_result["metadata"]["source_id"],
                "timeout_seconds": 1,
            }
        )
    )
    await job_event_bus.emit(
        "job_target_1",
        AgentEndEvent(
            event_id="evt_target_end",
            job_id="job_target_1",
            step_id=None,
            agent_id="deep_agent",
            payload=AgentEndPayload(
                response={"text": "答案是 56088"},
                final_text="答案是 56088",
                agent_id="deep_agent",
            ),
            timestamp=datetime.now().astimezone() + timedelta(seconds=1),
        ),
    )

    collected = await asyncio.wait_for(collect_task, timeout=1)
    monitor_task = background_task_registry.get_task(
        "monitor_session",
        monitor_result["task_id"],
    )
    assert monitor_task is not None
    await asyncio.wait_for(monitor_task, timeout=1)

    assert collected["interrupted"] is True
    assert collected["timed_out"] is False
    assert [message["content"] for message in collected["messages"]] == [
        "答案是 56088"
    ]


@pytest.mark.asyncio
async def test_monitor_rejects_delegated_session_final_text_forwarding():
    tool = create_monitor_session_agent_end_tool(
        "monitor_session",
        background_task_registry=_FakeBackgroundTaskRegistry(),
        background_message_bus=_FakeBackgroundMessageBus(),
        job_event_bus=_FakeJobEventBus(),
        job_service=_FakeJobService(),
        session_service=_FakeSessionService(kind="delegated"),
    )

    with pytest.raises(ValueError, match="必须通过 send_message_to_session"):
        await tool.ainvoke({"target_session_id": "target_session"})


@pytest.mark.asyncio
async def test_send_message_to_session_defaults_to_trusted_reminder_sender(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    orchestrator = _FakeSessionOrchestrator()

    tool = create_send_message_to_session_tool(
        sender_session_id="ses_sender",
        sender_agent_id="deep_agent",
        session_orchestrator=orchestrator,
    )

    result = await tool.ainvoke({"target_session_id": "ses_target", "content": "请再次只重复前面的话"})

    assert result["job_id"] == "job_test"
    assert result["message_id"] == "msg_test"
    assert result["status"] == "running"
    assert result["target_session_state"] == {
        "session_id": "ses_target",
        "job_id": "job_test",
        "job_status": "running",
        "active_job_id": "job_test",
        "blocked_by_job_id": None,
        "queued_jobs_ahead": 0,
        "queued_job_count": 0,
        "pending_job_count": 1,
    }
    assert result["simulate_user"] is False
    assert result["sender_session_id"] == "ses_sender"
    assert result["kind"] == "result"
    assert result["reply_required"] is False
    assert result["communication_id"].startswith("comm_")
    schema = tool.args_schema.model_json_schema()
    assert "role" not in schema["properties"]
    simulate_user_schema = schema["properties"]["simulate_user"]
    assert simulate_user_schema["default"] is False
    assert simulate_user_schema["type"] == "boolean"
    submitted_content = orchestrator.calls[0]["content"]
    assert isinstance(submitted_content, str)
    assert submitted_content.startswith("<system_reminder>\n")
    assert submitted_content.endswith("\n</system_reminder>")
    assert '"sender_session_id": "ses_sender"' in submitted_content
    assert '"sender_agent_id": "deep_agent"' in submitted_content
    assert '"target_session_id": "ses_target"' in submitted_content
    assert '"message": "请再次只重复前面的话"' in submitted_content
    assert "message_role" not in orchestrator.calls[0]
    metadata = orchestrator.calls[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["source"] == "send_message_to_session"
    assert metadata["simulate_user"] is False
    assert metadata["communication_id"] == result["communication_id"]
    assert metadata["kind"] == "result"
    assert metadata["reply_required"] is False


@pytest.mark.asyncio
async def test_send_message_to_session_question_requires_directional_reply():
    orchestrator = _FakeSessionOrchestrator()
    tool = create_send_message_to_session_tool(
        sender_session_id="ses_sender",
        session_orchestrator=orchestrator,
    )

    result = await tool.ainvoke(
        {
            "target_session_id": "ses_target",
            "content": "你确认这个结论吗？",
            "kind": "question",
        }
    )

    assert result["kind"] == "question"
    assert result["reply_required"] is True
    metadata = orchestrator.calls[0]["metadata"]
    assert metadata["communication_id"] == result["communication_id"]
    assert metadata["reply_required"] is True


@pytest.mark.asyncio
async def test_send_message_to_session_returns_atomic_target_queue_snapshot():
    tool = create_send_message_to_session_tool(
        sender_session_id="ses_sender",
        session_orchestrator=_FakeSessionOrchestrator(_FakeQueuedResult()),
    )

    result = await tool.ainvoke(
        {
            "target_session_id": "ses_target",
            "content": "排队处理",
        }
    )

    assert result["status"] == "queued"
    assert result["target_session_state"] == {
        "session_id": "ses_target",
        "job_id": "job_queued",
        "job_status": "queued",
        "active_job_id": "job_running",
        "blocked_by_job_id": "job_running",
        "queued_jobs_ahead": 2,
        "queued_job_count": 3,
        "pending_job_count": 4,
    }


@pytest.mark.asyncio
async def test_send_message_to_session_reply_requires_correlation_id():
    tool = create_send_message_to_session_tool(
        sender_session_id="ses_sender",
        session_orchestrator=_FakeSessionOrchestrator(),
    )

    with pytest.raises(ValueError, match="reply_to_communication_id"):
        await tool.ainvoke(
            {
                "target_session_id": "ses_target",
                "content": "确认",
                "kind": "reply",
            }
        )


@pytest.mark.asyncio
async def test_send_message_to_session_simulated_user_preserves_plain_content():
    orchestrator = _FakeSessionOrchestrator()
    tool = create_send_message_to_session_tool(
        sender_session_id="ses_sender",
        sender_agent_id="deep_agent",
        session_orchestrator=orchestrator,
    )

    result = await tool.ainvoke(
        {
            "target_session_id": "ses_target",
            "content": "普通用户消息",
            "simulate_user": True,
        }
    )

    assert result["simulate_user"] is True
    assert orchestrator.calls == [
        {
            "session_id": "ses_target",
            "content": "普通用户消息",
        }
    ]


@pytest.mark.asyncio
async def test_send_message_to_session_rejects_session_id_as_simulate_user():
    tool = create_send_message_to_session_tool(
        sender_session_id="ses_sender",
        session_orchestrator=_FakeSessionOrchestrator(),
    )

    with pytest.raises(ValueError, match="boolean"):
        await tool.ainvoke(
            {
                "target_session_id": "ses_target",
                "content": "不应发送",
                "simulate_user": "ses_sender",
            }
        )
