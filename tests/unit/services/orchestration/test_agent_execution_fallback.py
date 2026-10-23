from __future__ import annotations

import asyncio
import traceback
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.abstractions.tool_selection import ToolSelectionSnapshot
from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.agents.graph_binding import DEEP_AGENT_GRAPH_BINDING, GraphBindingStorePort
from app.agents.tools.testing import create_test_tool
from app.core.execution_admission_context import require_current_execution_admission
from app.core.model_delta_context import (
    AgentRunLifecycleCallbackHandler,
    get_current_model_delta_sink,
    model_tool_invocation_scope,
)
from app.core.path_utils import get_session_catalog_components
from app.core.session_interrupt_state import SessionInterruptState
from app.core.turn_execution_scope import ScopeCancelledError
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    UserExecutionIdentity,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.records import CanonicalItemRecord
from app.services.business.resource_derivations.agents import (
    WORKSPACE_AGENTS_RESOURCE_ID,
)
from app.services.infrastructure.config.snapshot import build_config_snapshot
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
    McpCatalogResourcePublisher,
)
from app.services.infrastructure.mcp.catalog_owner import (
    McpCatalogOwner,
    McpCatalogPublication,
)
from app.services.infrastructure.mcp.extension_catalog import (
    build_extension_catalog_binding,
)
from app.services.infrastructure.message_stream_store import MessageStreamTerminalError
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.orchestration.agent_execution_service import (
    AgentExecutionService,
)
from app.services.orchestration.event_stream.contracts import (
    AgentEventStreamResult,
    SuccessfulToolCall,
)
from app.services.orchestration.event_stream.identity import (
    STREAM_JOB_ID_METADATA_KEY,
    STREAM_SESSION_ID_METADATA_KEY,
)
from app.services.orchestration.execution_step.reminders import (
    has_valid_delegated_report as _has_valid_delegated_report,
)
from app.services.orchestration.execution_step.reminders import (
    has_valid_session_question_reply as _has_valid_session_question_reply,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from configs.runtime import read_jsonc_object
from tests.support.communication import SOURCE_SESSION, SOURCE_THREAD, WORKSPACE_ID
from tests.support.mcp_session_doubles import (
    FakeMcpSessionFactory,
    drain_pending_relists,
)

pytestmark = pytest.mark.usefixtures("workspace_scope")

_SKILL_CATALOG_RESOURCE_ID = "skill-entry:workspace:catalog:catalog"


class _AcceptedUserInputStore:
    """按真实 owner/job/message 身份提供已接受输入和 admission。"""

    def __init__(self) -> None:
        self._messages: dict[tuple[str, str, str, str], HumanMessage] = {}
        self._bindings: dict[tuple[str, str, str], UserExecutionIdentity] = {}

    def accept(
        self,
        *,
        session_id: str,
        thread_id: str,
        job_id: str,
        message_id: str,
        message: str,
        message_created_at: str,
        message_metadata: dict[str, object] | None,
        execution_binding: UserExecutionIdentity,
    ) -> None:
        if execution_binding.job_id != job_id:
            raise ValueError("已接受输入的 execution binding 与 Job 不一致")
        owner_job = (session_id, thread_id, job_id)
        key = (*owner_job, message_id)
        if owner_job in self._bindings or key in self._messages:
            raise ValueError(f"重复登记已接受输入: {owner_job}/{message_id}")
        self._bindings[owner_job] = execution_binding
        self._messages[key] = HumanMessage(
            id=message_id,
            content=message,
            response_metadata={
                "message_id": message_id,
                "created_at": message_created_at,
                "updated_at": message_created_at,
                "message_metadata": dict(message_metadata or {}),
            },
        )

    def read_user_message(
        self,
        session_id: str,
        *,
        thread_id: str,
        job_id: str,
        message_id: str,
        checkpoint_ns: str = "",
    ) -> HumanMessage:
        if checkpoint_ns:
            raise KeyError(f"测试已接受输入没有该 checkpoint namespace: {checkpoint_ns}")
        key = (session_id, thread_id, job_id, message_id)
        try:
            message = self._messages[key]
        except KeyError as error:
            raise KeyError(f"没有精确匹配的已接受用户输入: {key}") from error
        if message.id != message_id:
            raise ValueError(f"已接受用户输入 message identity 不一致: {key}")
        return message.model_copy(deep=True)

    def read_admission(
        self,
        session_id: str,
        *,
        thread_id: str,
        job_id: str,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        if checkpoint_ns:
            raise KeyError(f"测试 admission 没有该 checkpoint namespace: {checkpoint_ns}")
        key = (session_id, thread_id, job_id)
        try:
            binding = self._bindings[key]
        except KeyError as error:
            raise KeyError(f"没有精确匹配的已接受 execution binding: {key}") from error
        return ExecutionAdmission(
            session_id=session_id,
            thread_id=thread_id,
            execution_id="execution_test",
            job_id=job_id,
            admission_idempotency_key=binding.admission_idempotency_key,
            execution_origin="user_turn",
            turn_id=binding.turn_id,
        )


class _ActivationBodyStore:
    """只接受测试真实发布的 catalog、空 AGENTS chain 正文和精确 owner 引用。"""

    _EXPECTED: ClassVar[dict[str, tuple[int, object]]] = {
        MCP_TOOL_CATALOG_RESOURCE_ID: (0, {"content": ""}),
        _SKILL_CATALOG_RESOURCE_ID: (1, {"entries": []}),
        WORKSPACE_AGENTS_RESOURCE_ID: (2, {"content": ""}),
    }

    def __init__(self, *, workspace_id: str) -> None:
        self._expected = {
            **self._EXPECTED,
            f"workspace-agents:raw-file:workspace-agents-source:{workspace_id}": (
                3, {"content": ""},
            ),
        }
        self.written: list[tuple[str, int, object, DetailRef]] = []
        self._issued: set[tuple[str, str]] = set()

    def write_resource_body(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        resource_id: str,
        activation_ordinal: int,
        payload: object,
        checkpoint_ns: str,
    ) -> DetailRef:
        expected = self._expected.get(resource_id)
        if expected is None:
            raise AssertionError(f"activation 发布了未登记资源: {resource_id}")
        if (activation_ordinal, payload) != expected:
            raise AssertionError(
                f"activation body 与真实 {resource_id} 发布不一致: "
                f"ordinal={activation_ordinal} payload={payload!r}"
            )
        if not all((owner_session_id, owner_thread_id, activation_snapshot_id)):
            raise AssertionError("activation body 缺少完整 session/thread/snapshot owner")
        if checkpoint_ns != "":
            raise AssertionError(f"activation body checkpoint namespace 不匹配: {checkpoint_ns!r}")
        identity = (activation_snapshot_id, resource_id)
        if identity in self._issued:
            raise AssertionError(f"activation body identity 重复: {identity}")
        detail_id = f"resource-body-{activation_ordinal}-{resource_id.replace(':', '-') }"
        reference = DetailRef(
            owner_session_id, owner_thread_id, activation_snapshot_id, detail_id,
        )
        reference.require_owner(owner_session_id, owner_thread_id, activation_snapshot_id)
        self._issued.add(identity)
        self.written.append((resource_id, activation_ordinal, payload, reference))
        return reference


@pytest.fixture
def agent_execution_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    workspace_scope: tuple[str, Path],
):
    """创建一组共用的 mock 依赖。"""
    config_service = MagicMock()
    config_service.get_snapshot.return_value = build_config_snapshot(
        {"context": read_jsonc_object(Path.cwd() / "configs/workspace_inline.jsonc")["context"]},
        source_paths=(),
    )
    config_service.get_revision.return_value = config_service.get_snapshot.return_value.revision
    config_service.use_snapshot.side_effect = lambda _snapshot: nullcontext()
    config_service.resolve_agent_id.return_value = "test_agent"
    config_service.get_agent_runtime_config.return_value = {
        "providers": [
            {
                "custom_llm_provider": "openai",
                "model": "primary",
                "api_key": "k",
                "endpoint": "e",
                "temperature": 0.7,
                "top_p": 1.0,
                "max_output_tokens": 1024,
            },
            {
                "custom_llm_provider": "openai",
                "model": "fallback",
                "api_key": "k",
                "endpoint": "e",
                "temperature": 0.7,
                "top_p": 1.0,
                "max_output_tokens": 1024,
            },
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
    tool_selection_store.snapshot.side_effect = lambda agent_id: ToolSelectionSnapshot(
        agent_id, {}, {},
    )

    dependency_provider = MagicMock()
    dependency_provider.get_message_service.return_value = MagicMock()
    dependency_provider.get_session_service.return_value = MagicMock()
    dependency_provider.get_session_orchestrator.return_value = MagicMock()
    accepted_inputs = _AcceptedUserInputStore()
    # 此处只测编排，Saver 端口和 checkpoint 业务写入由显式 fake 替代。
    # 生产依赖始终包含 checkpointer，不能用 None 隐式关闭整个 v2 生命周期。
    saver = MagicMock(
        spec=[
            "append_items",
            "commit_tool_result",
            "register_model_call",
            "consume_prepared_context_for_dispatch",
            "update_model_call_outcome",
            "execution_for_turn",
            "execution_admission_for_job",
            "get_canonical_item",
            "converge_execution",
            "mark_execution_lost",
            "accepted_user_message",
            "context_owner_namespace",
            "frozen_resource_activation_for_admission",
            "find_resource_activation_snapshot",
            "save_resource_activation_snapshot",
            "register_execution_retry_source",
        ]
    )
    saver.accepted_user_message.side_effect = accepted_inputs.read_user_message
    saver.execution_admission_for_job.side_effect = accepted_inputs.read_admission
    saver.context_owner_namespace.side_effect = lambda checkpoint_ns: checkpoint_ns
    saver.frozen_resource_activation_for_admission.return_value = None
    saver.find_resource_activation_snapshot.return_value = None
    saver.save_resource_activation_snapshot = AsyncMock()
    def activation_factory(resource_registry):
        publisher = McpCatalogResourcePublisher(registry=resource_registry)
        publisher.publish(McpCatalogPublication(
            binding_ref=build_extension_catalog_binding(
                catalog_revision=sha256_jcs([]), generation=1, targets=(),
            ),
            descriptors=(), tools={}, status="available",
        ))
        skill_catalog_payload = {"entries": []}
        resource_registry.publish(ResourceSnapshot(
            resource_id=_SKILL_CATALOG_RESOURCE_ID,
            resource_kind="skill",
            facet="catalog",
            display_uri=None,
            revision=sha256_jcs(skill_catalog_payload),
            payload=skill_catalog_payload,
            source_lineage=(("skill-source:workspace:catalog", "sha256:fixture-empty"),),
            generation=0,
        ))
        return ResourceActivationCoordinator(
            registry=resource_registry, saver=saver, body_store=activation_body_store,
        )

    saver.consume_prepared_context_for_dispatch.return_value = {
        "assembly_id": "assembly_test",
        "execution_id": "execution_test",
    }
    default_prepared = saver.consume_prepared_context_for_dispatch.return_value

    def consume_prepared(session_id, thread_id, *, execution_id, model_call_id, checkpoint_ns):
        prepared = saver.consume_prepared_context_for_dispatch.return_value
        if prepared is not default_prepared:
            return prepared
        admission = require_current_execution_admission(langgraph_thread_id=thread_id)
        return {**prepared, "dispatch_ref": MagicMock(
            spec=SealedContextDispatchRef, session_id=session_id, thread_id=thread_id,
            execution_id=execution_id, turn_id=admission.turn_id,
            assembly_id=prepared["assembly_id"], callback_model_call_id=model_call_id,
        )}

    saver.consume_prepared_context_for_dispatch.side_effect = consume_prepared

    saver.execution_for_turn.return_value = "execution_test"
    saver.register_execution_retry_source.return_value = None
    saved_items: dict[str, CanonicalItemRecord] = {}

    def commit_tool_result(
        *, session_id: str, thread_id: str, job_id: str,
        completion: ToolResultCompletion, recovery: bool = False, checkpoint_ns: str = "",
    ) -> CanonicalItemRecord:
        if checkpoint_ns:
            raise ValueError(f"测试工具终态不接受 checkpoint namespace: {checkpoint_ns!r}")
        if recovery and completion.tool_outcome != "unknown":
            raise ValueError("测试冷恢复只接受未知工具终态")
        admission = accepted_inputs.read_admission(
            session_id, thread_id=thread_id, job_id=job_id, checkpoint_ns=checkpoint_ns,
        )
        item = CanonicalItemRecord.create(
            item_sequence=1,
            item_id=completion.item_id,
            semantic_kind="tool_result",
            payload_kind="tool_result",
            status="completed",
            producer_ref={
                "producer_kind": "tool",
                "producer_id": completion.tool_attempt_id,
                "invocation_id": completion.model_call_id,
            },
            payload={
                "tool_call_id": completion.tool_call_id,
                "tool_invocation_id": completion.tool_invocation_id,
                "tool_attempt_id": completion.tool_attempt_id,
                "result_id": completion.tool_attempt_id,
                "name": completion.tool_name,
                "content": completion.content,
                "tool_outcome": completion.tool_outcome,
                "execution_confirmed": completion.tool_outcome != "unknown",
            },
            projection_identity=ItemProjectionIdentity(model_call_id=completion.model_call_id),
            turn_id=admission.turn_id,
            turn_scope="turn_member" if admission.turn_id is not None else None,
            message_group_id=f"message-{completion.tool_attempt_id}",
            wire_role="tool",
        )
        saved_items[item.item_id] = item
        return item

    saver.commit_tool_result.side_effect = commit_tool_result

    def persist_final(
        *, message_id, turn_id, final_text, before_persist=None, **_kwargs
    ):
        if not final_text:
            return False
        if before_persist is not None:
            before_persist(
                AIMessage(
                    id=message_id,
                    content=final_text,
                    response_metadata={"phase": "final_answer"},
                )
            )
        item = CanonicalItemRecord.create(
            item_sequence=1,
            item_id=f"item-{message_id}",
            semantic_kind="assistant_output",
            wire_role="assistant",
            payload_kind="text",
            status="completed",
            producer_ref={"producer_kind": "provider", "producer_id": "model_test"},
            payload=final_text,
            turn_id=turn_id,
            turn_scope="turn_member",
        )
        saved_items[item.item_id] = item
        return True

    saver.get_canonical_item.side_effect = (
        lambda _session_id, *, item_id, checkpoint_ns: saved_items.get(item_id)
    )
    monkeypatch.setattr(
        "app.services.orchestration.execution_step.runner.persist_user_message_checkpoint",
        MagicMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.orchestration.execution_step.runner.persist_standard_assistant_checkpoint",
        persist_final,
    )
    dependency_provider.get_checkpointer.return_value = saver
    workspace_id, workspace_root = workspace_scope
    activation_body_store = _ActivationBodyStore(workspace_id=workspace_id)
    workspace_file_resource_registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace_root,
        watch_service=WorkspaceFileWatchService(workspace_root=workspace_root),
    )

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

    graph_store = MagicMock(spec=GraphBindingStorePort)
    graph_store.load_graph_binding.return_value = DEEP_AGENT_GRAPH_BINDING
    return {
        "graph_binding_store": graph_store,
        "residency_tracker": ThreadResidencyTracker(),
        "resource_activation_factory": activation_factory,
        "config_service": config_service,
        "registry": registry,
        "msg_bus": msg_bus,
        "job_event_bus": job_event_bus,
        "session_changes_service": session_changes_service,
        "tool_selection_store": tool_selection_store,
        "dependency_provider": dependency_provider,
        "accepted_user_inputs": accepted_inputs,
        "message_stream_store": message_stream_store,
        "workspace_root": workspace_root,
        "workspace_file_resource_registry": workspace_file_resource_registry,
        "workspace_id": workspace_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", [None, "register_model_call", "mark_execution_lost"]
)
async def test_step_requires_v2_owner_before_opening_stream(agent_execution_dependencies, missing):
    provider = agent_execution_dependencies["dependency_provider"]
    if missing is None:
        provider.get_checkpointer.return_value = None
    else:
        setattr(provider.get_checkpointer.return_value, missing, None)
    service = _build_agent_execution_service(agent_execution_dependencies)
    with pytest.raises((TypeError, RuntimeError), match="checkpointer|缺少可调用端口"):
        await service.run_step(
            session_id="ses_missing",
            message="用户输入",
            agent_id="test_agent",
            job_id="turn_missing",
            execution_binding=_user_execution_binding("turn_missing"),
            message_id="msg_missing",
            message_created_at="2026-07-20T00:00:00+00:00",
            thread_id="thr_test",
        )
    agent_execution_dependencies["message_stream_store"].open.assert_not_awaited()
    agent_execution_dependencies["job_event_bus"].publish.assert_not_awaited()


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


def _user_execution_binding(job_id: str) -> UserExecutionIdentity:
    return UserExecutionIdentity(
        job_id=job_id,
        turn_id=f"turn_{job_id}",
        admission_idempotency_key=f"adm_{job_id}",
    )


async def _run_accepted_step(service, agent_execution_dependencies, **step_args):
    """先登记真实 owner/job/message 输入，再从正式 Step 入口执行。"""
    execution_binding = step_args["execution_binding"]
    if not isinstance(execution_binding, UserExecutionIdentity):
        raise TypeError("测试 helper 只接受已登记的用户执行身份")
    agent_execution_dependencies["accepted_user_inputs"].accept(
        session_id=step_args["session_id"],
        thread_id=step_args["thread_id"],
        job_id=step_args["job_id"],
        message_id=step_args["message_id"],
        message=step_args["message"],
        message_created_at=step_args["message_created_at"],
        message_metadata=step_args.get("message_metadata"),
        execution_binding=execution_binding,
    )
    return await service.run_step(**step_args)


def _build_agent_execution_service(dependencies):
    """用当前测试夹具构造 AgentExecutionService。"""
    return AgentExecutionService(
        graph_binding_store=dependencies["graph_binding_store"],
        residency_tracker=dependencies["residency_tracker"],
        resource_activation_factory=dependencies["resource_activation_factory"],
        team_resource_subscriber=lambda _session, _thread, _registry, _scope: None,
        config_service=dependencies["config_service"],
        background_task_registry=dependencies["registry"],
        background_message_bus=dependencies["msg_bus"],
        job_event_bus=dependencies["job_event_bus"],
        dependency_provider=dependencies["dependency_provider"],
        session_changes_service=dependencies["session_changes_service"],
        tool_selection_store=dependencies["tool_selection_store"],
        message_stream_store=dependencies["message_stream_store"],
        workspace_id=dependencies["workspace_id"],
        workspace_root=dependencies["workspace_root"],
        workspace_file_resource_registry=dependencies["workspace_file_resource_registry"],
        session_catalog=get_session_catalog_components(
            dependencies["workspace_root"] / ".boxteam" / "sessions",
            workspace_id=dependencies["workspace_id"],
        ),
        control_inbox_state_path=lambda _session_id, turn_stream_id: (
            dependencies["workspace_root"] / "control" / f"{turn_stream_id}.json"
        ),
    )


def test_tool_face_blueprint_cache_is_keyed_without_session(
    agent_execution_dependencies,
):
    """OpenSpec 8.4：复用的是不含 session/thread 的工具面 blueprint 投影。

    同一配置 revision 反复查询命中同一份定义；配置 revision 变化则重建。
    缓存键不含 session_id，真实 invocation 的 session/thread 依赖不在其中。
    """
    service = _build_agent_execution_service(agent_execution_dependencies)
    agent_execution_dependencies["config_service"].get_revision.side_effect = [
        "revision-a",
        "revision-a",
        "revision-b",
    ]

    def fake_definitions(_agent: object, *, extension_tools: object) -> list[dict]:
        assert list(extension_tools) == []
        return [{"id": "read_file", "name": "read_file"}]

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            side_effect=[object(), object()],
        ) as build_runtime,
        patch(
            "app.services.orchestration.agent_execution_service.build_agent_tool_definitions",
            side_effect=fake_definitions,
        ),
    ):
        first = service.get_available_tools("test_agent")
        assert service.get_available_tools("test_agent") is first  # 命中缓存
        second = service.get_available_tools("test_agent")

    assert build_runtime.call_count == 2  # 配置 revision 变化 → 重建
    assert all(
        call.kwargs["workspace_file_resource_registry"]
        is agent_execution_dependencies["workspace_file_resource_registry"]
        for call in build_runtime.call_args_list
    )
    assert second is not first
    # 缓存键不含 session_id：不含任何会话身份。
    keys = list(service._tool_face_cache)
    assert len(keys) == 1
    key = keys[0]
    assert key[0] == "test_agent"
    assert key[1] == "revision-b"
    assert "ses_test" not in str(key)
    assert service._tool_face_cache[key] == [{"id": "read_file", "name": "read_file"}]


