"""真实 Runner、注册 factory、Saver 与 thread generation scope 的纵向合同。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sized
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from inspect import signature
from pathlib import Path
from time import perf_counter_ns
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt.tool_node import ToolCallRequest

from app.agents import agent_factory
from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    GRAPH_FACTORY_REGISTRY,
    GraphBinding,
    compute_capability_profile_hash,
)
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.sealed_assembly_dispatch import (
    SealedAssemblyDispatch,
    read_sealed_assembly_dispatch,
)
from app.agents.skill_runtime import (
    PublishedSkillCatalog,
    build_workspace_skill_catalog,
    resolve_bundled_skill_groups,
)
from app.agents.summarization.projection import apply_summarization_event
from app.agents.tool_invocation_context import ToolInvocationContextMiddleware
from app.api import message_stream as message_stream_api
from app.api import messages as messages_api
from app.api import session_turns as session_turns_api
from app.api import sessions as sessions_api
from app.api.deps import (
    get_message_stream_store,
    get_request_id,
    get_session_orchestrator,
    get_session_skill_tracking_service,
    get_session_turn_history_service,
    verify_local_token,
)
from app.core.background_task_contracts import BackgroundTaskDescriptor
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import (
    current_execution_admission,
    execution_admission_scope,
    require_current_execution_admission,
)
from app.core.lifecycle import LifetimeScope
from app.core.model_delta_context import AgentRunLifecycleCallbackHandler
from app.core.path_utils import get_boxteam_home, workspace_context
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import (
    current_session_thread_owner,
    session_thread_owner_scope,
)
from app.core.thread_creation import ThreadCreationService
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.job import JobControlRequest
from app.schemas.internal_v2.session import SessionForkRequest
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
)
from app.services.business.session_navigation.operations_service import (
    local_navigation_scope,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
)
from app.services.infrastructure.message_stream_store import (
    MessageStreamStore,
    MessageStreamWriter,
)
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
)
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.queries import (
    RolloutCheckpointQueriesMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_dispatch import (
    ContextDispatchOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.message_materializer import (
    RolloutMessageMaterializerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    skill_catalog_from_activation,
)
from app.services.mapping.itemized.canonical_langchain import (
    project_summary_source,
    summary_source_groups,
    validate_summary_source,
)
from app.services.orchestration.event_stream import processor as event_processor
from app.services.orchestration.execution_step import retry as stream_retry
from app.services.orchestration.execution_step import stream_bindings
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from app.services.orchestration.session_title_service import SessionTitleService
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_test_workspace import (
    prepare_rollout_workspace,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)
from tests.integration.backend.sessions.test_runner_cancellation import (
    RunnerCase,
    _admit,
    _facts,
    _session,
    _start_job,
)
from tests.support.session_navigation_queue import create_catalog_intent

COMPACTION_GATEWAY_ID = "gateway_0123456789abcdef0123456789abcdef"


@pytest.fixture
def runner_case(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> RunnerCase:
    workspace = prepare_rollout_workspace(request)
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    container = _build_owned_container(workspace, monkeypatch, request)
    monkeypatch.setattr(
        SessionTitleService,
        "maybe_auto_title_before_first_message",
        AsyncMock(),
    )
    container.job_service._execution_cancel_timeout_seconds = 0.02
    container.job_service._job_startup_timeout_seconds = 10
    container.job_service._job_timeout_seconds = 10
    return RunnerCase(container, workspace)


@dataclass
class RuntimeCase:
    runner: RunnerCase
    compiled: list[tuple[tuple[str, str], object, LifetimeScope]] = field(
        default_factory=list
    )
    reactors: list[ContextSourceReactor] = field(default_factory=list)
    model_admissions: list[ExecutionAdmission] = field(default_factory=list)
    model_requests: list[dict[str, object]] = field(default_factory=list)
    bindings: list[GraphBinding] = field(default_factory=list)
    build_scopes: list[LifetimeScope] = field(default_factory=list)
    resource_registries: list[ResourceRegistry] = field(default_factory=list)
    skill_catalogs: list[PublishedSkillCatalog] = field(default_factory=list)
    model_dispatches: list[SealedAssemblyDispatch] = field(default_factory=list)
    response_chunks: Callable[[dict[str, object]], list[dict[str, object]]] | None = None
    shutdown_owned_by_lifespan: bool = False

    @property
    def tracker(self):
        return self.runner.container.agent_execution_service._residency_tracker


@pytest.fixture
async def runtime_case(
    runner_case: RunnerCase, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
):
    """只替换外部模型；compiler、CSM、reactor、Saver 与 Runner 都用生产实现。"""
    case = RuntimeCase(
        runner_case, shutdown_owned_by_lifespan=getattr(request, "param", "fixture") == "lifespan",
    )
    resolve = GRAPH_FACTORY_REGISTRY.resolve
    reactor_type = agent_factory.ContextSourceReactor

    def recording_resolve(binding: GraphBinding) -> Callable[..., object]:
        builder = resolve(binding)

        def compile_thread(**kwargs):
            assert current_execution_admission() is None
            owner = current_session_thread_owner()
            pair = (kwargs["session_id"], kwargs["thread_id"])
            assert owner is not None and (owner.session_id, owner.thread_id) == pair
            scope = kwargs["reactor_lifetime_scope"]
            case.build_scopes.append(scope)
            case.resource_registries.append(kwargs["resource_registry"])
            case.skill_catalogs.append(kwargs["skill_catalog"])
            graph = builder(**kwargs)
            assert isinstance(graph, CompiledStateGraph)
            case.compiled.append((pair, graph, scope))
            case.bindings.append(binding)
            return graph

        return compile_thread

    def recording_reactor(**kwargs):
        assert current_execution_admission() is None
        reactor = reactor_type(**kwargs)
        case.reactors.append(reactor)
        return reactor

    class ModelReplyStream:
        received_finish_reason: str | None = None

        def __init__(self, payload: dict[str, object]):
            self.chunks = (
                case.response_chunks(payload) if case.response_chunks is not None else [
                    {"choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "真实编译图回复"},
                    }]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ]
            )
            self.logging_obj = SimpleNamespace(model_call_details={
                "model": payload["model"],
                "custom_llm_provider": payload["custom_llm_provider"],
                "call_type": "acompletion",
                "additional_args": {
                    "api_base": payload.get("api_base"),
                    "complete_input_dict": payload,
                },
            })

        async def __aiter__(self) -> AsyncIterator[dict[str, object]]:
            for chunk in self.chunks:
                finish_reason = chunk["choices"][0].get("finish_reason")
                if finish_reason is not None:
                    self.received_finish_reason = finish_reason
                yield chunk

    class RuntimeProtocolModel(BoxteamLiteLLMChatModel):
        admission_observer: Callable[[ExecutionAdmission], None]
        request_observer: Callable[[dict[str, object]], None]

        async def acompletion_with_retry(self, run_manager=None, **kwargs):
            self._validate_completion_dispatch(kwargs)
            dispatch = read_sealed_assembly_dispatch(invocation_purpose="agent")
            assert dispatch is not None
            case.model_dispatches.append(dispatch)
            self.admission_observer(require_current_execution_admission())
            self.request_observer(deepcopy(kwargs))
            return ModelReplyStream(kwargs)

    def model_from_provider(provider, runtime_config, **kwargs):
        # 外部模型构造与响应受控；真实 binding、wire 参数及封存校验仍执行。
        return RuntimeProtocolModel(
            name="BoxteamLiteLLMChatModel",
            model=provider["model"],
            provider_id=provider["id"],
            api_key="runtime-fixture-key",
            api_base="http://127.0.0.1:1/v1",
            custom_llm_provider="openai",
            streaming=True,
            max_retries=0,
            admission_observer=case.model_admissions.append,
            request_observer=case.model_requests.append,
        )

    monkeypatch.setattr(GRAPH_FACTORY_REGISTRY, "resolve", recording_resolve)
    monkeypatch.setattr(agent_factory, "ContextSourceReactor", recording_reactor)
    monkeypatch.setattr(agent_factory, "build_model_from_provider", model_from_provider)
    if not case.shutdown_owned_by_lifespan:
        await runner_case.container.mcp_catalog_owner.start()
        await runner_case.container.workspace_file_resource_registry.start()
        await runner_case.container.team_fanout_service.start()
    try:
        yield case
    finally:
        if not case.shutdown_owned_by_lifespan:
            with workspace_context(
                runner_case.container.workspace_service.workspace_id,
                runner_case.workspace,
                session_catalog=runner_case.container.session_catalog,
            ):
                runner_case.container.job_service.close_admission()
                await runner_case.container.job_service.force_interrupt_active(
                    reason="测试结束"
                )
                await runner_case.container.team_fanout_service.stop()
                await runner_case.container.agent_execution_service.shutdown()
                await runner_case.container.resource_platform.close()
                await runner_case.container.mcp_catalog_owner.shutdown()
                runner_case.container.workspace_source_owner.close()


async def _complete_job(
    case: RuntimeCase, session_id: str, thread_id: str, *, timeout_seconds: float = 8,
):
    job = await _start_job(case.runner, "user", session_id, thread_id)
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), timeout_seconds)
    assert job.status == JobStatus.completed, job.error_message
    return case.runner.saver.execution_admission_for_job(
        session_id,
        thread_id=thread_id,
        job_id=job.job_id,
    )


@pytest.mark.parametrize("start_outcome", ["completed", "failed", "cancelled"])
async def test_real_tool_body_waits_for_durable_start_and_producer_closes(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch, start_outcome: str,
) -> None:
    """真实 Provider/ToolNode 的同名同参调用只在各自 durable start 后进入工具体。"""
    case = runtime_case
    session_id, thread_id = await _session(case.runner)
    skill_name = "runtime-start-gate"
    skill_file = case.runner.workspace / ".boxteam" / "skills" / skill_name / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(
        f"---\nname: {skill_name}\ndescription: 工具开始持久化屏障\n---\n真实工具正文。\n",
        encoding="utf-8",
    )
    provider_ids = ("gate-first", "gate-second")
    entered = asyncio.Event()
    release = asyncio.Event()
    first_body = asyncio.Event()
    body_calls: list[tuple[str, str]] = []
    callback_tasks: dict[str, asyncio.Task[None]] = {}
    callback_runs: dict[str, str] = {}
    callback_closed: set[str] = set()
    processor_tasks: list[asyncio.Task[object]] = []
    processor_errors: list[BaseException] = []
    producer_tasks: set[asyncio.Task[object]] = set()
    stream_ids: list[str] = []
    start_error = RuntimeError("真实 tool.started 提交失败")
    create_agent = agent_factory.create_agent
    commit = MessageStreamWriter.commit
    on_tool_start = AgentRunLifecycleCallbackHandler.on_tool_start
    process = stream_retry.process_agent_event_stream

    def record_agent(*args, **kwargs):
        context = next(
            item._context for item in kwargs["middleware"]
            if isinstance(item, ToolInvocationContextMiddleware)
        )
        selected = next(tool for tool in kwargs["tools"] if tool.name == "skill_load")
        tool_body = selected.coroutine
        assert tool_body is not None

        @wraps(tool_body)
        async def record_body(*body_args, **body_kwargs):
            provider_id = context.require_tool_call_id()
            model_call_id = context.require_model_call_id()
            events = await case.runner.container.message_stream_store.list_events(
                session_id=session_id, turn_stream_id=stream_ids[0],
            )
            started = [
                event for event in events if event["type"] == "tool.started"
                and event["tool_call_id"] == f"{model_call_id}:tool-call:{provider_id}"
            ]
            assert len(started) == 1
            assert started[0]["tool_attempt_id"] == callback_runs[provider_id]
            body_calls.append((model_call_id, provider_id))
            if provider_id == provider_ids[0]:
                first_body.set()
            return await tool_body(*body_args, **body_kwargs)

        selected.coroutine = record_body
        return create_agent(*args, **kwargs)

    async def record_callback(handler, *args, **kwargs):
        provider_id = kwargs.get("tool_call_id")
        if provider_id not in provider_ids:
            return await on_tool_start(handler, *args, **kwargs)
        task = asyncio.current_task()
        assert task is not None
        callback_tasks[provider_id] = task
        callback_runs[provider_id] = str(kwargs["run_id"])
        try:
            return await on_tool_start(handler, *args, **kwargs)
        finally:
            callback_closed.add(provider_id)

    async def gated_commit(writer, event_type, payload=None, **kwargs):
        if event_type == "tool.started" and payload["tool_call_id"].endswith(
            f":tool-call:{provider_ids[1]}"
        ):
            stream_ids.append(writer.turn_stream_id)
            producer_tasks.update(
                task for task in asyncio.all_tasks()
                if getattr(task.get_coro(), "__qualname__", "").endswith(".consume_astream")
            )
            entered.set()
            await release.wait()
            if start_outcome == "failed":
                raise start_error
        elif event_type == "tool.started" and not stream_ids:
            stream_ids.append(writer.turn_stream_id)
        return await commit(writer, event_type, payload, **kwargs)

    async def record_processor(**kwargs):
        task = asyncio.current_task()
        assert task is not None
        processor_tasks.append(task)
        try:
            return await process(**kwargs)
        except BaseException as error:
            processor_errors.append(error)
            raise
        finally:
            # processor 返回前必须已经 join 真实 astream v2 producer 和 callback 树。
            assert producer_tasks and all(task.done() for task in producer_tasks)
            assert callback_closed == set(provider_ids)
            assert all(task.done() for task in callback_tasks.values())

    def model_reply(_payload):
        if len(case.model_requests) == 1:
            return [
                {"choices": [{"index": 0, "delta": {
                    "role": "assistant", "tool_calls": [
                        {"index": index, "id": provider_id, "type": "function",
                         "function": {"name": "skill_load", "arguments": json.dumps({
                             "name": skill_name, "mode": "snapshot",
                         })}}
                        for index, provider_id in enumerate(provider_ids)
                    ],
                }}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "工具完成。"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    monkeypatch.setattr(agent_factory, "create_agent", record_agent)
    monkeypatch.setattr(AgentRunLifecycleCallbackHandler, "on_tool_start", record_callback)
    monkeypatch.setattr(MessageStreamWriter, "commit", gated_commit)
    monkeypatch.setattr(stream_retry, "process_agent_event_stream", record_processor)
    case.response_chunks = model_reply
    job = await _start_job(case.runner, "user", session_id, thread_id)
    assert job.task is not None
    try:
        await asyncio.wait_for(entered.wait(), 8)
        await asyncio.wait_for(first_body.wait(), 3)
        model_call_id = case.model_dispatches[0].callback_model_call_id
        assert body_calls == [(model_call_id, provider_ids[0])]
        assert not callback_tasks[provider_ids[1]].done()
        events = await case.runner.container.message_stream_store.list_events(
            session_id=session_id, turn_stream_id=stream_ids[0],
        )
        assert [event["tool_call_id"] for event in events if event["type"] == "tool.started"] == [
            f"{model_call_id}:tool-call:{provider_ids[0]}"
        ]
        assert any(
            event["type"] == "tool_call.completed"
            and event["tool_call_id"] == f"{model_call_id}:tool-call:{provider_ids[1]}"
            for event in events
        )
        if start_outcome == "cancelled":
            processor_tasks[0].cancel("两次 commit 之间取消")
        else:
            release.set()
        await asyncio.wait_for(asyncio.shield(job.task), 12)
    finally:
        release.set()
        if not job.task.done():
            job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)

    assert callback_closed == set(provider_ids)
    assert all(task.done() for task in producer_tasks | set(callback_tasks.values()))
    assert len(case.compiled) == 1
    admission = case.model_admissions[0]
    assert (admission.session_id, admission.thread_id, admission.execution_id) == (
        session_id, thread_id, case.model_dispatches[0].snapshot.execution_id,
    )
    events = await case.runner.container.message_stream_store.list_events(
        session_id=session_id, turn_stream_id=stream_ids[0],
    )
    target_events = [
        event for event in events
        if event.get("tool_call_id") == f"{model_call_id}:tool-call:{provider_ids[1]}"
    ]
    if start_outcome == "completed":
        assert job.status == JobStatus.completed, job.error_message
        assert processor_errors == []
        assert body_calls == [(model_call_id, provider_id) for provider_id in provider_ids]
        assert len(case.model_requests) == len(case.model_dispatches) == 2
        tool_messages = [
            message for message in case.model_requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == list(provider_ids)
        assert [event["type"] for event in target_events] == [
            "tool_call.delta", "tool_call.completed", "tool.started", "tool.completed",
        ]
    else:
        assert body_calls == [(model_call_id, provider_ids[0])]
        assert len(case.model_requests) == len(case.model_dispatches) == 1
        assert not [event for event in target_events if event["type"] in {"tool.started", "tool.completed"}]
        results = [
            item for item in case.runner.saver._storage.storage.read_items(session_id, thread_id)
            if item.semantic_kind == "tool_result"
            and item.payload.get("tool_call_id") == f"{model_call_id}:tool-call:{provider_ids[1]}"
        ]
        assert results == []
        assert len(processor_errors) == 1
        if start_outcome == "failed":
            assert processor_errors[0] is start_error
            assert job.status == JobStatus.failed and str(start_error) in job.error_message
        else:
            assert isinstance(processor_errors[0], asyncio.CancelledError)
            assert job.status == JobStatus.cancelled


async def test_real_tool_callback_rejection_preserves_original_error_without_attempt(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 callback 拒绝必须先穿过 middleware，再关闭 producer；不能新建 start 等待。"""
    case = runtime_case
    session_id, thread_id = await _session(case.runner)
    provider_id = "callback-rejected"
    body_calls: list[str] = []
    callback_errors: list[BaseException] = []
    middleware_errors: list[BaseException] = []
    processor_errors: list[BaseException] = []
    producer_tasks: set[asyncio.Task[object]] = set()
    callback_tasks: list[asyncio.Task[object]] = []
    runtimes: list[MessageStreamRuntime] = []
    middleware_failed = asyncio.Event()
    create_agent = agent_factory.create_agent
    on_tool_start = AgentRunLifecycleCallbackHandler.on_tool_start
    wrap_tool_call = ToolInvocationContextMiddleware.awrap_tool_call
    tool_call_for_callback = MessageStreamRuntime.tool_call_for_callback
    process = stream_retry.process_agent_event_stream

    def record_agent(*args, **kwargs):
        selected = next(tool for tool in kwargs["tools"] if tool.name == "skill_load")
        tool_body = selected.coroutine
        assert tool_body is not None

        @wraps(tool_body)
        async def record_body(*body_args, **body_kwargs):
            body_calls.append(provider_id)
            return await tool_body(*body_args, **body_kwargs)

        selected.coroutine = record_body
        return create_agent(*args, **kwargs)

    async def reject_claimed_callback(handler, *args, **kwargs):
        if kwargs.get("tool_call_id") != provider_id:
            return await on_tool_start(handler, *args, **kwargs)
        task = asyncio.current_task()
        assert task is not None
        callback_tasks.append(task)
        producer_tasks.update(
            task for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__qualname__", "").endswith(".consume_astream")
        )
        runtime = runtimes[0]
        model_call_id = case.model_dispatches[0].callback_model_call_id
        # 上游拒绝重复 Provider 响应；这里用正式 claim 注入已经认领但尚未
        # start 的前置状态，随后执行原 callback 的真实拒绝分支。
        call_id = runtime.claim_tool_call_id(model_call_id, provider_id)
        assert call_id == f"{model_call_id}:tool-call:{provider_id}"
        assert runtime._active_tool_executions == runtime._pending_tool_starts == {}
        try:
            return await on_tool_start(handler, *args, **kwargs)
        except BaseException as error:
            callback_errors.append(error)
            raise

    async def record_middleware(middleware, request, handler):
        try:
            return await wrap_tool_call(middleware, request, handler)
        except BaseException as error:
            middleware_errors.append(error)
            middleware_failed.set()
            raise

    async def read_rejected_binding(runtime, callback_run_id):
        # 固定两条并发链的消费顺序，证明失败收口能在外层 close 之前结束。
        # 若它仍等待不存在的 start，这里会超时，不能被 producer 取消掩盖。
        await asyncio.wait_for(middleware_failed.wait(), 2)
        assert runtime._pending_tool_starts == {}
        return await tool_call_for_callback(runtime, callback_run_id)

    async def record_processor(**kwargs):
        runtimes.append(kwargs["message_stream_runtime"])
        try:
            return await process(**kwargs)
        except BaseException as error:
            processor_errors.append(error)
            raise
        finally:
            assert producer_tasks and all(task.done() for task in producer_tasks)
            assert callback_tasks and all(task.done() for task in callback_tasks)

    def model_reply(_payload):
        return [
            {"choices": [{"index": 0, "delta": {
                "role": "assistant", "tool_calls": [{
                    "index": 0, "id": provider_id, "type": "function",
                    "function": {"name": "skill_load", "arguments": json.dumps({
                        "name": "callback-rejected", "mode": "snapshot",
                    })},
                }],
            }}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]

    monkeypatch.setattr(agent_factory, "create_agent", record_agent)
    monkeypatch.setattr(AgentRunLifecycleCallbackHandler, "on_tool_start", reject_claimed_callback)
    monkeypatch.setattr(ToolInvocationContextMiddleware, "awrap_tool_call", record_middleware)
    monkeypatch.setattr(MessageStreamRuntime, "tool_call_for_callback", read_rejected_binding)
    monkeypatch.setattr(stream_retry, "process_agent_event_stream", record_processor)
    case.response_chunks = model_reply
    job = await _start_job(case.runner, "user", session_id, thread_id)
    assert job.task is not None
    try:
        await asyncio.wait_for(asyncio.shield(job.task), 12)
    finally:
        if not job.task.done():
            job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)

    assert body_calls == []
    assert len(case.compiled) == len(case.model_requests) == len(case.model_dispatches) == 1
    assert len(callback_errors) == len(middleware_errors) == len(processor_errors) == 1
    error = callback_errors[0]
    assert isinstance(error, RuntimeError) and "工具调用已经认领" in str(error)
    assert middleware_errors[0] is processor_errors[0] is error
    assert job.status == JobStatus.failed and str(error) in job.error_message
    runtime = runtimes[0]
    assert runtime._active_tool_executions == runtime._pending_tool_starts == {}
    assert runtime.writer is not None
    events = await case.runner.container.message_stream_store.list_events(
        session_id=session_id, turn_stream_id=runtime.writer.turn_stream_id,
    )
    assert not [event for event in events if event["type"] in {"tool.started", "tool.completed"}]
    assert not [
        item for item in case.runner.saver._storage.storage.read_items(session_id, thread_id)
        if item.semantic_kind == "tool_result"
    ]


async def _child(case: RuntimeCase, session_id: str, *, creation_key: str = "runtime-child") -> str:
    container = case.runner.container
    sessions_root = case.runner.workspace / ".boxteam" / "sessions"
    control = SessionControlStore(
        container.session_service.path_resolver.resolve_session_node(session_id)
        / "session-control.sqlite"
    )
    catalog = SessionCatalogStore(
        case.runner.workspace / ".boxteam" / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    try:
        result = await ThreadCreationService(
            store=catalog,
            control_store=control,
            sessions_root=sessions_root,
            workspace_id=container.workspace_service.workspace_id,
            compute_capability_profile_hash=compute_capability_profile_hash,
        ).create(
            idempotency_key=creation_key,
            session_id=session_id,
            thread_id=None,
            delegation_id=None,
            initial_state="idle",
            session_metadata={
                "graph_binding": asdict(DEEP_AGENT_GRAPH_BINDING),
                "capability_profile": dict(DEEP_AGENT_CAPABILITY_PROFILE),
                "task_seed": None,
                "task_reference": None,
            },
            artifact_manifest={},
        )
        return result.child_thread_id
    finally:
        catalog.close()
        control.close()


async def _lease(case: RuntimeCase, admission: ExecutionAdmission, **overrides):
    return await case.runner.container.agent_execution_service._acquire_step_agent(
        admission=admission,
        agent_id="default",
        execution_overrides=overrides,
        model_visibility_overrides={},
        preferred_provider_id=None,
        include_team_tools=False,
    )


@pytest.mark.asyncio
async def test_real_runner_reuses_main_graph_with_fresh_admissions_and_isolates_child(
    runtime_case: RuntimeCase,
):
    case = runtime_case
    session_id, main_thread_id = await _session(case.runner)
    first = await _complete_job(case, session_id, main_thread_id)
    second = await _complete_job(case, session_id, main_thread_id)
    assert first.execution_id != second.execution_id and first.turn_id != second.turn_id
    assert len(case.compiled) == 1
    assert case.bindings == [DEEP_AGENT_GRAPH_BINDING]
    assert case.model_admissions == [first, second]
    main_pair, main_graph, main_scope = case.compiled[0]
    assert main_pair == (session_id, main_thread_id)
    assert main_scope.state == "open" and not case.reactors[0].released

    child_id = await _child(case, session_id)
    child = await _complete_job(case, session_id, child_id)
    assert len(case.compiled) == 2
    assert case.compiled[1][0] == (session_id, child_id)
    assert case.compiled[1][1] is not main_graph
    assert case.model_admissions == [first, second, child]
    assert current_execution_admission() is None
    tracker = case.tracker
    assert tracker.snapshot(session_id, main_thread_id).execution_state == "idle"
    assert tracker.snapshot(session_id, child_id).execution_state == "idle"


async def test_real_catalog_delete_closes_exact_session_graphs_before_isolation(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
):
    """同一删除关闭 main/child 原代；后续 sweep 与其它 Session 仍可用。"""
    case = runtime_case
    container = case.runner.container
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id)
    cold_child = await _child(case, session_id, creation_key="runtime-delete-cold")
    case.tracker.record_activity(session_id, cold_child)
    assert case.tracker.snapshot(session_id, cold_child).residency == "cold"
    other_session, other_thread = await _session(case.runner)
    for owner_session, thread in [(session_id, main), (session_id, child), (other_session, other_thread)]:
        await _complete_job(case, owner_session, thread)
    selected = case.compiled[:2]
    other_scope = case.compiled[2][2]
    other_generation = case.tracker.snapshot(other_session, other_thread).generation
    catalog = container.session_catalog_service
    intent = create_catalog_intent(catalog, "delete_session", target_node_id=session_id)
    fenced = asyncio.Event()

    async def observe(operation_id: str, phase: str, *, details: Mapping[str, object] | None = None) -> None:
        del details
        if operation_id == intent.client_operation_id and phase == f"deletion.fence_closed/{session_id}":
            assert all(scope.state == "closed" for _pair, _graph, scope in selected)
            assert all(reactor.released for reactor in case.reactors[:2])
            assert all(pair[0] != session_id for pair in case.tracker._states)
            assert other_scope.state == "open"
            assert case.tracker.is_current_generation(other_session, other_thread, other_generation)
            fenced.set()

    monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe)
    scope = local_navigation_scope(catalog.workspace_id)
    await catalog.operations.start()
    try:
        await catalog.operations.enqueue(NavigationMutationEnqueueRequest(intents=[intent]), scope)
        receipt = await asyncio.wait_for(catalog.operations.await_terminal(intent.client_operation_id, scope), 10)
        assert receipt.state == "committed", receipt.error_detail
        await asyncio.wait_for(fenced.wait(), 10)
        store = container.session_service.path_resolver.catalog_store
        for _ in range(300):
            if store.get_subtree_delete_record(intent.client_operation_id).state == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("同一删除 journal 未完成物理排空")
        with pytest.raises(KeyError):
            store.get_node(session_id)
        await case.tracker.sweep()
        await _complete_job(case, other_session, other_thread)
        assert len(case.compiled) == 3
        assert other_scope.state == "open"
    finally:
        await catalog.operations.stop()


