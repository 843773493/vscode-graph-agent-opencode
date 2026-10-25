"""collaboration pending 投递、冷恢复与 Registry 发布；显式任务才消费 wakeup。"""

from __future__ import annotations

import asyncio
import json
import logging
from _thread import RLock
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from app.abstractions.internal_message import PreparedInternalMessage
from app.core.exceptions import NotFoundError
from app.core.key_lock_pool import KeyLockPool
from app.core.lifecycle import LifetimeScope
from app.core.phase_observer import observe_phase, observe_phase_sync
from app.core.session_control_store.collaboration_fanout import (
    CollaborationFanoutRecord,
)
from app.core.session_lifecycle_gate import (
    OperationHolderGate,
    SessionDeletionPendingError,
)
from app.services.business.resource_derivations.team import (
    TEAM_EVENT_SOURCE_ID,
    TEAM_FACETS,
    TEAM_LEDGER_SOURCE_ID,
    TEAM_RESOURCE_IDS,
    TeamFacetLoader,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.resource_platform.derivation.graph import (
    ResourceDerivationGraph,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _TeamAcceptedRevision:
    revision: str
    content: str
    available: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class _TeamAcceptedSources:
    """原 fanout 的冻结输入与持久投递门；不保存第二份协作正文。"""

    session_id: str
    record: CollaborationFanoutRecord | None = None
    ready: bool = True

    def observed_revision(self, source_id: str) -> _TeamAcceptedRevision:
        record = self.record
        revision = 0 if record is None else record.ledger_revision
        event_hash = "empty" if record is None else record.event_hash
        if source_id == TEAM_LEDGER_SOURCE_ID:
            source_revision = str(revision)
            content = record.payload_json if record is not None else json.dumps({
                "session_id": self.session_id, "ledger_revision": 0, "event_hash": event_hash,
                "event": None, "boards": [], "members": [],
            })
        elif source_id == TEAM_EVENT_SOURCE_ID:
            source_revision, content = event_hash, event_hash
        else:
            raise KeyError(source_id)
        return _TeamAcceptedRevision(source_revision, content, self.ready,
            None if self.ready else f"collaboration required revision={revision} 尚未完成 recipient 持久投递")


class TeamFanoutService:
    def __init__(self, *, factory: OwnerThreadCreationFactory, sessions: SessionService,
                 saver: RolloutCheckpointSaver, dispatcher: RuntimeSourceDispatcher,
                 publication_lock: RLock) -> None:
        self._factory, self._sessions, self._saver, self._dispatcher = factory, sessions, saver, dispatcher
        self._publication_lock = publication_lock
        self._holder = OperationHolderGate(sessions.path_resolver.sessions_root, namespace="collaboration")
        self._locks = KeyLockPool()
        self._accepted: dict[tuple[str, str], _TeamAcceptedSources] = {}
        self._subscribers: dict[tuple[str, str], set[ResourceRegistry]] = {}
        self._ready = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changed = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._finalizer: Callable[[CollaborationFanoutRecord, str], Awaitable[None]] | None = None
        factory.bind_collaboration_publication(self.observe_commit, publication_lock)

    def bind_wakeup_finalizer(self, callback: Callable[[CollaborationFanoutRecord, str], Awaitable[None]]) -> None:
        self._finalizer = callback

    def observe_commit(self, record: CollaborationFanoutRecord) -> None:
        """ledger commit 与 unavailable 发布受同一锁保护，active slot 不捕获旧 ready。"""
        with self._publication_lock:
            for recipient in record.recipients:
                pair = record.session_id, recipient.thread_id
                previous = self._accepted.get(pair)
                if previous is not None and previous.record is not None and previous.record.ledger_revision > record.ledger_revision:
                    continue
                self._accepted[pair] = _TeamAcceptedSources(record.session_id, record, ready=False)
                self._publish(pair)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._changed.set)
        observe_phase_sync(record.lease_id, "team.mutation.committed", details={
            "session_id": record.session_id, "ledger_revision": record.ledger_revision,
            "event_hash": record.event_hash,
        })

    def _publish(self, pair: tuple[str, str]) -> None:
        for registry in tuple(self._subscribers.get(pair, ())):
            self._publish_registry(self._accepted[pair], pair[1], registry)

    @staticmethod
    def _publish_registry(sources: _TeamAcceptedSources, thread_id: str, registry: ResourceRegistry) -> None:
        with registry.publication():
            graph = ResourceDerivationGraph(reconciler=sources, registry=registry)
            for facet in TEAM_FACETS:
                graph.register(SemanticResourceDescriptor(
                    resource_id=TEAM_RESOURCE_IDS[facet], resource_kind="team_state", facet=facet,
                    display_uri=None, source_ids=(TEAM_LEDGER_SOURCE_ID, TEAM_EVENT_SOURCE_ID),
                    loader_version=TeamFacetLoader.version,
                ), TeamFacetLoader(sources.session_id, thread_id, facet))
            graph.publish_all()

    def subscribe(self, session_id: str, thread_id: str, registry: ResourceRegistry, scope: LifetimeScope) -> None:
        """generation builder 只订阅内存投影，不查询 ledger、磁盘或当前成员。"""
        if not self._ready:
            raise RuntimeError("team-state-not-ready: 启动持久 owner 尚未恢复完成，拒绝 dispatch")
        pair = session_id, thread_id
        with self._publication_lock:
            sources = self._accepted.setdefault(pair, _TeamAcceptedSources(session_id))
            self._publish_registry(sources, thread_id, registry)
            self._subscribers.setdefault(pair, set()).add(registry)

        def unsubscribe() -> None:
            with self._publication_lock:
                subscribers = self._subscribers.get(pair)
                if subscribers is not None:
                    subscribers.discard(registry)
                    if not subscribers:
                        self._subscribers.pop(pair)

        try:
            scope.register(unsubscribe, label="team_state publication")
        except BaseException:
            unsubscribe()
            raise

    def _check_worker(self) -> None:
        if self._worker is not None and self._worker.done():
            self._worker.result()
            raise RuntimeError("collaboration fanout worker 意外停止")

    async def _deliver(self, session_id: str) -> None:
        with self._factory.for_owner_session(session_id) as thread_owner:
            control = thread_owner.control_store
            failures: list[Exception] = []
            for record in control.list_collaboration_fanouts(pending_only=True):
                lease = control.get_lease(record.lease_id)
                # 独立 OS holder 已取得；新 token 明确废除前一轮 callback。
                lease = control.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                for recipient in record.recipients:
                    if recipient.state != "pending":
                        continue
                    await observe_phase(record.lease_id, "team.fanout.before_recipient/" + recipient.thread_id, details={
                        "session_id": session_id, "thread_id": recipient.thread_id,
                        "ledger_revision": record.ledger_revision,
                        "captured_lifecycle_generation": lease.captured_lifecycle_generation,
                    })
                    try:
                        async with self._sessions.session_write_admission(session_id):
                            if control.get_fence() != ("active", lease.captured_lifecycle_generation):
                                control.finish_collaboration_delivery(record.ledger_revision, recipient.thread_id,
                                                                      fencing_token=lease.fencing_token, outcome="fence_closed")
                                continue
                            row = control.connection.execute("SELECT 1 FROM thread_catalog WHERE thread_id=?", (recipient.thread_id,)).fetchone()
                            cancelled = control.connection.execute("SELECT 1 FROM collaboration_members WHERE child_thread_id=? AND state='cancelled'", (recipient.thread_id,)).fetchone()
                            if row is None or cancelled is not None:
                                control.finish_collaboration_delivery(record.ledger_revision, recipient.thread_id,
                                                                      fencing_token=lease.fencing_token, outcome="recipient_deleted")
                                continue
                            if not control.verify_lease_token(lease_id=lease.lease_id, fencing_token=lease.fencing_token):
                                raise RuntimeError("collaboration recipient 的原 lease/token 已失效")
                            self._saver.receive_team_source_observation(
                                session_id, recipient.thread_id, ledger_revision=record.ledger_revision,
                                event_hash=record.event_hash, payload_json=record.payload_json,
                            )
                            control.finish_collaboration_delivery(record.ledger_revision, recipient.thread_id,
                                                                  fencing_token=lease.fencing_token, outcome="delivered")
                    except (SessionDeletionPendingError, NotFoundError):
                        control.finish_collaboration_delivery(record.ledger_revision, recipient.thread_id,
                                                              fencing_token=lease.fencing_token, outcome="fence_closed")
                    except Exception as error:  # 记录部分失败后继续其它 recipient，最终原样聚合抛出
                        detail = (
                            f"collaboration 投递失败: session_id={session_id}, "
                            f"thread_id={recipient.thread_id}, ledger_revision={record.ledger_revision}, "
                            f"stage=recipient_delivery, error={type(error).__name__}"
                        )
                        for name in ("errno", "winerror", "sqlite_errorcode"):
                            if isinstance(code := getattr(error, name, None), int):
                                detail += f", {name}={code}"
                        logger.error("%s；完整诊断见异常链", detail, exc_info=error)
                        control.record_collaboration_delivery_failure(record.ledger_revision, recipient.thread_id,
                                                                      fencing_token=lease.fencing_token, error=detail)
                        failures.append(error)
                if record.wakeup_json is None and all(item.state != "pending" for item in control.get_collaboration_fanout(record.ledger_revision).recipients):
                    control.finish_collaboration_fanout(record.ledger_revision, fencing_token=lease.fencing_token,
                                                       cancelled=any(item.state != "delivered" for item in control.get_collaboration_fanout(record.ledger_revision).recipients))
                elif record.wakeup_json is not None:
                    target = json.loads(record.wakeup_json)["target_thread_id"]
                    updated = control.get_collaboration_fanout(record.ledger_revision)
                    if all(item.state != "pending" for item in updated.recipients) and any(item.thread_id == target and item.state != "delivered" for item in updated.recipients):
                        control.finish_collaboration_fanout(record.ledger_revision, fencing_token=lease.fencing_token, cancelled=True)
            self._publish_reconciled(session_id)
            if failures:
                raise ExceptionGroup("collaboration recipient 部分投递失败，原 pending 保留且其它 recipient 已收敛", failures)

    def _publish_reconciled(self, session_id: str) -> None:
        with self._factory.for_owner_session(session_id) as thread_owner:
            control = thread_owner.control_store
            with self._publication_lock:
                for pair, sources in tuple(self._accepted.items()):
                    desired = sources.record
                    if desired is None or pair[0] != session_id or control.collaboration_recipient_pending(pair[1], desired.ledger_revision):
                        continue
                    record = control.get_collaboration_fanout(desired.ledger_revision)
                    delivery = next(item for item in record.recipients if item.thread_id == pair[1])
                    if delivery.state != "delivered":
                        continue
                    self._saver.require_team_source_observation(session_id, pair[1], ledger_revision=record.ledger_revision,
                                                               event_hash=record.event_hash, payload_json=record.payload_json)
                    self._accepted[pair] = replace(sources, ready=True)
                    self._publish(pair)

    async def reconcile(self, session_id: str) -> None:
        self._check_worker()
        async with self._locks.lock_for(session_id), self._holder.lock_for(session_id):
            await self._deliver(session_id)

    async def dispatch(self, session_id: str, revision: int) -> str | None:
        self._check_worker()
        async with self._locks.lock_for(session_id), self._holder.lock_for(session_id):
            await self._deliver(session_id)
            with self._factory.for_owner_session(session_id) as thread_owner:
                control = thread_owner.control_store
                record = control.get_collaboration_fanout(revision)
                if record.wakeup_json is None:
                    return None
                wakeup = json.loads(record.wakeup_json)
                if record.state == "cancelled" or any(item.thread_id == wakeup["target_thread_id"] and item.state != "delivered" for item in record.recipients):
                    raise RuntimeError("collaboration 显式 wakeup 的原 recipient/lifecycle 已终结")
                if record.state == "completed":
                    return str(json.loads(record.wakeup_receipt_json)["job_id"])
                lease = control.get_lease(record.lease_id)
                fencing_token = lease.fencing_token
                if record.wakeup_receipt_json is None:
                    receipt = await self._dispatcher.submit(
                        session_id, wakeup["target_thread_id"], PreparedInternalMessage(wakeup["content"], wakeup["metadata"]),
                        source_id=wakeup["source_id"], idempotency_key=wakeup["idempotency_key"],
                    )
                    async with self._sessions.session_write_admission(session_id):
                        control.record_collaboration_wakeup_receipt(revision, fencing_token=fencing_token,
                                                                    receipt_json=receipt.model_dump_json())
                    record = control.get_collaboration_fanout(revision)
                job_id = str(json.loads(record.wakeup_receipt_json)["job_id"])
                if self._finalizer is None:
                    raise RuntimeError("collaboration wakeup 未绑定业务 finalizer")
                await self._finalizer(record, job_id)
                control.finish_collaboration_fanout(revision, fencing_token=fencing_token)
                return job_id

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        _revision, nodes = self._sessions.path_resolver.catalog_store.read_nodes_snapshot()
        session_ids = [node.node_id for node in nodes if node.kind == "session" and node.state == "active"]
        for session_id in session_ids:
            with self._factory.for_owner_session(session_id) as thread_owner:
                control = thread_owner.control_store
                for record in control.latest_collaboration_fanouts():
                    self.observe_commit(record)
                await self.reconcile(session_id)
        # 所有 Session 的 pending source owner ready 后，才允许任何显式 task dispatch。
        self._ready = True
        for session_id in session_ids:
            await self._dispatch_pending(session_id)

        async def consume() -> None:
            while True:
                await self._changed.wait()
                self._changed.clear()
                owners = {session_id for (session_id, _thread), sources in self._accepted.items() if sources.record is not None}
                for session_id in sorted(owners):
                    await self.reconcile(session_id)
                    await self._dispatch_pending(session_id)

        self._worker = asyncio.create_task(consume(), name="collaboration-fanout")
        self._worker.add_done_callback(self._report_worker_failure)

    @staticmethod
    def _report_worker_failure(worker: asyncio.Task[None]) -> None:
        if not worker.cancelled() and worker.exception() is not None:
            logger.error("collaboration fanout worker 失败；后续 dispatch 明确拒绝", exc_info=worker.exception())

    async def _dispatch_pending(self, session_id: str) -> None:
        with self._factory.for_owner_session(session_id) as thread_owner:
            control = thread_owner.control_store
            for record in control.list_collaboration_fanouts(pending_only=True):
                if record.wakeup_json is not None:
                    await self.dispatch(session_id, record.ledger_revision)

    @property
    def deletion_holder(self) -> OperationHolderGate:
        """同一原 holder 交给删除 owner，在取得 Session gate 前等待并持有。"""
        return self._holder

    async def drain_session(self, session_id: str) -> None:
        """调用方已按 holder→Session gate 顺序持锁，只终结原冻结目标。"""
        with self._factory.for_owner_session(session_id) as thread_owner:
            control = thread_owner.control_store
            for record in control.list_collaboration_fanouts(pending_only=True):
                lease = control.get_lease(record.lease_id)
                lease = control.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                for item in record.recipients:
                    if item.state == "pending":
                        control.finish_collaboration_delivery(record.ledger_revision, item.thread_id,
                                                              fencing_token=lease.fencing_token, outcome="fence_closed")
                control.finish_collaboration_fanout(record.ledger_revision, fencing_token=lease.fencing_token, cancelled=True)
            with self._publication_lock:
                for pair in tuple(self._accepted):
                    if pair[0] == session_id:
                        self._accepted.pop(pair)

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._ready = False
        self._loop = None