@pytest.mark.asyncio
async def test_run_step_pins_snapshot_through_async_tool_stage(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)
    snapshot = build_config_snapshot({
        "test": "pinned",
        "context": agent_execution_dependencies["config_service"].get_snapshot.return_value.to_dict()["context"],
    }, source_paths=())
    active_snapshot: ContextVar[object | None] = ContextVar(
        "test_config_snapshot", default=None
    )
    agent_execution_dependencies["config_service"].get_snapshot.return_value = snapshot

    @contextmanager
    def use_snapshot(candidate):
        token = active_snapshot.set(candidate)
        try:
            yield
        finally:
            active_snapshot.reset(token)

    agent_execution_dependencies["config_service"].use_snapshot.side_effect = use_snapshot

    built_agent = object()

    def fake_build_agent(**kwargs):
        assert active_snapshot.get() is snapshot
        assert kwargs["config_service"] is agent_execution_dependencies["config_service"]
        assert kwargs["workspace_root"] == agent_execution_dependencies["workspace_root"]
        return built_agent

    async def fake_process_events(**kwargs):
        assert active_snapshot.get() is snapshot
        assert kwargs["agent"] is built_agent
        assert (
            kwargs["session_changes_service"]
            is agent_execution_dependencies["session_changes_service"]
        )
        assert kwargs["workspace_root"] == agent_execution_dependencies["workspace_root"]
        await asyncio.sleep(0)
        # 模拟模型返回后进入异步工具阶段，snapshot 仍必须固定。
        assert active_snapshot.get() is snapshot
        return AgentEventStreamResult(
            final_text="ok",
            latest_model_content_blocks=({"type": "text", "text": "ok"},),
            last_tool_result_text="",
        )

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            side_effect=fake_build_agent,
        ) as build_agent,
        patch(
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=fake_process_events,
        ) as process_events,
    ):
        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            execution_binding=_user_execution_binding("job_test"),
            message_id="msg_test",
            message_created_at="2026-07-19T00:00:00+00:00",
            thread_id="thr_test",
        )
    build_agent.assert_called_once()
    process_events.assert_awaited_once()

    assert result == "ok"
    assert active_snapshot.get() is None


