"""通信准入失败保留原 inbox 恢复身份，诊断原文只留日志。"""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import pytest

from app.core.session_control_store import SessionControlStore
from app.services.orchestration.communication.inbox_admission_worker import (
    InboxAdmissionError,
    InboxAdmissionTarget,
    InboxExecutionBinding,
)
from tests.integration.backend.sessions.test_communication_observation_lifecycle import (
    _AdmissionProbe,
    _Case,
)
from tests.integration.backend.sessions.test_communication_observation_lifecycle import (
    admission_case as admission_case,  # noqa: PLC0414 — 复用真实准入与生命周期 owner
)
from tests.integration.backend.sessions.test_communication_observation_lifecycle import (
    lifecycle_case as lifecycle_case,  # noqa: PLC0414 — 为复用准入fixture提供依赖
)


@pytest.fixture(params=[
    "/private/admission/workspace", r"C:\private\admission\workspace",
    r"\\server\private\admission\workspace",
])
def native_path(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=["runtime", "os", "sqlite"])
def binding_failure(request: pytest.FixtureRequest, native_path: str) -> Exception:
    if request.param == "os":
        return OSError(errno.EACCES, "原生绑定失败", native_path)
    if request.param == "sqlite":
        error = sqlite3.OperationalError("原生数据库失败: " + native_path)
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        return error
    return RuntimeError("原生绑定失败: " + native_path)


@pytest.fixture(params=[None, RuntimeError, KeyError, OSError, sqlite3.OperationalError])
def record_failure(request: pytest.FixtureRequest, native_path: str) -> Exception | None:
    return request.param("失败记录存储错误: " + native_path) if request.param else None


async def test_send_failure_keeps_safe_diagnostics_and_original_inbox_restart(
    admission_case: tuple[_Case, _AdmissionProbe], binding_failure: Exception,
    record_failure: Exception | None, native_path: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case, _probe = admission_case
    calls: list[InboxAdmissionTarget] = []
    record_messages: list[str] = []

    async def fail_bind(target: InboxAdmissionTarget) -> InboxExecutionBinding:
        calls.append(target)
        raise binding_failure

    monkeypatch.setattr(case.service, "_bind", fail_bind)
    with monkeypatch.context() as record_patch:
        if record_failure is not None:
            def fail_record(_store, *args, **kwargs):
                record_messages.append(kwargs["last_error"])
                raise record_failure

            record_patch.setattr(SessionControlStore, "record_communication_inbox_admission_failure", fail_record)
        receipt = await case.send("cop_failed_binding_recovery")
        async with asyncio.timeout(5):
            while case.service._failure is None:
                await asyncio.sleep(0)
        with pytest.raises(RuntimeError) as stopped:
            await case.service.stop()
    assert isinstance(stopped.value.__cause__, InboxAdmissionError)
    assert stopped.value.__cause__ is case.service._failure
    public = str(stopped.value)
    detail = (
        f"inbox admission 失败: session_id={case.target}, communication_id={receipt.communication_id}, "
        f"stage=execution_binding, error_type={type(binding_failure).__name__}"
    )
    for name in ("errno", "winerror", "sqlite_errorcode"):
        if isinstance(value := getattr(binding_failure, name, None), int):
            detail += f", {name}={value}"
    assert "private" not in public and native_path not in public
    assert detail in public
    if record_failure is not None:
        assert f"record_error_type={type(record_failure).__name__}" in public
        assert record_messages == [detail]
    with closing(case.service._store(case.target)) as store:
        original = store.get_communication_inbox(receipt.communication_id)
        lease = store.find_lease_by_operation("communication-target|" + receipt.communication_id)
        assert original.state == "target_accepted" and lease.state == "settling"
        assert original.last_error == (detail if record_failure is None else None)
        assert "private" not in json.dumps(asdict(original))
    assert len(calls) == 1 and calls[0].inbox.communication_id == receipt.communication_id
    for error in (binding_failure, record_failure):
        if error is not None:
            assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    case.restore()
    recovered_probe = _AdmissionProbe(case.sessions)
    monkeypatch.setattr(case.service, "_bind", recovered_probe)
    await case.service.start()
    with closing(case.service._store(case.target)) as store:
        recovered = store.get_communication_inbox(receipt.communication_id)
        assert (recovered.admission_id, recovered.payload_hash, recovered.wakeup_key,
                recovered.target_thread_id, recovered.request_id, recovered.content) == (
            original.admission_id, original.payload_hash, original.wakeup_key,
            original.target_thread_id, original.request_id, original.content,
        )
        assert recovered.state == "execution_bound" and recovered.last_error is None
        assert recovered.admission_claim_generation > original.admission_claim_generation
        assert store.get_lease(lease.lease_id).state == "completed"
    assert list(recovered_probe.bindings) == [receipt.communication_id]
    binding = recovered_probe.bindings[receipt.communication_id]
    assert (recovered.job_id, recovered.execution_id, recovered.turn_id) == (
        binding.job_id, binding.execution_id, binding.turn_id,
    )
    assert len(calls) == 1
