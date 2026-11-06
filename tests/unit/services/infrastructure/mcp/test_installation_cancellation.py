"""独立取消、共享执行、发布竞争与原进程退出结算。"""

# pytest 按名称注入复用 fixture，测试参数须与导入同名。
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import psutil
import pytest

from app.core import child_process
from app.domain.mcp_candidates import McpCandidateConflict
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.services.infrastructure.mcp.installation.python import _run
from app.services.infrastructure.mcp.installation.store import (
    InstallationCancelled,
    InstallationStore,
)
from tests.unit.services.infrastructure.mcp.test_installation import (  # noqa: F401 - 显式复用 pytest fixture，避免复制装具。
    artifact_key_value,
    binding,
    installation_intent,
    installation_store,
    operation,
    prepared_artifact,
)


@pytest.mark.parametrize("cancel_producer", [True, False])
def test_cancel_one_consumer_keeps_original_shared_execution(installation_store, operation,
    prepared_artifact, artifact_key_value, cancel_producer):
    artifact, _ = prepared_artifact
    follower = operation.model_copy(update={"operation_id": "other", "intent_digest": "other-intent", "created_at": 2})
    assert installation_store.reserve(follower, key=artifact_key_value).preparation is None
    cancelled, survivor = (operation, follower) if cancel_producer else (follower, operation)
    result = installation_store.request_cancel(cancelled.operation_id)
    assert result.status == "cancelled" and result.cancellation_requested and result.artifact is None
    assert installation_store.request_cancel(cancelled.operation_id) == result
    assert not installation_store.cancellation_pending(operation.operation_id)
    progress = installation_store.update(operation.model_copy(update={"stage": "verify", "log": "原共享执行继续\n"}))
    assert progress.status == "running" and progress.stage == "verify"
    installation_store.publish(progress, artifact)
    assert installation_store.operation(survivor.operation_id).status == "succeeded"
    assert installation_store.operation(survivor.operation_id).artifact == artifact
    assert installation_store.operation(cancelled.operation_id) == result
    assert installation_store.artifacts() == [artifact]


def test_last_cancel_fences_publication_and_new_join_until_exit(installation_store, operation,
    prepared_artifact, artifact_key_value):
    artifact, _ = prepared_artifact
    result = installation_store.request_cancel(operation.operation_id)
    assert result.status == "running" and result.cancellation_requested
    assert installation_store.cancellation_pending(operation.operation_id)
    with pytest.raises(InstallationCancelled):
        installation_store.publish(operation, artifact)
    with pytest.raises(McpCandidateConflict, match="正在取消"):
        installation_store.reserve(operation.model_copy(update={"operation_id": "late-join"}), key=artifact_key_value)
    assert installation_store.artifacts() == []
    assert installation_store.operation(operation.operation_id) == result


def test_publication_committed_first_remains_success(installation_store, operation, prepared_artifact):
    artifact, _ = prepared_artifact
    installation_store.publish(operation, artifact)
    succeeded = installation_store.operation(operation.operation_id)
    assert installation_store.request_cancel(operation.operation_id) == succeeded
    assert succeeded.status == "succeeded" and not succeeded.cancellation_requested
    assert installation_store.artifacts() == [artifact]


@pytest.fixture
async def running_installation(tmp_path, monkeypatch, installation_intent):
    request, plan, parameters = installation_intent
    started, cleanup_entered, release_cleanup = (asyncio.Event() for _ in range(3))
    roots = []
    original_close = child_process.OwnedChildProcess.close
    monkeypatch.setattr(child_process, "_EXIT_WAIT_SECONDS", 0.05)

    async def close(owned):
        cleanup_entered.set()
        await release_cleanup.wait()
        await original_close(owned)

    async def prepare(root, *, name, version, index, entrypoint, progress, execution_directory):
        root.mkdir(parents=True)
        roots.append(root)
        started.set()
        await _run([sys.executable, "-c", "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); print('stdout',flush=True); print('stderr',file=sys.stderr,flush=True); time.sleep(60)",
            str(root / "pid")], directory=execution_directory, progress=progress, stage="install")
        pytest.fail("取消后的真实子进程不得进入发布")

    monkeypatch.setattr(child_process.OwnedChildProcess, "close", close)
    monkeypatch.setattr("app.services.infrastructure.mcp.installation.owner.prepare_python_artifact", prepare)
    owner = McpInstallationOwner(home=tmp_path / "home", backend_id=request.candidate.target.backend_id)
    try:
        await owner.install(request, plan=plan, parameters=parameters)
        await asyncio.wait_for(started.wait(), 3)
        for _ in range(100):
            if (roots[0] / "pid").is_file():
                break
            await asyncio.sleep(0.02)
        pid = int((roots[0] / "pid").read_text())
        yield owner, request, pid, cleanup_entered, release_cleanup
    finally:
        release_cleanup.set()
        await owner.cancel(request.operation_id)
        await owner.close()


