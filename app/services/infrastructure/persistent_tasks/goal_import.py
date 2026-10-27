"""goal.json 的显式一次性导入；生产读写不再访问旧文件。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.atomic_fs import fsync_directory, native_io_path
from app.core.session_catalog_store import SessionCreationRecord
from app.schemas.internal_v2.goal import SessionGoalDTO

from .store import PersistentTaskStore


def import_legacy_goals(store: PersistentTaskStore) -> int:
    paths = store._goal_paths()
    candidates: list[tuple[str, str, Path, SessionCreationRecord | None]] = [
        (node.node_id, node.main_thread_id, paths.resolve_session_node(node.node_id), None)
        for node in paths.list_nodes() if node.kind == "session" and node.state == "active"
    ]
    for record in paths.catalog_store.list_preparing_creation_records(workspace_id=store.workspace_id, purpose="context_fork"):
        # record 可能先于物理准备提交；没有旧文件的记录仍交给原 creation owner 恢复。
        locations = (paths.sessions_root / ".staging" / record.session_id,
                     paths.catalog_store.resolve_session_locator(record.storage_relative_locator))
        if any((directory / "goal.json").exists() or (directory / "goal.json").is_symlink() for directory in locations):
            candidates.append((record.session_id, record.main_thread_id, paths.resolve_prepared_session_node(record), record))
    imported = 0
    for session_id, thread_id, directory, creation in candidates:
        path = native_io_path(directory / "goal.json")
        if path.is_symlink():
            raise RuntimeError(f"Goal 迁移拒绝符号链接: session_id={session_id}")
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            continue
        raw = json.loads(payload)
        if raw.get("session_id") != session_id:
            raise RuntimeError(f"Goal 旧文件 Session 归属冲突: session_id={session_id}")
        goal = SessionGoalDTO.model_validate({**raw, "thread_id": thread_id})
        store.goal_import(goal, hashlib.sha256(payload).hexdigest(), creation=creation)
        # SQL 事实和指纹先提交；删除失败可重试，不能重新覆盖已编辑/清除的 Goal。
        path.unlink()
        fsync_directory(directory)
        imported += 1
    return imported
