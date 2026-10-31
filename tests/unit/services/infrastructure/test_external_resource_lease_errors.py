"""原资源账本的文件错误不泄露位置，失败不改变真实占用事实。"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)


@pytest.fixture(params=[False, True], ids=["log-ok", "log-failed"])
def ledger_log_failure(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> OSError | None:
    if not request.param:
        return None
    failure = OSError("日志输出失败 /private/ledger-log-secret")
    original = logging.Logger.callHandlers

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original(logger, record)
        if "artifact=external_resource_leases" in record.getMessage():
            raise failure

    monkeypatch.setattr(logging.Logger, "callHandlers", call_handlers)
    return failure


@pytest.fixture
def ledger(tmp_path: Path) -> tuple[ExternalResourceLeaseLedger, Path]:
    path = tmp_path / "private" / "resources.json"
    owner = ExternalResourceLeaseLedger(state_path=path)
    owner.register_external(resource_id="terminal_original", kind="terminal", lifetime_scope="workspace")
    owner.acquire_operation(resource_id="terminal_original", turn_stream_id="holder", operation_id="original")
    return owner, path


@pytest.mark.parametrize("payload,reason", [
    ([], "必须是对象"),
    ({"records": "private-path"}, "records 必须是数组"),
    ({"leases": "private-path"}, "leases 必须是数组"),
    ({"records": [{}, "private-path"]}, "records[1] 必须是对象"),
    ({"leases": [{}, "private-path"]}, "leases[1] 必须是对象"),
])
def test_bad_envelope_reports_field_without_file_location(
    ledger: tuple[ExternalResourceLeaseLedger, Path], payload: object, reason: str,
    caplog: pytest.LogCaptureFixture, ledger_log_failure: OSError | None,
) -> None:
    owner, path = ledger
    before = path.read_bytes()
    damaged = json.dumps(payload).encode()
    path.write_bytes(damaged)
    with caplog.at_level("ERROR"), pytest.raises(TypeError) as raised:
        ExternalResourceLeaseLedger(state_path=path)
    detail = str(raised.value)
    assert str(path) not in detail and "private-path" not in detail
    assert reason in detail and "artifact=external_resource_leases" in detail and "stage=validate" in detail
    assert str(path) in caplog.text and path.read_bytes() == damaged
    assert isinstance(raised.value.__cause__, TypeError)
    if ledger_log_failure is not None:
        assert "diagnostic_sink_error=OSError" in detail
        assert any(str(ledger_log_failure) in note for note in raised.value.__cause__.__notes__)
    else:
        assert "diagnostic_sink_error" not in detail
    assert "ledger-log-secret" not in detail
    path.write_bytes(before)
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert cold.get("terminal_original") == owner.get("terminal_original")
    assert cold.leases_for_resource("terminal_original") == owner.leases_for_resource("terminal_original")


@pytest.mark.parametrize("stage", ["read", "mkdir", "open", "fsync", "replace"])
def test_native_failure_preserves_original_lease_and_explicit_retry(
    ledger: tuple[ExternalResourceLeaseLedger, Path], stage: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ledger_log_failure: OSError | None,
) -> None:
    owner, path = ledger
    original_bytes = path.read_bytes()
    original_record = owner.get("terminal_original")
    original_leases = owner.leases_for_resource("terminal_original")
    native = PermissionError(errno.EACCES, "原生拒绝", str(path))
    with caplog.at_level("ERROR"), monkeypatch.context() as fault:
        if stage == "fsync":
            def fail_sync(_fd: int) -> None:
                raise native
            fault.setattr(os, "fsync", fail_sync)
        else:
            method = {"read": "read_text", "mkdir": "mkdir", "open": "open", "replace": "replace"}[stage]
            target = path if stage == "read" else path.parent if stage == "mkdir" else path.with_suffix(".tmp")
            original_method = getattr(Path, method)

            def fail_target(current: Path, *args: object, **kwargs: object) -> object:
                if current == target:
                    raise native
                return original_method(current, *args, **kwargs)

            fault.setattr(Path, method, fail_target)
        with pytest.raises(PermissionError) as raised:
            if stage == "read":
                ExternalResourceLeaseLedger(state_path=path)
            else:
                owner.acquire_operation(resource_id="terminal_original", turn_stream_id="holder", operation_id="new")
    error = raised.value
    assert str(path) not in str(error) and error.filename is None
    assert error.errno == errno.EACCES and error.__cause__ is native
    assert f"stage={'read' if stage == 'read' else 'write'}" in str(error)
    assert str(path) in caplog.text and "原生拒绝" in caplog.text
    if ledger_log_failure is not None:
        assert "diagnostic_sink_error=OSError" in str(error)
        assert any(str(ledger_log_failure) in note for note in native.__notes__)
    else:
        assert "diagnostic_sink_error" not in str(error)
    assert "ledger-log-secret" not in str(error)
    assert path.read_bytes() == original_bytes and owner.get("terminal_original") == original_record
    assert owner.leases_for_resource("terminal_original") == original_leases
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert cold.leases_for_resource("terminal_original") == original_leases
    retried = owner.acquire_operation(resource_id="terminal_original", turn_stream_id="holder", operation_id="new")
    assert owner.acquire_operation(resource_id="terminal_original", turn_stream_id="holder", operation_id="new") == retried
    assert ExternalResourceLeaseLedger(state_path=path).get_lease(retried.lease_id) == retried


def test_directory_cannot_replace_a_missing_ledger(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "private" / "resources.json"
    path.mkdir(parents=True)
    sentinel = path / "unrelated"
    sentinel.write_bytes(b"untouched")
    with caplog.at_level("ERROR"), pytest.raises(OSError) as raised:
        ExternalResourceLeaseLedger(state_path=path)
    assert str(path) not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError) and str(path) in caplog.text
    assert sentinel.read_bytes() == b"untouched"
    sentinel.unlink()
    path.rmdir()
    assert ExternalResourceLeaseLedger(state_path=path).get("absent") is None


def test_memory_and_not_yet_created_ledgers_keep_their_contract(tmp_path: Path) -> None:
    path = tmp_path / "not-created" / "resources.json"
    assert ExternalResourceLeaseLedger(state_path=path).get("absent") is None
    assert not path.parent.exists()
    owner = ExternalResourceLeaseLedger()
    owner.register_external(resource_id="memory", kind="mcp_connection", lifetime_scope="workspace")
    assert owner.get("memory") is not None


@pytest.mark.parametrize("invalid_status", ["", "unknown", "running", "stopped", "active ", "/private/lease-status"])
def test_cold_load_rejects_invalid_lease_status_without_losing_occupancy(
    ledger: tuple[ExternalResourceLeaseLedger, Path], invalid_status: str,
) -> None:
    owner, path = ledger
    original = path.read_bytes()
    payload = json.loads(original)
    payload["leases"][0]["status"] = invalid_status
    path.write_text(json.dumps(payload), encoding="utf-8")
    damaged = path.read_bytes()
    with pytest.raises(ValidationError) as caught:
        ExternalResourceLeaseLedger(state_path=path)
    assert path.read_bytes() == damaged
    assert caught.value.errors(include_input=False)[0]["loc"] == (0, "status")
    assert caught.value.errors(include_input=False)[0]["type"] == "literal_error"
    assert "input_value" not in str(caught.value) and "/private/lease-status" not in str(caught.value)
    assert str(path) not in str(caught.value)
    path.write_bytes(original)
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert cold.leases_for_resource("terminal_original") == owner.leases_for_resource("terminal_original")
    assert cold.get("terminal_original") == owner.get("terminal_original")


@pytest.fixture
def competing_writes(
    ledger: tuple[ExternalResourceLeaseLedger, Path], monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[], object], Callable[[], object]], tuple[object, object]]:
    owner, path = ledger
    replace = Path.replace

    def run(first_action: Callable[[], object], second_action: Callable[[], object]) -> tuple[object, object]:
        entered, release_first, second_started, second_finished = (Event() for _ in range(4))

        def read_published() -> object:
            return owner.get("terminal_original"), owner.leases_for_turn("holder"), owner.get("browser_first")

        original = read_published()

        def pause_first_replace(current: Path, target: Path) -> Path:
            if current == path.with_suffix(".tmp") and not entered.is_set():
                entered.set()
                assert release_first.wait(5), "测试未放行原账本写入"
            return replace(current, target)

        def second_write() -> object:
            second_started.set()
            try:
                return second_action()
            finally:
                second_finished.set()

        monkeypatch.setattr(Path, "replace", pause_first_replace)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(first_action)
            second: Future[object] | None = None
            try:
                assert entered.wait(2)
                # 在途文件写入不发布草稿，也不阻塞读取最后成功的内存快照。
                assert pool.submit(read_published).result(timeout=2) == original
                second = pool.submit(second_write)
                assert second_started.wait(2)
                # 给竞争writer实际推进机会；它不能覆盖原临时文件或使用过时快照。
                second_finished.wait(0.25)
            finally:
                release_first.set()
            first_result = first.result(timeout=3)
            assert second is not None
            return first_result, second.result(timeout=3)

    return run


@pytest.mark.parametrize(("mutation", "lease_status", "resource_status"), [
    ("register", "active", "running"),
    ("acquire", "active", "running"),
    ("release", "released", "running"),
    ("settle", "settled", "running"),
    ("release_turn", "released", "running"),
    ("reconcile", "reconcile_required", "recovered"),
    ("update", "reconcile_required", "unknown"),
])
def test_competing_writers_preserve_both_mutations_and_published_reads(
    ledger: tuple[ExternalResourceLeaseLedger, Path], mutation: str, lease_status: str, resource_status: str,
    competing_writes: Callable[[Callable[[], object], Callable[[], object]], tuple[object, object]],
) -> None:
    owner, path = ledger
    original_lease, = owner.leases_for_resource("terminal_original")
    mutations: dict[str, Callable[[], object]] = {
        "register": lambda: owner.register_external(resource_id="terminal_peer", kind="terminal", lifetime_scope="workspace"),
        "acquire": lambda: owner.acquire_operation(resource_id="terminal_original", turn_stream_id="parallel", operation_id="new"),
        "release": lambda: owner.release(original_lease.lease_id),
        "settle": lambda: asyncio.run(owner.settle(original_lease.lease_id)),
        "release_turn": lambda: owner.release_turn_leases("holder"),
        "reconcile": lambda: asyncio.run(owner.reconcile({"terminal_original": "running", "browser_first": "running"})),
        "update": lambda: owner.update_external_state(
            "terminal_original", lifetime_scope="thread", status="unknown", unresolved_lease_id=original_lease.lease_id,
        ),
    }

    competing_writes(
        lambda: owner.register_external(resource_id="browser_first", kind="browser_context", lifetime_scope="workspace"),
        mutations[mutation],
    )
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert cold.get("browser_first") == owner.get("browser_first") is not None
    record = cold.get("terminal_original")
    lease = cold.get_lease(original_lease.lease_id)
    assert record is not None and record == owner.get("terminal_original") and record.status == resource_status
    assert lease is not None and lease == owner.get_lease(original_lease.lease_id) and lease.status == lease_status
    if mutation == "register":
        assert cold.get("terminal_peer") == owner.get("terminal_peer") is not None
    elif mutation == "acquire":
        assert cold.leases_for_turn("parallel") == owner.leases_for_turn("parallel")
        assert len(cold.leases_for_turn("parallel")) == 1
    if mutation == "update":
        assert record.lifetime_scope == "thread"
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize(("mutation", "lease_status", "resource_status", "scope"), [
    ("register", "reconcile_required", "unknown", "thread"),
    ("acquire", "reconcile_required", "unknown", "thread"),
    ("release_turn", "settled", "running", "workspace"),
    ("reconcile", "settled", "recovered", "workspace"),
    ("update", "settled", "unknown", "thread"),
])
def test_competing_mutations_recheck_state_after_previous_commit(
    ledger: tuple[ExternalResourceLeaseLedger, Path], mutation: str, lease_status: str, resource_status: str, scope: str,
    competing_writes: Callable[[Callable[[], object], Callable[[], object]], tuple[object, object]],
) -> None:
    owner, path = ledger
    original_lease, = owner.leases_for_resource("terminal_original")
    first: Callable[[], object]
    if mutation in {"register", "acquire"}:
        first = lambda: owner.update_external_state(
            "terminal_original", lifetime_scope="thread", status="unknown", unresolved_lease_id=original_lease.lease_id,
        )
    else:
        first = lambda: asyncio.run(owner.settle(original_lease.lease_id))
    mutations: dict[str, Callable[[], object]] = {
        "register": lambda: owner.register_external(resource_id="terminal_original", kind="terminal", lifetime_scope="workspace"),
        "acquire": lambda: owner.acquire_operation(resource_id="terminal_original", turn_stream_id="parallel", operation_id="new"),
        "release_turn": lambda: owner.release_turn_leases("holder"),
        "reconcile": lambda: asyncio.run(owner.reconcile({"terminal_original": "running"})),
        "update": lambda: owner.update_external_state(
            "terminal_original", lifetime_scope="thread", status="unknown", unresolved_lease_id=original_lease.lease_id,
        ),
    }
    if mutation == "acquire":
        with pytest.raises(RuntimeError, match="资源当前不可获取 lease"):
            competing_writes(first, mutations[mutation])
    else:
        competing_writes(first, mutations[mutation])
    cold = ExternalResourceLeaseLedger(state_path=path)
    record = cold.get("terminal_original")
    lease = cold.get_lease(original_lease.lease_id)
    assert record is not None and record == owner.get("terminal_original")
    assert (record.status, record.lifetime_scope) == (resource_status, scope)
    assert lease is not None and lease == owner.get_lease(original_lease.lease_id) and lease.status == lease_status
    assert cold.leases_for_turn("parallel") == []
    assert not path.with_suffix(".tmp").exists()
