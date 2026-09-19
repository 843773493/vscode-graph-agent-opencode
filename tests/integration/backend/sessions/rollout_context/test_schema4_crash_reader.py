"""独立验证 SQLite 读连接、storage 连接打开与硬退出后的锁/SHM 合同。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.itemized.refs import ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    seal_input_hash,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.harness.python.run_context import TestRunContext


@dataclass(frozen=True)
class ReaderCase:
    saver: RolloutCheckpointSaver
    session: str
    accepted: dict[str, object]
    index: Path
    context: TestRunContext


@pytest.fixture
def reader_case(
    request: pytest.FixtureRequest,
    session_bundle_factory: Callable[[Path, str], Path],
) -> ReaderCase:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session = f"ses_{uuid4().hex}"
    session_bundle_factory(sessions, session)
    saver = RolloutCheckpointSaver(sessions)
    accepted = saver.accept_turn(
        session,
        accepted_ingress_id="reader-ingress",
        acceptance_idempotency_key="reader-acceptance",
        payload="保留合法读连接",
    )
    return ReaderCase(
        saver, session, accepted, saver._storage.index_path(session), context
    )


@pytest.fixture
def idle_reader(reader_case: ReaderCase) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(reader_case.index)
    try:
        assert connection.execute(
            "SELECT schema_version FROM database_meta"
        ).fetchone() == (4,)
        assert not connection.in_transaction
        yield connection
    finally:
        connection.close()


def _kernel_locks(path: Path) -> list[str]:
    inode = path.stat().st_ino
    return [
        line
        for line in Path("/proc/locks").read_text().splitlines()
        if len(line.split()) >= 6
        and line.split()[4] == str(os.getpid())
        and line.split()[5].split(":")[-1] == str(inode)
    ]


def _evidence(case: ReaderCase) -> dict[str, object]:
    paths = [case.index, Path(str(case.index) + "-wal"), Path(str(case.index) + "-shm")]
    files = {
        path.name: {"inode": path.stat().st_ino, "size": path.stat().st_size}
        for path in paths
        if path.exists()
    }
    descriptors = {}
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = fd.readlink()
        except FileNotFoundError:
            continue
        if str(case.index) in str(target):
            descriptors[fd.name] = {"target": str(target), "inode": fd.stat().st_ino}
    return {"files": files, "fds": descriptors, "db_locks": _kernel_locks(case.index)}


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc POSIX 锁诊断")
@pytest.mark.parametrize("opener", ["sqlite", "storage"])
@pytest.mark.parametrize("read_only", [False, True])
def test_opening_connection_preserves_idle_readers_kernel_lock(
    reader_case,
    idle_reader,
    opener,
    read_only,
):
    case = reader_case
    before = _evidence(case)
    assert before["db_locks"], "已执行 SELECT 的 WAL 连接必须持有 database SHARED lock"
    if opener == "storage":
        extra = case.saver._storage._connect(case.session, read_only=read_only)
    else:
        extra = (
            sqlite3.connect(f"{case.index.as_uri()}?mode=ro", uri=True)
            if read_only
            else sqlite3.connect(case.index)
        )
        extra.execute("SELECT schema_version FROM database_meta").fetchone()
    extra.close()
    after = _evidence(case)
    artifact = case.context.artifacts_dir / f"{case.session}.locks.json"
    artifact.write_text(
        json.dumps(
            {
                "parent_pid": os.getpid(),
                "opener": opener,
                "read_only": read_only,
                "before": before,
                "after": after,
            },
            indent=2,
        )
    )
    assert idle_reader.execute(
        "SELECT schema_version FROM database_meta"
    ).fetchone() == (4,)
    assert not idle_reader.in_transaction
    assert after["db_locks"], f"{opener} 丢失另一合法连接的内核锁: {artifact}"


_WRITER = """
import os
import sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.runtime.composer import ContextPlanComposer
saver = RolloutCheckpointSaver(sys.argv[1])
session = sys.argv[2]
draft = saver.get_context_plan_registration(session, plan_id='reader-plan').draft
snapshot = ContextPlanComposer().assembly(
    plan=draft, assembly_id='reader-assembly', session_id=session,
    turn_id=sys.argv[3], execution_id=sys.argv[4], provider_version='reader-contract',
)
commit = saver._storage._commit_connection
def hard_exit(connection):
    assert connection.execute('SELECT plan_state FROM context_plans').fetchone() == ('sealed',)
    if sys.argv[5] == 'after_commit':
        commit(connection)
    os._exit(66)
