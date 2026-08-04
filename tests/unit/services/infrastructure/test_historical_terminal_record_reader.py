from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.infrastructure.historical_terminal_record_reader import (
    HistoricalTerminalRecordReader,
)


def test_reader_ignores_terminal_results_copied_from_parent_context(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path, "ses_child")
    reader = HistoricalTerminalRecordReader(
        sessions_dir=tmp_path,
    )
    copied_record = {
        "type": "tool",
        "name": "exec_command",
        "content": json.dumps(
            {
                "chunk_id": "term_parent",
                "session_id": "term_parent",
            }
        ),
        "response_metadata": {
            "context_fork_source_session_id": "ses_parent",
        },
    }

    records = reader.read_records(
        session_id="ses_child",
        active_terminals=[],
        agent_state_records=[copied_record],
    )

    assert records == []


def test_reader_keeps_terminal_results_created_in_current_context(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path, "ses_child")
    reader = HistoricalTerminalRecordReader(
        sessions_dir=tmp_path,
    )
    native_record = {
        "type": "tool",
        "name": "exec_command",
        "content": json.dumps(
            {
                "chunk_id": "term_child",
                "session_id": "term_child",
            }
        ),
    }

    records = reader.read_records(
        session_id="ses_child",
        active_terminals=[],
        agent_state_records=[native_record],
    )

    assert [record["terminal_id"] for record in records] == ["term_child"]


def test_reader_recovers_legacy_truncated_json_with_raw_newlines(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path, "ses_legacy")
    reader = HistoricalTerminalRecordReader(sessions_dir=tmp_path)
    legacy_content = (
        '{"chunk_id": "term_legacy", "output": "head\n\n'
        '... 工具输出过大 ...\n\ntail", "exit_code": 0}'
    )

    records = reader.read_records(
        session_id="ses_legacy",
        active_terminals=[],
        agent_state_records=[
            {
                "type": "tool",
                "name": "exec_command",
                "content": legacy_content,
            }
        ],
    )

    assert [record["terminal_id"] for record in records] == ["term_legacy"]


def test_reader_still_reports_irrecoverable_exec_command_json(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path, "ses_invalid")
    reader = HistoricalTerminalRecordReader(sessions_dir=tmp_path)

    with pytest.raises(json.JSONDecodeError, match="Expecting property name"):
        reader.read_records(
            session_id="ses_invalid",
            active_terminals=[],
            agent_state_records=[
                {
                    "type": "tool",
                    "name": "exec_command",
                    "content": '{"chunk_id": "term_invalid",}',
                }
            ],
        )
