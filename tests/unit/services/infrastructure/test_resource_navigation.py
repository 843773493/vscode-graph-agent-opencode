from __future__ import annotations

import base64
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.resource_uri import resource_display_uri
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.resource_platform.sources import resource_navigation
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

WORKSPACE_ID = "019d1b4a-1111-7000-8000-000000000001"
GATEWAY_ID = "gateway_navigation_test"


@pytest.fixture
def navigation_case(tmp_path: Path) -> tuple[ResourceNavigation, Path, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    owner = ResourceNavigation(workspace_root=workspace, workspace_id=WORKSPACE_ID,
        project_root=tmp_path, file_paths=WorkspaceFilePaths(
            workspace=workspace, workspace_id=WORKSPACE_ID, bindings=None,
        ))
    uri = resource_display_uri(scope="workspace", scope_id=WORKSPACE_ID, kind="files", tail_segments=("root",))
    return owner, workspace, uri


@pytest.mark.asyncio
async def test_pages_return_reopenable_uris_without_creating_resource_state(navigation_case) -> None:
    owner, workspace, uri = navigation_case
    names = ["a.txt", "b %.txt", "中文.v2.txt", "%2e%2e.txt", "z.txt"]
    for name in names:
        (workspace / name).write_text(name, encoding="utf-8")
    cursor, seen = None, []
    while True:
        result = await owner.navigate(ResourceNavigationRequest(uri=uri, operation="list", limit=2, cursor=cursor), local_gateway_id=GATEWAY_ID)
        assert result.view == "live"
        assert len(result.entries) <= 2
        for entry in result.entries:
            assert entry.uri not in seen
            seen.append(entry.uri)
            reopened = await owner.navigate(ResourceNavigationRequest(uri=entry.uri, operation="read"), local_gateway_id=GATEWAY_ID)
            assert reopened.content in names
        cursor = result.next_cursor
        if not result.truncated:
            assert cursor is None and result.truncation_reason is None
            break
        assert cursor and result.truncation_reason == "entry_limit"
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        assert str(workspace) not in decoded and "uri" not in decoded
    assert len(seen) == len(names)
    assert not (workspace / ".boxteam").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"uri_suffix": "/sub"}, {"operation": "search", "query": "a"},
    {"limit": 3}, {"scan_limit": 30}, {"max_bytes": 8},
    {"max_response_bytes": 2048}, {"timeout_ms": 500}, {"gateway_id": "gateway_other"},
])
async def test_cursor_rejects_another_target_query_or_limits(navigation_case, changes) -> None:
    owner, workspace, uri = navigation_case
    (workspace / "sub").mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_text(name)
    arguments = {"uri": uri, "operation": "list", "limit": 1}
    page = await owner.navigate(ResourceNavigationRequest(**arguments), local_gateway_id=GATEWAY_ID)
    changed = dict(changes)
    gateway_id = changed.pop("gateway_id", GATEWAY_ID)
    arguments["uri"] += changed.pop("uri_suffix", "")
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(ResourceNavigationRequest(**{**arguments, **changed, "cursor": page.next_cursor}), local_gateway_id=gateway_id)
    assert (failure.value.code, failure.value.reason) == ("execution_failed", "invalid_cursor")


@pytest.mark.asyncio
async def test_cursor_rejects_changed_file_in_consumed_prefix(navigation_case) -> None:
    owner, workspace, uri = navigation_case
    (workspace / "a.txt").write_text("first")
    (workspace / "b.txt").write_text("second")
    query = ResourceNavigationRequest(uri=uri, operation="list", limit=1)
    first = await owner.navigate(query, local_gateway_id=GATEWAY_ID)
    # 文件修改不会改变父目录 mtime；必须核对实际已消费前缀，不能只看目录时间。
    (workspace / "a.txt").write_text("first changed")
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(query.model_copy(update={"cursor": first.next_cursor}), local_gateway_id=GATEWAY_ID)
    assert failure.value.reason == "stale_cursor"


