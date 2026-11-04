"""详情路径拒绝与日志次错不改写冻结正文，也不丢失原错误归属。"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
    DetailUnavailableError,
    PreparedDetail,
)

_LOGGER = "app.services.infrastructure.rollout_context.runtime.detail_files"
_SESSION = "ses_019c205391bb7a4b81b6728df70f63e6"


@dataclass(frozen=True)
class DetailCase:
    sessions: Path
    store: ContextPlanDetailStore
    prepared: PreparedDetail
    target: Path
    sibling: Path


@pytest.fixture
def detail_case(integration_workspace_root_path: str, session_bundle_factory: Callable[[Path, str], Path],
                request: pytest.FixtureRequest) -> DetailCase:
    node = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    sessions = Path(integration_workspace_root_path) / "detail-cases" / node / ".boxteam/sessions"
    session_bundle_factory(sessions, _SESSION)
    resolver = get_session_path_resolver(sessions)
    thread = resolver.main_thread_id(_SESSION)
    store = ContextPlanDetailStore(sessions)
    prepared = store.prepare(
        session_id=_SESSION, thread_id=thread, assembly_id="path-diagnostic",
        detail_kind="request_source", retention_class="request_replay",
        visibility="internal", detail={"text": "冻结正文 Ω\r\n"},
        required=True, source_revision="original-revision",
    )
    target, = store.publish(prepared)
    sibling = target.with_name("untouched-sibling")
    sibling.write_bytes(b"unrelated immutable body")
    return DetailCase(sessions, store, prepared, target, sibling)


@pytest.fixture(params=[False, True], ids=["logging", "logging-unavailable"])
def detail_log(request: pytest.FixtureRequest, tmp_path: Path,
               monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    blocker = tmp_path / "detail-diagnostic-log-secret"
    blocker.write_text("日志目录阻挡原件", encoding="utf-8")
    records: list[logging.LogRecord] = []
    errors: list[OSError] = []
    emit = caplog.handler.emit

    def capture(record: logging.LogRecord) -> None:
        emit(record)
        if record.name != _LOGGER:
            return
        records.append(record)
        if request.param:
            try:
                (blocker / "service.log").write_text(record.getMessage(), encoding="utf-8")
            except OSError as error:
                errors.append(error)
                raise

    monkeypatch.setattr(caplog.handler, "emit", capture)
    return bool(request.param), records, errors


@pytest.fixture(params=["read", "publish", "remove", "gc"])
def detail_operation(request: pytest.FixtureRequest) -> Callable[[DetailCase], object]:
    def run(case: DetailCase) -> object:
        record = case.prepared.record
        if request.param == "publish":
            return case.store.publish(case.prepared)
        if request.param == "remove":
            return case.store.remove(session_id=_SESSION, thread_id=record.thread_id, record=record)
        if request.param == "gc":
            return case.store.gc(
                session_id=_SESSION, thread_id=record.thread_id,
                expired_before=datetime.now(UTC) + timedelta(days=31),
                allowed_refs=(record.detail_ref,),
            )
        return case.store.read(session_id=_SESSION, thread_id=record.thread_id, record=record)
    return run


@contextmanager
def _blocked_path(case: DetailCase, kind: str) -> Iterator[tuple[Path, str]]:
    target = case.target if kind == "target_directory" else case.target.parent
    if kind == "native_permission":
        mode = stat.S_IMODE(target.stat().st_mode)
        target.chmod(0)
        try:
            yield target, "assembly detail 路径访问失败"
        finally:
            target.chmod(mode)
        return
    backup = target.with_name(target.name + ".original")
    target.rename(backup)
    if kind == "target_directory":
        target.mkdir()
        reason = "detail target 不是普通文件"
    else:
        target.write_bytes(b"original blocking file")
        reason = "detail 父路径不是安全普通目录"
    try:
        yield target, reason
    finally:
        if target.is_dir():
            assert list(target.iterdir()) == []
            target.rmdir()
        else:
            assert target.read_bytes() == b"original blocking file"
            target.unlink()
        backup.rename(target)


@pytest.mark.parametrize("fault", ["target_directory", "parent_file", "native_permission"])
def test_detail_path_failure_keeps_owner_cause_and_cold_retry(
    detail_case: DetailCase, detail_log, detail_operation: Callable[[DetailCase], object], fault: str,
) -> None:
    if fault == "native_permission" and (os.name == "nt" or os.geteuid() == 0):
        pytest.skip("真实 chmod 拒绝需 POSIX 非 root 用户；其它文件形态拒绝跨平台执行")
    case = detail_case
    raw = case.target.read_bytes()
    sibling = case.sibling.read_bytes()
    unavailable, records, sink_errors = detail_log
    with (
        _blocked_path(case, fault) as (blocked, reason),
        pytest.raises(DetailUnavailableError, match=reason) as caught,
    ):
        detail_operation(case)
    error = caught.value
    record, = records
    assert "stage=path" in str(error) and str(case.prepared.record.detail_ref) in str(error)
    if fault == "native_permission":
        assert record.exc_info is not None and record.exc_info[1] is not None
        original = record.exc_info[1]
        assert isinstance(original, PermissionError) and original.errno == errno.EACCES
        assert original.filename == str(case.target)
        assert error.__cause__ is original and "error=PermissionError, errno=13" in str(error)
        assert original.strerror is not None and original.strerror in str(error)
    else:
        original = error
        assert error.__cause__ is None
    diagnostic = record.getMessage() + "\n".join(getattr(original, "__notes__", []))
    if fault != "native_permission":
        assert str(blocked) in diagnostic
    if unavailable:
        sink_error, = sink_errors
        assert sink_error.errno == errno.ENOTDIR
        assert any(str(sink_error) in note for note in original.__notes__)
        assert "diagnostic_sink_error=NotADirectoryError" in str(error)
    else:
        assert not sink_errors and "diagnostic_sink_error" not in str(error)
    assert str(blocked) in str(error) and "detail-diagnostic-log-secret" not in str(error)
    assert case.target.read_bytes() == raw == case.prepared.raw
    assert case.sibling.read_bytes() == sibling
    assert sorted(path.name for path in case.target.parent.iterdir()) == sorted([case.target.name, case.sibling.name])
    cold = ContextPlanDetailStore(case.sessions)
    restored = cold.read(session_id=_SESSION, thread_id=case.prepared.record.thread_id, record=case.prepared.record)
    assert restored["detail"] == {"text": "冻结正文 Ω\r\n"}
    assert restored["detail_ref"] == case.prepared.record.detail_ref.to_dict()


def test_missing_detail_keeps_native_location_and_cold_recovery(detail_case: DetailCase, detail_log) -> None:
    case = detail_case
    unavailable, records, sink_errors = detail_log
    case.target.unlink()
    record = case.prepared.record
    with pytest.raises(DetailUnavailableError) as caught:
        ContextPlanDetailStore(case.sessions).read(session_id=_SESSION, thread_id=record.thread_id, record=record)
    original = caught.value.__cause__
    assert isinstance(original, FileNotFoundError) and original.errno == errno.ENOENT
    assert original.filename == str(case.target)
    detail = str(caught.value)
    assert str(original) in detail and "stage=read" in detail and str(record.detail_ref) in detail
    logged, = records
    assert logged.exc_info is not None and logged.exc_info[1] is original
    if unavailable:
        sink_error, = sink_errors
        assert str(sink_error) in "\n".join(original.__notes__)
        assert "diagnostic_sink_error=NotADirectoryError" in detail
    assert "detail-diagnostic-log-secret" not in detail
    assert not case.target.exists() and case.sibling.read_bytes() == b"unrelated immutable body"
    case.store.publish(case.prepared)
    restored = ContextPlanDetailStore(case.sessions).read(session_id=_SESSION, thread_id=record.thread_id, record=record)
    assert restored["detail"] == {"text": "冻结正文 Ω\r\n"}
    assert case.target.read_bytes() == case.prepared.raw


def test_exclusive_detail_collision_keeps_both_native_filenames_and_original_body(
    detail_case: DetailCase, detail_log,
) -> None:
    case = detail_case
    unavailable, records, sink_errors = detail_log
    with pytest.raises(DetailUnavailableError) as caught:
        case.store.publish(case.prepared)
    original = caught.value.__cause__
    assert isinstance(original, FileExistsError) and original.errno == errno.EEXIST
    assert original.filename2 == str(case.target)
    detail = str(caught.value)
    assert str(original) in detail and "stage=publish" in detail
    assert str(case.prepared.record.detail_ref) in detail
    logged, = records
    assert logged.exc_info is not None and logged.exc_info[1] is original
    if unavailable:
        sink_error, = sink_errors
        assert str(sink_error) in "\n".join(original.__notes__)
        assert "diagnostic_sink_error=NotADirectoryError" in detail
    assert "detail-diagnostic-log-secret" not in detail
    assert case.target.read_bytes() == case.prepared.raw
    assert sorted(path.name for path in case.target.parent.iterdir()) == sorted([case.target.name, case.sibling.name])
