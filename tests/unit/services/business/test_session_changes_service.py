from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.path_utils import (
    get_session_changes_dir,
    get_sessions_dir,
    initialize_directories,
)
from app.schemas.public_v2.session import SessionCreateRequest
from app.services.business.session_changes_service import SessionChangesService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.infrastructure.session_changes_store import SessionChangesStore


@pytest.fixture
def session_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionService:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    initialize_directories()
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=get_sessions_dir()),
    )


@pytest.mark.asyncio
async def test_records_readable_changesets_for_file_tools(
    tmp_path: Path,
    session_service: SessionService,
) -> None:
    session = await session_service.create(SessionCreateRequest(title="changes"))
    service = SessionChangesService(
        session_service=session_service,
        store=SessionChangesStore(workspace_root=tmp_path),
    )
    target = tmp_path / "src" / "hello.txt"
    target.parent.mkdir(parents=True)

    before = service.capture_before("/src/hello.txt")
    target.write_text("hello\nworld\n", encoding="utf-8")
    record = await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_1",
        tool_call_id="call_1",
        execution_id="run_1",
        tool_name="write_file",
        before=before,
    )

    assert record is not None
    assert record.file_path == "/src/hello.txt"
    assert record.kind == "create"

    changes_dir = get_session_changes_dir(session.session_id)
    index_file = changes_dir / "index.jsonl"
    assert index_file.exists()
    index_record = json.loads(index_file.read_text(encoding="utf-8").strip())
    assert index_record["file_path"] == "/src/hello.txt"
    assert (changes_dir / record.diff_file).exists()

    list_result = await service.list_changesets(session.session_id)
    assert [item.changeset_id for item in list_result.items] == ["all", "turn:job_1"]
    assert list_result.items[0].summary.files == 1

    detail = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="all",
    )
    assert detail.summary.files == 1
    assert detail.summary.additions == 2
    assert detail.summary.deletions == 0
    assert len(detail.files) == 1
    file_change = detail.files[0]
    assert file_change.file_path == "/src/hello.txt"
    assert file_change.after_file is not None
    assert file_change.diff_file.endswith("diff.patch")
    assert "+hello" in file_change.diff_text
    assert (changes_dir / file_change.after_file).exists()
    assert (changes_dir / file_change.diff_file).exists()


@pytest.mark.asyncio
async def test_marks_file_changes_reviewed(
    tmp_path: Path,
    session_service: SessionService,
) -> None:
    session = await session_service.create(SessionCreateRequest(title="review"))
    service = SessionChangesService(
        session_service=session_service,
        store=SessionChangesStore(workspace_root=tmp_path),
    )
    target = tmp_path / "notes.md"
    target.write_text("before\n", encoding="utf-8")
    before = service.capture_before("/notes.md")
    target.write_text("after\n", encoding="utf-8")
    await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_2",
        tool_call_id="call_2",
        execution_id="run_2",
        tool_name="edit_file",
        before=before,
    )

    result = await service.set_file_reviewed(
        session_id=session.session_id,
        file_path="/notes.md",
        reviewed=True,
    )

    assert result.reviewed is True
    detail = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="turn:job_2",
    )
    assert detail.files[0].reviewed is True

    await service.set_file_reviewed(
        session_id=session.session_id,
        file_path="/notes.md",
        reviewed=False,
    )
    refreshed = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="all",
    )
    assert refreshed.files[0].reviewed is False


@pytest.mark.asyncio
async def test_aggregates_multiple_file_edit_tool_calls_across_turns(
    tmp_path: Path,
    session_service: SessionService,
) -> None:
    session = await session_service.create(SessionCreateRequest(title="multi edit"))
    service = SessionChangesService(
        session_service=session_service,
        store=SessionChangesStore(workspace_root=tmp_path),
    )
    first_file = tmp_path / "src" / "first.txt"
    second_file = tmp_path / "src" / "second.txt"
    first_file.parent.mkdir(parents=True)

    before_first_create = service.capture_before("/src/first.txt")
    first_file.write_text("one\n", encoding="utf-8")
    await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_1",
        tool_call_id="call_write_first",
        execution_id="run_write_first",
        tool_name="write_file",
        before=before_first_create,
    )

    before_first_edit = service.capture_before("/src/first.txt")
    first_file.write_text("one\ntwo\n", encoding="utf-8")
    await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_1",
        tool_call_id="call_edit_first",
        execution_id="run_edit_first",
        tool_name="edit_file",
        before=before_first_edit,
    )

    before_second_create = service.capture_before("/src/second.txt")
    second_file.write_text("second\n", encoding="utf-8")
    await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_1",
        tool_call_id="call_write_second",
        execution_id="run_write_second",
        tool_name="write_file",
        before=before_second_create,
    )

    before_second_turn = service.capture_before("/src/first.txt")
    first_file.write_text("one\ntwo\nthree\n", encoding="utf-8")
    await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_2",
        tool_call_id="call_edit_first_again",
        execution_id="run_edit_first_again",
        tool_name="edit_file",
        before=before_second_turn,
    )

    changesets = await service.list_changesets(session.session_id)

    assert [item.changeset_id for item in changesets.items] == [
        "all",
        "turn:job_2",
        "turn:job_1",
    ]

    all_changes = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="all",
    )
    assert all_changes.summary.files == 2
    assert all_changes.summary.additions == 4
    assert all_changes.summary.deletions == 0
    assert [file.file_path for file in all_changes.files] == [
        "/src/first.txt",
        "/src/second.txt",
    ]
    first_change = all_changes.files[0]
    assert first_change.tool_call_ids == [
        "call_write_first",
        "call_edit_first",
        "call_edit_first_again",
    ]
    assert first_change.execution_ids == [
        "run_write_first",
        "run_edit_first",
        "run_edit_first_again",
    ]
    assert first_change.turn_ids == ["job_1", "job_2"]
    assert "+three" in first_change.diff_text

    first_turn = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="turn:job_1",
    )
    assert first_turn.summary.files == 2
    assert first_turn.summary.additions == 3
    assert [file.file_path for file in first_turn.files] == [
        "/src/first.txt",
        "/src/second.txt",
    ]

    second_turn = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="turn:job_2",
    )
    assert second_turn.summary.files == 1
    assert second_turn.summary.additions == 1
    assert second_turn.summary.deletions == 0
    assert second_turn.files[0].file_path == "/src/first.txt"
    assert "+three" in second_turn.files[0].diff_text
