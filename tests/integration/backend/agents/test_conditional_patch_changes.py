"""实际部分发布经事件流进入原 Session Changes owner，失败不抹去变更。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from app.agents.tools.apply_patch import apply_patch_text
from app.core.hashing import sha256_hex
from app.core.path_utils import (
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
from app.services.infrastructure.workspace_files import text_mutation, text_publication
from app.services.orchestration.event_stream.processor import process_agent_event_stream


@pytest.fixture
def patch_changes_case(integration_workspace_root_path):
    root = Path(integration_workspace_root_path)
    workspace_id = load_or_create_workspace_id(root)
    initialize_directories(workspace_id, root)
    with workspace_context(workspace_id, root):
        sessions = SessionService(config_service=ConfigService(workspace_root=root),
            trace_event_store=TraceEventStore(sessions_dir=get_sessions_dir()), workspace_id=workspace_id,
            path_resolver=get_session_path_resolver(get_sessions_dir()),
            creation_service=get_session_creation_service(get_sessions_dir()))
        changes = SessionChangesService(session_service=sessions, store=SessionChangesStore(workspace_root=root))
        yield root, sessions, changes


async def test_partial_patch_keeps_published_file_in_changeset(patch_changes_case, monkeypatch):
    root, sessions, changes = patch_changes_case
    session = await sessions.create(SessionCreateRequest(title="部分补丁变更"))
    original = text_publication.rename_entry_exclusive

    def fail_second(source, target):
        if target.name == "patch-second.txt":
            raise PermissionError("second publication refused")
        original(source, target)

    monkeypatch.setattr(text_publication, "rename_entry_exclusive", fail_second)
    patch = "*** Begin Patch\n*** Add File: src/patch-first.txt\n+first\n*** Add File: src/patch-second.txt\n+second\n*** Add File: src/patch-third.txt\n+third\n*** End Patch"
    versions = {f"src/patch-{name}.txt": "absent" for name in ("first", "second", "third")}
    receipt = None

    class PatchAgent:
        async def astream_events(self, _input, **kwargs):
            nonlocal receipt
            metadata = kwargs["config"]["metadata"]
            yield {"event": "on_tool_start", "run_id": "run_partial_patch", "name": "apply_patch",
                   "data": {"input": {"input": patch, "explanation": "部分发布", "expected_versions": versions}}, "metadata": metadata}
            receipt = apply_patch_text(patch, explanation="部分发布", expected_versions=versions, workspace_root=root)
            yield {"event": "on_tool_end", "run_id": "run_partial_patch", "name": "apply_patch",
                   "data": {"output": ToolMessage(content=json.dumps(receipt), name="apply_patch", tool_call_id="call_partial_patch")}, "metadata": metadata}

    events = []
    async def publish(event, payload):
        events.append((event, payload))

    result = await process_agent_event_stream(agent=PatchAgent(), input_payload={"messages": []}, config={},
        session_id=session.session_id, job_id="job_partial_patch", turn_id="job_partial_patch", agent_id="default",
        custom_tool_skill_sources={}, publish=publish, session_changes_service=changes, workspace_root=root)
    assert receipt["effect"] == "partial" and receipt["status"] == "error"
    assert [item["state"] for item in receipt["files"]] == ["published", "not_published", "not_attempted"]
    assert (root / "src/patch-first.txt").read_bytes() == b"first"
    assert not (root / "src/patch-second.txt").exists() and not (root / "src/patch-third.txt").exists()
    assert result.successful_tool_calls == ()
    end = next(payload for _, payload in events if payload.get("tool_name") == "apply_patch" and "failed" in payload)
    assert end["failed"] and end["status"] == "error"
    file_edit, = end["file_edits"]
    uri = resource_display_uri(scope="workspace", scope_id=load_or_create_workspace_id(root), kind="files", tail_segments=("root", "src", "patch-first.txt"))
    assert file_edit["file_path"] == uri and file_edit["kind"] == "create"
    changeset = await changes.get_changeset(session_id=session.session_id, changeset_id="all")
    changed, = changeset.files
    assert changed.file_path == uri and changed.kind == "create"
    assert "+first" in changed.diff_text and changed.after_preview == "first"


async def test_changeset_uses_owner_postimage_when_later_writer_precedes_tool_event(patch_changes_case):
    root, sessions, changes = patch_changes_case
    session = await sessions.create(SessionCreateRequest(title="发布后件归属"))
    receipt = apply_patch_text("*** Begin Patch\n*** Add File: src/observed.txt\n+first\n*** End Patch",
        explanation="第一个调用", expected_versions={"src/observed.txt": "absent"}, workspace_root=root)
    later = apply_patch_text("*** Begin Patch\n*** Update File: src/observed.txt\n@@\n-first\n+later\n*** End Patch",
        explanation="稍后的writer", expected_versions={"src/observed.txt": "sha256:" + sha256_hex(b"first")}, workspace_root=root)
    assert receipt["status"] == later["status"] == "success"

    class DelayedToolEvent:
        async def astream_events(self, _input, **kwargs):
            metadata = kwargs["config"]["metadata"]
            yield {"event": "on_tool_start", "run_id": "run_first", "name": "apply_patch",
                "data": {"input": {"explanation": "第一个调用"}}, "metadata": metadata}
            yield {"event": "on_tool_end", "run_id": "run_first", "name": "apply_patch",
                "data": {"output": ToolMessage(content=json.dumps(receipt), name="apply_patch", tool_call_id="call_first")},
                "metadata": metadata}

    async def publish(_event, _payload):
        return None

    await process_agent_event_stream(agent=DelayedToolEvent(), input_payload={"messages": []}, config={},
        session_id=session.session_id, job_id="job_first", turn_id="job_first", agent_id="default",
        custom_tool_skill_sources={}, publish=publish, session_changes_service=changes, workspace_root=root)
    changed, = (await changes.get_changeset(session_id=session.session_id, changeset_id="all")).files
    assert changed.file_path == receipt["files"][0]["path"]
    assert changed.after_preview == "first" and "+first" in changed.diff_text and "+later" not in changed.diff_text
    assert (root / "src/observed.txt").read_bytes() == b"later"


async def test_unknown_non_utf8_postimage_is_visible_without_fabricated_changeset(patch_changes_case, monkeypatch):
    root, sessions, changes = patch_changes_case
    session = await sessions.create(SessionCreateRequest(title="未知非文本后件"))
    original = text_mutation.publish_file_content

    def external_writer(path, content, **kwargs):
        result = original(path, content, **kwargs)
        path.write_bytes(b"\xffEXTERNAL")
        return result

    monkeypatch.setattr(text_mutation, "publish_file_content", external_writer)
    receipt = apply_patch_text("*** Begin Patch\n*** Add File: unknown.txt\n+intended\n*** End Patch",
        explanation="观察非文本后件", expected_versions={"unknown.txt": "absent"}, workspace_root=root)
    assert receipt["effect"] == "unknown" and receipt["status"] == "error" and receipt["retryable"] is False
    assert receipt["files"][0]["state"] == "unknown"
    assert receipt["files"][0]["content_observation_error"] == "invalid_encoding"

    class UnknownToolEvent:
        async def astream_events(self, _input, **kwargs):
            metadata = kwargs["config"]["metadata"]
            yield {"event": "on_tool_start", "run_id": "run_unknown", "name": "apply_patch",
                "data": {"input": {"explanation": "未知效果"}}, "metadata": metadata}
            yield {"event": "on_tool_end", "run_id": "run_unknown", "name": "apply_patch",
                "data": {"output": ToolMessage(content=json.dumps(receipt), name="apply_patch", tool_call_id="call_unknown")},
                "metadata": metadata}

    events = []
    async def publish(event, payload):
        events.append((event, payload))

    result = await process_agent_event_stream(agent=UnknownToolEvent(), input_payload={"messages": []}, config={},
        session_id=session.session_id, job_id="job_unknown", turn_id="job_unknown", agent_id="default",
        custom_tool_skill_sources={}, publish=publish, session_changes_service=changes, workspace_root=root)
    assert result.successful_tool_calls == ()
    end = next(payload for _, payload in events if "failed" in payload)
    assert end["failed"] and end["status"] == "error" and "unknown" in end["result"]
    assert (await changes.get_changeset(session_id=session.session_id, changeset_id="all")).summary.files == 0
    assert (root / "unknown.txt").read_bytes() == b"\xffEXTERNAL"
