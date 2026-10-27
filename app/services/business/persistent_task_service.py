"""持久任务管理与目标校验；所有修改都回到原工作区任务账本。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from app.core.resource_uri import parse_session_thread_uri, thread_resource_uri
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.domain.itemized.records import RuntimeNoticePayload
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskCheckRequest,
    PersistentTaskConfigurationDTO,
    PersistentTaskControlRequest,
    PersistentTaskCreateRequest,
    PersistentTaskDTO,
    PersistentTaskNotificationRequest,
    PersistentTaskRunDetailDTO,
    PersistentTaskRunDTO,
    PersistentTaskUpdateRequest,
    SessionGenerationActionDTO,
    TaskInputConsumptionDTO,
    TaskLifecycle,
    TaskThreadTargetDTO,
)
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationValidateRequest,
)
from app.services.business.session_service import SessionService
from app.services.orchestration.persistent_tasks.conditions import condition_input
from app.services.orchestration.persistent_tasks.runtime import PersistentTaskRuntime


class PersistentTaskService:
    def __init__(self, *, runtime: PersistentTaskRuntime, sessions: SessionService) -> None:
        self.runtime = runtime
        self._sessions = sessions
        self._store = runtime.store

    async def _validate_thread_uri(self, uri: str) -> str:
        resource, session_id, thread_id = parse_session_thread_uri(uri)
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
            return thread_resource_uri(
                resource.scope_id, session_id, thread_id, gateway_authority=gateway_id,
            )
        return uri

    async def _validate_target(self, configuration: PersistentTaskConfigurationDTO) -> PersistentTaskConfigurationDTO:
        if isinstance(configuration.action, SessionGenerationActionDTO):
            generation = configuration.action.generation
            if generation.gateway_id is not None and generation.gateway_id != get_current_gateway_id():
                raise ValueError("会话动作目标必须属于当前 Gateway")
            result = await self.runtime.generation.validate_definition(SessionGenerationValidateRequest(
                name=configuration.name, execution_workspace_id=self._sessions.workspace_id,
                **generation.model_dump(exclude={"created_from_uri", "gateway_id", "ui_policy", "mount_missing", "delete_outputs"}),
            ))
            if not result.provider_available:
                raise ValueError(result.reason)
            if generation.created_from_uri is not None:
                parse_session_thread_uri(generation.created_from_uri)
            generation = generation.model_copy(update={"gateway_id": get_current_gateway_id()})
            action = configuration.action.model_copy(update={"generation": generation})
        else:
            action = configuration.action.model_copy(update={"target_uri": await self._validate_thread_uri(configuration.action.target_uri)})
        trigger = configuration.trigger
        if trigger.source_uri is not None:
            trigger = trigger.model_copy(update={"source_uri": await self._validate_thread_uri(trigger.source_uri)})
        condition = configuration.condition
        if condition.model is not None:
            model = condition.model.model_copy(update={"thread_uri": await self._validate_thread_uri(condition.model.thread_uri)})
            condition = condition.model_copy(update={"model": model})
        return configuration.model_copy(update={"action": action, "trigger": trigger, "condition": condition})

    def get(self, task_id: str) -> PersistentTaskDTO:
        task = self._store.get(task_id)
        recovering = self.runtime.recovering(task_id)
        if task.application_type == "goal":
            return task.model_copy(update={"recovering": recovering, "phase": "recovering" if recovering else task.phase})
        pending = self._store.runs(task_id, pending_only=True)
        phase = "recovering" if recovering else (pending[0].phase if pending else task.phase)
        return task.model_copy(update={"recovering": recovering, "phase": phase})

    def list(self, session_id: str | None = None) -> list[PersistentTaskDTO]:
        tasks = self._store.list()
        if session_id is not None:
            generated = self._store.generated_for_session(session_id)
            tasks = [task for task in tasks if task.task_id in generated or self._references_session(task, session_id)]
        return [self.get(task.task_id) for task in tasks]

    @staticmethod
    def _references_session(task: PersistentTaskDTO, session_id: str) -> bool:
        if task.goal is not None:
            return task.goal.session_id == session_id
        configuration = task.configuration
        if configuration is None:
            raise RuntimeError("自动化关联查询缺少任务定义")
        action = configuration.action
        uris = [configuration.trigger.source_uri]
        if configuration.condition.model is not None:
            uris.append(configuration.condition.model.thread_uri)
        if isinstance(action, SessionGenerationActionDTO):
            generation = action.generation
            target = generation.session_strategy.target
            if target is not None and target.workspace_id == task.workspace_id and target.session_id == session_id:
                return True
            source = generation.context_source
            if source.workspace_id == task.workspace_id and source.session_id == session_id:
                return True
        else:
            uris.append(action.target_uri)
        return any(uri is not None and parse_session_thread_uri(uri)[1] == session_id for uri in uris)

    async def create(self, request: PersistentTaskCreateRequest) -> PersistentTaskDTO:
        self.runtime.assert_healthy()
        request = request.model_copy(update={"configuration": await self._validate_target(request.configuration)})
        task = self._store.create(request, self.runtime.clock(),
                                  source_cursor=await self.runtime.execution_source_cursor(request.configuration))
        self.runtime.reconcile_execution_source(task.task_id)
        self.runtime.notify(task.task_id)
        return self.get(task.task_id)

    async def update(self, task_id: str, request: PersistentTaskUpdateRequest) -> PersistentTaskDTO:
        self.runtime.assert_healthy()
        configuration = await self._validate_target(request.configuration)
        async with self.runtime.locks.lock_for(task_id):
            self._store.update(task_id, expected_revision=request.expected_revision, configuration=configuration,
                               now=self.runtime.clock(), source_cursor=await self.runtime.execution_source_cursor(configuration))
            self.runtime.reconcile_execution_source(task_id)
            self.runtime.notify(task_id)
            return self.get(task_id)

    async def control(self, task_id: str, request: PersistentTaskControlRequest) -> PersistentTaskDTO:
        lifecycles: dict[str, TaskLifecycle] = {"pause": "paused", "resume": "active", "terminate": "terminated"}
        lifecycle = lifecycles[request.action]
        async with self.runtime.locks.lock_for(task_id):
            configuration = self._store.get_automation(task_id).configuration
            cursor = await self.runtime.execution_source_cursor(configuration) if lifecycle == "active" else None
            self._store.control(task_id, expected_revision=request.expected_revision, lifecycle=lifecycle,
                                now=self.runtime.clock(), source_cursor=cursor)
            self.runtime.reconcile_execution_source(task_id)
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

    @staticmethod
    def _thread_target(uri: str) -> TaskThreadTargetDTO:
        _resource, session_id, thread_id = parse_session_thread_uri(uri)
        if thread_id is None:
            raise RuntimeError("冻结任务引用缺少明确Thread")
        return TaskThreadTargetDTO(session_id=session_id, thread_id=thread_id, uri=uri)

    async def inspect_run(self, task_id: str, run_id: str) -> PersistentTaskRunDetailDTO:
        run = self._store.run(task_id, run_id)
        configuration = self._store.configuration(run)
        action = configuration.action
        work_target = None
        if isinstance(action, SessionGenerationActionDTO):
            if run.generation is not None and run.generation.outputs:
                session_id = run.generation.outputs[0].session_id
                target = action.generation.session_strategy.target
                thread_id = (target.thread_id if action.generation.session_strategy.mode == "continue_existing" and target is not None
                             else await self._sessions.resolve_main_thread(session_id))
                work_target = self._thread_target(thread_resource_uri(
                    self._sessions.workspace_id, session_id, thread_id, gateway_authority=action.generation.gateway_id,
                ))
        else:
            work_target = self._thread_target(action.target_uri)
        detail = PersistentTaskRunDetailDTO(
            run=run, configuration=configuration,
            check_input=condition_input(configuration, run, workspace_id=self._sessions.workspace_id),
            work_target=work_target,
            evaluation_target=self._thread_target(run.evaluation.thread_uri) if run.evaluation is not None else None,
        )
        if run.delivery is None or work_target is None:
            return detail
        session_id, thread_id = work_target.session_id, work_target.thread_id
        saver = self.runtime._saver
        async with self._sessions.session_read_admission(session_id):
            self._sessions.path_resolver.resolve_thread_node(session_id, thread_id)
            if isinstance(action, SessionGenerationActionDTO):
                assert run.generation is not None and run.generation.job_id is not None and run.generation.message_id is not None
                item = saver.accepted_user_input(session_id, thread_id=thread_id, job_id=run.generation.job_id)
                content = saver.accepted_user_message(session_id, thread_id=thread_id, job_id=run.generation.job_id,
                                                      message_id=run.generation.message_id).content
                if not isinstance(content, str):
                    raise TypeError("会话生成输入应是冻结的文本消息")
            else:
                if run.delivery.item_id is None:
                    raise RuntimeError("任务发布回执缺少canonical输入身份")
                item = saver.get_canonical_item(session_id, thread_id=thread_id, item_id=run.delivery.item_id)
                if item is None:
                    raise RuntimeError("任务发布的canonical正文缺失")
                if isinstance(item.payload, str):
                    content = item.payload
                elif isinstance(item.payload, Mapping):
                    content = RuntimeNoticePayload.from_dict(item.payload).prompt
                else:
                    raise TypeError("任务发布的canonical正文类型无效")
            consumption = saver.runtime_input_consumption(
                session_id, thread_id, item_id=item.item_id, source_id=run.delivery.source_id,
                source_revision=run.delivery.source_revision if configuration.context.mode == "state" else None,
            )
            outcome = None if run.delivery.execution_id is None else saver.runtime_execution_outcome(
                session_id, thread_id, run.delivery.execution_id,
            )
        return detail.model_copy(update={
            "action_content": content, "execution_outcome": outcome,
            "consumption": TaskInputConsumptionDTO(**asdict(consumption)) if consumption is not None else None,
        })
