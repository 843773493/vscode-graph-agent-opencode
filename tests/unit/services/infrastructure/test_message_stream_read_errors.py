"""消息流持久读取的公开错误、原件保全及失败后的同owner重试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import app.services.infrastructure.message_stream_store as stream_module
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.services.infrastructure.message_stream_store import (
    MessageStreamError,
    MessageStreamStore,
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

MessageStreamStoreCase = tuple[MessageStreamStore, SessionCatalogPathResolver, str]


@pytest.fixture
def message_stream_store(empty_workspace_scope: tuple[str, Path]) -> MessageStreamStoreCase:
    _, workspace = empty_workspace_scope
    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
    resolver.initialize()
    session_id = "ses_00000000400070008000000000000011"
    seed_catalog_session_bundle(resolver.sessions_root, session_id)
    return MessageStreamStore(path_resolver=resolver), resolver, session_id


def damage_record(original: bytes, fault: str, marker: str) -> bytes:
    """只修改指定的磁盘故障点，不替代恢复或投影逻辑。"""
    raw = json.loads(original.splitlines()[0] if fault.startswith("events_") else original)
    if fault == "events_identity":
        raw["event"]["session_id"] = marker
    elif fault == "events_sequence":
        raw["event"]["event_seq"] = marker
    elif fault == "events_payload_identity":
        raw["event"]["block_id"] = "block_original"
        raw["event"]["payload"]["block_id"] = marker
    elif fault == "events_missing_id":
        raw["event"].pop("event_id")
        raw["diagnostic_marker"] = marker
    elif fault == "state_identity":
        raw["thread_id"] = marker
    elif fault == "state_sequence":
        raw["snapshot_seq"] = marker
    elif fault in {"state_shape", "applied_event_ids_shape"}:
        raw = [marker]
    elif fault == "index_version":
        raw["format_version"] = marker
    elif fault == "index_thread":
        raw["threads"] = {marker: {}}
    elif fault == "index_turns":
        raw["threads"] = {next(iter(raw["threads"])): marker}
    elif fault == "applied_event_ids_entry":
        raw = {marker: marker}
    else:
        raise AssertionError(f"未登记故障: {fault}")
    encoded = (json.dumps(raw, ensure_ascii=False) + "\n").encode()
    if fault.startswith("events_"):
        encoded += b"".join(original.splitlines(keepends=True)[1:])
    return encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["/private/stream-secret", r"C:\private\stream-secret", r"\\host\private\stream-secret"])
@pytest.mark.parametrize("fault", [
    "events_identity", "events_sequence", "events_payload_identity", "events_missing_id",
    "state_identity", "state_sequence", "state_shape", "index_version", "index_thread", "index_turns",
    "applied_event_ids_shape", "applied_event_ids_entry",
])
async def test_corrupt_read_keeps_diagnostics_and_rejects_again_without_partial_cache(
    message_stream_store: MessageStreamStoreCase, caplog: pytest.LogCaptureFixture,
    fault: str, marker: str,
) -> None:
    store, resolver, session_id = message_stream_store
    thread = resolver.main_thread_id(session_id)
    writer = await store.open(session_id=session_id, thread_id=thread, turn_id="turn_read_errors")
    await writer.close_completed()
    await asyncio.gather(*store._snapshot_tasks.values())
    expected = await store.get_state(writer.turn_stream_id)
    paths = {
        "events": store._stream_path(session_id, writer.turn_stream_id),
        "state": store._state_path(session_id, writer.turn_stream_id),
        "index": store._index_path(session_id),
        "applied_event_ids": store._applied_event_ids_path(session_id, writer.turn_stream_id),
    }
    # 普通事件没有显式幂等键时文件可以尚未创建；为空的索引仍是合法读取输入。
    if not paths["applied_event_ids"].exists():
        paths["applied_event_ids"].write_text("{}", encoding="utf-8")
    originals = {name: path.read_bytes() for name, path in paths.items()}
    artifact = next(name for name in paths if fault.startswith(name + "_"))
    damaged = damage_record(originals[artifact], fault, marker)
    paths[artifact].write_bytes(damaged)
    cold = MessageStreamStore(path_resolver=resolver)
    try:
        for _ in range(2):
            with pytest.raises(MessageStreamError) as caught:
                await cold.open_existing(session_id=session_id, thread_id=thread, turn_id=writer.turn_id)
            public = str(caught.value)
            assert str(paths[artifact]) not in public and str(resolver.sessions_root) not in public
            assert marker not in public
            assert f"session_id={session_id}" in public and f"artifact={artifact}" in public
            assert not cold._states and not cold._event_ids and not cold._event_ids_loaded
            assert paths[artifact].read_bytes() == damaged
        assert repr(marker) in caplog.text
    finally:
        for name, path in paths.items():
            path.write_bytes(originals[name])
    reopened = await cold.open_existing(session_id=session_id, thread_id=thread, turn_id=writer.turn_id)
    assert reopened.turn_stream_id == writer.turn_stream_id
    assert await cold.get_state(writer.turn_stream_id) == expected
    assert {name: path.read_bytes() for name, path in paths.items()} == originals


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["events", "state", "index", "applied_event_ids"])
async def test_read_os_failure_keeps_original_cause_and_explicit_retry(
    message_stream_store: MessageStreamStoreCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, artifact: str,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    writer = await store.open(session_id=session, thread_id=thread, turn_id="turn_read_os_error")
    await writer.close_completed()
    await asyncio.gather(*store._snapshot_tasks.values())
    paths = {
        "events": store._stream_path(session, writer.turn_stream_id),
        "state": store._state_path(session, writer.turn_stream_id),
        "index": store._index_path(session),
        "applied_event_ids": store._applied_event_ids_path(session, writer.turn_stream_id),
    }
    paths["applied_event_ids"].write_text("{}", encoding="utf-8")
    target = paths[artifact]
    raw_open = Path.open
    original_error = PermissionError(13, "真实读取失败", str(target))

    def failed_open(path: Path, *args, **kwargs):
        if path == target:
            raise original_error
        return raw_open(path, *args, **kwargs)

    cold = MessageStreamStore(path_resolver=resolver)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", failed_open)
        with pytest.raises(MessageStreamError) as caught:
            await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
    assert caught.value.__cause__ is original_error
    assert str(target) not in str(caught.value) and f"artifact={artifact}" in str(caught.value)
    assert str(target) in caplog.text
    assert not cold._states
    assert (await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)).turn_stream_id == writer.turn_stream_id


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("field", [
    "session_id", "thread_id", "turn_id", "turn_stream_id", "event_id", "map_key",
    "event_seq", "payload", "payload_identity", "type",
])
async def test_applied_events_reject_invalid_owner_before_publishing_recovery(
    message_stream_store: MessageStreamStoreCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, compact: bool, field: str,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    writer = await store.open(session_id=session, thread_id=thread, turn_id="turn_applied_owner")
    event = await writer.commit("block.delta", {
        "block_id": "block_owner", "operation": "append", "text": "ORIGINAL",
    }, block_id="block_owner", event_id="evt_owner_original")
    if compact:
        monkeypatch.setattr(stream_module, "MESSAGE_STREAM_MAX_BYTES", 1_200)
        monkeypatch.setattr(stream_module, "MESSAGE_STREAM_RETAINED_BYTES", 500)
        for _ in range(8):
            await writer.commit("block.delta", {
                "block_id": "block_owner", "operation": "append", "text": "." * 30,
            }, block_id="block_owner")
    await asyncio.gather(*store._snapshot_tasks.values())
    expected = await store.get_state(writer.turn_stream_id)
    log = store._stream_path(session, writer.turn_stream_id)
    assert (event["event_id"] not in log.read_text()) is compact
    sidecar = store._applied_event_ids_path(session, writer.turn_stream_id)
    original = sidecar.read_bytes()
    raw = json.loads(original)
    value = raw[event["event_id"]]
    marker = "/private/foreign-stream-owner"
    if field == "map_key":
        raw = {marker: value}
    elif field == "payload_identity":
        value["payload"]["block_id"] = marker
    elif field == "payload":
        value[field] = [marker]
    elif field == "event_seq":
        value[field] = -1
    elif field == "type":
        value[field] = ""
    else:
        value[field] = marker
    sidecar.write_text(json.dumps(raw), encoding="utf-8")
    originals = {p: p.read_bytes() for p in sidecar.parent.iterdir() if p.is_file()}
    cold = MessageStreamStore(path_resolver=resolver)
    try:
        for _ in range(2):
            with pytest.raises(MessageStreamError) as caught:
                await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
            assert "artifact=applied_event_ids" in str(caught.value)
            assert marker not in str(caught.value) and str(sidecar) not in str(caught.value)
            assert not cold._states and not cold._event_ids and not cold._event_ids_loaded
            assert {p: p.read_bytes() for p in originals} == originals
        assert "已应用" in caplog.text
    finally:
        sidecar.write_bytes(original)
    reopened = await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
    assert await cold.get_state(reopened.turn_stream_id) == expected
    duplicate = await reopened.commit("block.delta", event["payload"],
                                      block_id="block_owner", event_id=event["event_id"])
    assert duplicate == event
    assert await cold.get_state(reopened.turn_stream_id) == expected


@pytest.mark.asyncio
async def test_warm_append_rejects_foreign_applied_event_before_touching_log(
    message_stream_store: MessageStreamStoreCase,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    writer = await store.open(session_id=session, thread_id=thread, turn_id="turn_warm_applied")
    event = await writer.commit("block.delta", {
        "block_id": "block_warm", "operation": "append", "text": "ORIGINAL",
    }, block_id="block_warm", event_id="evt_warm_original")
    await asyncio.gather(*store._snapshot_tasks.values())
    sidecar = store._applied_event_ids_path(session, writer.turn_stream_id)
    original = sidecar.read_bytes()
    raw = json.loads(original)
    raw[event["event_id"]]["thread_id"] = "/private/foreign-warm-owner"
    sidecar.write_text(json.dumps(raw), encoding="utf-8")
    originals = {p: p.read_bytes() for p in sidecar.parent.iterdir() if p.is_file()}
    try:
        with pytest.raises(MessageStreamError, match="artifact=applied_event_ids"):
            await writer.commit("block.delta", {
                "block_id": "block_warm", "operation": "append", "text": "NEW",
            }, block_id="block_warm", event_id="evt_warm_new")
        assert {p: p.read_bytes() for p in originals} == originals
    finally:
        sidecar.write_bytes(original)
    writer = await store.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
    accepted = await writer.commit("block.delta", {
        "block_id": "block_warm", "operation": "append", "text": "NEW",
    }, block_id="block_warm", event_id="evt_warm_new")
    assert accepted["event_seq"] == event["event_seq"] + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completed", "lost", "resumed"])
@pytest.mark.parametrize("fault", ["state_identity", "state_sequence", "applied_owner"])
async def test_startup_recovery_uses_full_cold_validation_before_any_publication(
    message_stream_store: MessageStreamStoreCase, mode: str, fault: str,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    job = "job_startup_read_validation"
    writer = await store.open(session_id=session, thread_id=thread,
                              turn_id="turn_startup_read_validation", job_id=job)
    first = await writer.commit("block.delta", {
        "block_id": "block_startup", "operation": "append", "text": "ORIGINAL",
    }, block_id="block_startup", event_id="evt_startup_original")
    # 使用真实快照触发点得到与日志同一高水位的快照，不手填恢复状态。
    for _ in range(stream_module.MESSAGE_STREAM_SNAPSHOT_INTERVAL_EVENTS - 2):
        await writer.commit("block.delta", {
            "block_id": "block_startup", "operation": "append", "text": ".",
        }, block_id="block_startup")
    if mode == "completed":
        await writer.close_completed()
    await asyncio.gather(*store._snapshot_tasks.values())
    expected = await store.get_state(writer.turn_stream_id)
    state_path = store._state_path(session, writer.turn_stream_id)
    applied_path = store._applied_event_ids_path(session, writer.turn_stream_id)
    target = applied_path if fault == "applied_owner" else state_path
    original = target.read_bytes()
    raw = json.loads(original)
    marker = "/private/startup-foreign-owner"
    if fault == "state_identity":
        raw["thread_id"] = marker
    elif fault == "state_sequence":
        raw["snapshot_seq"] = expected["snapshot_seq"] + 1
    else:
        raw[first["event_id"]]["thread_id"] = marker
    target.write_text(json.dumps(raw), encoding="utf-8")
    originals = {p: p.read_bytes() for p in target.parent.iterdir() if p.is_file()}
    resumed = frozenset({job}) if mode == "resumed" else frozenset()
    cold = MessageStreamStore(path_resolver=resolver)
    try:
        for _ in range(2):
            with pytest.raises(MessageStreamError) as caught:
                await cold.reconcile_unfinished_streams(resumed_job_ids=resumed)
            assert f"session_id={session}" in str(caught.value)
            assert marker not in str(caught.value) and str(target) not in str(caught.value)
            assert not cold._states and not cold._event_ids and not cold._event_ids_loaded
            assert {p: p.read_bytes() for p in target.parent.iterdir() if p.is_file()} == originals
    finally:
        target.write_bytes(original)
    assert await cold.reconcile_unfinished_streams(resumed_job_ids=resumed) == (1 if mode == "lost" else 0)
    if mode == "completed":
        assert not cold._states and not cold._event_ids
    reopened = await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
    recovered = await cold.get_state(reopened.turn_stream_id)
    if mode == "lost":
        assert recovered["stream_status"] == "failed"
        assert recovered["failure"]["code"] == "execution_lost"
        assert recovered["snapshot_seq"] > expected["snapshot_seq"]
    else:
        assert recovered == expected
    assert await reopened.commit("block.delta", first["payload"],
                                 block_id="block_startup", event_id=first["event_id"]) == first
    before_second = store._stream_path(session, writer.turn_stream_id).read_bytes()
    assert await cold.reconcile_unfinished_streams(resumed_job_ids=resumed) == 0
    assert store._stream_path(session, writer.turn_stream_id).read_bytes() == before_second


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [b"", b"\n  \n"])
async def test_startup_still_skips_logs_without_committed_events(
    message_stream_store: MessageStreamStoreCase, empty: bytes,
) -> None:
    store, _, session = message_stream_store
    path = store._stream_path(session, "strm_empty_startup")
    path.parent.mkdir(parents=True)
    path.write_bytes(empty)
    assert await store.reconcile_unfinished_streams() == 0
    assert path.read_bytes() == empty
    assert not store._states and not store._event_ids and not store._event_ids_loaded


_INVALID_STREAM_LOCATORS = [
    "absolute", "../foreign-stream", r"..\foreign-stream", r"C:\private\foreign-stream",
    "C:foreign-stream", r"\\host\share\foreign-stream", "boxteam://invalid/stream", "",
    ".", "..", "stream\x00tail", "stream\ntail",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["request", "index"])
@pytest.mark.parametrize("value", _INVALID_STREAM_LOCATORS)
async def test_stream_locator_rejects_native_values_before_read_or_fallback(
    message_stream_store: MessageStreamStoreCase, monkeypatch: pytest.MonkeyPatch,
    source: str, value: str,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    writer = await store.open(session_id=session, thread_id=thread, turn_id="turn_locator")
    await writer.close_completed()
    await asyncio.gather(*store._snapshot_tasks.values())
    index = store._index_path(session)
    original_index = index.read_bytes()
    foreign = resolver.sessions_root / "foreign-stream.jsonl"
    foreign.write_bytes(b"[]\n")
    value = str(foreign.with_suffix("")) if value == "absolute" else value
    if source == "index":
        raw = json.loads(original_index)
        raw["threads"][thread][writer.turn_id] = value
        index.write_text(json.dumps(raw), encoding="utf-8")
    damaged_index = index.read_bytes()
    opened = []
    original_open = Path.open

    def observe_open(path: Path, *args, **kwargs):
        if path == foreign:
            opened.append(path)
        return original_open(path, *args, **kwargs)

    cold = MessageStreamStore(path_resolver=resolver)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", observe_open)
            for _ in range(2):
                arguments = {"turn_stream_id": value} if source == "request" else {}
                with pytest.raises(MessageStreamError) as caught:
                    await cold.open_existing(session_id=session, thread_id=thread,
                                             turn_id=writer.turn_id, **arguments)
                public = str(caught.value)
                assert "field=turn_stream_id" in public and f"session_id={session}" in public
                if value:
                    assert repr(value) not in public and value not in public
                assert not cold._states and not cold._event_ids
                assert not opened
                assert index.read_bytes() == damaged_index
    finally:
        index.write_bytes(original_index)
    assert foreign.read_bytes() == b"[]\n"
    reopened = await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id)
    assert reopened.turn_stream_id == writer.turn_stream_id


@pytest.mark.parametrize("builder", ["_stream_path", "_state_path", "_applied_event_ids_path"])
@pytest.mark.parametrize("value", _INVALID_STREAM_LOCATORS)
def test_all_stream_file_templates_reject_locator_values(
    message_stream_store: MessageStreamStoreCase, builder: str, value: str,
) -> None:
    store, _, session = message_stream_store
    value = "/private/foreign-stream" if value == "absolute" else value
    with pytest.raises(MessageStreamError, match="field=turn_stream_id"):
        getattr(store, builder)(session, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_id", ["legacy-stream.v4", "stream_custom_label"])
async def test_existing_opaque_stream_labels_keep_same_storage_and_restore_contract(
    message_stream_store: MessageStreamStoreCase, monkeypatch: pytest.MonkeyPatch, stream_id: str,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    generate = stream_module.create_prefixed_id
    with monkeypatch.context() as patch:
        patch.setattr(stream_module, "create_prefixed_id", lambda prefix: stream_id if prefix == "strm" else generate(prefix))
        writer = await store.open(session_id=session, thread_id=thread, turn_id="turn_opaque_label")
        await writer.close_completed()
    await asyncio.gather(*store._snapshot_tasks.values())
    cold = MessageStreamStore(path_resolver=resolver)
    reopened = await cold.open_existing(session_id=session, thread_id=thread, turn_id=writer.turn_id,
                                        turn_stream_id=stream_id)
    assert reopened.turn_stream_id == stream_id
    assert await cold.get_state(stream_id) == await store.get_state(stream_id)
    assert await cold.reconcile_unfinished_streams() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_id", ["absolute", ""])
async def test_generated_invalid_stream_id_is_rejected_before_index_or_cache_publish(
    message_stream_store: MessageStreamStoreCase, monkeypatch: pytest.MonkeyPatch, stream_id: str,
) -> None:
    store, resolver, session = message_stream_store
    thread = resolver.main_thread_id(session)
    stream_id = str(resolver.sessions_root / "generated-stream") if stream_id == "absolute" else stream_id
    # 冻结文件前先完成原本必需的Thread查询，包含SQLite读侧的首次WAL/SHM建立。
    resolver.resolve_thread_node(session, thread)
    node = resolver.resolve_session_node(session)
    original = {p: p.read_bytes() for p in node.rglob("*") if p.is_file()}
    generate = stream_module.create_prefixed_id
    monkeypatch.setattr(stream_module, "create_prefixed_id", lambda prefix: stream_id if prefix == "strm" else generate(prefix))
    with pytest.raises(MessageStreamError, match="field=turn_stream_id"):
        await store.open(session_id=session, thread_id=thread, turn_id="turn_invalid_generated_id")
    assert not store._states and not store._event_ids
    assert {p: p.read_bytes() for p in node.rglob("*") if p.is_file()} == original