async def test_real_initialized_child_file_tool_reaches_canonical_commit(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实委派 child 初始化后的文件工具必须提交同一实时 canonical result。"""
    case = runtime_case
    session_id, main_thread_id = await _session(case.runner)
    (case.runner.workspace / "runtime-tool-probe.txt").write_text("真实文件工具结果。\n")
    phases: list[tuple[str, str]] = []
    events: list[tuple[str, str]] = []
    observe = stream_bindings.observe_phase
    iterate = event_processor.iter_agent_events

    async def record_phase(operation_id, phase, **kwargs):
        phases.append((operation_id, phase))
        await observe(operation_id, phase, **kwargs)

    async def record_events(**kwargs):
        async for event in iterate(**kwargs):
            events.append((event.get("event", ""), event.get("name", "")))
            yield event

    model_calls: dict[str, int] = {}

    def model_reply(_payload):
        admission = case.model_admissions[-1]
        call = model_calls.get(admission.execution_id, 0) + 1
        model_calls[admission.execution_id] = call
        if admission.thread_id == main_thread_id and call == 1:
            return [
                {"choices": [{"index": 0, "delta": {
                    "role": "assistant", "tool_calls": [{
                        "index": 0, "id": "runtime-child-delegate", "type": "function",
                        "function": {"name": "task", "arguments": json.dumps({
                            "description": "独立核对文件，初始化后等待直接对话。",
                            "subagent_type": "general-purpose",
                        })},
                    }],
                }}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        if admission.thread_id != main_thread_id and admission.execution_origin == "user_turn" and call == 1:
            return [
                {"choices": [{"index": 0, "delta": {
                    "role": "assistant", "reasoning_content": "先核对工作区文件。", "tool_calls": [{
                        "index": 0, "id": "runtime-file-read", "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "runtime-tool-probe.txt"})},
                    }],
                }}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "真实文件读取完成。"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    monkeypatch.setattr(stream_bindings, "observe_phase", record_phase)
    monkeypatch.setattr(event_processor, "iter_agent_events", record_events)
    case.response_chunks = model_reply
    await _complete_job(case, session_id, main_thread_id, timeout_seconds=20)
    children = await case.runner.container.session_service.list_child_threads(session_id)
    assert len(children.items) == 1
    thread_id = children.items[0].thread_id
    initial_job = next(job for job in case.runner.container.job_service._jobs.values() if job.thread_id == thread_id)
    assert initial_job.task is not None
    await asyncio.wait_for(asyncio.shield(initial_job.task), 20)
    assert initial_job.status == JobStatus.completed, initial_job.error_message
    initial_generation = case.tracker.snapshot(session_id, thread_id).generation
    await _complete_job(case, session_id, thread_id, timeout_seconds=20)
    assert len(case.model_requests) == 5
    assert len(case.compiled) == 2
    assert case.tracker.snapshot(session_id, thread_id).generation == initial_generation
    tool_messages = [message for message in case.model_requests[-1]["messages"] if message.get("role") == "tool"]
    assert len(tool_messages) == 1 and "真实文件工具结果。" in tool_messages[0]["content"]
    call_id = case.model_dispatches[-2].callback_model_call_id
    assert (f"{call_id}:tool-call:runtime-file-read", "canonical.tool_result.before_commit") in phases, events


async def test_real_child_runtime_default_and_explicit_same_provider_share_generation(
    runtime_case: RuntimeCase,
) -> None:
    """initial 默认模型和直接 Turn 的同一显式模型不能重新编译 child。"""
    case = runtime_case
    session_id, _main = await _session(case.runner)
    child_id = await _child(case, session_id)
    service = case.runner.container.agent_execution_service
    provider_id = case.runner.container.config_service.resolve_agent_provider_id("default")
    generations = []
    for origin, preference in (("internal", None), ("user", provider_id)):
        lease = await service._acquire_step_agent(
            admission=_admit(case.runner, session_id, child_id, origin),
            agent_id="default", execution_overrides={}, model_visibility_overrides={},
            preferred_provider_id=preference, include_team_tools=False,
        )
        generations.append(lease.generation)
        lease.release()
    assert generations[0] == generations[1]
    assert len(case.compiled) == 1


@pytest.fixture
async def thread_message_history_client(runtime_case: RuntimeCase):
    """真实消息与历史 HTTP 边界；只替换鉴权和请求身份。"""
    container = runtime_case.runner.container
    application = FastAPI()
    application.include_router(messages_api.router, prefix="/api/v1")
    application.include_router(session_turns_api.router, prefix="/api/v1")
    application.dependency_overrides[get_session_orchestrator] = lambda: container.session_orchestrator
    application.dependency_overrides[get_session_turn_history_service] = lambda: container.session_turn_history_service
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    application.dependency_overrides[get_request_id] = lambda: "request-queued-owner"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        yield client


async def test_real_http_queued_child_turn_is_readable_and_fenced_from_active_execution(
    runtime_case: RuntimeCase, thread_message_history_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 接受 queued root 后立即可读，旧执行继续工具循环仍排除它。"""
    case = runtime_case
    container = case.runner.container
    session_id, _main_thread_id = await _session(case.runner)
    child = await _child(case, session_id, creation_key="queued-http-child")
    (case.runner.workspace / "runtime-queued-probe.txt").write_text("queued 工具边界。\n")
    entered, release = asyncio.Event(), asyncio.Event()
    observe = stream_bindings.observe_phase

    async def pause_result(operation_id, phase, **kwargs):
        if phase == "canonical.tool_result.before_commit" and operation_id.endswith(":tool-call:runtime-queued-read"):
            entered.set()
            await release.wait()
        await observe(operation_id, phase, **kwargs)

    calls: dict[str, int] = {}

    def model_reply(_payload):
        admission = case.model_admissions[-1]
        call = calls.get(admission.execution_id, 0) + 1
        calls[admission.execution_id] = call
        if len(calls) == 1 and call == 1:
            return [
                {"choices": [{"index": 0, "delta": {
                    "role": "assistant", "reasoning_content": "先读取当前输入的文件。", "tool_calls": [{
                        "index": 0, "id": "runtime-queued-read", "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "runtime-queued-probe.txt"})},
                    }],
                }}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "当前 FIFO 输入已完成。"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    monkeypatch.setattr(stream_bindings, "observe_phase", pause_result)
    case.response_chunks = model_reply
    client = thread_message_history_client
    endpoint = f"/api/v1/sessions/{session_id}/threads/{child}"
    first_prompt, queued_prompt = "第一条继续真实工具循环。", "第二条仍在 FIFO 等待，不得泄露。"
    jobs = []
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        try:
            first_response = await client.post(endpoint + "/messages", json={"message": {"content": first_prompt}, "run": {}})
            assert first_response.status_code == 200, first_response.text
            first = first_response.json()["data"]
            jobs.append(container.job_service._jobs[first["job_id"]])
            await asyncio.wait_for(entered.wait(), 8)
            compiled_count = len(case.compiled)
            second_response = await client.post(endpoint + "/messages", json={"message": {"content": queued_prompt}, "run": {}})
            assert second_response.status_code == 200, second_response.text
            second = second_response.json()["data"]
            queued_job = container.job_service._jobs[second["job_id"]]
            jobs.append(queued_job)
            assert second["status"] == "queued" and queued_job.task is None
            assert container.job_service._thread_current_job[(session_id, child)] == first["job_id"]
            history = await client.post(endpoint + "/history", json={"turn_ids": [second["turn_id"]]})
            assert history.status_code == 200, history.text
            accepted = history.json()["data"]["items"]
            assert len(accepted) == 1
            assert accepted[0]["status"] == "accepted"
            assert accepted[0]["job_id"] == second["job_id"]
            assert accepted[0]["source_message_ids"] == [second["message_id"]]
            assert accepted[0]["user_messages"][0]["content"] == queued_prompt
            assert len(case.compiled) == compiled_count
            assert all(value.job_id != second["job_id"] for value in case.model_admissions)
            storage = case.runner.saver._storage.storage
            with storage._connect(child, session_id=session_id, read_only=True) as connection:
                root = connection.execute("SELECT root_input_item_sequence,initial_execution_id FROM turn_records WHERE turn_id=?", (second["turn_id"],)).fetchone()
                assert root is not None
                assert connection.execute("SELECT COUNT(*) FROM context_assemblies WHERE execution_id=?", (root[1],)).fetchone()[0] == 0
                assert connection.execute("SELECT COUNT(*) FROM model_calls WHERE execution_id=?", (root[1],)).fetchone()[0] == 0
            release.set()
            for job in jobs:
                await asyncio.wait_for(asyncio.shield(job.task), 20)
                assert job.status == JobStatus.completed, job.error_message
            first_requests = [payload for payload, admission in zip(case.model_requests, case.model_admissions, strict=True) if admission.job_id == first["job_id"]]
            assert len(first_requests) == 2
            assert first_prompt in json.dumps(first_requests[-1]["messages"], ensure_ascii=False)
            assert queued_prompt not in json.dumps(first_requests[-1]["messages"], ensure_ascii=False)
            queued_requests = [payload for payload, admission in zip(case.model_requests, case.model_admissions, strict=True) if admission.job_id == second["job_id"]]
            assert len(queued_requests) == 1
            assert queued_prompt in json.dumps(queued_requests[0]["messages"], ensure_ascii=False)
            with storage._connect(child, session_id=session_id, read_only=True) as connection:
                first_sequences = [row[0] for row in connection.execute("SELECT item_sequence FROM item_catalog WHERE turn_id=? AND turn_scope='turn_member'", (first["turn_id"],))]
                assert any(sequence < root[0] for sequence in first_sequences)
                assert any(sequence > root[0] for sequence in first_sequences)
                assert connection.execute("SELECT COUNT(*) FROM turn_acceptances WHERE accepted_ingress_id=?", (second["message_id"],)).fetchone()[0] == 1
        finally:
            release.set()


@pytest.fixture
async def cold_message_stream_client(runtime_case: RuntimeCase):
    """局部 HTTP Integration：仅鉴权/请求 ID 是替身，读取真实 Runner 的冷持久流。"""
    store = MessageStreamStore(path_resolver=runtime_case.runner.container.session_service.path_resolver)
    application = FastAPI()
    application.include_router(message_stream_api.router, prefix="/api/v1")
    application.dependency_overrides[get_message_stream_store] = lambda: store
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    application.dependency_overrides[get_request_id] = lambda: "request-message-owner"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        yield client


async def test_real_runner_message_v1_child_owner_matches_model_admission_and_rejects_main_selector(
    runtime_case: RuntimeCase, cold_message_stream_client: httpx.AsyncClient, request: pytest.FixtureRequest,
) -> None:
    case = runtime_case
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id)
    admission = await _complete_job(case, session_id, child, timeout_seconds=20)
    before = len(case.compiled)
    turn_id = admission.turn_id
    assert turn_id is not None
    root = f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream"
    client = cold_message_stream_client
    snapshot = await client.get(root + "/snapshot", params={"thread_id": child})
    assert snapshot.status_code == 200, snapshot.text
    frozen = snapshot.json()["data"]
    assert frozen["thread_id"] == child and frozen["turn_id"] == turn_id
    selector = {"thread_id": child, "turn_stream_id": frozen["turn_stream_id"]}
    events = await client.get(root + "/events", params=selector)
    assert events.status_code == 200, events.text
    records = events.json()["data"]
    assert records and all((event["session_id"], event["thread_id"], event["turn_id"]) == (
        session_id, child, turn_id) for event in records)
    model_ids = {event["model_call_id"] for event in records if event["type"] == "model.started"}
    assert model_ids
    storage = case.runner.saver._storage
    with storage.storage._connect(child, session_id=session_id, read_only=True) as connection:
        for model_id in model_ids:
            assert connection.execute(
                "SELECT m.session_id,m.thread_id,m.execution_id,e.turn_id FROM model_calls m "
                "JOIN executions e ON e.session_id=m.session_id AND e.thread_id=m.thread_id "
                "AND e.execution_id=m.execution_id WHERE m.model_call_id=?", (model_id,),
            ).fetchone() == (session_id, child, admission.execution_id, turn_id)
    sse = await client.get(root, params=selector)
    assert sse.status_code == 200, sse.text
    replay = [json.loads(line.removeprefix("data: ")) for line in sse.text.splitlines() if line.startswith("data: ")]
    assert replay and all(event["thread_id"] == child for event in replay)
    assert sse.headers["X-Message-Stream-ID"] == frozen["turn_stream_id"]
    cursor = records[1]["event_seq"]
    resumed = await client.get(root + "/events", params={**selector, "after_seq": cursor})
    assert resumed.status_code == 200, resumed.text
    assert all(event["thread_id"] == child and event["event_seq"] > cursor for event in resumed.json()["data"])
    for suffix in ("", "/snapshot", "/events"):
        for stream_id in (None, frozen["turn_stream_id"]):
            wrong = {"thread_id": main}
            if stream_id is not None:
                wrong["turn_stream_id"] = stream_id
            rejected = await client.get(root + suffix, params=wrong)
            assert rejected.status_code == 404, rejected.text
    assert len(case.compiled) == before
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    context.artifacts_dir.joinpath("message-v1-real-child-owner.json").write_text(json.dumps({
        "session_id": session_id, "thread_id": child, "execution_id": admission.execution_id,
        "turn_id": turn_id, "model_call_ids": sorted(model_ids), "snapshot": frozen,
        "events": records, "sse_events": replay, "compiled_before_after": [before, len(case.compiled)],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def compaction_runtime_call_profile(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """只观察原压缩节点的真实调用；嵌套耗时必须按边界解读，不能相加。"""
    if request.node.name != "test_real_runner_compaction_seals_candidates_then_continues_and_forks_selected_context":
        yield
        return
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    path = context.artifacts_dir / f"compaction-runtime-calls-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.jsonl"
    origin = perf_counter_ns()
    parents: ContextVar[tuple[int, ...]] = ContextVar("compaction_runtime_calls", default=())
    async_callers: ContextVar[dict[str, object] | None] = ContextVar("compaction_async_caller", default=None)
    lock = threading.Lock()
    counts: dict[str, int] = {}
    call_id = 0

    def size(value: object) -> int | None:
        # 不展开迭代器，也不把业务正文或密钥写入统计。
        return len(value) if isinstance(value, Sized) else None

    with path.open("x", encoding="utf-8", buffering=1) as stream:
        def write(record: dict[str, object]) -> None:
            with lock:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        def install(owner: type, name: str) -> None:
            original: Callable[..., object] = getattr(owner, name)
            parameters = signature(original)

            @wraps(original)
            def measured(instance: object, *args: object, **kwargs: object) -> object:
                nonlocal call_id
                parent_ids = parents.get()
                with lock:
                    call_id += 1
                    identity = call_id
                    counts[name] = counts.get(name, 0) + 1
                    ordinal = counts[name]
                admission = current_execution_admission()
                bound = parameters.bind(instance, *args, **kwargs)
                bound.apply_defaults()
                arguments = bound.arguments
                if name in {"load_checkpoint", "checkpoint_values"}:
                    scale = {"checkpoint_json_chars": size(arguments["index"].checkpoint_json)}
                elif name == "_read_messages":
                    scale = {"message_sequences": size(arguments["sequences"])}
                else:
                    scale = {
                        key: size(arguments[key])
                        for key in ("request_messages", "messages", "items", "committed_items", "request_only_content")
                        if key in arguments
                    }
                checkpoint_ns = arguments.get("checkpoint_ns")
                if checkpoint_ns is None:
                    config = arguments.get("config")
                    snapshot = arguments.get("snapshot")
                    checkpoint_ns = (
                        config.get("configurable", {}).get("checkpoint_ns", "")
                        if isinstance(config, Mapping)
                        else snapshot.checkpoint_ns if snapshot is not None else None
                    )
                caller = sys._getframe(1)
                caller_identity = {
                    "module": caller.f_globals.get("__name__"),
                    "symbol": caller.f_code.co_qualname,
                    "line": caller.f_lineno,
                } if name == "get_tuple" else None
                del caller
                entered = perf_counter_ns()
                write({
                    "event": "begin", "call_id": identity, "method": name, "call_ordinal": ordinal,
                    "parent_call_id": parent_ids[-1] if parent_ids else None, "depth": len(parent_ids),
                    "thread_id": threading.get_ident(), "at_seconds": (entered - origin) / 1e9,
                    "execution_id": admission.execution_id if admission is not None else None,
                    "turn_id": admission.turn_id if admission is not None else None,
                    "checkpoint_ns": checkpoint_ns,
                    "include_execution_context": arguments.get("include_execution_context"),
                    "summary_candidate": arguments.get("summary_candidate") is not None,
                    "input_scale": scale,
                    "requested_channels": list(arguments["channels"]) if name == "get_delta_channel_history" else None,
                    "caller": caller_identity,
                    "async_caller": async_callers.get() if name == "get_tuple" else None,
                })
                token = parents.set((*parent_ids, identity))
                returned: object = None
                succeeded = False
                started = perf_counter_ns()
                try:
                    returned = original(instance, *args, **kwargs)
                    succeeded = True
                    return returned
                finally:
                    ended = perf_counter_ns()
                    parents.reset(token)
                    messages = returned
                    if isinstance(returned, Mapping):
                        channels = returned.get("channel_values", returned)
                        messages = channels.get("messages") if isinstance(channels, Mapping) else None
                    write({
                        "event": "end", "call_id": identity, "method": name,
                        "method_started_seconds": (started - origin) / 1e9,
                        "at_seconds": (ended - origin) / 1e9, "elapsed_seconds": (ended - started) / 1e9,
                        "returned": succeeded, "output_messages": size(messages),
                    })

            monkeypatch.setattr(owner, name, measured)

        original_aget_tuple = RolloutCheckpointSaver.aget_tuple

        @wraps(original_aget_tuple)
        async def observe_aget_tuple(instance, config):
            caller = sys._getframe(1)
            token = async_callers.set({
                "module": caller.f_globals.get("__name__"),
                "symbol": caller.f_code.co_qualname,
                "line": caller.f_lineno,
            })
            del caller
            try:
                return await original_aget_tuple(instance, config)
            finally:
                async_callers.reset(token)

        monkeypatch.setattr(RolloutCheckpointSaver, "aget_tuple", observe_aget_tuple)
        install(RolloutCheckpointQueriesMixin, "load_checkpoint")
        install(RolloutCheckpointQueriesMixin, "checkpoint_values")
        install(RolloutCheckpointQueriesMixin, "committed_context_items")
        install(RolloutMessageMaterializerMixin, "_read_messages")
        install(ContextDispatchOwnerMixin, "prepare_context_for_provider")
        for name in (
            "get_tuple", "get_delta_channel_history", "put", "ensure_request_items", "_compose_committed_context_plan",
            "load_context_source_rebuild_state", "register_context_contribution",
            "_request_content_for_plan", "_draft_request_body", "seal_context_plan",
            "project_context_plan_to_provider", "project_context_plan_to_native",
            "require_provider_resource_activation", "read_resource_activation_payload",
        ):
            install(RolloutCheckpointSaver, name)
        write({"event": "opened", "started_at": datetime.now(UTC).isoformat(), "node_id": request.node.nodeid,
               "elapsed_semantics": "包含嵌套调用；按 parent_call_id 解析，不累加为总耗时"})
        try:
            yield
        finally:
            write({"event": "closed", "at_seconds": (perf_counter_ns() - origin) / 1e9, "call_counts": counts})
            print(f"compaction runtime call profile: {path}")


@pytest.fixture
def runner_compaction_gateway_context(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    gateway_token = set_current_gateway_id(COMPACTION_GATEWAY_ID)
    request_token = set_current_request_id("compaction-fork-request")
    for kind in ("terminal", "browser"):
        provider = runtime_case.runner.container.session_resource_service._provider_registry.get(kind)
        monkeypatch.setattr(provider._owner._transport, "list_resources", AsyncMock(return_value=[]))
    try:
        yield
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


async def test_real_runner_compaction_seals_candidates_then_continues_and_forks_selected_context(
    runtime_case: RuntimeCase, cold_message_stream_client: httpx.AsyncClient,
    runner_compaction_gateway_context: None, gateway_skill_publication, fork_deadlines,
) -> None:
    """仅替换 Provider 响应，经过真实准入、摘要重试、checkpoint、冷流和 fork 后继。"""
    case = runtime_case
    assert gateway_skill_publication is case.runner.container.resource_platform.gateway_skill_sources
    session_id, thread_id = await _session(case.runner)
    phase = "history"
    history_count = 0
    candidate_count = 0
    compaction_calls = 0
    summary_round = 1
    summary_text = "压缩事实：历史工作已完成；继续核对真实后继与分支。"
    second_summary_text = "再次压缩事实：已合并前次摘要与新增工作；继续核对最新分支。"

    def text_chunks(content: str, *, reasoning: bool = False, tokens: int = 100):
        return [
            {"choices": [{"index": 0, "delta": {
                "role": "assistant", "reasoning_content" if reasoning else "content": content,
            }}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": tokens - 1, "completion_tokens": 1, "total_tokens": tokens}},
        ]

    def model_reply(_payload):
        nonlocal history_count, candidate_count, compaction_calls
        dispatch = case.model_dispatches[-1]
        if dispatch.snapshot.selection_policy == "summary_candidate":
            candidate_count += 1
            if candidate_count % 2 == 1:
                return text_chunks("候选私有思考，不应进入用户正文。", reasoning=True)
            return text_chunks(summary_text if summary_round == 1 else second_summary_text)
        if phase == "history":
            history_count += 1
            return text_chunks(f"历史段{history_count}：" + (f"阶段{history_count}已完成的详细记录。" * 300), tokens=90000)
        if phase == "compact":
            compaction_calls += 1
            if compaction_calls == 1:
                return [
                    {"choices": [{"index": 0, "delta": {
                        "role": "assistant", "tool_calls": [{"index": 0, "id": f"runtime-compact-{summary_round}", "type": "function",
                            "function": {"name": "compact_conversation", "arguments": "{}"}}],
                    }}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                     "usage": {"prompt_tokens": 89999, "completion_tokens": 1, "total_tokens": 90000}},
                ]
            return text_chunks("压缩后正式回答。")
        return text_chunks("后继正式回答。")

    case.response_chunks = model_reply
    history_admissions = []
    for _ in range(5):
        history_admissions.append(await _complete_job(case, session_id, thread_id, timeout_seconds=25))
    assert history_count == len(history_admissions) == 5
    assert case.model_admissions == history_admissions
    for history, dispatch in zip(history_admissions, case.model_dispatches, strict=True):
        assert (dispatch.snapshot.session_id, dispatch.snapshot.thread_id, dispatch.snapshot.turn_id) == (
            history.session_id, history.thread_id, history.turn_id,
        ) == (session_id, thread_id, history.turn_id)
    phase = "compact"
    admission = await _complete_job(case, session_id, thread_id, timeout_seconds=30)
    assert all(observed == admission for observed in case.model_admissions[5:])
    candidates = [dispatch for dispatch in case.model_dispatches if dispatch.snapshot.selection_policy == "summary_candidate"]
    assert len(candidates) == 2 and candidate_count == 2 and compaction_calls == 2
    assert len({dispatch.snapshot.assembly_id for dispatch in candidates}) == 2
    assert len({dispatch.callback_model_call_id for dispatch in candidates}) == 2
    final = case.model_dispatches[-1].snapshot
    assert final.seal_binding.prefix.state.epoch.reason.value == "compaction"
    assert final.seal_binding.prefix.consumed_transition is not None
    assert all(dispatch.snapshot.seal_binding.prefix.consumed_transition is None for dispatch in candidates)
    candidate_requests = [payload for payload, dispatch in zip(case.model_requests, case.model_dispatches, strict=True)
                          if dispatch.snapshot.selection_policy == "summary_candidate"]
    assert candidate_requests[0]["tools"] and not candidate_requests[1].get("tools")
    assert all("历史段1：" in json.dumps(payload["messages"], ensure_ascii=False) for payload in candidate_requests)
    assert all("历史段5：" not in json.dumps(payload["messages"], ensure_ascii=False) for payload in candidate_requests)
    assert all("runtime-compact" not in json.dumps(payload["messages"], ensure_ascii=False) for payload in candidate_requests)
    saver = case.runner.saver
    items = saver._storage.storage.read_items(session_id, thread_id)
    summaries = [item for item in items if item.semantic_kind == "compaction_summary"]
    assert len(summaries) == 1 and summaries[0].turn_id == admission.turn_id and summaries[0].turn_scope == "turn_member"
    first_summary_wire = json.dumps(summaries[0].payload["summary"], ensure_ascii=False)
    assert json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False).count(first_summary_wire) == 1
    assert all("候选私有思考" not in json.dumps(item.payload, ensure_ascii=False) for item in items)
    source_group = next(group for group in summary_source_groups(items).values()
                        if group[-1].semantic_kind == "assistant_output")
    source_message = project_summary_source(source_group)
    for sidecar in ({"function_call": {"name": "forged", "arguments": "{}"}},
                    {"tool_calls": [{"id": "forged", "type": "function", "function": {"name": "forged", "arguments": "{}"}}]}):
        with pytest.raises(ValueError, match="non canonical|非 canonical wire sidecar"):
            validate_summary_source(source_message.model_copy(update={"additional_kwargs": sidecar}), source_group)
    with saver._storage.storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        root, final_item = connection.execute(
            "SELECT root_input_item_id,final_item_id FROM turn_records WHERE turn_id=?", (admission.turn_id,),
        ).fetchone()
        assert root != summaries[0].item_id and final_item != summaries[0].item_id
        transitions = connection.execute("SELECT reason,status FROM context_epoch_transitions WHERE reason='compaction'").fetchall()
        assert transitions == [("compaction", "consumed")]
        for dispatch in candidates:
            cuts = connection.execute(
                "SELECT source_cut_position_kind,source_cut_item_id FROM context_assembly_selections WHERE assembly_id=?",
                (dispatch.snapshot.assembly_id,),
            ).fetchall()
            assert cuts and all(cut == (None, None) for cut in cuts)
    cold = RolloutCheckpointSaver(case.runner.workspace / ".boxteam" / "sessions")
    for dispatch in candidates:
        frozen_candidate = cold.get_context_assembly(
            session_id, thread_id, assembly_id=dispatch.snapshot.assembly_id,
        )
        assert frozen_candidate.to_dict() == dispatch.snapshot.to_dict()
        messages, _, losses = cold.project_context_plan_to_provider(
            session_id, thread_id, frozen_candidate.as_sealed_plan(), target_format="chat_completions",
            include_runtime_notices=True, include_summaries=True,
        )
        assert not losses
        assert "历史段1：" in str([message.content for message in messages])
        assert "历史段5：" not in str([message.content for message in messages])
    with session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id):
        checkpoint = await cold.aget_tuple(build_checkpoint_config(thread_id))
    values = checkpoint.checkpoint["channel_values"]
    event = values["_summarization_event"]
    prefix_ids = {message.id for message in event["cache_prefix_messages"]}
    compacted_messages = [message for message in values["messages"][:event["cutoff_index"]] if message.id not in prefix_ids]
    compacted_ids = {message.id for message in compacted_messages}
    compacted_markers = {
        f"历史段{index}：" for index in range(1, 6)
        if any(f"历史段{index}：" in str(message.content) for message in compacted_messages)
    }
    assert compacted_ids and compacted_markers
    effective = apply_summarization_event(values["messages"], event)
    assert sum(message.id == summaries[0].projection_identity.projection_message_id for message in effective) == 1
    assert prefix_ids <= {message.id for message in effective}
    assert compacted_ids.isdisjoint(message.id for message in effective)
    assert all(marker not in str(message.content) for message in effective for marker in compacted_markers)
    root_url = f"/api/v1/sessions/{session_id}/turns/{admission.turn_id}/message-stream"
    response = await cold_message_stream_client.get(root_url + "/snapshot", params={"thread_id": thread_id})
    assert response.status_code == 200, response.text
    stream_id = response.json()["data"]["turn_stream_id"]
    response = await cold_message_stream_client.get(root_url + "/events", params={"thread_id": thread_id, "turn_stream_id": stream_id})
    assert response.status_code == 200, response.text
    records = response.json()["data"]
    started = {event["model_call_id"] for event in records if event["type"] == "model.started"}
    started_records = {record["model_call_id"]: record for record in records if record["type"] == "model.started"}
    assert {dispatch.callback_model_call_id for dispatch in candidates} <= started
    candidate_completed = {record["model_call_id"]: record for record in records
                           if record["type"] == "model.completed"
                           and record["model_call_id"] in {dispatch.callback_model_call_id for dispatch in candidates}}
    assert set(candidate_completed) == {dispatch.callback_model_call_id for dispatch in candidates}
    for record in candidate_completed.values():
        assert (record["session_id"], record["thread_id"], record["turn_id"]) == (session_id, thread_id, admission.turn_id)
        assert (record["job_id"], record["workspace_id"], record["turn_stream_id"]) == (
            admission.job_id, case.runner.container.workspace_service.workspace_id, stream_id,
        )
        started_record = started_records[record["model_call_id"]]
        assert started_record["event_seq"] < record["event_seq"]
        assert record["payload"]["attempt"] == started_record["payload"]["attempt"]
        assert record["payload"]["outcome"] == "accepted"
        assert record["payload"]["reason"] == "摘要候选传输完成；正文是否可用由压缩中间件校验"
    assert "候选私有思考" not in json.dumps(records, ensure_ascii=False)
    assert summary_text not in json.dumps(records, ensure_ascii=False)
    phase = "successor"
    await _complete_job(case, session_id, thread_id, timeout_seconds=25)
    assert json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False).count(first_summary_wire) == 1
    assert all(marker not in json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False) for marker in compacted_markers)

    # 旧 event 仍在的同一 thread 再积累真实历史，再由新 Turn 发起第二次压缩。
    phase = "history"
    history_start = len(case.model_admissions)
    dispatch_start = len(case.model_dispatches)
    for _ in range(5):
        history_admissions.append(await _complete_job(case, session_id, thread_id, timeout_seconds=25))
    assert history_count == len(history_admissions) == 10
    assert case.model_admissions[history_start:] == history_admissions[5:]
    assert len({history.turn_id for history in history_admissions}) == 10
    assert len({history.execution_id for history in history_admissions}) == 10
    assert len({history.job_id for history in history_admissions}) == 10
    for history, dispatch in zip(history_admissions[5:], case.model_dispatches[dispatch_start:], strict=True):
        assert (dispatch.snapshot.session_id, dispatch.snapshot.thread_id, dispatch.snapshot.turn_id) == (
            history.session_id, history.thread_id, history.turn_id,
        ) == (session_id, thread_id, history.turn_id)
    with session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id):
        before_second = await cold.aget_tuple(build_checkpoint_config(thread_id))
    assert before_second.checkpoint["channel_values"]["_summarization_event"] == event
    assert before_second.checkpoint["channel_values"]["messages"][:event["cutoff_index"]] == values["messages"][:event["cutoff_index"]]
    phase = "compact"
    summary_round = 2
    compaction_calls = 0
    second_start = len(case.model_admissions)
    second_admission = await _complete_job(case, session_id, thread_id, timeout_seconds=30)
    assert second_admission.turn_id != admission.turn_id and second_admission.execution_id != admission.execution_id
    assert second_admission.job_id != admission.job_id
    assert all(observed == second_admission for observed in case.model_admissions[second_start:])
    all_candidates = [dispatch for dispatch in case.model_dispatches if dispatch.snapshot.selection_policy == "summary_candidate"]
    assert len(all_candidates) == 4 and candidate_count == 4 and compaction_calls == 2
    assert len({dispatch.snapshot.assembly_id for dispatch in all_candidates}) == 4
    assert len({dispatch.callback_model_call_id for dispatch in all_candidates}) == 4
    assert all(dispatch.snapshot.seal_binding.prefix.consumed_transition is None for dispatch in all_candidates)
    second_final = case.model_dispatches[-1].snapshot
    assert second_final.seal_binding.prefix.state.epoch.reason.value == "compaction"
    assert second_final.seal_binding.prefix.state.epoch.ordinal == final.seal_binding.prefix.state.epoch.ordinal + 1
    assert second_final.seal_binding.prefix.consumed_transition is not None
    assert second_final.seal_binding.prefix.consumed_transition.transition_id != final.seal_binding.prefix.consumed_transition.transition_id
    second_candidate_ids = {dispatch.snapshot.assembly_id for dispatch in all_candidates[2:]}
    second_requests = [payload for payload, dispatch in zip(case.model_requests, case.model_dispatches, strict=True)
                       if dispatch.snapshot.assembly_id in second_candidate_ids]
    assert len(second_requests) == 2 and second_requests[0]["tools"] and not second_requests[1].get("tools")
    assert all(json.dumps(payload["messages"], ensure_ascii=False).count(first_summary_wire) == 1 for payload in second_requests)
    assert all("runtime-compact-2" not in json.dumps(payload["messages"], ensure_ascii=False) for payload in second_requests)
    assert all(marker not in json.dumps(payload["messages"], ensure_ascii=False) for payload in second_requests for marker in compacted_markers)
    all_summaries = [item for item in saver._storage.storage.read_items(session_id, thread_id)
                     if item.semantic_kind == "compaction_summary"]
    assert len(all_summaries) == 2 and all_summaries[0] == summaries[0]
    latest_summary = all_summaries[1]
    assert latest_summary.turn_id == second_admission.turn_id and latest_summary.turn_scope == "turn_member"
    assert second_summary_text in latest_summary.payload["summary"]
    latest_summary_wire = json.dumps(latest_summary.payload["summary"], ensure_ascii=False)
    assert json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False).count(latest_summary_wire) == 1
    assert latest_summary.projection_identity.projection_message_id != summaries[0].projection_identity.projection_message_id
    trigger_snapshots = [next(dispatch.snapshot for dispatch in case.model_dispatches
                              if dispatch.snapshot.turn_id == owner.turn_id and dispatch.snapshot.selection_policy != "summary_candidate")
                         for owner in (admission, second_admission)]
    with saver._storage.storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        transitions = connection.execute(
            "SELECT transition.transition_id,transition.status,transition.consumed_assembly_id,"
            "snapshot.source_assembly_id,snapshot.source_count,"
            "(SELECT COUNT(*) FROM checkpoint_context_source_states AS state WHERE state.checkpoint_id=snapshot.checkpoint_id),"
            "transition.checkpoint_ns,transition.source_checkpoint_id,snapshot.session_id,snapshot.thread_id,snapshot.checkpoint_ns "
            "FROM context_epoch_transitions AS transition "
            "JOIN checkpoint_context_source_snapshots AS snapshot ON snapshot.checkpoint_id=transition.source_checkpoint_id "
            "AND snapshot.checkpoint_ns=transition.checkpoint_ns "
            "WHERE transition.reason='compaction' ORDER BY transition.rowid",
        ).fetchall()
        assert len(transitions) == 2
        for transition, applied, trigger, summary, owner in zip(
            transitions, (final, second_final), trigger_snapshots, all_summaries, (admission, second_admission), strict=True,
        ):
            assert transition[:3] == (applied.seal_binding.prefix.consumed_transition.transition_id, "consumed", applied.assembly_id)
            assert transition[3] == trigger.assembly_id
            assert transition[4] > 0 and transition[4] == transition[5]
            assert transition[6] == "" and transition[8:] == (session_id, thread_id, "")
            summary_id = summary.projection_identity.projection_message_id
            assert summary.item_id == f"item-{summary_id}"
            assert summary.payload["summary_id"] == summary_id and summary.payload["view_revision"] == transition[7]
            assert summary.producer_ref == {"producer_kind": "runtime", "producer_id": transition[7], "invocation_id": owner.execution_id}
        assert connection.execute("SELECT COUNT(*) FROM checkpoints WHERE checkpoint_kind='compaction'").fetchone() == (2,)
        selected_summaries = connection.execute(
            "SELECT item.item_id,member.context_selected FROM checkpoint_namespace_state AS namespace "
            "JOIN branches AS branch ON branch.branch_id=namespace.active_branch_id "
            "JOIN context_view_items AS member ON member.view_id=branch.head_view_id AND member.visible=1 "
            "JOIN item_catalog AS item ON item.item_id=member.item_id "
            "WHERE namespace.checkpoint_ns='' AND item.semantic_kind='compaction_summary'",
        ).fetchall()
        assert dict(selected_summaries) == {summaries[0].item_id: 0, latest_summary.item_id: 1}
    recovered = RolloutCheckpointSaver(case.runner.workspace / ".boxteam" / "sessions")
    for trigger in trigger_snapshots:
        assert recovered.get_context_assembly(session_id, thread_id, assembly_id=trigger.assembly_id).to_dict() == trigger.to_dict()
    for dispatch in all_candidates:
        frozen_candidate = recovered.get_context_assembly(session_id, thread_id, assembly_id=dispatch.snapshot.assembly_id)
        assert frozen_candidate.to_dict() == dispatch.snapshot.to_dict()
        if frozen_candidate.assembly_id in second_candidate_ids:
            messages, _, losses = recovered.project_context_plan_to_provider(
                session_id, thread_id, frozen_candidate.as_sealed_plan(), target_format="chat_completions",
                include_runtime_notices=True, include_summaries=True,
            )
            assert not losses and [message.content for message in messages].count(summaries[0].payload["summary"]) == 1
            assert all(marker not in str(message.content) for message in messages for marker in compacted_markers)
    with session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id):
        repeated_checkpoint = await recovered.aget_tuple(build_checkpoint_config(thread_id))
        assert (await recovered.aget_tuple(checkpoint.config)).checkpoint == checkpoint.checkpoint
    repeated_values = repeated_checkpoint.checkpoint["channel_values"]
    repeated_event = repeated_values["_summarization_event"]
    assert repeated_event["summary_message"].content == latest_summary.payload["summary"]
    assert repeated_event["cutoff_index"] > event["cutoff_index"]
    assert repeated_values["messages"][repeated_event["cutoff_index"] - 1].id == repeated_event["cutoff_message_id"]
    assert repeated_event["cache_prefix_messages"] == event["cache_prefix_messages"]
    repeated_middle = [message for message in repeated_values["messages"][:repeated_event["cutoff_index"]]
                       if message.id not in prefix_ids]
    repeated_ids = {message.id for message in repeated_middle}
    assert compacted_ids < repeated_ids
    repeated_markers = {f"历史段{index}：" for index in range(1, 11)
                        if any(f"历史段{index}：" in str(message.content) for message in repeated_middle)}
    assert compacted_markers < repeated_markers
    for index in range(6, 11):
        marker = f"历史段{index}："
        assert all((marker in json.dumps(payload["messages"], ensure_ascii=False)) == (marker in repeated_markers)
                   for payload in second_requests)
    repeated_effective = apply_summarization_event(repeated_values["messages"], repeated_event)
    assert sum(message.id == latest_summary.projection_identity.projection_message_id for message in repeated_effective) == 1
    assert summaries[0].projection_identity.projection_message_id not in {message.id for message in repeated_effective}
    assert prefix_ids <= {message.id for message in repeated_effective}
    assert repeated_ids.isdisjoint(message.id for message in repeated_effective)
    assert all(marker not in str(message.content) for message in repeated_effective for marker in repeated_markers)
    root_url = f"/api/v1/sessions/{session_id}/turns/{second_admission.turn_id}/message-stream"
    response = await cold_message_stream_client.get(root_url + "/snapshot", params={"thread_id": thread_id})
    assert response.status_code == 200, response.text
    stream_id = response.json()["data"]["turn_stream_id"]
    response = await cold_message_stream_client.get(root_url + "/events", params={"thread_id": thread_id, "turn_stream_id": stream_id})
    assert response.status_code == 200, response.text
    records = response.json()["data"]
    started = {record["model_call_id"] for record in records if record["type"] == "model.started"}
    started_records = {record["model_call_id"]: record for record in records if record["type"] == "model.started"}
    assert {dispatch.callback_model_call_id for dispatch in all_candidates[2:]} <= started
    candidate_completed = {record["model_call_id"]: record for record in records
                           if record["type"] == "model.completed"
                           and record["model_call_id"] in {dispatch.callback_model_call_id for dispatch in all_candidates[2:]}}
    assert set(candidate_completed) == {dispatch.callback_model_call_id for dispatch in all_candidates[2:]}
    for record in candidate_completed.values():
        assert (record["session_id"], record["thread_id"], record["turn_id"]) == (session_id, thread_id, second_admission.turn_id)
        assert (record["job_id"], record["workspace_id"], record["turn_stream_id"]) == (
            second_admission.job_id, case.runner.container.workspace_service.workspace_id, stream_id,
        )
        started_record = started_records[record["model_call_id"]]
        assert started_record["event_seq"] < record["event_seq"]
        assert record["payload"]["attempt"] == started_record["payload"]["attempt"]
        assert record["payload"]["outcome"] == "accepted"
        assert record["payload"]["reason"] == "摘要候选传输完成；正文是否可用由压缩中间件校验"
    assert all(text not in json.dumps(records, ensure_ascii=False) for text in ("候选私有思考", summary_text, second_summary_text))
    phase = "successor"
    await _complete_job(case, session_id, thread_id, timeout_seconds=25)
    assert json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False).count(latest_summary_wire) == 1
    assert summary_text not in json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False)
    assert all(marker not in json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False) for marker in repeated_markers)
    forks = case.runner.container.session_context_fork_service
    point = await forks.source_point(session_id=session_id, thread_id=thread_id, mode="context_fork")
    fork_request = SessionForkRequest(
        idempotency_key="runtime-compacted-fork", source_uri=point.source_uri,
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
        mode="context_fork", creation_kind="new_child_thread", target_container_uri=point.child_container_uri,
    )
    fork_deadline = fork_deadlines.for_operation(
        target=(f"{case.runner.container.workspace_service.workspace_id}/"
                f"{fork_request.target_container_uri}"),
        operation_key=fork_request.idempotency_key,
    )
    forked = await fork_deadline.wait(
        forks.fork(fork_request), "SessionContextForkService.fork",
    )
    assert forked.creation_kind == "new_child_thread" and forked.session.session_id == session_id
    assert forked.export_release is None or forked.export_release.state == "confirmed"
    fork_deadlines.complete(fork_deadline)
    with session_thread_owner_scope(MutationIntentOwner(session_id, forked.thread_id), langgraph_thread_id=forked.thread_id):
        fork_head = await recovered.aget_tuple(build_checkpoint_config(forked.thread_id))
    fork_values = fork_head.checkpoint["channel_values"]
    assert "_summarization_event" not in fork_values and "_force_cache_compaction" not in fork_values
    assert [message.content for message in fork_values["messages"]].count(latest_summary.payload["summary"]) == 1
    assert summary_text not in str([message.content for message in fork_values["messages"]])
    assert repeated_ids.isdisjoint(message.id for message in fork_values["messages"])
    assert all(marker not in str(message.content) for message in fork_values["messages"] for marker in repeated_markers)
    context = await case.runner.container.message_service.get_agent_context_state(session_id, forked.thread_id)
    assert json.dumps(context["records"], ensure_ascii=False).count(latest_summary_wire) == 1
    assert summary_text not in json.dumps(context["records"], ensure_ascii=False)
    child_admission = await _complete_job(case, session_id, forked.thread_id, timeout_seconds=25)
    assert case.model_admissions[-1] == child_admission
    assert (child_admission.session_id, child_admission.thread_id) == (session_id, forked.thread_id)
    assert (case.model_dispatches[-1].snapshot.session_id, case.model_dispatches[-1].snapshot.thread_id,
            case.model_dispatches[-1].snapshot.turn_id) == (session_id, forked.thread_id, child_admission.turn_id)
    assert json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False).count(latest_summary_wire) == 1
    assert summary_text not in json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False)
    assert all(marker not in json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False) for marker in repeated_markers)