def test_delegated_report_requires_successful_parent_directed_system_message():
    parent = "ses_parent"
    assert not _has_valid_delegated_report(
        [
            SuccessfulToolCall(
                "send_message_to_session", {"target_session_id": "ses_other"}
            )
        ],
        parent_session_id=parent,
    )
    assert not _has_valid_delegated_report(
        [
            SuccessfulToolCall(
                "send_message_to_session",
                {
                    "target_session_id": parent,
                    "kind": "progress",
                },
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
            "kind": "reply",
            "reply_to_communication_id": "comm_question",
        },
    )
    assert _has_valid_session_question_reply(
        [valid],
        communication_id="comm_question",
    )
    assert not _has_valid_session_question_reply(
        [valid],
        communication_id="comm_other",
    )


@pytest.mark.asyncio
async def test_delegated_report_is_not_enforced_by_default(agent_execution_dependencies):
    service = _build_agent_execution_service(agent_execution_dependencies)
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
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            new=AsyncMock(return_value=stream_result),
        ) as process,
    ):
        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_child",
            message="委派任务",
            agent_id="test_agent",
            job_id="job_child",
            execution_binding=_user_execution_binding("job_child"),
            message_id="msg_child",
            message_created_at="2026-07-16T00:00:00+00:00",
            message_metadata={
                "source": "session_subagent_delegation",
                "parent_session_id": "ses_parent",
            },
            thread_id="thr_test",
        )

    assert result == "普通文本结果"
    process.assert_awaited_once()