async def test_last_cancel_waits_for_actual_process_exit(running_installation):
    owner, request, pid, cleanup_entered, release_cleanup = running_installation
    process = psutil.Process(pid)
    result = await owner.cancel(request.operation_id)
    assert result.status == "running" and result.cancellation_requested
    await asyncio.wait_for(cleanup_entered.wait(), 3)
    assert process.is_running() and owner.operation(request.operation_id).status == "running"
    release_cleanup.set()
    await owner.close()
    assert not psutil.pid_exists(pid)
    result = owner.operation(request.operation_id)
    assert result.status == "cancelled" and result.cancellation_requested and result.error is None
    assert "stdout" in result.log and "stderr" in result.log
    assert result.artifact is None and owner.artifacts() == []


async def test_cleanup_failure_is_failed_not_cancelled(running_installation, monkeypatch):
    owner, request, pid, cleanup_entered, release_cleanup = running_installation
    original_close = child_process.OwnedChildProcess.close

    async def fail_after_exit(owned):
        await original_close(owned)
        raise OSError("退出核验后的清理失败")

    monkeypatch.setattr(child_process.OwnedChildProcess, "close", fail_after_exit)
    await owner.cancel(request.operation_id)
    await asyncio.wait_for(cleanup_entered.wait(), 3)
    release_cleanup.set()
    await owner.close()
    result = owner.operation(request.operation_id)
    assert result.status == "failed" and "清理失败" in result.error
    assert not psutil.pid_exists(pid) and result.artifact is None


async def test_tool_output_merges_stdout_and_stderr(tmp_path):
    progress = []

    async def report(stage, output):
        progress.append((stage, output))

    output = await _run([sys.executable, "-c", "import sys; print('original-out',flush=True); print('original-error',file=sys.stderr,flush=True)"],
        directory=tmp_path, progress=report, stage="verify")
    assert "original-out" in output and "original-error" in output
    assert progress == [("verify", ""), ("verify", output)]


@pytest.mark.parametrize("live", [False, True])
def test_v3_migration_preserves_terminal_and_rejects_live_writer(installation_store, operation,
    prepared_artifact, tmp_path, live):
    artifact, _ = prepared_artifact
    if not live:
        installation_store.publish(operation, artifact)
    database = tmp_path / "home/artifacts/mcp" / artifact.binding.backend_id / "installations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE operation_owners DROP COLUMN execution_payload")
        connection.execute("PRAGMA user_version=3")
    if live:
        with pytest.raises(McpCandidateConflict, match="活进程安装"):
            InstallationStore(database.parent)
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (3,)
            assert "execution_payload" not in [row[1] for row in connection.execute("PRAGMA table_info(operation_owners)")]
    else:
        restored = InstallationStore(database.parent)
        assert restored.operation(operation.operation_id).status == "succeeded"
        assert restored.artifacts() == [artifact]
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (4,)


async def test_other_process_records_cancellation_in_original_sqlite(installation_store, operation,
    prepared_artifact, tmp_path):
    artifact, _ = prepared_artifact
    root = tmp_path / "home/artifacts/mcp" / artifact.binding.backend_id

    async def progress(stage, output):
        pass

    output = await _run([sys.executable, "-c", "import pathlib,sys; from app.services.infrastructure.mcp.installation.store import InstallationStore; print(InstallationStore(pathlib.Path(sys.argv[1])).request_cancel(sys.argv[2]).model_dump_json())",
        str(root), operation.operation_id], directory=Path.cwd(), progress=progress, stage="verify")
    receipt = json.loads(output)
    assert receipt["status"] == "running" and receipt["cancellation_requested"]
    assert installation_store.cancellation_pending(operation.operation_id)
    assert installation_store.operation(operation.operation_id).model_dump() == receipt
    assert installation_store.artifacts() == []


def test_cancelled_producer_stays_cancelled_when_shared_executor_crashes(installation_store, operation,
    prepared_artifact, artifact_key_value, tmp_path):
    artifact, _ = prepared_artifact
    follower = operation.model_copy(update={"operation_id": "waiting", "intent_digest": "waiting-intent"})
    installation_store.reserve(follower, key=artifact_key_value)
    cancelled = installation_store.request_cancel(operation.operation_id)
    assert cancelled.status == "cancelled"
    database = tmp_path / "home/artifacts/mcp" / artifact.binding.backend_id / "installations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE operation_owners SET pid=2147483647, started=0")
    assert installation_store.operation(operation.operation_id) == cancelled
    interrupted = installation_store.operation(follower.operation_id)
    assert interrupted.status == "interrupted" and "原安装进程已退出" in interrupted.error
    assert not interrupted.cancellation_requested and interrupted.artifact is None
    next_intent = operation.model_copy(update={"operation_id": "explicit-retry"})
    assert installation_store.reserve(next_intent, key=artifact_key_value).preparation is not None
    assert installation_store.artifacts() == []


def test_cancel_pending_then_executor_exit_does_not_claim_verified_cancellation(installation_store, operation,
    prepared_artifact, tmp_path):
    artifact, _ = prepared_artifact
    assert installation_store.request_cancel(operation.operation_id).status == "running"
    database = tmp_path / "home/artifacts/mcp" / artifact.binding.backend_id / "installations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE operation_owners SET pid=2147483647, started=0")
    interrupted = installation_store.operation(operation.operation_id)
    assert interrupted.status == "interrupted" and interrupted.cancellation_requested
    assert "原安装进程已退出" in interrupted.error and interrupted.artifact is None
