from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.resource_uri import resource_display_uri
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.services.infrastructure.resource_platform.sources import resource_navigation
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

WORKSPACE_ID = "019d1b4a-1111-7000-8000-000000000001"
GATEWAY_ID = "gateway_navigation_test"


@pytest.mark.parametrize("field", ["node", "entries", "matches"])
@pytest.mark.parametrize("value", [
    "", "/private/host-root/secret", r"C:\private\host-root\secret", r"\\host\share\secret",
    "file:///private/host-root/secret",
    "boxteam://workspace/root/resources/files/root/%2Fprivate%2Fhost-root",
    "boxteam://workspace/root/resources/files/root/secret?revision=1",
])
def test_navigation_response_rejects_native_or_malformed_references(field, value):
    uri = resource_display_uri(scope="workspace", scope_id=WORKSPACE_ID, kind="files", tail_segments=("root", "missing.txt"))
    node = {"uri": uri, "resource_shape": "file", "operations": ["read"]}
    payload = {"node": node.copy(), "entries": [node.copy()], "matches": [
        {"uri": uri, "line": 1, "column": 1, "text": "/private/user-content", "snippet_start_column": 1}]}
    (payload[field] if field == "node" else payload[field][0])["uri"] = value
    with pytest.raises(ValidationError) as failure:
        ResourceNavigationResultDTO.model_validate(payload)
    assert failure.value.errors(include_input=False)[0]["loc"] == (("node", "uri") if field == "node" else (field, 0, "uri"))
    assert "资源 URI" in str(failure.value)
    if value:
        assert value not in str(failure.value)


def test_navigation_response_accepts_dangling_uri_without_rewriting_body():
    uri = resource_display_uri(scope="workspace", scope_id=WORKSPACE_ID, kind="files", tail_segments=("root", "missing.txt"))
    text = "/private/user-content\nC:\\private\\user-content"
    response = ResourceNavigationResultDTO.model_validate({
        "node": {"uri": uri, "resource_shape": "file", "operations": ["read"]}, "content": text})
    assert response.model_dump()["node"]["uri"] == uri
    assert response.model_dump()["content"] == text


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("collection,artifact_id", [
    ("attachments", "att_" + "a" * 32), ("tool-results", "tool_" + "a" * 24 + ".txt"),
])
async def test_missing_artifact_uses_not_found_reason_without_native_locator(navigation_case, synchronous, collection, artifact_id):
    owner, workspace, _ = navigation_case
    uri = resource_display_uri(scope="workspace", scope_id=WORKSPACE_ID, kind="session",
        tail_segments=("ses_019c3567da377a6f855e1bddac8f1f76", collection, artifact_id))
    calls = []

    def missing_reader(address, max_bytes):
        calls.append((address, max_bytes))
        raise FileNotFoundError(2, "missing original", str(workspace / ".boxteam/private-artifact"))

    request = ResourceNavigationRequest(uri=uri, operation="read")
    with pytest.raises(ResourceUriResolveError) as failure:
        if synchronous:
            owner.navigate_sync(request, local_gateway_id=GATEWAY_ID, artifact_reader=missing_reader)
        else:
            await owner.navigate(request, local_gateway_id=GATEWAY_ID, artifact_reader=missing_reader)
    assert failure.value.reason_code == "unknown_resource"
    assert str(workspace) not in str(failure.value)
    assert isinstance(failure.value.__cause__, FileNotFoundError)
    assert calls == [(uri, request.max_bytes)]


@pytest.mark.asyncio
async def test_files_private_boundary_is_shared_with_native_aliases_but_not_skill_owner(navigation_case):
    owner, workspace, uri = navigation_case
    private = workspace / ".boxteam"
    skill = private / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill original", encoding="utf-8")
    (workspace / "private-alias").symlink_to(private, target_is_directory=True)
    (workspace / ".github").mkdir()
    for target in (uri + "/.boxteam", uri + "/private-alias/skills/example/SKILL.md"):
        with pytest.raises(ResourceUriResolveError) as failure:
            await owner.navigate(ResourceNavigationRequest(uri=target, operation="read"), local_gateway_id=GATEWAY_ID)
        assert failure.value.reason_code == "capability_denied"
    listed = await owner.navigate(ResourceNavigationRequest(uri=uri, operation="list"), local_gateway_id=GATEWAY_ID)
    assert {child.uri for child in listed.entries} == {uri + "/.github"}
    skill_uri = resource_display_uri(scope="workspace", scope_id=WORKSPACE_ID, kind="skills", tail_segments=("example", "SKILL.md"))
    read = await owner.navigate(ResourceNavigationRequest(uri=skill_uri, operation="read"), local_gateway_id=GATEWAY_ID)
    assert read.content == "skill original"


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


