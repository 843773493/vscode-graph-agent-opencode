"""持久任务管理与目标校验；所有修改都回到原工作区任务账本。"""

from __future__ import annotations

from app.core.resource_uri import parse_session_thread_uri, thread_resource_uri
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskCheckRequest,
    PersistentTaskConfigurationDTO,
    PersistentTaskControlRequest,
    PersistentTaskCreateRequest,
    PersistentTaskDTO,
    PersistentTaskNotificationRequest,
    PersistentTaskRunDTO,
    PersistentTaskUpdateRequest,
    TaskLifecycle,
)
from app.services.business.session_service import SessionService
from app.services.orchestration.persistent_tasks.runtime import PersistentTaskRuntime


class PersistentTaskService:
    def __init__(self, *, runtime: PersistentTaskRuntime, sessions: SessionService) -> None:
        self.runtime = runtime
        self._sessions = sessions
        self._store = runtime.store

    async def _validate_target(self, configuration: PersistentTaskConfigurationDTO) -> PersistentTaskConfigurationDTO:
        resource, session_id, thread_id = parse_session_thread_uri(configuration.action.target_uri)
        if resource.scope_id != self._sessions.workspace_id:
            raise ValueError("持久任务目标必须属于任务所在工作区")
        if thread_id is None:
            raise ValueError("持久任务目标必须明确 Thread")
        gateway_id = get_current_gateway_id()
        if resource.gateway_authority is not None and resource.gateway_authority != gateway_id:
            raise ValueError("持久任务目标必须属于当前 Gateway")
        async with self._sessions.session_read_admission(session_id):
            self._sessions.path_resolver.resolve_thread_node(session_id, thread_id)
        if resource.gateway_authority is None and gateway_id is not None:
            action = configuration.action.model_copy(update={"target_uri": thread_resource_uri(
                resource.scope_id, session_id, thread_id, gateway_authority=gateway_id,
            )})
            return configuration.model_copy(update={"action": action})
        return configuration

    def get(self, task_id: str) -> PersistentTaskDTO:
        task = self._store.get(task_id)
        recovering = self.runtime.recovering(task_id)
        pending = self._store.runs(task_id, pending_only=True)
        phase = "recovering" if recovering else (pending[0].phase if pending else task.phase)
        return task.model_copy(update={"recovering": recovering, "phase": phase})

    def list(self) -> list[PersistentTaskDTO]:
        return [self.get(task.task_id) for task in self._store.list()]

    async def create(self, request: PersistentTaskCreateRequest) -> PersistentTaskDTO:
        self.runtime.assert_healthy()
        request = request.model_copy(update={"configuration": await self._validate_target(request.configuration)})
        task = self._store.create(request, self.runtime.clock())
        self.runtime.notify(task.task_id)
        return self.get(task.task_id)

    async def update(self, task_id: str, request: PersistentTaskUpdateRequest) -> PersistentTaskDTO:
        self.runtime.assert_healthy()
        configuration = await self._validate_target(request.configuration)
        async with self.runtime.locks.lock_for(task_id):
            self._store.update(task_id, expected_revision=request.expected_revision, configuration=configuration,
                               now=self.runtime.clock())
            self.runtime.notify(task_id)
            return self.get(task_id)

    async def control(self, task_id: str, request: PersistentTaskControlRequest) -> PersistentTaskDTO:
        lifecycles: dict[str, TaskLifecycle] = {"pause": "paused", "resume": "active", "terminate": "terminated"}
        lifecycle = lifecycles[request.action]
        async with self.runtime.locks.lock_for(task_id):
            self._store.control(task_id, expected_revision=request.expected_revision, lifecycle=lifecycle, now=self.runtime.clock())
            self.runtime.notify(task_id)
            return self.get(task_id)

    async def check(self, task_id: str, request: PersistentTaskCheckRequest | PersistentTaskNotificationRequest) -> PersistentTaskRunDTO:
        self.runtime.assert_healthy()
        async with self.runtime.locks.lock_for(task_id):
            run = self._store.trigger(task_id, request, self.runtime.clock(), request_id=get_current_request_id())
            self.runtime.notify(task_id, run.run_id)
            return run

    def runs(self, task_id: str) -> list[PersistentTaskRunDTO]:
        return self._store.runs(task_id)
