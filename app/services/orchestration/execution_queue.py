"""聚合 Thread rollout 执行队列、协调领取与持有权，并投影待处理请求。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.abstractions.execution_queue import ExecutionQueueLoad, ExecutionQueueOwner
from app.core.path_utils import get_session_path_resolver
from app.core.phase_observer import observe_phase
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionLifecycleGate,
    ThreadExecutionHolderGate,
)
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.domain.itemized.execution_queue import (
    ExecutionQueueInput,
    ExecutionQueuePause,
    ExecutionQueueRecord,
    ExecutionQueueSnapshot,
    QueueOrderConflictError,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.pending_request import (
    DeliveryPolicy,
    PendingRequestControl,
    PendingRequestDTO,
    PendingRequestListDTO,
    PendingRequestSummaryDTO,
    PendingRequestSummaryListDTO,
)


class ExecutionQueueClaim:
    """持有精确 Thread 的 OS 执行权，直到所有写入与终态结算完成。"""

    def __init__(self, record: ExecutionQueueRecord, holder, owner: ExecutionQueueOwner) -> None:
        self.record = record
        self._holder = holder
        self._owner = owner
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("执行 holder 已释放")
        record = self.record
        admission = record.cursor.admission
        await asyncio.to_thread(
            self._owner.start_execution_queue, admission.session_id, admission.thread_id,
            job_id=admission.job_id, claim_owner=record.claim_owner, claim_generation=record.claim_generation,
        )
        await observe_phase(admission.job_id, "execution.queue.started", details={
            "session_id": admission.session_id, "thread_id": admission.thread_id,
            "execution_id": admission.execution_id, "claim_generation": record.claim_generation,
        })

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._holder.__aexit__(None, None, None)


class ExecutionQueueService:
    """执行事实始终归 rollout owner，本服务不另存队列或消息正文。"""

    def __init__(self, *, sessions_dir: Path, owner: ExecutionQueueOwner,
                 input_reader: Callable[[PendingRequestControl], MessageDTO]) -> None:
        self._path_resolver = get_session_path_resolver(sessions_dir)
        self._owner = owner
        self._input_reader = input_reader
        self._holder = ThreadExecutionHolderGate(sessions_dir)
        self._topology_gate = NavigationTopologyGate(sessions_dir)
        self._session_gate = SessionLifecycleGate(sessions_dir)
        self._claim_owner = uuid4().hex

    def _legacy_path(self, session_id: str) -> Path:
        return self._path_resolver.resolve_session_node_for_runtime(session_id) / "pending_requests.json"

    def _owners(self, session_id: str) -> tuple[MutationIntentOwner, ...]:
        owners = self._owner.execution_queue_owners(session_id=session_id)
        for identity in dict.fromkeys(owner.session_id for owner in owners):
            if self._legacy_path(identity).exists():
                raise RuntimeError(f"pending v5 控制引用须显式 migrate_schema 后再启动: session_id={identity}")
        return owners

    def _snapshots(self, session_id: str) -> tuple[tuple[MutationIntentOwner, ExecutionQueueSnapshot], ...]:
        snapshots = []
        for owner in self._owners(session_id):
            with self._access(owner.session_id, owner.thread_id, "read_control"):
                snapshots.append((owner, self._owner.execution_queue_snapshot(
                    owner.session_id, owner.thread_id, validate_inputs=True,
                )))
        return tuple(snapshots)

    @staticmethod
    @contextmanager
    def _access(session_id: str, thread_id: str, stage: str) -> Iterator[None]:
        """API 诊断按逻辑 owner 定位；原生路径与未校验正文只保留在原因链。"""
        try:
            yield
        except Exception as error:
            detail = (f"session_id={session_id}, thread_id={thread_id}, artifact=execution_queue, "
                      f"stage={stage}, error={type(error).__name__}")
            if isinstance(error, OSError):
                raise type(error)(error.errno, f"{detail}, errno={error.errno}") from error
            if isinstance(error, json.JSONDecodeError):
                detail += f", line={error.lineno}, column={error.colno}"
            raise RuntimeError(detail) from error

    async def record(self, session_id: str, thread_id: str, job_id: str) -> ExecutionQueueRecord:
        return await asyncio.to_thread(self._owner.execution_queue_record, session_id, thread_id, job_id)

    async def _workspace_snapshots(self) -> tuple[tuple[MutationIntentOwner, ExecutionQueueSnapshot], ...]:
        snapshots = []
        nodes = await run_sync_shielded(self._path_resolver.list_nodes)
        for candidate in nodes:
            if candidate.kind != "session" or candidate.state != "active":
                continue
            async with AsyncExitStack() as admission:
                async with self._topology_gate.shared():
                    await admission.enter_async_context(self._session_gate.shared(candidate.node_id))
                    try:
                        current = await run_sync_shielded(self._path_resolver.get_node, candidate.node_id)
                    except KeyError:
                        # 枚举后删除已提交；不能再打开该 owner 的物理目录。
                        continue
                    if current.state != "active":
                        continue
                # 删除必须等原 worker 关闭全部读取句柄；取消也不能提前归还 guard。
                snapshots.extend(await run_sync_shielded(self._snapshots, candidate.node_id))
        return tuple(snapshots)

    async def records(self, session_id: str | None = None) -> tuple[ExecutionQueueRecord, ...]:
        snapshots = (await self._workspace_snapshots() if session_id is None
                     else await asyncio.to_thread(self._snapshots, session_id))
        return tuple(record for _owner, snapshot in snapshots for record in snapshot.records)

    async def pauses(self) -> tuple[ExecutionQueuePause, ...]:
        return tuple(snapshot.pause for _owner, snapshot in await self._workspace_snapshots() if snapshot.pause is not None)

    async def pause_state(self, session_id: str, thread_id: str) -> ExecutionQueuePause | None:
        return await run_sync_shielded(self._owner.execution_queue_pause, session_id, thread_id)

    async def pause(self, session_id: str, thread_id: str, *, execution_id: str | None) -> ExecutionQueuePause:
        return await run_sync_shielded(self._owner.pause_execution_queue, session_id, thread_id, execution_id=execution_id)

    async def resume(self, session_id: str, thread_id: str, *, pause_id: str, continuation_job_id: str | None = None) -> None:
        await run_sync_shielded(self._owner.resume_execution_queue, session_id, thread_id,
                                pause_id=pause_id, continuation_job_id=continuation_job_id)

    async def snapshot_version(self, session_id: str) -> int:
        def read() -> int:
            revision = 0
            for owner in self._owners(session_id):
                with self._access(owner.session_id, owner.thread_id, "read_revision"):
                    revision += self._owner.execution_queue_revision(owner.session_id, owner.thread_id)
            return revision

        return await asyncio.to_thread(read)

    @staticmethod
    def _control(record: ExecutionQueueRecord, *, position: int, snapshot_version: int) -> PendingRequestControl:
        admission = record.cursor.admission
        frozen = record.input
        binding = frozen.execution_binding
        return PendingRequestControl(
            job_id=admission.job_id, execution_binding=binding, message_id=frozen.message_id,
            session_id=admission.session_id, thread_id=admission.thread_id,
            turn_id=binding.turn_id if isinstance(binding, UserExecutionIdentity) else None,
            agent_id=frozen.agent_id, gateway_id=frozen.gateway_id, request_id=frozen.request_id,
            delivery_policy=record.delivery_policy, enqueue_sequence=record.cursor.commit_id,
            position=position, status="queued", snapshot_version=snapshot_version,
            created_at=datetime.fromisoformat(frozen.created_at), updated_at=datetime.fromisoformat(record.updated_at),
        )

    async def project(self, record: ExecutionQueueRecord, *, position: int, snapshot_version: int) -> PendingRequestDTO:
        reference = self._control(record, position=position, snapshot_version=snapshot_version)
        with self._access(reference.session_id, reference.thread_id, "project_input"):
            message = await asyncio.to_thread(self._input_reader, reference)
        if (message.session_id, message.thread_id, message.message_id) != (
            reference.session_id, reference.thread_id, reference.message_id,
        ):
            raise RuntimeError("canonical 输入与执行队列 owner 不一致")
        return PendingRequestDTO(
            **reference.model_dump(), content=message.content, attachments=message.attachments,
            reorderable=record.priority == 0 and record.cursor.admission.execution_origin == "user_turn",
            message_created_at=message.created_at.isoformat(),
            message_metadata={**message.metadata, "boxteam_session_model": record.input.model},
        )

    async def load(self, session_id: str) -> ExecutionQueueLoad:
        snapshots = await asyncio.to_thread(self._snapshots, session_id)
        version = sum(snapshot.revision for _owner, snapshot in snapshots)
        queued = [record for _owner, snapshot in snapshots for record in snapshot.records if record.phase == "queued"]
        return ExecutionQueueLoad(
            tuple(queued),
            version,
        )

    async def load_summaries(self, session_id: str, *, limit: int, thread_id: str | None = None) -> PendingRequestSummaryListDTO:
        if not 1 <= limit <= 8:
            raise ValueError("待处理摘要 limit 必须在 1..8 范围内")
        snapshots = await asyncio.to_thread(self._snapshots, session_id)
        records = [record for owner, snapshot in snapshots if thread_id is None or owner.thread_id == thread_id
                   for record in snapshot.records if record.phase == "queued"]
        active = next((record.cursor.admission.job_id for owner, snapshot in snapshots
                       if thread_id is None or owner.thread_id == thread_id
                       for record in snapshot.records if record.phase in {"claimed", "started"}), None)
        summaries = [PendingRequestSummaryDTO(
            job_id=record.cursor.admission.job_id, message_id=record.input.message_id,
            thread_id=record.cursor.admission.thread_id, enqueue_sequence=record.cursor.commit_id,
            delivery_policy=record.delivery_policy, status="queued", updated_at=datetime.fromisoformat(record.updated_at),
        ) for record in records[:limit]]
        return PendingRequestSummaryListDTO(
            session_id=session_id, thread_id=thread_id, active_job_id=active, requests=summaries,
            request_count=len(records), snapshot_version=sum(snapshot.revision for _owner, snapshot in snapshots),
            truncated=len(summaries) < len(records),
        )

    async def update_policy(self, reference: PendingRequestControl, policy: DeliveryPolicy,
                            *, expected_snapshot_version: int | None) -> None:
        snapshots = await asyncio.to_thread(self._snapshots, reference.session_id)
        actual = sum(snapshot.revision for _owner, snapshot in snapshots)
        if expected_snapshot_version is not None and expected_snapshot_version != actual:
            raise RuntimeError(f"队列快照已过期: expected={expected_snapshot_version}, actual={actual}")
        revision = next(snapshot.revision for owner, snapshot in snapshots if owner.thread_id == reference.thread_id)
        await asyncio.to_thread(self._owner.update_execution_queue_policy,
                                reference.session_id, reference.thread_id, job_id=reference.job_id,
                                policy=policy, expected_revision=revision)

    async def snapshot(self, session_id: str, thread_id: str) -> PendingRequestListDTO:
        snapshots = await asyncio.to_thread(self._snapshots, session_id)
        version = sum(snapshot.revision for _owner, snapshot in snapshots)
        selected = next((snapshot for owner, snapshot in snapshots if owner.thread_id == thread_id), ExecutionQueueSnapshot((), 0))
        queued = [record for record in selected.records if record.phase == "queued"]
        return PendingRequestListDTO(
            session_id=session_id, thread_id=thread_id, thread_revision=selected.revision, snapshot_version=version,
            active_job_id=next((record.cursor.admission.job_id for record in selected.records if record.phase in {"claimed", "started"}), None),
            requests=[await self.project(record, position=position, snapshot_version=version) for position, record in enumerate(queued)],
        )

    async def reorder(self, session_id: str, thread_id: str, *, message_ids: tuple[str, ...], expected_revision: int) -> None:
        await asyncio.to_thread(self._owner.reorder_execution_queue, session_id, thread_id, message_ids=message_ids, expected_revision=expected_revision)

    async def before_dispatch(self, record: ExecutionQueueRecord) -> bool:
        return await asyncio.to_thread(self._owner.execution_queue_before_dispatch, record)

    async def claim(self, record: ExecutionQueueRecord) -> ExecutionQueueClaim | None:
        admission = record.cursor.admission
        holder = self._holder.lock_for(admission.session_id, admission.thread_id)
        await holder.__aenter__()
        if not holder.acquired:
            return None
        try:
            claimed = await asyncio.to_thread(
                self._owner.claim_execution_queue, admission.session_id, admission.thread_id,
                job_id=admission.job_id, claim_owner=self._claim_owner, previous_generation=record.claim_generation,
            )
            await observe_phase(admission.job_id, "execution.queue.claimed", details={
                "session_id": admission.session_id, "thread_id": admission.thread_id,
                "execution_id": admission.execution_id, "claim_generation": claimed.claim_generation,
            })
        except QueueOrderConflictError:
            await holder.__aexit__(None, None, None)
            return None
        except BaseException:
            await holder.__aexit__(None, None, None)
            raise
        return ExecutionQueueClaim(claimed, holder, self._owner)

    async def migrate_schema(self, session_id: str) -> None:
        """只接受已登记 v5 控制引用；逐 Thread 原子导入，全部成功才删除输入。"""
        from app.services.infrastructure.pending_queue_migration import (
            PendingQueueV5Migration,
        )

        path = self._legacy_path(session_id)
        if not path.exists():
            return
        original = path.read_bytes()
        _header, references = PendingQueueV5Migration._read_control_snapshot(session_id, path)
        groups: dict[str, list[tuple[str, ExecutionQueueInput]]] = {}
        for reference in references:
            if reference.provider_id is not None:
                raise ValueError(
                    f"pending v5 缺少完整模型引用，无法迁移 Provider 选择: "
                    f"session_id={session_id}, job_id={reference.job_id}, provider_id={reference.provider_id}"
                )
            # 原 canonical reader 同时校验正文和 typed admission，不从旧 JSON 补正文。
            await asyncio.to_thread(self._input_reader, reference)
            groups.setdefault(reference.thread_id, []).append((reference.job_id, ExecutionQueueInput(
                execution_binding=reference.execution_binding, message_id=reference.message_id,
                agent_id=reference.agent_id, model=None,
                delivery_policy=reference.delivery_policy, gateway_id=reference.gateway_id,
                request_id=reference.request_id, created_at=reference.created_at.isoformat(),
            )))
        for thread_id, inputs in groups.items():
            await asyncio.to_thread(self._owner.import_execution_queue, session_id, thread_id, tuple(inputs))
        if path.read_bytes() != original:
            raise RuntimeError("迁移期间 pending v5 原件已变化，保留原件")
        path.unlink()

    async def migrate_all(self) -> int:
        migrated = 0
        for node in self._path_resolver.list_nodes(kind="session"):
            if self._legacy_path(node.node_id).exists():
                await self.migrate_schema(node.node_id)
                migrated += 1
        return migrated
