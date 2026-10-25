"""source SQLite reader 在 rollout fork 前后保持快照；独立 writer 可继续提交。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    fork_workspace as fork_workspace,  # noqa: PLC0414 - 当前正式路径独立工作区
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    protected_source as protected_source,  # noqa: PLC0414 - 不运行其它正式文件
)

SOURCE_SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"
TARGET_SESSION_ID = "ses_019b9187b87f78c78ef242bbd6043bfc"


def _linux_database_lock_records(index: Path) -> list[str]:
    inode = index.stat().st_ino
    return [
        line
        for line in Path("/proc/locks").read_text().splitlines()
        if len(line.split()) >= 6
        and line.split()[4] == str(os.getpid())
        and line.split()[5].split(":")[-1] == str(inode)
    ]


def _wal_checkpoint(index: Path) -> tuple[int, int, int]:
    """读取 WAL 截断是否被既有读快照阻塞的跨平台证据。"""
    connection = sqlite3.connect(index, timeout=0)
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None:
            raise RuntimeError(f"SQLite WAL checkpoint 未返回状态: {index}")
        busy, log_frames, checkpointed_frames = (int(value) for value in checkpoint)
        return busy, log_frames, checkpointed_frames
    finally:
        connection.close()


@pytest.fixture
def source_reader(protected_source, request):
    saver = protected_source[0]
    connection = sqlite3.connect(saver._storage.storage.index_path(SOURCE_SESSION_ID))
    if request.param:
        connection.execute("BEGIN")
    connection.execute("SELECT count(*) FROM context_plans").fetchone()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def lock_artifacts(request):
    context = TestRunContext.from_test_file(Path(request.node.path))
    run_directory = context.runtime_root_for_node(request.node.nodeid).parent
    artifacts = context.artifacts_dir / run_directory.name
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts


@pytest.fixture
def forbid_parent_sqlite_copy(protected_source, monkeypatch):
    source_root = protected_source[0]._storage.storage.root(SOURCE_SESSION_ID)
    original = shutil.copyfile

    def checked_copyfile(source, destination, *, follow_symlinks=True):
        path = Path(source)
        if path.parent == source_root and path.name in {
            "index.sqlite",
            "index.sqlite-wal",
            "index.sqlite-journal",
            "index.sqlite-shm",
        }:
            pytest.fail("父进程不得通过 copyfile/copy2 打开 live SQLite 文件")
        return original(source, destination, follow_symlinks=follow_symlinks)

    # copy2/copytree 最终也调用此函数；独立解释器不继承本进程 monkeypatch。
    monkeypatch.setattr(shutil, "copyfile", checked_copyfile)


_WRITER = """
import sys
from pathlib import Path
from app.domain.itemized.plans.request import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from tests.support.session_catalog_owner import fixture_catalog_scope

with fixture_catalog_scope(), RolloutCheckpointSaver(Path(sys.argv[1])) as saver:
    session = sys.argv[2]
    thread = saver._storage.storage.main_thread_id(session)
    saver.create_context_plan(session, ContextRequestPlan(
        session_id=session, thread_id=thread, plan_id='independent-writer', refs=(),
        plan_creation_idempotency_key='writer-after-fork',
    ))
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_reader",
    [
        pytest.param(
            False,
            id="idle-reader",
            marks=pytest.mark.skipif(
                sys.platform != "linux",
                reason="空闲连接的内核锁证据仅在 Linux /proc 可直接观测",
            ),
        ),
        pytest.param(True, id="transaction-reader"),
    ],
    indirect=True,
)
@pytest.mark.parametrize("operation", ["preflight", "full-copy"])
async def test_fork_preserves_live_reader_and_independent_writer(
    protected_source,
    source_reader,
    lock_artifacts,
    forbid_parent_sqlite_copy,
    fork_deadlines,
    operation,
):
    saver = protected_source[0]
    index = saver._storage.storage.index_path(SOURCE_SESSION_ID)
    has_read_transaction = source_reader.in_transaction
    lock_records_before_fork = (
        _linux_database_lock_records(index) if sys.platform == "linux" else []
    )
    if sys.platform == "linux":
        assert lock_records_before_fork, (
            "合法 source reader 必须持有 SQLite database SHARED lock"
        )
    else:
        assert has_read_transaction, (
            "非 Linux 平台只运行可由 SQLite checkpoint 验证的显式读事务"
        )
    initial_count = source_reader.execute(
        "SELECT count(*) FROM context_plans"
    ).fetchone()[0]
    if operation == "preflight":
        await saver.forks.source.preflight_fork(source_session_id=SOURCE_SESSION_ID, mode="full_rollout_copy")
    else:
        await copy_rollout_snapshot(saver, SOURCE_SESSION_ID, TARGET_SESSION_ID, fork_deadlines=fork_deadlines)
    lock_records_after_fork = (
        _linux_database_lock_records(index) if sys.platform == "linux" else []
    )
    if sys.platform == "linux":
        assert lock_records_after_fork, (
            "fork 不得因父进程 open/close 原始 DB 而释放其它 SQLite reader 的锁"
        )
    assert source_reader.in_transaction == has_read_transaction
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", _WRITER, str(saver._storage.storage.sessions_dir), SOURCE_SESSION_ID],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    checkpoint = _wal_checkpoint(index) if has_read_transaction else None
    evidence = {
        "parent_pid": os.getpid(),
        "operation": operation,
        "transaction_reader": has_read_transaction,
        "locks_before": lock_records_before_fork,
        "locks_after_fork": lock_records_after_fork,
        "wal_checkpoint_after_writer": checkpoint,
        "writer_exit_code": result.returncode,
        "writer_stderr": result.stderr,
    }
    artifact = lock_artifacts / f"{operation}-{has_read_transaction}.json"
    artifact.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert result.returncode == 0, f"独立 writer 失败，证据: {artifact}"
    if checkpoint is not None:
        assert checkpoint[0] == 1, (
            "活动 SQLite reader 必须阻止 WAL 截断 checkpoint；"
            f"observed={checkpoint}, evidence={artifact}"
        )
    if has_read_transaction:
        assert (
            source_reader.execute("SELECT count(*) FROM context_plans").fetchone()[0]
            == initial_count
        )
        source_reader.commit()
    assert (
        source_reader.execute("SELECT count(*) FROM context_plans").fetchone()[0]
        == initial_count + 1
    )
    assert source_reader.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert source_reader.execute("PRAGMA foreign_key_check").fetchall() == []
    if operation == "full-copy":
        with saver._storage.storage._connect(
            saver._storage.storage.main_thread_id(TARGET_SESSION_ID), "",
            session_id=TARGET_SESSION_ID, read_only=True,
        ) as target:
            assert target.execute(
                "SELECT count(*) FROM context_plans WHERE plan_id='independent-writer'"
            ).fetchone() == (0,)
            assert target.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