@pytest.mark.asyncio
async def test_delegated_first_turn_fails_after_two_missing_tool_reports(
    agent_execution_dependencies,
):
    agent_execution_dependencies["config_service"].get_agent_runtime_config.return_value[
        "require_delegated_report"
    ] = True
    service = _build_agent_execution_service(agent_execution_dependencies)
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
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            new=AsyncMock(side_effect=stream_results),
        ) as process,
    ):
        with pytest.raises(RuntimeError, match="没有通过 send_message_to_session"):
            await _run_accepted_step(service, agent_execution_dependencies,
                session_id="ses_child",
                message="委派任务",
                agent_id="test_agent",
                job_id="job_child",
                execution_binding=_user_execution_binding("job_child"),
                message_id="msg_child",
                message_created_at="2026-07-16T00:00:00+00:00",
                message_metadata={
                    "source": "session_subagent_delegation",
                    "parent_session_id": "ses_parent",
                },
                thread_id="thr_test",
            )

    assert process.await_count == 3
    assert process.await_args_list[0].kwargs["config"]["recursion_limit"] == 9999


@pytest.mark.asyncio
async def test_delegated_progress_only_cannot_replace_final_result(
    agent_execution_dependencies,
):
    agent_execution_dependencies["config_service"].get_agent_runtime_config.return_value[
        "require_delegated_report"
    ] = True
    service = _build_agent_execution_service(agent_execution_dependencies)
    progress_call = SuccessfulToolCall(
        "send_message_to_session",
        {
            "target_session_id": "ses_parent",
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
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            new=AsyncMock(side_effect=stream_results),
        ),
    ):
        with pytest.raises(RuntimeError, match="没有通过 send_message_to_session"):
            await _run_accepted_step(service, agent_execution_dependencies,
                session_id="ses_child",
                message="委派任务",
                agent_id="test_agent",
                job_id="job_child",
                execution_binding=_user_execution_binding("job_child"),
                message_id="msg_child",
                message_created_at="2026-07-16T00:00:00+00:00",
                message_metadata={
                    "source": "session_subagent_delegation",
                    "parent_session_id": "ses_parent",
                },
                thread_id="thr_test",
            )


