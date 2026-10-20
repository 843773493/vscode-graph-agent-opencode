from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from app.abstractions.session_changes import FileEditSnapshot
from app.core.exceptions import ForbiddenError
from app.core.path_utils import (
    get_session_changes_dir,
    get_session_creation_service,
    get_session_path_resolver,
    get_sessions_dir,
    initialize_directories,
    workspace_context,
)
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_changes_service import SessionChangesService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.session_changes_store import SessionChangesStore
from app.services.infrastructure.trace_event_store import TraceEventStore


def _uri(root: Path, path: str) -> str:
    return resource_display_uri(scope="workspace", scope_id=load_or_create_workspace_id(root), kind="files",
                                tail_segments=("root", *Path(path).parts))


@pytest.fixture
def session_service(tmp_path: Path):
    workspace_id = load_or_create_workspace_id(tmp_path)
    initialize_directories(workspace_id, tmp_path)
    with workspace_context(workspace_id, tmp_path):
        local_config_path = tmp_path / ".boxteam" / "workspace.jsonc"
        local_config_path.write_text(
            json.dumps(
                {
                    "llm": {
                        "providers": [
                            {
                                "id": "test-local",
                                "endpoint": "http://127.0.0.1:9/v1",
                                "model": "offline-test-model",
                                "api_key": "offline-test-key",
                                "custom_llm_provider": "openai",
                                "api_mode": {
                                    "protocol": "chat_completions",
                                    "model_info": {
                                        "supports_function_calling": True,
                                        "supports_reasoning": True,
                                    },
                                    "supports_reasoning": {"reasoning_content": True},
                                },
                            }
                        ]
                    },
                    "agents": {
                        "default": {
                            "model": {
                                "primary_provider": "test-local",
                                "fallback_providers": [],
                            }
                        }
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        # R17：workspace_id 与 resolver 同源（identity API）；catalog 模式下
        # register 校验 manifest workspace_id 与分配一致，不再硬编码假 UUID。
        yield SessionService(
            config_service=ConfigService(workspace_root=tmp_path),
            trace_event_store=TraceEventStore(sessions_dir=get_sessions_dir()),
            workspace_id=load_or_create_workspace_id(tmp_path),
            path_resolver=get_session_path_resolver(get_sessions_dir()),
            creation_service=get_session_creation_service(get_sessions_dir()),

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

    before = FileEditSnapshot(_uri(tmp_path, "src/hello.txt"), False, "", True, "hello\nworld\n")
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
    assert record.file_path == _uri(tmp_path, "src/hello.txt")
    assert record.kind == "create"

    changes_dir = get_session_changes_dir(session.session_id)
    index_file = changes_dir / "index.jsonl"
    assert index_file.exists()
    index_record = json.loads(index_file.read_text(encoding="utf-8").strip())
    assert index_record["file_path"] == _uri(tmp_path, "src/hello.txt")
    assert (changes_dir / "edits" / record.edit_id / "diff.patch").exists()
    metadata = json.loads((changes_dir / "edits" / record.edit_id / "metadata.json").read_text())
    assert metadata == index_record == asdict(record)
    assert not {"before_file", "after_file", "diff_file"} & metadata.keys()

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
    assert file_change.file_path == _uri(tmp_path, "src/hello.txt")
    assert "+hello" in file_change.diff_text
    assert file_change.after_preview == "hello\nworld\n"
    assert not {"before_file", "after_file", "diff_file"} & file_change.model_dump().keys()
    saved_summary = json.loads((changes_dir / "changesets/all/summary.json").read_text())
    assert saved_summary["files"][0] == file_change.model_dump()
    after, = (changes_dir / "changesets/all").glob("*/after.txt")
    diff, = (changes_dir / "changesets/all").glob("*/diff.patch")
    assert after.read_text() == "hello\nworld\n" and "+hello" in diff.read_text()


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
    before = FileEditSnapshot(_uri(tmp_path, "notes.md"), True, "before\n", True, "after\n")
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
        file_path=_uri(tmp_path, "notes.md"),
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
        file_path=_uri(tmp_path, "notes.md"),
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

    before_first_create = FileEditSnapshot(_uri(tmp_path, "src/first.txt"), False, "", True, "one\n")
    first_file.write_text("one\n", encoding="utf-8")
    await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_1",
        tool_call_id="call_write_first",
        execution_id="run_write_first",
        tool_name="write_file",
        before=before_first_create,
    )

    before_first_edit = FileEditSnapshot(_uri(tmp_path, "src/first.txt"), True, "one\n", True, "one\ntwo\n")
    first_file.write_text("one\ntwo\n", encoding="utf-8")
    await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_1",
        tool_call_id="call_edit_first",
        execution_id="run_edit_first",
        tool_name="edit_file",
        before=before_first_edit,
    )

    before_second_create = FileEditSnapshot(_uri(tmp_path, "src/second.txt"), False, "", True, "second\n")
    second_file.write_text("second\n", encoding="utf-8")
    await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_1",
        tool_call_id="call_write_second",
        execution_id="run_write_second",
        tool_name="write_file",
        before=before_second_create,
    )

    before_second_turn = FileEditSnapshot(_uri(tmp_path, "src/first.txt"), True, "one\ntwo\n", True, "one\ntwo\nthree\n")
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
        _uri(tmp_path, "src/first.txt"),
        _uri(tmp_path, "src/second.txt"),
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
        _uri(tmp_path, "src/first.txt"),
        _uri(tmp_path, "src/second.txt"),
    ]

    second_turn = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="turn:job_2",
    )
    assert second_turn.summary.files == 1
    assert second_turn.summary.additions == 1
    assert second_turn.summary.deletions == 0
    assert second_turn.files[0].file_path == _uri(tmp_path, "src/first.txt")
    assert "+three" in second_turn.files[0].diff_text


