from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.core.history_loading import DEFAULT_INITIAL_INCLUDE
from app.domain.itemized.enums import CanonicalItemStatus, PayloadKind, SemanticKind
from app.domain.itemized.records import (
    CanonicalItemRecord,
    InternalDisplayPart,
    InternalDisplayProvenance,
    RuntimeNoticePayload,
)
from app.schemas.internal_v2.turn import (
    StaleTurnCursorErrorDTO,
    TurnCursorDTO,
    TurnHistoryLoadRequest,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    HistorySnapshotBounds,
    InternalExecutionHistoryEntry,
    InternalExecutionHistoryPage,
)
from app.services.infrastructure.rollout_history.paging import HistoryPageReadMixin
from app.services.infrastructure.rollout_history.snapshot import IndexedHistory
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from app.services.infrastructure.turn_history.models import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
)


@dataclass
class _FakeContextReader:
    bounds: HistorySnapshotBounds
    turn_pages: dict[str, list[tuple[list[tuple[str, int, int, int, int]], bool]]]
    internal_pages: dict[str, list[InternalExecutionHistoryPage]]
    turn_queries: list[tuple[str, int | None, int | None, int]]
    internal_queries: list[tuple[str, int | None, int | None, int | None, int]]
    around_rows: list[tuple[str, int, int, int, int]]
    resolved_item_window: tuple[int, int] | None

    def read_history_snapshot_bounds(
        self, _snapshot: object, _chain: object
    ) -> HistorySnapshotBounds:
        return self.bounds

    def read_context_turn_page(
        self,
        _snapshot: object,
        _chain: object,
        *,
        direction: str,
        anchor_ordinal: int | None,
        upper_ordinal: int | None,
        limit: int,
    ) -> tuple[list[tuple[str, int, int, int, int]], bool]:
        self.turn_queries.append((direction, anchor_ordinal, upper_ordinal, limit))
        pages = self.turn_pages.get(direction, [])
        return pages.pop(0) if pages else ([], False)

    def read_internal_execution_history_page(
        self,
        _snapshot: object,
        _chain: object,
        *,
        direction: str,
        anchor_logical_item_ordinal: int | None,
        lower_logical_item_ordinal: int | None,
        upper_logical_item_ordinal: int | None,
        limit: int,
    ) -> InternalExecutionHistoryPage:
        self.internal_queries.append(
            (
                direction,
                anchor_logical_item_ordinal,
                lower_logical_item_ordinal,
                upper_logical_item_ordinal,
                limit,
            )
        )
        pages = self.internal_pages.get(direction, [])
        return pages.pop(0) if pages else InternalExecutionHistoryPage((), False)

    def read_context_turn_ids(
        self,
        _snapshot: object,
        _chain: object,
        _turn_ids: list[str],
    ) -> list[tuple[str, int, int, int, int]]:
        return self.around_rows

    def read_turn_projections(
        self,
        _snapshot: object,
        _turn_ids: list[str],
    ) -> dict[str, object]:
        return {}

    def resolve_context_turn_item_window(
        self,
        _snapshot: object,
        _chain: object,
        *,
        first_logical_turn_ordinal: int,
        last_logical_turn_ordinal: int,
    ) -> SimpleNamespace:
        self.resolved_item_window = (
            first_logical_turn_ordinal,
            last_logical_turn_ordinal,
        )
        return SimpleNamespace(
            lower_logical_item_ordinal=30,
            upper_logical_item_ordinal=60,
        )


class _HistoryHarness(HistoryPageReadMixin):
    def __init__(self, context_reader: _FakeContextReader) -> None:
        self._context_reader = context_reader
        self.pages: list[dict[str, object]] = []

    def _indexed_page(
        self,
        _session_id: str,
        spans: list[object],
        **kwargs: object,
    ) -> dict[str, object]:
        page = {"spans": spans, **kwargs}
        self.pages.append(page)
        return page


@pytest.fixture
def history_setup() -> tuple[_HistoryHarness, IndexedHistory, SimpleNamespace]:
    context_reader = _FakeContextReader(
        bounds=HistorySnapshotBounds(
            view_id="view-1",
            history_view_revision=7,
            upper_turn_ordinal=5,
            upper_logical_item_ordinal=100,
        ),
        turn_pages={},
        internal_pages={},
        turn_queries=[],
        internal_queries=[],
        around_rows=[],
        resolved_item_window=None,
    )
    snapshot = SimpleNamespace(
        thread_id="thread-1",
        checkpoint_ns="",
        manifest=SimpleNamespace(history_view_revision=7),
    )
    indexed = IndexedHistory(
        rollout_id="rollout-1",
        projection_epoch=3,
        view_id="view-1",
        turn_count=5,
        snapshot=snapshot,
        chain=SimpleNamespace(),
    )
    return _HistoryHarness(context_reader), indexed, snapshot


