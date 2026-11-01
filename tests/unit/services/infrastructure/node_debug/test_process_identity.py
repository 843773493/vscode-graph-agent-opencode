from __future__ import annotations

import errno
import logging
import os
import signal
from pathlib import Path

import psutil
import pytest

from app.services.infrastructure.node_debug.process import (
    process_identity,
    process_lifecycle,
)
from app.services.infrastructure.node_debug.process.process_identity import (
    IDENTITY_SOURCE_LINUX_PROC,
    IDENTITY_SOURCE_PSUTIL,
    NodeDebugProcessIdentity,
    probe_process_identity,
)
from tests.unit.services.infrastructure.node_debug.test_node_debug_launch_claim import (
    _make_service,
)
from tests.unit.services.infrastructure.node_debug.test_node_debug_launch_claim import (
    session_tree as session_tree,  # noqa: PLC0414
)


@pytest.mark.parametrize("change", ["reused", "incomparable", "gone"])
async def test_force_stop_rechecks_identity_after_the_original_wait(
    tmp_path: Path, session_tree: tuple[object, Path, Path],
    change: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _root = _make_service(tmp_path, session_tree[0])
    same = NodeDebugProcessIdentity(pid=4321, source=IDENTITY_SOURCE_LINUX_PROC, start_marker="original")
    changed = (
        None if change == "gone" else NodeDebugProcessIdentity(
            pid=4321,
            source=IDENTITY_SOURCE_PSUTIL if change == "incomparable" else same.source,
            start_marker=same.start_marker if change == "incomparable" else "replacement",
        )
    )
    probes = iter([same, same])
    signals: list[tuple[int, int]] = []

    def probe(_pid: int) -> NodeDebugProcessIdentity | None:
        return next(probes, changed)

    # 只缩短测试的原等待，身份在等待到期与下一次派发之间变化。
    monkeypatch.setattr(process_lifecycle, "_RECONCILE_TERMINATE_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(process_lifecycle, "probe_process_identity", probe)
    monkeypatch.setattr(process_lifecycle.os, "kill", lambda pid, value: signals.append((pid, value)))
    terminated = await service._lifecycle.terminate_verified_instance(
        pid=same.pid, recorded_source=same.source, recorded_start_marker=same.start_marker,
    )
    assert terminated is (change != "incomparable")
    assert signals == [(same.pid, signal.SIGTERM)]


@pytest.fixture(params=[False, True], ids=["log-ok", "log-failed"])
def identity_diagnostic_errors(
    request: pytest.FixtureRequest, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> list[OSError]:
    failures: list[OSError] = []
    if request.param:
        blocker = tmp_path / "identity-log-secret"
        blocker.write_text("此普通文件不能承载日志子文件")
        destination = blocker / "service.log"
        capture = caplog.handler.emit

        def emit(record: logging.LogRecord) -> None:
            capture(record)
            if record.name not in {process_identity.__name__, "app.core.failures"}:
                return
            try:
                destination.write_text(record.getMessage())
            except OSError as error:
                failures.append(error)
                raise

        monkeypatch.setattr(caplog.handler, "emit", emit)
    return failures


def assert_private_diagnostics(
    cause: BaseException, failures: list[OSError], caplog: pytest.LogCaptureFixture,
) -> None:
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    notes = getattr(cause, "__notes__", [])
    if failures:
        assert failures[-1].errno == errno.ENOTDIR
        assert any(str(failures[-1]) in note for note in notes)
    else:
        assert not any("诊断日志输出失败" in note for note in notes)


def test_psutil_provides_verifiable_identity_off_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)

    identity = probe_process_identity(os.getpid())

    assert identity is not None
    assert identity.source == IDENTITY_SOURCE_PSUTIL
    assert identity.verifiable
    assert identity.compare(
        recorded_source=identity.source,
        recorded_start_marker=identity.start_marker,
    ) == "match"
    assert identity.compare(
        recorded_source=identity.source,
        recorded_start_marker=f"{identity.start_marker}-reused",
    ) == "mismatch"


def test_linux_missing_proc_entry_is_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda _path: pytest.fail("Linux PID 身份探测应直接读取 /proc/stat"),
    )
    monkeypatch.setattr(
        process_identity,
        "_probe_psutil_identity",
        lambda _pid: pytest.fail("Linux /proc 读取结果不得回落到 psutil"),
    )

    assert probe_process_identity(4242) is None


def test_linux_process_identity_requires_boot_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    identity_diagnostic_errors: list[OSError],
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda _path: pytest.fail("Linux 进程身份读取不应先探测文件是否存在"),
    )
    process_root = tmp_path / "4242"
    process_root.mkdir()
    stat_fields = ["S", *(["0"] * 18), "12345"]
    (process_root / "stat").write_text(
        f"4242 (python) {' '.join(stat_fields)}",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="boot_id"):
        probe_process_identity(4242)

    boot_id_path = tmp_path / "sys" / "kernel" / "random" / "boot_id"
    boot_id_path.parent.mkdir(parents=True)
    boot_id_path.write_text("\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=process_identity.__name__), pytest.raises(RuntimeError, match="启动 ID 为空") as caught:
        probe_process_identity(4242)
    assert str(boot_id_path) not in str(caught.value)
    assert f"source={IDENTITY_SOURCE_LINUX_PROC}" in str(caught.value)
    assert str(boot_id_path) in caplog.text
    cause = caught.value.__cause__
    assert isinstance(cause, RuntimeError)
    assert any(str(boot_id_path) in note for note in cause.__notes__)
    assert_private_diagnostics(cause, identity_diagnostic_errors, caplog)
    assert "identity-log-secret" not in str(caught.value)


@pytest.mark.parametrize("stage,code", [
    ("process_stat_read", errno.EACCES),
    ("boot_id_read", errno.ENOENT),
    ("boot_id_read", errno.EACCES),
])
@pytest.mark.parametrize("private", ["/private/identity-secret", r"C:\private\identity-secret", r"\\server\private\identity-secret"])
def test_linux_identity_io_failure_keeps_native_facts_and_private_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    stage: str, code: int, private: str,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    process_root = tmp_path / "4242"
    process_root.mkdir()
    stat_path = process_root / "stat"
    stat_path.write_text(f"4242 (python) {' '.join(['S', *(['0'] * 18), '12345'])}")
    failed_path = stat_path if stage == "process_stat_read" else tmp_path / "sys/kernel/random/boot_id"
    native_error = OSError(code, f"身份读取失败: {private}", private)
    read_text = Path.read_text

    def read_identity(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == failed_path:
            raise native_error
        return read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_identity)
    monkeypatch.setattr(process_identity, "_probe_psutil_identity", lambda _pid: pytest.fail("Linux 身份读取失败不得回落"))
    with caplog.at_level(logging.ERROR), pytest.raises(type(native_error)) as caught:
        probe_process_identity(4242)
    error = caught.value
    assert error is not native_error and error.__cause__ is native_error
    assert error.errno == code and error.filename is None
    assert str(tmp_path) not in str(error) and "identity-secret" not in str(error)
    assert f"stage={stage}" in str(error) and f"error={type(native_error).__name__}" in str(error)
    assert f"source={IDENTITY_SOURCE_LINUX_PROC}" in str(error)
    if stage == "process_stat_read":
        assert "pid=4242" in str(error)
    assert any(record.exc_info and record.exc_info[1] is native_error for record in caplog.records)
    assert str(native_error) in caplog.text


@pytest.mark.parametrize("raw,reason", [
    ("进程身份原文 /private/identity-secret", "格式异常"),
    ("4242 (/private/identity-secret) S 0", "字段不足"),
])
def test_linux_malformed_identity_rejects_without_publishing_private_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    raw: str, reason: str,
    identity_diagnostic_errors: list[OSError],
) -> None:
    process_root = tmp_path / "4242"
    process_root.mkdir()
    stat_path = process_root / "stat"
    stat_path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_probe_psutil_identity", lambda _pid: pytest.fail("损坏的 /proc 身份不得回落"))

    with caplog.at_level(logging.ERROR, logger=process_identity.__name__), pytest.raises(RuntimeError, match=reason) as caught:
        probe_process_identity(4242)
    message = str(caught.value)
    assert str(tmp_path) not in message and "identity-secret" not in message and "/proc/" not in message
    assert "pid=4242" in message and f"source={IDENTITY_SOURCE_LINUX_PROC}" in message
    assert str(stat_path) in caplog.text and raw in caplog.text
    cause = caught.value.__cause__
    assert isinstance(cause, RuntimeError)
    assert any(str(stat_path) in note and raw in note for note in cause.__notes__)
    assert_private_diagnostics(cause, identity_diagnostic_errors, caplog)
    assert "identity-log-secret" not in message


