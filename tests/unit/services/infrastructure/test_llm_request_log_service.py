from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.infrastructure.llm_request_log_service import LLMRequestLogService


def write_log(sessions_dir: Path, session_id: str, timestamp: int, payload: dict) -> Path:
    session_dir = sessions_dir / session_id / "logs" / "llm_requests"
    session_dir.mkdir(parents=True, exist_ok=True)
    log_file = session_dir / f"{timestamp}.json"
    log_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return log_file


def test_list_session_logs_reads_request_and_response(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_read"
    second = write_log(
        sessions_dir,
        session_id,
        2000,
        {
            "timestamp": 2000,
            "session_id": session_id,
            "job_id": "job_2",
            "request": {"messages": [{"type": "human", "content": "后一个"}]},
            "response": {"result": [{"type": "ai", "content": "响应二"}]},
        },
    )
    first = write_log(
        sessions_dir,
        session_id,
        1000,
        {
            "timestamp": 1000,
            "session_id": session_id,
            "job_id": "job_1",
            "request": {"messages": [{"type": "human", "content": "前一个"}]},
            "response": {"result": [{"type": "ai", "content": "响应一"}]},
        },
    )

    records = LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs(session_id)

    assert [record.timestamp for record in records] == [1000, 2000]
    assert [record.file_path for record in records] == [str(first), str(second)]
    assert records[0].request["messages"][0]["content"] == "前一个"
    assert records[0].response["result"][0]["content"] == "响应一"


def test_list_session_logs_returns_empty_for_missing_session(tmp_path: Path):
    records = LLMRequestLogService(sessions_dir=tmp_path / "sessions").list_session_logs("ses_missing")

    assert records == []


def test_list_session_logs_exposes_invalid_log_file(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    session_dir = sessions_dir / "ses_bad" / "logs" / "llm_requests"
    session_dir.mkdir(parents=True)
    (session_dir / "1000.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="不是 JSON object"):
        LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs("ses_bad")


def test_list_session_logs_exposes_missing_response(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    write_log(
        sessions_dir,
        "ses_bad_shape",
        1000,
        {
            "timestamp": 1000,
            "session_id": "ses_bad_shape",
            "request": {"messages": []},
        },
    )

    with pytest.raises(ValueError, match="缺少 response object"):
        LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs("ses_bad_shape")
