"""保留 source SQLite reader 时 fork 不释放其 POSIX 锁；独立 writer 可继续提交。"""

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
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    fork_workspace as fork_workspace,  # noqa: PLC0414 - 当前正式路径独立工作区
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    protected_key as protected_key,  # noqa: PLC0414 - 显式注入 fixture key
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    protected_source as protected_source,  # noqa: PLC0414 - 不运行其它正式文件
)


def _locks(index: Path) -> list[str]:
    inode = index.stat().st_ino
    return [
        line
        for line in Path("/proc/locks").read_text().splitlines()
        if len(line.split()) >= 6
        and line.split()[4] == str(os.getpid())
        and line.split()[5].split(":")[-1] == str(inode)
    ]


@pytest.fixture
def source_reader(protected_source, request):
    saver = protected_source[0]
    connection = sqlite3.connect(saver._storage.index_path("source"))
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
    context.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return context.artifacts_dir


@pytest.fixture
def forbid_parent_sqlite_copy(protected_source, monkeypatch):
    source_root = protected_source[0]._storage.root("source")
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
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver

with RolloutCheckpointSaver(Path(sys.argv[1])) as saver:
    saver.create_context_plan('source', ContextRequestPlan(
        session_id='source', plan_id='independent-writer', refs=(),
        plan_creation_idempotency_key='writer-after-fork',
    ))
"""


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc POSIX 锁证据")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_reader",
    [False, True],
    indirect=True,
    ids=["idle-reader", "transaction-reader"],
)
@pytest.mark.parametrize("operation", ["preflight", "full-copy"])
async def test_fork_preserves_live_reader_and_independent_writer(
    protected_source,
    source_reader,
    lock_artifacts,
    forbid_parent_sqlite_copy,
    operation,
):
    saver = protected_source[0]
    index = saver._storage.index_path("source")
    before = _locks(index)
    assert before, "合法 source reader 必须持有 SQLite database SHARED lock"
    transaction_reader = source_reader.in_transaction
    initial_count = source_reader.execute(
        "SELECT count(*) FROM context_plans"
    ).fetchone()[0]
    if operation == "preflight":
        await saver.preflight_fork(source_session_id="source", mode="full_rollout_copy")
    else:
        await saver.afork(
            source_session_id="source",
            target_session_id="target",
            mode="full_rollout_copy",
        )
    after = _locks(index)
    assert after, "fork 不得因父进程 open/close 原始 DB 而释放其它 SQLite reader 的锁"
    assert source_reader.in_transaction == transaction_reader
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", _WRITER, str(saver._storage.sessions_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    evidence = {
        "parent_pid": os.getpid(),
        "operation": operation,
        "transaction_reader": transaction_reader,
        "locks_before": before,
        "locks_after_fork": after,
        "writer_exit_code": result.returncode,
        "writer_stderr": result.stderr,
    }
    artifact = lock_artifacts / f"{operation}-{transaction_reader}.json"
    artifact.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert result.returncode == 0, f"独立 writer 失败，证据: {artifact}"
    if transaction_reader:
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
        with saver._storage._connect("target", "", read_only=True) as target:
            assert target.execute(
                "SELECT count(*) FROM context_plans WHERE plan_id='independent-writer'"
            ).fetchone() == (0,)
            assert target.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
