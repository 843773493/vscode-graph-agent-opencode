import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.abstractions.turn_history import (
    TurnProjectionMutation,
    TurnProjectionOperation,
    TurnProjectionPatch,
    TurnProjectionPublicationConflict,
)
from app.core.path_utils import get_session_path_resolver
from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.turn import TurnDetailDTO, TurnUserMessageDTO
from app.services.infrastructure.turn_history import (
    StaleTurnCursorError,
    TurnHistoryStore,
)
from app.services.infrastructure.turn_history.files import MAX_TURN_HEADER_BYTES


@pytest.fixture
def turn_store(
    tmp_path: Path,
    session_bundle_factory,
) -> tuple[TurnHistoryStore, Path]:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    return TurnHistoryStore(sessions_dir, compaction_threshold=3), sessions_dir


def _turn(index: int, *, revision: int = 1) -> TurnDetailDTO:
    created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    return TurnDetailDTO(
        turn_id=f"job_{index}",
        job_id=f"job_{index}",
        session_id="session_1",
        ordinal=index,
        revision=revision,
        status=JobStatus.completed,
        source_message_ids=[f"message_{index}"],
        user_messages=[
            TurnUserMessageDTO(
                message_id=f"message_{index}",
                content=f"问题 {index}",
                created_at=created_at,
            )
        ],
        final_response=f"回答 {index}",
        created_at=created_at,
        updated_at=created_at,
        completed_at=created_at,
    )


def _apply(store: TurnHistoryStore, turn: TurnDetailDTO, event_id: str) -> None:
    current = store.get_turn("session_1", turn.turn_id)
    mutation = (
        TurnProjectionMutation(
            turn_id=turn.turn_id,
            base_revision=0,
            create=turn,
        )
        if current is None
        else TurnProjectionMutation(
            turn_id=turn.turn_id,
            base_revision=current.revision,
            patch=TurnProjectionPatch(
                revision=turn.revision,
                updated_at=turn.updated_at,
                status=turn.status,
                completed_at=turn.completed_at,
                source_message_ids=turn.source_message_ids,
                merged_job_ids=turn.merged_job_ids,
                user_messages=turn.user_messages,
                response_preview=turn.response_preview,
                preview_truncated=turn.preview_truncated,
                final_response=turn.final_response,
            ),
        )
    )
    store.apply_operation(
        "session_1",
        TurnProjectionOperation(event_id=event_id, mutations=[mutation]),
    )


def _history_root(sessions_dir: Path) -> Path:
    return (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "turn_history"
    )


