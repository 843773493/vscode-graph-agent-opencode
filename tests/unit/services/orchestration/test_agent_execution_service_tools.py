from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.agents.agent_tools import (
    build_default_tools,
    create_system_time_emitter_tool,
)
from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.core.background_message_bus import BackgroundMessageBus
from app.core.identifier import create_uuid_hex
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.runtime.agent_runtime import build_agent_tool_definitions
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from tests.support.background_work import build_background_registry

pytestmark = pytest.mark.usefixtures("workspace_scope")

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


class _BlueprintBackgroundService:
    """只用于工具定义装配；执行测试使用正式 mailbox/registry。"""


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

    async def publish(self, *args, **kwargs):
        return None

    async def emit(self, job_id, event):
        await self.queues[job_id].put(event)


class _FakeJobService:
    async def list(self, session_id=None):
        class _FakeTargetJob:
            job_id = "job_target_1"
            created_at = datetime.now(UTC)

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

    async def create_and_run_internal(
        self,
        session_id: str,
        message,
        **kwargs,
    ):
        return await self.create_and_run(
            session_id,
            message.content,
            metadata=message.metadata,
            **kwargs,
        )




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
        thread_id="thr_01a100c4a1897a2d8d40b568d23fcbe6",
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


def test_mcp_catalog_definition_uses_extension_group_metadata():
    @tool
    def mcp_status(value: str) -> str:
        """查询 MCP 状态。"""
        return value

    mcp_status = mcp_status.model_copy(
        update={"metadata": {"mcp_server_id": "tui-mcp"}}
    )

    @tool
    def read_file(value: str) -> str:
        """读取文件。"""
        return value

    tool_node = SimpleNamespace(
        data=SimpleNamespace(tools_by_name={read_file.name: read_file})
    )
    agent = SimpleNamespace(
        get_graph=lambda: SimpleNamespace(nodes={"tools": tool_node})
    )

    definitions = build_agent_tool_definitions(agent, extension_tools=[mcp_status])
    mcp_definition = next(
        definition for definition in definitions if definition["name"] == "mcp_status"
    )

    assert mcp_definition["kind"] == "extension"
    assert mcp_definition["group_id"] == "mcp:tui-mcp"
    assert mcp_definition["group_name"] == "扩展工具 · MCP · tui-mcp"


