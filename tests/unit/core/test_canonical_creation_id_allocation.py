"""实时创建 journal 必须自然分配单调 UUIDv7，并从 ID 冻结 UTC 时间。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import identifier
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore

FIXED_CREATED_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
FIXED_EPOCH_MS = identifier.to_epoch_ms(FIXED_CREATED_AT)


def uuid7_payload(epoch_ms: int, sequence: int) -> str:
    rand_a = sequence >> 60
    rand_b = sequence & ((1 << 60) - 1)
    return f"{epoch_ms:012x}7{rand_a:03x}8{rand_b:015x}"


def monotonic_uuid7_source() -> tuple[Callable[..., object], list[str], list[str]]:
    """固定毫秒并分别提供默认有序、显式 timestamp 逆序的 UUIDv7。"""
    default_ids: list[str] = []
    explicit_ids: list[str] = []

    def uuid7(*, timestamp: int | None = None, nanos: int | None = None):
        if timestamp is None:
            sequence = len(default_ids)
            epoch_ms = FIXED_EPOCH_MS
            value = uuid7_payload(epoch_ms, sequence)
            default_ids.append(value)
        else:
            assert nanos is not None
            epoch_ms = timestamp * 1000 + nanos // 1_000_000
            sequence = 100_000 - len(explicit_ids)
            value = uuid7_payload(epoch_ms, sequence)
            explicit_ids.append(value)
        return SimpleNamespace(timestamp=epoch_ms, hex=value)

    return uuid7, default_ids, explicit_ids


def test_session_creation_journal_uses_default_monotonic_uuid7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uuid7, default_ids, explicit_ids = monotonic_uuid7_source()
    monkeypatch.setattr(identifier, "_resolve_uuid7", lambda: uuid7)
    sessions_root = tmp_path / ".boxteam" / "sessions"
    store = SessionCatalogStore(
        tmp_path / ".boxteam" / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    try:
        requests = [
            {
                "idempotency_key": f"session-{index}",
                "workspace_id": "ws",
                "parent_node_id": None,
                "display_name": f"Session {index}",
                "preimage_hash": f"preimage-{index}",
            }
            for index in range(128)
        ]
        records = [store.create_or_get_creation_record(**request) for request in requests]

        allocated_ids = [
            identity
            for record in records
            for identity in (record.session_id, record.main_thread_id)
        ]
        allocated_payloads = [identity[4:] for identity in allocated_ids]
        assert allocated_payloads == sorted(allocated_payloads)
        assert len(allocated_ids) == len(set(allocated_ids))
        assert default_ids == allocated_payloads
        assert explicit_ids == []
        assert all(
            record.created_at == FIXED_CREATED_AT.isoformat()
            and record.storage_relative_locator
            == f"sessions/2026/06/01/{record.session_id}"
            for record in records
        )
        issued_before_replay = list(default_ids)
        assert store.create_or_get_creation_record(**requests[0]) == records[0]
        assert default_ids == issued_before_replay
    finally:
        store.close()


def test_child_thread_creation_journal_uses_default_monotonic_uuid7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uuid7, default_ids, explicit_ids = monotonic_uuid7_source()
    monkeypatch.setattr(identifier, "_resolve_uuid7", lambda: uuid7)
    store = SessionControlStore(tmp_path / "session-control.sqlite")
    fixed_main_thread_id = f"thr_{uuid7_payload(FIXED_EPOCH_MS, 100_000)}"
    store.initialize_main_thread(fixed_main_thread_id, FIXED_CREATED_AT)
    store.initialize_fence("active", 1)
    try:
        requests = [
            {
                "idempotency_key": f"thread-{index}",
                "initial_state": "idle",
                "preimage_hash": f"preimage-{index}",
                "graph_binding": "{}",
                "capability_profile": "{}",
            }
            for index in range(128)
        ]
        records = [
            store.create_or_get_thread_creation_record(**request)
            for request in requests
        ]

        allocated_ids = [record.child_thread_id for record in records]
        allocated_payloads = [thread_id[4:] for thread_id in allocated_ids]
        assert allocated_payloads == sorted(allocated_payloads)
        assert len(allocated_ids) == len(set(allocated_ids))
        assert default_ids == allocated_payloads
        assert explicit_ids == []
        assert all(
            record.child_created_at == FIXED_CREATED_AT.isoformat()
            and record.final_relative_locator
            == f"threads/2026/06/01/{record.child_thread_id}"
            for record in records
        )
        issued_before_replay = list(default_ids)
        assert store.create_or_get_thread_creation_record(**requests[0]) == records[0]
        assert default_ids == issued_before_replay
    finally:
        store.close()


def test_session_journal_bucket_uses_session_id_across_midnight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_ms = identifier.to_epoch_ms(
        datetime(2026, 6, 1, 23, 59, 59, 999_000, tzinfo=UTC)
    )
    main_thread_ms = session_ms + 1
    child_thread_ms = session_ms + 2
    generated: list[str] = []
    moments = (session_ms, main_thread_ms, child_thread_ms)

    def uuid7() -> object:
        epoch_ms = moments[len(generated)]
        value = uuid7_payload(epoch_ms, 0)
        generated.append(value)
        return SimpleNamespace(timestamp=epoch_ms, hex=value)

    monkeypatch.setattr(identifier, "_resolve_uuid7", lambda: uuid7)
    store = SessionCatalogStore(
        tmp_path / ".boxteam" / "navigation" / "session-catalog.sqlite",
        tmp_path / ".boxteam" / "sessions",
    )
    control_store: SessionControlStore | None = None
    try:
        record = store.create_or_get_creation_record(
            idempotency_key="midnight-session",
            workspace_id="ws",
            parent_node_id=None,
            display_name="午夜会话",
            preimage_hash="midnight-preimage",
        )

        assert record.created_at == "2026-06-01T23:59:59.999000+00:00"
        assert record.storage_relative_locator == (
            f"sessions/2026/06/01/{record.session_id}"
        )
        assert record.main_thread_id[4:16] == f"{main_thread_ms:012x}"
        assert identifier.uuid7_datetime_from_hex(
            record.main_thread_id[4:]
        ).date().isoformat() == "2026-06-02"

        # Main row 是 Session 创建事实，沿用 ses ID 冻结的时刻；main thr
        # 可在午夜后自然分配，因为它没有独立物理日期桶。
        session_directory = (
            tmp_path
            / ".boxteam"
            / "sessions"
            / "2026"
            / "06"
            / "01"
            / record.session_id
        )
        session_directory.mkdir(parents=True)
        control_store = SessionControlStore(
            session_directory / "session-control.sqlite"
        )
        session_created_at = datetime.fromisoformat(record.created_at)
        control_store.initialize_main_thread(
            record.main_thread_id, session_created_at
        )
        control_store.initialize_fence("active", 1)
        main_row = control_store.get_main_thread()
        assert str(main_row["thread_id"]) == record.main_thread_id
        assert str(main_row["created_at"]) == record.created_at
        assert datetime.fromisoformat(str(main_row["created_at"])).date().isoformat() == (
            "2026-06-01"
        )

        child_record = control_store.create_or_get_thread_creation_record(
            idempotency_key="midnight-child",
            initial_state="idle",
            preimage_hash="a" * 64,
            graph_binding="{}",
            capability_profile="{}",
        )
        assert child_record.child_thread_id[4:16] == f"{child_thread_ms:012x}"
        assert child_record.child_created_at == "2026-06-02T00:00:00.001000+00:00"
        assert child_record.final_relative_locator == (
            f"threads/2026/06/02/{child_record.child_thread_id}"
        )
        control_store.freeze_thread_creation_artifact_manifest(
            "midnight-child",
            artifact_manifest="{}",
            artifact_manifest_hash=sha256(b"{}").hexdigest(),
        )
        control_store.publish_thread_creation_record("midnight-child")
        child_row = control_store.connection.execute(
            "SELECT thread_id, kind, created_at FROM thread_catalog "
            "WHERE kind = 'child'"
        ).fetchone()
        assert child_row is not None
        assert str(child_row["thread_id"]) == child_record.child_thread_id
        assert str(child_row["created_at"]) == child_record.child_created_at
        assert generated == [
            record.session_id[4:],
            record.main_thread_id[4:],
            child_record.child_thread_id[4:],
        ]
    finally:
        if control_store is not None:
            control_store.close()
        store.close()