@pytest.fixture(params=["/private/identity-secret", r"C:\private\identity-secret", r"\\server\private\identity-secret"])
def psutil_identity_error(request: pytest.FixtureRequest) -> psutil.AccessDenied:
    return psutil.AccessDenied(pid=4242, name=request.param, msg=f"读取失败: {request.param}")


def test_psutil_identity_failure_keeps_cause_and_private_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    psutil_identity_error: psutil.AccessDenied,
    identity_diagnostic_errors: list[OSError],
) -> None:
    def denied_process(pid: int) -> None:
        assert pid == 4242
        raise psutil_identity_error

    monkeypatch.setattr(psutil, "Process", denied_process)
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")
    with caplog.at_level(logging.ERROR, logger=process_identity.__name__), pytest.raises(RuntimeError) as caught:
        probe_process_identity(4242)
    message = str(caught.value)
    assert caught.value.__cause__ is psutil_identity_error
    assert "identity-secret" not in message
    assert "pid=4242" in message and f"source={IDENTITY_SOURCE_PSUTIL}" in message
    assert "error_type=AccessDenied" in message
    assert any(record.exc_info and record.exc_info[1] is psutil_identity_error for record in caplog.records)
    assert str(psutil_identity_error) in caplog.text
    assert_private_diagnostics(psutil_identity_error, identity_diagnostic_errors, caplog)
    assert "identity-log-secret" not in message


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_linux_terminal_proc_states_have_no_live_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    process_root = tmp_path / "4242"
    process_root.mkdir()
    stat_fields = [state, *(["0"] * 18), "12345"]
    (process_root / "stat").write_text(
        f"4242 (python) {' '.join(stat_fields)}",
        encoding="utf-8",
    )

    assert probe_process_identity(4242) is None