def test_store_pages_complete_turns_with_stable_cursor(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, _ = turn_store
    for index in range(1, 5):
        _apply(store, _turn(index), f"event_{index}")

    first = store.list_summaries("session_1", limit=2)
    _apply(store, _turn(5), "event_5")
    second = store.list_summaries(
        "session_1",
        limit=2,
        cursor=first.next_cursor,
    )

    assert [item.turn_id for item in first.items] == ["job_4", "job_3"]
    assert [item.turn_id for item in second.items] == ["job_2", "job_1"]
    assert second.has_more is False


def test_truncate_from_message_atomically_hides_suffix_and_invalidates_cursor(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, _ = turn_store
    for index in range(1, 5):
        _apply(store, _turn(index), f"event_{index}")

    old_page = store.list_summaries("session_1", limit=2)
    old_epoch = old_page.projection_epoch

    assert store.truncate_from_message("session_1", "message_2") == 3
    latest = store.list_summaries("session_1", limit=20)

    assert [item.turn_id for item in latest.items] == ["job_1"]
    assert latest.projection_epoch == old_epoch + 1
    with pytest.raises(StaleTurnCursorError):
        store.list_summaries(
            "session_1",
            limit=2,
            cursor=old_page.next_cursor,
        )


def test_truncate_uses_header_only_hidden_updates(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = turn_store
    for index in range(1, 5):
        _apply(store, _turn(index), f"event_{index}")

    def reject_detail_read(*args, **kwargs):
        raise AssertionError("隐藏 Turn 时不应解析完整 detail")

    monkeypatch.setattr(
        "app.services.infrastructure.turn_history.files.TurnHistoryFiles.read_turn_record",
        reject_detail_read,
    )

    assert store.truncate_from_message("session_1", "message_2") == 3
    assert [
        item.turn_id for item in store.list_summaries("session_1", limit=20).items
    ] == ["job_1"]


def test_latest_summary_uses_index_without_scanning_hidden_timeline_tail(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir, compaction_threshold=1000)
    for index in range(1, 141):
        _apply(store, _turn(index), f"event_{index}")
    store.apply_operation(
        "session_1",
        TurnProjectionOperation(
            event_id="event_hide_tail",
            hidden_turn_ids=[f"job_{index}" for index in range(2, 141)],
        ),
    )

    def reject_timeline_scan(*args, **kwargs):
        raise AssertionError("latest limit=1 不得扫描 timeline")

    monkeypatch.setattr(
        store._timeline,
        "read_visible_entries_before",
        reject_timeline_scan,
    )
    page = store.list_summaries("session_1", limit=1)

    assert [item.turn_id for item in page.items] == ["job_1"]
    assert page.has_more is False


def test_hidden_timeline_tail_page_has_bounded_scan_and_progress_cursor(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir, compaction_threshold=1000)
    for index in range(1, 301):
        _apply(store, _turn(index), f"event_{index}")
    store.apply_operation(
        "session_1",
        TurnProjectionOperation(
            event_id="event_hide_long_tail",
            hidden_turn_ids=[f"job_{index}" for index in range(2, 301)],
        ),
    )
    original_read_header = store._files.read_turn_header
    header_reads = 0

    def count_header_reads(*args, **kwargs):
        nonlocal header_reads
        header_reads += 1
        return original_read_header(*args, **kwargs)

    monkeypatch.setattr(store._files, "read_turn_header", count_header_reads)
    first = store.list_summaries("session_1", limit=20)
    first_reads = header_reads
    assert first.items == []
    assert first.has_more is True
    assert first.next_cursor is not None
    assert first_reads <= 130

    second = store.list_summaries(
        "session_1",
        limit=20,
        cursor=first.next_cursor,
    )
    assert second.items == []
    assert second.has_more is True
    assert second.next_cursor is not None
    assert second.next_cursor != first.next_cursor
    assert header_reads - first_reads <= 131


def test_summary_page_reads_only_bounded_header_for_large_detail(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = turn_store
    marker = "LARGE-FINAL-RESPONSE-"
    large = _turn(1).model_copy(
        update={"final_response": marker + "x" * (2 * 1024 * 1024)}
    )
    _apply(store, large, "event_large_detail")
    record_path = store._files.turn_record_path("session_1", large.turn_id)
    with record_path.open("rb") as stream:
        header_line = stream.readline(MAX_TURN_HEADER_BYTES + 1)
    assert len(header_line) <= MAX_TURN_HEADER_BYTES
    assert record_path.stat().st_size > 2 * 1024 * 1024

    def reject_full_record(*args, **kwargs):
        raise AssertionError("summary/page 不得读取完整 Turn detail")

    monkeypatch.setattr(store._files, "read_turn_record", reject_full_record)
    page = store.list_summaries("session_1", limit=1)

    assert page.items[0].response_preview.startswith(marker)
    assert page.items[0].preview_truncated is True


def test_revision_update_is_idempotent_and_does_not_change_epoch(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, _ = turn_store
    _apply(store, _turn(1), "event_created")
    updated = _turn(1, revision=2).model_copy(update={"final_response": "新回答"})
    _apply(store, updated, "event_completed")
    store.apply_operation(
        "session_1",
        TurnProjectionOperation(
            event_id="event_completed",
            mutations=[
                TurnProjectionMutation(
                    turn_id=updated.turn_id,
                    base_revision=1,
                    patch=TurnProjectionPatch(
                        revision=2,
                        updated_at=updated.updated_at,
                        final_response="新回答",
                    ),
                )
            ],
        ),
    )

    detail = store.get_details("session_1", ["job_1"]).items[0]
    assert detail.revision == 2
    assert detail.final_response == "新回答"
    assert store.projection_epoch("session_1") == 1


def test_manifest_without_projection_version_is_loaded_as_v1(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, sessions_dir = turn_store
    store.projection_epoch("session_1")
    manifest_path = store._files.manifest_path("session_1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("projection_version")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    restarted = TurnHistoryStore(sessions_dir)
    assert restarted.projection_version("session_1") == 1


def test_lower_source_offset_replay_never_appends_wal_or_updates_turn(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, sessions_dir = turn_store
    created = _turn(1)
    assert store.apply_operation(
        "session_1",
        TurnProjectionOperation(
            event_id="event_source_100",
            source_event_offset=100,
            mutations=[
                TurnProjectionMutation(
                    turn_id=created.turn_id,
                    base_revision=0,
                    create=created,
                )
            ],
        ),
    )
    updated_at = created.updated_at + timedelta(seconds=1)
    assert store.apply_operation(
        "session_1",
        TurnProjectionOperation(
            event_id="event_source_200",
            source_event_offset=200,
            mutations=[
                TurnProjectionMutation(
                    turn_id=created.turn_id,
                    base_revision=1,
                    patch=TurnProjectionPatch(
                        revision=2,
                        updated_at=updated_at,
                        final_response="authoritative",
                    ),
                )
            ],
        ),
    )
    operation_path = next(_history_root(sessions_dir).glob("operations.*.jsonl"))
    wal_before = operation_path.read_bytes()
    replay = TurnProjectionOperation(
        event_id="event_source_150",
        source_event_offset=150,
        mutations=[
            TurnProjectionMutation(
                turn_id=created.turn_id,
                base_revision=2,
                patch=TurnProjectionPatch(
                    revision=3,
                    updated_at=updated_at + timedelta(seconds=1),
                    final_response="stale replay",
                ),
            )
        ],
    )

    assert store.apply_operation("session_1", replay) is False

    detail = store.get_turn("session_1", created.turn_id)
    assert detail is not None
    assert detail.revision == 2
    assert detail.final_response == "authoritative"
    assert operation_path.read_bytes() == wal_before
    assert store.event_cursor("session_1") == "event_source_200"


def test_recovery_skips_lower_source_offset_wal_without_updating_turn(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, sessions_dir = turn_store
    created = _turn(1)
    store.apply_operation(
        "session_1",
        TurnProjectionOperation(
            event_id="event_source_200",
            source_event_offset=200,
            mutations=[
                TurnProjectionMutation(
                    turn_id=created.turn_id,
                    base_revision=0,
                    create=created,
                )
            ],
        ),
    )
    stale = TurnProjectionOperation(
        event_id="event_source_150",
        source_event_offset=150,
        mutations=[
            TurnProjectionMutation(
                turn_id=created.turn_id,
                base_revision=1,
                patch=TurnProjectionPatch(
                    revision=2,
                    updated_at=created.updated_at + timedelta(seconds=1),
                    final_response="stale recovery",
                ),
            )
        ],
    )
    operation_path = next(_history_root(sessions_dir).glob("operations.*.jsonl"))
    with operation_path.open("ab") as stream:
        stream.write(stale.model_dump_json().encode("utf-8") + b"\n")

    recovered = TurnHistoryStore(sessions_dir, compaction_threshold=3)
    detail = recovered.get_turn("session_1", created.turn_id)

    assert detail is not None
    assert detail.revision == 1
    assert detail.final_response == created.final_response
    assert recovered.event_cursor("session_1") == "event_source_200"


def test_recovery_replays_operation_when_manifest_write_failed(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sessions_dir = turn_store
    assert store.projection_epoch("session_1") == 1
    original = store._files.write_manifest
    failed = False

    def fail_once(session_id: str, manifest: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("模拟 manifest 写入前崩溃")
        original(session_id, manifest)  # type: ignore[arg-type]

    monkeypatch.setattr(store._files, "write_manifest", fail_once)
    with pytest.raises(OSError, match="模拟 manifest"):
        _apply(store, _turn(1), "event_1")

    recovered = TurnHistoryStore(sessions_dir)
    detail = recovered.get_details("session_1", ["job_1"]).items[0]
    assert detail.turn_id == "job_1"
    assert detail.revision == 1


def test_recovery_rebuilds_index_when_index_write_failed(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sessions_dir = turn_store
    assert store.projection_epoch("session_1") == 1
    original = store._files.write_index
    failed = False

    def fail_once(session_id: str, index: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("模拟 index 写入前崩溃")
        original(session_id, index)  # type: ignore[arg-type]

    monkeypatch.setattr(store._files, "write_index", fail_once)
    with pytest.raises(OSError, match="模拟 index"):
        _apply(store, _turn(1), "event_1")

    recovered = TurnHistoryStore(sessions_dir)
    page = recovered.list_summaries("session_1", limit=20)
    timeline_path = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "turn_history"
        / "timeline.jsonl"
    )
    assert [item.turn_id for item in page.items] == ["job_1"]
    assert recovered.turn_count("session_1") == 1
    assert len(timeline_path.read_text(encoding="utf-8").splitlines()) == 1


def test_recovery_finishes_multi_turn_operation_without_duplicate_timeline(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sessions_dir = turn_store
    assert store.projection_epoch("session_1") == 1
    original = store._files.write_turn_record
    failed = False

    def fail_second(session_id: str, record: object) -> None:
        nonlocal failed
        turn_id = record.turn.turn_id  # type: ignore[attr-defined]
        if turn_id == "job_2" and not failed:
            failed = True
            raise OSError("模拟第二个 Turn 写入失败")
        original(session_id, record)  # type: ignore[arg-type]

    monkeypatch.setattr(store._files, "write_turn_record", fail_second)
    operation = TurnProjectionOperation(
        event_id="event_merge",
        mutations=[
            TurnProjectionMutation(
                turn_id=turn.turn_id,
                base_revision=0,
                create=turn,
            )
            for turn in (_turn(1), _turn(2))
        ],
    )
    with pytest.raises(OSError, match="第二个 Turn"):
        store.apply_operation("session_1", operation)

    recovered = TurnHistoryStore(sessions_dir)
    page = recovered.list_summaries("session_1", limit=20)
    timeline_path = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "turn_history"
        / "timeline.jsonl"
    )
    assert [item.turn_id for item in page.items] == ["job_2", "job_1"]
    assert recovered.turn_count("session_1") == 2
    assert len(timeline_path.read_text(encoding="utf-8").splitlines()) == 2


def test_compaction_retains_idempotency(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, sessions_dir = turn_store
    _apply(store, _turn(1), "event_1")
    updated = _turn(1, revision=2)
    _apply(store, updated, "event_2")
    latest = _turn(1, revision=3)
    _apply(store, latest, "event_3")

    operation_files = list(_history_root(sessions_dir).glob("operations.*.jsonl"))
    assert len(operation_files) == 1
    assert operation_files[0].read_bytes() == b""
    store.apply_operation(
        "session_1",
        TurnProjectionOperation(
            event_id="event_3",
            mutations=[
                TurnProjectionMutation(
                    turn_id=latest.turn_id,
                    base_revision=2,
                    patch=TurnProjectionPatch(
                        revision=3,
                        updated_at=latest.updated_at,
                    ),
                )
            ],
        ),
    )
    assert store.get_details("session_1", ["job_1"]).items[0].revision == 3


def test_compaction_recovers_when_new_log_exists_before_manifest_switch(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sessions_dir = turn_store
    _apply(store, _turn(1), "event_1")
    _apply(store, _turn(1, revision=2), "event_2")
    original = store._files.write_manifest

    def fail_generation_switch(session_id: str, manifest: object) -> None:
        if manifest.operation_generation == 2:  # type: ignore[attr-defined]
            raise OSError("模拟新 log 落盘后、manifest 切换前崩溃")
        original(session_id, manifest)  # type: ignore[arg-type]

    monkeypatch.setattr(store._files, "write_manifest", fail_generation_switch)
    with pytest.raises(OSError, match="manifest 切换前"):
        _apply(store, _turn(1, revision=3), "event_3")

    operation_files = sorted(_history_root(sessions_dir).glob("operations.*.jsonl"))
    assert len(operation_files) == 2
    recovered = TurnHistoryStore(sessions_dir)
    detail = recovered.get_details("session_1", ["job_1"]).items[0]
    timeline = _history_root(sessions_dir) / "timeline.jsonl"
    assert detail.revision == 3
    assert len(timeline.read_text(encoding="utf-8").splitlines()) == 1
    assert len(list(_history_root(sessions_dir).glob("operations.*.jsonl"))) == 1


def test_compaction_recovers_when_manifest_switched_but_old_log_remains(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sessions_dir = turn_store
    _apply(store, _turn(1), "event_1")
    _apply(store, _turn(1, revision=2), "event_2")

    def fail_old_log_cleanup(session_id: str, *, active_generation: int) -> None:
        if active_generation == 1:
            return
        raise OSError(
            "模拟 manifest 已切换但旧 log 尚未清理: "
            f"session_id={session_id}, generation={active_generation}"
        )

    monkeypatch.setattr(
        store._operations,
        "remove_inactive_logs",
        fail_old_log_cleanup,
    )
    with pytest.raises(OSError, match="旧 log 尚未清理"):
        _apply(store, _turn(1, revision=3), "event_3")

    operation_files = sorted(_history_root(sessions_dir).glob("operations.*.jsonl"))
    assert len(operation_files) == 2
    recovered = TurnHistoryStore(sessions_dir)
    detail = recovered.get_details("session_1", ["job_1"]).items[0]
    timeline = _history_root(sessions_dir) / "timeline.jsonl"
    assert detail.revision == 3
    assert len(timeline.read_text(encoding="utf-8").splitlines()) == 1
    assert len(list(_history_root(sessions_dir).glob("operations.*.jsonl"))) == 1


def test_publish_recovery_rolls_back_backup_when_authoritative_root_is_missing(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, sessions_dir = turn_store
    _apply(store, _turn(1), "event_1")
    root = store._files.root("session_1")
    backup = store._files.publish_backup_path("session_1")
    os.rename(root, backup)
    assert root.exists() is False

    recovered = TurnHistoryStore(sessions_dir)
    page = recovered.list_summaries("session_1", limit=20)

    assert [item.turn_id for item in page.items] == ["job_1"]
    assert page.projection_epoch == 1
    assert root.is_dir()
    assert backup.exists() is False


def test_publish_recovery_validates_new_root_before_removing_old_backup(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, sessions_dir = turn_store
    _apply(store, _turn(1), "event_old")
    staging = TurnHistoryStore(
        sessions_dir,
        directory_name="turn_history_staging",
    )
    _apply(staging, _turn(2), "event_new")
    staging_manifest, staging_index = staging._load_recovered("session_1")
    staging_manifest.projection_epoch = 2
    staging_index.projection_epoch = 2
    staging._files.write_index("session_1", staging_index)
    staging._files.write_manifest("session_1", staging_manifest)

    root = store._files.root("session_1")
    backup = store._files.publish_backup_path("session_1")
    os.rename(root, backup)
    os.rename(staging._files.root("session_1"), root)
    assert root.is_dir() and backup.is_dir()

    recovered = TurnHistoryStore(sessions_dir)
    page = recovered.list_summaries("session_1", limit=20)

    assert [item.turn_id for item in page.items] == ["job_2"]
    assert page.projection_epoch == 2
    assert backup.exists() is False


def test_publish_staging_syncs_deferred_writes_before_rename(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sessions_dir = turn_store
    _apply(store, _turn(1), "event_1")
    staging = TurnHistoryStore(
        sessions_dir,
        directory_name="turn_history_staging",
        write_durability="publish",
    )
    _apply(staging, _turn(2), "event_1")
    staging.set_projection_status("session_1", "ready")
    staging.mark_history_initialized("session_1", projection_version=1)
    sync_calls: list[str] = []
    original_sync_tree = staging._files.sync_tree

    def record_sync(session_id: str) -> None:
        sync_calls.append(session_id)
        original_sync_tree(session_id)

    monkeypatch.setattr(staging._files, "sync_tree", record_sync)

    next_epoch = store.publish_staging("session_1", staging)

    assert next_epoch == 2
    assert sync_calls == ["session_1"]
    assert store.get_turn("session_1", "job_2") is not None


def test_stale_staging_cannot_overwrite_new_projection_epoch(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, _ = turn_store
    _apply(store, _turn(1), "event_1")
    publication_base = store.publication_watermark("session_1")
    stale_staging = store.create_rebuild_staging("session_1")
    _apply(stale_staging, _turn(1), "stale_event_1")
    stale_staging.set_projection_status("session_1", "ready")

    assert store.truncate_from_message("session_1", "message_1") == 1
    assert store.projection_epoch("session_1") == publication_base.projection_epoch + 1

    with pytest.raises(TurnProjectionPublicationConflict, match="epoch 已变化"):
        store.publish_staging(
            "session_1",
            stale_staging,
            publication_base=publication_base,
        )
    assert store.list_summaries("session_1", limit=20).items == []


def test_rebase_invalidates_existing_cursor(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, _ = turn_store
    _apply(store, _turn(1), "event_1")
    _apply(store, _turn(2), "event_2")
    cursor = store.list_summaries("session_1", limit=1).next_cursor
    assert cursor is not None

    store.rebase("session_1")

    with pytest.raises(StaleTurnCursorError):
        store.list_summaries("session_1", limit=1, cursor=cursor)


def test_summary_is_bounded_and_strips_full_user_payload(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, _ = turn_store
    turn = _turn(1).model_copy(
        update={
            "user_messages": [
                TurnUserMessageDTO(
                    message_id="message_1",
                    content="u" * 50_000,
                    metadata={
                        "inline_data_url": "data:image/png;base64," + "A" * 50_000
                    },
                    created_at=datetime.now(UTC),
                )
            ],
            "final_response": "r" * 50_000,
        }
    )
    _apply(store, turn, "event_1")

    summary = store.latest_summary("session_1")
    assert summary is not None
    assert len(summary.user_messages[0].preview) == 500
    assert summary.user_messages[0].content_truncated is True
    assert len(summary.response_preview) == 1000
    assert summary.preview_truncated is True
    serialized = summary.model_dump_json()
    assert "inline_data_url" not in serialized
    assert "data:image" not in serialized


def test_reader_rejects_turn_record_pointing_to_another_timeline_entry(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, _ = turn_store
    _apply(store, _turn(1), "event_1")
    _apply(store, _turn(2), "event_2")
    first = store._files.read_turn_record("session_1", "job_1", required=True)
    second = store._files.read_turn_record("session_1", "job_2", required=True)
    store._files.write_turn_record(
        "session_1",
        first.model_copy(
            update={
                "timeline_start": second.timeline_start,
                "timeline_end": second.timeline_end,
            }
        ),
    )

    with pytest.raises(RuntimeError, match="timeline anchor 身份不一致"):
        store.get_details("session_1", ["job_1"])


def test_full_timeline_rebuild_rejects_duplicate_ordinal(
    turn_store: tuple[TurnHistoryStore, Path],
) -> None:
    store, _ = turn_store
    _apply(store, _turn(1), "event_1")
    _apply(store, _turn(2), "event_2")
    timeline_path = store._files.timeline_path("session_1")
    lines = timeline_path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["ordinal"] = 1
    timeline_path.write_text(
        f"{lines[0]}\n{json.dumps(second)}\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="ordinal 必须严格连续递增"):
        store._timeline.rebuild_index("session_1", projection_epoch=1)


def test_rebase_failure_before_publish_keeps_authoritative_projection(
    turn_store: tuple[TurnHistoryStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sessions_dir = turn_store
    _apply(store, _turn(1), "event_1")
    staging = TurnHistoryStore(
        sessions_dir,
        directory_name=".turn_history-rebuild-staging",
        write_durability="publish",
    )
    staging.discard_projection("session_1")
    staging.projection_epoch("session_1")

    def provide_staging(session_id: str):
        return staging

    def fail_staging_manifest(session_id: str, manifest: object) -> None:
        raise OSError("模拟 staging manifest 写入失败")

    monkeypatch.setattr(store, "create_rebuild_staging", provide_staging)
    monkeypatch.setattr(staging._files, "write_manifest", fail_staging_manifest)

    with pytest.raises(OSError, match="staging manifest"):
        store.rebase("session_1")

    restarted = TurnHistoryStore(sessions_dir, compaction_threshold=3)
    assert restarted.get_turn("session_1", "job_1") is not None
    assert restarted.projection_epoch("session_1") == 1
