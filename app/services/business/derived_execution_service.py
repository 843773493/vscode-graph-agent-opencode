"""派生提交及有限观察；任务账本、准入和恢复继续由原后台注册器裁决。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

from boxteam_plugin_sdk.failures import bounded_failure_text
from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded
from pydantic import JsonValue

from app.abstractions.background_task_registry import BackgroundTaskRegistryProtocol
from app.agents.instruction_producers import freeze_initial_derived_sources
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_current_gateway_id
from app.domain.background_work import (
    ACTIVE_BACKGROUND_TASK_STATUSES,
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
)
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.derived_execution import (
    DerivedDispatchReferenceDTO,
    DerivedExecutionDTO,
    DerivedExecutionResultDTO,
    DerivedExecutionSubmitRequest,
    DerivedItemReferenceDTO,
)
from app.services.business.agent_tool_settings.service import AgentToolSettingsService
from app.services.business.session_service import SessionService
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.background_work.derived_execution import (
    DerivedExecutionHandler,
)


class DerivedExecutionConflictError(ValueError):
    """同一幂等身份已经绑定另一份显式输入或 Thread。"""


class DerivedExecutionBusyError(RuntimeError):
    """原调度额度不足；没有接受新任务。"""


class DerivedExecutionService:
    def __init__(self, *, registry: BackgroundTaskRegistryProtocol, handler: DerivedExecutionHandler,
                 store: BackgroundWorkStore, saver: RolloutCheckpointSaver,
                 sessions: SessionService, config: ConfigService,
                 tool_settings: Callable[[], AgentToolSettingsService]) -> None:
        self._registry, self._handler, self._store = registry, handler, store
        self._saver, self._sessions, self._config = saver, sessions, config
        self._tool_settings = tool_settings

    async def submit(self, request: DerivedExecutionSubmitRequest) -> DerivedExecutionDTO:
        submission_hash = sha256_jcs(request.model_dump(mode="json", exclude={"foreground"}))
        deadline = datetime.now(UTC) + timedelta(seconds=request.timeout_seconds)
        identity = sha256_jcs({"session_id": request.session_id, "idempotency_key": request.idempotency_key}).rsplit(":", 1)[1]
        task_id, plan_id = f"derived-{identity}", f"derived-plan-{identity}"
        async with self._store.admission(request.session_id):
            self._store.resolve_thread(request.session_id, request.thread_id)
            session = await self._sessions.get(request.session_id)
            existing = self._registry.get_handle(request.session_id, task_id)
            if existing is not None:
                await self._check_submission(existing, request.thread_id, submission_hash)

        @asynccontextmanager
        async def freeze(existing: BackgroundTaskHandle | None) -> AsyncIterator[BackgroundTaskDescriptor]:
            if existing is not None:
                async with self._store.admission(request.session_id):
                    await self._check_submission(existing, request.thread_id, submission_hash)
                yield existing.descriptor
                return
            async with self._store.admission(request.session_id):
                gateway_id = get_current_gateway_id()
                initial_tool_set = None
                if request.allowed_tool_ids:
                    if gateway_id is None:
                        raise ValueError("derived-tool-context-unsupported: 文件工具需要真实 Gateway 身份")
                    policies, initial_tool_set = await run_sync_shielded(
                        self._tool_settings().derived_tools, session.current_agent_id, tuple(request.allowed_tool_ids),
                    )
                    denied = [name for name, policy in policies.items() if not policy.execution_enabled or policy.confirmation_required]
                    if denied:
                        raise ValueError(f"derived-tool-denied: 工具被禁用或需要审批: {', '.join(denied)}")
                snapshot = self._config.get_snapshot()
                with self._config.use_snapshot(snapshot):
                    reference = self._config.resolve_agent_model(session.current_agent_id, request.model)
                    recipe = self._config.freeze_model_recipe(reference)
                    initial_sources = freeze_initial_derived_sources(
                        agent_id=session.current_agent_id,
                        runtime_config=self._config.get_agent_runtime_config(session.current_agent_id, preferred_model=request.model),
                        config_revision=snapshot.revision,
                        provider_candidates=((reference.provider_id, reference.model_id),),
                    )
                try:
                    ref = await run_sync_shielded(
                        self._saver.freeze_derived_input, request.session_id, request.thread_id,
                        task_id=task_id, plan_id=plan_id, instruction=request.instruction,
                        allowed_tool_ids=tuple(request.allowed_tool_ids),
                        deadline=deadline,
                        max_model_calls=request.max_model_calls, submission_hash=submission_hash,
                        model_recipe=recipe, item_ids=None if request.source is None else request.source.item_ids,
                        initial_sources=initial_sources,
                        gateway_id=gateway_id,
                        initial_tool_set=initial_tool_set,
                    )
                except ValueError as error:
                    if str(error).startswith("derived-input-conflict:"):
                        raise DerivedExecutionConflictError(str(error)) from error
                    raise
                frozen = await run_sync_shielded(self._saver.read_derived_input, request.session_id, request.thread_id, ref)
                descriptor = BackgroundTaskDescriptor(kind=self._handler.kind, version=self._handler.version,
                    payload={"input_ref": cast(JsonValue, ref.to_dict()), "checkpoint_ns": frozen.checkpoint_ns,
                             "deadline": frozen.deadline.isoformat()})
            try:
                yield descriptor
            except BaseException:
                # 原调度锁仍由调用方持有；失败清理结束后才允许下一次同键提交。
                if self._registry.get_handle(request.session_id, task_id) is None:
                    await run_cleanup_shielded(lambda: run_sync_shielded(
                        self._saver.discard_derived_input, request.session_id, request.thread_id, plan_id=plan_id,
                    ))
                raise

        try:
            handle = await self._registry.spawn_derived(
                session_id=request.session_id, thread_id=request.thread_id, agent_id=session.current_agent_id,
                task_id=task_id, descriptor=freeze, foreground=request.foreground,
            )
        except ValueError as error:
            if str(error).startswith("derived-input-conflict:"):
                raise DerivedExecutionConflictError(str(error)) from error
            raise
        if handle is None:
            raise DerivedExecutionBusyError("派生执行额度已满，本次请求未受理")
        await self._check_submission(handle, request.thread_id, submission_hash)
        return await self.get(request.session_id, request.thread_id, task_id)

    async def _check_submission(self, handle: BackgroundTaskHandle, thread_id: str, submission_hash: str) -> None:
        self._check_scope(handle, thread_id)
        frozen = await run_sync_shielded(self._handler.read_input, handle)
        if frozen.submission_hash != submission_hash:
            raise DerivedExecutionConflictError("幂等键已经绑定不同的显式输入")

    @staticmethod
    def _check_scope(handle: BackgroundTaskHandle, thread_id: str) -> None:
        if handle.thread_id != thread_id or (handle.descriptor.kind, handle.descriptor.version) != (
            DerivedExecutionHandler.kind, DerivedExecutionHandler.version,
        ):
            raise DerivedExecutionConflictError("task_id 不属于请求的派生 Thread")

    async def _read_handle(self, session_id: str, thread_id: str, task_id: str) -> BackgroundTaskHandle:
        async with self._store.admission(session_id):
            self._store.resolve_thread(session_id, thread_id)
            handle = self._registry.get_handle(session_id, task_id)
            if handle is None:
                raise LookupError("派生任务不存在")
            self._check_scope(handle, thread_id)
        return handle

    async def get(self, session_id: str, thread_id: str, task_id: str, *, wait_seconds: float = 0) -> DerivedExecutionDTO:
        await self._read_handle(session_id, thread_id, task_id)
        await self._registry.recover_derived_thread(session_id, thread_id)
        observe_until = asyncio.get_running_loop().time() + wait_seconds
        while True:
            handle = await self._read_handle(session_id, thread_id, task_id)
            remaining = observe_until - asyncio.get_running_loop().time()
            if handle.status not in ACTIVE_BACKGROUND_TASK_STATUSES or remaining <= 0:
                return await self._snapshot(handle)
            # 取消 HTTP 观察只结束本协程，后台 task 仍由 registry 独立持有。
            await asyncio.sleep(min(0.05, remaining))

    async def cancel(self, session_id: str, thread_id: str, task_id: str) -> DerivedExecutionDTO:
        await self._read_handle(session_id, thread_id, task_id)
        return await self._snapshot(await self._registry.cancel(session_id, task_id))

    async def _snapshot(self, handle: BackgroundTaskHandle) -> DerivedExecutionDTO:
        frozen = await run_sync_shielded(self._handler.read_input, handle)
        result = await run_sync_shielded(self._handler.completed_result, frozen) if handle.status == "completed" else None
        if handle.status == "completed" and result is None:
            raise RuntimeError("已完成派生任务缺少 canonical 完整结果")
        source = frozen.refs[-1]
        note = handle.metadata.get("status_note")
        return DerivedExecutionDTO(task_id=handle.task_id, session_id=handle.session_id, thread_id=handle.thread_id,
            status=handle.status, created_at=handle.created_at, started_at=handle.started_at, ended_at=handle.ended_at,
            error=bounded_failure_text(note) if handle.status == "failed" and isinstance(note, str) else None,
            source_view_id=frozen.source_view_id, source_last_ref=DerivedItemReferenceDTO(
                session_id=source.session_id, thread_id=source.thread_id, item_id=source.ref_id, content_hash=source.content_hash),
            result=None if result is None else self._result_dto(result))

    @staticmethod
    def _result_dto(result: Mapping[str, object]) -> DerivedExecutionResultDTO:
        dispatch = cast(Mapping[str, str], result["dispatch_ref"])
        outputs = cast(list[Mapping[str, str]], result["output_refs"])
        return DerivedExecutionResultDTO(execution_id=cast(str, result["execution_id"]),
            dispatch_ref=DerivedDispatchReferenceDTO(assembly_id=dispatch["assembly_id"],
                model_call_id=dispatch["sealed_model_call_id"], request_hash=dispatch["request_hash"], reference_hash=dispatch["reference_hash"]),
            output_refs=[DerivedItemReferenceDTO(session_id=ref["session_id"], thread_id=ref["thread_id"],
                item_id=ref["ref_id"], content_hash=ref["content_hash"]) for ref in outputs[:128]],
            output_ref_count=len(outputs), output_refs_truncated=len(outputs) > 128)
