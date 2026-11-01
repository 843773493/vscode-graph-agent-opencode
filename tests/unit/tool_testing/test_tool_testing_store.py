from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.tool_testing.store import ToolTestStore
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414
)


def test_tool_test_store_lists_readable_run_files(tmp_path: Path) -> None:
    store = ToolTestStore(root=tmp_path / "tool_tests")
    store.reset_tool("apply_patch")
    store.write_run(
        "run_a", {"run_id": "run_a", "tool_name": "apply_patch", "created_at": "2"}
    )
    store.reset_tool("edit_file")
    store.write_run(
        "run_b", {"run_id": "run_b", "tool_name": "edit_file", "created_at": "1"}
    )

    assert [item["run_id"] for item in store.list_runs(tool_name="apply_patch")] == [
        "run_a"
    ]

    store.reset_tool("apply_patch")
    store.write_run(
        "run_c", {"run_id": "run_c", "tool_name": "apply_patch", "created_at": "3"}
    )
    with pytest.raises(FileNotFoundError, match="已被新测试覆盖"):
        store.read_run("run_a")
    assert [item["run_id"] for item in store.list_runs(tool_name="apply_patch")] == [
        "run_c"
    ]


@pytest.mark.parametrize("native", ["/private/tool-test.json", r"C:\private\tool-test.json", r"\\server\share\tool-test.json"])
@pytest.mark.parametrize("stage", ["initialize", "reset_tool", "prepare_case", "write_case", "write_run", "read_run", "list_runs"])
@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
def test_store_io_failure_keeps_original_in_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    native: str, stage: str, sink_fails: bool, diagnostic_sink: DiagnosticFailure,
) -> None:
    store = ToolTestStore(root=tmp_path / "tool_tests")
    store.reset_tool("apply_patch")
    payload = {"run_id": "run_a", "tool_name": "apply_patch", "created_at": "1"}
    store.write_run("run_a", payload)
    case = store.prepare_case_dir(tool_name="apply_patch", provider_id="provider", case_id="case")
    original = PermissionError(13, f"原生 I/O 错误 {native}", native)
    if sink_fails:
        diagnostic_sink.primary = original
    method, target = {
        "initialize": ("mkdir", store.root),
        "reset_tool": ("mkdir", store.tool_dir("apply_patch")),
        "prepare_case": ("mkdir", case),
        "write_case": ("write_text", case / "result.json.tmp"),
        "write_run": ("write_text", store.tool_dir("apply_patch") / "run.json.tmp"),
        "read_run": ("read_text", store.tool_dir("apply_patch") / "run.json"),
        "list_runs": ("read_text", store.tool_dir("apply_patch") / "run.json"),
    }[stage]
    operation = {
        "initialize": lambda: ToolTestStore(root=store.root),
        "reset_tool": lambda: store.reset_tool("apply_patch"),
        "prepare_case": lambda: store.prepare_case_dir(tool_name="apply_patch", provider_id="provider", case_id="case"),
        "write_case": lambda: store.write_case_json(tool_name="apply_patch", provider_id="provider", case_id="case", file_name="result.json", payload={}),
        "write_run": lambda: store.write_run("run_a", payload),
        "read_run": lambda: store.read_run("run_a"),
        "list_runs": lambda: store.list_runs(),
    }[stage]
    actual = getattr(Path, method)

    def fail(path: Path, *args: object, **kwargs: object) -> object:
        if path == target:
            raise original
        return actual(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        with pytest.raises(PermissionError) as caught:
            operation()
    assert caught.value is not original and caught.value.__cause__ is original
    assert native not in str(caught.value) and str(store.root) not in str(caught.value)
    assert f"stage={stage}" in str(caught.value) and "errno=13" in str(caught.value)
    record, = [entry for entry in caplog.records if entry.name == "app.tool_testing.store"]
    assert record.exc_info is not None and record.exc_info[1] is original
    if sink_fails:
        secondary, = diagnostic_sink.errors
        assert str(secondary) in "\n".join(original.__notes__)
        assert "diagnostic_sink_error=NotADirectoryError" in str(caught.value)
        assert "diagnostic-log-secret" not in str(caught.value)
    operation()  # 故障解除后沿原入口重试，不重建另一套存储。


@pytest.mark.parametrize("value", [b'not-json /private/record', b'[]', b'\xff/private/record'])
def test_store_corrupt_record_reports_read_failure_without_location(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, value: bytes,
) -> None:
    store = ToolTestStore(root=tmp_path / "private-native")
    directory = store.reset_tool("apply_patch")
    path = directory / "run.json"
    path.write_bytes(value)
    with pytest.raises((TypeError, ValueError)) as caught:
        store.read_run("run_a")
    assert str(path) not in str(caught.value)
    assert path.read_bytes() == value
    assert any(entry.name == "app.tool_testing.store" for entry in caplog.records)
    path.write_text('{"run_id":"run_a"}', encoding="utf-8")
    assert store.read_run("run_a") == {"run_id": "run_a"}


def test_store_rejects_implicit_native_path_serialization(tmp_path: Path) -> None:
    store = ToolTestStore(root=tmp_path / "tool_tests")
    directory = store.reset_tool("apply_patch")
    payload: dict[str, object] = {"run_id": "run_a", "tool_name": "apply_patch"}
    store.write_run("run_a", payload)
    original = (directory / "run.json").read_bytes()
    with pytest.raises(TypeError, match="Path"):
        store.write_run("run_a", {**payload, "resource": tmp_path / "private-native"})
    assert (directory / "run.json").read_bytes() == original
    assert not (directory / "run.json.tmp").exists()


@pytest.mark.parametrize("query", ["get", "list", "filtered_list"])
def test_store_missing_root_is_not_an_empty_result(tmp_path: Path, query: str) -> None:
    store = ToolTestStore(root=tmp_path / "tool_tests")
    store.root.rmdir()
    with pytest.raises(FileNotFoundError) as caught:
        if query != "get":
            store.list_runs(tool_name="apply_patch" if query == "filtered_list" else None)
        else:
            store.read_run("run_a")
    assert caught.value.__cause__ is not None
    assert str(store.root) not in str(caught.value)
    store.root.mkdir()
    assert store.list_runs() == []


@pytest.mark.parametrize("filtered", [False, True])
def test_store_directory_record_is_not_silently_skipped(tmp_path: Path, filtered: bool) -> None:
    store = ToolTestStore(root=tmp_path / "tool_tests")
    directory = store.reset_tool("apply_patch")
    (directory / "run.json").mkdir()
    with pytest.raises(TypeError, match="普通文件"):
        store.list_runs(tool_name="apply_patch" if filtered else None)


@pytest.mark.parametrize("damage", ["json", "utf8", "object", "directory"])
@pytest.mark.parametrize("listing", [False, True])
def test_corrupt_record_rejection_survives_diagnostic_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, listing: bool,
) -> None:
    from app.tool_testing import store as store_module

    store = ToolTestStore(root=tmp_path / "tool_tests")
    directory = store.reset_tool("apply_patch")
    record = directory / "run.json"
    payload = {"run_id": "run_a", "tool_name": "apply_patch", "created_at": "1"}
    store.write_run("run_a", payload)
    original = record.read_bytes()
    if damage == "directory":
        record.unlink()
        record.mkdir()
        broken = None
    else:
        broken = {"json": b'{"private":"/private/record",', "utf8": b'\xff/private/record', "object": b'[]'}[damage]
        record.write_bytes(broken)
    blocker = tmp_path / "diagnostic-sink-private"
    blocker.write_text("普通文件不能作为日志目录")
    primaries: list[BaseException | None] = []
    secondaries: list[OSError] = []
    call_handlers = logging.Logger.callHandlers

    def reject_log(logger: logging.Logger, record: logging.LogRecord) -> None:
        call_handlers(logger, record)
        if record.name == store_module.logger.name:
            primaries.append(record.exc_info[1] if record.exc_info else None)
            try:
                (blocker / "service.log").write_text(record.getMessage())
            except OSError as error:
                secondaries.append(error)
                raise

    with monkeypatch.context() as patch:
        patch.setattr(logging.Logger, "callHandlers", reject_log)
        error_type = TypeError if damage in {"object", "directory"} else ValueError
        with pytest.raises(error_type) as caught:
            store.list_runs() if listing else store.read_run("run_a")
    primary, = primaries
    secondary, = secondaries
    assert primary is not None
    assert caught.value.__cause__ is primary
    assert str(secondary) in "\n".join(primary.__notes__)
    assert "diagnostic_sink_error=NotADirectoryError" in str(caught.value)
    assert "private" not in str(caught.value) and str(store.root) not in str(caught.value)
    assert (record.is_dir() if broken is None else record.read_bytes() == broken)
    if damage == "directory":
        assert str(record) in "\n".join(primary.__notes__)
        record.rmdir()
    record.write_bytes(original)
    assert store.read_run("run_a") == payload