def _entry(
    sequence: int,
    *,
    logical_item_ordinal: int,
    execution_id: str = "execution-1",
) -> InternalExecutionHistoryEntry:
    payload = RuntimeNoticePayload(
        prompt="只允许内部使用的 prompt 内容",
        display_part=InternalDisplayPart(
            kind="generated_session_result",
            text="子会话已生成并返回结果。",
            provenance=InternalDisplayProvenance(
                source_session_id="session-generated",
                source_execution_id="execution-generated",
            ),
        ),
    ).to_dict()
    item = CanonicalItemRecord.create(
        item_sequence=sequence,
        item_id=f"item-{sequence}",
        semantic_kind=SemanticKind.RUNTIME_NOTICE,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "runtime", "producer_id": execution_id},
        payload=payload,
        metadata={"internal": True},
        turn_scope="ambient",
        wire_role="user",
        created_at="2026-10-04T00:00:00Z",
    )
    return InternalExecutionHistoryEntry(
        execution_id=execution_id,
        job_id="job-1",
        outcome="completed",
        created_at="2026-10-04T00:00:00Z",
        updated_at="2026-10-04T00:00:01Z",
        logical_item_ordinal=logical_item_ordinal,
        item=item,
    )


def _turn_row(turn_id: str, ordinal: int) -> tuple[str, int, int, int, int]:
    return (turn_id, ordinal * 10 - 9, ordinal * 10, ordinal, ordinal * 10 - 10)


def _cursor(
    reader: _HistoryHarness,
    snapshot: SimpleNamespace,
    *,
    direction: str,
    turn_ordinal: int | None,
    logical_item_ordinal: int | None,
    include: tuple[str, ...] = ("internal",),
    history_view_revision: int = 7,
) -> str:
    return reader._encode_cursor(
        session_id="session-1",
        thread_id=snapshot.thread_id,
        checkpoint_ns=snapshot.checkpoint_ns,
        rollout_id="rollout-1",
        projection_epoch=3,
        view_id="view-1",
        history_view_revision=history_view_revision,
        turn_read_upper_bound=5,
        logical_item_read_upper_bound=100,
        direction=direction,
        query_mode="linear",
        effective_include=include,
        anchor_turn_id=None,
        window_before_turns=None,
        window_after_turns=None,
        stage=0,
        turn_ordinal=turn_ordinal,
        logical_item_ordinal=logical_item_ordinal,
    )


def test_internal_only_tail_returns_safe_typed_display_and_composite_cursor(
    history_setup: tuple[_HistoryHarness, IndexedHistory, SimpleNamespace],
) -> None:
    reader, indexed, _snapshot = history_setup
    reader._context_reader.bounds = HistorySnapshotBounds(
        view_id="view-1",
        history_view_revision=7,
        upper_turn_ordinal=None,
        upper_logical_item_ordinal=20,
    )
    entry = _entry(80, logical_item_ordinal=20)
    reader._context_reader.internal_pages["before"] = [
        InternalExecutionHistoryPage((entry,), True)
    ]

    page = reader._load_indexed_history(
        "session-1",
        TurnHistoryLoadRequest(direction="tail"),
        indexed,
        policy=None,
    )

    assert page["spans"] == []
    assert page["has_more"] is True
    cursor = TurnCursorDTO.model_validate(
        reader._decode_cursor_payload(page["before_cursor"])
    )
    assert cursor.turn_ordinal is None
    assert cursor.logical_item_ordinal == 20
    projected = RolloutHistoryReader._project_internal_entries(
        "session-1",
        "thread-1",
        (entry,),
        include=DEFAULT_INITIAL_INCLUDE,
    )
    assert projected[0].status == "completed"
    assert projected[0].display_parts[0].logical_item_ordinal == 20
    assert projected[0].display_parts[0].text == "子会话已生成并返回结果。"
    assert "prompt" not in str(projected[0].model_dump(mode="json"))