@pytest.fixture
def runtime_clock(runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch):
    seconds = [0.0]
    monkeypatch.setattr(runtime_case.tracker, "_clock", lambda: seconds[0])
    return seconds


@pytest.mark.asyncio
async def test_real_compiler_cold_reads_idle_boundary_background_blocker_and_exact_rehydrate(
    runtime_case: RuntimeCase, runtime_clock,
):
    case = runtime_case
    container = case.runner.container
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id)
    sibling = await _child(case, session_id, creation_key="runtime-sibling")
    assert case.compiled == []
    await container.session_service.get(session_id)
    from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
    for thread in (main, child, sibling):
        page = await container.session_turn_history_service.load_history(session_id, TurnHistoryLoadRequest(), thread_id=thread)
        assert page.thread_id == thread
    assert case.compiled == []
    for thread in (main, main, child, child, sibling):
        await _complete_job(case, session_id, thread, timeout_seconds=20)
    assert [pair for pair, _graph, _scope in case.compiled] == [(session_id, main), (session_id, child), (session_id, sibling)]
    assert case.bindings == [DEEP_AGENT_GRAPH_BINDING] * 3
    registry = container.background_task_registry
    handle = await registry.spawn(session_id=session_id, thread_id=main, agent_id="default",
        task_id="bgt_idle-runtime", descriptor=BackgroundTaskDescriptor(
            kind="emit_system_time_messages", version=1, payload={
                "interval_seconds": 7200, "message_count": 1, "source_id": "idle-runtime",
                "started_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            }))
    runtime_clock[0] = 1799
    await case.tracker.sweep()
    assert all(case.tracker.snapshot(session_id, thread).residency == "resident" for thread in (main, child, sibling))
    runtime_clock[0] = 1800
    await case.tracker.sweep()
    assert case.tracker.snapshot(session_id, main).residency == "resident"
    assert [blocker.kind for blocker in case.tracker.snapshot(session_id, main).blockers] == ["background_task"]
    assert all(case.tracker.snapshot(session_id, thread).residency == "cold" for thread in (child, sibling))
    assert all(scope.state == "closed" for _pair, _graph, scope in case.compiled[1:])
    await registry.cancel(session_id, handle.task_id)
    assert case.tracker.snapshot(session_id, main).blockers == ()
    runtime_clock[0] = 3599
    await case.tracker.sweep()
    assert case.tracker.snapshot(session_id, main).residency == "resident"
    runtime_clock[0] = 3600
    await case.tracker.sweep()
    assert case.tracker.snapshot(session_id, main).residency == "cold"
    assert case.compiled[0][2].state == "closed"
    await container.session_service.get(session_id)
    assert len(case.compiled) == 3
    for thread in (main, child, sibling):
        await _complete_job(case, session_id, thread, timeout_seconds=20)
    assert len(case.compiled) == 6
    assert case.bindings == [DEEP_AGENT_GRAPH_BINDING] * 6
    assert all(case.compiled[index][1] is not case.compiled[index + 3][1] for index in range(3))


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "internal"])
async def test_real_job_cancel_waits_for_pending_compile_and_releases_returned_lease(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
):
    case = runtime_case
    service = case.runner.container.agent_execution_service
    session_id, thread_id = await _session(case.runner)
    entered, release = threading.Event(), threading.Event()
    build = service._build_agent

    def held_build(**kwargs):
        assert current_execution_admission() is None
        entered.set()
        if not release.wait(10):
            raise TimeoutError("真实 compiler 屏障未释放")
        return build(**kwargs)

    monkeypatch.setattr(service, "_build_agent", held_build)
    job = await _start_job(case.runner, origin, session_id, thread_id)
    try:
        assert await asyncio.to_thread(entered.wait, 3), job.error_message
        await case.runner.container.job_service.control(
            job.job_id, JobControlRequest(action="cancel")
        )
        await asyncio.sleep(0.08)
        await case.runner.container.job_service.control(
            job.job_id, JobControlRequest(action="cancel")
        )
        assert job.status == JobStatus.cancelling
        assert job.task is not None and not job.task.done()
        assert (
            case.runner.container.job_service._thread_current_job[(session_id, job.thread_id)]
            == job.job_id
        )
        assert case.tracker.snapshot(session_id, thread_id).residency == "loading"
    finally:
        release.set()
        assert job.task is not None
        await asyncio.wait_for(asyncio.shield(job.task), 8)
    assert job.status == JobStatus.cancelled
    snapshot = case.tracker.snapshot(session_id, thread_id)
    assert snapshot.execution_state == "idle" and snapshot.residency == "resident"
    assert len(case.compiled) == 1 and case.model_admissions == []
    executions, turns, commits = _facts(case.runner, session_id, thread_id)
    assert executions[0][3] == "cancelled"
    assert len(turns) == (1 if origin == "user" else 0)
    await service.shutdown()
    assert case.compiled[0][2].state == "closed" and case.reactors[0].released
    await asyncio.sleep(0.04)
    assert _facts(case.runner, session_id, thread_id)[2] == commits


@pytest.mark.asyncio
async def test_real_compile_failure_closes_created_reactor_and_retry_rebuilds(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
):
    case = runtime_case
    session_id, thread_id = await _session(case.runner)
    compiler = agent_factory.create_agent
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("测试真实 compiler 首次失败")
        return compiler(*args, **kwargs)

    monkeypatch.setattr(agent_factory, "create_agent", fail_once)
    job = await _start_job(case.runner, "user", session_id, thread_id)
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), 8)
    assert job.status == JobStatus.failed
    assert "首次失败" in job.error_message
    assert len(case.reactors) == 1 and case.reactors[0].released
    assert case.tracker.snapshot(session_id, thread_id).generation == 0
    await _complete_job(case, session_id, thread_id)
    assert len(case.compiled) == 1 and len(case.reactors) == 2
    assert not case.reactors[1].released