@pytest.mark.asyncio
async def test_recursive_search_pages_consume_nonmatching_entries_once(navigation_case) -> None:
    owner, workspace, uri = navigation_case
    for directory in ("a", "b", "b/deep"):
        (workspace / directory).mkdir()
    names = ("a/needle-1.txt", "b/needle-2.txt", "b/deep/needle-3.txt")
    for name in (*names, "a/other.txt", "b/deep/unmatched.txt"):
        (workspace / name).write_text(name)
    query = ResourceNavigationRequest(uri=uri, operation="search", query="needle", limit=1)
    cursor, seen = None, []
    for _ in range(4):
        page = await owner.navigate(query.model_copy(update={"cursor": cursor}), local_gateway_id=GATEWAY_ID)
        seen.extend(node.uri for node in page.entries)
        if not page.truncated:
            assert page.next_cursor is None
            break
        assert page.truncation_reason == "entry_limit" and page.next_cursor
        cursor = page.next_cursor
    else:
        pytest.fail("搜索续页未收敛")
    assert len(seen) == len(set(seen)) == len(names)
    assert set(seen) == {uri + "/" + name for name in names}
    empty = await owner.navigate(query.model_copy(update={"query": "absent"}), local_gateway_id=GATEWAY_ID)
    assert not empty.entries and not empty.truncated and empty.next_cursor is None


@pytest.mark.asyncio
async def test_response_budget_counts_encoded_nodes_and_can_continue(navigation_case) -> None:
    owner, workspace, uri = navigation_case
    for index in range(7):
        (workspace / f"{index}-中文 空格 %.txt").write_text("bounded")
    query = ResourceNavigationRequest(uri=uri, operation="list", max_response_bytes=1200)
    seen, cursor = [], None
    while True:
        page = await owner.navigate(query.model_copy(update={"cursor": cursor}), local_gateway_id=GATEWAY_ID)
        assert len(page.model_dump_json().encode("utf-8")) <= query.max_response_bytes
        seen.extend(entry.uri for entry in page.entries)
        if not page.truncated:
            break
        assert page.truncation_reason == "response_byte_limit" and page.next_cursor
        cursor = page.next_cursor
    assert len(set(seen)) == len(seen) == 7


@pytest.mark.asyncio
async def test_read_bounds_json_escaping_and_utf8_without_claiming_complete(navigation_case) -> None:
    owner, workspace, uri = navigation_case
    content = '\0"\\Ω' * 1000
    (workspace / "text.txt").write_text(content, encoding="utf-8")
    result = await owner.navigate(ResourceNavigationRequest(uri=uri + "/text.txt", operation="read", max_bytes=10000,
        max_response_bytes=1024), local_gateway_id=GATEWAY_ID)
    assert result.content and content.startswith(result.content)
    assert result.truncated and result.truncation_reason == "response_byte_limit"
    assert len(result.model_dump_json().encode("utf-8")) <= 1024
    (workspace / "text.txt").write_text("中文", encoding="utf-8")
    bounded = await owner.navigate(ResourceNavigationRequest(uri=uri + "/text.txt", operation="read", max_bytes=4), local_gateway_id=GATEWAY_ID)
    assert bounded.content == "中" and bounded.truncation_reason == "file_byte_limit"
    (workspace / "text.txt").write_bytes(b"invalid\xff")
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(ResourceNavigationRequest(uri=uri + "/text.txt", operation="read"), local_gateway_id=GATEWAY_ID)
    assert failure.value.code == "invalid_encoding"


@pytest.mark.asyncio
async def test_scan_budget_stops_enumeration_before_sorting_all_files(navigation_case, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, workspace, uri = navigation_case
    for index in range(30):
        (workspace / f"{index}.txt").touch()
    original = resource_navigation.os.scandir
    observed = []
    closed = []

    @contextmanager
    def counted(path):
        with original(path) as entries:
            def consume():
                for entry in entries:
                    if path == workspace:
                        observed.append(entry.name)
                    yield entry
            try:
                yield consume()
            finally:
                closed.append(True)

    monkeypatch.setattr(resource_navigation, "os", SimpleNamespace(scandir=counted))
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(ResourceNavigationRequest(uri=uri, operation="search", query="missing", scan_limit=3), local_gateway_id=GATEWAY_ID)
    assert (failure.value.code, failure.value.reason) == ("output_limit_exceeded", "scan_limit")
    assert len(observed) == 4
    assert closed == [True]


@pytest.mark.asyncio
async def test_one_deadline_covers_root_resolution_and_recursive_scan(navigation_case, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, workspace, uri = navigation_case
    (workspace / "sub").mkdir()
    (workspace / "sub/a.txt").touch()
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.01, 0.01])
    monkeypatch.setattr(resource_navigation, "time", SimpleNamespace(monotonic=lambda: next(ticks, 0.01)))
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(ResourceNavigationRequest(uri=uri, operation="search", query="a", timeout_ms=1), local_gateway_id=GATEWAY_ID)
    assert (failure.value.code, failure.value.reason) == ("output_limit_exceeded", "deadline")


