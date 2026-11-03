from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.infrastructure.llm_request_log_service import LLMRequestLogService


def write_llm_request_log(
    session_directory: Path,
    filename_timestamp_ns: int,
    log_payload: dict[str, object],
) -> Path:
    request_log_directory = session_directory / "logs" / "llm_requests"
    request_log_directory.mkdir(parents=True, exist_ok=True)
    log_file = request_log_directory / f"{filename_timestamp_ns}.json"
    log_file.write_text(json.dumps(log_payload, ensure_ascii=False), encoding="utf-8")
    return log_file


def test_list_session_logs_reads_request_and_response(
    tmp_path: Path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b9a5623b97db291238e0644b0b4ae"
    session_directory = session_bundle_factory(sessions_dir, session_id)
    second = write_llm_request_log(
        session_directory,
        2_000_000_002,
        {
            "timestamp": 2000,
            "session_id": session_id,
            "job_id": "job_2",
            "request": {"messages": [{"type": "human", "content": "后一个"}]},
            "response": {"result": [{"type": "ai", "content": "响应二"}]},
        },
    )
    first = write_llm_request_log(
        session_directory,
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
    session_directory = session_bundle_factory(sessions_dir, session_id)
    request_log_directory = session_directory / "logs" / "llm_requests"
    request_log_directory.mkdir(parents=True)
    (request_log_directory / "1000.json").write_text("[]", encoding="utf-8")

    with pytest.raises(TypeError, match="不是 JSON object"):
        LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs(session_id)


def test_list_session_logs_exposes_missing_response(
    tmp_path: Path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c257bff2c7c6989d1c38a6ae7fbf2"
    session_directory = session_bundle_factory(sessions_dir, session_id)
    write_llm_request_log(
        session_directory,
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
    session_directory = session_bundle_factory(sessions_dir, session_id)
    log_payload: dict[str, object] = {"request": {"messages": []}, "response": {"result": []}}
    if timestamp is not None:
        log_payload["timestamp"] = timestamp
    write_llm_request_log(session_directory, 1_000_000_001, log_payload)

    with pytest.raises(TypeError, match="timestamp 必须是 epoch 毫秒整数"):
        LLMRequestLogService(sessions_dir=sessions_dir).list_session_logs(session_id)


@pytest.fixture
def searchable_logs(tmp_path: Path, session_bundle_factory):
    from app.services.infrastructure.llm_request_log_service import LogSearchBudget

    session_id = "ses_019c257bff2c7c6989d1c38a6ae7fbf2"
    root = tmp_path / "sessions"
    directory = session_bundle_factory(root, session_id)
    path = write_llm_request_log(directory, 1000, {
        "session_id": session_id, "timestamp": 100,
        "request": {"prompt": "开始😀\n请求词😀，再来请求词😀，结束"}, "response": {"result": []},
    })
    return LLMRequestLogService(root), session_id, path, lambda: LogSearchBudget(check=lambda: None)


def test_search_and_exact_location_preserve_unicode_and_do_not_load_all_logs(searchable_logs, monkeypatch):
    service, session_id, path, budget = searchable_logs
    monkeypatch.setattr(service, "list_session_logs", lambda _: pytest.fail("搜索不能拉全量列表"))
    page = service.search_session_logs(session_id, "请求词😀", limit=1, position=None, budget=budget())
    assert len(page.items) == 1 and not page.complete and page.position
    hit = page.items[0]
    location = service.locate(hit.reference)
    assert location.record.file_name == path.name
    assert location.text[hit.reference.start_codepoint:hit.reference.end_codepoint] == "请求词😀"
    assert hit.preview[hit.reference.start_codepoint-hit.preview_start_codepoint:hit.reference.end_codepoint-hit.preview_start_codepoint] == "请求词😀"
    second = service.search_session_logs(session_id, "请求词😀", limit=1, position=page.position, budget=budget())
    assert len(second.items) == 1 and second.items[0].reference.start_codepoint > hit.reference.start_codepoint
    assert second.complete and not second.position


def test_log_reference_rejects_same_length_mutation_and_deletion(searchable_logs):
    from app.services.infrastructure.llm_request_log_service import (
        LLMRequestLogError,
    )

    service, session_id, path, budget = searchable_logs
    page = service.search_session_logs(session_id, "请求词😀", limit=1, position=None, budget=budget())
    reference = page.items[0].reference
    original = path.read_bytes()
    path.write_bytes(original.replace("请求词".encode(), "已改变".encode()))
    with pytest.raises(LLMRequestLogError, match="已变化"):
        service.locate(reference)
    with pytest.raises(LLMRequestLogError, match="已更新或裁剪"):
        service.search_session_logs(session_id, "请求词😀", limit=1, position=page.position, budget=budget())
    path.unlink()
    with pytest.raises(LLMRequestLogError, match="已删除"):
        service.locate(reference)


def test_log_scan_and_byte_budgets_are_explicit_and_resumable(searchable_logs):
    from app.services.infrastructure.llm_request_log_service import (
        MAX_SEARCH_BYTES,
        MAX_SEARCH_RECORDS,
    )

    service, session_id, _path, budget = searchable_logs
    scan = budget()
    scan.scanned_records = MAX_SEARCH_RECORDS
    page = service.search_session_logs(session_id, "请求词", limit=50, position=None, budget=scan)
    assert not page.complete and page.limited_reason == "scan_limit" and not page.items
    tiny = budget()
    tiny.scanned_bytes = MAX_SEARCH_BYTES - 1
    page = service.search_session_logs(session_id, "请求词", limit=50, position=None, budget=tiny)
    assert page.limited_reason == "byte_limit" and page.position and not page.items
    resumed = service.search_session_logs(session_id, "请求词", limit=50, position=page.position, budget=budget())
    assert resumed.complete and len(resumed.items) == 2
    expired = budget()
    expired.deadline = 0
    page = service.search_session_logs(session_id, "请求词", limit=50, position=None, budget=expired)
    assert not page.complete and page.limited_reason == "deadline"


def test_oversize_log_is_limited_without_unusable_cursor(searchable_logs):
    from app.services.infrastructure.llm_request_log_service import MAX_LOG_RECORD_BYTES

    service, session_id, path, budget = searchable_logs
    path.write_bytes(b"x" * (MAX_LOG_RECORD_BYTES + 1))
    page = service.search_session_logs(session_id, "x", limit=50, position=None, budget=budget())
    assert not page.complete and page.limited_reason == "item_size_limit" and not page.position
    assert not page.items


def test_search_rejects_cross_session_body_and_path_reference(searchable_logs):
    from pydantic import ValidationError

    from app.schemas.internal_v2.llm_request_log import LLMRequestLogReferenceDTO

    service, session_id, path, budget = searchable_logs
    data = json.loads(path.read_text())
    data["session_id"] = "ses_019b9a5623b97db291238e0644b0b4ae"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="归属不一致"):
        service.search_session_logs(session_id, "请求词", limit=50, position=None, budget=budget())
    with pytest.raises(ValidationError):
        LLMRequestLogReferenceDTO(session_id=session_id, file_name="../1000.json", content_version="a" * 64,
                                 start_codepoint=0, end_codepoint=1)


def test_directory_limit_never_exposes_a_cursor_that_repeats_the_same_unscannable_directory(searchable_logs, monkeypatch):
    import app.services.infrastructure.llm_request_log_service as log_module

    service, session_id, path, budget = searchable_logs
    monkeypatch.setattr(log_module, "MAX_DIRECTORY_FILES", 1)
    path.with_name("2000.json").write_bytes(path.read_bytes())
    page = service.search_session_logs(session_id, "请求词", limit=50, position=None, budget=budget())
    assert page.limited_reason == "scan_limit" and not page.complete and not page.position
    assert not page.items


def test_record_budget_resumes_after_the_exhausted_file_instead_of_reading_it_again(searchable_logs, monkeypatch):
    import app.services.infrastructure.llm_request_log_service as log_module

    service, session_id, path, budget = searchable_logs
    monkeypatch.setattr(log_module, "MAX_SEARCH_RECORDS", 1)
    path.with_name("2000.json").write_bytes(path.read_bytes())
    first = service.search_session_logs(session_id, "结束", limit=50, position=None, budget=budget())
    assert first.limited_reason == "scan_limit" and first.position and first.position.file_index == 1
    assert first.position.offset == 0
    reads = []
    read = service._read_bytes
    monkeypatch.setattr(service, "_read_bytes", lambda path, size: (reads.append(path.name), read(path, size))[1])
    second = service.search_session_logs(session_id, "结束", limit=50, position=first.position, budget=budget())
    assert second.complete and [hit.reference.file_name for hit in second.items] == ["2000.json"]
    assert reads == ["2000.json"]
