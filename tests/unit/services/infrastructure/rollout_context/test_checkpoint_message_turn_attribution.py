"""真实 checkpoint writer 的 Turn attribution 合同。"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage

SESSION_ID = "ses_019c3635a6c278efb831fe265bdcd656"
SOURCE_TURN_ID = "source-job-1"
REPORT_BACK_TURN_ID = "report-back-job-1"


def _checkpoint(checkpoint_id: str, messages: list[object]) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": checkpoint_id}
    checkpoint["updated_channels"] = ["messages"]
    return checkpoint


def _make_saver(
    tmp_path: Path, session_bundle_factory
) -> tuple[RolloutStorage, RolloutCheckpointSaver]:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutStorage(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    return storage, RolloutCheckpointSaver(sessions_dir, storage=storage)


def test_internal_report_back_root_sets_following_model_start_turn(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    storage, saver = _make_saver(tmp_path, session_bundle_factory)
    source_user = HumanMessage(
        content="source input",
        id="source-user",
        response_metadata={
            "message_metadata": {
                "turn_id": SOURCE_TURN_ID,
                "job_id": SOURCE_TURN_ID,
            }
        },
    )
    source_final = AIMessage(content="source answer", id="source-final")
    source_config = saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("checkpoint-source", [source_user, source_final]),
        {"source": "unit", "step": 1},
        {"messages": "1"},
    )
    saver.finalize_turn(
        session_id=SESSION_ID,
        turn_id=SOURCE_TURN_ID,
        final_message_id="source-final",
    )

    report_back_root = HumanMessage(
        content="child result",
        id="report-back-root",
        response_metadata={
            "internal": True,
            "message_metadata": {
                "internal": True,
                "turn_id": REPORT_BACK_TURN_ID,
                "job_id": REPORT_BACK_TURN_ID,
            },
        },
    )
    model_start = AIMessage(content="", id="lc_run--report-back-model-call")
    report_back_final = AIMessage(
        content="report-back answer",
        id="report-back-final",
        response_metadata={
            "message_metadata": {
                "turn_id": REPORT_BACK_TURN_ID,
                "job_id": REPORT_BACK_TURN_ID,
            }
        },
    )
    saver.put(
        source_config,
        _checkpoint(
            "checkpoint-report-back",
            [
                source_user,
                source_final,
                report_back_root,
                model_start,
                report_back_final,
            ],
        ),
        {"source": "unit", "step": 2},
        {"messages": "2"},
    )
    saver.finalize_turn(
        session_id=SESSION_ID,
        turn_id=REPORT_BACK_TURN_ID,
        final_message_id="report-back-final",
    )

    with storage.open_read_snapshot(SESSION_ID) as snapshot:
        source_projection = snapshot.connection.execute(
            "SELECT final_message_id FROM turns WHERE turn_id = ?",
            (SOURCE_TURN_ID,),
        ).fetchone()
        message_turns = snapshot.connection.execute(
            "SELECT message_id, turn_id FROM messages "
            "WHERE message_id IN (?, ?) ORDER BY message_sequence",
            ("lc_run--report-back-model-call", "report-back-final"),
        ).fetchall()

    assert source_projection == ("source-final",)
    assert message_turns == [
        ("lc_run--report-back-model-call", REPORT_BACK_TURN_ID),
        ("report-back-final", REPORT_BACK_TURN_ID),
    ]
    for turn_id in (SOURCE_TURN_ID, REPORT_BACK_TURN_ID):
        page = saver.load_history(
            SESSION_ID,
            TurnHistoryLoadRequest(turn_ids=[turn_id]),
        )
        assert [item.turn_id for item in page.items] == [turn_id]


def test_leading_internal_root_without_execution_identity_stays_unindexed(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    storage, saver = _make_saver(tmp_path, session_bundle_factory)
    internal_root = HumanMessage(
        content="unscoped internal notice",
        id="unscoped-internal-root",
        response_metadata={"internal": True},
    )
    following_model_start = AIMessage(content="", id="lc_run--unscoped-model-call")
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("checkpoint-unscoped", [internal_root, following_model_start]),
        {"source": "unit", "step": 1},
        {"messages": "1"},
    )

    with storage.open_read_snapshot(SESSION_ID) as snapshot:
        turns = snapshot.connection.execute("SELECT turn_id FROM turns").fetchall()

    assert turns == []