@pytest.mark.parametrize(
    ("turn_has_more", "internal_has_more", "expected_turn", "expected_item"),
    [
        (True, False, 2, None),
        (False, True, None, 20),
    ],
)
def test_tail_cursor_anchor_only_tracks_non_exhausted_lane(
    history_setup: tuple[_HistoryHarness, IndexedHistory, SimpleNamespace],
    turn_has_more: bool,
    internal_has_more: bool,
    expected_turn: int | None,
    expected_item: int | None,
) -> None:
    reader, indexed, _snapshot = history_setup
    reader._context_reader.turn_pages["tail"] = [[(_turn_row("turn-2", 2),), turn_has_more]]
    reader._context_reader.internal_pages["before"] = [
        InternalExecutionHistoryPage(
            (_entry(80, logical_item_ordinal=20),),
            internal_has_more,
        )
    ]

    page = reader._load_indexed_history(
        "session-1",
        TurnHistoryLoadRequest(direction="tail", include=["internal"]),
        indexed,
        policy=None,
    )

    assert page["has_more"] is (turn_has_more or internal_has_more)
    if page["before_cursor"] is not None:
        cursor = TurnCursorDTO.model_validate(
            reader._decode_cursor_payload(page["before_cursor"])
        )
        assert cursor.turn_ordinal == expected_turn
        assert cursor.logical_item_ordinal == expected_item


def test_default_anchor_include_reads_internal_history(
    history_setup: tuple[_HistoryHarness, IndexedHistory, SimpleNamespace],
) -> None:
    reader, indexed, _snapshot = history_setup
    entry = _entry(80, logical_item_ordinal=20)
    reader._context_reader.internal_pages["before"] = [
        InternalExecutionHistoryPage((entry,), False)
    ]

    page = reader._load_indexed_history(
        "session-1",
        TurnHistoryLoadRequest(direction="before"),
        indexed,
        policy=None,
    )

    assert reader._context_reader.internal_queries == [
        ("before", None, None, 100, 256)
    ]
    assert page["internal_entries"] == (entry,)


@pytest.mark.parametrize(
    ("direction", "expected_query_direction"),
    [("before", "before"), ("after", "after")],
)
def test_continuation_queries_both_lanes_from_independent_anchors(
    history_setup: tuple[_HistoryHarness, IndexedHistory, SimpleNamespace],
    direction: str,
    expected_query_direction: str,
) -> None:
    reader, indexed, snapshot = history_setup
    anchor_direction = "before" if direction == "before" else "after"
    reader._context_reader.turn_pages[anchor_direction] = [
        ([_turn_row("turn-next", 2)], False)
    ]
    reader._context_reader.internal_pages[anchor_direction] = [
        InternalExecutionHistoryPage(
            (_entry(80, logical_item_ordinal=20),),
            True,
        )
    ]
    cursor = _cursor(
        reader,
        snapshot,
        direction=direction,
        turn_ordinal=3,
        logical_item_ordinal=15,
    )

    page = reader._load_indexed_history(
        "session-1",
        TurnHistoryLoadRequest(direction=direction, cursor=cursor),
        indexed,
        policy=None,
    )

    assert page["has_more"] is True
    turn_query = reader._context_reader.turn_queries[-1]
    internal_query = reader._context_reader.internal_queries[-1]
    assert turn_query[:2] == (expected_query_direction, 3)
    assert internal_query[:2] == (expected_query_direction, 15)
    next_cursor = TurnCursorDTO.model_validate(
        reader._decode_cursor_payload(page["next_cursor"])
    )
    assert next_cursor.turn_ordinal is None
    assert next_cursor.logical_item_ordinal == 20


def test_around_uses_nearest_turn_pages_across_ordinal_gaps(
    history_setup: tuple[_HistoryHarness, IndexedHistory, SimpleNamespace],
) -> None:
    reader, indexed, _snapshot = history_setup
    reader._context_reader.around_rows = [_turn_row("turn-anchor", 9)]
    reader._context_reader.turn_pages["before"] = [
        ([_turn_row("turn-3", 3), _turn_row("turn-6", 6)], True)
    ]
    reader._context_reader.turn_pages["after"] = [
        ([_turn_row("turn-12", 12)], False)
    ]
    reader._context_reader.internal_pages["after"] = [
        InternalExecutionHistoryPage((_entry(45, logical_item_ordinal=85),), False),
        InternalExecutionHistoryPage((), False),
        InternalExecutionHistoryPage((), False),
    ]

    page = reader._load_indexed_history(
        "session-1",
        TurnHistoryLoadRequest(
            direction="around",
            anchor_turn_id="turn-anchor",
            before_turns=2,
            after_turns=1,
            include=["internal"],
        ),
        indexed,
        policy=None,
    )

    assert [span.ordinal for span in page["spans"]] == [3, 6, 9, 12]
    assert reader._context_reader.resolved_item_window == (3, 12)
    assert page["has_before"] is True
    assert page["before_cursor"] is not None
    assert page["has_after"] is False
    assert page["after_cursor"] is None


