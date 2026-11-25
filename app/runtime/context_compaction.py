"""压缩运行入口：只编排原候选 owner 和原后台任务，不另存运行状态。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import TYPE_CHECKING, cast

from boxteam_plugin_sdk.lifecycle import LifetimeScope, run_cleanup_shielded
from pydantic import JsonValue

from app.abstractions.background_task_registry import BackgroundTaskRegistryProtocol
from app.core.identifier import create_uuid_hex
from app.core.shielded_cleanup import run_sync_shielded
from app.core.turn_execution_scope import get_current_turn_execution_scope
from app.domain.background_work import (
    ACTIVE_BACKGROUND_TASK_STATUSES,
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
)
from app.domain.itemized.compaction import (
    FrozenCompactionRange,
    FrozenCompactionTaskInput,
    FrozenSummaryResult,
    ReadyCompactionCandidate,
    compaction_selection_ids,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.summary_compiler import (
    compaction_history_items,
)

if TYPE_CHECKING:
    from app.agents.providers.wire_finalize import ChatWireProfile, ResponsesWireProfile
    from app.domain.context_compaction import CompactionPolicy, ContextBudget
    from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
    from app.domain.itemized.plans.contributions import ContextContribution
    from app.domain.itemized.records import CanonicalItemRecord
    from app.schemas.internal_v2.model_reference import ModelReferenceDTO
    from app.services.infrastructure.rollout_context.checkpoint.owner.context_projection import (
        ProviderContextRequest,
    )


class _CompactionInFlight(Exception):
    def __init__(self, handle: BackgroundTaskHandle) -> None:
        self.handle = handle


def compaction_deadline(deadline: datetime | None) -> datetime:
    now = datetime.now(UTC)
    absolute = deadline if deadline is not None else now + timedelta(seconds=300)
    if absolute.tzinfo is None:
        raise ValueError("压缩操作期限必须带时区")
    scope = get_current_turn_execution_scope()
    scope = None if scope is None else scope.effective_scope
    while scope is not None:
        if scope.deadline is not None:
            absolute = min(absolute, now + timedelta(seconds=scope.deadline - time.monotonic()))
        scope = scope.parent
    return absolute


@dataclass(slots=True)
class _PreparationScope:
    """仅当前 Thread 代际的学习反馈；候选和任务仍从原 owner 查询。"""

    generation: str
    policy: CompactionPolicy
    prepare_fraction: float
    background_task_id: str | None = None
    model_waiting: bool = False
    last_feedback_task_id: str | None = None
    lifetime_scope: LifetimeScope | None = None
    event_loop: asyncio.AbstractEventLoop | None = None
    observation_task: asyncio.Future[object] | None = None
    observation_dirty: bool = False
    observation_budget: ContextBudget | None = None
    observed_sequence: int = 0
    observation_agent_id: str | None = None
    observation_model_recipe: Mapping[str, object] | None = None
    observation_wire_profile: ChatWireProfile | ResponsesWireProfile | None = None
    observation_items: tuple[CanonicalItemRecord, ...] = ()
    observation_reassess: Callable[[Sequence[CanonicalItemRecord]], tuple[ProviderContextRequest, tuple[ContextContribution, ...], ToolSetSnapshot]] | None = None
    observation_source_dirty: bool = False
    observation_sources: tuple[ContextContribution, ...] = ()
    observation_tool_set: ToolSetSnapshot | None = None


class AgentSummarizationCompactor:
    """HTTP、模型工具和实际模型准备共用的压缩运行端口。"""

    def __init__(self, *, config_service: ConfigService, saver: RolloutCheckpointSaver,
                 registry: BackgroundTaskRegistryProtocol, sessions: SessionService) -> None:
        self._config, self._saver, self._registry, self._sessions = config_service, saver, registry, sessions
        self._preparation_scopes: dict[MutationIntentOwner, _PreparationScope] = {}
        self._scope_lock = RLock()

    def policy(self) -> tuple[CompactionPolicy, bool]:
        """下一次准备读取当前配置；策略和自动开关来自同一次快照。"""
        snapshot = self._config.get_snapshot()
        with self._config.use_snapshot(snapshot):
            return self._config.get_compaction_policy(), self._config.is_automatic_compaction_enabled()

    def effective_policy(self, owner: MutationIntentOwner, generation: str, policy: CompactionPolicy) -> CompactionPolicy:
        """普通执行保留学习值；模型、容量或策略改变才开启新准备代际。"""
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if scope is None or scope.generation != generation:
                previous = scope
                scope = _PreparationScope(generation, policy, policy.prepare_fraction,
                    lifetime_scope=None if previous is None else previous.lifetime_scope,
                    event_loop=None if previous is None else previous.event_loop)
                self._preparation_scopes[owner] = scope
            return policy.model_copy(update={"prepare_fraction": scope.prepare_fraction})

    def bind_observation(self, owner: MutationIntentOwner, lifetime_scope: LifetimeScope) -> None:
        """只绑定真实 Thread 代的 scope；稳定提交不创建另一套常驻任务。"""
        policy, _enabled = self.policy()
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if scope is None:
                scope = _PreparationScope("", policy, policy.prepare_fraction)
                self._preparation_scopes[owner] = scope
            scope.lifetime_scope, scope.event_loop = lifetime_scope, asyncio.get_running_loop()
            scope.observation_budget = None
        lifetime_scope.register(lambda: self._release_observation(owner, lifetime_scope), label="compaction-observation")

    def _release_observation(self, owner: MutationIntentOwner, lifetime_scope: LifetimeScope) -> None:
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if scope is not None and scope.lifetime_scope is lifetime_scope:
                self._preparation_scopes.pop(owner)

    def seed_observation(
        self, owner: MutationIntentOwner, budget: ContextBudget, item_sequence: int, generation: str,
        *, agent_id: str, model_recipe: Mapping[str, object],
        wire_profile: ChatWireProfile | ResponsesWireProfile,
        reassess_sources: Callable[[Sequence[CanonicalItemRecord]], tuple[ProviderContextRequest, tuple[ContextContribution, ...], ToolSetSnapshot]] | None = None,
    ) -> None:
        """只消费实际已封存请求的完整预算及同一冻结快照序号。"""
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if scope is None or scope.generation != generation:
                return
            scope.observation_budget, scope.observed_sequence = budget, item_sequence
            scope.observation_agent_id, scope.observation_model_recipe = agent_id, model_recipe
            scope.observation_wire_profile = wire_profile
            scope.observation_items, scope.observation_source_dirty = (), False
            scope.observation_reassess = reassess_sources
            scope.observation_sources, scope.observation_tool_set = (), None

    def invalidate_observation(self, owner: MutationIntentOwner) -> None:
        """来源提交只合并标记；沿原冻结 compiler 重评完整 wire，避免新旧正文累加。"""
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if scope is not None:
                scope.observation_source_dirty = True
        self.observe_committed(owner)

    def observe_committed(self, owner: MutationIntentOwner) -> None:
        """worker 提交通知只置一个 dirty 标记，序号和正文沿原 owner 增量读取。"""
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if (scope is None or scope.observation_budget is None or scope.event_loop is None
                    or scope.lifetime_scope is None or scope.lifetime_scope.state != "open"
                    or scope.observation_dirty):
                return
            scope.observation_dirty = True
            scope.event_loop.call_soon_threadsafe(self._start_observation, owner, scope)

    def _start_observation(self, owner: MutationIntentOwner, scope: _PreparationScope) -> None:
        with self._scope_lock:
            if (self._preparation_scopes.get(owner) is not scope or scope.lifetime_scope is None
                    or scope.lifetime_scope.state != "open"):
                return
            if scope.observation_task is None or scope.observation_task.done():
                scope.observation_task = scope.lifetime_scope.start_task(
                    lambda: self._observe_stable_items(owner, scope), label="compaction-stable-items")

    async def _observe_stable_items(self, owner: MutationIntentOwner, scope: _PreparationScope) -> None:
        from langchain_core.messages import message_to_dict

        from app.agents.providers.request_budget import estimate_wire_input
        from app.agents.providers.wire_finalize import (
            ChatWireProfile,
            finalize_chat_messages,
        )
        from app.domain.context_compaction import ContextBudget, InputTokenEstimate
        from app.services.infrastructure.rollout_context.provider.native_request import (
            project_message_inputs,
        )
        from app.services.mapping.itemized.canonical_langchain import (
            project_canonical_items,
        )

        while True:
            with self._scope_lock:
                if self._preparation_scopes.get(owner) is not scope or not scope.observation_dirty:
                    return
                scope.observation_dirty = False
                budget, sequence, profile = scope.observation_budget, scope.observed_sequence, scope.observation_wire_profile
                source_dirty, reassess = scope.observation_source_dirty, scope.observation_reassess
                scope.observation_source_dirty = False
                retained_items = scope.observation_items
                initial_sources, initial_tool_set = scope.observation_sources, scope.observation_tool_set
            if budget is None or profile is None:
                return
            policy, enabled = self.policy()
            if not enabled or policy != scope.policy:
                return
            items = []
            last_sequence = sequence
            async with self._sessions.session_read_admission(owner.session_id):
                while True:
                    next_sequence, page = await run_sync_shielded(
                        self._saver._storage.compactions.observation_increment,
                        owner.session_id, owner.thread_id, after_sequence=last_sequence,
                    )
                    if next_sequence == last_sequence:
                        break
                    items.extend(page)
                    last_sequence = next_sequence
                # 先合拢索引小页再投影，消息／并行工具组不会被分页拆成伪 carrier。
                all_items = (*retained_items, *items)
                messages = project_canonical_items(items, include_runtime_notices=False, include_summaries=False, preserve_order=True)
                if isinstance(profile, ChatWireProfile):
                    wire = {"messages": finalize_chat_messages(messages, profile=profile)}
                else:
                    wire = {"input": [entry for message in messages for entry in project_message_inputs(
                        message_to_dict(message), target_source=profile.model_source,
                        target_capabilities=profile.replay_capabilities(),
                    )[0]]}
                increment = estimate_wire_input(wire) if messages else None
                empty = estimate_wire_input({"messages" if isinstance(profile, ChatWireProfile) else "input": []})
                updated = budget if increment is None else ContextBudget(capacity=budget.capacity,
                    estimate=InputTokenEstimate(known_tokens=budget.estimate.known_tokens + increment.known_tokens - empty.known_tokens,
                        unknown_parts=tuple(sorted({*budget.estimate.unknown_parts, *increment.unknown_parts})),
                        source="langchain-approximate-final-wire+stable-items:v1"))
                if source_dirty:
                    if reassess is None:
                        raise RuntimeError("稳定来源观察缺少原冻结请求的只读编译端口")
                    request, initial_sources, initial_tool_set = await run_sync_shielded(reassess, all_items)
                    updated = request.budget(budget.capacity)
                with self._scope_lock:
                    if self._preparation_scopes.get(owner) is not scope or scope.lifetime_scope.state != "open":
                        return
                    if scope.observation_budget is not budget or scope.observed_sequence != sequence:
                        continue
                    scope.observation_budget, scope.observed_sequence = updated, last_sequence
                    scope.observation_items = all_items
                    scope.observation_sources, scope.observation_tool_set = initial_sources, initial_tool_set
                budget = updated
            policy, enabled = self.policy()
            if not enabled or policy != scope.policy:
                return
            policy = self.effective_policy(owner, scope.generation, policy)
            if budget.decision(policy, auto_summarize=True) not in {"prepare", "compact"}:
                continue
            with self._scope_lock:
                if self._preparation_scopes.get(owner) is not scope or scope.observation_budget is not budget:
                    return
                agent_id, recipe = scope.observation_agent_id, scope.observation_model_recipe
            if agent_id is None or recipe is None:
                raise RuntimeError("稳定上下文观察缺少实际请求冻结的 Agent／模型引用")
            handle = await self.request(owner, agent_id=agent_id, model_recipe=recipe,
                initial_sources=initial_sources, initial_tool_set=initial_tool_set,
                target_constraint={"capacity": budget.capacity.model_dump(mode="json"),
                    "policy": policy.model_dump(mode="json", exclude={"prepare_fraction"})})
            if handle is not None:
                self.note_background(owner, handle.task_id)

    def note_background(self, owner: MutationIntentOwner, task_id: str) -> None:
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if scope is not None and scope.background_task_id != task_id:
                scope.background_task_id = task_id
                scope.model_waiting = False

    def observe_provider_wait(self, handle: BackgroundTaskHandle, waiting: bool) -> None:
        """只接实际模型 await 的始末，不把任务排队或本地准备算作生成等待。"""
        owner = MutationIntentOwner(handle.session_id, handle.thread_id)
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if scope is not None and scope.background_task_id == handle.task_id:
                scope.model_waiting = waiting

    def waiting_background(self, owner: MutationIntentOwner) -> str | None:
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            return scope.background_task_id if scope is not None and scope.model_waiting else None

    def record_generation_wait(self, owner: MutationIntentOwner, generation: str, task_id: str) -> None:
        """完整适用结果最终能发送后，每个原后台 task 最多降低一次准备比例。"""
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            if (scope is None or scope.generation != generation or scope.background_task_id != task_id
                    or scope.last_feedback_task_id == task_id):
                return
            proposed = min(scope.policy.prepare_fraction, max(0.60, round(scope.prepare_fraction - 0.05, 10)))
            if scope.policy.target_fraction < proposed < scope.prepare_fraction:
                scope.prepare_fraction = proposed
                scope.last_feedback_task_id = task_id

    async def release_thread(self, session_id: str, thread_id: str, _generation: int) -> None:
        with self._scope_lock:
            self._preparation_scopes.pop(MutationIntentOwner(session_id, thread_id), None)

    def status(self, owner: MutationIntentOwner) -> dict[str, object]:
        """查询不恢复任务、不读结果正文，也不把展示状态写成另一份业务事实。"""
        policy, enabled = self.policy()
        with self._scope_lock:
            scope = self._preparation_scopes.get(owner)
            active = scope if enabled and scope is not None and scope.policy == policy else None
            effective = policy.prepare_fraction if active is None else active.prepare_fraction
            feedback = None if active is None else active.last_feedback_task_id
        candidate = self._saver._storage.compactions.current_status(owner.session_id, owner.thread_id)
        if candidate is not None:
            handle = self._registry.get_handle(owner.session_id, candidate["task_id"])
            if handle is not None:
                candidate = {**candidate, "task_status": handle.status, "task_note": handle.metadata.get("status_note")}
        return {"session_id": owner.session_id, "thread_id": owner.thread_id, "automatic": enabled,
                "policy": policy.model_dump(mode="json"), "effective_prepare_fraction": effective,
                "preparation_reason": None if feedback is None else "background-model-wait",
                "feedback_task_id": feedback, "candidate": candidate}

    def source_item_ids(self, owner: MutationIntentOwner, *, checkpoint_ns: str = "") -> tuple[str, ...]:
        """只冻结最新用户输入之前的闭合历史；工具配对仍由共同边界 owner 验证。"""
        with self._saver._context_reader.open_snapshot(owner.session_id, owner.thread_id, checkpoint_ns) as snapshot:
            items = self._saver._storage.checkpoints.committed_context_items(snapshot, predecessor=None, include_pending_notices=False)
        return tuple(item.item_id for item in compaction_history_items(items))

    async def request_application(self, handle: BackgroundTaskHandle) -> BackgroundTaskHandle:
        from app.services.orchestration.background_work.compaction import (
            CompactionTaskHandler,
        )

        namespace = CompactionTaskHandler.descriptor_input(handle.descriptor)[1]
        await run_sync_shielded(
            self._saver._storage.compactions.request_application, handle.session_id, handle.thread_id,
            handle.task_id, checkpoint_ns=namespace,
        )
        current = self._registry.get_handle(handle.session_id, handle.task_id)
        if current is None or current.status not in {*ACTIVE_BACKGROUND_TASK_STATUSES, "completed"}:
            raise RuntimeError(f"compaction-generation-failed: 手动请求未受理，task={handle.task_id}, "
                               f"status={None if current is None else current.status}, detail={None if current is None else current.metadata}")
        return current

    def ready(self, owner: MutationIntentOwner, *, checkpoint_ns: str = "",
              include_automatic: bool = True) -> ReadyCompactionCandidate | None:
        return self._saver._storage.compactions.latest_ready(
            owner.session_id, owner.thread_id, checkpoint_ns=checkpoint_ns, include_automatic=include_automatic,
        )

    def _in_flight(self, owner: MutationIntentOwner) -> BackgroundTaskHandle | None:
        from app.services.orchestration.background_work.compaction import (
            CompactionTaskHandler,
        )

        return next((handle for handle in self._registry.list_handles(owner.session_id)
                     if handle.thread_id == owner.thread_id and handle.descriptor.kind == CompactionTaskHandler.kind
                     and handle.status in ACTIVE_BACKGROUND_TASK_STATUSES), None)

    def read_history(self, owner: MutationIntentOwner, *, line_offset: int = 1, limit: int = 200) -> str:
        return self._saver._storage.compactions.read_history(owner.session_id, owner.thread_id, line_offset=line_offset, limit=limit)

    async def request(
        self, owner: MutationIntentOwner, *, agent_id: str, item_ids: Sequence[str] | None = None,
        model_recipe: Mapping[str, object] | None = None, preferred_model: ModelReferenceDTO | None = None,
        deadline: datetime | None = None,
        checkpoint_ns: str = "", foreground: bool = False, manual: bool = False,
        predecessor_candidate_id: str | None = None, target_constraint: Mapping[str, object] | None = None,
        initial_sources: tuple[ContextContribution, ...] = (), initial_tool_set: ToolSetSnapshot | None = None,
    ) -> BackgroundTaskHandle | None:
        """先沿原 registry 裁决额度，再冻结唯一计划；满载时不留下候选。"""
        from app.services.orchestration.background_work.compaction import (
            CompactionTaskHandler,
        )

        await self._registry.recover_derived_thread(owner.session_id, owner.thread_id)
        if (active := self._in_flight(owner)) is not None:
            handle = await self._registry.promote_derived(owner.session_id, active.task_id) if foreground else active
            return await self.request_application(handle) if manual else handle
        ids = tuple(item_ids) if item_ids is not None else await run_sync_shielded(self.source_item_ids, owner, checkpoint_ns=checkpoint_ns)
        if not ids:
            return None
        # 保留估算不改变提案身份；手动入口没有新容量约束时仍复用原候选。
        target_hash = None if target_constraint is None else sha256_jcs({
            key: target_constraint[key] for key in ("capacity", "policy")
        })
        if model_recipe is None:
            snapshot = self._config.get_snapshot()
            with self._config.use_snapshot(snapshot):
                model_recipe = self._config.freeze_model_recipe(self._config.resolve_agent_model(agent_id, preferred_model))
                if target_constraint is None:
                    # 手动入口没有最终请求预算；仍冻结同次策略，不伪造容量或保留量。
                    target_constraint = {"capacity": {}, "policy": self._config.get_compaction_policy().model_dump(
                        mode="json", exclude={"prepare_fraction"},
                    )}
        # 与共同派生提交相同的有界操作上限；调用方可传更早的实际请求期限。
        absolute = compaction_deadline(deadline) if foreground else (
            deadline if deadline is not None else datetime.now(UTC) + timedelta(seconds=300)
        )
        task_id = f"compaction-{create_uuid_hex()}"
        candidates = self._saver._storage.compactions
        model_hash = sha256_jcs(model_recipe)
        proposal = await run_sync_shielded(candidates.existing_proposal, owner.session_id, owner.thread_id, ids,
                                          model_config_hash=model_hash, target_constraint_hash=target_hash, checkpoint_ns=checkpoint_ns)
        if proposal is not None:
            original = self._registry.get_handle(owner.session_id, proposal.task_id)
            if original is None:
                raise RuntimeError("compaction-task-unavailable: 已冻结提案缺少原后台任务")
            if not manual or original.status != "failed":
                return await self.request_application(original) if manual else original
            from app.agents.summarization.generation import MAX_DERIVED_CALLS

            calls, _failures, retryable = await run_sync_shielded(candidates.call_usage, proposal)
            if calls < MAX_DERIVED_CALLS and retryable and await run_sync_shielded(
                candidates.applicability, owner.session_id, owner.thread_id, proposal.candidate_id, checkpoint_ns=checkpoint_ns,
            ) is None:
                handle = await self._registry.retry_derived(owner.session_id, original.task_id, deadline=compaction_deadline(absolute), validate=self._validate_retry)
                return await self.request_application(handle)
            # 只有这次显式手动操作可发起新评估；普通 prepare 始终观察原失败事实。
        predecessor = (await run_sync_shielded(candidates.read, owner.session_id, owner.thread_id, predecessor_candidate_id, checkpoint_ns=checkpoint_ns)
                       if predecessor_candidate_id is not None else
                       await run_sync_shielded(candidates.reusable_prefix, owner.session_id, owner.thread_id, ids, checkpoint_ns=checkpoint_ns))
        same_range = predecessor is not None and (compaction_selection_ids(predecessor, ids) == ids or ids == (f"item-summary-{predecessor.candidate_id}",))
        if same_range and predecessor is not None and predecessor.model_config_hash == model_hash and target_hash in (None, predecessor.target_constraint_hash):
            previous_handle = self._registry.get_handle(owner.session_id, predecessor.task_id)
            if previous_handle is None:
                raise RuntimeError("compaction-task-unavailable: 可复用候选缺少原后台任务")
            return await self.request_application(previous_handle) if manual else previous_handle
        if same_range and predecessor is not None and predecessor.predecessor is not None:
            raise RuntimeError("compaction-successor-exhausted: 同范围已有一次二级缩减，不能重复生成")

        @asynccontextmanager
        async def freeze(existing: BackgroundTaskHandle | None) -> AsyncIterator[BackgroundTaskDescriptor]:
            if existing is not None:
                yield existing.descriptor
                return
            if (active := self._in_flight(owner)) is not None:
                raise _CompactionInFlight(active)
            latest = await run_sync_shielded(candidates.existing_proposal, owner.session_id, owner.thread_id, ids,
                model_config_hash=model_hash, target_constraint_hash=target_hash, checkpoint_ns=checkpoint_ns)
            if latest is not None and (proposal is None or latest.task_id != proposal.task_id):
                merged = self._registry.get_handle(owner.session_id, latest.task_id)
                if merged is None:
                    raise RuntimeError("compaction-task-unavailable: 并发受理提案缺少原后台任务")
                raise _CompactionInFlight(merged)
            frozen = None
            try:
                async with self._sessions.session_read_admission(owner.session_id):
                    frozen = await run_sync_shielded(
                        candidates.freeze, owner.session_id, owner.thread_id, candidate_id=task_id, task_id=task_id,
                        item_ids=ids, model_config_hash=model_hash, checkpoint_ns=checkpoint_ns,
                        predecessor_candidate_id=None if predecessor is None else predecessor.candidate_id,
                        target_constraint_hash=target_hash,
                        initial_sources=initial_sources,
                    )
                    ref = await run_sync_shielded(candidates.save_task_input, FrozenCompactionTaskInput(
                        frozen, model_recipe, absolute, target_constraint,
                        initial_sources=initial_sources, initial_tool_set=initial_tool_set))
                yield BackgroundTaskDescriptor(kind=CompactionTaskHandler.kind, version=CompactionTaskHandler.version,
                    payload={"input_ref": cast(JsonValue, ref.to_dict()), "checkpoint_ns": checkpoint_ns, "deadline": absolute.isoformat()})
            except BaseException:
                if frozen is not None and self._registry.get_handle(owner.session_id, task_id) is None:
                    await run_cleanup_shielded(lambda: run_sync_shielded(
                        candidates.discard, owner.session_id, owner.thread_id, task_id,
                        reason="task-not-accepted", checkpoint_ns=checkpoint_ns,
                    ))
                raise
        try:
            handle = await self._registry.spawn_derived(session_id=owner.session_id, thread_id=owner.thread_id,
                agent_id=agent_id, task_id=task_id, descriptor=freeze, foreground=foreground)
            if handle is None:
                raise RuntimeError("compaction-capacity-exhausted: 派生任务额度已满，未受理压缩")
            return await self.request_application(handle) if manual else handle
        except _CompactionInFlight as merged:
            handle = await self._registry.promote_derived(owner.session_id, merged.handle.task_id) if foreground else merged.handle
            return await self.request_application(handle) if manual else handle

    async def wait(self, handle: BackgroundTaskHandle, *, deadline: datetime | None = None) -> tuple[FrozenCompactionRange, FrozenSummaryResult]:
        """阻断请求只提升并等待同 task；取消只传给本请求实际等待的任务。"""
        from app.services.orchestration.background_work.compaction import (
            CompactionTaskHandler,
        )

        absolute = compaction_deadline(deadline)
        handle = await self._registry.promote_derived(handle.session_id, handle.task_id)
        ref, namespace, _first_deadline = CompactionTaskHandler.descriptor_input(handle.descriptor)
        value = await run_sync_shielded(self._saver._storage.compactions.read_task_input,
            handle.session_id, handle.thread_id, ref, checkpoint_ns=namespace)
        frozen = value.frozen
        try:
            while True:
                result = await run_sync_shielded(self._saver._storage.compactions.read_result,
                    handle.session_id, handle.thread_id, frozen.candidate_id, checkpoint_ns=namespace)
                if result is not None:
                    return frozen, result
                current = self._registry.get_handle(handle.session_id, handle.task_id)
                if current is not None and current.status == "failed":
                    handle = await self._registry.retry_derived(handle.session_id, handle.task_id, deadline=absolute, validate=self._validate_retry)
                    continue
                if current is None or current.status not in ACTIVE_BACKGROUND_TASK_STATUSES:
                    raise RuntimeError(f"compaction-generation-failed: task={handle.task_id}, status={None if current is None else current.status}, detail={None if current is None else current.metadata}")
                if datetime.now(UTC) >= absolute:
                    await self._registry.cancel(handle.session_id, handle.task_id)
                    raise TimeoutError("compaction-deadline-exceeded: 等待摘要任务超过本次前台操作期限")
                if datetime.now(UTC) >= CompactionTaskHandler.deadline(current):
                    await self._registry.recover_derived_thread(handle.session_id, handle.thread_id)
                    raise TimeoutError("compaction-deadline-exceeded: 等待摘要任务超过本次 attempt 期限")
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            await run_cleanup_shielded(lambda: self._registry.cancel(handle.session_id, handle.task_id))
            raise

    def _validate_retry(self, handle: BackgroundTaskHandle) -> None:
        from app.agents.summarization.generation import MAX_DERIVED_CALLS
        from app.services.orchestration.background_work.compaction import (
            CompactionTaskHandler,
        )

        ref, namespace, _first_deadline = CompactionTaskHandler.descriptor_input(handle.descriptor)
        candidates = self._saver._storage.compactions
        frozen = candidates.read_task_input(handle.session_id, handle.thread_id, ref, checkpoint_ns=namespace).frozen
        calls, _failures, retryable = candidates.call_usage(frozen)
        if calls >= MAX_DERIVED_CALLS or not retryable or candidates.applicability(handle.session_id, handle.thread_id, frozen.candidate_id,
                                                   checkpoint_ns=namespace) is not None:
            raise RuntimeError("compaction-retry-unavailable: 原任务不是首次上游失败或来源已经失效")
