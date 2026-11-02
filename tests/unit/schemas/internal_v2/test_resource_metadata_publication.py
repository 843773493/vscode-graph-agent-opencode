"""资源展示元数据允许原生路径；真实定位由具体字段和 owner 校验。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticSerializationError

from app.schemas.internal_v2.session_resource import (
    SessionResourceDTO,
    SessionResourceListDTO,
)
from app.schemas.internal_v2.workspace_resource import (
    TerminalResourceCreateRequest,
    WorkspaceResourceDTO,
    WorkspaceResourceListDTO,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A, WORKSPACE_ID

Resource = WorkspaceResourceDTO | SessionResourceDTO
FILE_URI = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root/entry.txt"
DISPLAY_METADATA = [
    {"nested": {"paths": ["/private/resource-secret"]}},
    {"checkpoint_path": r"C:\private\resource-secret"},
    {"storage_directory": r"\\target\private\resource-secret"},
    {"path": "file:///private/resource-secret"},
    {"cwd_relative": "/private/resource-secret"},
    {"cwd_relative": "file:///private/resource-secret"},
    {"cwd_relative": "C:resource-secret"},
    {"cwd_relative": FILE_URI},
    {"cwd_relative": ["relative"]},
    {"paths": ["/private/resource-secret"]},
    {"log_path": "/tmp/build.log"},
]


@pytest.fixture(params=["workspace", "session"])
def resource(request: pytest.FixtureRequest) -> Resource:
    stamp = "2026-11-01T00:00:00Z"
    if request.param == "session":
        return SessionResourceDTO(resource_id="terminal-test", session_id=SESSION_A, kind="terminal",
            name="终端", status="running", created_at=datetime.fromisoformat(stamp), updated_at=datetime.fromisoformat(stamp))
    return WorkspaceResourceDTO.model_validate({"resource_id": "terminal-test", "workspace_id": WORKSPACE_ID,
        "kind": "terminal", "retention_scope": "thread", "source_thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
        "thread_associations": [], "owner_revision": 1, "status": "running", "reconciliation_status": "verified",
        "unattached_since": None, "reclaim_at": None, "available_actions": [], "metadata": {},
        "created_at": stamp, "updated_at": stamp, "error": None})


@pytest.mark.parametrize("metadata", DISPLAY_METADATA)
def test_resource_metadata_preserves_native_display_paths_at_construction(resource: Resource, metadata: dict[str, object]) -> None:
    candidate = type(resource).model_validate({**resource.model_dump(), "metadata": metadata})
    assert candidate.model_dump()["metadata"] == metadata


@pytest.mark.parametrize("metadata", DISPLAY_METADATA)
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_nested_display_paths_survive_resource_publication(
    resource: Resource, metadata: dict[str, object], mode: Literal["python", "json", "json-text"],
) -> None:
    original = resource.model_dump_json()
    candidate = resource.model_copy(deep=True)
    candidate.metadata.update(metadata)
    envelope: BaseModel = (
        WorkspaceResourceListDTO(workspace_id=WORKSPACE_ID, items=[candidate])
        if isinstance(candidate, WorkspaceResourceDTO)
        else SessionResourceListDTO(session_id=SESSION_A, items=[candidate])
    )
    if mode == "json-text":
        payload = type(envelope).model_validate_json(envelope.model_dump_json()).model_dump()
    else:
        payload = envelope.model_dump(mode=mode)
    assert payload["items"][0]["metadata"] == metadata
    assert resource.model_dump_json() == original


def test_resource_metadata_preserves_relative_paths_and_user_content(resource: Resource) -> None:
    metadata: dict[str, object] = {
        "cwd_relative": "../outside/目录",
        "path": FILE_URI,
        "checkpoint_path": "nested/C:entry.json",
        "relative_path": "file:entry.txt",
        "command": ["/usr/bin/python", r"C:\private\script.py"],
        "last_input": "cat /etc/hosts",
        "content": {"text": "/private/user-text"},
        "changed_paths": ["/agent/provider"],
        "applied_paths": ["/runtime/agent"],
        "deferred_paths": ["/runtime/model"],
    }
    candidate = type(resource).model_validate({**resource.model_dump(), "metadata": metadata})
    assert candidate.model_dump(mode="json")["metadata"] == metadata


@pytest.mark.parametrize("metadata", ["/private/resource-secret", ["/private/resource-secret"], None])
def test_copy_cannot_replace_metadata_with_non_object(resource: Resource, metadata: object) -> None:
    candidate = resource.model_copy(update={"metadata": metadata})
    with pytest.raises(PydanticSerializationError, match="公开资源元数据必须是对象"):
        candidate.model_dump()


@pytest.mark.parametrize("url", ["file:///private/user-opened-page.html", "file:///C:/private/user-opened-page.html"])
def test_browser_page_url_exception_is_preserved(resource: Resource, url: str) -> None:
    candidate = type(resource).model_validate({**resource.model_dump(), "kind": "browser", "metadata": {"url": url}})
    assert candidate.model_dump(mode="json")["metadata"] == {"url": url}


@pytest.mark.parametrize("directory", ["../outside", "/outside", r"C:\outside", FILE_URI])
def test_terminal_creation_preserves_native_and_uri_inputs(directory: str) -> None:
    request = TerminalResourceCreateRequest(session_id=SESSION_A, cwd_relative=directory)
    assert request.model_dump(mode="json")["cwd_relative"] == directory
