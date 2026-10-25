from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import pytest

from tests.support.e2e_control import harness as e2e_test_control


@dataclass
class LeaseCase:
    lease: e2e_test_control.E2EProcessLease
    port_checks: list[tuple[int, str]]
    start_times: dict[int, float]

    def expected_record(self) -> dict[str, object]:
        return {
            "pid": os.getpid(),
            "start_time": self.start_times[os.getpid()],
            "token": self.lease.token,
            "ports": list(self.lease.ports),
        }


@pytest.fixture
def lease_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LeaseCase:
    checks: list[tuple[int, str]] = []
    start_times = {os.getpid(): 100.25}
    monkeypatch.setattr(
        e2e_test_control,
        "_process_start_time",
        lambda pid: start_times.get(pid),
    )
    monkeypatch.setattr(e2e_test_control, "ensure_local_port_available", lambda port, *, service_name: checks.append((port, service_name)))
    return LeaseCase(
        e2e_test_control.E2EProcessLease(
            tmp_path / "control/process-lease.json",
            (18800,),
            token="fixture-token",
        ),
        checks,
        start_times,
    )


@pytest.mark.parametrize("stage", ["dump", "close"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_new_lease_write_failure_removes_own_file_and_allows_explicit_retry(
    lease_case: LeaseCase, stage: str, interrupted: bool,
) -> None:
    lease = lease_case.lease
    error = KeyboardInterrupt("租约写入中断") if interrupted else OSError("租约写入失败")
    with pytest.MonkeyPatch.context() as patch:
        if stage == "dump":
            def fail_dump(_record: object, stream: TextIO) -> None:
                stream.write('{"pid":')
                raise error

            patch.setattr(e2e_test_control.json, "dump", fail_dump)
        else:
            original_open = Path.open

            class FailedClose:
                def __init__(self, stream: TextIO) -> None:
                    self.stream = stream

                def __enter__(self) -> TextIO:
                    return self.stream

                def __exit__(self, *_args: object) -> None:
                    self.stream.close()
                    raise error

            def open_lease(path: Path, mode: str = "r", *args: object, **kwargs: object):
                stream = original_open(path, mode, *args, **kwargs)
                return FailedClose(stream) if path == lease.path and mode == "x" else stream

            patch.setattr(Path, "open", open_lease)
        with pytest.raises(type(error)) as failure:
            lease.acquire()

        assert failure.value is error
        assert not lease.path.exists(), "本次失败写入不能遗留损坏或未取得的租约"

    lease.acquire()
    assert json.loads(lease.path.read_text()) == lease_case.expected_record()
    lease.release()
    assert not lease.path.exists()


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_lease_removal_exposes_cleanup_error_and_original_write_cause(
    lease_case: LeaseCase, monkeypatch: pytest.MonkeyPatch, interrupted: bool,
) -> None:
    lease = lease_case.lease
    write_error = OSError("租约正文写入失败")
    cleanup_error = KeyboardInterrupt("租约回收中断") if interrupted else PermissionError("租约回收被拒绝")
    original_unlink = Path.unlink

    def fail_dump(_record: object, stream: TextIO) -> None:
        stream.write("{")
        raise write_error

    def fail_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == lease.path:
            raise cleanup_error
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(e2e_test_control.json, "dump", fail_dump)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(type(cleanup_error)) as failure:
        lease.acquire()

    assert failure.value is cleanup_error
    assert cleanup_error.__context__ is write_error
    assert lease.path.read_text() == "{"


def test_exclusive_create_collision_does_not_remove_competing_file(
    lease_case: LeaseCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = lease_case.lease
    original_open = Path.open
    competing_bytes = b'{"token":"another-owner"}'

    def create_competing_lease(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path == lease.path and mode == "x":
            path.write_bytes(competing_bytes)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", create_competing_lease)
    with pytest.raises(FileExistsError):
        lease.acquire()
    assert lease.path.read_bytes() == competing_bytes


def test_existing_corrupt_lease_remains_visible_without_implicit_replacement(lease_case: LeaseCase) -> None:
    lease = lease_case.lease
    lease.path.parent.mkdir(parents=True)
    lease.path.write_text("broken-existing-json")
    with pytest.raises(json.JSONDecodeError):
        lease.acquire()
    assert lease.path.read_text() == "broken-existing-json"


def test_live_owner_cannot_be_displaced(lease_case: LeaseCase) -> None:
    lease = lease_case.lease
    lease.acquire()
    before = lease.path.read_bytes()
    competitor = e2e_test_control.E2EProcessLease(lease.path, lease.ports, token="competitor")
    with pytest.raises(RuntimeError, match="独占租约仍有效"):
        competitor.acquire()
    assert lease.path.read_bytes() == before
    lease.release()


def test_development_port_rejection_does_not_create_lease(lease_case: LeaseCase) -> None:
    lease_case.lease.ports = (8011,)
    with pytest.raises(ValueError, match="不得租用开发端口"):
        lease_case.lease.acquire()
    assert not lease_case.lease.path.exists()
    assert lease_case.port_checks == []


def test_release_with_wrong_token_preserves_current_lease(lease_case: LeaseCase) -> None:
    lease = lease_case.lease
    lease.acquire()
    before = lease.path.read_bytes()
    competitor = e2e_test_control.E2EProcessLease(lease.path, lease.ports, token="competitor")
    with pytest.raises(RuntimeError, match="owner 变化"):
        competitor.release()
    assert lease.path.read_bytes() == before
    lease.release()


def test_success_preserves_record_and_checks_ports_on_acquire_and_release(lease_case: LeaseCase) -> None:
    lease = lease_case.lease
    lease.acquire()
    assert json.loads(lease.path.read_text()) == lease_case.expected_record()
    lease.release()
    assert not lease.path.exists()
    assert lease_case.port_checks == [
        (18800, "E2E process lease owner"),
        (18800, "E2E process lease teardown"),
    ]


def test_reused_pid_with_different_creation_time_does_not_own_old_lease(
    lease_case: LeaseCase,
) -> None:
    lease = lease_case.lease
    lease.path.parent.mkdir(parents=True)
    lease.path.write_text(json.dumps({**lease_case.expected_record(), "start_time": "previous-process", "token": "old-token"}))
    lease_case.start_times[os.getpid()] = 101.5
    lease.acquire()
    assert json.loads(lease.path.read_text()) == lease_case.expected_record()
    lease.release()
