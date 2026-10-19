"""真实双 AppContainer 的请求、compiler 身份与逆序 lifespan 排空。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import litellm
import pytest

from app import container as container_module
from app import main
from app.agents import agent_factory
from app.container import AppContainer
from app.core.job_event_bus import EventType
from app.core.mounted_workspaces import MountedWorkspace
from app.core.path_utils import (
    get_workspace_id,
    get_workspace_root,
    workspace_context,
)
from app.core.session_thread_context import current_session_thread_owner
from app.core.workspace_identity import load_or_create_workspace_id
from app.runtime.model_stream import ModelStreamTransportController
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.goal import GoalStatus
from app.services.business.job.service import JobState
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.message_stream_store import MessageStreamNotFoundError
from app.services.infrastructure.resource_platform.bootstrap import ResourcePlatform
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from app.testing.model_stream import load_model_stream_config
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_runner_cancellation import (
    RunnerCase,
    _session,
    _start_job,
)
from tests.support.workspaces import prepare_default_test_workspace


class _ModelProtocolStream:
    received_finish_reason = False

    def __init__(self, text: str, request: dict[str, object]) -> None:
        self.text = text
        self.logging_obj = SimpleNamespace(model_call_details={
            "call_type": "acompletion", "custom_llm_provider": request["custom_llm_provider"],
            "model": request["model"],
            "additional_args": {
                "api_base": request.get("api_base"), "complete_input_dict": request,
            },
        })

    async def __aiter__(self):
        yield {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {"content": self.text}, "finish_reason": None}]}
        self.received_finish_reason = True
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}

    async def aclose(self) -> None:
        pass


class _ModelProtocol:
    async def completion(self, *_args, **kwargs):
        assert kwargs["stream"] is True
        assert isinstance(kwargs["messages"], list) and kwargs["messages"]
        return _ModelProtocolStream(f"工作区 {get_workspace_id()} 的真实编译图", kwargs)


@dataclass
class MountedCase:
    roots: dict[str, Path]
    containers: list[AppContainer] = field(default_factory=list)
    closed: list[tuple[str, str]] = field(default_factory=list)
    compiled: list[tuple[str, Path, str, str]] = field(default_factory=list)
    fail_second: bool = False


@pytest.fixture
def mounted_case(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    roots = {}
    for name in ("a", "b"):
        root = context.workspace_root / name
        prepare_default_test_workspace(workspace_root=root, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace")
        (root / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
        roots[load_or_create_workspace_id(root)] = root
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    monkeypatch.setenv("BOXTEAM_MOUNTED_WORKSPACES", json.dumps([{"workspace_id": identity, "workspace_root": str(root), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None} for identity, root in roots.items()]))
    monkeypatch.setattr(main, "_model_stream_controller", None)
    case = MountedCase(roots)
    factory = container_module.build_app_container
    platform_close = ResourcePlatform.close

    async def close_platform(platform):
        owner = next(container for container in case.containers if container.resource_platform is platform)
        case.closed.append((owner.workspace_id, "platform"))
        await platform_close(platform)

    monkeypatch.setattr(ResourcePlatform, "close", close_platform)

    def build(**kwargs):
        container = factory(**kwargs)
        case.containers.append(container)
        drain = container.job_service.force_interrupt_active
        shutdown = container.agent_execution_service.shutdown
        validate = container.config_service.validate_workspace_config

        async def drain_jobs(**options):
            case.closed.append((container.workspace_id, "jobs"))
            await drain(**options)

        async def shutdown_runtime():
            case.closed.append((container.workspace_id, "runtime"))
            await shutdown()

        def validate_config():
            validate()
            if case.fail_second and len(case.containers) == 2:
                raise RuntimeError("第二工作区启动校验失败")

        monkeypatch.setattr(container.job_service, "force_interrupt_active", drain_jobs)
        monkeypatch.setattr(container.agent_execution_service, "shutdown", shutdown_runtime)
        monkeypatch.setattr(container.config_service, "validate_workspace_config", validate_config)
        return container

    monkeypatch.setattr(container_module, "build_app_container", build)

    model_builder = agent_factory.build_model_from_provider
    protocol = _ModelProtocol()

    async def completion_stub(*args, **kwargs):
        return await protocol.completion(*args, **kwargs)

    monkeypatch.setattr(litellm, "acompletion", completion_stub)
    monkeypatch.setattr(litellm, "aresponses", completion_stub)

    def model_runtime(*args, **kwargs):
        owner = current_session_thread_owner()
        assert owner is not None
        identity, root = get_workspace_id(), get_workspace_root()
        assert roots[identity] == root
        case.compiled.append((identity, root, owner.session_id, owner.thread_id))
        return model_builder(*args, **kwargs)

    # 只在外部模型传输边界返回协议桩；真实 model binding、说明来源和 sealed dispatch 保留。
    monkeypatch.setattr(agent_factory, "build_model_from_provider", model_runtime)
    yield case


def assert_closed(case: MountedCase):
    identities = list(case.roots)
    assert case.closed == [(identity, phase) for identity in reversed(identities) for phase in ("jobs", "runtime", "platform")]
    for container in case.containers:
        assert container.agent_execution_service._sweep_task is None
        assert container.resource_platform.process_root_scope.is_closed
        assert container.session_catalog_service.operations._worker_task is None
        with pytest.raises(RuntimeError, match="已关闭|closed"):
            _ = container.session_catalog.resolver.catalog_store.connection
    assert main.app.state.workspace_container_registry is None


@pytest.mark.asyncio
async def test_real_double_mount_requests_compilation_and_reverse_cleanup(mounted_case: MountedCase):
    case = mounted_case
    async with main.lifespan(main.app):
        registry = main.app.state.workspace_container_registry
        assert registry is not None
        containers = [registry.get(identity) for identity in case.roots]
        assert len({id(container) for container in containers}) == 2
        for field_name in ("job_service", "message_service", "checkpointer", "agent_execution_service", "resource_platform", "mcp_catalog_owner", "workspace_activity_service"):
            assert len({id(getattr(container, field_name)) for container in containers}) == 2
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test") as client:
            healths = await asyncio.gather(*(client.get(f"/api/v1/workspaces/{identity}/health") for identity in case.roots))
            assert [response.json()["workspace_id"] for response in healths] == list(case.roots)
            assert all(response.status_code == 200 and "workspace_root" not in response.json() for response in healths)
            assert (await client.get("/api/v1/health")).status_code == 400
            assert (await client.get("/api/v1/health", headers={"X-BoxTeam-Workspace-Id": "not-a-uuid"})).status_code == 400
        async def run(container):
            with workspace_context(
                container.workspace_id,
                container.workspace_root,
                session_catalog=container.session_catalog,
            ):
                runner = RunnerCase(container, container.workspace_root)
                session_id, thread_id = await _session(runner)
                job = await _start_job(runner, "user", session_id, thread_id)
                await asyncio.wait_for(asyncio.shield(job.task), 10)
                assert job.status == JobStatus.completed, job.error_message
                return session_id, thread_id
        pairs = await asyncio.gather(*(run(container) for container in containers))
        assert {(identity, root, *pair) for (identity, root), pair in zip(case.roots.items(), pairs, strict=True)} == set(case.compiled)
        mounted_sessions = [
            await container.session_service.get(pair[0])
            for container, pair in zip(containers, pairs, strict=True)
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://workspace.test",
        ) as client:
            for container, session in zip(containers, mounted_sessions, strict=True):
                assert session.workspace_id == container.workspace_id
                resource = {
                    "resource_identity": str(session.resource_identity),
                    "uri": session.uri,
                    "scope": session.scope,
                    "view": "messages",
                    "include": ["visible_text"],
                }
                response = await client.post(
                    f"/api/v1/workspaces/{container.workspace_id}/context/read",
                    json=resource,
                )
                assert response.status_code == 200, response.text
                result = response.json()["data"]
                echoed_fields = ("resource_identity", "uri", "scope", "view")
                assert {key: result[key] for key in echoed_fields} == {
                    key: resource[key] for key in echoed_fields
                }
                assert result["items"]
                assert all(item["kind"] == "message" for item in result["items"])
                assert all(
                    (item["resource_identity"], item["uri"], item["scope"])
                    == (resource["resource_identity"], resource["uri"], resource["scope"])
                    for item in result["items"]
                )
                other_container = next(
                    other for other in containers
                    if other.workspace_id != container.workspace_id
                )
                message_text = "\n".join(
                    item["text"] or "" for item in result["items"]
                )
                assert f"工作区 {container.workspace_id} 的真实编译图" in message_text
                assert f"工作区 {other_container.workspace_id} 的真实编译图" not in message_text

            for container, other_session in zip(
                containers, reversed(mounted_sessions), strict=True
            ):
                assert other_session.workspace_id != container.workspace_id
                resource = {
                    "resource_identity": str(other_session.resource_identity),
                    "uri": other_session.uri,
                    "scope": other_session.scope,
                    "view": "messages",
                    "include": ["visible_text"],
                }
                response = await client.post(
                    f"/api/v1/workspaces/{container.workspace_id}/context/read",
                    json=resource,
                )
                assert response.status_code == 400, response.text
                error = response.json()
                assert error["detail"]["code"] == "scope_mismatch"
                assert "data" not in error
                assert "items" not in error
                assert other_session.session_id not in response.text
                assert other_session.uri not in response.text
                assert str(other_session.resource_identity) not in response.text

        for container, other, pair in zip(containers, reversed(containers), pairs, strict=True):
            with workspace_context(
                container.workspace_id,
                container.workspace_root,
                session_catalog=container.session_catalog,
            ):
                assert (await container.session_service.get(pair[0])).workspace_id == container.workspace_id
                with pytest.raises(KeyError):
                    other.session_service.path_resolver.get_node(pair[0])
                activity = container.workspace_activity_service.store.list_activity()
                assert [(record.session_id, record.status) for record in activity] == [(pair[0], "completed")]
    assert_closed(case)


@pytest.mark.asyncio
async def test_second_mount_failure_closes_both_real_containers(mounted_case: MountedCase):
    mounted_case.fail_second = True
    with pytest.raises(RuntimeError, match="第二工作区启动校验失败"):
        async with main.lifespan(main.app):
            raise AssertionError("部分启动失败不得进入 ready")
    assert len(mounted_case.containers) == 2
    assert_closed(mounted_case)


@pytest.fixture
def unused_model_stream_controller(monkeypatch: pytest.MonkeyPatch):
    controller = ModelStreamTransportController.configure(load_model_stream_config(
        Path.cwd() / "configs/tests/model_stream/model_stream_chat_basic.jsonc",
    ))
    assert controller is not None
    monkeypatch.setattr(main, "_model_stream_controller", controller)
    return controller


@pytest.mark.asyncio
@pytest.mark.parametrize("registry_stage", ["mounted", "containers"])
async def test_registry_failure_closes_configured_transport(
    mounted_case: MountedCase,
    unused_model_stream_controller: ModelStreamTransportController,
    monkeypatch: pytest.MonkeyPatch,
    registry_stage: str,
):
    controller = unused_model_stream_controller
    closed: list[str] = []
    original_close = controller.aclose

    async def close_transport():
        await original_close()
        closed.append("transport")

    def fail_registry(*_args, **_kwargs):
        raise RuntimeError("registry 前置构造失败")

    monkeypatch.setattr(controller, "aclose", close_transport)
    if registry_stage == "mounted":
        monkeypatch.setattr(main.MountedWorkspaceRegistry, "from_environment", fail_registry)
    else:
        monkeypatch.setattr(main, "WorkspaceContainerRegistry", fail_registry)
    try:
        with pytest.raises(RuntimeError, match="registry 前置构造失败"):
            async with main.lifespan(main.app):
                raise AssertionError("registry 构造失败不得进入 ready")
        assert closed == ["transport"]
        assert controller.client is None
        assert not mounted_case.containers
        assert main.app.state.workspace_container_registry is None
    finally:
        await original_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "user_status", "expected_status"),
    [
        (JobStatus.cancelled, GoalStatus.active, GoalStatus.paused),
        (JobStatus.failed, GoalStatus.active, GoalStatus.blocked),
        (JobStatus.failed, GoalStatus.paused, GoalStatus.paused),
        (JobStatus.cancelled, GoalStatus.blocked, GoalStatus.blocked),
    ],
)
async def test_shutdown_waits_for_real_goal_terminal_state_before_catalog_close(
    mounted_case: MountedCase,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: JobStatus,
    user_status: GoalStatus,
    expected_status: GoalStatus,
) -> None:
    ready, close_requested = asyncio.Event(), asyncio.Event()
    model_entered, model_release = asyncio.Event(), asyncio.Event()
    goal_entered, goal_captured = asyncio.Event(), asyncio.Event()
    containers: list[AppContainer] = []
    jobs: list[JobState] = []
    goal_paths: list[Path] = []
    locks: list[asyncio.Lock] = []

    async def blocked_model(_model, *_args, **_kwargs):
        model_entered.set()
        await model_release.wait()
        raise RuntimeError("受控模型失败")

    monkeypatch.setattr(_ModelProtocol, "completion", blocked_model)

    async def run_lifespan() -> None:
        async with main.lifespan(main.app):
            container = mounted_case.containers[0]
            containers.append(container)
            with workspace_context(
                container.workspace_id,
                container.workspace_root,
                session_catalog=container.session_catalog,
            ):
                case = RunnerCase(container, container.workspace_root)
                session_id, thread_id = await _session(case)
                goals, runtime = container.goal_service, container.goal_runtime_service
                await goals.set(session_id, objective="关闭时必须保存真实 Goal 状态")
                real_capture = runtime._capture_started_job
                real_get = goals.get

                async def capture(job_id: str) -> None:
                    await real_capture(job_id)
                    goal_captured.set()

                async def get_goal(identity: str):
                    if close_requested.is_set() and asyncio.current_task() in runtime._tasks:
                        goal_entered.set()
                    return await real_get(identity)

                monkeypatch.setattr(runtime, "_capture_started_job", capture)
                monkeypatch.setattr(goals, "get", get_goal)
                job = await _start_job(case, "user", session_id, thread_id)
                jobs.append(job)
                await asyncio.wait_for(model_entered.wait(), 5)
                await asyncio.wait_for(goal_captured.wait(), 5)
                await goals.set(session_id, status=user_status)
                lock = goals.lock_for(session_id)
                await lock.acquire()
                locks.append(lock)
                goal_paths.append(
                    container.session_service.path_resolver.resolve_session_node(session_id)
                    / "goal.json"
                )
                ready.set()
                await close_requested.wait()
                if terminal_status == JobStatus.failed:
                    model_release.set()
                    await asyncio.wait_for(asyncio.shield(job.task), 5)
                    assert job.status == JobStatus.failed

    task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(ready.wait(), 8)
        close_requested.set()
        await asyncio.wait_for(goal_entered.wait(), 5)
        await asyncio.wait_for(asyncio.shield(jobs[0].task), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        assert not task.done()
        assert jobs[0].status == terminal_status
        assert containers[0].session_catalog.resolver.catalog_store.connection is not None
    finally:
        for lock in locks:
            lock.release()
        close_requested.set()
        model_release.set()
        await asyncio.wait_for(task, 8)
    assert json.loads(goal_paths[0].read_text(encoding="utf-8"))["status"] == expected_status.value
    assert not containers[0].goal_runtime_service._tasks
    assert not containers[0].job_service._finalization_tasks
    assert_closed(mounted_case)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_failure", [False, True])
async def test_shutdown_waits_for_pre_body_cancel_finalization_writer(
    mounted_case: MountedCase,
    monkeypatch: pytest.MonkeyPatch,
    writer_failure: bool,
) -> None:
    ready, close_requested = asyncio.Event(), asyncio.Event()
    entered, release = threading.Event(), threading.Event()
    containers: list[AppContainer] = []
    jobs: list[JobState] = []

    async def run_lifespan() -> None:
        async with main.lifespan(main.app):
            container = mounted_case.containers[0]
            containers.append(container)
            real_start = container.job_service._start_job_task
            real_writer = container.checkpointer.mark_job_terminal_status

            def cancel_before_body(job: JobState) -> None:
                real_start(job)
                assert job.task is not None
                job.task.cancel()

            def blocked_writer(**kwargs) -> bool:
                entered.set()
                if not release.wait(8):
                    raise TimeoutError("Job 终态 writer 测试屏障未释放")
                if writer_failure:
                    raise RuntimeError("真实 writer 边界失败")
                return real_writer(**kwargs)

            monkeypatch.setattr(container.job_service, "_start_job_task", cancel_before_body)
            monkeypatch.setattr(container.checkpointer, "mark_job_terminal_status", blocked_writer)
            with workspace_context(
                container.workspace_id,
                container.workspace_root,
                session_catalog=container.session_catalog,
            ):
                case = RunnerCase(container, container.workspace_root)
                session_id, thread_id = await _session(case)
                jobs.append(await _start_job(case, "user", session_id, thread_id))
            ready.set()
            await close_requested.wait()
            if writer_failure:
                raise ValueError("原始 lifespan 失败")

    task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(ready.wait(), 8)
        assert await asyncio.to_thread(entered.wait, 5)
        close_requested.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        assert jobs[0].task is not None and jobs[0].task.cancelled()
        assert jobs[0].status == JobStatus.cancelled
        assert containers[0].job_service._finalization_tasks
        assert containers[0].session_catalog.resolver.catalog_store.connection is not None
        assert not mounted_case.compiled
    finally:
        release.set()
        close_requested.set()
        if writer_failure:
            with pytest.raises(BaseExceptionGroup, match="Job 后台终态收尾失败") as caught:
                await asyncio.wait_for(task, 8)
            context = caught.value.__context__
            assert isinstance(context, ValueError)
            assert str(context) == "原始 lifespan 失败"
        else:
            await asyncio.wait_for(task, 8)
    assert not containers[0].job_service._finalization_tasks
    assert not containers[0].goal_runtime_service._tasks
    assert_closed(mounted_case)


@pytest.mark.asyncio
async def test_double_mount_event_trace_message_background_and_catalog_isolation(
    mounted_case: MountedCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with main.lifespan(main.app):
        a, b = mounted_case.containers
        sessions = []
        for container in (a, b):
            with workspace_context(
                container.workspace_id, container.workspace_root,
                session_catalog=container.session_catalog,
            ):
                sessions.append((await _session(RunnerCase(container, container.workspace_root)))[0])
        session_a, session_b = sessions
        job_id = "job_same_id_on_both_mounts"
        subscriptions = [
            await container.job_event_bus.subscribe(job_id, subscriber_kind="double-mount-integration")
            for container in (a, b)
        ]
        writers = []
        message_subscriptions = []
        try:
            event_a = await a.job_event_bus.publish(job_id, EventType.JOB_CREATED, {
                "session_id": session_a, "thread_id": a.session_catalog.resolver.main_thread_id(session_a),
                "message": "仅 A 的事件", "agent_id": "default",
            })
            assert (await asyncio.wait_for(subscriptions[0].get(), 2)).event_id == event_a.event_id
            assert subscriptions[1].empty()
            event_b = await b.job_event_bus.publish(job_id, EventType.JOB_CREATED, {
                "session_id": session_b, "thread_id": b.session_catalog.resolver.main_thread_id(session_b),
                "message": "仅 B 的事件", "agent_id": "default",
            })
            assert (await asyncio.wait_for(subscriptions[1].get(), 2)).event_id == event_b.event_id
            assert subscriptions[0].empty()
            assert [event.event_id for event in a.trace_event_store.read_events(session_a)] == [event_a.event_id]
            assert [event.event_id for event in b.trace_event_store.read_events(session_b)] == [event_b.event_id]
            nodes = [
                container.session_catalog.resolver.resolve_session_node_for_runtime(session_id)
                for container, session_id in zip((a, b), sessions, strict=True)
            ]
            for container, node in zip((a, b), nodes, strict=True):
                assert node.is_relative_to(container.workspace_root / ".boxteam/sessions")
                assert (node / "logs/traces/events.jsonl").is_file()
            for container, session_id in zip((a, b), sessions, strict=True):
                writer = await container.message_stream_store.open(thread_id=container.message_stream_store._path_resolver.main_thread_id(session_id),
                    session_id=session_id, turn_id=job_id, job_id=job_id,
                )
                writers.append(writer)
                message_subscriptions.append(await container.message_stream_store.subscribe(writer.turn_stream_id))
            await writers[0].commit("block.started", {
                "block_id": "same-block", "block_index": 0, "carrier_type": "text",
            }, block_id="same-block")
            assert (await asyncio.wait_for(message_subscriptions[0].get(), 2)).event["type"] == "block.started"
            assert message_subscriptions[1].queue.empty()
            await writers[0].commit("block.delta", {
                "block_id": "same-block", "carrier_type": "text", "operation": "append", "text": "仅 A",
            }, block_id="same-block")
            await writers[1].commit("block.started", {
                "block_id": "same-block", "block_index": 0, "carrier_type": "text",
            }, block_id="same-block")
            assert (await asyncio.wait_for(message_subscriptions[1].get(), 2)).event["type"] == "block.started"
            await writers[1].commit("block.delta", {
                "block_id": "same-block", "carrier_type": "text", "operation": "append", "text": "仅 B",
            }, block_id="same-block")
            for index, (container, other, session_id, node) in enumerate(zip(
                (a, b), (b, a), sessions, nodes, strict=True,
            )):
                await writers[index].close_completed()
                state = await container.message_stream_store.get_state(writers[index].turn_stream_id)
                assert state["blocks"][0]["text"] == ("仅 A" if index == 0 else "仅 B")
                events = await container.message_stream_store.list_events(
                    session_id=session_id, turn_stream_id=writers[index].turn_stream_id,
                )
                assert events[-1]["type"] == "stream.completed"
                assert (node / f"message_streams/{writers[index].turn_stream_id}.jsonl").is_file()
                with pytest.raises(MessageStreamNotFoundError):
                    await other.message_stream_store.get_state(writers[index].turn_stream_id)

            started_a, started_b, finish_b = asyncio.Event(), asyncio.Event(), asyncio.Event()

            from datetime import UTC, datetime

            from app.core.background_task_contracts import BackgroundTaskDescriptor
            from app.services.infrastructure.background_work_handlers import (
                SystemTimeEmitterHandler,
            )

            real_step = SystemTimeEmitterHandler.step

            async def controlled_step(handler, handle):
                if handle.descriptor.payload["source_id"] == "A":
                    started_a.set()
                    await asyncio.Event().wait()
                else:
                    started_b.set()
                    await finish_b.wait()
                return await real_step(handler, handle)

            monkeypatch.setattr(SystemTimeEmitterHandler, "step", controlled_step)
            handles = []
            for container, session_id, source in ((a, session_a, "A"), (b, session_b, "B")):
                handles.append(await container.background_task_registry.spawn(
                    session_id=session_id, thread_id=container.session_catalog.resolver.main_thread_id(session_id),
                    agent_id="default", task_id="bgt_" + source,
                    descriptor=BackgroundTaskDescriptor(kind="emit_system_time_messages", version=1, payload={
                        "interval_seconds": 0.001, "message_count": 1, "source_id": source,
                        "started_at": datetime.now(UTC).isoformat(),
                    }),
                ))
            handle_a, handle_b = handles
            await asyncio.wait_for(asyncio.gather(started_a.wait(), started_b.wait()), 2)
            await a.background_task_registry.cancel(session_a, handle_a.task_id)
            task_b = b.background_task_registry.get_task(session_b, handle_b.task_id)
            assert task_b is not None and not task_b.done()
            assert b.background_task_registry.get_handle(session_b, handle_b.task_id).status == "running"
            finish_b.set()
            await asyncio.wait_for(task_b, 2)
            assert [(handle.task_id, handle.status) for handle in a.background_task_registry.list_closed_handles(session_a)] == [(handle_a.task_id, "cancelled")]
            assert [(handle.task_id, handle.status) for handle in b.background_task_registry.list_closed_handles(session_b)] == [(handle_b.task_id, "completed")]
            for container, session_id, expected in ((a, session_a, handle_a.task_id), (b, session_b, handle_b.task_id)):
                with container.background_task_registry._store.control(session_id, read_only=True) as control:
                    assert [item.task_id for item in control.list_background_tasks()] == [expected]
                assert not (container.session_catalog.resolver.resolve_session_node(session_id) / "resources/background_tasks.json").exists()

            store_a, store_b = (container.session_catalog.resolver.catalog_store for container in (a, b))
            before_a, before_b = store_a.current_generation(), store_b.current_generation()
            locked, reader_started, release = threading.Event(), threading.Event(), threading.Event()

            def hold_a():
                with store_a.write_transaction() as connection:
                    connection.execute("SELECT COUNT(*) FROM nodes")
                    locked.set()
                    assert release.wait(5), "A 事务未收到释放信号"

            def read_a():
                reader_started.set()
                return store_a.current_generation()

            def write_b():
                with store_b.write_transaction() as connection:
                    connection.execute("SELECT COUNT(*) FROM nodes")
                return store_b.current_generation()

            holding = asyncio.create_task(asyncio.to_thread(hold_a))
            await asyncio.wait_for(asyncio.to_thread(locked.wait), 2)
            reading = asyncio.create_task(asyncio.to_thread(read_a))
            try:
                await asyncio.wait_for(asyncio.to_thread(reader_started.wait), 2)
                assert await asyncio.wait_for(asyncio.to_thread(write_b), 2) == before_b + 1
                assert not reading.done(), "A 读绕过了同一 catalog 真实事务锁"
            finally:
                release.set()
                await asyncio.wait_for(holding, 2)
            assert await asyncio.wait_for(reading, 2) == before_a + 1
        finally:
            for container, subscription in zip((a, b), subscriptions, strict=True):
                await container.job_event_bus.unsubscribe(job_id, subscription, reason="double-mount-test-completed")
            for container, subscription in zip((a, b), message_subscriptions, strict=False):
                await container.message_stream_store.unsubscribe(subscription)
    assert_closed(mounted_case)


@pytest.mark.asyncio
async def test_double_mount_consumes_each_explicit_candidate_and_ignores_process_candidate(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mounts = []
    expected = []

    async def require_restart(*_args):
        raise ConfigRestartRequiredError("隔离工作区配置需要重启", changed_sections=("logger",))

    for identity, root in mounted_case.roots.items():
        with WorkspaceStateStore(workspace_root=root) as store:
            config = ConfigService(workspace_root=root, workspace_state_store=store)
            config.validate_workspace_config()
            agent_id = config.get_snapshot().to_dict()["default_agent"]
            prompt = f"仅挂载工作区 {identity} 的候选说明"
            # 日志属于同一进程，两个 mount 保持同一值；工作区说明各自独立。
            (root / ".boxteam/workspace.jsonc").write_text(json.dumps({
                "logger": {"level": "debug"},
                "agents": {agent_id: {"instructions": {"system_prompt": prompt}}},
            }))
            with pytest.raises(ConfigRestartRequiredError):
                await config.reload(candidate_applier=require_restart)
            pending = store.get_pending_config_candidate(config_domain="workspace")
            assert pending is not None
            assert pending.candidate_ref and pending.target_generation and pending.fencing_token
            mounts.append(MountedWorkspace(
                identity, root.resolve(), config_candidate_ref=pending.candidate_ref,
                config_generation=pending.target_generation, config_fencing_token=pending.fencing_token,
            ))
            expected.append((agent_id, prompt, pending))
    assert expected[0][2].effective_digest != expected[1][2].effective_digest
    assert mounts[0].config_candidate_ref != mounts[1].config_candidate_ref
    assert mounts[0].config_generation != mounts[1].config_generation
    assert mounts[0].config_fencing_token != mounts[1].config_fencing_token
    monkeypatch.setenv("BOXTEAM_CONFIG_CANDIDATE_REF", mounts[0].config_candidate_ref)
    monkeypatch.setenv("BOXTEAM_CONFIG_GENERATION", mounts[0].config_generation)
    monkeypatch.setenv("BOXTEAM_CONFIG_FENCING_TOKEN", mounts[0].config_fencing_token)
    monkeypatch.setenv("BOXTEAM_MOUNTED_WORKSPACES", json.dumps([mount.to_payload() for mount in mounts]))
    async with main.lifespan(main.app):
        for container, (agent_id, prompt, pending) in zip(mounted_case.containers, expected, strict=True):
            proof = container.config_service.get_loaded_config_proof()
            snapshot = container.config_service.get_snapshot().to_dict()
            assert snapshot["logger"]["level"] == "debug"
            assert snapshot["agents"][agent_id]["instructions"]["system_prompt"] == prompt
            assert proof["loaded_source"] == "pending"
            assert proof["candidate_id"] == pending.candidate_id
            assert proof["candidate_digest"] == pending.candidate_digest
            assert proof["effective_digest"] == pending.effective_digest
            assert proof["generation_id"] == pending.target_generation
            assert proof["fencing_token_digest"] == hashlib.sha256(pending.fencing_token.encode()).hexdigest()
    assert_closed(mounted_case)