@pytest.mark.asyncio
async def test_cross_session_question_retries_until_correlated_tool_reply(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)
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
                        "target_session_id": SOURCE_SESSION,
                        "kind": "reply",
                        "reply_to_communication_id": "comm_question",
                    },
                ),
            ),
        ),
    ]

    async def process_side_effect(*, input_payload, **_kwargs):
        input_messages.append(input_payload["messages"])
        return stream_results.pop(0)

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_side_effect,
        ),
    ):
        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_answerer",
            message="跨会话问题",
            agent_id="test_agent",
            job_id="job_answer",
            execution_binding=_user_execution_binding("job_answer"),
            message_id="msg_question",
            message_created_at="2026-07-16T00:00:00+00:00",
            message_metadata={
                "source": "send_message_to_session",
                "kind": "question",
                "reply_required": True,
                "source_address": {"gateway_id": "gw-question", "workspace_id": WORKSPACE_ID, "session_id": SOURCE_SESSION, "thread_id": SOURCE_THREAD},
                "communication_id": "comm_question",
            },
            thread_id="thr_test",
        )

    assert result == "已通过会话工具回复"
    assert len(input_messages) == 2
    assert input_messages[1] == []
    retry = agent_execution_dependencies["dependency_provider"].get_checkpointer.return_value.register_execution_retry_source.call_args.kwargs["message"]
    assert retry.metadata["source"] == "session_question_reply_retry"


@pytest.mark.asyncio
async def test_primary_success_no_fallback(agent_execution_dependencies):
    """测试：主模型成功时，不使用 fallback。"""
    deps = agent_execution_dependencies
    deps["tool_selection_store"].snapshot.side_effect = lambda agent_id: ToolSelectionSnapshot(
        agent_id, {"apply_patch": False, "test_tool_2": False}, {},
    )
    service = _build_agent_execution_service(deps)

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

        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            execution_binding=_user_execution_binding("job_test"),
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
            thread_id="thr_test",
        )

        assert result == "主模型成功"
        assert mock_build.call_count == 1
        assert mock_build.call_args.kwargs["execution_overrides"] == {
            "apply_patch": False,
            "test_tool_2": False,
        }


@pytest.mark.asyncio
async def test_reasoning_stream_not_mixed_into_final_text(agent_execution_dependencies):
    """reasoning 流只参与最终聚合，不应污染正式回复或旧实时事件。"""
    deps = agent_execution_dependencies
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
    service = _build_agent_execution_service(deps)

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

        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            execution_binding=_user_execution_binding("job_test"),
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
            thread_id="thr_test",
        )

    assert result == "OK"
    assert not [
        call
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") == "text_delta"
    ]


@pytest.mark.asyncio
async def test_model_event_stream_does_not_recreate_legacy_text_events(
    agent_execution_dependencies,
):
    """模型事件流不应重新创建消息流已经替代的旧文本事件。"""
    deps = agent_execution_dependencies
    service = _build_agent_execution_service(deps)

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

        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            execution_binding=_user_execution_binding("job_test"),
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
            thread_id="thr_test",
        )

    assert result == "第一段\n\n第二段"
    assert not [
        call
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") in {"text_start", "text_delta", "text_end"}
    ]


@pytest.mark.asyncio
async def test_reasoning_only_response_retries_with_system_reminder(agent_execution_dependencies):
    """reasoning-only 空响应应继续请求模型产生工具调用或最终正文。"""
    deps = agent_execution_dependencies
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
    service = _build_agent_execution_service(deps)

    input_messages: list[object] = []

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:

        async def mock_events(input_payload, *args, **kwargs):
            input_messages.append(input_payload["messages"])
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
            assert input_payload["messages"] == []
            reminder = deps["dependency_provider"].get_checkpointer.return_value.register_execution_retry_source.call_args.kwargs["message"]
            assert "<system_reminder>" in reminder.content
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

        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            execution_binding=_user_execution_binding("job_test"),
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
            thread_id="thr_test",
        )

    assert result == "OK"
    assert len(input_messages) == 2
    assert input_messages[1] == []
    retry = agent_execution_dependencies["dependency_provider"].get_checkpointer.return_value.register_execution_retry_source.call_args.kwargs["message"]
    assert retry.metadata["source"] == "empty_response_retry"


@pytest.mark.asyncio
async def test_requested_custom_tool_missing_result_retries_with_system_reminder(
    agent_execution_dependencies,
):
    """用户点名配置 custom tool 但模型只输出正文时，应继续要求真实工具调用。"""
    deps = agent_execution_dependencies
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
    service = _build_agent_execution_service(deps)

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
            "app.services.orchestration.execution_step.retry.process_agent_event_stream"
        ) as mock_process,
    ):
        mock_build.return_value = MagicMock()

        async def process_side_effect(*, input_payload, **kwargs):
            input_messages.append(input_payload["messages"])
            return stream_results.pop(0)

        mock_process.side_effect = process_side_effect

        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="test",
            message="请调用 test_tool_2",
            agent_id="test_agent",
            job_id="job_test",
            execution_binding=_user_execution_binding("job_test"),
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
            thread_id="thr_test",
        )

    assert result == "4568"
    assert len(input_messages) == 2
    assert input_messages[1] == []
    retry = agent_execution_dependencies["dependency_provider"].get_checkpointer.return_value.register_execution_retry_source.call_args.kwargs["message"]
    assert retry.metadata["source"] == "missing_custom_tool_retry"
    assert "test_tool_2" in retry.metadata["missing_tools"]


@pytest.mark.asyncio
async def test_standard_content_blocks_stream_split_reasoning_and_text(
    agent_execution_dependencies,
):
    """标准 content blocks 流应拆分为 reasoning/text 两种事件。"""
    deps = agent_execution_dependencies
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
    service = _build_agent_execution_service(deps)

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

        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            execution_binding=_user_execution_binding("job_test"),
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
            thread_id="thr_test",
        )

    assert result == "OK"
    assert not [
        call
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") == "text_delta"
    ]


