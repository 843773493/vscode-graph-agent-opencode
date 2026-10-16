from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest

from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_SCHEMA_VERSION,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from tests.integration.backend.sessions.rollout_context.deterministic_rollout_fixture_helpers import (
    seed_deterministic_v2_rollout,
)


def _reader(workspace_root: Path) -> RolloutHistoryReader:
    return RolloutHistoryReader(
        RolloutContextReader(
            RolloutContextStore(
                workspace_root / ".boxteam" / "sessions",
                message_codec=LangChainMessageCodec(),
            )
        )
    )


def _advance_to_64_cursor(
    reader: RolloutHistoryReader,
    session_id: str,
    thread_id: str,
) -> str:
    _, cursor, _ = reader.bootstrap(session_id, thread_id)
    assert cursor is not None
    first = reader.load(
        session_id,
        thread_id,
        TurnHistoryLoadRequest(direction="before", cursor=cursor),
    )
    assert [item.ordinal for item in first.items] == [125, 126, 127]
    second = reader.load(
        session_id,
        thread_id,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=first.next_cursor,
            turns=16,
        ),
    )
    assert [item.ordinal for item in second.items] == list(range(109, 125))
    assert second.next_cursor is not None
    return second.next_cursor


@pytest.fixture
def deterministic_v2_rollout(
    integration_workspace_root_path: str,
    session_bundle_factory,
    frozen_provider_prepare,
):
    """确定性集成数据由真实 Saver 写入，不是旧真实 Provider 记录。"""
    return seed_deterministic_v2_rollout(
        Path(integration_workspace_root_path),
        session_bundle_factory,
        frozen_provider_prepare,
    )


def test_deterministic_v2_128_turn_history_is_bounded_and_preserves_tool_body(
    integration_workspace_root_path: str,
    deterministic_v2_rollout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, tool_bodies = deterministic_v2_rollout
    reader = _reader(Path(integration_workspace_root_path))
    storage = reader._context_reader._storage
    thread_id = storage.storage.main_thread_id(session_id)
    items = storage.storage.read_items(session_id, thread_id)
    assert len([item for item in items if item.semantic_kind == "user_input"]) == 128
    assert all(item.format_version == 2 for item in items)
    stored_results = [item for item in items if item.semantic_kind == "tool_result"]
    assert len(stored_results) == len(tool_bodies) == 16
    for body in tool_bodies.values():
        assert any(
            body in json.dumps(item.payload, ensure_ascii=False)
            for item in stored_results
        )
    with storage.storage._connect(thread_id, "", session_id=session_id, read_only=True) as connection:
        assert connection.execute(
            "SELECT schema_version, rollout_format_version FROM database_meta"
        ).fetchone() == (ROLLOUT_SCHEMA_VERSION, 2)
    source_by_sequence = {item.item_sequence: item for item in items}
    accessed: list[int] = []
    original_read = storage.checkpoints._read_record_envelopes

    def capture_read(session_id, thread_id, checkpoint_ns, rows, *, connection=None):
        rows = list(rows)
        accessed.extend(row[0] for row in rows)
        return original_read(session_id, thread_id, checkpoint_ns, rows, connection=connection)

    def reject_full_scan(*args, **kwargs):
        raise AssertionError("有界 history 不得 materialize 全量消息")

    monkeypatch.setattr(storage.checkpoints, "_read_record_envelopes", capture_read)
    monkeypatch.setattr(storage.checkpoints, "materialize_messages", reject_full_scan)
    tail = reader.load(session_id, thread_id, TurnHistoryLoadRequest(direction="tail", turns=1))
    assert [item.ordinal for item in tail.items] == [128]
    assert tail.items[0].final_response == "确定性集成回答 128"
    assert tail.items[0].tool_summary
    assert len(accessed) <= 4
    assert all(
        source_by_sequence[seq].semantic_kind != "tool_result" for seq in accessed
    )
    accessed.clear()
    detail = reader.load(
        session_id,
        thread_id,
        TurnHistoryLoadRequest(
            turn_ids=["deterministic-turn-0128"],
            tool_call_ids=["deterministic-call-0128"],
            include=["tool_call", "tool_result"],
        ),
    )
    rendered = detail.model_dump(mode="json")
    assert detail.items[0].detail_truncated is True
    assert tool_bodies["deterministic-call-0128"] not in json.dumps(
        rendered, ensure_ascii=False
    )
    assert tool_bodies["deterministic-call-0120"] not in json.dumps(
        rendered, ensure_ascii=False
    )
    assert len(accessed) <= 3
    assert all(
        source_by_sequence[seq].turn_id == "deterministic-turn-0128" for seq in accessed
    )
    accessed.clear()
    small_detail = reader.load(
        session_id,
        thread_id,
        TurnHistoryLoadRequest(
            turn_ids=["deterministic-turn-0112"],
            tool_call_ids=["deterministic-call-0112"],
            include=["tool_call", "tool_result"],
        ),
    )
    assert small_detail.items[0].detail_truncated is False
    assert tool_bodies["deterministic-call-0112"] in json.dumps(
        small_detail.model_dump(mode="json"), ensure_ascii=False
    )
    assert len(accessed) <= 3
    assert all(
        source_by_sequence[seq].turn_id == "deterministic-turn-0112" for seq in accessed
    )
    cursor = _advance_to_64_cursor(reader, session_id, thread_id)
    samples = []
    for _ in range(8):
        accessed.clear()
        started = perf_counter()
        page = reader.load(
            session_id,
            thread_id,
            TurnHistoryLoadRequest(direction="before", cursor=cursor, turns=64),
        )
        samples.append((perf_counter() - started) * 1000)
        assert len(accessed) <= 128
        assert all(
            source_by_sequence[seq].semantic_kind != "tool_result" for seq in accessed
        )
    assert [item.ordinal for item in page.items] == list(range(45, 109))
    assert median(samples) < 200
