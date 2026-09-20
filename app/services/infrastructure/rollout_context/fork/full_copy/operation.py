"""typed full-copy：staging 完成认证、本地化和验证后才原子安装。"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from app.services.infrastructure.node_debug.fork import (
    NodeDebugSourceCopySnapshot,
    NodeDebugWorkspaceForkConfig,
)
from app.services.infrastructure.rollout_context.fork.assembly_copy import (
    validate_source_assemblies,
)
from app.services.infrastructure.rollout_context.fork.full_copy.details import (
    write_private,
)
from app.services.infrastructure.rollout_context.fork.full_copy.staging import (
    FullCopyStagingStorage,
)
from app.services.infrastructure.rollout_context.fork.node_debug_materialization import (
    PreparedNodeDebugFork,
    prepare_node_debug_fork,
    publish_ready_node_debug_fork,
    ready_node_debug_fork,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    one_of_text,
    required_text,
)
from app.services.infrastructure.rollout_context.runtime.detail_fork import (
    ForkDetailCapability,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage


def full_rollout_copy(
    storage: RolloutStorage,
    *,
    source_session_id: str,
    target_session_id: str,
    source_checkpoint_id: str | None,
    relationship: str,
    checkpoint_ns: str,
    detail_capability: ForkDetailCapability,
    debug_snapshot: NodeDebugSourceCopySnapshot | None = None,
    debug_workspace_config: Callable[[], NodeDebugWorkspaceForkConfig] | None = None,
) -> tuple[str | None, str]:
    source_session_id = required_text(source_session_id, field="fork.source_session_id")
    target_session_id = required_text(target_session_id, field="fork.target_session_id")
    relationship = one_of_text(
        relationship, {"detached", "pinned"}, field="fork.relationship"
    )
    if source_session_id == target_session_id:
        raise ValueError("fork source/target session 必须不同")
    target = storage.root(target_session_id, checkpoint_ns)
    source = storage.root(source_session_id, checkpoint_ns)
    storage._safe_session_relative_path(target.parent, target.name)
    with storage._lock(target_session_id, checkpoint_ns):
        if target.exists():
            raise FileExistsError(target)
        with TemporaryDirectory(
            prefix=".fork-staging-", dir=target.parent
        ) as temporary:
            stage_root = Path(temporary) / "rollout"
            target_key = secrets.token_bytes(32)
            stage = FullCopyStagingStorage(
                storage,
                source=source_session_id,
                target=target_session_id,
                root=stage_root,
                capability=detail_capability,
                target_session_key=target_key,
            )
            # 从读取 source manifest 到全部正文认证保持共享锁；不修改 source SQLite。
            with ExitStack() as source_locks:
                source_lock = _RolloutFileLock(
                    source.parent / ".rollout.write.lock", exclusive=False
                )
                source_lock.acquire()
                source_locks.callback(source_lock.release)
                source_view = stage.clone_rollout(
                    source_thread_id=source_session_id,
                    target_thread_id=target_session_id,
                    source_checkpoint_id=source_checkpoint_id,
                    checkpoint_ns=checkpoint_ns,
                    detail_capability=detail_capability,
                )
                write_private(stage_root / ".context-redaction-key", target_key)
                fields = {
                    "source_session_id": source_session_id,
                    "target_session_id": target_session_id,
                    "source_checkpoint_id": source_checkpoint_id,
                    "source_view_id": source_view,
                    "fork_mode": "full_rollout_copy",
                    "relationship": relationship,
                    "checkpoint_ns": checkpoint_ns,
                }
                materialization, fork_id = stage.begin_fork_materialization(**fields)
                prepared_debug = prepare_node_debug_fork(
                    stage,
                    debug_snapshot,
                    debug_workspace_config,
                    materialization_id=materialization,
                    fork_id=fork_id,
                    target_session_id=target_session_id,
                    checkpoint_ns=checkpoint_ns,
                )
                if debug_snapshot is not None and prepared_debug is not None:
                    ready_node_debug_fork(
                        stage,
                        debug_snapshot,
                        prepared_debug,
                        debug_workspace_config,
                        materialization_id=materialization,
                        target_session_id=target_session_id,
                        checkpoint_ns=checkpoint_ns,
                    )
                stage.commit_fork_materialization(
                    materialization, **fields, defer_completion=True
                )
            with stage._connect(target_session_id, checkpoint_ns) as connection:
                stage._validate_schema_state(connection)
                stage._validate_v2_commit_offsets(
                    connection, stage_root / "rollout.jsonl"
                )
                validate_source_assemblies(stage, connection, checkpoint_ns)
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("fork staging SQLite integrity_check failed")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("fork staging SQLite foreign_key_check failed")
                if (
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                    != 0
                ):
                    raise RuntimeError("fork staging SQLite checkpoint failed")
            install_staging(stage_root, target)
            if debug_snapshot is not None and prepared_debug is not None:
                installed_prepared = PreparedNodeDebugFork(
                    staging_root=target
                    / ".fork-debug-staging"
                    / materialization
                    / "node",
                    target_node=prepared_debug.target_node,
                    target_manifest_sha256=prepared_debug.target_manifest_sha256,
                )
                # rollout 与 ready journal 已先原子安装。后续任意崩溃都能由
                # target recovery 清理 staging/debug，不会形成无 journal 半发布。
                publish_ready_node_debug_fork(
                    storage,
                    installed_prepared,
                    materialization_id=materialization,
                    target_session_id=target_session_id,
                    checkpoint_ns=checkpoint_ns,
                )
        # 安装后 journal 为 target_committed；retention 失败由既有恢复链重试。
        storage.commit_fork_materialization(materialization, **fields)
    return source_view, fork_id


def install_staging(stage_root: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    stage_root.rename(target)
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
