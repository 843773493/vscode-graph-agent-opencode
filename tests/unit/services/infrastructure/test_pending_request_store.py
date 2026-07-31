import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.public_v2.pending_request import PendingRequestDTO
from app.services.infrastructure.pending_request_store import PendingRequestStore


@pytest.mark.asyncio
async def test_pending_request_store_round_trip(tmp_path, session_bundle_factory):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_restore"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    request = PendingRequestDTO(
        job_id="job_restore",
        message_id="msg_restore",
        session_id=session_id,
        content="重启后继续保留",
        kind="steering",
        position=0,
        agent_id="default",
        message_created_at="2026-07-17T00:00:00+00:00",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    await store.save(session_id, [request])
    restored = await store.load(session_id)

    assert restored == [request]
    assert (session_dir / "pending_requests.json").is_file()


@pytest.mark.asyncio
async def test_pending_summary_read_is_bounded_and_skips_full_detail(
    tmp_path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_pending_summary"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    now = datetime.now(UTC)
    requests = [
        PendingRequestDTO(
            job_id=f"job_{index}",
            message_id=f"msg_{index}",
            session_id=session_id,
            content="x" * (512 * 1024),
            kind="queued",
            position=index,
            agent_id="default",
            message_created_at=now.isoformat(),
            message_metadata={"large": "y" * 128 * 1024},
            created_at=now,
            updated_at=now,
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
        f"job_{index}" for index in range(8)
    ]
    assert summaries.request_count == 40
    assert summaries.truncated is True
    assert bytes_read <= 64 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_payload", [[], None])
async def test_pending_store_explicitly_migrates_legacy_array_schema(
    tmp_path,
    session_bundle_factory,
    legacy_payload,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_pending_legacy"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    now = datetime.now(UTC)
    requests = (
        []
        if legacy_payload == []
        else [
            PendingRequestDTO(
                job_id="job_legacy",
                message_id="msg_legacy",
                session_id=session_id,
                content="x" * (2 * 1024 * 1024),
                kind="queued",
                position=0,
                agent_id="default",
                message_created_at=now.isoformat(),
                created_at=now,
                updated_at=now,
            )
        ]
    )
    path = session_dir / "pending_requests.json"
    path.write_text(
        json.dumps(
            [request.model_dump(mode="json") for request in requests],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    await store.migrate_schema(session_id)
    summaries = await store.load_summaries(session_id, limit=8)
    restored = await store.load(session_id)

    assert summaries.request_count == len(requests)
    assert [item.job_id for item in summaries.requests] == [
        request.job_id for request in requests
    ]
    assert restored == requests
    with path.open("rb") as stream:
        assert json.loads(stream.readline())["version"] == 1


@pytest.mark.asyncio
async def test_pending_schema_migration_is_atomic_when_publish_fails(
    tmp_path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_pending_migration_crash"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    path = session_dir / "pending_requests.json"
    legacy = "[]\n"
    path.write_text(legacy, encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("模拟 pending schema 原子发布前崩溃")

    monkeypatch.setattr(
        "app.services.infrastructure.pending_request_store.os.replace",
        fail_replace,
    )
    with pytest.raises(OSError, match="原子发布前崩溃"):
        await store.migrate_schema(session_id)

    assert path.read_text(encoding="utf-8") == legacy
    assert not list(session_dir.glob(".pending_requests.json.*"))


@pytest.mark.asyncio
async def test_pending_schema_migration_runs_explicitly_for_all_sessions_at_startup(
    tmp_path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_ids = ("ses_legacy_a", "ses_legacy_b")
    for session_id in session_ids:
        session_dir = session_bundle_factory(sessions_dir, session_id)
        (session_dir / "pending_requests.json").write_text(
            json.dumps([], indent=2) + "\n",
            encoding="utf-8",
        )
    store = PendingRequestStore(sessions_dir=sessions_dir)

    migrated = await store.migrate_all()

    assert migrated == 2
    assert await store.migrate_all() == 0
    for session_id in session_ids:
        assert (await store.load_summaries(session_id, limit=8)).request_count == 0
