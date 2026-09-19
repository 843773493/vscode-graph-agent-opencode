from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.infrastructure.llm_request_log_service import LLMRequestLogService


def write_log(session_dir: Path, timestamp: int, payload: dict) -> Path:
    log_dir = session_dir / "logs" / "llm_requests"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{timestamp}.json"
    log_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return log_file


def test_list_session_logs_reads_request_and_response(
    tmp_path: Path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_56dd25ee0c7a40618621b7492d151691"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    second = write_log(
        session_dir,
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
        session_dir,
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
    assert records[0].upstream == {"attempts": []}


def test_list_session_logs_returns_empty_without_log_files(
    tmp_path: Path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_748ad3f52a2842748bf651d80f475960"
    session_bundle_factory(sessions_dir, session_id)
    records = LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs(
        session_id
    )

    assert records == []


def test_list_session_logs_exposes_invalid_log_file(
    tmp_path: Path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_64a3feef8e4d4177829260fdfc01db12"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    log_dir = session_dir / "logs" / "llm_requests"
    log_dir.mkdir(parents=True)
    (log_dir / "1000.json").write_text("[]", encoding="utf-8")

    with pytest.raises(TypeError, match="不是 JSON object"):
        LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs(session_id)


def test_list_session_logs_exposes_missing_response(
    tmp_path: Path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_d1080d1dc6864a638064642b7bf6aef3"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    write_log(
        session_dir,
        1000,
        {
            "timestamp": 1000,
            "session_id": session_id,
            "request": {"messages": []},
        },
    )

    with pytest.raises(TypeError, match="缺少 response object"):
        LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs(session_id)
