"""本地 pinned fork 的 target→source→target 释放 saga；任何阶段只持一个 Session gate。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.atomic_fs import native_io_path
from app.core.phase_observer import observe_phase
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.fork_retention_release import (
    ForkRetentionReleaseRecord,
    commit_fork_retention_release,
    prepare_fork_retention_release,
    read_fork_retention_lease,
    read_fork_retention_releases,
)
from app.core.session_lifecycle_gate import NavigationTopologyGate, SessionLifecycleGate

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


async def release_fork_retentions(
    storage: RolloutStorage, catalog: SessionCatalogStore, target_session_id: str,
    *, target_thread_id: str | None = None, copy_operation_id: str | None = None,
) -> None:
    topology = NavigationTopologyGate(storage.sessions_dir)
    gate = SessionLifecycleGate(storage.sessions_dir)
    async with topology.shared(), gate.exclusive(target_session_id):
        target_node = storage.fork_paths.session_node(target_session_id)
        control_path = target_node / CONTROL_DATABASE_NAME
        if not native_io_path(control_path).is_file():
            raise RuntimeError(f"fork retention release 缺少 target 控制库: {control_path}")
        control = SessionControlStore(control_path)
        try:
            for claim in catalog.list_fork_retention_claims_for_target(target_session_id):
                if claim.state == "released":
                    continue
                capture = catalog.get_fork_source_capture(fork_id=claim.fork_retention_claim_id)
                if capture is None:
                    raise RuntimeError(f"fork retention claim 缺少 target capture record: {claim.fork_retention_claim_id}")
                if (target_thread_id is not None and capture.target_thread_id != target_thread_id
                    or copy_operation_id is not None and capture.copy_operation_id != copy_operation_id):
                    continue
                prepare_fork_retention_release(
                    control, claim=claim, capture=capture,
                    release_reason="target session 删除或 fork 中止",
                )
            records = tuple(record for record in read_fork_retention_releases(control)
                            if (target_thread_id is None or record.target_thread_id == target_thread_id)
                            and (copy_operation_id is None or record.copy_operation_id == copy_operation_id))
        finally:
            control.close()
    for record in records:
        if record.target_session_id != target_session_id:
            raise RuntimeError("fork retention release record 不属于当前 target")
        claim = catalog.get_fork_retention_claim(record.fork_retention_claim_id)
        record.verify_claim(claim)
        if record.state == "committed":
            if claim.state != "released" or claim.release_reason != record.release_reason:
                raise RuntimeError("fork retention release 已 committed，但 source claim 尚未释放")
            continue
        await observe_phase(record.fork_retention_claim_id, "fork.retention.release.prepared")
        await _release_source_retention(storage, catalog, topology, gate, record)
        await observe_phase(record.fork_retention_claim_id, "fork.retention.release.source_released")
        async with topology.shared(), gate.exclusive(target_session_id):
            control = SessionControlStore(storage.fork_paths.session_node(target_session_id) / CONTROL_DATABASE_NAME)
            try:
                claim = catalog.get_fork_retention_claim(record.fork_retention_claim_id)
                record.verify_claim(claim)
                if claim.state != "released" or claim.release_reason != record.release_reason:
                    raise RuntimeError("fork retention target 确认前 source claim 未终结")
                commit_fork_retention_release(control, record)
            finally:
                control.close()
        await observe_phase(record.fork_retention_claim_id, "fork.retention.release.committed")


async def _release_source_retention(
    storage: RolloutStorage, catalog: SessionCatalogStore,
    topology: NavigationTopologyGate, gate: SessionLifecycleGate,
    record: ForkRetentionReleaseRecord,
) -> None:
    async with topology.shared(), gate.exclusive(record.source_session_id):
        claim = catalog.get_fork_retention_claim(record.fork_retention_claim_id)
        record.verify_claim(claim)
        capture = catalog.get_fork_source_capture(copy_operation_id=record.copy_operation_id)
        if capture is None:
            raise RuntimeError("fork retention release recovery 缺少原 capture record")
        record.verify_capture(capture)
        if claim.state == "released":
            if claim.release_reason != record.release_reason:
                raise RuntimeError("fork retention release reason 与 source 终态不一致")
            # source 可在该 durable 终态后已删除；不能再打开其目录或补建 index。
            return
        source = catalog.get_node(record.source_session_id)
        if source.state != "active" or source.workspace_id != record.workspace_id:
            raise RuntimeError("未释放的 fork retention source catalog 非 active 或 workspace 冲突")
        source_control_path = storage.fork_paths.session_node(record.source_session_id) / CONTROL_DATABASE_NAME
        if not native_io_path(source_control_path).is_file():
            raise RuntimeError(f"fork retention release 缺少 source 控制库: {source_control_path}")
        control = SessionControlStore(source_control_path, read_only=True)
        try:
            control.verify_matches_catalog_main_thread(source.main_thread_id)
            if control.get_fence() != ("active", record.source_lifecycle_generation):
                raise RuntimeError("fork retention release source lifecycle generation/fence 已漂移")
            lease = read_fork_retention_lease(control, capture, expected_generation=record.source_lifecycle_generation)
            if (lease is None and claim.state != "preparing") or (
                lease is not None and lease.state not in {"active", "settling", "completed"}
            ):
                raise RuntimeError("fork retention release 缺少可恢复的原 source lease")
        finally:
            control.close()
        index_path = storage.index_path(
            record.source_session_id, record.checkpoint_ns, thread_id=record.source_thread_id,
        )
        if not native_io_path(index_path).is_file():
            if claim.state != "preparing":
                raise RuntimeError(f"active fork retention source index 缺失: {index_path}")
        else:
            with (
                storage._lock(record.source_thread_id, record.checkpoint_ns, session_id=record.source_session_id),
                storage._connect(record.source_thread_id, record.checkpoint_ns, session_id=record.source_session_id) as connection,
            ):
                storage._require_v2_runtime(connection)
                owner = connection.execute("SELECT session_id, thread_id FROM database_meta WHERE singleton_id = 1").fetchone()
                if owner != (record.source_session_id, record.source_thread_id):
                    raise RuntimeError("fork retention release source rollout owner 不一致")
                refs = connection.execute(
                    "SELECT owner_session_id, status FROM retention_refs WHERE reference_kind = 'fork' AND reference_id = ?",
                    (record.fork_retention_claim_id,),
                ).fetchall()
                if (claim.state == "active" and not refs) or any(
                    row[0] != record.target_session_id or row[1] not in {"active", "released"} for row in refs
                ):
                    raise RuntimeError("fork retention release source ref 缺失或与 target 前像冲突")
                connection.execute(
                    "UPDATE retention_refs SET status = 'released' WHERE reference_kind = 'fork' "
                    "AND reference_id = ? AND owner_session_id = ? AND status = 'active'",
                    (record.fork_retention_claim_id, record.target_session_id),
                )
            await observe_phase(record.fork_retention_claim_id, "fork.retention.release.source_ref_released")
        if lease is not None:
            control = SessionControlStore(source_control_path)
            try:
                with control._write_transaction() as connection:
                    current = read_fork_retention_lease(control, capture, expected_generation=record.source_lifecycle_generation)
                    if current != lease:
                        raise RuntimeError("fork retention release source lease/token 已漂移")
                    if lease.state == "active":
                        lease = control.mark_lease_settling(
                            lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                            transaction_connection=connection,
                        )
                    if lease.state == "settling":
                        control.settle_lease(
                            lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                            outcome="completed", transaction_connection=connection,
                        )
            finally:
                control.close()
            await observe_phase(record.fork_retention_claim_id, "fork.retention.release.source_lease_settled")
        # rollout ref 的独立事务先 durable，catalog claim 的事务随后提交。
        catalog.release_fork_retention_claim(record.fork_retention_claim_id, record.release_reason)
