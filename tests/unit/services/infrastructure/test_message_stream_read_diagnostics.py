"""持久读取拒绝和真实日志写入同时失败，不能丢失原错误或发布候选缓存。"""

from __future__ import annotations

import asyncio
import json
import logging
from io import TextIOBase
from pathlib import Path
from traceback import format_exception

import pytest

from app.core.logging_config import _RuntimeHandler
from app.services.infrastructure.message_stream_store import (
    MessageStreamError,
    MessageStreamStore,
)
from tests.unit.services.infrastructure.test_message_stream_store import (
    MessageStreamStoreCase,
)
from tests.unit.services.infrastructure.test_message_stream_store import (
    message_stream_store as message_stream_store,  # noqa: PLC0414 - 原 owner 与隔离工作区
)


@pytest.mark.parametrize("sink", ["healthy", "not_directory", "missing"])
@pytest.mark.parametrize("fault", [
    "stream_id", "index_json", "index_shape", "index_id", "events_shape", "events_middle",
    "events_owner", "state_json", "state_shape", "state_owner", "state_seq", "workspace",
    "applied_json", "applied_shape", "applied_value", "applied_key", "applied_owner",
])
async def test_cold_read_rejection_survives_diagnostic_sink_failure(
    message_stream_store: MessageStreamStoreCase, fault: str, sink: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    writer = await store.open(session_id=session, thread_id=thread, turn_id="read-diagnostics")
    await writer.close_completed()
    if store._snapshot_tasks:
        await asyncio.gather(*store._snapshot_tasks.values())
    expected = await store.list_events(session_id=session, turn_stream_id=writer.turn_stream_id)
    paths = {
        "index": store._index_path(session),
        "events": store._stream_path(session, writer.turn_stream_id),
        "state": store._state_path(session, writer.turn_stream_id),
        "applied": store._applied_event_ids_path(session, writer.turn_stream_id),
    }
    originals = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    secret = str(resolver.resolve_session_node(session) / "rejected-owner-secret")
    artifact = fault.split("_")[0]
    stream_id = writer.turn_stream_id
    if fault == "stream_id":
        stream_id = secret
    elif fault == "workspace":
        artifact = "state"
        state = json.loads(paths["state"].read_text())
        state["workspace_id"] = secret
        paths["state"].write_text(json.dumps(state))
    elif fault.startswith("state_"):
        state = json.loads(paths["state"].read_text())
        if fault == "state_owner":
            state["thread_id"] = secret
        elif fault == "state_seq":
            state["snapshot_seq"] = secret
        paths["state"].write_text(
            "{" if fault == "state_json" else json.dumps([] if fault == "state_shape" else state),
        )
    elif fault.startswith("index_"):
        index = json.loads(paths["index"].read_text())
        if fault == "index_id":
            index["threads"][thread][writer.turn_id] = secret
        paths["index"].write_text(
            "{" if fault == "index_json" else json.dumps([] if fault == "index_shape" else index),
        )
    elif fault.startswith("events_"):
        records = [json.loads(line) for line in paths["events"].read_text().splitlines()]
        if fault == "events_owner":
            records[0]["event"]["session_id"] = secret
        elif fault == "events_shape":
            records[0] = []
        paths["events"].write_text(
            ("{\n" if fault == "events_middle" else json.dumps(records[0]) + "\n")
            + "\n".join(json.dumps(record) for record in records[1:]) + "\n",
        )
    else:
        event = dict(expected[0])
        if fault == "applied_owner":
            event["thread_id"] = secret
        applied = {secret if fault == "applied_key" else event["event_id"]:
                   secret if fault == "applied_value" else event}
        paths["applied"].write_text(
            "{" if fault == "applied_json" else json.dumps([] if fault == "applied_shape" else applied),
        )
    damaged = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    blocker = resolver.resolve_session_node(session) / "diagnostic-sink-secret"
    if sink == "not_directory":
        blocker.write_text("普通文件不能作为日志目录")
    destination = blocker / "service.log"
    failures: list[OSError] = []
    emit = caplog.handler.emit

    def capture(record: logging.LogRecord) -> None:
        emit(record)
        if record.name == "app.services.infrastructure.message_stream_store" and sink != "healthy":
            try:
                destination.write_text(record.getMessage())
            except OSError as error:
                failures.append(error)
                raise

    reader = MessageStreamStore(path_resolver=resolver, workspace_id="expected-workspace")
    try:
        with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
            patch.setattr(caplog.handler, "emit", capture)
            with pytest.raises(MessageStreamError) as caught:
                await reader.open_existing(
                    session_id=session, thread_id=thread, turn_id=writer.turn_id, turn_stream_id=stream_id,
                )
        error = caught.value
        assert f"session_id={session}" in str(error)
        if fault != "workspace":
            expected_artifact = {"applied": "applied_event_ids", "stream": "request"}.get(artifact, artifact)
            assert f"artifact={expected_artifact}" in str(error)
        for private in (secret, str(destination), str(resolver.resolve_session_node(session))):
            assert private not in str(error)
        records = [record for record in caplog.records
                   if record.name == "app.services.infrastructure.message_stream_store"]
        assert records and all(record.exc_info is not None for record in records)
        diagnostic = "".join(format_exception(error))
        assert secret in diagnostic or fault in {"index_json", "index_shape", "events_shape", "events_middle",
                                                "state_json", "state_shape", "applied_json", "applied_shape"}
        if sink == "healthy":
            assert not failures and "diagnostic_sink_error" not in str(error)
        else:
            assert failures
            assert all(str(failure) in diagnostic for failure in failures)
            assert "diagnostic_sink_error=" + type(failures[0]).__name__ in str(error)
        assert writer.turn_stream_id not in reader._states
        assert writer.turn_stream_id not in reader._event_ids_loaded
        assert {name: path.read_bytes() if path.exists() else None for name, path in paths.items()} == damaged
    finally:
        for name, original in originals.items():
            if original is None:
                paths[name].unlink(missing_ok=True)
            else:
                paths[name].write_bytes(original)
    reopened = MessageStreamStore(path_resolver=resolver)
    await reopened.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
    assert await reopened.list_events(session_id=session, turn_stream_id=writer.turn_stream_id) == expected


@pytest.mark.parametrize("artifact", ["events", "state", "index", "applied_event_ids"])
@pytest.mark.parametrize("sink", ["healthy", "not_directory", "missing"])
async def test_native_read_failure_keeps_exact_cause_and_all_original_bytes(
    message_stream_store: MessageStreamStoreCase, artifact: str, sink: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    writer = await store.open(session_id=session, thread_id=thread, turn_id="native-read")
    await writer.close_completed()
    if store._snapshot_tasks:
        await asyncio.gather(*store._snapshot_tasks.values())
    events = await store.list_events(session_id=session, turn_stream_id=writer.turn_stream_id)
    paths = {
        "events": store._stream_path(session, writer.turn_stream_id),
        "state": store._state_path(session, writer.turn_stream_id),
        "index": store._index_path(session),
        "applied_event_ids": store._applied_event_ids_path(session, writer.turn_stream_id),
    }
    target = paths[artifact]
    if not target.exists():
        target.write_text("{}")
    originals = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    blocker = resolver.resolve_session_node(session) / "native-read-private"
    blocker.write_text("普通文件不能作为读取目录")
    native_destination = blocker / "stream.json"
    log_blocker = blocker.with_name("native-log-private")
    if sink == "not_directory":
        log_blocker.write_text("普通文件不能作为日志目录")
    log_destination = log_blocker / "service.log"
    native_errors: list[OSError] = []
    sink_errors: list[OSError] = []
    open_file = Path.open

    def read_file(path: Path, *args, **kwargs):
        if path != target:
            return open_file(path, *args, **kwargs)
        try:
            return open_file(native_destination, *args, **kwargs)
        except OSError as error:
            native_errors.append(error)
            raise

    class FileSink(TextIOBase):
        def write(self, text: str) -> int:
            try:
                return log_destination.write_text(text)
            except OSError as error:
                sink_errors.append(error)
                raise

    diagnostic_logger = logging.getLogger("app.services.infrastructure.message_stream_store")
    handlers: list[logging.Handler] = [caplog.handler]
    if sink != "healthy":
        handlers.append(_RuntimeHandler(FileSink()))
    cold = MessageStreamStore(path_resolver=resolver)
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(Path, "open", read_file)
        patch.setattr(diagnostic_logger, "handlers", handlers)
        patch.setattr(diagnostic_logger, "propagate", False)
        with pytest.raises(MessageStreamError) as caught:
            await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
    original, = native_errors
    assert isinstance(original, NotADirectoryError) and caught.value.__cause__ is original
    assert f"artifact={artifact}" in str(caught.value) and f"session_id={session}" in str(caught.value)
    assert str(native_destination) not in str(caught.value) and str(log_destination) not in str(caught.value)
    record, = [record for record in caplog.records
               if record.name == "app.services.infrastructure.message_stream_store"]
    assert record.exc_info is not None and record.exc_info[1] is original
    if sink == "healthy":
        assert not sink_errors and "diagnostic_sink_error" not in str(caught.value)
    else:
        failure, = sink_errors
        assert "diagnostic_sink_error=" + type(failure).__name__ in str(caught.value)
        assert str(failure) in "".join(format_exception(caught.value))
    assert {name: path.read_bytes() if path.exists() else None for name, path in paths.items()} == originals
    assert writer.turn_stream_id not in cold._states and writer.turn_stream_id not in cold._event_ids_loaded
    await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
    assert await cold.list_events(session_id=session, turn_stream_id=writer.turn_stream_id) == events
