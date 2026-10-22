"""验证 Turn 与 standalone display 在同一 view 中的真实存储与分页行为。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.enums import CanonicalItemStatus, PayloadKind, SemanticKind
from app.domain.itemized.errors import FormatDispatchError
from app.domain.itemized.hashing import payload_content_length
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
)
from app.domain.itemized.records import (
    CanonicalItemRecord,
    InternalDisplayPart,
    InternalDisplayProvenance,
    RuntimeNoticePayload,
)
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest, TurnHistoryPageDTO
from app.services.infrastructure.rollout_context.activation.schema import (
    create_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_FORMAT_VERSION,
    ROLLOUT_SCHEMA_VERSION,
    initialize_rollout_schema,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
    canonical_json_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    validate_item_storage_projection,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from app.services.infrastructure.rollout_history.paging import HistoryPageReadMixin
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from app.services.infrastructure.turn_history.models import InvalidTurnCursorError
from tests.support.rollout_view_mutations import (
    mutate_view_members,
    mutate_view_members_many,
)

_CREATED_AT = "2026-10-04T00:00:00Z"
_UPDATED_AT = "2026-10-04T00:00:01Z"
_VIEW_ID = "view-internal-display"
_BRANCH_ID = "branch-001"


def test_cold_execution_selector_is_exact_and_has_frozen_bounded_continuation(history_environment):
    environment = history_environment(turns=(), displays=tuple(
        DisplaySpec(index, index * 10, execution_id="report-owner" if index % 2 else "sibling-report")
        for index in range(1, 521)
    ))
    page = environment.history_reader.load(
        environment.session_id, environment.thread_id,
        TurnHistoryLoadRequest(execution_id="report-owner", include=["internal"]),
    )
    assert page.items == []
    assert len(page.internal_executions) == 1
    assert page.internal_executions[0].execution_id == "report-owner"
    assert len(page.internal_executions[0].display_parts) == 256
    assert page.has_before and page.before_cursor is not None
    following = environment.history_reader.load(
        environment.session_id, environment.thread_id,
        TurnHistoryLoadRequest(direction="before", execution_id="report-owner", cursor=page.before_cursor),
    )
    assert following.items == []
    assert len(following.internal_executions[0].display_parts) == 4
    assert not following.has_before
    ids = {part.item_id for part in page.internal_executions[0].display_parts}
    assert ids.isdisjoint(part.item_id for part in following.internal_executions[0].display_parts)
    with pytest.raises(InvalidTurnCursorError, match="execution_id"):
        environment.history_reader.load(
            environment.session_id, environment.thread_id,
            TurnHistoryLoadRequest(direction="before", execution_id="sibling-report", cursor=page.before_cursor),
        )
    for field, wrong, message in (
        ("session_id", "other-session", "当前 rollout"),
        ("thread_id", "other-thread", "当前 rollout"),
        ("checkpoint_ns", "other-namespace", "当前 rollout"),
        ("rollout_id", "other-rollout", "当前 rollout"),
        ("view_id", "other-view", "当前 context view"),
    ):
        payload = environment.history_reader._decode_cursor_payload(page.before_cursor)
        payload[field] = wrong
        cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        with pytest.raises(InvalidTurnCursorError, match=message):
            environment.history_reader.load(
                environment.session_id, environment.thread_id,
                TurnHistoryLoadRequest(direction="before", execution_id="report-owner", cursor=cursor),
            )


@dataclass(frozen=True, slots=True)
class TurnSpec:
    turn_id: str
    ordinal: int
    item_sequence: int
    view_ordinal: int


@dataclass(frozen=True, slots=True)
class DisplaySpec:
    item_sequence: int
    view_ordinal: int
    execution_id: str | None = None
    is_response: bool = False


def _internal_execution_id(spec: DisplaySpec) -> str:
    return spec.execution_id or f"execution-internal-{spec.item_sequence:04d}"


@dataclass(frozen=True, slots=True)
class HistoryEnvironment:
    session_id: str
    thread_id: str
    view_id: str
    storage: RolloutContextStore
    context_reader: RolloutContextReader
    history_reader: RolloutHistoryReader

    def open_snapshot(self) -> RolloutReadSnapshot:
        return self.context_reader.open_snapshot(
            self.session_id,
            self.thread_id,
            "",
        )


@pytest.fixture
def history_environment(
    integration_workspace_root_path: str,
    session_bundle_factory: Callable[[Path, str], Path],
) -> Iterator[Callable[..., HistoryEnvironment]]:
    """使用真实 session catalog、rollout SQLite 与 canonical JSONL locator。"""
    sessions_root = Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions_root, session_id)
    thread_id = get_session_path_resolver(sessions_root).main_thread_id(session_id)
    storage = RolloutContextStore(sessions_root, message_codec=LangChainMessageCodec())
    _initialize_rollout_fixture(storage, session_id=session_id, thread_id=thread_id)
    context_reader = RolloutContextReader(storage)
    history_reader = RolloutHistoryReader(context_reader)

    def seed(
        *,
        turns: tuple[TurnSpec, ...] = (
            TurnSpec("job-0002", 2, 1, 10),
            TurnSpec("job-0007", 7, 4, 40),
            TurnSpec("job-0013", 13, 8, 80),
        ),
        displays: tuple[DisplaySpec, ...] = (
            DisplaySpec(2, 20),
            DisplaySpec(3, 30),
            DisplaySpec(5, 50),
            DisplaySpec(6, 60),
            DisplaySpec(7, 70),
            DisplaySpec(9, 90),
        ),
    ) -> HistoryEnvironment:
        _seed_rollout(
            storage,
            session_id=session_id,
            thread_id=thread_id,
            turns=turns,
            displays=displays,
        )
        return HistoryEnvironment(
            session_id=session_id,
            thread_id=thread_id,
            view_id=_VIEW_ID,
            storage=storage,
            context_reader=context_reader,
            history_reader=history_reader,
        )

    yield seed


def _seed_rollout(
    storage: RolloutContextStore,
    *,
    session_id: str,
    thread_id: str,
    turns: tuple[TurnSpec, ...],
    displays: tuple[DisplaySpec, ...],
) -> None:
    """按正式 v2 schema 写入合法 owner、item catalog、execution 与 view 行。"""
    specs = [
        *((spec.item_sequence, "turn", spec) for spec in turns),
        *((spec.item_sequence, "display", spec) for spec in displays),
    ]
    if not specs:
        raise ValueError("fixture 至少需要一个 canonical item")
    ordered = sorted(specs, key=lambda entry: entry[0])
    if [entry[0] for entry in ordered] != list(range(1, len(ordered) + 1)):
        raise ValueError("fixture item sequence 必须从 1 连续递增")

    items: list[tuple[CanonicalItemRecord, str | None]] = []
    for sequence, kind, spec in ordered:
        if kind == "turn":
            assert isinstance(spec, TurnSpec)
            execution_id = f"execution-{spec.turn_id}"
            item = CanonicalItemRecord.create(
                item_sequence=sequence,
                item_id=f"root-{spec.turn_id}",
                semantic_kind=SemanticKind.USER_INPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.COMPLETED,
                producer_ref={
                    "producer_kind": "user",
                    "producer_id": f"ingress-{spec.turn_id}",
                },
                payload=f"输入 {spec.turn_id}",
                turn_id=spec.turn_id,
                turn_scope="turn_root",
                wire_role="user",
                created_at=_CREATED_AT,
            )
        else:
            assert isinstance(spec, DisplaySpec)
            execution_id = _internal_execution_id(spec)
            item = CanonicalItemRecord.create(
                item_sequence=sequence,
                item_id=f"internal-item-{sequence}",
                semantic_kind=SemanticKind.RUNTIME_NOTICE,
                payload_kind=PayloadKind.STRUCTURED_CONTENT,
                status=CanonicalItemStatus.COMPLETED,
                producer_ref={
                    "producer_kind": "runtime",
                    "producer_id": execution_id,
                },
                payload=RuntimeNoticePayload(
                    prompt="仅供内部上下文使用",
                    display_part=InternalDisplayPart(
                        kind="generated_session_result",
                        text=f"子任务结果 {sequence}",
                        provenance=InternalDisplayProvenance(),
                    ),
                ).to_dict(),
                extensions={},
                turn_scope="ambient",
                wire_role="user",
                created_at=_CREATED_AT,
            )
            if spec.is_response:
                item = CanonicalItemRecord.create(
                    item_sequence=sequence,
                    item_id=f"internal-item-{sequence}",
                    semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                    payload_kind=PayloadKind.TEXT,
                    status=CanonicalItemStatus.COMPLETED,
                    producer_ref={"producer_kind": "provider", "producer_id": execution_id},
                    payload=f"内部执行回复 {sequence}",
                    projection_identity=ItemProjectionIdentity(
                        model_call_id=f"model-call-internal-{execution_id}",
                        block_id=f"block-internal-{sequence}",
                        block_index=sequence - 1,
                    ),
                    wire_role="assistant",
                    created_at=_CREATED_AT,
                )
        items.append((item, execution_id))

    jsonl_path = storage.storage.jsonl_path(session_id, thread_id=thread_id)
    item_lines = [canonical_json_line(item.to_dict()) for item, _ in items]
    jsonl_bytes = b"".join(item_lines)
    with jsonl_path.open("wb") as stream:
        stream.write(jsonl_bytes)
        stream.flush()
        os.fsync(stream.fileno())

    with storage.storage._connect(thread_id, session_id=session_id) as connection:
        branch_row = connection.execute(
            "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
            (_BRANCH_ID,),
        ).fetchone()
        if branch_row is None or branch_row[0] is None:
            raise RuntimeError("fixture 初始化后缺少 active root context view")
        view_id = str(branch_row[0])
        connection.execute(
            "INSERT INTO storage_commits "
            "(commit_id, transaction_id, first_message_sequence, last_message_sequence, "
            "jsonl_start_offset, jsonl_end_offset, first_control_sequence, "
            "last_control_sequence, jsonl_fsync_at, status, created_at, committed_at, "
            "commit_kind, commit_mode, jsonl_offset_before, jsonl_offset_after, "
            "jsonl_record_count, subject_id, idempotency_key, outcome, metadata_json) "
            "VALUES (1, 'fixture-transaction', NULL, NULL, 0, ?, NULL, NULL, ?, "
            "'committed', ?, ?, 'item_convergence', 'item_bearing', 0, ?, ?, ?, "
            "'fixture-items', NULL, '{}')",
            (
                len(jsonl_bytes),
                _UPDATED_AT,
                _CREATED_AT,
                _UPDATED_AT,
                len(jsonl_bytes),
                len(items),
                items[0][0].item_id,
            ),
        )
        item_offset = 0
        for (item, execution_id), line in zip(items, item_lines, strict=True):
            source_revision, _part_ordinal = validate_item_storage_projection(item)
            connection.execute(
                "INSERT INTO item_catalog "
                "(item_sequence, item_id, semantic_kind, payload_kind, status, turn_id, "
                "execution_id, turn_scope, message_group_id, wire_role, producer_ref_json, "
                "payload_length, source_revision, content_hash, jsonl_offset, jsonl_length, "
                "commit_id, operation_anchor_capable, searchable, created_at, projection_manifest_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)",
                (
                    item.item_sequence,
                    item.item_id,
                    item.semantic_kind,
                    item.payload_kind,
                    item.status,
                    item.turn_id,
                    execution_id,
                    item.turn_scope,
                    item.wire_role,
                    canonical_json_text(item.producer_ref),
                    payload_content_length(item.payload_kind, item.payload),
                    source_revision or f"canonical:{item.item_id}:{item.content_hash}",
                    item.content_hash,
                    item_offset,
                    len(line),
                    int(
                        item.semantic_kind
                        in {SemanticKind.USER_INPUT, SemanticKind.ASSISTANT_OUTPUT}
                    ),
                    item.created_at,
                    canonical_json_text(item.projection_manifest()),
                ),
            )
            item_offset += len(line)

        connection.execute(
            "UPDATE database_meta SET last_item_sequence = ?, last_commit_id = 1, "
            "committed_jsonl_offset = ?, updated_at = ? WHERE singleton_id = 1",
            (len(items), len(jsonl_bytes), _UPDATED_AT),
        )
        mutate_view_members(connection,
            "DELETE FROM context_view_items WHERE view_id = ?", (view_id,)
        )
        connection.execute(
            "UPDATE context_views SET head_turn_id = ?, head_message_sequence = 0, "
            "logical_turn_count = ? WHERE view_id = ?",
            (turns[-1].turn_id if turns else None, len(turns), view_id),
        )

        for spec in turns:
            root_item_id = f"root-{spec.turn_id}"
            execution_id = f"execution-{spec.turn_id}"
            connection.execute(
                "INSERT INTO executions "
                "(execution_id, session_id, thread_id, turn_id, execution_origin, job_id, "
                "admission_idempotency_key, admission_payload_hash, attempt, accepted_ingress_id, "
                "outcome, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'user_turn', ?, ?, ?, 1, ?, 'completed', ?, ?)",
                (
                    execution_id,
                    session_id,
                    thread_id,
                    spec.turn_id,
                    f"history-job:{spec.turn_id}",
                    f"idempotency-{spec.turn_id}",
                    "sha256:jcs:v1:" + "a" * 64,
                    f"ingress-{spec.turn_id}",
                    _CREATED_AT,
                    _UPDATED_AT,
                ),
            )
            connection.execute(
                "INSERT INTO turns "
                "(turn_id, turn_ordinal, turn_kind, branch_id, first_message_sequence, "
                "last_message_sequence, user_message_sequence, status, created_at, updated_at) "
                "VALUES (?, ?, 'normal', ?, ?, ?, ?, 'completed', ?, ?)",
                (
                    spec.turn_id,
                    spec.ordinal,
                    _BRANCH_ID,
                    spec.item_sequence,
                    spec.item_sequence,
                    spec.item_sequence,
                    _CREATED_AT,
                    _UPDATED_AT,
                ),
            )
            connection.execute(
                "INSERT INTO turn_records "
                "(turn_id, session_id, thread_id, turn_ordinal, source_branch_id, "
                "root_input_item_id, root_input_item_sequence, accepted_ingress_id, "
                "acceptance_idempotency_key, initial_execution_id, last_execution_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)",
                (
                    spec.turn_id,
                    session_id,
                    thread_id,
                    spec.ordinal,
                    _BRANCH_ID,
                    root_item_id,
                    spec.item_sequence,
                    f"ingress-{spec.turn_id}",
                    f"acceptance-{spec.turn_id}",
                    execution_id,
                    execution_id,
                    _CREATED_AT,
                    _UPDATED_AT,
                ),
            )
            connection.execute(
                "INSERT INTO context_view_turns "
                "(view_id, turn_id, logical_turn_ordinal, user_message_sequence, "
                "root_input_item_id, fork_lineage_json) VALUES (?, ?, ?, ?, ?, '{}')",
                (
                    view_id,
                    spec.turn_id,
                    spec.ordinal,
                    spec.item_sequence,
                    root_item_id,
                ),
            )

        for execution_id in dict.fromkeys(_internal_execution_id(spec) for spec in displays):
            connection.execute(
                "INSERT INTO executions "
                "(execution_id, session_id, thread_id, turn_id, execution_origin, job_id, "
                "admission_idempotency_key, admission_payload_hash, attempt, accepted_ingress_id, "
                "outcome, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, 'internal', 'job-internal', ?, ?, 1, NULL, "
                "'completed', ?, ?)",
                (
                    execution_id,
                    session_id,
                    thread_id,
                    f"idempotency-{execution_id}",
                    "sha256:jcs:v1:" + "b" * 64,
                    _CREATED_AT,
                    _UPDATED_AT,
                ),
            )

        memberships = [
            (view_id, f"root-{spec.turn_id}", spec.view_ordinal, "canonical")
            for spec in turns
        ] + [
            (
                view_id,
                f"internal-item-{spec.item_sequence}",
                spec.view_ordinal,
                "display_only",
            )
            for spec in displays
        ]
        mutate_view_members_many(connection,
            "INSERT INTO context_view_items "
            "(view_id, item_id, logical_item_ordinal, visible, source_kind, context_selected, context_ordinal) "
            "VALUES (?, ?, ?, 1, ?, 1, ?)",
            [(*row, row[2]) for row in memberships],
        )
        connection.commit()


def _initialize_rollout_fixture(
    storage: RolloutContextStore,
    *,
    session_id: str,
    thread_id: str,
) -> None:
    """通过正式写锁与 rollout DDL 建立最小 v2 root view 与 owner meta。"""
    root = storage.storage.root(session_id, thread_id=thread_id)
    with storage.storage._lock(thread_id, "", session_id=session_id):
        root.mkdir(parents=True)
        storage.storage.jsonl_path(session_id, thread_id=thread_id).touch()
    connection = sqlite3.connect(storage.storage.index_path(session_id, thread_id=thread_id))
    try:
        initialize_rollout_schema(connection, initialize_activation=create_resource_activation_schema)
        connection.execute(
            "INSERT INTO schema_migrations "
            "(from_version, to_version, migration_name, migration_checksum, status, "
            "started_at, completed_at) VALUES (0, ?, ?, ?, 'completed', ?, ?)",
            (
                ROLLOUT_SCHEMA_VERSION,
                f"rollout_sqlite_v{ROLLOUT_SCHEMA_VERSION}",
                hashlib.sha256(
                    f"rollout_sqlite_v{ROLLOUT_SCHEMA_VERSION}".encode()
                ).hexdigest(),
                _CREATED_AT,
                _UPDATED_AT,
            ),
        )
        connection.execute(
            "INSERT INTO database_meta "
            "(singleton_id, rollout_id, session_id, thread_id, schema_version, "
            "message_format_version, database_state, last_commit_id, "
            "last_message_sequence, last_control_sequence, committed_jsonl_offset, "
            "active_branch_id, projection_epoch, created_at, updated_at, "
            "rollout_format_version, last_item_sequence, history_view_revision, "
            "source_overlay_epoch) "
            "VALUES (1, ?, ?, ?, ?, 1, 'active', NULL, 0, 0, 0, ?, 1, ?, ?, ?, 0, 0, 0)",
            (
                f"rollout-{create_uuid_hex()}",
                session_id,
                thread_id,
                ROLLOUT_SCHEMA_VERSION,
                _BRANCH_ID,
                _CREATED_AT,
                _UPDATED_AT,
                ROLLOUT_FORMAT_VERSION,
            ),
        )
        connection.execute(
            "INSERT INTO branches "
            "(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, "
            "created_at, updated_at) VALUES (?, 'root', 'active', ?, NULL, ?, ?)",
            (_BRANCH_ID, _VIEW_ID, _CREATED_AT, _UPDATED_AT),
        )
        connection.execute(
            "INSERT INTO checkpoint_namespace_state "
            "(checkpoint_ns, active_branch_id, projection_epoch, created_at, updated_at) "
            "VALUES ('', ?, 1, ?, ?)",
            (_BRANCH_ID, _CREATED_AT, _UPDATED_AT),
        )
        connection.execute(
            "INSERT INTO context_views "
            "(view_id, branch_id, parent_view_id, view_kind, head_turn_id, "
            "head_message_sequence, logical_turn_count, created_at) "
            "VALUES (?, ?, NULL, 'root', NULL, 0, 0, ?)",
            (_VIEW_ID, _BRANCH_ID, _CREATED_AT),
        )
        connection.commit()
    finally:
        connection.close()


def _display_specs(count: int) -> tuple[DisplaySpec, ...]:
    return tuple(DisplaySpec(sequence, sequence) for sequence in range(1, count + 1))


def _turn_specs(count: int) -> tuple[TurnSpec, ...]:
    return tuple(
        TurnSpec(f"job-{ordinal:04d}", ordinal, ordinal, ordinal)
        for ordinal in range(1, count + 1)
    )


def _load_history(
    environment: HistoryEnvironment,
    request: TurnHistoryLoadRequest,
) -> TurnHistoryPageDTO:
    """走 RolloutHistoryReader 的 session/thread 显式 owner 读取入口。"""
    page = environment.history_reader.load(
        environment.session_id,
        environment.thread_id,
        request,
    )
    for turn in page.items:
        assert turn.job_id == f"history-job:{turn.turn_id}"
        assert turn.job_id != turn.turn_id
    return page


def _internal_display_identities(page: TurnHistoryPageDTO) -> list[tuple[int, int]]:
    executions = page.internal_executions
    return [
        (part.item_sequence, part.logical_item_ordinal)
        for execution in executions
        for part in execution.display_parts
    ]


def test_internal_history_page_enforces_256_item_limit_with_real_jsonl_reads(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(turns=(), displays=_display_specs(257))
    with environment.open_snapshot() as snapshot:
        chain = environment.context_reader.resolve_chain(snapshot)
        bounds = environment.context_reader.read_history_snapshot_bounds(
            snapshot, chain
        )
        page = environment.context_reader.read_internal_execution_history_page(
            snapshot,
            chain,
            direction="before",
            anchor_logical_item_ordinal=None,
            lower_logical_item_ordinal=None,
            upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            limit=256,
        )
        assert len(page.entries) == 256
        assert page.has_more is True
        assert page.entries[0].item.item_sequence == 2
        assert page.entries[-1].item.item_sequence == 257
        with pytest.raises(ValueError, match="limit 必须在 1 到 256 之间"):
            environment.context_reader.read_internal_execution_history_page(
                snapshot,
                chain,
                direction="before",
                anchor_logical_item_ordinal=None,
                lower_logical_item_ordinal=None,
                upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
                snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
                limit=257,
            )


def test_internal_history_page_at_256_item_boundary_has_no_more(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(turns=(), displays=_display_specs(256))
    with environment.open_snapshot() as snapshot:
        chain = environment.context_reader.resolve_chain(snapshot)
        bounds = environment.context_reader.read_history_snapshot_bounds(snapshot, chain)
        page = environment.context_reader.read_internal_execution_history_page(
            snapshot,
            chain,
            direction="before",
            anchor_logical_item_ordinal=None,
            lower_logical_item_ordinal=None,
            upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            limit=256,
        )

    assert len(page.entries) == 256
    assert page.has_more is False
    assert page.entries[0].item.item_sequence == 1
    assert page.entries[-1].item.item_sequence == 256


def test_internal_only_history_pages_from_tail_to_head_without_gaps_or_duplicates(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(turns=(), displays=_display_specs(300))
    tail = _load_history(
        environment,
        TurnHistoryLoadRequest(direction="tail", include=["internal"]),
    )
    older = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=tail.before_cursor,
            include=["internal"],
        ),
    )

    identities = _internal_display_identities(older) + _internal_display_identities(
        tail
    )
    assert len(identities) == 300
    assert [sequence for sequence, _ordinal in identities] == list(range(1, 301))
    assert [ordinal for _sequence, ordinal in identities] == list(range(1, 301))
    assert tail.has_more is True
    assert tail.before_cursor is not None
    assert older.has_more is False
    assert older.before_cursor is None


def test_internal_history_keyset_follows_membership_ordinal_across_physical_reverse_order(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(
        turns=(),
        displays=tuple(
            DisplaySpec(sequence, 300 - sequence)
            for sequence in range(1, 301)
        ),
    )
    tail = _load_history(
        environment,
        TurnHistoryLoadRequest(direction="tail", include=["internal"]),
    )
    cursor = RolloutHistoryReader._decode_cursor_payload(tail.before_cursor or "")
    older = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=tail.before_cursor,
            include=["internal"],
        ),
    )

    assert cursor["logical_item_ordinal"] == 44
    assert cursor["logical_item_read_upper_bound"] == 299
    assert [ordinal for _sequence, ordinal in _internal_display_identities(older)] == list(
        range(44)
    )
    assert [ordinal for _sequence, ordinal in _internal_display_identities(tail)] == list(
        range(44, 300)
    )
    identities = _internal_display_identities(older) + _internal_display_identities(tail)
    assert [ordinal for _sequence, ordinal in identities] == list(range(300))
    assert [sequence for sequence, _ordinal in identities] == list(range(300, 0, -1))
    assert tail.has_more is True
    assert older.has_more is False


def test_internal_history_accepts_zero_membership_ordinal(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(
        turns=(),
        displays=(DisplaySpec(1, 0),),
    )

    page = _load_history(
        environment,
        TurnHistoryLoadRequest(direction="tail", include=["internal"]),
    )

    assert "logical_item_ordinal" not in page.internal_executions[0].model_dump()
    assert page.internal_executions[0].display_parts[0].logical_item_ordinal == 0
    assert page.before_cursor is None


def test_single_internal_execution_response_history_keeps_256_record_pages(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    execution_id = "execution-single-internal"
    displays = (DisplaySpec(1, 1, execution_id),) + tuple(
        DisplaySpec(sequence, sequence, execution_id, is_response=True)
        for sequence in range(2, 302)
    )
    environment = history_environment(turns=(), displays=displays)
    tail = _load_history(
        environment,
        TurnHistoryLoadRequest(direction="tail", include=["internal", "final_response"]),
    )
    assert len(tail.internal_executions) == 1
    assert tail.internal_executions[0].execution_id == execution_id
    assert tail.internal_executions[0].display_parts == []
    assert len(tail.internal_executions[0].response_parts) == 256
    assert tail.before_cursor is not None
    older = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=tail.before_cursor,
            include=["internal", "final_response"],
        ),
    )
    assert len(older.internal_executions) == 1
    assert len(older.internal_executions[0].display_parts) == 1
    assert len(older.internal_executions[0].response_parts) == 44
    sequences = sorted(
        part.source.item_sequence
        for page in (tail, older)
        for entry in page.internal_executions
        for part in entry.response_parts
    )
    assert sequences == list(range(2, 302))
    assert older.has_more is False
    assert older.before_cursor is None


def test_around_uses_gapped_turn_ordinals_and_dense_shared_view_coordinates(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(
        displays=(
            DisplaySpec(2, 70),
            DisplaySpec(3, 20),
            DisplaySpec(5, 90),
            DisplaySpec(6, 60),
            DisplaySpec(7, 50),
            DisplaySpec(9, 30),
        ),
    )
    page = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="around",
            anchor_turn_id="job-0007",
            before_turns=1,
            after_turns=1,
            include=["internal"],
        ),
    )

    assert [item.ordinal for item in page.items] == [2, 7, 13]
    assert [item.root_logical_item_ordinal for item in page.items] == [10, 40, 80]
    assert _internal_display_identities(page) == [
        (3, 20),
        (9, 30),
        (7, 50),
        (6, 60),
        (2, 70),
    ]
    assert page.has_after is True
    assert page.after_cursor is not None
    after_page = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="after",
            cursor=page.after_cursor,
            include=["internal"],
        ),
    )
    assert _internal_display_identities(after_page) == [(5, 90)]


def test_around_before_internal_uses_full_view_snapshot_coordinate(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(
        turns=(
            TurnSpec("job-0002", 2, 1, 10),
            TurnSpec("job-0007", 7, 3, 40),
            TurnSpec("job-0013", 13, 4, 80),
        ),
        displays=(DisplaySpec(2, 5),),
    )
    page = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="around",
            anchor_turn_id="job-0007",
            before_turns=1,
            after_turns=1,
            include=["internal"],
        ),
    )

    assert [item.root_logical_item_ordinal for item in page.items] == [10, 40, 80]
    assert _internal_display_identities(page) == []
    assert page.before_cursor is not None
    before_cursor = RolloutHistoryReader._decode_cursor_payload(page.before_cursor)
    assert before_cursor["turn_ordinal"] is None
    assert before_cursor["logical_item_ordinal"] == 10
    assert before_cursor["logical_item_read_upper_bound"] == 80

    before_page = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=page.before_cursor,
            include=["internal"],
        ),
    )

    assert before_page.items == []
    assert _internal_display_identities(before_page) == [(2, 5)]
    assert before_page.has_before is False
    assert before_page.before_cursor is None


def test_around_without_internal_entries_keeps_full_view_item_fence(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(
        turns=(
            TurnSpec("job-0002", 2, 1, 10),
            TurnSpec("job-0007", 7, 2, 40),
            TurnSpec("job-0013", 13, 3, 80),
        ),
        displays=(),
    )
    with environment.open_snapshot() as snapshot:
        chain = environment.context_reader.resolve_chain(snapshot)
        bounds = environment.context_reader.read_history_snapshot_bounds(snapshot, chain)

    page = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="around",
            anchor_turn_id="job-0007",
            before_turns=1,
            after_turns=1,
            include=["internal"],
        ),
    )

    assert bounds.upper_logical_item_ordinal == 80
    assert _internal_display_identities(page) == []
    assert page.before_cursor is None
    assert page.after_cursor is None


def test_around_internal_continuations_have_no_gaps_or_duplicates(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment(
        turns=(
            TurnSpec("job-0002", 2, 1, 10),
            TurnSpec("job-0007", 7, 3, 40),
            TurnSpec("job-0013", 13, 5, 80),
        ),
        displays=(
            DisplaySpec(2, 5),
            DisplaySpec(4, 25),
            DisplaySpec(6, 50),
            DisplaySpec(7, 90),
            DisplaySpec(8, 110),
        ),
    )
    around_page = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="around",
            anchor_turn_id="job-0007",
            before_turns=1,
            after_turns=1,
            include=["internal"],
        ),
    )
    assert _internal_display_identities(around_page) == [(4, 25), (6, 50)]
    assert around_page.before_cursor is not None
    assert around_page.after_cursor is not None

    before_page = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=around_page.before_cursor,
            include=["internal"],
        ),
    )
    after_page = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="after",
            cursor=around_page.after_cursor,
            include=["internal"],
        ),
    )

    identities = (
        _internal_display_identities(before_page)
        + _internal_display_identities(around_page)
        + _internal_display_identities(after_page)
    )
    assert identities == [
        (2, 5),
        (4, 25),
        (6, 50),
        (7, 90),
        (8, 110),
    ]
    assert before_page.has_before is False
    assert before_page.before_cursor is None
    assert after_page.has_after is False
    assert after_page.after_cursor is None


def test_head_and_tail_read_the_same_view_turn_and_internal_boundaries(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment()
    with environment.open_snapshot() as snapshot:
        chain = environment.context_reader.resolve_chain(snapshot)
        bounds = environment.context_reader.read_history_snapshot_bounds(
            snapshot, chain
        )
        head_turns, _head_more = environment.context_reader.read_context_turn_page(
            snapshot,
            chain,
            direction="head",
            anchor_ordinal=None,
            upper_ordinal=bounds.upper_turn_ordinal,
            limit=1,
        )
        tail_turns, _tail_more = environment.context_reader.read_context_turn_page(
            snapshot,
            chain,
            direction="tail",
            anchor_ordinal=None,
            upper_ordinal=bounds.upper_turn_ordinal,
            limit=1,
        )
        head_internal = environment.context_reader.read_internal_execution_history_page(
            snapshot,
            chain,
            direction="after",
            anchor_logical_item_ordinal=None,
            lower_logical_item_ordinal=None,
            upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            limit=1,
        )
        tail_internal = environment.context_reader.read_internal_execution_history_page(
            snapshot,
            chain,
            direction="before",
            anchor_logical_item_ordinal=None,
            lower_logical_item_ordinal=None,
            upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            limit=1,
        )

    assert HistoryPageReadMixin._span_from_row(head_turns[0]).ordinal == 2
    assert (
        HistoryPageReadMixin._span_from_row(head_turns[0]).root_logical_item_ordinal
        == 10
    )
    assert HistoryPageReadMixin._span_from_row(tail_turns[0]).ordinal == 13
    assert (
        HistoryPageReadMixin._span_from_row(tail_turns[0]).root_logical_item_ordinal
        == 80
    )
    assert head_internal.entries[0].logical_item_ordinal == 20
    assert tail_internal.entries[0].logical_item_ordinal == 90


def test_turn_lane_can_exhaust_before_internal_lane_and_continue(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    turns = (TurnSpec("job-0001", 1, 1, 1),)
    displays = tuple(DisplaySpec(sequence, sequence) for sequence in range(2, 259))
    environment = history_environment(turns=turns, displays=displays)
    first = _load_history(
        environment,
        TurnHistoryLoadRequest(direction="after", turns=1, include=["internal"]),
    )
    cursor = RolloutHistoryReader._decode_cursor_payload(first.after_cursor or "")
    second = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="after",
            cursor=first.after_cursor,
            turns=1,
            include=["internal"],
        ),
    )

    assert [item.ordinal for item in first.items] == [1]
    assert len(_internal_display_identities(first)) == 256
    assert cursor["turn_ordinal"] is None
    assert cursor["logical_item_ordinal"] == 257
    assert second.items == []
    assert _internal_display_identities(second) == [(258, 258)]
    assert second.has_more is False
    assert second.after_cursor is None


def test_internal_lane_can_exhaust_before_turn_lane_and_continue(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    turns = _turn_specs(65)
    displays = (DisplaySpec(66, 66),)
    environment = history_environment(turns=turns, displays=displays)
    first = _load_history(
        environment,
        TurnHistoryLoadRequest(direction="after", turns=64, include=["internal"]),
    )
    cursor = RolloutHistoryReader._decode_cursor_payload(first.after_cursor or "")
    second = _load_history(
        environment,
        TurnHistoryLoadRequest(
            direction="after",
            cursor=first.after_cursor,
            turns=64,
            include=["internal"],
        ),
    )

    assert [item.ordinal for item in first.items] == list(range(1, 65))
    assert _internal_display_identities(first) == [(66, 66)]
    assert cursor["turn_ordinal"] == 64
    assert cursor["logical_item_ordinal"] is None
    assert [item.ordinal for item in second.items] == [65]
    assert second.internal_executions == []
    assert second.has_more is False


@pytest.mark.parametrize("owner_field", ["session_id", "thread_id"])
@pytest.mark.parametrize("upper_logical_item_ordinal", [1, None], ids=["bounded", "empty-bound"])
def test_internal_history_rejects_snapshot_from_another_owner(
    history_environment: Callable[..., HistoryEnvironment],
    owner_field: str,
    upper_logical_item_ordinal: int | None,
) -> None:
    environment = history_environment()
    with environment.open_snapshot() as snapshot:
        chain = environment.context_reader.resolve_chain(snapshot)
        wrong_owner = replace(
            snapshot,
            **{
                owner_field: (
                    f"ses_{create_uuid_hex()}"
                    if owner_field == "session_id"
                    else f"thr_{create_uuid_hex()}"
                )
            },
        )
        # session/thread 错配都必须在同一 database_meta owner 上拒绝，
        # 且 empty-bound 不得绕过该守卫。
        with pytest.raises(RuntimeError, match="owner|session|thread|不匹配"):
            environment.context_reader.read_internal_execution_history_page(
                wrong_owner,
                chain,
                direction="after",
                anchor_logical_item_ordinal=None,
                lower_logical_item_ordinal=None,
                upper_logical_item_ordinal=upper_logical_item_ordinal,
                snapshot_upper_logical_item_ordinal=upper_logical_item_ordinal,
                limit=1,
            )


def test_v1_rollout_is_rejected_by_the_real_context_reader(
    history_environment: Callable[..., HistoryEnvironment],
) -> None:
    environment = history_environment()
    with environment.storage.storage._connect(
        environment.thread_id,
        session_id=environment.session_id,
    ) as connection:
        connection.execute(
            "UPDATE database_meta SET rollout_format_version = 1 WHERE singleton_id = 1"
        )
        connection.commit()

    with pytest.raises(FormatDispatchError, match="v1_migration_required"):
        environment.open_snapshot()


@pytest.mark.parametrize(
    "corruption",
    ["duplicate", "negative", "missing_root", "turn_order"],
)
def test_context_view_order_integrity_rejects_corrupt_coordinates(
    history_environment: Callable[..., HistoryEnvironment],
    corruption: str,
) -> None:
    environment = history_environment()
    with environment.storage.storage._connect(
        environment.thread_id,
        session_id=environment.session_id,
    ) as connection:
        if corruption == "negative":
            mutate_view_members(connection,
                "UPDATE context_view_items SET logical_item_ordinal = -1 "
                "WHERE view_id = ? AND item_id = 'root-job-0002'",
                (_VIEW_ID,),
            )
        elif corruption == "missing_root":
            mutate_view_members(connection,
                "DELETE FROM context_view_items WHERE view_id = ? AND item_id = 'root-job-0002'",
                (_VIEW_ID,),
            )
        elif corruption == "turn_order":
            mutate_view_members(connection,
                "UPDATE context_view_items SET logical_item_ordinal = 45 "
                "WHERE view_id = ? AND item_id = 'root-job-0002'",
                (_VIEW_ID,),
            )
            mutate_view_members(connection,
                "UPDATE context_view_items SET logical_item_ordinal = 35 "
                "WHERE view_id = ? AND item_id = 'root-job-0007'",
                (_VIEW_ID,),
            )
        else:
            mutate_view_members(
                connection,
                "UPDATE context_view_items SET logical_item_ordinal = 10 "
                "WHERE view_id = ? AND item_id = 'internal-item-2'",
                (_VIEW_ID,),
            )
        connection.commit()

    with environment.open_snapshot() as snapshot:
        chain = environment.context_reader.resolve_chain(snapshot)
        with pytest.raises(
            RuntimeError, match="projection integrity|logical_item_ordinal 重复"
        ):
            environment.context_reader.read_history_snapshot_bounds(snapshot, chain)
