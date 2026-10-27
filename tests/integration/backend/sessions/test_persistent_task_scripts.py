"""经任务API启动真实脚本，验证等待、迟到判断及Backend硬重启。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from xml.etree import ElementTree

import psutil
import pytest

from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
    _check,
    _configuration,
    _control,
    _create,
    _release_match,
    _run,
    _target,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    RuntimeSourceCase,
    _job,
    _send,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离
)

SCRIPT_INPUT = """
import json, os, pathlib, sys, time
value = json.loads(sys.stdin.buffer.read().decode('utf-8'))
with pathlib.Path('checks.jsonl').open('a', encoding='utf-8') as output:
    output.write(json.dumps({'input':value,'cwd':os.getcwd(),'pid':os.getpid()})+'\\n')
"""


def _script_configuration(target: str, program: str, *, timeout: float = 5, limit: int = 65536,
                          mode: str = "context_only") -> dict:
    configuration = _configuration(target)
    configuration["action"]["delivery_mode"] = mode
    configuration["condition"] = {"type_id": "builtin.script", "version": "1", "script": {
        "command": [sys.executable, "-c", SCRIPT_INPUT + program], "timeout_seconds": timeout,
        "output_limit_bytes": limit, "memory_limit_mb": 128,
    }}
    return configuration


async def _marker(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


def _inputs(workspace: Path) -> list[dict]:
    return [json.loads(line) for line in (workspace / "checks.jsonl").read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_script_waits_for_real_service_then_passes_result_to_work_model(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    malformed = _script_configuration(target, "print('{}')")
    malformed["condition"]["script"]["timeout_seconds"] = 0
    response = await case.client.post(ROOT, json={"idempotency_key": "invalid-timeout", "configuration": malformed})
    assert response.status_code == 422, response.text
    with socket.socket() as service:
        service.bind(("127.0.0.1", 0))
        port = service.getsockname()[1]
        program = f"""
import socket
try:
    connection = socket.create_connection(('127.0.0.1',{port}), timeout=0.2)
except (ConnectionRefusedError, TimeoutError):
    decision, ready = 'wait', False
else:
    connection.close()
    decision, ready = 'proceed', True
print(json.dumps({{'decision':decision,'reason':'service-probe','result':{{'service_ready':ready,'probe_id':'script-result-original'}}}}))
"""
        configuration = _script_configuration(target, program, mode="context_and_run")
        configuration["policies"]["completion"] = "after_delivery"
        task = await _create(case, configuration)
        for index in range(2):
            run = await _check(case, task, f"not-ready-{index}")
            actual = await _run(case, task, run, "checked")
            assert actual["decision"]["decision"] == "wait" and actual["decision"]["result"]["service_ready"] is False
            assert actual["delivery"] is None
        assert case.call_count(session_id, thread_id) == 0 and case.requests() == []
        service.listen(8)
        call, = await case.plan(session_id, [{"content": "已读取服务探测结果。"}])
        run = await _check(case, task, "ready")
        actual = await _run(case, task, run)
        await _job(case, actual["delivery"]["job_id"])
        message, = [message["content"] for message in case.request(call)["messages"]
                    if isinstance(message["content"], str) and "script-result-original" in message["content"]]
        body = ElementTree.fromstring(message).findtext("untrusted_instructions")
        assert body is not None
        assert json.loads(body.strip().splitlines()[-1])["condition_decision"] == actual["decision"]
        assert actual["decision"]["result"] == {"service_ready": True, "probe_id": "script-result-original"}
        assert case.call_count(session_id, thread_id) == 1
        assert await _check(case, task, "ready") == actual
    recorded = _inputs(case.workspace)
    assert len(recorded) == 3 and {row["cwd"] for row in recorded} == {str(case.workspace)}
    assert recorded[-1]["input"]["run_id"] == run["run_id"]
    assert recorded[-1]["input"]["context"]["content"] == configuration["context"]["content"]
    assert recorded[-1]["input"]["trigger"] == {"key": "ready", "type": "manual", "scheduled_for": None}
    assert (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]["lifecycle"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("management", ["pause", "edit", "terminate"])
async def test_late_script_result_cannot_deliver_after_management_changes(source_case: RuntimeSourceCase, management: str) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    program = """
pathlib.Path('started').touch()
while not pathlib.Path('release').exists():
    time.sleep(0.01)