saver._storage._commit_connection = hard_exit
saver._storage.seal_context_assembly(
    snapshot, seal_idempotency_key='reader-seal', seal_input_hash=sys.argv[6],
)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc POSIX 锁诊断")
@pytest.mark.parametrize("keep_reader", [False, True])
@pytest.mark.parametrize("crash_point", ["before_commit", "after_commit"])
def test_crash_recovery_preserves_commit_with_independent_idle_reader(
    reader_case,
    idle_reader,
    keep_reader,
    crash_point,
):
    case = reader_case
    plan = ContextRequestPlan(
        session_id=case.session,
        plan_id="reader-plan",
        refs=(),
        plan_creation_idempotency_key="reader-create",
        tool_set_refs=(
            ToolSetRef.from_tool_snapshot(
                session_id=case.session,
                plan_id="reader-plan",
                snapshot_id="reader-tools",
                source_revision="reader-v1",
                tools=({"name": "read_file"},),
            ),
        ),
    )
    evidence = {
        "sqlite_version": sqlite3.sqlite_version,
        "parent_pid": os.getpid(),
        "keep_reader": keep_reader,
        "crash_point": crash_point,
        "before_create": _evidence(case),
    }
    original_jsonl = case.saver._storage.jsonl_path(case.session).read_bytes()
    case.saver.create_context_plan(case.session, plan)
    assembly_options = {
        "turn_id": str(case.accepted["turn_id"]),
        "execution_id": str(case.accepted["initial_execution_id"]),
        "provider_version": "reader-contract",
        "model_call_id": None,
        "target_format": "unknown",
        "omitted_ref_ids": (),
        "loss": (),
    }
    input_hash = seal_input_hash(plan, "", assembly_options, None)
    evidence["after_create"] = _evidence(case)
    if not keep_reader:
        idle_reader.close()
    evidence["before_child"] = _evidence(case)
    artifact = case.context.artifacts_dir / f"{case.session}.crash.json"
    try:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                _WRITER,
                str(case.saver._storage.sessions_dir),
                case.session,
                str(case.accepted["turn_id"]),
                str(case.accepted["initial_execution_id"]),
                crash_point,
                input_hash,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        evidence["child"] = {
            "exit_code": child.returncode,
            "stdout": child.stdout,
            "stderr": child.stderr,
        }
        assert child.returncode == 66, child.stdout + child.stderr
        evidence["after_child"] = _evidence(case)
        restarted = RolloutCheckpointSaver(case.saver._storage.sessions_dir)
        registration = restarted.get_context_plan_registration(
            case.session, plan_id=plan.plan_id
        )
        evidence["restored_state"] = registration.plan_state
        evidence["after_restart"] = _evidence(case)
        assert registration.plan_state == (
            "sealed" if crash_point == "after_commit" else "unsealed"
        )
        snapshot = ContextPlanComposer().assembly(
            plan=plan,
            assembly_id="reader-assembly",
            session_id=case.session,
            turn_id=str(case.accepted["turn_id"]),
            execution_id=str(case.accepted["initial_execution_id"]),
            provider_version="reader-contract",
        )
        restarted._storage.seal_context_assembly(
            snapshot,
            seal_idempotency_key="reader-seal",
            seal_input_hash=input_hash,
        )
        with restarted._storage._connect(case.session, read_only=True) as final:
            assert final.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert final.execute(
                "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'"
            ).fetchone() == (1,)
        assert (
            case.saver._storage.jsonl_path(case.session).read_bytes() == original_jsonl
        )
    finally:
        artifact.write_text(json.dumps(evidence, indent=2))
