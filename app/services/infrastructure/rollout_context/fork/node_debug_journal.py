"""显式静态调试方案的目标发布 journal；不登记历史身份映射。"""

from __future__ import annotations

import json
from typing import Literal

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugSourceCopySnapshot,
    NodeDebugTargetPrepublication,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
)


class NodeDebugForkJournalMixin:
    def read_fork_debug_snapshot(
        self, materialization_id: str, *, target_session_id: str,
        target_thread_id: str, checkpoint_ns: str = "",
    ) -> tuple[str, dict[str, object]] | None:
        with self._storage._connect(target_thread_id, checkpoint_ns,
                                   session_id=target_session_id, read_only=True) as connection:
            row = connection.execute(
                "SELECT state,proof_json FROM fork_debug_snapshots WHERE materialization_id=? "
                "AND target_session_id=? AND target_thread_id=?",
                (materialization_id, target_session_id, target_thread_id),
            ).fetchone()
        return (row[0], json.loads(row[1])) if row is not None else None

    def prepare_fork_debug_snapshot(
        self, materialization_id: str, fork_id: str, snapshot: NodeDebugSourceCopySnapshot,
        prepublication: NodeDebugTargetPrepublication, *, target_session_id: str,
        target_thread_id: str, target_manifest_sha256: str, checkpoint_ns: str = "",
    ) -> None:
        if snapshot.source_snapshot_id != prepublication.source_snapshot_id:
            raise RuntimeError("fork debug snapshot 与 target prepublication 不匹配")
        proof = {
            "fork_id": fork_id, "snapshot": snapshot.to_payload(),
            "target_manifest_sha256": target_manifest_sha256,
            "target_configuration_id_map": prepublication.configuration_id_map,
            "target_workspace_config_revision": prepublication.target_workspace_config_revision,
            "target_workspace_config_hash": prepublication.target_workspace_config_hash,
        }
        encoded = canonical_json_bytes(proof).decode()
        with self._storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id), self._storage._connect(
            target_thread_id, checkpoint_ns, session_id=target_session_id,
        ) as connection:
            if current_scope(connection) != (target_session_id, target_thread_id):
                raise RuntimeError("fork debug journal 当前 owner 不一致")
            journal = connection.execute(
                "SELECT fork_id,status FROM fork_materializations WHERE materialization_id=? "
                "AND target_session_id=? AND target_thread_id=? AND checkpoint_ns=?",
                (materialization_id, target_session_id, target_thread_id, checkpoint_ns),
            ).fetchone()
            if journal != (fork_id, "prepared"):
                raise RuntimeError("fork debug snapshot 缺少精确目标 prepared materialization")
            existing = connection.execute(
                "SELECT target_session_id,target_thread_id,proof_json FROM fork_debug_snapshots "
                "WHERE materialization_id=?", (materialization_id,),
            ).fetchone()
            if existing is not None:
                if existing != (target_session_id, target_thread_id, encoded):
                    raise RuntimeError("fork debug snapshot 与原冻结 publication proof 冲突")
                return
            connection.execute(
                "INSERT INTO fork_debug_snapshots(materialization_id,target_session_id,target_thread_id,"
                "state,proof_json,created_at) VALUES (?,?,?,'prepared',?,?)",
                (materialization_id, target_session_id, target_thread_id, encoded, utc_now_text()),
            )
            connection.commit()

    def advance_fork_debug_snapshot(
        self, materialization_id: str, *, target_session_id: str, target_thread_id: str,
        state: Literal["ready", "published"], checkpoint_ns: str = "",
    ) -> None:
        previous = "prepared" if state == "ready" else "ready"
        timestamp_column = "ready_at" if state == "ready" else "published_at"
        with self._storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id), self._storage._connect(
            target_thread_id, checkpoint_ns, session_id=target_session_id,
        ) as connection:
            if current_scope(connection) != (target_session_id, target_thread_id):
                raise RuntimeError("fork debug publication 当前 owner 不一致")
            row = connection.execute(
                "SELECT state FROM fork_debug_snapshots WHERE materialization_id=? "
                "AND target_session_id=? AND target_thread_id=?",
                (materialization_id, target_session_id, target_thread_id),
            ).fetchone()
            if row == (state,):
                return
            if row != (previous,):
                raise RuntimeError(f"fork debug snapshot 不能从 {row} 进入 {state}")
            changed = connection.execute(
                f"UPDATE fork_debug_snapshots SET state=?,{timestamp_column}=? "
                "WHERE materialization_id=? AND target_session_id=? AND target_thread_id=? AND state=?",
                (state, utc_now_text(), materialization_id, target_session_id, target_thread_id, previous),
            )
            if changed.rowcount != 1:
                raise RuntimeError("fork debug publication CAS 未成功")
            connection.commit()

    def _assert_fork_debug_snapshot_published(self, connection, materialization_id: str) -> None:
        session_id, thread_id = current_scope(connection)
        row = connection.execute(
            "SELECT state,proof_json FROM fork_debug_snapshots WHERE materialization_id=? "
            "AND target_session_id=? AND target_thread_id=?", (materialization_id, session_id, thread_id),
        ).fetchone()
        if row is None:
            return
        if row[0] != "published":
            raise RuntimeError(f"fork debug snapshot 尚未发布: {materialization_id}")
        from app.services.infrastructure.rollout_context.fork.node_debug_materialization import (
            verify_published_target_snapshot,
        )

        proof = json.loads(row[1])
        verify_published_target_snapshot(
            self._storage.fork_paths.thread_node(session_id, thread_id),
            target_session_id=session_id, target_thread_id=thread_id,
            manifest_sha256=proof["target_manifest_sha256"],
            source_configurations_json=json.dumps(proof["snapshot"]["configuration_artifacts"]),
            configuration_id_map_json=json.dumps(proof["target_configuration_id_map"]),
        )