@pytest.mark.asyncio
async def test_tool_events_use_tool_start_input_and_tool_message_content(
    agent_execution_dependencies,
):
    """工具卡片应使用 on_tool_start 的完整参数和 ToolMessage.content。"""
    deps = agent_execution_dependencies
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
    service = _build_agent_execution_service(deps)
    tool_arguments = {"code": "print('LC_BLOCK_OK_2')"}

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:

        async def mock_events(*args, **kwargs):
            delta_sink = get_current_model_delta_sink()
            if delta_sink is None:
                raise AssertionError("测试模型流缺少消息 delta sink")
            event_metadata = {
                **stream_metadata(),
                "ls_model_name": "primary",
            }
            model_run_id = "model_run_with_tool"
            yield {
                "event": "on_chat_model_start",
                "name": "ChatOpenAI",
                "run_id": model_run_id,
                "data": {"input": []},
                "metadata": event_metadata,
            }
            tool_call = {
                "id": "call_1",
                "name": "python_exec",
                "args": dict(tool_arguments),
                "type": "tool_call",
            }
            tool_chunk = create_chunk("", tool_calls=[tool_call])
            await delta_sink.accept_message_chunk(
                tool_chunk, model_call_id=model_run_id,
            )
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "run_id": model_run_id,
                "data": {"chunk": tool_chunk},
                "metadata": event_metadata,
            }
            yield {
                "event": "on_chat_model_end",
                "name": "ChatOpenAI",
                "run_id": model_run_id,
                "data": {"output": AIMessage(content="", tool_calls=[tool_call])},
                "metadata": event_metadata,
            }
            await delta_sink.prepare_tool_execution(model_run_id)
            lifecycle_callback = AgentRunLifecycleCallbackHandler(delta_sink)
            with model_tool_invocation_scope(model_run_id, "call_1"):
                tool_callback = asyncio.create_task(
                    lifecycle_callback.on_tool_start(
                        run_id="run_python_exec",
                        tool_call_id="call_1",
                    )
                )
            yield {
                "event": "on_tool_start",
                "run_id": "run_python_exec",
                "name": "python_exec",
                "data": {"input": dict(tool_arguments)},
                "metadata": stream_metadata(),
            }
            await tool_callback
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
            final_model_run_id = "model_run_after_tool"
            yield {
                "event": "on_chat_model_start",
                "name": "ChatOpenAI",
                "run_id": final_model_run_id,
                "data": {"input": []},
                "metadata": event_metadata,
            }
            final_chunk = create_chunk(
                "完成",
                part_id="part_after_tool",
                index=0,
            )
            await delta_sink.accept_message_chunk(
                final_chunk, model_call_id=final_model_run_id,
            )
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "run_id": final_model_run_id,
                "data": {"chunk": final_chunk},
                "metadata": event_metadata,
            }
            yield {
                "event": "on_chat_model_end",
                "name": "ChatOpenAI",
                "run_id": final_model_run_id,
                "data": {"output": AIMessage(content="完成")},
                "metadata": event_metadata,
            }

        mock_agent = MagicMock()
        mock_agent.astream_events = mock_events
        mock_agent.get_graph = None
        mock_build.return_value = mock_agent

        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="test",
            message="test",
            agent_id="test_agent",
            job_id="job_test",
            execution_binding=_user_execution_binding("job_test"),
            message_id="msg_test",
            message_created_at="2026-07-14T00:00:00+00:00",
            thread_id="thr_test",
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
    tool_publish_sequence = [
        (call.kwargs["event_type"], call.kwargs["payload"]["execution_id"])
        for call in deps["job_event_bus"].publish.call_args_list
        if call.kwargs.get("event_type") in {"tool_call_start", "tool_call_end"}
    ]
    assert tool_publish_sequence == [
        ("tool_call_start", "run_python_exec"),
        ("tool_call_end", "run_python_exec"),
    ]
    assert "ToolMessage" not in tool_end_payloads[0]["result"]
    assert "content=" not in tool_end_payloads[0]["result"]


@pytest.mark.asyncio
async def test_execution_builds_single_agent_runtime_without_model_override(
    agent_execution_dependencies,
):
    """成功 fake Agent 流验证单次 runtime 构建且不传模型覆盖参数。"""
    deps = agent_execution_dependencies
    service = _build_agent_execution_service(deps)

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
                                "Agent 流成功",
                                part_id="part_agent_success",
                                index=0,
                            )
                        },
                        "metadata": stream_metadata(),
                    }

                mock_agent.astream_events = mock_events

                return mock_agent

            mock_build.side_effect = create_mock_agent

            result = await _run_accepted_step(service, agent_execution_dependencies,
                session_id="test",
                message="test",
                agent_id="test_agent",
                job_id="job_test",
                execution_binding=_user_execution_binding("job_test"),
                message_id="msg_test",
                message_created_at="2026-07-14T00:00:00+00:00",
                thread_id="thr_test",
            )

            assert result == "Agent 流成功"
            assert mock_build.call_count == 1
            assert "override_model" not in mock_build.call_args.kwargs


@pytest.mark.asyncio
async def test_agent_runtime_build_exception_propagates(agent_execution_dependencies):
    """Agent runtime 构建阶段异常会传播给调用方。"""
    deps = agent_execution_dependencies
    service = _build_agent_execution_service(deps)

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as mock_build:
        mock_build.side_effect = Exception("Agent runtime 构建失败")

        with pytest.raises(Exception, match="Agent runtime 构建失败"):
            await _run_accepted_step(service, agent_execution_dependencies,
                session_id="test",
                message="test",
                agent_id="test_agent",
                job_id="job_test",
                execution_binding=_user_execution_binding("job_test"),
                message_id="msg_test",
                message_created_at="2026-07-14T00:00:00+00:00",
                thread_id="thr_test",
            )


@pytest.mark.asyncio
async def test_successful_agent_stream_publishes_agent_start_once(agent_execution_dependencies):
    """成功 fake Agent 流只发布一次 agent_start。"""
    deps = agent_execution_dependencies
    service = _build_agent_execution_service(deps)

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
                                "Agent 流成功",
                                part_id="part_agent_success",
                                index=0,
                            )
                        },
                        "metadata": stream_metadata(),
                    }

                mock_agent.astream_events = mock_events

                return mock_agent

            mock_build.side_effect = create_mock_agent

            await _run_accepted_step(service, agent_execution_dependencies,
                session_id="test",
                message="test",
                agent_id="test_agent",
                job_id="job_test",
                execution_binding=_user_execution_binding("job_test"),
                message_id="msg_test",
                message_created_at="2026-07-14T00:00:00+00:00",
                thread_id="thr_test",
            )

    publish_calls = [
        c
        for c in deps["job_event_bus"].publish.call_args_list
        if c.kwargs.get("event_type") == "agent_start"
    ]
    assert len(publish_calls) == 1


@pytest.mark.asyncio
async def test_agent_loop_retry_keeps_model_attempt_boundaries(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)
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
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_with_retry,
        ),
    ):
        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_retry",
            message="重试",
            agent_id="test_agent",
            job_id="job_retry",
            execution_binding=_user_execution_binding("job_retry"),
            message_id="msg_retry",
            message_created_at="2026-07-20T00:00:00+00:00",
            thread_id="thr_test",
        )

    assert result == "最终答案"
    committed_types = [
        call.args[0]
        for call in agent_execution_dependencies[
            "message_stream_store"
        ].open.return_value.commit.await_args_list
    ]
    assert committed_types.count("model.started") == 2
    assert committed_types.count("model.completed") == 2
    assert committed_types.count("model.retrying") == 1
    agent_execution_dependencies[
        "message_stream_store"
    ].open.return_value.close_completed.assert_awaited_once()
    completed_outcomes = [
        call.args[1]["outcome"]
        for call in agent_execution_dependencies[
            "message_stream_store"
        ].open.return_value.commit.await_args_list
        if call.args[0] == "model.completed"
    ]
    assert completed_outcomes == ["validation_failed", "accepted"]


