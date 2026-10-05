"""真实双 AppContainer 的请求、compiler 身份与逆序 lifespan 排空。"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app import container as container_module
from app import main
from app.agents import agent_factory
from app.container import AppContainer
from app.core.path_utils import (
    get_workspace_id,
    get_workspace_root,
    workspace_context,
)
from app.core.session_thread_context import current_session_thread_owner
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.goal import GoalStatus
from app.services.business.job.service import JobState
from app.services.infrastructure.resource_platform.bootstrap import ResourcePlatform
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_runner_cancellation import (
    RunnerCase,
    _session,
    _start_job,
)
from tests.support.workspaces import prepare_default_test_workspace


class _WorkspaceModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


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
    monkeypatch.setenv("BOXTEAM_MOUNTED_WORKSPACES", json.dumps([{"workspace_id": identity, "workspace_root": str(root)} for identity, root in roots.items()]))
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

    def model_runtime(**kwargs):
        owner = current_session_thread_owner()
        assert owner is not None
        identity, root = get_workspace_id(), get_workspace_root()
        assert roots[identity] == root
        case.compiled.append((identity, root, owner.session_id, owner.thread_id))
        return {"model": _WorkspaceModel(name="BoxteamLiteLLMChatModel", responses=[AIMessage(content=f"工作区 {identity} 的真实编译图")]), "system_prompt": "返回固定回复", "model_routing": None}

    monkeypatch.setattr(agent_factory, "build_runtime_for_agent", model_runtime)
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

    monkeypatch.setattr(_WorkspaceModel, "_agenerate", blocked_model)

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