@pytest.mark.asyncio
async def test_real_generation_change_waits_for_lease_and_scope_close_and_fences_reactor(
    runtime_case: RuntimeCase,
):
    case = runtime_case
    session_id, thread_id = await _session(case.runner)
    admission = _admit(case.runner, session_id, thread_id, "user")
    first = await _lease(case, admission)
    async with first:
        graph = first.runtime
        scope = case.compiled[0][2]
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_close():
            entered.set()
            await release.wait()

        scope.register(held_close, label="实际 scope 关闭屏障")
        next_admission = _admit(case.runner, session_id, thread_id, "internal")
        pending = asyncio.create_task(_lease(case, next_admission, read_file=False))
        try:
            await asyncio.sleep(0)
            assert not pending.done() and not entered.is_set()
            assert first.runtime is graph and len(case.compiled) == 1
            first.release()
            await asyncio.wait_for(entered.wait(), 3)
            assert not pending.done() and not case.reactors[0].released
            release.set()
            second = await asyncio.wait_for(pending, 8)
            try:
                assert second.generation == first.generation + 1
                assert second.runtime is not graph and scope.state == "closed"
                assert case.reactors[0].released and not case.reactors[1].released
                assert case.bindings == [DEEP_AGENT_GRAPH_BINDING, DEEP_AGENT_GRAPH_BINDING]
                assert case.reactors[0].ingest_notifications() == 0
                assert (
                    case.reactors[0]._handle_event(
                        ResourceObservation("file:late", "rev-late", True)
                    )
                    == 0
                )
                assert not case.tracker.is_current_generation(
                    session_id, thread_id, first.generation
                )
            finally:
                second.release()
        finally:
            first.release()
            release.set()
            (await pending).release()


