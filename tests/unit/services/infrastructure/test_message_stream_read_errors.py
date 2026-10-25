"""消息流持久读取的公开错误、原件保全及失败后的同owner重试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

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