@pytest.mark.asyncio
async def test_interrupt_wins_when_completion_races_with_persisted_request(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)
    writer = agent_execution_dependencies["message_stream_store"].open.return_value
    writer.close_completed.side_effect = MessageStreamTerminalError("已进入中断闸门")
    agent_execution_dependencies["message_stream_store"].get_state.return_value = {
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
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_success,
        ),
    ):
        result = await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_race",
            message="竞态",
            agent_id="test_agent",
            job_id="job_race",
            execution_binding=_user_execution_binding("job_race"),
            message_id="msg_race",
            message_created_at="2026-07-20T00:00:00+00:00",
            thread_id="thr_test",
        )

    assert result == "竞态结果"
    writer.close_interrupted.assert_awaited_once_with("intr_race")
    writer.close_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_without_user_interrupt_persists_checkpoint_and_closes_as_execution_lost(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)

    async def process_cancelled(**_kwargs):
        raise asyncio.CancelledError()

    with (  # noqa: SIM117 - 取消异常需要单独断言原始 CancelledError。
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_cancelled,
        ),
        patch(
            "app.services.orchestration.execution_step.failures.persist_interrupt_checkpoint",
        ) as persist_checkpoint,
    ):
        with pytest.raises(asyncio.CancelledError):
            await _run_accepted_step(service, agent_execution_dependencies,
                session_id="ses_cancelled",
                message="取消",
                agent_id="test_agent",
                job_id="job_cancelled",
                execution_binding=_user_execution_binding("job_cancelled"),
                message_id="msg_cancelled",
                message_created_at="2026-07-20T00:00:00+00:00",
                thread_id="thr_test",
            )

    persist_checkpoint.assert_called_once()
    assert persist_checkpoint.call_args.kwargs["checkpoint_source"] == "execution_lost"
    writer = agent_execution_dependencies["message_stream_store"].open.return_value
    writer.close_failed.assert_awaited_once_with(
        code="execution_lost",
        message="AgentLoop 因内部取消而结束，未收到用户中断请求",
        resumable=False,
    )


@pytest.mark.asyncio
async def test_scope_job_timeout_cancelled_error_keeps_job_timeout_code(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)

    async def process_job_timeout(**_kwargs):
        raise asyncio.CancelledError("运行时 scope 已取消: reason=job_timeout")

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_job_timeout,
        ),
        patch(
            "app.services.orchestration.execution_step.failures.persist_interrupt_checkpoint",
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_scope_job_timeout",
            message="总超时",
            agent_id="test_agent",
            job_id="job_scope_job_timeout",
            execution_binding=_user_execution_binding("job_scope_job_timeout"),
            message_id="msg_scope_job_timeout",
            message_created_at="2026-07-20T00:00:00+00:00",
            thread_id="thr_test",
        )

    writer = agent_execution_dependencies["message_stream_store"].open.return_value
    writer.close_failed.assert_awaited_once_with(
        code="job_timeout",
        message="Job 执行超过总超时上限",
        resumable=False,
    )


@pytest.mark.asyncio
async def test_scope_deadline_failure_persists_checkpoint_without_user_interrupt(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)

    async def process_scope_deadline(**_kwargs):
        raise ScopeCancelledError("scope_deadline_exceeded")

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_scope_deadline,
        ),
        patch(
            "app.services.orchestration.execution_step.failures.persist_interrupt_checkpoint",
        ) as persist_checkpoint,
        pytest.raises(ScopeCancelledError, match="scope_deadline_exceeded"),
    ):
        await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_scope_deadline",
            message="在预算内完成",
            agent_id="test_agent",
            job_id="job_scope_deadline",
            execution_binding=_user_execution_binding("job_scope_deadline"),
            message_id="msg_scope_deadline",
            message_created_at="2026-07-20T00:00:00+00:00",
            thread_id="thr_test",
        )

    persist_checkpoint.assert_called_once()
    assert persist_checkpoint.call_args.kwargs["checkpoint_source"] == (
        "scope_deadline_exceeded"
    )
    writer = agent_execution_dependencies["message_stream_store"].open.return_value
    writer.close_failed.assert_awaited_once_with(
        code="scope_deadline_exceeded",
        message="运行时 scope 已取消: reason=scope_deadline_exceeded",
        after_interrupt_requested=False,
        resumable=False,
    )


@pytest.mark.asyncio
async def test_cancelled_with_complete_tool_call_closes_as_dispatch_timeout(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)
    zero_argument_tool = create_test_tool()
    assert zero_argument_tool.args == {}

    async def process_cancelled(**kwargs):
        runtime = kwargs["message_stream_runtime"]
        await runtime.start_model("model_dispatch_timeout", "backup_3")
        await runtime.accept_message_chunk(
            create_chunk(
                tool_calls=[
                    {
                        "id": "call_dispatch_timeout",
                        "name": zero_argument_tool.name,
                        "args": "{}",
                    }
                ]
            )
        )
        raise asyncio.CancelledError()

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_cancelled,
        ),
        patch(
            "app.services.orchestration.execution_step.failures.persist_interrupt_checkpoint",
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_dispatch_timeout_cancelled",
            message="分派超时",
            agent_id="test_agent",
            job_id="job_dispatch_timeout_cancelled",
            execution_binding=_user_execution_binding("job_dispatch_timeout_cancelled"),
            message_id="msg_dispatch_timeout_cancelled",
            message_created_at="2026-07-20T00:00:00+00:00",
            thread_id="thr_test",
        )

    writer = agent_execution_dependencies["message_stream_store"].open.return_value
    writer.close_failed.assert_awaited_once_with(
        code="tool_dispatch_timeout",
        message=(
            "模型工具调用参数已完整，但工具执行分派在取消前没有启动: "
            "tool_calls=['model_dispatch_timeout:tool-call:call_dispatch_timeout']"
        ),
        resumable=False,
    )