@pytest.mark.parametrize("synchronous", [False, True])
async def test_execution_deadline_does_not_change_cursor_binding(navigation_case, synchronous):
    owner, workspace, uri = navigation_case
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_text(name)
    query = ResourceNavigationRequest(uri=uri, operation="list", limit=1, timeout_ms=2000)
    names, cursor = [], None
    for remaining in (1.5, 0.8, 0.2):
        request = query.model_copy(update={"cursor": cursor})
        options = {"local_gateway_id": GATEWAY_ID, "deadline": time.monotonic() + remaining}
        page = owner.navigate_sync(request, **options) if synchronous else await owner.navigate(request, **options)
        names.extend(node.uri for node in page.entries)
        cursor = page.next_cursor
    assert names == [uri + "/" + name for name in ("a.txt", "b.txt", "c.txt")]
    assert cursor is None


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
    assert media.content_hash == "sha256:" + hashlib.sha256(b"\xff\x00image").hexdigest()
    (workspace / "text.txt").write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\ngamma\n")
    text = await owner.navigate(ResourceNavigationRequest(uri=uri + "/text.txt", operation="read",
        line_offset=2, max_lines=1), local_gateway_id=GATEWAY_ID)
    assert text.content == "beta\r\n" and text.line_offset == 2 and text.content_encoding == "utf-8"
    assert text.truncated and text.truncation_reason == "line_limit"
    assert text.content_hash == "sha256:" + hashlib.sha256((workspace / "text.txt").read_bytes()).hexdigest()
    for operation in ("list", "search", "describe"):
        with pytest.raises(ValidationError, match="格式和行范围"):
            ResourceNavigationRequest(uri=uri, operation=operation, query="text" if operation == "search" else None, line_offset=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"", b"\xef\xbb\xbfalpha\r\nbeta\n", "正文\r\n".encode()])
async def test_read_hash_versions_original_bytes_and_not_display(navigation_case, raw) -> None:
    owner, workspace, uri = navigation_case
    (workspace / "text.txt").write_bytes(raw)
    result = await owner.navigate(ResourceNavigationRequest(uri=uri + "/text.txt", operation="read"), local_gateway_id=GATEWAY_ID)
    assert result.content == raw.decode("utf-8")
    assert result.content_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert (await owner.navigate(ResourceNavigationRequest(uri=uri, operation="list"), local_gateway_id=GATEWAY_ID)).content_hash is None


@pytest.mark.asyncio
async def test_response_truncation_keeps_full_hash_but_byte_window_does_not(navigation_case) -> None:
    owner, workspace, uri = navigation_case
    raw = ('正文"\\\n' * 500).encode()
    (workspace / "text.txt").write_bytes(raw)
    query = ResourceNavigationRequest(uri=uri + "/text.txt", operation="read", max_bytes=len(raw), max_response_bytes=1024)
    displayed = await owner.navigate(query, local_gateway_id=GATEWAY_ID)
    assert displayed.truncation_reason == "response_byte_limit"
    assert displayed.content_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert displayed.content_hash != "sha256:" + hashlib.sha256(displayed.content.encode()).hexdigest()
    window = await owner.navigate(query.model_copy(update={"max_bytes": 16}), local_gateway_id=GATEWAY_ID)
    assert window.truncation_reason == "file_byte_limit" and window.content_hash is None


@pytest.mark.asyncio
async def test_read_rejects_file_rewritten_during_read_even_with_restored_mtime(navigation_case, monkeypatch) -> None:
    owner, workspace, uri = navigation_case
    path = workspace / "text.txt"
    path.write_bytes(b"first")
    metadata = path.stat()
    original_open = Path.open

    @contextmanager
    def changed_open(target, mode="r", *args, **kwargs):
        with original_open(target, mode, *args, **kwargs) as stream:
            yield stream
        if target == path and mode == "rb":
            path.write_bytes(b"later")
            resource_navigation.os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

    monkeypatch.setattr(Path, "open", changed_open)
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(ResourceNavigationRequest(uri=uri + "/text.txt", operation="read"), local_gateway_id=GATEWAY_ID)
    assert (failure.value.code, failure.value.reason) == ("execution_failed", "file_changed")


@pytest.mark.asyncio
async def test_content_search_preserves_match_position_when_excerpt_is_bounded(navigation_case) -> None:
    owner, workspace, uri = navigation_case
    text = "中文" * 1000 + "needle" + "后文" * 1000
    (workspace / "long.txt").write_text(text)
    result = await owner.navigate(ResourceNavigationRequest(uri=uri + "/long.txt", operation="search",
        search_mode="content", query="needle", max_snippet_bytes=32), local_gateway_id=GATEWAY_ID)
    match, = result.matches
    assert (match.line, match.column) == (1, 2001)
    assert match.truncated and len(match.text.encode()) <= 32 and "needle" in match.text
    assert text[match.snippet_start_column - 1:].startswith(match.text)
    assert match.uri == result.entries[0].uri == uri + "/long.txt"


