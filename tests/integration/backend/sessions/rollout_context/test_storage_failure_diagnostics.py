"""消息入口生成历史，原生存储故障及日志次错后仍能冷读和继续执行。"""

from __future__ import annotations

import errno
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.atomic_fs import native_io_path
from app.core.path_utils import workspace_context
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_cold_rollout_history import (
    _history_request,
)
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 — 使用当前用例的独立工作区和端口。
)
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 — 日志写入实际产生 ENOTDIR。
)


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("logging_unavailable", [False, True])
@pytest.mark.parametrize("fault", ["index_inspection", "lock_open"])
async def test_public_history_keeps_original_failure_and_recovers(
    source_case: RuntimeSourceCase, diagnostic_sink: DiagnosticFailure,
    fault: str, logging_unavailable: bool,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    with pytest.raises(OSError) as expected_sink:
        diagnostic_sink.destination.write_text("目标平台日志失败原件", encoding="utf-8")
    sink_type = type(expected_sink.value).__name__
    session, thread, _ = await _target(case)
    (case.workspace / "storage-input.txt").write_text("冻结历史正文 Ω", encoding="utf-8")
    first_calls = await case.plan(session, [
        _tool_reply("read_file", {"path": "storage-input.txt"}, "storage-read"),
        {"content": "已读取原文件。"},
    ])
    accepted = await _send(case, session, "读取文件，形成可恢复的工具历史。")
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    await _job(case, job_id)
    storage = case.container.checkpointer._storage.storage
    with workspace_context(case.container.workspace_id, case.workspace, session_catalog=case.container.session_catalog):
        canonical = storage.jsonl_path(session, thread_id=thread)
        original_bytes = canonical.read_bytes()
        original_items = tuple(storage.read_items(session, thread))
        index = storage.index_path(session, thread_id=thread)
        lock = storage.read_lock(session, thread)._path
        path = index if fault == "index_inspection" else lock
        failure = PermissionError(errno.EACCES, f"原生存储拒绝 {path}", str(path))
        diagnostic_sink.primary = failure if logging_unavailable else None
        original_inspect, original_open = Path.is_symlink, Path.open
        observations = 0

        def inspect(target: Path) -> bool:
            nonlocal observations
            if target in (index, native_io_path(index)):
                observations += 1
                if observations == 2:
                    raise failure
            return original_inspect(target)

        def open_lock(target: Path, *args, **kwargs):
            if target == lock:
                raise failure
            return original_open(target, *args, **kwargs)

        for entry in ("bootstrap", "history"):
            before = await _history_request(case.client, session, thread, entry)
            assert before.status_code == 200, before.text
            observations = 0
            with monkeypatch.context() as patch:
                if fault == "index_inspection":
                    patch.setattr(Path, "is_symlink", inspect)
                else:
                    patch.setattr(Path, "open", open_lock)
                failed = await _history_request(case.client, session, thread, entry)
            assert failed.status_code == 500, failed.text
            message = failed.json()["detail"]["message"]
            assert "stage=history_read, error=PermissionError, errno=13" in message
            assert str(case.workspace) not in failed.text
            assert str(diagnostic_sink.destination) not in failed.text
            if fault == "index_inspection":
                assert observations == 2
            native_logs = [record for record in caplog.records if record.exc_info and record.exc_info[1] is failure]
            assert native_logs and str(path) in caplog.text
            projected = [record.exc_info[1] for record in caplog.records
                         if record.exc_info and record.exc_info[1] is not None
                         and record.exc_info[1].__cause__ is failure]
            assert projected
            for error in projected:
                assert isinstance(error, PermissionError) and error.errno == errno.EACCES
                assert error.filename is None and str(path) not in str(error)
                assert ("diagnostic_sink_error=" in str(error)) is logging_unavailable
                if logging_unavailable:
                    assert f"diagnostic_sink_error={sink_type}" in str(error)

            def probe_writer() -> None:
                with storage._lock(thread, "", session_id=session):
                    pass

            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(probe_writer).result(timeout=2)
            with RolloutCheckpointSaver(storage.sessions_dir) as cold:
                assert tuple(cold.read_canonical_items(session, thread)) == original_items
            restored = await _history_request(case.client, session, thread, entry)
            assert restored.status_code == 200, restored.text
            assert restored.json()["data"] == before.json()["data"]
            assert canonical.read_bytes() == original_bytes

        assert len(diagnostic_sink.errors) == (2 if logging_unavailable else 0)
        assert all(type(error) is type(expected_sink.value) and error.errno == expected_sink.value.errno
                   for error in diagnostic_sink.errors)
        if logging_unavailable:
            assert any(str(diagnostic_sink.destination) in note for note in failure.__notes__)
        diagnostic_sink.primary = None
        successor_call, = await case.plan(session, [{"content": "原 Thread 继续成功。"}])
        successor = await _send(case, session, "继续原 Thread。")
        successor_id = successor["job_id"]
        assert isinstance(successor_id, str) and successor_id != job_id
        await _job(case, successor_id)
        assert case.call_count(session, thread) == 3
        assert tuple(storage.read_items(session, thread))[:len(original_items)] == original_items
        assemblies = case.container.checkpointer._storage.assemblies.list_context_assemblies(session, thread)
        assert {assembly.model_call_id for assembly in assemblies} == {*first_calls, successor_call}
        for assembly in assemblies:
            assert (assembly.session_id, assembly.thread_id) == (session, thread)
            assert isinstance(assembly.model_call_id, str)
            assert assembly.request_hash_preimage == normalize_wire_request(case.request(assembly.model_call_id))
            assembly.validate_hashes()
        case.write_actual(f"storage-{fault}-{logging_unavailable}", {
            "session_id": session, "thread_id": thread,
            "job_id": job_id, "successor_id": successor_id,
            "diagnostic_failures": len(diagnostic_sink.errors),
            "calls": [*first_calls, successor_call],
        })
