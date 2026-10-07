"""真实 Runner、注册 factory、Saver 与 thread generation scope 的纵向合同。"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
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
    build_workspace_skill_catalog,
    resolve_bundled_skill_groups,
)
from app.agents.tool_invocation_context import ToolInvocationContextMiddleware
from app.core.execution_admission_context import (
    current_execution_admission,
    execution_admission_scope,
    require_current_execution_admission,
)
from app.core.lifecycle import LifetimeScope
from app.core.path_utils import workspace_context
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import (
    current_session_thread_owner,
    session_thread_owner_scope,
)
from app.core.thread_creation import ThreadCreationService
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.job import JobControlRequest
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
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
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    skill_catalog_from_activation,
)
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
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


async def _child(case: RuntimeCase, session_id: str) -> str:
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
            idempotency_key="runtime-child",
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
            case.runner.container.job_service._session_current_job[session_id]
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
    order: list[str] = []

    async def close_runtime():
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
    assert order == ["runtime", "resource-platform"]
    assert main.app.state.workspace_container_registry is None


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
                live_catalogs.append(build_workspace_skill_catalog(
                    case.runner.workspace, registry=case.resource_registries[0],
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
    with saver._storage._connect(
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
        catalog = skill_catalog_from_activation(restored, restarted.read_resource_activation_payload)
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
    with restarted._storage._connect(
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
    rollout_path = restarted._storage.jsonl_path(session_id, "", thread_id=thread_id)
    rollout_before = rollout_path.read_bytes()
    with pytest.raises(ExtensionCatalogUnavailableError, match="无 model-call scope.*scoped ID"):
        restarted.load_tool_call_resource_activation(
            session_id, thread_id=thread_id, tool_call_id=raw_only_call_id,
        )
    with restarted._storage._connect(
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