@pytest.mark.asyncio
async def test_counts_changed_lines_that_start_with_diff_prefixes(
    tmp_path: Path,
    session_service: SessionService,
) -> None:
    """正文以 ++/-- 开头时仍是变更行，不能被当成 diff 文件头丢弃。"""
    session = await session_service.create(SessionCreateRequest(title="prefix lines"))
    service = SessionChangesService(
        session_service=session_service,
        store=SessionChangesStore(workspace_root=tmp_path),
    )
    target = tmp_path / "src" / "counter.txt"
    target.parent.mkdir(parents=True)

    before = FileEditSnapshot(_uri(tmp_path, "src/counter.txt"), False, "", True, "++added\n--removed\n")
    target.write_text("++added\n--removed\n", encoding="utf-8")
    record = await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id="job_prefix",
        tool_call_id="call_prefix",
        execution_id="run_prefix",
        tool_name="write_file",
        before=before,
    )

    assert record is not None
    assert record.additions == 2
    assert record.deletions == 0

    detail = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="all",
    )
    assert detail.summary.files == 1
    assert detail.summary.additions == 2
    assert detail.summary.deletions == 0

@pytest.mark.asyncio
async def test_internal_file_edits_stay_in_all_changes_and_have_no_turn_changeset(
    tmp_path: Path,
    session_service: SessionService,
) -> None:
    session = await session_service.create(SessionCreateRequest(title="internal edit"))
    service = SessionChangesService(
        session_service=session_service,
        store=SessionChangesStore(workspace_root=tmp_path),
    )
    target = tmp_path / "internal.txt"
    before = FileEditSnapshot(_uri(tmp_path, "internal.txt"), False, "", True, "internal output\n")
    target.write_text("internal output\n", encoding="utf-8")

    record = await service.record_tool_file_edit(
        session_id=session.session_id,
        turn_id=None,
        tool_call_id="call_internal",
        execution_id="execution_internal",
        tool_name="write_file",
        before=before,
    )

    assert record is not None
    assert record.turn_id is None
    changesets = await service.list_changesets(session.session_id)
    assert [item.changeset_id for item in changesets.items] == ["all"]
    all_changes = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="all",
    )
    assert all_changes.summary.files == 1
    assert all_changes.files[0].turn_ids == []
    assert all_changes.files[0].execution_ids == ["execution_internal"]
    turn_changes = await service.get_changeset(
        session_id=session.session_id,
        changeset_id="turn:execution_internal",
    )
    assert turn_changes.summary.files == 0


