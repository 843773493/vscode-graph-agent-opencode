import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.internal_v2.pending_request import PendingRequestDTO
from app.services.infrastructure.pending_request_store import PendingRequestStore


def _request(
    session_id: str,
    *,
    sequence: int,
    content: str = "内容",
    gateway_id: str | None = "gateway_test",
    request_id: str | None = "request_test",
) -> PendingRequestDTO:
    now = datetime.now(UTC)
    return PendingRequestDTO(
        job_id=f"job_{sequence}",
        message_id=f"msg_{sequence}",
        session_id=session_id,
        content=content,
        delivery_policy="after_turn",
        enqueue_sequence=sequence,
        position=sequence - 1,
        agent_id="default",
        message_created_at=now.isoformat(),
        created_at=now,
        updated_at=now,
        snapshot_version=sequence,
        gateway_id=gateway_id,
        request_id=request_id,
    )


@pytest.mark.asyncio
async def test_pending_request_store_round_trip(tmp_path, session_bundle_factory):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    request = _request(session_id, sequence=1)

    await store.save(session_id, [request])
    restored = await store.load(session_id)

    assert restored == [request]
    assert (session_dir / "pending_requests.json").is_file()


@pytest.mark.asyncio
async def test_pending_request_store_round_trip_preserves_gateway_id(
    tmp_path,
    session_bundle_factory,
):
    """gateway_id 是 Job 作为独立执行根的身份，落盘再读回必须逐字一致。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    request = _request(session_id, sequence=1, gateway_id="gateway_abcd1234")

    await store.save(session_id, [request])
    restored = await store.load(session_id)

    assert [item.gateway_id for item in restored] == ["gateway_abcd1234"]


@pytest.mark.asyncio
async def test_pending_request_store_round_trip_preserves_request_id(
    tmp_path,
    session_bundle_factory,
):
    """request_id 是 Job 作为独立执行根的身份，落盘再读回必须逐字一致。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_00000000f00070008000000000c4a1d2"
    session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    request = _request(session_id, sequence=1, request_id="request_abcd1234")

    await store.save(session_id, [request])
    restored = await store.load(session_id)

    assert [item.request_id for item in restored] == ["request_abcd1234"]


@pytest.mark.asyncio
async def test_pending_request_store_rejects_record_missing_gateway_id(
    tmp_path,
    session_bundle_factory,
):
    """老数据缺 gateway_id 时必须诚实失败，绝不静默补字面量或默认值。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b893703987b7faae8c21f93eddc34"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(session_id, [_request(session_id, sequence=1)])
    path = session_dir / "pending_requests.json"
    header, detail = path.read_text(encoding="utf-8").split("\n", 1)
    records = json.loads(detail)
    records[0].pop("gateway_id")
    path.write_text(
        header + "\n" + json.dumps(records, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="待处理队列恢复失败"):
        await store.load(session_id)


@pytest.mark.asyncio
async def test_pending_request_store_rejects_record_missing_request_id(
    tmp_path,
    session_bundle_factory,
):
    """老数据缺 request_id 时必须诚实失败，绝不静默补字面量或默认值。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_00000000f00070008000000000c4a1d2"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(session_id, [_request(session_id, sequence=1)])
    path = session_dir / "pending_requests.json"
    header, detail = path.read_text(encoding="utf-8").split("\n", 1)
    records = json.loads(detail)
    records[0].pop("request_id")
    path.write_text(
        header + "\n" + json.dumps(records, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="待处理队列恢复失败"):
        await store.load(session_id)


@pytest.mark.asyncio
async def test_pending_summary_read_is_bounded_and_skips_full_detail(
    tmp_path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b893703987b7faae8c21f93eddc34"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    requests = [
        _request(
            session_id,
            sequence=index + 1,
            content="x" * (512 * 1024),
        )
        for index in range(40)
    ]
    await store.save(session_id, requests)
    pending_path = session_dir / "pending_requests.json"
    original_open = Path.open
    bytes_read = 0

    class BoundedHeaderReader:
        def __init__(self, stream) -> None:
            self._stream = stream

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def readline(self, size: int = -1):
            nonlocal bytes_read
            assert 0 < size <= 64 * 1024 + 1
            payload = self._stream.readline(size)
            bytes_read += len(payload)
            return payload

        def read(self, *args, **kwargs):
            raise AssertionError("摘要读取不得加载完整 pending detail")

    def bounded_open(path: Path, mode: str = "r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path == pending_path and mode == "rb":
            return BoundedHeaderReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", bounded_open)
    summaries = await store.load_summaries(session_id, limit=8)

    assert [item.job_id for item in summaries.requests] == [
        f"job_{index}" for index in range(1, 9)
    ]
    assert summaries.request_count == 40
    assert summaries.truncated is True
    assert bytes_read <= 64 * 1024


@pytest.mark.asyncio
async def test_legacy_pending_schema_is_rejected_without_compatibility_migration(
    tmp_path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c68b3cc6b735b85733e284efa48c3"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    path = session_dir / "pending_requests.json"
    legacy = json.dumps(
        [{"job_id": "legacy", "kind": "steering"}],
        ensure_ascii=False,
    ) + "\n"
    path.write_text(legacy, encoding="utf-8")

    with pytest.raises(RuntimeError, match="旧 pending kind"):
        await store.migrate_schema(session_id)
    assert path.read_text(encoding="utf-8") == legacy


@pytest.mark.asyncio
async def test_store_rejects_duplicate_queue_sequences(
    tmp_path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c31e67b4172ddbaf02f165d52ef8f"
    session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)

    with pytest.raises(RuntimeError, match="重复入队序号"):
        await store.save(
            session_id,
            [_request(session_id, sequence=1), _request(session_id, sequence=1)],
        )