@pytest.mark.asyncio
async def test_response_serialization_consumes_the_same_deadline(navigation_case, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, _, uri = navigation_case
    clock = [0.0]
    original = owner._size

    def serialize(result):
        clock[0] = 0.002
        return original(result)

    monkeypatch.setattr(resource_navigation, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(owner, "_size", serialize)
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(ResourceNavigationRequest(uri=uri, operation="describe", timeout_ms=1), local_gateway_id=GATEWAY_ID)
    assert failure.value.reason == "deadline"


@pytest.mark.asyncio
async def test_deadline_stops_waiting_for_blocked_read_only_filesystem(navigation_case, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, _, uri = navigation_case
    release, finished = threading.Event(), threading.Event()
    original = owner._root

    def blocked(parsed):
        try:
            assert release.wait(2), "测试必须释放只读 worker"
            return original(parsed)
        finally:
            finished.set()

    monkeypatch.setattr(owner, "_root", blocked)
    try:
        with pytest.raises(ResourceFileOperationError) as failure:
            await owner.navigate(ResourceNavigationRequest(uri=uri, operation="describe", timeout_ms=50), local_gateway_id=GATEWAY_ID)
        assert failure.value.reason == "deadline"
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(1), "只读 worker 必须正常收尾"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["not-base64!", "W10", "e30"])
async def test_malformed_cursor_is_an_execution_error_not_uri_failure(navigation_case, value) -> None:
    owner, _, uri = navigation_case
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(ResourceNavigationRequest(uri=uri, operation="list", cursor=value), local_gateway_id=GATEWAY_ID)
    assert failure.value.detail()["owner"] == "resource_file_operations"
    assert failure.value.reason == "invalid_cursor"


def test_cursor_is_not_a_read_or_describe_parameter(navigation_case) -> None:
    _, _, uri = navigation_case
    for operation in ("read", "describe"):
        with pytest.raises(ValidationError, match="cursor"):
            ResourceNavigationRequest(uri=uri, operation=operation, cursor=json.dumps({"offset": 1}))


@pytest.mark.asyncio
async def test_read_format_is_explicit_and_line_windows_preserve_raw_text(navigation_case) -> None:
    owner, workspace, uri = navigation_case
    (workspace / "data.png").write_bytes(b"\xff\x00image")
    query = ResourceNavigationRequest(uri=uri + "/data.png", operation="read")
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(query, local_gateway_id=GATEWAY_ID)
    assert failure.value.code == "invalid_encoding"
    media = await owner.navigate(query.model_copy(update={"read_format": "auto"}), local_gateway_id=GATEWAY_ID)
    assert media.content_encoding == "base64" and base64.b64decode(media.content) == b"\xff\x00image"
    assert media.media_type == "image" and media.mime_type == "image/png" and not media.truncated
    (workspace / "text.txt").write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\ngamma\n")
    text = await owner.navigate(ResourceNavigationRequest(uri=uri + "/text.txt", operation="read",
        line_offset=2, max_lines=1), local_gateway_id=GATEWAY_ID)
    assert text.content == "beta\r\n" and text.line_offset == 2 and text.content_encoding == "utf-8"
    assert text.truncated and text.truncation_reason == "line_limit"
    for operation in ("list", "search", "describe"):
        with pytest.raises(ValidationError, match="格式和行范围"):
            ResourceNavigationRequest(uri=uri, operation=operation, query="text" if operation == "search" else None, line_offset=2)
