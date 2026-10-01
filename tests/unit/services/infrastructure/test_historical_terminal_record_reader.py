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
    session_bundle_factory(tmp_path, "ses_3f6bd5ea76cd451389bb1f0b3fa2127a")
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
        session_id="ses_3f6bd5ea76cd451389bb1f0b3fa2127a",
        active_terminals=[],
        agent_state_records=[copied_record],
    )

    assert records == []


def test_reader_keeps_terminal_results_created_in_current_context(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path, "ses_3f6bd5ea76cd451389bb1f0b3fa2127a")
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
        session_id="ses_3f6bd5ea76cd451389bb1f0b3fa2127a",
        active_terminals=[],
        agent_state_records=[native_record],
    )

    assert [record["terminal_id"] for record in records] == ["term_child"]


def test_reader_recovers_legacy_truncated_json_with_raw_newlines(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path, "ses_022688effb79462d886cfd4edec831e4")
    reader = HistoricalTerminalRecordReader(sessions_dir=tmp_path)
    legacy_content = (
        '{"chunk_id": "term_legacy", "output": "head\n\n'
        '... 工具输出过大 ...\n\ntail", "exit_code": 0}'
    )

    records = reader.read_records(
        session_id="ses_022688effb79462d886cfd4edec831e4",
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
    session_bundle_factory(tmp_path, "ses_425515d88940460d87e63f57334ff320")
    reader = HistoricalTerminalRecordReader(sessions_dir=tmp_path)

    with pytest.raises(json.JSONDecodeError, match="Expecting property name"):
        reader.read_records(
            session_id="ses_425515d88940460d87e63f57334ff320",
            active_terminals=[],
            agent_state_records=[
                {
                    "type": "tool",
                    "name": "exec_command",
                    "content": '{"chunk_id": "term_invalid",}',
                }
            ],
        )


def _write_tool_call_end_trace(
    bundle: Path,
    *,
    terminal_id: str,
    timestamp: str,
) -> None:
    trace_dir = bundle / "logs" / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "messages.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_call_end",
                "timestamp": timestamp,
                "payload": {"tool_name": "exec_command"},
                "terminal_id": terminal_id,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _real_exec_command_record(terminal_id: str) -> dict[str, object]:
    # 逐字复刻真实会话 rollout 中的 exec_command 工具结果 payload：
    # 只有 chunk_id/terminal_id/wall_time_seconds/original_token_count/output/
    # exit_code/status/cwd，不含任何时间字段。
    return {
        "type": "tool",
        "name": "exec_command",
        "content": json.dumps(
            {
                "chunk_id": "12fa9d",
                "terminal_id": terminal_id,
                "wall_time_seconds": 3.0465938829584047,
                "original_token_count": 29,
                "output": "/tmp/ws\nhello",
                "exit_code": 0,
                "status": "success",
                "cwd": "/tmp/ws",
            },
            ensure_ascii=False,
        ),
    }


def test_reader_takes_historical_terminal_times_from_trace_tool_call_end(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """历史终端三个时间字段必须来自 Trace 的 tool_call_end 事件时间。"""
    session_id = "ses_3f6bd5ea76cd451389bb1f0b3fa2127a"
    bundle = session_bundle_factory(tmp_path, session_id)
    trace_time = "2026-09-13T06:18:47.823837Z"
    _write_tool_call_end_trace(
        bundle,
        terminal_id="term_trace_time",
        timestamp=trace_time,
    )
    reader = HistoricalTerminalRecordReader(sessions_dir=tmp_path)

    records = reader.read_records(
        session_id=session_id,
        active_terminals=[],
        agent_state_records=[_real_exec_command_record("term_trace_time")],
    )

    assert len(records) == 1
    terminal = records[0]
    assert terminal["created_at"] == trace_time
    assert terminal["updated_at"] == trace_time
    assert terminal["ended_at"] == trace_time
    assert terminal["last_command_completed_at"] == trace_time


def test_reader_uses_epoch_when_historical_terminal_trace_is_missing(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """Trace 缺失时三个时间字段统一退回 epoch 兜底，不得产生虚假时间。"""
    session_id = "ses_022688effb79462d886cfd4edec831e4"
    session_bundle_factory(tmp_path, session_id)
    reader = HistoricalTerminalRecordReader(sessions_dir=tmp_path)

    records = reader.read_records(
        session_id=session_id,
        active_terminals=[],
        agent_state_records=[_real_exec_command_record("term_without_trace")],
    )

    assert len(records) == 1
    terminal = records[0]
    assert terminal["created_at"] == "1970-01-01T00:00:00+00:00"
    assert terminal["updated_at"] == "1970-01-01T00:00:00+00:00"
    assert terminal["ended_at"] == "1970-01-01T00:00:00+00:00"