def test_linux_proc_identity_reports_vanishing_entry_as_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取 /proc 时条目消失按已核实终结收口，其余错误显式抛出。"""

    class VanishingStatFile:
        def read_text(
            self, encoding: str | None = None, errors: str | None = None
        ) -> str:
            raise FileNotFoundError(2, "No such file or directory")

    class VanishingProcessDirectory:
        def __truediv__(self, name: str) -> VanishingStatFile:
            return VanishingStatFile()

    class VanishingProcRoot:
        def __truediv__(self, name: str) -> VanishingProcessDirectory:
            return VanishingProcessDirectory()

    class DeniedStatFile:
        def read_text(
            self, encoding: str | None = None, errors: str | None = None
        ) -> str:
            raise PermissionError(13, "Permission denied")

    class DeniedProcessDirectory:
        def __truediv__(self, name: str) -> DeniedStatFile:
            return DeniedStatFile()

    class DeniedProcRoot:
        def __truediv__(self, name: str) -> DeniedProcessDirectory:
            return DeniedProcessDirectory()

    monkeypatch.setattr(process_identity, "_PROC_ROOT", VanishingProcRoot())
    # 进程在 read_text 期间消失：与"条目不存在"同一事实 ⇒ 已核实终结。
    assert process_identity._probe_linux_proc_identity(4242) is None

    monkeypatch.setattr(process_identity, "_PROC_ROOT", DeniedProcRoot())
    # 非消失类读取错误仍然显式抛出，绝不默默吞掉。
    with pytest.raises(PermissionError):
        process_identity._probe_linux_proc_identity(4242)