@pytest.mark.asyncio
async def test_agent_includes_background_message_collection_tool(communication_probe, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    background_message_bus = _BlueprintBackgroundService()
    background_task_registry = _BlueprintBackgroundService()
    job_event_bus = _FakeJobEventBus()
    message_service = _FakeMessageService()
    session_service = _FakeSessionService()
    config_service = _DummyConfigService()
    job_service = _FakeJobService()

    tools = build_default_tools(session_communication_service=communication_probe,
        session_id="ses_019bca51019973818b63e9d44c0e94f5",
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
        terminal_resource_owner=_FakeTerminalManagerClient(),
        workspace_root=tmp_path,
        invocation_context=ToolInvocationContext(),
        include_test_tools=True,
        include_team_tools=True,
    )

    tool_names = [tool.name for tool in tools]
    assert "test_tool" in tool_names
    assert "apply_patch" in tool_names
    assert "python_exec" in tool_names
    assert "emit_system_time_messages" in tool_names
    assert "wait_for_session" in tool_names
    assert "collect_background_messages" in tool_names
    assert "exec_command" in tool_names
    assert "write_stdin" in tool_names
    assert "list_terminal_sessions" in tool_names
    assert "kill_terminal" in tool_names
    assert "send_message_to_session" in tool_names
    assert "task" in tool_names
    assert "create_team" in tool_names
    assert "attach_team_session" not in tool_names
    assert "assign_team_task" in tool_names
    assert "update_team_task" in tool_names
    assert len(tools) == 18


@pytest.mark.asyncio
async def test_agent_omits_test_tool_without_development_config(communication_probe, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    tools = build_default_tools(session_communication_service=communication_probe,
        session_id="session_production",
        background_task_registry=_BlueprintBackgroundService(),
        background_message_bus=_BlueprintBackgroundService(),
        job_event_bus=_FakeJobEventBus(),
        job_service=_FakeJobService(),
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService(),
        session_orchestrator=_FakeSessionOrchestrator(),
        session_subagent_service=_FakeSessionSubagentService(),
        team_service=_FakeTeamService(),
        config_service=_DummyConfigService(),
        terminal_resource_owner=_FakeTerminalManagerClient(),
        workspace_root=tmp_path,
        invocation_context=ToolInvocationContext(),
        include_team_tools=True,
    )

    assert "test_tool" not in {tool.name for tool in tools}


@pytest.mark.asyncio
async def test_single_agent_tool_set_omits_team_board_tools(communication_probe, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    tools = build_default_tools(session_communication_service=communication_probe,
        session_id="session_single_agent",
        background_task_registry=_BlueprintBackgroundService(),
        background_message_bus=_BlueprintBackgroundService(),
        job_event_bus=_FakeJobEventBus(),
        job_service=_FakeJobService(),
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService(),
        session_orchestrator=_FakeSessionOrchestrator(),
        session_subagent_service=_FakeSessionSubagentService(),
        config_service=_DummyConfigService(),
        terminal_resource_owner=_FakeTerminalManagerClient(),
        workspace_root=tmp_path,
        invocation_context=ToolInvocationContext(),
        include_team_tools=False,
    )

    tool_names = {tool.name for tool in tools}
    assert tool_names.isdisjoint(
        {
            "create_team",
            "list_my_teams",
            "get_team_board",
            "create_team_member",
                "assign_team_task",
            "update_team_task",
        }
    )
    assert {"send_message_to_session", "task"} <= tool_names



@pytest.mark.asyncio
async def test_agent_tool_denylist_filters_direct_and_middleware_tools(communication_probe, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    job_event_bus = _FakeJobEventBus()

    class _DenylistConfigService(_DummyConfigService):
        def get_agent_tool_config(self, agent_id=None):
            return {
                "denylist": ["send_message_to_session", "edit_file"],
                "confirmation_required": [],
            }

    config_service = _DenylistConfigService()

    tools = build_default_tools(session_communication_service=communication_probe,
        session_id="session_denylist",
        agent_id="deep_agent",
        background_task_registry=_BlueprintBackgroundService(),
        background_message_bus=_BlueprintBackgroundService(),
        job_event_bus=job_event_bus,
        job_service=_FakeJobService(),
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService(),
        session_orchestrator=_FakeSessionOrchestrator(),
        session_subagent_service=_FakeSessionSubagentService(),
        team_service=_FakeTeamService(),
        config_service=config_service,
        terminal_resource_owner=_FakeTerminalManagerClient(),
        workspace_root=tmp_path,
        invocation_context=ToolInvocationContext(),
        include_test_tools=True,
        include_team_tools=True,
    )

    direct_tool_names = [tool.name for tool in tools]
    assert direct_tool_names == [
        "test_tool",
        "apply_patch",
        "python_exec",
        "emit_system_time_messages",
        "collect_background_messages",
        "exec_command",
        "write_stdin",
        "list_terminal_sessions",
        "kill_terminal",
        "wait_for_session",
        "send_message_to_session",
        "task",
        "create_team",
        "list_my_teams",
        "get_team_board",
        "create_team_member",
        "assign_team_task",
        "update_team_task",
    ]


@pytest.mark.asyncio
async def test_emit_system_time_messages_tool_emits_periodic_messages(
    monkeypatch,
    tmp_path,
    session_bundle_factory,
):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, "ses_019bca51019973818b63e9d44c0e94f5")
    background_message_bus = BackgroundMessageBus(store=BackgroundWorkStore(sessions_dir=sessions_dir))
    background_task_registry = build_background_registry(sessions_dir)
    thread_id = get_session_path_resolver(sessions_dir).main_thread_id("ses_019bca51019973818b63e9d44c0e94f5")
    invocation = ToolInvocationContext(thread_binding=ThreadRuntimeBinding("ses_019bca51019973818b63e9d44c0e94f5", thread_id))
    invocation.set_tool_call_id("clock_call", projection_identity=ItemProjectionIdentity(
        projection_message_id="clock-assistant", model_call_id=str(UUID(hex=create_uuid_hex())),
    ))

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("app.services.infrastructure.background_work_handlers.asyncio.sleep", fake_sleep)

    tool = create_system_time_emitter_tool(
        "ses_019bca51019973818b63e9d44c0e94f5",
        background_task_registry=background_task_registry,
        invocation_context=invocation,
    )

    result = await tool.ainvoke({"interval_seconds": 0.01, "message_count": 3, "source_id": "clock-stream"})

    assert result["task_name"] == "emit_system_time_messages"
    task = background_task_registry.get_task("ses_019bca51019973818b63e9d44c0e94f5", result["task_id"])
    assert task is not None
    await task

    handle = background_task_registry.get_handle("ses_019bca51019973818b63e9d44c0e94f5", result["task_id"])
    assert handle is not None
    assert handle.status == "completed"
    assert handle.metadata["message_count"] == 3
    assert handle.metadata["source_id"] == "clock-stream"
    assert isinstance(handle.metadata["result"], dict)

    messages = await background_message_bus.list_messages("ses_019bca51019973818b63e9d44c0e94f5", thread_id, "default")
    assert len(messages) == 3
    assert all(message.content for message in messages)