def test_cursor_rejects_changed_include_and_stale_history_revision(
    history_setup: tuple[_HistoryHarness, IndexedHistory, SimpleNamespace],
) -> None:
    reader, indexed, snapshot = history_setup
    cursor = _cursor(
        reader,
        snapshot,
        direction="before",
        turn_ordinal=4,
        logical_item_ordinal=80,
        include=("internal", "user"),
    )

    with pytest.raises(InvalidTurnCursorError, match="effective_include"):
        reader._load_indexed_history(
            "session-1",
            TurnHistoryLoadRequest(
                direction="before",
                cursor=cursor,
                include=["internal"],
            ),
            indexed,
            policy=None,
        )

    stale = _cursor(
        reader,
        snapshot,
        direction="before",
        turn_ordinal=4,
        logical_item_ordinal=80,
        include=("internal",),
        history_view_revision=6,
    )
    snapshot.manifest.history_view_revision = 8
    reader._context_reader.bounds = HistorySnapshotBounds(
        view_id="view-1",
        history_view_revision=8,
        upper_turn_ordinal=5,
        upper_logical_item_ordinal=100,
    )
    with pytest.raises(StaleTurnCursorError) as error:
        reader._load_indexed_history(
            "session-1",
            TurnHistoryLoadRequest(direction="before", cursor=stale),
            indexed,
            policy=None,
        )

    assert error.value.detail.reason == "history_view_revision"
    assert error.value.detail.cursor_history_view_revision == 6
    assert error.value.detail.current_history_view_revision == 8


def test_stale_projection_epoch_error_keeps_original_contract() -> None:
    error = StaleTurnCursorError(
        session_id="session-1",
        cursor_epoch=2,
        current_epoch=3,
    )

    assert isinstance(error.detail, StaleTurnCursorErrorDTO)
    assert error.detail.reason == "projection_epoch"
    assert error.detail.cursor_history_view_revision is None
    assert error.detail.current_history_view_revision is None


def test_stale_history_revision_requires_both_revision_values() -> None:
    with pytest.raises(ValueError, match="必须同时提供"):
        StaleTurnCursorError(
            session_id="session-1",
            cursor_epoch=2,
            current_epoch=3,
            cursor_history_view_revision=4,
        )


def test_cursor_rejects_physical_item_sequence_v2_shape(
    history_setup: tuple[_HistoryHarness, IndexedHistory, SimpleNamespace],
) -> None:
    reader, indexed, snapshot = history_setup
    current = reader._decode_cursor_payload(
        _cursor(
            reader,
            snapshot,
            direction="before",
            turn_ordinal=4,
            logical_item_ordinal=80,
        )
    )
    current["item_sequence"] = current.pop("logical_item_ordinal")
    current["item_read_upper_bound"] = current.pop("logical_item_read_upper_bound")
    old_shape = base64.urlsafe_b64encode(
        json.dumps(current, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")

    with pytest.raises(InvalidTurnCursorError, match="物理 item sequence"):
        reader._load_indexed_history(
            "session-1",
            TurnHistoryLoadRequest(direction="before", cursor=old_shape),
            indexed,
            policy=None,
        )


def test_cursor_accepts_zero_logical_item_ordinal(
    history_setup: tuple[_HistoryHarness, IndexedHistory, SimpleNamespace],
) -> None:
    reader, _indexed, snapshot = history_setup
    cursor = TurnCursorDTO.model_validate(
        reader._decode_cursor_payload(
            _cursor(
                reader,
                snapshot,
                direction="after",
                turn_ordinal=None,
                logical_item_ordinal=0,
            )
        )
    )

    assert cursor.logical_item_ordinal == 0
    assert cursor.logical_item_read_upper_bound == 100
