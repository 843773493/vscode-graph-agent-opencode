"""固定旧来源的会话历史升级；复用原维护屏障、备份、CAS 发布与恢复。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from app.core.atomic_fs import fsync_directory
from app.core.session_catalog_store.migrations.v11_sqlite_to_v12_sqlite import (
    upgrade_catalog_v11,
    validate_catalog_v11,
)
from app.core.session_catalog_store.validators import validate_session_storage_identity
from app.core.session_control_store.migrations.v18_sqlite_to_v20_sqlite import (
    upgrade_control_v18,
    validate_control_v18,
)
from app.core.sqlite_state import sqlite_file_uri
from app.services.infrastructure.rollout_context.storage.migrations.v26_sqlite_to_v28_sqlite import (
    upgrade_rollout_v26,
    validate_rollout_v26,
)
from app.services.infrastructure.session_trace.migrations.v1_jsonl_to_v3_sqlite import (
    LEGACY_FILES,
    convert_trace_v1,
    iter_v1_trace,
)
from configs.installer import atomic_write
from configs.maintenance.runtime_logging_commit import (
    MigrationAsset,
    MigrationJournal,
    _digest,
    _json_bytes,
    _lock_databases,
    _publish,
)


@dataclass(frozen=True)
class SessionHistoryPlan:
    workspace: Path
    databases: tuple[tuple[Path, Callable[[sqlite3.Connection], None]], ...]
    traces: tuple[tuple[Path, str, str], ...]


class SessionHistoryJournal(MigrationJournal):
    format_version: Literal[1]
    source_commit: str
    discarded_trace_events: list[dict[str, object]]


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(sqlite_file_uri(path) + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN")
    return connection


def _checkpoint(path: Path) -> None:
    # 整文件备份及 CAS 必须包含已提交 WAL；忙碌 reader/writer 不能被文件替换绕过。
    with closing(sqlite3.connect(sqlite_file_uri(path) + "?mode=rw", uri=True, timeout=0)) as connection:
        if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise RuntimeError(f"历史 SQLite 仍有活跃使用者，保留原件: {path}")


def preview_session_history(workspace: Path) -> SessionHistoryPlan | None:
    """只从已登记的 catalog 及 main Thread 读取来源，不扫盘补身份。"""
    root = workspace / ".boxteam"
    catalog = root / "navigation/session-catalog.sqlite"
    if not catalog.exists():
        return None
    with closing(_readonly(catalog)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 12:
            return None
        validate_catalog_v11(connection)
        sessions = connection.execute("SELECT * FROM nodes WHERE kind='session'").fetchall()
    databases: list[tuple[Path, Callable[[sqlite3.Connection], None]]] = [(catalog, upgrade_catalog_v11)]
    traces: list[tuple[Path, str, str]] = []
    for session in sessions:
        session_id, thread_id = session["node_id"], session["main_thread_id"]
        locator = session["storage_relative_locator"]
        validate_session_storage_identity(session_id, datetime.fromisoformat(session["created_at"]), locator)
        node = root / locator
        if node.is_symlink() or not node.is_dir() or node.resolve() != node:
            raise ValueError(f"旧 catalog 定位的会话目录缺失或被替换，保留原件: {node}")
        control = node / "session-control.sqlite"
        with closing(_readonly(control)) as connection:
            validate_control_v18(connection)
            threads = connection.execute("SELECT thread_id,kind FROM thread_catalog").fetchall()
            if [tuple(row) for row in threads] != [(thread_id, "main")]:
                raise ValueError("固定来源升级尚未登记 child Thread 恢复合同，保留原件")
        databases.append((control, upgrade_control_v18))
        rollout = node / "threads" / thread_id / "rollout/index.sqlite"
        jsonl = rollout.with_name("rollout.jsonl")
        if rollout.exists():
            with closing(_readonly(rollout)) as connection:
                validate_rollout_v26(connection, jsonl, session_id=session_id, thread_id=thread_id)
            databases.append((rollout, partial(upgrade_rollout_v26, jsonl_path=jsonl, session_id=session_id, thread_id=thread_id)))
        trace = node / "logs/traces"
        if (trace / "trace.sqlite").exists():
            raise ValueError(f"固定来源不能混用 Trace JSONL 与 SQLite，保留原件: {trace}")
        if any((trace / name).exists() for name in LEGACY_FILES):
            for _record in iter_v1_trace(trace, session_id=session_id, main_thread_id=thread_id):
                pass
            traces.append((trace, session_id, thread_id))
    return SessionHistoryPlan(workspace, tuple(databases), tuple(traces))


def _prepare(plan: SessionHistoryPlan) -> SessionHistoryJournal:
    root = plan.workspace / ".boxteam"
    backup_root = root / "session-history-migrations" / uuid4().hex
    backup_root.mkdir(parents=True, mode=0o700)
    assets: list[MigrationAsset] = []
    discarded: list[dict[str, object]] = []

    def asset(path: Path, before: Path, after: Path, *, retire: bool = False) -> None:
        if path.exists():
            atomic_write(before, path.read_bytes(), 0o600)
        assets.append({"kind": "file", "path": str(path), "before": str(before), "after": str(after),
                       "before_digest": _digest(before), "after_digest": None if retire else _digest(after)})

    for index, (path, convert) in enumerate(plan.databases):
        _checkpoint(path)
        before, after = backup_root / f"db-{index}.before.sqlite", backup_root / f"db-{index}.after.sqlite"
        atomic_write(after, path.read_bytes(), 0o600)
        with closing(sqlite3.connect(after)) as candidate:
            candidate.row_factory = sqlite3.Row
            convert(candidate)
        asset(path, before, after)
    for index, (directory, session_id, thread_id) in enumerate(plan.traces):
        after = backup_root / f"trace-{index}.after.sqlite"
        counts = convert_trace_v1(directory, after, session_id=session_id, main_thread_id=thread_id)
        if counts:
            discarded.append({"session_id": session_id, "counts": counts})
        asset(directory / "trace.sqlite", backup_root / f"trace-{index}.absent", after)
        for name in LEGACY_FILES:
            path = directory / name
            if path.exists():
                asset(path, backup_root / f"trace-{index}.{name}.before", backup_root / "absent", retire=True)
    return {"format_version": 1, "action": "session-history", "state": "prepared", "workspaces": [str(plan.workspace)],
            "source_commit": "bf0f2e1d9d037efcb0210c458fa157fb4d54a27a", "discarded_trace_events": discarded,
            "assets": assets, "backup_root": str(backup_root), "settings": None,
            "guards": [str(root / "state/workspace.sqlite"), str(root / "workspace.jsonc")],
            "databases": [], "format_notice": "原 catalog 创建意图和 preimage、SQLite 与 Trace JSONL 全部保存在 backup_root；消息及请求正文不重写。"}


def migrate_session_history(workspace: Path, *, restore: bool = False) -> dict[str, object] | None:
    workspace = workspace.expanduser().resolve()
    journal_path = workspace / ".boxteam/.session-history-migration.json"
    with ExitStack() as stack:
        # 与真实 Workspace 启动共用同一进程 ownership；未停写时不创建备份或候选。
        _lock_databases([workspace / ".boxteam/state/workspace.sqlite"], stack)
        journal = cast(SessionHistoryJournal | None, json.loads(journal_path.read_bytes()) if journal_path.exists() else None)
        if journal is not None and journal["format_version"] != 1:
            raise ValueError("会话历史迁移 journal 版本未知，保留原件")
        if journal is not None and journal["workspaces"] != [str(workspace)]:
            raise ValueError("会话历史迁移属于另一工作区，禁止重绑定")
        if journal is not None and journal["state"] == "restored" and not restore:
            raise ValueError("会话历史已显式回退，不自动反转原恢复方向")
        if journal is None:
            if restore:
                raise ValueError("没有可恢复的会话历史迁移原件")
            plan = preview_session_history(workspace)
            if plan is None:
                return None
            journal = _prepare(plan)
            atomic_write(journal_path, _json_bytes(journal), 0o600)
        elif restore or journal["state"] != "completed":
            # 中断后及显式恢复同样先合入 WAL，防止 CAS 遗漏只存在于 WAL 的新事实。
            for entry in journal["assets"]:
                path = Path(entry["path"])
                if path.suffix == ".sqlite" and path.exists():
                    _checkpoint(path)
        if restore or journal["state"] != "completed":
            _publish(journal_path, journal, {}, restore=restore or journal["state"] == "restoring")
            for directory in {Path(asset["path"]).parent for asset in journal["assets"]}:
                fsync_directory(directory)
        return {"state": journal["state"], "backup_root": journal["backup_root"], "journal": str(journal_path),
                "source_commit": journal["source_commit"], "discarded_trace_events": journal["discarded_trace_events"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate_session_history(args.workspace, restore=args.restore), ensure_ascii=False))