@pytest.mark.asyncio
async def test_remote_observation_is_recorded_without_reading_source_decoy(tmp_path, session_service):
    session = await session_service.create(SessionCreateRequest(title="固定目标变更"))
    service = SessionChangesService(session_service=session_service, store=SessionChangesStore(workspace_root=tmp_path))
    decoy = tmp_path / "same.txt"
    decoy.write_text("SOURCE DECOY\n", encoding="utf-8")
    remote_uri = resource_display_uri(scope="workspace", scope_id="0191f2ab-1234-7abc-8def-0123456789ab",
        kind="files", tail_segments=("root", "same.txt"), gateway_authority="gateway_0123456789abcdef0123456789abcdef")
    record = await service.record_tool_file_edit(session_id=session.session_id, turn_id="turn_remote", tool_call_id="call_remote",
        execution_id="run_remote", tool_name="edit_file",
        before=FileEditSnapshot(remote_uri, True, "TARGET BEFORE\n", True, "TARGET AFTER\n"))
    assert record is not None and record.file_path == remote_uri
    changed, = (await service.get_changeset(session_id=session.session_id, changeset_id="all")).files
    assert changed.file_path == remote_uri
    assert "-TARGET BEFORE" in changed.diff_text and "+TARGET AFTER" in changed.diff_text
    assert "SOURCE DECOY" not in changed.diff_text
    assert decoy.read_text() == "SOURCE DECOY\n"
    reviewed = await service.set_file_reviewed(session_id=session.session_id, file_path=remote_uri, reviewed=True)
    assert reviewed.file_path == remote_uri and reviewed.reviewed
    with pytest.raises(ValueError):
        await service.set_file_reviewed(session_id=session.session_id, file_path="/same.txt", reviewed=True)
    reopened = SessionChangesService(session_service=session_service, store=SessionChangesStore(workspace_root=tmp_path))
    changed, = (await reopened.get_changeset(session_id=session.session_id, changeset_id="all")).files
    assert changed.file_path == remote_uri and changed.reviewed
    assert changed.before_preview == "TARGET BEFORE\n" and changed.after_preview == "TARGET AFTER\n"
    assert decoy.read_text() == "SOURCE DECOY\n"


@pytest.fixture
async def recorded_file_change(tmp_path, session_service):
    session = await session_service.create(SessionCreateRequest(title="冷读变更"))
    service = SessionChangesService(session_service=session_service, store=SessionChangesStore(workspace_root=tmp_path))
    record = await service.record_tool_file_edit(session_id=session.session_id, turn_id="turn_cold",
        tool_call_id="call_cold", execution_id="execution_cold", tool_name="edit_file",
        before=FileEditSnapshot(_uri(tmp_path, "src/cold.txt"), True, "before\n", True, "after\n"))
    assert record is not None
    return session, record, get_session_changes_dir(session.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["/private/file.txt", r"C:\private\file.txt", "src/file.txt", "",
    "boxteam://workspace/target/resources/files/root/../file.txt",
    "boxteam://workspace/target/resources/config/workspace", "boxteam://workspace/target/resources/files/root"])
