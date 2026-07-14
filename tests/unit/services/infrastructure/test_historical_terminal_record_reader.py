from __future__ import annotations

import json
from pathlib import Path

from app.services.infrastructure.historical_terminal_record_reader import (
    HistoricalTerminalRecordReader,
)


def test_reader_ignores_terminal_results_copied_from_parent_context(
    tmp_path: Path,
) -> None:
    reader = HistoricalTerminalRecordReader(
        logs_dir=tmp_path,
        attach_url=lambda terminal_id: f"http://terminal/{terminal_id}",
    )
    copied_record = {
        "type": "tool",
        "name": "persistent_terminal",
        "content": json.dumps(
            {
                "terminal_id": "term_parent",
                "status": "running",
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
) -> None:
    reader = HistoricalTerminalRecordReader(
        logs_dir=tmp_path,
        attach_url=lambda terminal_id: f"http://terminal/{terminal_id}",
    )
    native_record = {
        "type": "tool",
        "name": "persistent_terminal",
        "content": json.dumps(
            {
                "terminal_id": "term_child",
                "status": "running",
            }
        ),
    }

    records = reader.read_records(
        session_id="ses_child",
        active_terminals=[],
        agent_state_records=[native_record],
    )

    assert [record["terminal_id"] for record in records] == ["term_child"]