@pytest.mark.asyncio
async def test_job_timeout_cancel_closes_stream_as_timed_out(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)

    async def process_timed_out(**_kwargs):
        raise asyncio.CancelledError("job_timeout")

    with (
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_timed_out,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_accepted_step(service, agent_execution_dependencies,
            session_id="ses_job_timeout_stream",
            message="总超时",
            agent_id="test_agent",
            job_id="job_timeout_stream",
            execution_binding=_user_execution_binding("job_timeout_stream"),
            message_id="msg_timeout_stream",
            message_created_at="2026-07-20T00:00:00+00:00",
            thread_id="thr_test",
        )

    writer = agent_execution_dependencies["message_stream_store"].open.return_value
    writer.close_failed.assert_awaited_once_with(
        code="job_timeout",
        message="Job 执行超过总超时上限",
        resumable=False,
    )


@pytest.mark.asyncio
async def test_cancelled_after_user_interrupt_closes_as_interrupted(
    agent_execution_dependencies,
):
    service = _build_agent_execution_service(agent_execution_dependencies)

    async def process_user_cancelled(**_kwargs):
        SessionInterruptState.set(
            "ses_user_cancelled", thread_id="thr_test",
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
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_user_cancelled,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _run_accepted_step(service, agent_execution_dependencies,
                session_id="ses_user_cancelled",
                message="用户打断",
                agent_id="test_agent",
                job_id="job_user_cancelled",
                execution_binding=_user_execution_binding("job_user_cancelled"),
                message_id="msg_user_cancelled",
                message_created_at="2026-07-20T00:00:00+00:00",
                thread_id="thr_test",
            )

    writer = agent_execution_dependencies["message_stream_store"].open.return_value
    writer.close_interrupted.assert_awaited_once_with("intr_user_cancelled")
    writer.close_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_exception_persists_stream_failure_before_rethrow(
    agent_execution_dependencies, caplog,
):
    service = _build_agent_execution_service(agent_execution_dependencies)
    original_error = RuntimeError("上游异常")

    async def process_failed(**_kwargs):
        raise original_error

    with (  # noqa: SIM117 - 异常路径需要单独断言原始执行异常。
        patch(
            "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.orchestration.execution_step.retry.process_agent_event_stream",
            side_effect=process_failed,
        ),
    ):
        with pytest.raises(RuntimeError, match="上游异常") as raised:
            await _run_accepted_step(service, agent_execution_dependencies,
                session_id="ses_execution_error",
                message="异常",
                agent_id="test_agent",
                job_id="job_execution_error",
                execution_binding=_user_execution_binding("job_execution_error"),
                message_id="msg_execution_error",
                message_created_at="2026-07-20T00:00:00+00:00",
                thread_id="thr_test",
            )

    assert raised.value is original_error
    (failure_record,) = [
        record
        for record in caplog.records
        if record.getMessage().startswith(
            "[agent_execution_service] ERROR published:"
        )
    ]
    assert failure_record.exc_info is not None
    error_type, logged_error, error_traceback = failure_record.exc_info
    assert error_type is RuntimeError
    assert logged_error is original_error
    assert error_traceback is not None
    assert any(
        frame.name == "process_failed"
        for frame in traceback.extract_tb(error_traceback)
    )

    writer = agent_execution_dependencies["message_stream_store"].open.return_value
    writer.close_failed.assert_awaited_once_with(
        code="execution_error",
        message="上游异常",
        after_interrupt_requested=False,
        resumable=False,
    )


@pytest.mark.asyncio
async def test_generation_reactor_release_is_isolated_per_thread(agent_execution_dependencies):
    """跨会话 runtime 持有各自订阅；一个 thread 卸载不影响其它 active lease。"""
    service = _build_agent_execution_service(agent_execution_dependencies)
    tracker = agent_execution_dependencies["residency_tracker"]
    reactors: dict[str, AsyncMock] = {}

    def fake_build(*, session_id, reactor_lifetime_scope, **kwargs):
        close = AsyncMock()
        reactors[session_id] = close
        reactor_lifetime_scope.register(close, label="reactor")
        return object()

    async def acquire(session_id: str, job_id: str):
        return await service._acquire_step_agent(
            admission=ExecutionAdmission(
                session_id=session_id,
                thread_id="thr_test",
                execution_id=f"exec_{job_id}",
                job_id=job_id,
                admission_idempotency_key=f"adm_{job_id}",
                execution_origin="user_turn",
                turn_id=f"turn_{job_id}",
            ),
            agent_id="test_agent",
            execution_overrides={},
            model_visibility_overrides={},
            preferred_provider_id=None,
            include_team_tools=False,
        )

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime",
        side_effect=fake_build,
    ):
        first, active = await asyncio.gather(
            acquire("ses_a", "job_a"), acquire("ses_b", "job_b")
        )
        async with first, active:
            first_value = first.runtime
            first.release()
            again = await acquire("ses_a", "job_a2")
            async with again:
                assert again.runtime is first_value
                assert set(reactors) == {"ses_a", "ses_b"}
                reactors["ses_a"].assert_not_awaited()
                again.release()
                state = tracker._states[("ses_a", "thr_test")]
                await tracker._start_runtime_unload(state, state.runtime)
                reactors["ses_a"].assert_awaited_once()
                reactors["ses_b"].assert_not_awaited()
                rebuilt = await acquire("ses_a", "job_a3")
                async with rebuilt:
                    assert rebuilt.generation == max(first.generation, active.generation) + 1
                    assert rebuilt.runtime is not first_value
                    rebuilt.release()
                    active.release()
                    await service.shutdown()
                    reactors["ses_b"].assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_catalog_change_updates_resident_registry_without_rebuilding_runtime(agent_execution_dependencies):
    factory = FakeMcpSessionFactory(initial_tools={"mini": []})
    owner = McpCatalogOwner(
        raw_config={"servers": {"mini": {"enabled": True, "transport": "stdio", "command": "fake"}}},
        workspace_root=agent_execution_dependencies["workspace_root"],
        event_service=EventChannelService(), session_factory=factory,
    )
    await owner.start()
    agent_execution_dependencies["dependency_provider"].get_mcp_catalog_owner.return_value = owner
    service = _build_agent_execution_service(agent_execution_dependencies)
    registries = []

    def build(*, resource_registry, reactor_lifetime_scope, **_kwargs):
        registries.append(resource_registry)
        publisher = McpCatalogResourcePublisher(registry=resource_registry)
        reactor_lifetime_scope.register(owner.subscribe_publication(publisher.publish), label="MCP publication")
        return object()

    async def acquire(job_id: str, mcp_enabled: bool | None = None):
        return await service._acquire_step_agent(
            admission=ExecutionAdmission(
                session_id="ses_mcp_resident", thread_id="thr_test",
                execution_id=f"exec_{job_id}", job_id=job_id,
                admission_idempotency_key=f"adm_{job_id}", execution_origin="user_turn", turn_id=f"turn_{job_id}",
            ),
            agent_id="test_agent",
            execution_overrides={} if mcp_enabled is None else {"mcp__mini__echo": mcp_enabled},
            model_visibility_overrides={} if mcp_enabled is None else {"mcp__mini__echo": mcp_enabled},
            preferred_provider_id=None, include_team_tools=False,
        )

    try:
        with patch("app.services.orchestration.agent_execution_service.build_session_agent_runtime", side_effect=build) as builder:
            first = await acquire("job_empty")
            async with first:
                original = first.runtime
                original_generation = first.generation
            empty = registries[0].snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
            assert not empty.members
            factory.sessions["mini"].tool_names = ["echo"]
            factory.notify_callbacks["mini"]()
            await drain_pending_relists(owner)
            updated = registries[0].snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
            assert updated.members[0].member_id == "mcp__mini__echo"
            assert updated.revision != empty.revision
            second = await acquire("job_added", True)
            async with second:
                assert second.runtime is original
                assert second.generation == original_generation
            factory.sessions["mini"].tool_names = []
            factory.notify_callbacks["mini"]()
            await drain_pending_relists(owner)
            third = await acquire("job_removed", False)
            async with third:
                assert third.runtime is original
                assert not registries[0].snapshot(MCP_TOOL_CATALOG_RESOURCE_ID).members
            assert builder.call_count == 1
    finally:
        await service.shutdown()
        await owner.shutdown()