@pytest.mark.asyncio
async def test_real_scope_close_failure_remains_unloading_until_retry(
    runtime_case: RuntimeCase,
):
    case = runtime_case
    session_id, thread_id = await _session(case.runner)
    first = await _lease(case, _admit(case.runner, session_id, thread_id, "user"))
    async with first:
        scope = case.compiled[0][2]
        attempts = 0

        def fail_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("真实 runtime scope 释放失败")

        scope.register(fail_once, label="失败资源")
        first.release()
        admission = _admit(case.runner, session_id, thread_id, "internal")
        with pytest.raises(ExceptionGroup, match="LifetimeScope close"):
            await _lease(case, admission, read_file=False)
        tracker = case.tracker
        assert tracker.snapshot(session_id, thread_id).residency == "unloading"
        assert not tracker.is_current_generation(session_id, thread_id, first.generation)
        assert scope.state == "close_failed" and case.reactors[0].released
        second = await _lease(case, admission, read_file=False)
        async with second:
            assert attempts == 2 and scope.state == "closed"
            assert second.generation == 2 and len(case.compiled) == 2
            second.release()


@pytest.mark.asyncio
async def test_failed_compile_scope_retains_owner_until_cleanup_then_fences_late_reactor(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
):
    case = runtime_case
    session_id, thread_id = await _session(case.runner)
    admission = _admit(case.runner, session_id, thread_id, "user")
    compiler = agent_factory.create_agent
    allow_close = threading.Event()
    compiler_calls = 0
    cleanup_calls = 0

    def failed_resource():
        nonlocal cleanup_calls
        cleanup_calls += 1
        if not allow_close.is_set():
            raise RuntimeError("失败 compiler 的资源尚不能关闭")

    def fail_once(*args, **kwargs):
        nonlocal compiler_calls
        compiler_calls += 1
        if compiler_calls == 1:
            case.build_scopes[-1].register(failed_resource, label="编译失败资源")
            raise RuntimeError("真实 compiler 与清理双失败")
        return compiler(*args, **kwargs)

    monkeypatch.setattr(agent_factory, "create_agent", fail_once)
    with pytest.raises(ExceptionGroup, match="构建与资源释放均失败") as failure:
        await _lease(case, admission)
    assert "真实 compiler" in str(failure.value.exceptions[0])
    assert "尚不能关闭" in str(failure.value.exceptions[1].exceptions[0].__cause__)
    failed_scope = case.build_scopes[0]
    state = case.tracker._states[(session_id, thread_id)]
    assert state.runtime is None and state.runtime_build_scope is failed_scope
    assert failed_scope.state == "close_failed"
    assert case.tracker.snapshot(session_id, thread_id).residency == "unloading"
    assert case.reactors[0].released and compiler_calls == 1

    with pytest.raises(ExceptionGroup, match="LifetimeScope close"):
        await asyncio.wait_for(_lease(case, admission), 3)
    assert cleanup_calls == 2 and compiler_calls == 1
    assert state.runtime_build_scope is failed_scope
    with pytest.raises(ExceptionGroup, match="ThreadRuntime owner"):
        await asyncio.wait_for(case.runner.container.agent_execution_service.shutdown(), 3)
    assert cleanup_calls == 3 and state.runtime_build_scope is failed_scope

    # shutdown 已关闭准入；使用同一 owner 的下一轮关闭重试，不重新物化。
    allow_close.set()
    await asyncio.wait_for(case.runner.container.agent_execution_service.shutdown(), 3)
    assert failed_scope.state == "closed" and state.runtime_build_scope is None
    assert compiler_calls == 1


