"""真实产品历史读失败不能公开原生位置，恢复后保持原历史与请求。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.services.business.job.service import JobService
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 当前文件独立工作区、端口与产物。
)
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_native_history_read_fails_explicitly_then_recovers_unchanged(
    source_case: RuntimeSourceCase, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session_id, thread_id, _ = await _target(case)
    call, = await case.plan(session_id, [{"content": "历史错误修复后仍保持原回答。"}])
    accepted = await _send(case, session_id, "生成用于冷读错误边界核对的历史。")
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    await _job(case, job_id)
    assert isinstance(case.container.job_service, JobService)
    await case.container.job_service.drain_finalizations()
    root = case.container.session_service.path_resolver.resolve_thread_node(session_id, thread_id)
    canonical = root / "rollout/rollout.jsonl"
    original = canonical.read_bytes()
    index = root / "rollout/index.sqlite"
    logs = tuple(case.container.session_service.path_resolver.resolve_session_node(session_id).joinpath("logs/llm_requests").glob("*.json"))
    captures = {call: json.loads((case.model.artifacts / f"{call}.json").read_text(encoding="utf-8"))}
    audit = audit_sealed_requests(index, logs, captures)
    base = f"/api/v1/sessions/{session_id}"

    async def read_all() -> list[httpx.Response]:
        return [
            await case.client.get(base + "/bootstrap", params={"thread_id": thread_id}),
            await case.client.post(base + "/history", json={"direction": "tail", "turns": 10}),
            await case.client.post(base + f"/threads/{thread_id}/history", json={"direction": "tail", "turns": 10}),
        ]

    before = await read_all()
    assert all(response.status_code == 200 for response in before), [response.text for response in before]
    history = before[2].json()["data"]
    assert [turn["turn_id"] for turn in history["items"]] == [accepted["turn_id"]]
    assert before[1].json()["data"] == history
    parked = canonical.with_name("canonical-history-private-original")
    canonical.rename(parked)
    try:
        missing = await read_all()
        for response in missing:
            assert response.status_code == 500, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            detail = response.json()["detail"]
            assert detail["code"] == "turn_projection_corrupted" and detail["session_id"] == session_id
            assert "stage=history_read, error=RuntimeError" in detail["message"]
            assert str(case.workspace) not in response.text and "rollout.jsonl" not in response.text
        integrity = [record for record in caplog.records if record.exc_info
                     and isinstance(record.exc_info[1], RuntimeError)
                     and "recovery_required: rollout 权威文件缺失" in str(record.exc_info[1])]
        assert len(integrity) == 3
        assert not canonical.exists() and parked.read_bytes() == original
    finally:
        parked.rename(canonical)

    # 缺文件在布局校验已被拒绝；另在真正的正文 open 处触发原生 I/O，避免用
    # preflight RuntimeError 冒充原生读取验收。只替换文件访问目标，不替换历史 reader。
    unavailable = case.workspace / "history-private-missing" / "history-secret.sqlite"
    original_open = Path.open

    def fail_open(path: Path, mode: str = "r", buffering: int = -1,
                  encoding: str | None = None, errors: str | None = None, newline: str | None = None):
        target = unavailable if path == canonical and mode == "rb" else path
        return original_open(target, mode, buffering, encoding, errors, newline)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "open", fail_open)
        failed = await read_all()
        for response in failed:
            assert response.status_code == 500, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            detail = response.json()["detail"]
            assert detail["code"] == "turn_projection_corrupted" and detail["session_id"] == session_id
            assert "stage=history_read, error=FileNotFoundError, errno=2" in detail["message"]
            assert str(case.workspace) not in response.text and "history-secret" not in response.text
        diagnostics = [record for record in caplog.records if record.exc_info
                       and isinstance(record.exc_info[1], FileNotFoundError)
                       and record.exc_info[1].filename == str(unavailable)]
        assert len(diagnostics) == 3
        assert all(record.name == "app.core.failures" for record in diagnostics)
        assert str(unavailable) in caplog.text
    after = await read_all()
    assert all(response.status_code == 200 for response in after), [response.text for response in after]
    assert after[2].json()["data"] == history == after[1].json()["data"]
    assert after[0].json()["data"]["latest_turn"] == before[0].json()["data"]["latest_turn"]
    assert canonical.read_bytes() == original
    assert audit_sealed_requests(index, logs, captures) == audit
    assert len(case.requests()) == 1
    case.write_actual("history-failure", {
        "session_id": session_id, "thread_id": thread_id, "turn_id": accepted["turn_id"],
        "canonical_sha256": hashlib.sha256(original).hexdigest(),
        "missing_errors": [response.json() for response in missing],
        "errors": [response.json() for response in failed],
        "history": history, "sealed_request_audit": audit,
    })