@pytest.fixture
def artifact_case(navigation_case):
    from app.abstractions.resource_file_content import ResourceFileContent
    from app.core.resource_uri import session_artifact_uri

    owner, _workspace, _uri = navigation_case
    session_id = "ses_0123456789ab7def8123456789abcdef"
    uri = session_artifact_uri(WORKSPACE_ID, session_id, "tool-results", "tool_" + "a" * 24 + ".txt")
    state = {"data": b"hit one\nmiss\nhit two\nhit three\n", "mime": "text/plain", "reads": []}

    def read(uri, maximum):
        state["reads"].append((uri, maximum))
        return ResourceFileContent(state["data"], state["mime"])

    return owner, uri, read, state


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_artifact_search_is_single_file_bounded_and_reopenable(artifact_case, synchronous):
    owner, uri, read, state = artifact_case
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="hit", limit=1)
    matches, cursor = [], None
    for _ in range(3):
        request = query.model_copy(update={"cursor": cursor})
        page = (owner.navigate_sync(request, local_gateway_id=GATEWAY_ID, artifact_reader=read) if synchronous
                else await owner.navigate(request, local_gateway_id=GATEWAY_ID, artifact_reader=read))
        matches.extend((match.line, match.text, match.uri) for match in page.matches)
        cursor = page.next_cursor
    assert matches == [(1, "hit one", uri), (3, "hit two", uri), (4, "hit three", uri)]
    assert cursor is None and not page.truncated
    filtered = await owner.navigate(query.model_copy(update={"file_glob": "*.json"}), local_gateway_id=GATEWAY_ID, artifact_reader=read)
    assert not filtered.entries and not filtered.matches
    data = await owner.navigate(ResourceNavigationRequest(uri=uri, operation="read", line_offset=3, max_lines=1),
                                local_gateway_id=GATEWAY_ID, artifact_reader=read)
    assert data.content == "hit two\n"
    assert data.content_hash == "sha256:" + hashlib.sha256(state["data"]).hexdigest()
    with pytest.raises(resource_navigation.ResourceUriResolveError, match="私有目录"):
        await owner.navigate(ResourceNavigationRequest(uri=uri, operation="list"), local_gateway_id=GATEWAY_ID, artifact_reader=read)


@pytest.mark.asyncio
async def test_artifact_cursor_rejects_changed_bytes_and_query(artifact_case):
    owner, uri, read, state = artifact_case
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="hit", limit=1)
    first = await owner.navigate(request, local_gateway_id=GATEWAY_ID, artifact_reader=read)
    for changed in ({"query": "miss"}, {"file_glob": "*.json"}, {"max_bytes": 4}):
        with pytest.raises(ResourceFileOperationError) as invalid:
            await owner.navigate(request.model_copy(update={"cursor": first.next_cursor, **changed}), local_gateway_id=GATEWAY_ID, artifact_reader=read)
        assert invalid.value.reason == "invalid_cursor"
    state["data"] += b"hit four\n"
    with pytest.raises(ResourceFileOperationError) as stale:
        await owner.navigate(request.model_copy(update={"cursor": first.next_cursor}), local_gateway_id=GATEWAY_ID, artifact_reader=read)
    assert stale.value.reason == "stale_cursor"


@pytest.mark.asyncio
async def test_artifact_media_uses_owner_mime_and_cannot_bypass_explicit_capability(artifact_case, navigation_case):
    from app.core.resource_uri import session_artifact_uri

    owner, _uri, read, state = artifact_case
    uri = session_artifact_uri(WORKSPACE_ID, "ses_0123456789ab7def8123456789abcdef", "attachments", "att_" + "b" * 32)
    state.update(data=b"\x89PNG\r\n\x1a\n\xff", mime="image/png")
    owner._artifact_reader = read
    result = await owner.navigate(ResourceNavigationRequest(uri=uri, operation="read", read_format="auto"), local_gateway_id=GATEWAY_ID)
    assert (result.media_type, result.mime_type) == ("image", "image/png")
    assert base64.b64decode(result.content) == state["data"]

    def denied(uri, maximum):
        raise PermissionError("缺少 emitting model-call 的精确 membership")

    with pytest.raises(resource_navigation.ResourceUriResolveError, match="membership") as denied_result:
        await owner.navigate(ResourceNavigationRequest(uri=uri, operation="read", read_format="auto"), local_gateway_id=GATEWAY_ID, artifact_reader=denied)
    assert denied_result.value.reason_code == "capability_denied"
    assert len(state["reads"]) == 1
    with pytest.raises(ResourceFileOperationError) as exceeded:
        await owner.navigate(ResourceNavigationRequest(uri=uri, operation="read", read_format="auto", max_bytes=2), local_gateway_id=GATEWAY_ID)
    assert exceeded.value.reason == "file_byte_limit"
    with pytest.raises(ResourceFileOperationError) as encoding:
        await owner.navigate(ResourceNavigationRequest(uri=uri, operation="read"), local_gateway_id=GATEWAY_ID)
    assert encoding.value.code == "invalid_encoding"