@pytest.mark.asyncio
async def test_retry_after_failed_compile_cleanup_closes_old_scope_before_build(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
):
    case = runtime_case
    session_id, thread_id = await _session(case.runner)
    admission = _admit(case.runner, session_id, thread_id, "user")
    compiler = agent_factory.create_agent
    order: list[str] = []
    compiler_calls = 0

    def fail_cleanup_once():
        order.append("close")
        if order.count("close") == 1:
            raise RuntimeError("旧编译 scope 首次释放失败")

    def fail_compile_once(*args, **kwargs):
        nonlocal compiler_calls
        compiler_calls += 1
        order.append("build")
        if compiler_calls == 1:
            case.build_scopes[-1].register(fail_cleanup_once, label="编译失败资源")
            raise RuntimeError("真实 compiler 首次失败")
        assert case.build_scopes[0].state == "closed"
        return compiler(*args, **kwargs)

    monkeypatch.setattr(agent_factory, "create_agent", fail_compile_once)
    with pytest.raises(ExceptionGroup, match="构建与资源释放均失败"):
        await _lease(case, admission)
    lease = await asyncio.wait_for(_lease(case, admission), 8)
    try:
        assert order == ["build", "close", "close", "build"]
        assert lease.generation == 1
        assert case.tracker.is_current_generation(session_id, thread_id, lease.generation)
        assert case.reactors[0].released and not case.reactors[1].released
        assert case.reactors[0].ingest_notifications() == 0
        assert case.reactors[0]._handle_event(ResourceObservation("file:late", "rev", True)) == 0
        assert case.build_scopes[0].state == "closed"
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_worker_failure_still_closes_other_runtime_scopes_and_retains_failed_scope(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
):
    case = runtime_case
    service = case.runner.container.agent_execution_service
    session_id, main_thread_id = await _session(case.runner)
    main = await _lease(case, _admit(case.runner, session_id, main_thread_id, "user"))
    async with main:
        child_id = await _child(case, session_id)
        child = await _lease(case, _admit(case.runner, session_id, child_id, "internal"))
        async with child:
            main.release()
            child.release()
            failed_scope, healthy_scope = (entry[2] for entry in case.compiled)
            allow_close = asyncio.Event()
            wake_worker = asyncio.Event()
            original_sleep = asyncio.sleep
            original_sweep = case.tracker.sweep
            sweep_calls = 0

            def held_failure():
                if not allow_close.is_set():
                    raise RuntimeError("真实 generation scope 关闭失败")

            async def worker_sleep(seconds):
                if seconds == 60:
                    await wake_worker.wait()
                else:
                    await original_sleep(seconds)

            async def fail_worker_sweep():
                nonlocal sweep_calls
                sweep_calls += 1
                if sweep_calls == 2:
                    raise RuntimeError("真实 residency worker 失败")
                return await original_sweep()

            failed_scope.register(held_failure, label="待重试资源")
            monkeypatch.setattr(asyncio, "sleep", worker_sleep)
            monkeypatch.setattr(case.tracker, "sweep", fail_worker_sweep)
            await service.start()
            worker = service._sweep_task
            assert worker is not None
            wake_worker.set()
            with pytest.raises(RuntimeError, match="真实 residency worker"):
                await asyncio.wait_for(asyncio.shield(worker), 3)
            with pytest.raises(RuntimeError, match="真实 residency worker"):
                service._check_sweeper()
            with pytest.raises(ExceptionGroup, match="ThreadRuntime owner") as failure:
                await asyncio.wait_for(service.shutdown(), 3)
            assert str(failure.value.exceptions[0]) == "真实 residency worker 失败"
            assert failed_scope.state == "close_failed" and healthy_scope.state == "closed"
            assert case.tracker._states[(session_id, main_thread_id)].runtime.lifetime_scope is failed_scope
            healthy = case.tracker.snapshot(session_id, child_id)
            assert healthy.residency == "cold" and healthy.execution_state == "idle"
            assert all(reactor.released for reactor in case.reactors)
            allow_close.set()
            await asyncio.wait_for(service.shutdown(), 3)
            assert failed_scope.state == "closed" and service._sweep_task is None


@pytest.mark.asyncio
async def test_real_turn_close_failure_releases_lease_after_error_and_shutdown_ends(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
):
    case = runtime_case
    service = case.runner.container.agent_execution_service
    session_id, thread_id = await _session(case.runner)
    entered, release = asyncio.Event(), asyncio.Event()
    registry = service.execution_scope_registry
    create = registry.create
    scopes = []
    settlements: list[asyncio.Event] = []
    run_step = service.run_step
    cleanup_calls = 0

    async def held_cleanup():
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("真实 Turn.close 资源失败")

    def create_scope(turn_stream_id):
        scope = create(turn_stream_id)
        scope.register_cleanup(held_cleanup)
        scopes.append(scope)
        return scope

    async def observe_step(*args, **kwargs):
        settlements.append(kwargs["protected_operations_settled"])
        return await run_step(*args, **kwargs)

    monkeypatch.setattr(registry, "create", create_scope)
    monkeypatch.setattr(service, "run_step", observe_step)
    job = await _start_job(case.runner, "user", session_id, thread_id)
    shutdown_task = None
    try:
        await asyncio.wait_for(entered.wait(), 8)
        assert job.task is not None and not job.task.done()
        assert len(settlements) == 1 and not settlements[0].is_set()
        assert case.tracker.snapshot(session_id, thread_id).execution_state == "active"
        assert case.compiled[0][2].state == "open"
        shutdown_task = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)
        assert not shutdown_task.done()
    finally:
        release.set()
        assert job.task is not None
        await asyncio.wait_for(asyncio.shield(job.task), 8)
        if shutdown_task is not None:
            await asyncio.wait_for(shutdown_task, 3)
    assert job.status == JobStatus.failed and "LifetimeScope close" in job.error_message
    assert settlements[0].is_set()
    assert case.tracker.snapshot(session_id, thread_id).execution_state == "idle"
    assert case.compiled[0][2].state == "closed" and case.reactors[0].released
    assert scopes[0]._lifetime_scope.state == "close_failed"
    await scopes[0].close()


@pytest.mark.asyncio
async def test_real_compiler_uses_the_same_mcp_tool_snapshot_as_generation_key(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
):
    case = runtime_case
    service = case.runner.container.agent_execution_service
    owner = service._dependency_provider.get_mcp_catalog_owner()
    publication = owner.publication_snapshot()
    publications = []
    read_publication = owner.publication_snapshot
    build_agent = service._build_agent

    def record_publication():
        current = read_publication()
        publications.append(current)
        return current

    def compile_same_publication(**kwargs):
        assert kwargs["mcp_catalog_owner"] is owner
        assert tuple(kwargs["mcp_tools"]) == tuple(publication.tools.values())
        return build_agent(**kwargs)

    monkeypatch.setattr(owner, "publication_snapshot", record_publication)
    monkeypatch.setattr(service, "_build_agent", compile_same_publication)
    session_id, thread_id = await _session(case.runner)
    lease = await _lease(case, _admit(case.runner, session_id, thread_id, "user"))
    try:
        # 准入和 generation 订阅各读取一次同一原子 publication；不再走旧 get_mcp_tools。
        assert len(publications) == 2 and all(value is publication for value in publications)
        assert len(case.compiled) == 1
        runtime = case.tracker._states[(session_id, thread_id)].runtime
        assert runtime.generation_key[-2:] == (
            publication.binding_ref.catalog_revision, publication.binding_ref.generation,
        )
    finally:
        lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_case", ["lifespan"], indirect=True)
async def test_lifespan_starts_cold_worker_and_closes_runtime_before_resource_platform(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
):
    from app import main
    from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
        _isolate_non_navigation_services,
    )

    case = runtime_case
    container = case.runner.container
    service = container.agent_execution_service
    session_id, thread_id = await _session(case.runner)
    start, shutdown = service.start, service.shutdown
    mcp_start, mcp_shutdown = container.mcp_catalog_owner.start, container.mcp_catalog_owner.shutdown
    registry_start = container.workspace_file_resource_registry.start
    platform_type = type(container.resource_platform)
    close_resources = platform_type.close
    job_drain = container.job_service.drain_finalizations
    fanout_stop = container.team_fanout_service.stop
    order: list[str] = []

    async def drain_jobs():
        await job_drain()
        order.append("jobs-drained")

    async def stop_fanout():
        assert order == ["jobs-drained"]
        await fanout_stop()
        assert container.team_fanout_service._worker is None
        order.append("fanout-stopped")

    async def close_runtime():
        assert order == ["jobs-drained", "fanout-stopped"]
        order.append("runtime")
        await shutdown()

    async def close_platform(platform):
        assert platform is container.resource_platform
        assert service._sweep_task is None
        assert case.compiled[0][2].state == "closed" and case.reactors[0].released
        order.append("resource-platform")
        await close_resources(platform)

    with monkeypatch.context() as lifespan_patch:
        _isolate_non_navigation_services(container, lifespan_patch)
        lifespan_patch.setattr(service, "start", start)
        lifespan_patch.setattr(service, "shutdown", close_runtime)
        lifespan_patch.setattr(container.job_service, "drain_finalizations", drain_jobs)
        lifespan_patch.setattr(container.team_fanout_service, "stop", stop_fanout)
        # 生命周期用例由同一个 lifespan task 真实启动并关闭 MCP 和文件 registry。
        lifespan_patch.setattr(container.mcp_catalog_owner, "start", mcp_start)
        lifespan_patch.setattr(container.mcp_catalog_owner, "shutdown", mcp_shutdown)
        lifespan_patch.setattr(container.workspace_file_resource_registry, "start", registry_start)
        lifespan_patch.setattr(platform_type, "close", close_platform)
        lifespan_patch.setattr(main, "configure_application_logging", lambda **_: None)
        lifespan_patch.setattr(main, "_model_stream_controller", None)
        async with main.lifespan(main.app):
            assert service._sweep_task is not None and not service._sweep_task.done()
            assert case.tracker.snapshot(session_id, thread_id).residency == "cold"
            assert case.tracker.snapshot(session_id, thread_id).generation == 0
            assert case.compiled == [] and case.model_admissions == []
            lease = await _lease(case, _admit(case.runner, session_id, thread_id, "user"))
            async with lease:
                lease.release()
    assert order == ["jobs-drained", "fanout-stopped", "runtime", "resource-platform"]
    assert main.app.state.workspace_container_registry is None