print(json.dumps({'decision':'proceed','reason':'late-result','result':{'content':value['context']['content']}}))
"""
    task = await _create(case, _script_configuration(target, program))
    run = await _check(case, task)
    await _marker(case.workspace / "started")
    try:
        if management in {"pause", "terminate"}:
            changed = await asyncio.wait_for(_control(case, task, management), 1)
        else:
            configuration = task["configuration"]
            configuration["context"]["content"] = "new-context"
            response = await asyncio.wait_for(case.client.put(f"{ROOT}/{task['task_id']}", json={
                "expected_revision": task["revision"], "configuration": configuration,
            }), 1)
            assert response.status_code == 200, response.text
            changed = response.json()["data"]
    finally:
        (case.workspace / "release").touch()
    actual = await _run(case, task, run, "suppressed")
    assert actual["decision"]["result"]["content"].startswith("task-original-private")
    assert actual["delivery"] is None and case.call_count(session_id, thread_id) == 0
    current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
    assert current["revision"] == changed["revision"] and current["lifecycle"] == changed["lifecycle"]


@pytest.mark.asyncio
async def test_script_dry_run_keeps_all_decisions_without_work_or_lifecycle_changes(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    for decision in ["wait", "proceed", "complete", "needs_attention"]:
        program = f"print(json.dumps({{'decision':{decision!r},'reason':'trial','result':{{'input_trial':value['check_only']}}}}))"
        task = await _create(case, _script_configuration(target, program), decision)
        paused = await _control(case, task, "pause")
        run = await _check(case, paused, check_only=True)
        actual = await _run(case, paused, run, "checked")
        assert actual["decision"] == {"decision": decision, "reason": "trial", "result": {"input_trial": True}}
        assert actual["delivery"] is None
        current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
        assert current["lifecycle"] == "paused" and current["revision"] == paused["revision"]
    assert len(_inputs(case.workspace)) == 4 and case.call_count(session_id, thread_id) == 0
    for decision in ["complete", "needs_attention"]:
        program = f"print(json.dumps({{'decision':{decision!r},'reason':'terminal-decision'}}))"
        task = await _create(case, _script_configuration(target, program), "live-" + decision)
        await _run(case, task, await _check(case, task), "checked")
        current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
        assert current["lifecycle"] == decision and current["reason"] == "terminal-decision"
    assert case.call_count(session_id, thread_id) == 0


@pytest.mark.asyncio
async def test_script_failures_are_visible_and_do_not_disable_other_tasks(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    programs = [
        ("raise SystemExit(7)", "exit_code=7", 5, 65536),
        ("print('invalid-json')", "输出不符合判断协议", 5, 65536),
        ("print(json.dumps({'decision':'unknown','reason':'bad'}))", "输出不符合判断协议", 5, 65536),
        ("print('x'*200000)", "输出超限", 5, 1024),
        ("print('x'*200000,file=sys.stderr)", "输出超限", 5, 1024),
        ("time.sleep(45)", "exit_code=124", 0.3, 65536),
    ]
    for index, (program, error, timeout, limit) in enumerate(programs):
        task = await _create(case, _script_configuration(target, program, timeout=timeout, limit=limit), f"failure-{index}")
        if index == 0:
            trial = await _check(case, task, "failing-trial", check_only=True)
            trial = await _run(case, task, trial, "failed")
            assert error in trial["error"] and trial["delivery"] is None
            current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
            assert current["lifecycle"] == "active" and current["revision"] == task["revision"]
        run = await _check(case, task)
        actual = await _run(case, task, run, "failed")
        assert error in actual["error"] and actual["decision"] is None and actual["delivery"] is None
        current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
        assert current["lifecycle"] == "needs_attention" and current["reason"] == actual["error"]
    healthy = await _create(case, _script_configuration(target, "print(json.dumps({'decision':'proceed','reason':'healthy'}))"), "healthy")
    await _run(case, healthy, await _check(case, healthy))
    assert case.call_count(session_id, thread_id) == 0
    assert (await case.client.get("/api/v1/health")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("check_only", [False, True])
async def test_checking_hard_restart_reports_unknown_without_replaying_script(
    restart_source_case: RestartRuntimeSourceCase, check_only: bool,
) -> None:
    case = restart_source_case
    _session_id, _thread_id, target = await _target(case)
    program = """
import subprocess
child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)'])
pathlib.Path('processes.json').write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(45)
"""
    task = await _create(case, _script_configuration(target, program, timeout=1))
    run = await _check(case, task, check_only=check_only)
    await _marker(case.workspace / "processes.json")
    await case.restart(hard=True)
    actual = await _run(case, task, run, "failed")
    assert "结果未知" in actual["error"] and actual["delivery"] is None
    assert len(_inputs(case.workspace)) == 1
    current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
    assert current["lifecycle"] == ("active" if check_only else "needs_attention")
    assert current["revision"] == task["revision"] + (0 if check_only else 1)
    for pid in json.loads((case.workspace / "processes.json").read_text()):
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["task.check.decided_before_delivery", "task.action.accepted_before_record"])
async def test_decided_script_restarts_from_original_result_and_source(restart_source_case: RestartRuntimeSourceCase, phase: str) -> None:
    case = restart_source_case
    session_id, thread_id, target = await _target(case)
    program = "print(json.dumps({'decision':'proceed','reason':'frozen-result','result':{'marker':pathlib.Path('result.txt').read_text()}}))"
    (case.workspace / "result.txt").write_text("script-original-result")
    task = await _create(case, _script_configuration(target, program))
    case.control.arm_match("script-before-restart", phase, {"task_id": task["task_id"]})
    run = await _check(case, task)
    barrier = await asyncio.to_thread(case.control.acknowledged_match, "script-before-restart")
    (case.workspace / "result.txt").write_text("script-changed-result")
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "script-before-restart"))
    actual = await _run(case, task, run)
    assert actual["request_id"] == run["request_id"] and actual["definition_revision"] == run["definition_revision"]
    if phase == "task.action.accepted_before_record":
        assert actual["delivery"]["item_id"] == barrier["details"]["item_id"]
    assert actual["decision"]["result"] == {"marker": "script-original-result"}
    assert len(_inputs(case.workspace)) == 1
    call, = await case.plan(session_id, [{"content": "读取已保存的脚本结果。"}])
    accepted = await _send(case, session_id, "读取脚本事件。")
    await _job(case, accepted["job_id"])
    wire = _wire_text(case.request(call)["messages"])
    assert wire.count("script-original-result") == 1 and "script-changed-result" not in wire
    assert case.call_count(session_id, thread_id) == 1
