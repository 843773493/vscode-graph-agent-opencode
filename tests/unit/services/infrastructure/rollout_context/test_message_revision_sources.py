"""只验证 canonical revision source 的局部 SQLite 合同。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
    ProjectionGroup,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.message_revisions import (
    validate_message_revision_source,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)


@pytest.fixture
def revision_database():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript("""
            CREATE TABLE database_meta(
                singleton_id INTEGER PRIMARY KEY, session_id TEXT, thread_id TEXT,
                database_state TEXT
            );
            INSERT INTO database_meta VALUES (1, 'session', 'thread', 'active');
            CREATE TABLE executions(
                execution_id TEXT PRIMARY KEY, session_id TEXT, thread_id TEXT
            );
            CREATE TABLE model_calls(
                model_call_id TEXT PRIMARY KEY, session_id TEXT, thread_id TEXT,
                execution_id TEXT, assembly_id TEXT
            );
            CREATE TABLE context_assemblies(
                assembly_id TEXT PRIMARY KEY, session_id TEXT, thread_id TEXT,
                execution_id TEXT
            );
            CREATE TABLE item_catalog(
                item_id TEXT PRIMARY KEY, item_sequence INTEGER, execution_id TEXT,
                turn_id TEXT, semantic_kind TEXT, turn_scope TEXT,
                message_group_id TEXT, projection_manifest_json TEXT
            );
            CREATE TABLE turn_records(turn_id TEXT, root_input_item_id TEXT);
        """)
        yield connection


def _source_item(
    *,
    semantic_kind: SemanticKind = SemanticKind.ASSISTANT_OUTPUT,
    turn_scope: TurnScope = TurnScope.TURN_MEMBER,
    projection_message_id: str = "logical-message",
    model_call_id: str | None = "source-call",
) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=10,
        item_id="source-item",
        semantic_kind=semantic_kind,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "user" if turn_scope == TurnScope.TURN_ROOT else "provider",
            "producer_id": projection_message_id,
            "invocation_id": "source-execution",
        },
        payload="source body",
        projection_identity=ItemProjectionIdentity(
            projection_message_id=projection_message_id,
            model_call_id=model_call_id,
        ),
        turn_id="turn",
        turn_scope=turn_scope,
        message_group_id="source-group",
    )


def _revision_item(
    *,
    relations: tuple[ItemProvenanceRelation, ...] | None = None,
    projection_message_id: str = "logical-message",
    model_call_id: str | None = "source-call",
    turn_id: str | None = "turn",
    item_sequence: int = 20,
) -> CanonicalItemRecord:
    if relations is None:
        relations = (ItemProvenanceRelation(
            relation="replaces",
            target_kind="canonical_item",
            target_id="source-item",
            target_session_id="session",
            target_thread_id="thread",
            target_turn_id="turn",
            target_model_call_id="source-call",
        ),)
    return CanonicalItemRecord.create(
        item_sequence=item_sequence,
        item_id="revision-item",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "middleware",
            "producer_id": "logical-message",
            "invocation_id": "current-execution",
        },
        payload="revised body",
        projection_identity=ItemProjectionIdentity(
            projection_message_id=projection_message_id,
            model_call_id=model_call_id,
        ),
        provenance_relations=relations,
        turn_id=turn_id,
        turn_scope=TurnScope.TURN_MEMBER if turn_id is not None else None,
        message_group_id="revision-group",
    )


def _insert_source(
    connection: sqlite3.Connection,
    source: CanonicalItemRecord | None = None,
    *,
    execution_id: str = "source-execution",
    session_id: str = "session",
    thread_id: str = "thread",
    turn_id: str | None = "turn",
    assembly_execution_id: str = "source-execution",
) -> None:
    source = source or _source_item()
    connection.execute(
        "INSERT INTO item_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source.item_id,
            source.item_sequence,
            execution_id,
            turn_id,
            source.semantic_kind,
            source.turn_scope,
            source.message_group_id,
            canonical_json_text(source.projection_manifest()),
        ),
    )
    connection.execute(
        "INSERT INTO executions VALUES (?, ?, ?)",
        (execution_id, session_id, thread_id),
    )
    connection.execute(
        "INSERT INTO context_assemblies VALUES (?, ?, ?, ?)",
        ("source-assembly", session_id, thread_id, assembly_execution_id),
    )
    connection.execute(
        "INSERT INTO model_calls VALUES (?, ?, ?, ?, ?)",
        ("source-call", session_id, thread_id, execution_id, "source-assembly"),
    )


def test_validates_exact_source_and_allows_current_producer_to_retain_source_call(
    revision_database: sqlite3.Connection,
) -> None:
    _insert_source(revision_database)

    source = validate_message_revision_source(
        revision_database, _revision_item(),
    )

    assert source is not None
    assert source.item_id == "source-item"
    assert source.item_sequence == 10
    assert (source.session_id, source.thread_id, source.turn_id) == (
        "session", "thread", "turn",
    )
    assert source.execution_id == "source-execution"
    assert source.model_call_id == "source-call"
    assert source.message_group_id == "source-group"


def test_no_exact_replacement_returns_none(revision_database: sqlite3.Connection) -> None:
    assert validate_message_revision_source(
        revision_database, _revision_item(relations=()),
    ) is None


def test_stream_projection_replacement_keeps_its_existing_contract(revision_database) -> None:
    relation = ItemProvenanceRelation(
        relation="replaces", target_kind="projection_message", target_id="stream-shadow",
        target_model_call_id="source-call",
    )
    assert validate_message_revision_source(
        revision_database, _revision_item(relations=(relation,)),
    ) is None


def test_source_and_revision_must_be_group_anchors(revision_database) -> None:
    _insert_source(revision_database)
    companion = replace(_revision_item(), projection_group=ProjectionGroup(0, 2, "str"))
    with pytest.raises(ValueError, match="由消息组 anchor"):
        validate_message_revision_source(revision_database, companion)
    source = replace(_source_item(), projection_group=ProjectionGroup(0, 2, "str"))
    revision_database.execute(
        "UPDATE item_catalog SET projection_manifest_json=? WHERE item_id='source-item'",
        (canonical_json_text(source.projection_manifest()),),
    )
    with pytest.raises(ValueError, match="source 必须是消息组 anchor"):
        validate_message_revision_source(revision_database, _revision_item())


@pytest.mark.parametrize("group_id", [None, "source-group"])
def test_revision_requires_its_own_message_group(revision_database, group_id) -> None:
    _insert_source(revision_database)
    with pytest.raises(ValueError, match="独立的新消息组"):
        validate_message_revision_source(
            revision_database, replace(_revision_item(), message_group_id=group_id),
        )


def test_revision_cannot_reuse_an_unrelated_group(revision_database) -> None:
    _insert_source(revision_database)
    revision_database.execute(
        "INSERT INTO item_catalog SELECT 'unrelated-item',item_sequence+1,execution_id,"
        "turn_id,semantic_kind,turn_scope,'revision-group',projection_manifest_json "
        "FROM item_catalog WHERE item_id='source-item'",
    )
    with pytest.raises(ValueError, match="其它 anchor 占用"):
        validate_message_revision_source(revision_database, _revision_item())


def test_rejects_multiple_replacement_sources(revision_database: sqlite3.Connection) -> None:
    _insert_source(revision_database)
    first = _revision_item().provenance_relations[0]
    second = ItemProvenanceRelation(
        relation="replaces", target_kind="canonical_item", target_id="another",
        target_session_id="session", target_thread_id="thread",
        target_turn_id="turn", target_model_call_id="source-call",
    )
    with pytest.raises(ValueError, match="只有一个"):
        validate_message_revision_source(
            revision_database, _revision_item(relations=(first, second)),
        )


def test_rejects_missing_source_and_non_earlier_source(revision_database: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="source item 缺失"):
        validate_message_revision_source(revision_database, _revision_item())

    _insert_source(revision_database)
    with pytest.raises(ValueError, match="严格早于"):
        validate_message_revision_source(
            revision_database, _revision_item(item_sequence=10),
        )


@pytest.mark.parametrize(
    ("source", "revision", "message"),
    [
        (_source_item(projection_message_id="other"), _revision_item(), "logical projection ID"),
        (_source_item(), _revision_item(turn_id="other-turn"), "source Turn"),
        (_source_item(), _revision_item(projection_message_id="other"), "logical projection ID"),
    ],
)
def test_rejects_source_logical_identity_or_turn_mismatch(
    revision_database: sqlite3.Connection,
    source: CanonicalItemRecord,
    revision: CanonicalItemRecord,
    message: str,
) -> None:
    _insert_source(revision_database, source)
    with pytest.raises(ValueError, match=message):
        validate_message_revision_source(revision_database, revision)


def test_rejects_relation_model_call_and_execution_assembly_mismatch(
    revision_database: sqlite3.Connection,
) -> None:
    _insert_source(revision_database)
    relation = ItemProvenanceRelation(
        relation="replaces", target_kind="canonical_item", target_id="source-item",
        target_session_id="session", target_thread_id="thread",
        target_turn_id="turn", target_model_call_id="different-call",
    )
    with pytest.raises(ValueError, match="source ModelCall relation"):
        validate_message_revision_source(
            revision_database, _revision_item(relations=(relation,)),
        )

    revision_database.execute(
        "UPDATE model_calls SET execution_id='different-execution' WHERE model_call_id='source-call'",
    )
    with pytest.raises(ValueError, match="ModelCall/assembly owner"):
        validate_message_revision_source(revision_database, _revision_item())


def test_rejects_unbound_historical_scope(revision_database: sqlite3.Connection) -> None:
    _insert_source(revision_database)
    relation = ItemProvenanceRelation(
        relation="replaces", target_kind="canonical_item", target_id="source-item",
        target_session_id="foreign-session", target_thread_id="thread",
        target_turn_id="turn", target_model_call_id="source-call",
    )
    with pytest.raises(ValueError, match="历史 SessionThread"):
        validate_message_revision_source(
            revision_database, _revision_item(relations=(relation,)),
        )


def test_rejects_accepted_root_replacement(revision_database: sqlite3.Connection) -> None:
    source = _source_item(
        semantic_kind=SemanticKind.USER_INPUT,
        turn_scope=TurnScope.TURN_ROOT,
        model_call_id=None,
    )
    _insert_source(revision_database, source)
    revision_database.execute(
        "INSERT INTO turn_records VALUES ('turn', 'source-item')",
    )
    with pytest.raises(ValueError, match="accepted root"):
        validate_message_revision_source(revision_database, _revision_item())