@pytest.mark.asyncio
async def test_real_runner_refreshes_tracked_skill_in_same_generation_and_stops_publication(
    runtime_case: RuntimeCase,
):
    case = runtime_case
    skill_name = "runtime-resident-skill"
    old_body, new_body, late_body = (
        "同代 Skill 正文 V1 Ω\n", "同代 Skill 正文 V2 é\n", "关闭后 Skill 正文 V3\n",
    )
    frontmatter = f"---\nname: {skill_name}\ndescription: 同代自动刷新\n---\n"
    skill_file = case.runner.workspace / ".boxteam" / "skills" / skill_name / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(frontmatter + old_body, encoding="utf-8", newline="")
    provider_call_id = "call_runtime_resident_skill"

    def model_reply(_payload):
        if len(case.model_requests) == 1:
            return [
                {"choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0, "id": provider_call_id, "type": "function",
                            "function": {
                                "name": "skill_load",
                                "arguments": json.dumps({"name": skill_name, "mode": "tracked"}),
                            },
                        }],
                    },
                }]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "同代刷新回复"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    case.response_chunks = model_reply
    session_id, thread_id = await _session(case.runner)
    file_registry = case.runner.container.workspace_file_resource_registry
    subscribers_before = set(file_registry.observation_channel.subscriber_ids)
    first = await _complete_job(case, session_id, thread_id, timeout_seconds=25)
    assert len(case.model_requests) == 2 and len(case.compiled) == 1
    pair, graph, scope = case.compiled[0]
    registry = case.resource_registries[0]
    generation = case.tracker.snapshot(session_id, thread_id).generation
    activation_id = f"skill-entry:workspace:{skill_name}:activation"
    metadata_id = f"skill-entry:workspace:{skill_name}:metadata"
    catalog_id = "skill-entry:workspace:catalog:catalog"
    original_catalog = registry.snapshot(catalog_id)
    original_activation = registry.snapshot(activation_id)
    uri = original_activation.display_uri
    assert uri is not None
    original_source = file_registry.snapshot(uri)
    assert original_source.content == frontmatter + old_body
    assert scope.state == "open" and pair == (session_id, thread_id)
    assert len(set(file_registry.observation_channel.subscriber_ids) - subscribers_before) == 2
    publication_tasks = [
        task for task in asyncio.all_tasks()
        if task.get_name() == f"boxteam-skill-catalog:{scope.name}"
    ]
    assert len(publication_tasks) == 1
    first_dispatch = case.model_dispatches[0]
    frozen, model_call_id = case.runner.saver.load_tool_call_resource_activation(
        session_id, thread_id=thread_id,
        tool_call_id=f"{first_dispatch.callback_model_call_id}:tool-call:{provider_call_id}",
    )
    assert model_call_id == first_dispatch.callback_model_call_id

    async def wait_for_source(content: str):
        # 仅等既有共享 watcher 接受真实文件变化；不手工 refresh 或重建 catalog。
        async with asyncio.timeout(6):
            while True:
                source = file_registry.snapshot(uri)
                if source.available and source.content == content:
                    return source
                await asyncio.sleep(0.02)

    invalid_content = f"---\nname: {skill_name}\n---\n非法 metadata 不得替代 V1\n"
    skill_file.write_text(invalid_content, encoding="utf-8", newline="")
    invalid_source = await wait_for_source(invalid_content)
    async with asyncio.timeout(3):
        while registry.snapshot(catalog_id).available:
            await asyncio.sleep(0.02)
    unavailable_catalog = registry.snapshot(catalog_id)
    assert unavailable_catalog.error is not None and "description" in unavailable_catalog.error
    assert unavailable_catalog.payload is None
    assert unavailable_catalog.retained_revision == original_catalog.revision
    assert registry.snapshot(activation_id) == original_activation
    assert invalid_source.revision != original_source.revision

    skill_file.write_text(frontmatter + new_body, encoding="utf-8", newline="")
    accepted = await wait_for_source(frontmatter + new_body)
    async with asyncio.timeout(3):
        while (
            not registry.snapshot(catalog_id).available
            or registry.snapshot(activation_id).payload != {"body": new_body}
        ):
            await asyncio.sleep(0.02)
    activation = registry.snapshot(activation_id)
    assert activation.revision != original_activation.revision
    assert activation.source_lineage == registry.snapshot(metadata_id).source_lineage == (
        (f"skill-source:workspace:{skill_name}", accepted.revision),
    )
    live_catalog = registry.snapshot(catalog_id)
    assert live_catalog.revision != original_catalog.revision
    entry = next(item for item in live_catalog.payload["entries"] if item["name"] == skill_name)
    assert entry["activation_revision"] == activation.revision
    assert entry["metadata_revision"] == registry.snapshot(metadata_id).revision

    second = await _complete_job(case, session_id, thread_id, timeout_seconds=25)
    assert first.execution_id != second.execution_id and first.turn_id != second.turn_id
    assert case.compiled == [(pair, graph, scope)]
    assert case.resource_registries == [registry] and scope.state == "open"
    assert case.tracker.snapshot(session_id, thread_id).generation == generation
    assert case.model_admissions == [first, first, second]
    assert len(case.model_requests) == 3
    assert "同代 Skill 正文 V1 Ω" in json.dumps(case.model_requests[1]["messages"], ensure_ascii=False)
    assert "同代 Skill 正文 V2 é" not in json.dumps(case.model_requests[1]["messages"], ensure_ascii=False)
    assert "同代 Skill 正文 V2 é" in json.dumps(case.model_requests[2]["messages"], ensure_ascii=False)

    # 原工具仍从第一次请求的 durable activation 恢复 V1，与 live V2 彻底隔离。
    restarted = RolloutCheckpointSaver(case.runner.workspace / ".boxteam" / "sessions")
    restored, restored_model_call_id = restarted.load_tool_call_resource_activation(
        session_id, thread_id=thread_id,
        tool_call_id=f"{model_call_id}:tool-call:{provider_call_id}",
    )
    assert restored == frozen and restored_model_call_id == model_call_id
    frozen_catalog = skill_catalog_from_activation(
        restored, lambda snapshot, binding: restarted.read_resource_activation_payload(
            snapshot, binding, storage_owner=(session_id, thread_id),
        ),
    )
    assert frozen_catalog is not None
    assert frozen_catalog.entries[skill_name].body == old_body
    assert frozen_catalog.entries[skill_name].activation_revision == original_activation.revision
    assert frozen_catalog.catalog_revision == original_catalog.revision
    receipt = next(
        json.loads(message["content"]) for message in case.model_requests[1]["messages"]
        if message.get("role") == "tool" and json.loads(message["content"]).get("name") == skill_name
    )
    assert receipt["status"] == "loaded" and receipt["tracked"] is True
    assert receipt["revision"] == original_activation.revision

    await case.tracker.close(session_id=session_id)
    assert scope.state == "closed" and publication_tasks[0].done()
    assert set(file_registry.observation_channel.subscriber_ids) == subscribers_before
    registry_after_close = registry.capture()
    skill_file.write_text(frontmatter + late_body, encoding="utf-8", newline="")
    late_source = await wait_for_source(frontmatter + late_body)
    assert late_source.revision != accepted.revision
    await asyncio.sleep(0)
    assert registry.capture() == registry_after_close
    assert registry.snapshot(activation_id) == activation


@pytest.fixture
async def skill_tracking_client(runtime_case: RuntimeCase):
    """通过真实 HTTP handler 和服务操作当前 runner 的持久 Skill tracking。"""
    container = runtime_case.runner.container
    application = FastAPI()
    application.include_router(sessions_api.router, prefix="/api/v1")
    application.dependency_overrides[get_session_skill_tracking_service] = lambda: container.session_skill_tracking_service
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    application.dependency_overrides[get_request_id] = lambda: "request-skill-tracking"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        yield client


@pytest.fixture
async def gateway_skill_publication(runtime_case: RuntimeCase):
    """真实 raw 文件 owner 与传输协议桩；真实 Gateway 进程另由闭环用例验证。"""
    owner = GatewaySkillSourceOwner(
        gateway_id=COMPACTION_GATEWAY_ID, skills_root=get_boxteam_home() / "skills",
    )
    await owner.start()
    source = runtime_case.runner.container.resource_platform.gateway_skill_sources
    assert source is not None
    independent_requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal independent_requests
        request_id = request.headers.get("X-Request-ID")
        if request_id is None:
            # 真实 Gateway 的 TraceMiddleware 为独立轮询创建 ID；协议桩显式模拟此边界。
            independent_requests += 1
            request_id = f"gateway-skill-fixture-poll-{independent_requests}"
        snapshot = owner.snapshot(workspace_id=runtime_case.runner.container.workspace_id)
        etag = '"' + snapshot.revision + '"'
        headers = {"ETag": etag, "X-Request-ID": request_id}
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers=headers)
        return httpx.Response(200, json={
            "code": 0, "request_id": request_id, "data": snapshot.model_dump(mode="json"),
        }, headers=headers)

    source._state(COMPACTION_GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        yield source
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_real_runner_discovers_skill_overrides_and_downgrades_without_rebinding_tracked_source(
    runtime_case: RuntimeCase,
    skill_tracking_client: httpx.AsyncClient,
    gateway_skill_publication,
):
    case = runtime_case
    source = gateway_skill_publication
    name = "runtime-directory-skill"
    gateway_id = "gateway_0123456789abcdef0123456789abcdef"
    gateway_body = "Gateway 原 tracked 正文 G1\n"
    gateway_updated = "Gateway 被覆盖后继续跟踪 G2\n"
    workspace_body = "Workspace 新高层正文 W1\n"
    frontmatter = f"---\nname: {name}\ndescription: 目录动态发现\n---\n"
    workspace_root = case.runner.workspace / ".boxteam" / "skills"
    gateway_file = get_boxteam_home() / "skills" / name / "SKILL.md"
    workspace_file = workspace_root / name / "SKILL.md"
    extra_name = "runtime-new-entry"
    extra_file = workspace_root / extra_name / "SKILL.md"

    def model_reply(_payload):
        request_number = len(case.model_requests)
        if request_number == 2:
            return [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"call_directory_skill_{request_number}", "type": "function",
                    "function": {"name": "skill_load", "arguments": json.dumps({"name": name, "mode": "tracked"})},
                }]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "目录变化后真实回复"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    case.response_chunks = model_reply
    session_id, thread_id = await _session(case.runner)
    file_registry = case.runner.container.workspace_file_resource_registry
    catalog_id = "skill-entry:workspace:catalog:catalog"
    gateway_uri = f"boxteam://gateway/{gateway_id}/resources/skills/{name}/SKILL.md"

    async def complete_with_gateway():
        token = set_current_gateway_id(gateway_id)
        try:
            return await _complete_job(case, session_id, thread_id, timeout_seconds=25)
        finally:
            reset_current_gateway_id(token)

    # 首次编译时 Gateway skills 根尚不存在；后续新建也必须被现有共享 watcher 接受。
    first = await complete_with_gateway()
    assert len(case.model_requests) == 1
    registry = case.resource_registries[0]
    pair, graph, scope = case.compiled[0]
    generation = case.tracker.snapshot(session_id, thread_id).generation
    directories = case.skill_catalogs[0].directory_ids
    initial_workspace_directory = file_registry.skill_directory_snapshot(directories["workspace"])
    initial_gateway_directory = source.snapshot(gateway_id=gateway_id)
    assert initial_workspace_directory.entries == initial_gateway_directory.entries == ()

    def assert_dispatch_source(resource_id: str, source_revision: str | None):
        assert source_revision is not None
        activation = registry.snapshot(resource_id)
        assert activation.source_lineage == ((f"gateway-skill-source:{gateway_id}:{name}", source_revision),)
        assembly = case.model_dispatches[-1].snapshot
        frozen = case.runner.saver.load_sealed_resource_activation(
            session_id, thread_id=thread_id, assembly_id=assembly.assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
        )
        ref = next(binding for binding in frozen.bindings if binding.resource_id == resource_id)
        assert ref.revision == activation.revision and ref.display_uri == activation.display_uri
        assert case.runner.saver.read_resource_activation_payload(
            frozen, ref, storage_owner=(session_id, thread_id),
        ) == activation.payload

    async def wait_for_layer(expected_layer: str, *, extra: bool):
        async with asyncio.timeout(6):
            while True:
                snapshot = registry.snapshot(catalog_id)
                if snapshot.available:
                    entries = {item["name"]: item for item in snapshot.payload["entries"]}
                    if name in entries and entries[name]["layer"] == expected_layer and (extra_name in entries) == extra:
                        return snapshot
                await asyncio.sleep(0.02)

    gateway_file.parent.mkdir(parents=True, exist_ok=True)
    gateway_file.write_text(frontmatter + gateway_body, encoding="utf-8", newline="")
    await wait_for_layer("gateway", extra=False)
    accepted_gateway_directory = source.snapshot(gateway_id=gateway_id)
    assert accepted_gateway_directory.revision != initial_gateway_directory.revision
    assert [entry.display_uri for entry in accepted_gateway_directory.entries] == [gateway_uri]
    # 第二个 admission 显式加载真正新发现的 Gateway entry。
    second = await complete_with_gateway()
    assert first.execution_id != second.execution_id
    assert len(case.model_requests) == 3
    assert gateway_body.strip() in json.dumps(case.model_requests[2]["messages"], ensure_ascii=False)
    assert_dispatch_source(f"skill-entry:gateway:{gateway_id}:{name}:activation", next(entry.revision for entry in source.snapshot(gateway_id=gateway_id).entries if entry.name == name))

    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text(frontmatter + workspace_body, encoding="utf-8", newline="")
    extra_file.parent.mkdir(parents=True, exist_ok=True)
    extra_file.write_text(
        f"---\nname: {extra_name}\ndescription: 新增独立条目\n---\n新增独立正文\n", encoding="utf-8",
    )
    await wait_for_layer("workspace", extra=True)
    accepted_workspace_directory = file_registry.skill_directory_snapshot(directories["workspace"])
    assert {entry.name for entry in accepted_workspace_directory.entries} == {name, extra_name}
    assert accepted_workspace_directory.revision != initial_workspace_directory.revision
    gateway_file.write_text(frontmatter + gateway_updated, encoding="utf-8", newline="")
    async with asyncio.timeout(6):
        while registry.snapshot(f"skill-entry:gateway:{gateway_id}:{name}:activation").payload != {"body": gateway_updated}:
            await asyncio.sleep(0.02)
    await complete_with_gateway()
    overridden_request = json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False)
    assert gateway_updated.strip() in overridden_request
    assert workspace_body.strip() not in overridden_request
    assert_dispatch_source(f"skill-entry:gateway:{gateway_id}:{name}:activation", next(entry.revision for entry in source.snapshot(gateway_id=gateway_id).entries if entry.name == name))
    state = case.runner.saver.load_context_source_control_states(ContextSourceOwnerKey(session_id, thread_id))
    tracked = [item for item in state if item.source_kind == "skill" and item.tracking_status == "tracked"]
    assert [item.source_id for item in tracked] == [f"skill:skill-entry:gateway:{gateway_id}:{name}"]

    workspace_file.unlink()
    extra_file.unlink()
    await wait_for_layer("gateway", extra=False)
    assert file_registry.skill_directory_snapshot(directories["workspace"]).revision == initial_workspace_directory.revision
    assert f"skill-entry:workspace:{name}:activation" not in {item.resource_id for item in registry.snapshots()}
    await complete_with_gateway()
    assert gateway_updated.strip() in json.dumps(case.model_requests[-1]["messages"], ensure_ascii=False)
    assert case.compiled == [(pair, graph, scope)] and scope.state == "open"
    assert case.tracker.snapshot(session_id, thread_id).generation == generation

    # 高层再次出现后删除原 tracked 来源，不能自动改绑到仍存在的高层 entry。
    workspace_file.write_text(frontmatter + workspace_body, encoding="utf-8", newline="")
    await wait_for_layer("workspace", extra=False)
    gateway_file.unlink()
    async with asyncio.timeout(6):
        while (
            source.snapshot(gateway_id=gateway_id).entries
            or f"skill-entry:gateway:{gateway_id}:{name}:activation" in {item.resource_id for item in registry.snapshots()}
        ):
            await asyncio.sleep(0.02)
    await wait_for_layer("workspace", extra=False)
    before_failure = len(case.model_requests)
    token = set_current_gateway_id(gateway_id)
    try:
        job = await _start_job(case.runner, "user", session_id, thread_id)
    finally:
        reset_current_gateway_id(token)
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), 25)
    assert job.status == JobStatus.failed and "tracked-source-unavailable" in job.error_message
    assert len(case.model_requests) == before_failure
    assert not source.snapshot(gateway_id=gateway_id).entries
    with pytest.raises(KeyError, match="尚未注册"):
        file_registry.snapshot(gateway_uri)

    # 用户通过已有受信入口停止原 tracking，无需恢复已删除来源即可继续真实请求。
    response = await skill_tracking_client.post(
        f"/api/v1/sessions/{session_id}/threads/{thread_id}/skills/untrack", json={"name": name},
    )
    assert response.status_code == 200
    receipt = response.json()["data"]
    assert receipt["status"] == "loaded" and receipt["tracked"] is False and receipt["error"] is None
    control_states = case.runner.saver.load_context_source_control_states(ContextSourceOwnerKey(session_id, thread_id))
    assert next(item for item in control_states if item.source_id == f"skill:skill-entry:gateway:{gateway_id}:{name}").tracking_status == "untracked"
    await complete_with_gateway()
    assert len(case.model_requests) == before_failure + 1
    assert case.compiled == [(pair, graph, scope)] and scope.state == "open"
    assert case.tracker.snapshot(session_id, thread_id).generation == generation


