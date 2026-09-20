"""Node debug fork proof 与 materialization SQLite journal。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.services.infrastructure.node_debug.fork import (
    NodeDebugSourceCopySnapshot,
    NodeDebugTargetPrepublication,
)
from app.services.infrastructure.rollout_context.fork.validation import required_text


def _now() -> str:
    return datetime.now(UTC).isoformat()


class NodeDebugForkJournalMixin:
    """把 debug proof 纳入 fork materialization 的唯一 SQLite journal。"""

    def prepare_fork_debug_snapshot(
        self,
        materialization_id: str,
        fork_id: str,
        snapshot: NodeDebugSourceCopySnapshot,
        prepublication: NodeDebugTargetPrepublication,
        *,
        target_session_id: str,
        staging_relative_path: Path,
        target_manifest_sha256: str,
        checkpoint_ns: str = "",
    ) -> None:
        """把冻结 debug proof 写进与 fork materialization 同一 SQLite journal。"""
        materialization_id = required_text(materialization_id, field="materialization_id")
        fork_id = required_text(fork_id, field="fork_id")
        if snapshot.source_snapshot_id != prepublication.source_snapshot_id:
            raise RuntimeError("fork debug snapshot 与 target prepublication 不匹配")
        target_manifest_sha256 = required_text(
            target_manifest_sha256, field="target_manifest_sha256"
        )
        source_configurations = [
            {
                "configuration_id": artifact.configuration_id,
                "revision": artifact.revision,
                "payload_bytes": artifact.payload_bytes.hex(),
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
                "source_lineage": [list(item) for item in artifact.source_lineage],
            }
            for artifact in snapshot.configuration_artifacts
        ]
        with self._lock(target_session_id, checkpoint_ns), self._connect(
            target_session_id, checkpoint_ns
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM fork_identity_mappings "
                "WHERE fork_id = ? AND entity_type = 'debug_snapshot'",
                (fork_id,),
            ).fetchone()
            if existing is not None:
                raise RuntimeError("同一 fork 已存在 debug snapshot journal")
            row = connection.execute(
                "SELECT fork_id, target_session_id, status FROM fork_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
            if row is None or row[0] != fork_id or row[1] != target_session_id:
                raise KeyError(f"fork materialization 不存在: {materialization_id}")
            if row[2] != "prepared":
                raise RuntimeError("fork debug snapshot 只能写入 prepared journal")
            expected_relative = (
                Path("rollout")
                / ".fork-debug-staging"
                / materialization_id
                / "node"
            )
            if staging_relative_path != expected_relative:
                raise RuntimeError("fork debug staging journal 路径与 materialization 不一致")
            staging_relative = staging_relative_path.as_posix()
            timestamp = _now()
            lineage = {
                "kind": "node_debug_source_copy_snapshot",
                "materialization_id": materialization_id,
                "state": "prepared",
                "capture_mode": snapshot.capture_mode,
                "source_thread_id": snapshot.source_thread_id,
                "source_manifest_bytes": snapshot.manifest_bytes.hex(),
                "source_manifest_size_bytes": snapshot.manifest_size_bytes,
                "source_manifest_sha256": snapshot.manifest_sha256,
                "source_manifest_revision": snapshot.manifest_revision,
                "source_configurations": source_configurations,
                "source_lineage": [list(item) for item in snapshot.source_lineage],
                "source_workspace_config_revision": snapshot.workspace_config_revision,
                "source_workspace_config_hash": snapshot.workspace_config_hash,
                "target_workspace_config_revision": prepublication.target_workspace_config_revision,
                "target_workspace_config_hash": prepublication.target_workspace_config_hash,
                "target_configuration_id_map": list(
                    prepublication.configuration_id_map
                ),
                "target_debug_staging_path": staging_relative,
                "target_manifest_sha256": target_manifest_sha256,
            }
            result = connection.execute(
                "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at) VALUES (?, ?, ?, ?, 'debug_snapshot', ?, ?, NULL, NULL, ?, ?)",
                (
                    uuid4().hex,
                    fork_id,
                    snapshot.source_session_id,
                    target_session_id,
                    snapshot.source_snapshot_id,
                    materialization_id,
                    json.dumps(lineage, ensure_ascii=False, sort_keys=True),
                    timestamp,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("fork debug snapshot journal 未写入")
            for artifact, (_source_id, target_id) in zip(
                snapshot.configuration_artifacts,
                prepublication.configuration_id_map,
                strict=True,
            ):
                if _source_id != artifact.configuration_id:
                    raise RuntimeError("fork debug configuration 映射顺序不一致")
                mapping_lineage = {
                    "kind": "node_debug_configuration",
                    "source_session_id": snapshot.source_session_id,
                    "source_thread_id": snapshot.source_thread_id,
                    "source_configuration_id": artifact.configuration_id,
                    "source_revision": artifact.revision,
                    "source_sha256": artifact.sha256,
                    "target_configuration_id": target_id,
                }
                inserted = connection.execute(
                    "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at) VALUES (?, ?, ?, ?, 'debug_configuration', ?, ?, NULL, NULL, ?, ?)",
                    (
                        uuid4().hex,
                        fork_id,
                        snapshot.source_session_id,
                        target_session_id,
                        artifact.configuration_id,
                        target_id,
                        json.dumps(
                            mapping_lineage, ensure_ascii=False, sort_keys=True
                        ),
                        timestamp,
                    ),
                )
                if inserted.rowcount != 1:
                    raise RuntimeError("fork debug configuration lineage 未写入")
            connection.commit()

    def mark_fork_debug_snapshot_published(
        self,
        materialization_id: str,
        *,
        target_session_id: str,
        checkpoint_ns: str = "",
        target_manifest_sha256: str | None,
    ) -> None:
        with self._lock(target_session_id, checkpoint_ns), self._connect(
            target_session_id, checkpoint_ns
        ) as connection:
            row = connection.execute(
                "SELECT mapping_id, lineage_json FROM fork_identity_mappings "
                "WHERE target_session_id = ? AND entity_type = 'debug_snapshot' "
                "AND target_local_id = ?",
                (target_session_id, materialization_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("fork debug snapshot journal 不存在")
            lineage = json.loads(required_text(row[1], field="debug_snapshot.lineage"))
            if not isinstance(lineage, dict) or lineage.get("state") != "ready":
                raise RuntimeError("fork debug snapshot 不处于 ready")
            lineage["state"] = "published"
            expected_manifest_sha256 = required_text(
                lineage.get("target_manifest_sha256"),
                field="debug_snapshot.target_manifest_sha256",
            )
            if (
                required_text(target_manifest_sha256, field="target_manifest_sha256")
                != expected_manifest_sha256
            ):
                raise RuntimeError("fork debug manifest hash 与 ready journal 不一致")
            lineage["published_at"] = _now()
            result = connection.execute(
                "UPDATE fork_identity_mappings SET lineage_json = ? WHERE mapping_id = ?",
                (json.dumps(lineage, ensure_ascii=False, sort_keys=True), row[0]),
            )
            if result.rowcount != 1:
                raise RuntimeError("fork debug snapshot 未进入 published")
            connection.commit()

    def mark_fork_debug_snapshot_ready(
        self,
        materialization_id: str,
        *,
        target_session_id: str,
        checkpoint_ns: str = "",
        target_manifest_sha256: str,
    ) -> None:
        """在最终配置复核后冻结可恢复发布所需的 target manifest hash。"""
        with self._lock(target_session_id, checkpoint_ns), self._connect(
            target_session_id, checkpoint_ns
        ) as connection:
            row = connection.execute(
                "SELECT mapping_id, lineage_json FROM fork_identity_mappings "
                "WHERE target_session_id = ? AND entity_type = 'debug_snapshot' "
                "AND target_local_id = ?",
                (target_session_id, materialization_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("fork debug snapshot journal 不存在")
            lineage = json.loads(required_text(row[1], field="debug_snapshot.lineage"))
            if not isinstance(lineage, dict) or lineage.get("state") != "prepared":
                raise RuntimeError("fork debug snapshot 不处于 prepared")
            expected_manifest_sha256 = required_text(
                lineage.get("target_manifest_sha256"),
                field="debug_snapshot.target_manifest_sha256",
            )
            if (
                required_text(target_manifest_sha256, field="target_manifest_sha256")
                != expected_manifest_sha256
            ):
                raise RuntimeError("fork debug manifest hash 与 prepared journal 不一致")
            lineage["state"] = "ready"
            lineage["ready_at"] = _now()
            result = connection.execute(
                "UPDATE fork_identity_mappings SET lineage_json = ? WHERE mapping_id = ?",
                (json.dumps(lineage, ensure_ascii=False, sort_keys=True), row[0]),
            )
            if result.rowcount != 1:
                raise RuntimeError("fork debug snapshot 未进入 ready")
            connection.commit()

    def _assert_fork_debug_snapshot_ready(
        self,
        connection,
        materialization_id: str,
        *,
        allow_ready: bool = False,
    ) -> None:
        row = connection.execute(
            "SELECT mapping.lineage_json FROM fork_materializations AS materialization "
            "JOIN fork_identity_mappings AS mapping ON mapping.fork_id = materialization.fork_id "
            "AND mapping.entity_type = 'debug_snapshot' "
            "WHERE materialization.materialization_id = ?",
            (materialization_id,),
        ).fetchone()
        if row is None:
            return
        lineage = json.loads(required_text(row[0], field="debug_snapshot.lineage"))
        expected_states = {"published", "ready"} if allow_ready else {"published"}
        if not isinstance(lineage, dict) or lineage.get("state") not in expected_states:
            raise RuntimeError(
                f"fork debug snapshot 尚未发布，禁止提交 materialization: {materialization_id}"
            )




__all__ = ["NodeDebugForkJournalMixin"]
