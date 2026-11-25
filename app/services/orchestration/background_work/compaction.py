"""压缩业务 handler：只生成候选，原任务账本独占期限、取消和恢复。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded
from pydantic import JsonValue

from app.abstractions.background_task_registry import BackgroundTaskRegistryProtocol
from app.abstractions.trace_event_sink import TraceEventSinkProtocol
from app.agents.summarization.generation import (
    MAX_DERIVED_CALLS,
    CompactionGenerator,
    SummaryCallBudget,
)
from app.core.path_utils import SessionCatalogComponents, workspace_context
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.background_work import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
)
from app.domain.itemized.compaction import FrozenCompactionTaskInput
from app.domain.itemized.identity.detail_ref import DetailRef
from app.runtime.provider_model import build_model_from_provider
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator


class CompactionTaskHandler:
    kind = "context_compaction"
    version = 1
    effect: Literal["derived_request"] = "derived_request"

    def __init__(self, *, saver: RolloutCheckpointSaver, config: ConfigService,
                 activation: ResourceActivationCoordinator, registry: Callable[[], BackgroundTaskRegistryProtocol],
                 workspace_id: str, workspace_root: Path, session_catalog: SessionCatalogComponents,
                 trace_sink: TraceEventSinkProtocol,
                 provider_wait: Callable[[BackgroundTaskHandle, bool], None] | None = None) -> None:
        self._saver, self._config, self._activation, self._registry = saver, config, activation, registry
        self._workspace_id, self._workspace_root, self._session_catalog = workspace_id, workspace_root, session_catalog
        self._provider_wait = provider_wait
        self._trace_sink = trace_sink

    @staticmethod
    def descriptor_input(descriptor: BackgroundTaskDescriptor) -> tuple[DetailRef, str, datetime]:
        payload = descriptor.payload
        if set(payload) != {"input_ref", "checkpoint_ns", "deadline"}:
            raise ValueError("压缩 descriptor 只能携带冻结输入引用、namespace 和绝对期限")
        namespace, deadline = payload["checkpoint_ns"], payload["deadline"]
        if not isinstance(namespace, str) or not isinstance(deadline, str):
            raise TypeError("压缩 descriptor 缺少合法 namespace 或期限")
        absolute = datetime.fromisoformat(deadline)
        if absolute.tzinfo is None:
            raise ValueError("压缩期限必须带时区")
        return DetailRef.from_dict(payload["input_ref"]), namespace, absolute

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]:
        self.descriptor_input(descriptor)
        return {}

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None:
        if checkpoint:
            raise ValueError("压缩 task checkpoint 不得另存输入、结果或执行进度")

    @staticmethod
    def deadline(handle: BackgroundTaskHandle) -> datetime:
        value = handle.metadata["deadline"]
        if not isinstance(value, str):
            raise TypeError("压缩 attempt 缺少合法绝对期限")
        deadline = datetime.fromisoformat(value)
        if deadline.tzinfo is None:
            raise ValueError("压缩 attempt 期限必须带时区")
        return deadline

    def read_input(self, handle: BackgroundTaskHandle) -> FrozenCompactionTaskInput:
        ref, namespace, deadline = self.descriptor_input(handle.descriptor)
        value = self._saver._storage.compactions.read_task_input(handle.session_id, handle.thread_id, ref, checkpoint_ns=namespace)
        if value.frozen.task_id != handle.task_id or value.deadline != deadline:
            raise ValueError("source-mismatch: 原 task 与冻结压缩输入不一致")
        return value

    def result(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None:
        value = self.read_input(handle)
        frozen = value.frozen
        result = self._saver._storage.compactions.read_result(
            frozen.session_id, frozen.thread_id, frozen.candidate_id, checkpoint_ns=frozen.checkpoint_ns,
        )
        return None if result is None else BackgroundTaskStep({}, completed=True, result={
            "candidate_id": frozen.candidate_id, "result_ref": cast(JsonValue, result.result_ref.to_dict()),
        })

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None:
        return await run_sync_shielded(self.result, handle)

    async def abandon(self, handle: BackgroundTaskHandle, *, outcome: Literal["failed", "cancelled"]) -> None:
        value = await run_sync_shielded(self.read_input, handle)
        frozen = value.frozen
        candidates = self._saver._storage.compactions
        if outcome == "failed":
            calls, _failures, retryable = await run_sync_shielded(candidates.call_usage, frozen)
            if calls < MAX_DERIVED_CALLS and retryable and await run_sync_shielded(
                candidates.applicability, frozen.session_id, frozen.thread_id, frozen.candidate_id,
                checkpoint_ns=frozen.checkpoint_ns,
            ) is None:
                # 首次上游失败供前台沿原32次预算补尝试；新 attempt 才登记其前台期限。
                return
        await run_sync_shielded(
            candidates.discard, frozen.session_id, frozen.thread_id, frozen.candidate_id,
            reason=f"task-{outcome}", checkpoint_ns=frozen.checkpoint_ns,
        )

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
        with workspace_context(self._workspace_id, self._workspace_root, session_catalog=self._session_catalog):
            value = await run_sync_shielded(self.read_input, handle)
            calls, failures, _retryable = await run_sync_shielded(self._saver._storage.compactions.call_usage, value.frozen)
            budget = SummaryCallBudget(self.deadline(handle).timestamp(), calls_used=calls, failure_retries_used=min(max(failures - 1, 0), 1))
            previous = await run_sync_shielded(self._saver._storage.compactions.predecessor_result, value.frozen)
            async with AsyncExitStack() as resources:
                provider = await self._config.prepare_model_recipe(value.model_recipe)
                model = await run_sync_shielded(build_model_from_provider, provider, {}, invocation_purpose="compaction")
                resources.push_async_callback(run_cleanup_shielded, model.aclose)
                await CompactionGenerator(saver=self._saver, model=model, activation=self._activation, trace_sink=self._trace_sink,
                    target_constraint=value.target_constraint,
                    initial_sources=value.initial_sources, initial_tool_set=value.initial_tool_set,
                    provider_wait=None if self._provider_wait is None else lambda active: self._provider_wait(handle, active)).generate(
                    value.frozen, budget=budget, derived_summaries=() if previous is None else (previous,),
                    allow_external_retry=calls > 0,
                    publication_gate=self._registry().derived_publication(handle),
                )
            result = await run_sync_shielded(self.result, handle)
            if result is None:
                raise RuntimeError("compaction-result-unavailable: 生成返回后没有完整 ready 结果")
            return result
