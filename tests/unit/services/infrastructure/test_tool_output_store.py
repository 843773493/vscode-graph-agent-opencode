"""工具结果 owner 失败时保留 URI、错误类别和原件。"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from app.services.infrastructure.tool_output_store import (
    ToolOutputStore,
    extract_tool_output_reference,
)


@dataclass(frozen=True)
class StoredOutput:
    store: ToolOutputStore
    session_id: str
    directory: Path
    path: Path
    uri: str
    content: str

    def bound(self, content: str) -> ToolMessage:
        return self.store.bound(
            session_id=self.session_id, tool_name="large", tool_call_id="call-large",
            message=ToolMessage(content=content, tool_call_id="call-large"),
        )


@pytest.fixture
def stored_output(tmp_path: Path, session_bundle_factory: Callable[[Path, str], Path]) -> StoredOutput:
    session_id = "ses_019bd0aa8bb2784aaadbe51563ee17f0"
    directory = session_bundle_factory(tmp_path / ".boxteam/sessions", session_id)
    store = ToolOutputStore(workspace_root=tmp_path, max_lines=8, max_bytes=1024)
    content = "原始输出\r\n" * 1000
    result = store.bound(
        session_id=session_id, tool_name="large", tool_call_id="call-large",
        message=ToolMessage(content=content, tool_call_id="call-large"),
    )
    reference = extract_tool_output_reference(result)
    assert reference is not None
    uri = str(reference["path"])
    return StoredOutput(store, session_id, directory, directory / "tool-results" / uri.rsplit("/", 1)[1], uri, content)


@pytest.mark.parametrize("concurrent", [False, True])
def test_conflicting_result_reports_uri_and_keeps_exact_winner(
    stored_output: StoredOutput, concurrent: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = stored_output.path.read_bytes()
    if concurrent:
        stored_output.path.unlink()

        def publish_winner(_source: Path, target: Path) -> None:
            target.write_bytes(original)
            raise FileExistsError(errno.EEXIST, "exists", str(target))

        monkeypatch.setattr(os, "link", publish_winner)
    with pytest.raises(RuntimeError) as caught:
        stored_output.bound("另一份输出\r\n" * 1000)
    assert stored_output.uri in str(caught.value)
    assert str(stored_output.directory) not in str(caught.value)
    assert stored_output.path.read_bytes() == original
    assert not list(stored_output.path.parent.glob(".*.tmp"))
    assert stored_output.store.read(stored_output.session_id, stored_output.uri, len(original)).data == original


@pytest.mark.parametrize("operation", ["read", "write"])
def test_io_failure_keeps_error_type_errno_and_cause_without_storage_location(
    stored_output: StoredOutput, operation: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = stored_output.path.read_bytes()
    native_open = Path.open

    def denied(candidate: Path, *args, **kwargs):
        if candidate == stored_output.path:
            raise PermissionError(errno.EACCES, "denied", str(candidate))
        return native_open(candidate, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "open", denied)
        with pytest.raises(PermissionError) as caught:
            if operation == "read":
                stored_output.store.read(stored_output.session_id, stored_output.uri, len(original))
            else:
                stored_output.bound(stored_output.content)
    assert caught.value.errno == errno.EACCES
    assert isinstance(caught.value.__cause__, PermissionError)
    assert operation in str(caught.value) and stored_output.uri in str(caught.value)
    assert str(stored_output.directory) not in str(caught.value)
    assert stored_output.path.read_bytes() == original


def test_missing_output_reports_the_same_uri_without_recreating_data(stored_output: StoredOutput) -> None:
    stored_output.path.unlink()
    with pytest.raises(FileNotFoundError) as caught:
        stored_output.store.read(stored_output.session_id, stored_output.uri, 1024)
    assert caught.value.errno == errno.ENOENT
    assert stored_output.uri in str(caught.value)
    assert str(stored_output.directory) not in str(caught.value)
    assert not stored_output.path.exists()