async def test_invalid_stored_change_reference_fails_cold_read_and_write_without_side_effects(
    tmp_path, session_service, recorded_file_change, reference,
):
    session, record, changes = recorded_file_change
    index = changes / "index.jsonl"
    invalid = {**asdict(record), "file_path": reference}
    index.write_text(json.dumps(invalid) + "\n")
    originals = {path: path.read_bytes() for path in changes.rglob("*") if path.is_file()}
    store = SessionChangesStore(workspace_root=tmp_path)
    reopened = SessionChangesService(session_service=session_service, store=store)
    for operation in (reopened.list_changesets(session.session_id),
                      reopened.get_changeset(session_id=session.session_id, changeset_id="all")):
        with pytest.raises(ValueError, match="文件变更") as failure:
            await operation
        if reference:
            assert reference not in str(failure.value)
    with pytest.raises(ValueError, match="文件变更"):
        store.save_edit(record=replace(record, file_path=reference), before_content="before\n",
                        after_content="after\n", diff_text="invalid")
    assert {path: path.read_bytes() for path in changes.rglob("*") if path.is_file()} == originals


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewed", [
    {"/private/file.txt": True}, {r"C:\private\file.txt": True}, {"src/file.txt": True},
    {"boxteam://workspace/target/resources/files/root/file.txt": "true"},
    {"boxteam://workspace/target/resources/files/root/file.txt": 1},
    {"boxteam://workspace/target/resources/files/root/%e4%b8%ad.txt": True,
     "boxteam://workspace/target/resources/files/root/%E4%B8%AD.txt": False},
])
async def test_invalid_reviewed_reference_fails_read_and_update_without_rewriting(
    tmp_path, session_service, recorded_file_change, reviewed,
):
    session, record, changes = recorded_file_change
    path = changes / "reviewed.json"
    original = json.dumps(reviewed).encode()
    path.write_bytes(original)
    store = SessionChangesStore(workspace_root=tmp_path)
    reopened = SessionChangesService(session_service=session_service, store=store)
    with pytest.raises((ValueError, TypeError)):
        await reopened.get_changeset(session_id=session.session_id, changeset_id="all")
    with pytest.raises((ValueError, TypeError)):
        await reopened.set_file_reviewed(session_id=session.session_id, file_path=record.file_path, reviewed=True)
    with pytest.raises((ValueError, TypeError)):
        store.save_reviewed_map(session.session_id, reviewed)
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("obsolete_field", ["before_file", "after_file", "diff_file"])
async def test_legacy_snapshot_locations_are_not_accepted_as_cold_read_targets(
    tmp_path, session_service, recorded_file_change, obsolete_field,
):
    session, record, changes = recorded_file_change
    index = changes / "index.jsonl"
    original = json.dumps({**asdict(record), obsolete_field: "/private/decoy.txt"}).encode() + b"\n"
    index.write_bytes(original)
    reopened = SessionChangesService(session_service=session_service, store=SessionChangesStore(workspace_root=tmp_path))
    with pytest.raises(TypeError, match=obsolete_field):
        await reopened.list_changesets(session.session_id)
    assert index.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [
    "boxteam://workspace/absent-target/resources/files/root/%e4%b8%ad.txt",
    "boxteam://gateway_0123456789abcdef0123456789abcdef/gateway/gateway_01900000000070008000000000000001/resources/files/01900000000070008000000000000001/%e4%b8%ad.txt",
])
async def test_dangling_workspace_and_gateway_change_uris_preserve_content_after_cold_read(
    tmp_path, session_service, reference,
):
    session = await session_service.create(SessionCreateRequest(title="悬空引用"))
    service = SessionChangesService(session_service=session_service, store=SessionChangesStore(workspace_root=tmp_path))
    record = await service.record_tool_file_edit(session_id=session.session_id, turn_id=None,
        tool_call_id="call_dangling", execution_id="execution_dangling", tool_name="edit_file",
        before=FileEditSnapshot(reference, True, "before /etc/hosts\n", False, ""))
    assert record is not None and record.kind == "delete"
    await service.set_file_reviewed(session_id=session.session_id, file_path=reference, reviewed=True)
    index = get_session_changes_dir(session.session_id) / "index.jsonl"
    original = index.read_bytes()
    reopened = SessionChangesService(session_service=session_service, store=SessionChangesStore(workspace_root=tmp_path))
    changed, = (await reopened.get_changeset(session_id=session.session_id, changeset_id="all")).files
    assert changed.file_path == record.file_path == reference.replace("%e4%b8%ad", "%E4%B8%AD")
    assert changed.reviewed and changed.kind == "delete"
    assert changed.before_preview == "before /etc/hosts\n" and changed.after_preview is None
    assert "-before /etc/hosts" in changed.diff_text
    assert index.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["edit_directory", "before_snapshot"])
async def test_derived_snapshot_lookup_keeps_containment(tmp_path, session_service, recorded_file_change, alias):
    session, record, changes = recorded_file_change
    edit = changes / "edits" / record.edit_id
    if alias == "edit_directory":
        for path in edit.iterdir():
            path.unlink()
        edit.rmdir()
        external = tmp_path / "external-snapshots"
        external.mkdir()
        (external / "before.txt").write_text("SOURCE DECOY\n")
        (external / "after.txt").write_text("SOURCE DECOY AFTER\n")
        edit.symlink_to(external, target_is_directory=True)
    else:
        external = tmp_path / "external-snapshot.txt"
        external.write_text("SOURCE DECOY\n")
        (edit / "before.txt").unlink()
        (edit / "before.txt").symlink_to(external)
    reopened = SessionChangesService(session_service=session_service, store=SessionChangesStore(workspace_root=tmp_path))
    with pytest.raises(ForbiddenError):
        await reopened.get_changeset(session_id=session.session_id, changeset_id="all")
    assert not (changes / "changesets/all/summary.json").exists()