@pytest.mark.asyncio
async def test_real_runner_skill_tool_consumes_frozen_provenance_after_live_refresh_and_restart(
    runtime_case: RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
):
    case = runtime_case
    skill_name = "runtime-frozen-skill"
    old_body = "冻结 Skill 正文 Ω 001  \n保留 é 与 e\u0301。\n"
    new_body = "当前 live Skill 正文 Ω 002\n"
    frontmatter = f"---\nname: {skill_name}\ndescription: 真实工具冻结归属\n---\n"
    skill_file = case.runner.workspace / ".boxteam" / "skills" / skill_name / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(frontmatter + old_body, encoding="utf-8", newline="")
    session_id, thread_id = await _session(case.runner)
    provider_call_id = "call_runtime_frozen_skill"
    tools: list[BaseTool] = []
    middlewares: list[ToolInvocationContextMiddleware] = []
    model_starts: list[str] = []
    resolutions: list[tuple[str, ResourceActivationSnapshotRef, str]] = []
    live_catalogs = []
    create_agent = agent_factory.create_agent
    register_start = StepModelCallAdapter.register
    resolve_activation = case.runner.saver.load_tool_call_resource_activation
    wrap_tool = ToolInvocationContextMiddleware.awrap_tool_call

    def record_agent(*args, **kwargs):
        tools.extend(tool for tool in kwargs["tools"] if tool.name == "skill_load")
        middlewares.extend(
            item for item in kwargs["middleware"]
            if isinstance(item, ToolInvocationContextMiddleware)
        )
        return create_agent(*args, **kwargs)

    async def record_model_start(adapter, model_call_id, attempt, model):
        result = await register_start(adapter, model_call_id, attempt, model)
        model_starts.append(model_call_id)
        return result

    def record_resolution(
        owner_session_id, *, thread_id, tool_call_id, model_call_id=None,
        checkpoint_ns="",
    ):
        snapshot, resolved_model_call_id = resolve_activation(
            owner_session_id, thread_id=thread_id, tool_call_id=tool_call_id,
            model_call_id=model_call_id, checkpoint_ns=checkpoint_ns,
        )
        assert model_call_id == resolved_model_call_id
        resolutions.append((tool_call_id, snapshot, resolved_model_call_id))
        return snapshot, resolved_model_call_id

    def forbid_live(*_args, **_kwargs):
        raise AssertionError("skill_load 不得读取当前 Registry/catalog")

    async def audit_skill_tool(middleware, request, handler):
        if request.tool_call.get("name") != "skill_load":
            return await wrap_tool(middleware, request, handler)
        # 只封住工具消费阶段的 live 读取；durable detail 完整性读取保持真实。
        with monkeypatch.context() as tool_patch:
            for name in ("capture", "snapshots", "snapshot", "last_valid_snapshot", "latest_revision"):
                tool_patch.setattr(case.resource_registries[0], name, forbid_live)
            return await wrap_tool(middleware, request, handler)

    def model_reply(_payload):
        call_number = len(case.model_requests)
        if call_number <= 2:
            chunks = [
                {"choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0, "id": provider_call_id, "type": "function",
                            "function": {
                                "name": "skill_load",
                                "arguments": json.dumps({"name": skill_name, "mode": "snapshot"}),
                            },
                        }],
                    },
                }]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
            if call_number == 1:
                # 调用已由真实 Provider 生成；在真实工具消费前发布不同的 live 正文。
                skill_file.write_text(frontmatter + new_body, encoding="utf-8", newline="")
                activation_descriptor = case.resource_registries[0].descriptor(
                    f"skill-entry:workspace:{skill_name}:activation"
                )
                assert activation_descriptor.display_uri is not None
                file_registry = case.runner.container.workspace_file_resource_registry
                file_registry.refresh(activation_descriptor.display_uri)
                live_catalogs.append(build_workspace_skill_catalog(
                    case.runner.workspace,
                    registry=case.resource_registries[0],
                    file_registry=file_registry,
                    bundled_skill_groups=resolve_bundled_skill_groups(),
                ))
                assert live_catalogs[0].activation_bodies[skill_name] == new_body
            return chunks
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "冻结正文消费完成"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    monkeypatch.setattr(agent_factory, "create_agent", record_agent)
    monkeypatch.setattr(StepModelCallAdapter, "register", record_model_start)
    monkeypatch.setattr(case.runner.saver, "load_tool_call_resource_activation", record_resolution)
    monkeypatch.setattr(ToolInvocationContextMiddleware, "awrap_tool_call", audit_skill_tool)
    case.response_chunks = model_reply
    # 三次真实模型调用的持久化收口实测约 10.4s，仅本新增场景使用独立等待预算。
    admission = await _complete_job(case, session_id, thread_id, timeout_seconds=20)

    assert len(case.compiled) == 1 and len(tools) == len(middlewares) == 1
    assert len(case.model_requests) == len(case.model_dispatches) == 3
    callback_ids = [dispatch.callback_model_call_id for dispatch in case.model_dispatches]
    assert len(set(callback_ids)) == 3 and model_starts == callback_ids
    for dispatch in case.model_dispatches:
        assert str(UUID(dispatch.callback_model_call_id)) == dispatch.callback_model_call_id
        assert dispatch.snapshot.model_call_id == dispatch.callback_model_call_id
        assert dispatch.snapshot.execution_id == admission.execution_id
        assert dispatch.snapshot.turn_id == admission.turn_id
    assert [call_id for call_id, _, _ in resolutions] == [provider_call_id, provider_call_id]
    assert [model_call_id for _, _, model_call_id in resolutions] == callback_ids[:2]
    assert all(
        (snapshot.owner_session_id, snapshot.owner_thread_id, snapshot.execution_id, snapshot.turn_id)
        == (session_id, thread_id, admission.execution_id, admission.turn_id)
        for _, snapshot, _ in resolutions
    )
    for payload in case.model_requests[1:]:
        serialized = json.dumps(payload["messages"], ensure_ascii=False)
        assert "冻结 Skill 正文 Ω 001" in serialized
        assert "当前 live Skill 正文 Ω 002" not in serialized
    receipts = [
        json.loads(message["content"])
        for message in case.model_requests[-1]["messages"]
        if message.get("role") == "tool"
    ]
    receipts = [receipt for receipt in receipts if receipt.get("name") == skill_name]
    assert len(receipts) == 2
    assert [receipt["status"] for receipt in receipts] == ["loaded", "already_active"]
    assert [receipt["append_status"] for receipt in receipts] == ["appended", "already_active"]
    assert all(receipt["error"] is None for receipt in receipts)

    saver = case.runner.saver
    with saver._storage.storage._connect(
        thread_id, "", session_id=session_id, read_only=True,
    ) as connection:
        rows = connection.execute(
            "SELECT m.model_call_id, m.assembly_id, m.execution_id, e.turn_id "
            "FROM model_calls m JOIN executions e "
            "ON e.session_id = m.session_id AND e.thread_id = m.thread_id "
            "AND e.execution_id = m.execution_id "
            "WHERE m.session_id = ? AND m.thread_id = ?",
            (session_id, thread_id),
        ).fetchall()
        assert {row[0]: row[1:] for row in rows} == {
            dispatch.callback_model_call_id: (
                dispatch.snapshot.assembly_id, admission.execution_id, admission.turn_id,
            ) for dispatch in case.model_dispatches
        }
        persisted_call_ids = {
            row[0] for row in connection.execute(
                "SELECT tool_call_id FROM tool_calls WHERE tool_name = 'skill_load'",
            ).fetchall()
        }
    expected_persisted_ids = {
        f"{model_call_id}:tool-call:{provider_call_id}" for model_call_id in callback_ids[:2]
    }
    assert expected_persisted_ids <= persisted_call_ids
    restarted = RolloutCheckpointSaver(case.runner.workspace / ".boxteam" / "sessions")
    for call_id, frozen, model_call_id in resolutions:
        persisted_id = f"{model_call_id}:tool-call:{call_id}"
        restored, restored_model_call_id = restarted.load_tool_call_resource_activation(
            session_id, thread_id=thread_id, tool_call_id=persisted_id,
        )
        assert restored == frozen and restored_model_call_id == model_call_id
        catalog = skill_catalog_from_activation(
            restored, lambda frozen, binding: restarted.read_resource_activation_payload(
                frozen, binding, storage_owner=(session_id, thread_id),
            ),
        )
        assert catalog is not None
        binding = catalog.entries[skill_name]
        assert binding.body == old_body
        assert catalog.catalog_revision != live_catalogs[0].catalog_revision
        assert binding.activation_revision == receipts[0]["revision"]
        assert binding.activation_revision != next(
            entry.activation_revision for entry in live_catalogs[0].entries if entry.name == skill_name
        )
    with pytest.raises(ExtensionCatalogUnavailableError, match="缺失或歧义"):
        restarted.load_tool_call_resource_activation(
            session_id, thread_id=thread_id, tool_call_id="missing-tool-call",
            model_call_id=callback_ids[0],
        )

    # 通过正式 Saver 创建唯一 raw canonical 样本，核对真实 typed owner；
    # 无 scope 拒绝不得依赖 raw 查询恰好未命中，也不得改写原调用数据。
    raw_only_call_id = "call_runtime_unscoped_fixture"
    raw_message_id = "runtime-unscoped-fixture-assistant"
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        restarted.append_items(session_id, (CanonicalItemRecord.create(
            item_sequence=1, item_id="item-runtime-unscoped-fixture",
            semantic_kind="tool_call", payload_kind="tool_call", status="completed",
            producer_ref={"producer_kind": "provider", "producer_id": callback_ids[0]},
            payload={"tool_calls": [{
                "id": raw_only_call_id, "name": "skill_load", "type": "tool_call",
                "args": {"name": skill_name, "mode": "snapshot"},
            }]},
            projection_identity=ItemProjectionIdentity(
                projection_message_id=raw_message_id, model_call_id=callback_ids[0],
            ),
            turn_id=admission.turn_id, turn_scope="turn_member", wire_role="assistant",
        ),), thread_id=thread_id)
    with restarted._storage.storage._connect(
        thread_id, "", session_id=session_id, read_only=True,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM tool_calls WHERE tool_call_id = ?", (raw_only_call_id,),
        ).fetchone() == (1,)
        durable_before = tuple(connection.iterdump())
    raw_bound, raw_model_call_id = restarted.load_tool_call_resource_activation(
        session_id, thread_id=thread_id, tool_call_id=raw_only_call_id,
        model_call_id=callback_ids[0],
    )
    assert raw_bound == resolutions[0][1] and raw_model_call_id == callback_ids[0]
    rollout_path = restarted._storage.storage.jsonl_path(session_id, "", thread_id=thread_id)
    rollout_before = rollout_path.read_bytes()
    with pytest.raises(ExtensionCatalogUnavailableError, match="无 model-call scope.*scoped ID"):
        restarted.load_tool_call_resource_activation(
            session_id, thread_id=thread_id, tool_call_id=raw_only_call_id,
        )
    with restarted._storage.storage._connect(
        thread_id, "", session_id=session_id, read_only=True,
    ) as connection:
        assert tuple(connection.iterdump()) == durable_before
    assert rollout_path.read_bytes() == rollout_before

    # 真 Factory 注册的工具与原 invocation middleware；缺请求 provenance
    # 时必须拒绝，不能使用 manager 的当前/已加载 catalog 伪造再次成功。
    missing_request = ToolCallRequest(
        tool_call={
            "id": provider_call_id, "name": "skill_load", "type": "tool_call",
            "args": {"name": skill_name, "mode": "snapshot"},
        },
        tool=tools[0], state={}, runtime=None,
    )

    async def invoke_registered(request):
        return await request.tool.ainvoke(request.tool_call)

    with pytest.raises(RuntimeError, match="emitting assistant.*model_call_id"):
        await middlewares[0].awrap_tool_call(missing_request, invoke_registered)
    assert len(resolutions) == 2
    with pytest.raises(RuntimeError, match="tool_call_id"):
        middlewares[0]._context.require_tool_call_id()
