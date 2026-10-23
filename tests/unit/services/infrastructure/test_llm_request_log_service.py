from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.infrastructure.llm_request_log_service import LLMRequestLogService


def write_log(session_dir: Path, filename_timestamp_ns: int, payload: dict) -> Path:
    log_dir = session_dir / "logs" / "llm_requests"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{filename_timestamp_ns}.json"
    log_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return log_file


def test_list_session_logs_reads_request_and_response(
    tmp_path: Path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b9a5623b97db291238e0644b0b4ae"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    second = write_log(
        session_dir,
        2_000_000_002,
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
        1_000_000_001,
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
    assert [record.file_name for record in records] == [first.name, second.name]
    assert all("file_path" not in record.model_dump() for record in records)
    assert records[0].request["messages"][0]["content"] == "前一个"
    assert records[0].response["result"][0]["content"] == "响应一"
    assert records[0].upstream == {"attempts": []}


def test_list_session_logs_returns_empty_without_log_files(
    tmp_path: Path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019be2d298c276f18933ff78b7dcd8bb"
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
    session_id = "ses_019c47ed3c2570fe8cfaaba65b26f15f"
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
    session_id = "ses_019c257bff2c7c6989d1c38a6ae7fbf2"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    write_log(
        session_dir,
        1_000_000_001,
        {
            "timestamp": 1000,
            "session_id": session_id,
            "request": {"messages": []},
        },
    )

    with pytest.raises(TypeError, match="缺少 response object"):
        LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs(session_id)


@pytest.mark.parametrize("timestamp", [None, "1000", 1000.0, True])
def test_log_body_requires_epoch_milliseconds_without_filename_fallback(
    tmp_path: Path,
    session_bundle_factory,
    timestamp: object,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c257bff2c7c6989d1c38a6ae7fbf2"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    payload: dict[str, object] = {"request": {"messages": []}, "response": {"result": []}}
    if timestamp is not None:
        payload["timestamp"] = timestamp
    write_log(session_dir, 1_000_000_001, payload)

    with pytest.raises(TypeError, match="timestamp 必须是 epoch 毫秒整数"):
        LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs(session_id)
