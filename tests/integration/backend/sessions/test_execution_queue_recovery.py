"""真实进程退出后，准入/领取/启动/终态保持同一身份与 FIFO。"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
async def queue_process_case(request: pytest.FixtureRequest):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    # 单个文件内各崩溃窗口使用独立的正式工作区。
    root = context.workspace_root / request.node.name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return context, root


async def invoke(context, root: Path, mode: str, barrier: str, expected: int) -> str:
    command = [sys.executable, "-m", "tests.integration.backend.sessions._execution_queue_process", str(root), mode, barrier]
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 40)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    path = context.artifacts_dir / f"{barrier}-{mode}.json"
    path.write_text(json.dumps({"command": command, "exit_code": process.returncode, "output": output.decode()}, ensure_ascii=False, indent=2))
    assert process.returncode == expected, output.decode()
    return output.decode()


@pytest.mark.asyncio
async def test_live_holder_rejects_concurrent_runtime_then_recovers(queue_process_case):
    context, root = queue_process_case
    command = [sys.executable, "-m", "tests.integration.backend.sessions._execution_queue_process", str(root), "produce", "live_holder"]
    holder = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        async with asyncio.timeout(20):
            while not (root / "holder-ready").exists():
                assert holder.returncode is None
                await asyncio.sleep(0.02)
        output = await invoke(context, root, "recover", "live_holder", 1)
        assert "拒绝并存启动" in output
        assert not (root / "executed.jsonl").exists()
    finally:
        if holder.returncode is None:
            holder.kill()
        output, _ = await holder.communicate()
        (context.artifacts_dir / "live-holder-produce.json").write_text(json.dumps({
            "command": command, "exit_code": holder.returncode, "output": output.decode(),
        }, ensure_ascii=False, indent=2))
    await invoke(context, root, "recover_after_exit", "live_holder", 0)
    frozen = json.loads((root / "identities.json").read_text())
    executed = [json.loads(line) for line in (root / "executed.jsonl").read_text().splitlines()]
    assert [row["job_id"] for row in executed] == [job["job_id"] for job in frozen["jobs"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier,expected", [("acceptance", 81), ("claim", 82), ("task", 83), ("terminal", 84)])
async def test_durable_queue_recovers_original_job_after_process_exit(queue_process_case, barrier: str, expected: int):
    context, root = queue_process_case
    await invoke(context, root, "produce", barrier, expected)
    frozen = json.loads((root / "identities.json").read_text())
    sessions = root / "workspace/.boxteam/sessions"
    saver = RolloutCheckpointSaver(sessions_dir=sessions)
    before = saver.execution_queue_snapshot(frozen["session"], frozen["thread"])
    assert [row.cursor.admission.job_id for row in before.records] == [job["job_id"] for job in frozen["jobs"]][1 if barrier == "terminal" else 0:]
    if barrier in {"claim", "task"}:
        assert before.records[0].claim_generation == 1
        assert before.records[0].phase == ("claimed" if barrier == "claim" else "started")
    await invoke(context, root, "recover", barrier, 0)
    # 第二次冷启动不得把终态或旧 pending 引用重新入队。
    await invoke(context, root, "again", barrier, 0)
    executed = [json.loads(line) for line in (root / "executed.jsonl").read_text().splitlines()]
    assert executed == [{"job_id": job["job_id"], "thread_id": frozen["thread"], "message": job["content"]} for job in frozen["jobs"]]
    cold = RolloutCheckpointSaver(sessions_dir=sessions)
    assert cold.execution_queue_snapshot(frozen["session"], frozen["thread"]).records == ()
    for job in frozen["jobs"]:
        admission = cold.execution_admission_for_job(frozen["session"], thread_id=frozen["thread"], job_id=job["job_id"])
        assert (admission.execution_id, admission.turn_id, admission.admission_idempotency_key) == (job["execution_id"], job["turn_id"], job["key"])
        assert cold.runtime_execution_outcome(frozen["session"], frozen["thread"], admission.execution_id) == "cancelled"
    assert not (get_session_path_resolver(sessions).resolve_session_node(frozen["session"]) / "pending_requests.json").exists()
