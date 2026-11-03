"""同一文件导航入口的显式文本选项、路径过滤和码点位置。"""

import json
import os
import time
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationSearchOptions,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from tests.harness.python.run_context import TestRunContext
from tests.unit.services.infrastructure.resource_platform.sources.test_resource_navigation import (
    GATEWAY_ID,
)
from tests.unit.services.infrastructure.resource_platform.sources.test_resource_navigation import (
    artifact_case as artifact_case,  # noqa: PLC0414 - 精确产物读取capability沿原fixture
)
from tests.unit.services.infrastructure.resource_platform.sources.test_resource_navigation import (
    navigation_case as navigation_case,  # noqa: PLC0414 - 复用隔离文件owner装配
)


@pytest.mark.parametrize("synchronous", [False, True])
async def test_content_cursor_skips_completed_file_bodies(navigation_case, monkeypatch, request, synchronous):
    owner, workspace, uri = navigation_case
    count = 64
    body = "unchanged filler\n" * 4095 + "needle\n"
    names = [f"{index:03}.txt" for index in range(count)]
    for name in names:
        (workspace / name).write_text(body, encoding="utf-8")
    reads = Counter()
    read_bytes = 0
    read = owner._read_bytes

    def observed(path, *args, **kwargs):
        nonlocal read_bytes
        data = read(path, *args, **kwargs)
        reads[path.name] += 1
        read_bytes += len(data)
        return data

    monkeypatch.setattr(owner, "_read_bytes", observed)
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle", limit=1, max_bytes=128 * 1024)
    seen, durations, cursor = [], [], None
    for _ in range(count + 1):
        started = time.perf_counter()
        payload = query.model_copy(update={"cursor": cursor})
        page = owner.navigate_sync(payload, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(payload, local_gateway_id=GATEWAY_ID)
        durations.append(time.perf_counter() - started)
        seen.extend(hit.uri for hit in page.matches)
        assert len(page.matches) == 1
        cursor = page.next_cursor
        if not page.truncated:
            assert cursor is None
            break
        assert cursor and page.truncation_reason == "entry_limit"
    else:
        pytest.fail("文件正文分页未收敛")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "pagination-measurement.json").write_text(json.dumps({
        "synchronous": synchronous, "files": count, "file_bytes": len(body.encode()), "pages": len(durations),
        "body_reads": reads.total(), "body_bytes": read_bytes, "page_seconds": durations,
        "elapsed_seconds": sum(durations), "per_file_reads": dict(reads),
    }, indent=2), encoding="utf-8")
    assert seen == [uri + "/" + name for name in names]
    # 下一页可重读自己的锚点，不能再读此前已完成文件的正文。
    assert reads.total() == count * 2 - 1
    assert reads[names[0]] == 1 and all(reads[name] == 2 for name in names[1:])
    assert read_bytes == len(body.encode()) * (count * 2 - 1)


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.skipif(os.name == "nt", reason="此用例验证 POSIX 修改 ctime；Windows ctime 仍可能表示创建时间")
async def test_consumed_body_change_rejects_cursor_before_any_body_read(navigation_case, monkeypatch, synchronous):
    owner, workspace, uri = navigation_case
    first = workspace / "a.txt"
    first.write_text("needle first")
    (workspace / "b.txt").write_text("needle second")
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle", limit=1)

    async def search(payload):
        return owner.navigate_sync(payload, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(payload, local_gateway_id=GATEWAY_ID)

    page = await search(query)
    assert page.next_cursor and [hit.uri for hit in page.matches] == [uri + "/a.txt"]
    before = first.stat()
    first.write_text("needle other")
    os.utime(first, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert first.stat().st_size == before.st_size
    assert first.stat().st_ctime_ns != before.st_ctime_ns
    reads = []
    monkeypatch.setattr(owner, "_read_bytes", lambda *args, **kwargs: reads.append(args))
    with pytest.raises(ResourceFileOperationError) as failure:
        await search(query.model_copy(update={"cursor": page.next_cursor}))
    assert failure.value.reason == "stale_cursor" and not reads


@pytest.mark.parametrize("synchronous", [False, True])
async def test_unconsumed_body_change_is_visible_without_rereading_prefix(navigation_case, monkeypatch, synchronous):
    owner, workspace, uri = navigation_case
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_text("needle old")
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle", limit=1)

    async def search(payload):
        return owner.navigate_sync(payload, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(payload, local_gateway_id=GATEWAY_ID)

    first = await search(query)
    assert first.next_cursor
    (workspace / "b.txt").write_text("needle updated")
    reads = []
    read = owner._read_bytes

    def observed(path, *args, **kwargs):
        reads.append(path.name)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(owner, "_read_bytes", observed)
    second = await search(query.model_copy(update={"cursor": first.next_cursor}))
    assert [(hit.uri, hit.text) for hit in second.matches] == [(uri + "/b.txt", "needle updated")]
    assert "a.txt" not in reads and "b.txt" in reads


@pytest.mark.parametrize("synchronous", [False, True])
async def test_fresh_owner_resumes_file_and_match_position(navigation_case, synchronous):
    owner, workspace, uri = navigation_case
    for name in ("a.txt", "b.txt"):
        (workspace / name).write_bytes("🧪 needle needle\r\nneedle Ω\r\n".encode())
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle", limit=1,
        search_options=ResourceNavigationSearchOptions(all_matches=True, context_lines=1))
    seen, cursor = [], None
    for _ in range(7):
        # 每页重建 owner，续查不能依赖上页留下的匹配列表或目录缓存。
        owner = ResourceNavigation(workspace_root=workspace, workspace_id=owner._workspace_id,
            project_root=Path.cwd(), file_paths=owner._file_paths)
        payload = query.model_copy(update={"cursor": cursor})
        page = owner.navigate_sync(payload, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(payload, local_gateway_id=GATEWAY_ID)
        seen.extend((hit.uri, hit.line, hit.column, hit.utf16_column) for hit in page.matches)
        assert page.contexts and len(page.matches) == 1
        cursor = page.next_cursor
        if not page.truncated:
            assert cursor is None
            break
        assert cursor
    else:
        pytest.fail("重建 owner 后的正文续页未收敛")
    assert seen == [(uri + "/" + name, line, column, utf16) for name in ("a.txt", "b.txt")
        for line, column, utf16 in ((1, 3, 4), (1, 10, 11), (2, 1, 1))]


@pytest.mark.parametrize("synchronous", [False, True])
async def test_model_coverage_excludes_body_before_read_count_and_cursor(navigation_case, monkeypatch, synchronous):
    owner, workspace, uri = navigation_case
    directory = workspace / "model-coverage"
    directory.mkdir()
    excluded = directory / "a.txt"
    excluded.write_bytes(b"\xffcovered body must not be read")
    for name in ("b.txt", "c.txt"):
        (directory / name).write_text("needle\ncontext")
    root = uri + "/model-coverage"
    reads = []
    read = owner._read_bytes

    def observed(path, *args, **kwargs):
        reads.append(path)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(owner, "_read_bytes", observed)
    request = ResourceNavigationRequest(uri=root, operation="search", search_mode="content", query="needle", limit=1,
        search_options={"excluded_resource_uris": [root + "/a.txt"], "context_lines": 1})

    async def search(payload):
        return owner.navigate_sync(payload, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(payload, local_gateway_id=GATEWAY_ID)

    first = await search(request)
    assert [hit.uri for hit in first.matches] == [root + "/b.txt"]
    assert first.truncated and first.next_cursor and first.truncation_reason == "entry_limit"
    assert excluded not in reads
    assert {line.uri for line in first.contexts} == {root + "/b.txt"}
    # 覆盖文件的磁盘正文改变不应成为这份正文续页的前缀依赖。
    excluded.write_text("needle now exists on disk")
    following = await search(request.model_copy(update={"cursor": first.next_cursor}))
    assert [hit.uri for hit in following.matches] == [root + "/c.txt"]
    assert not following.truncated and following.next_cursor is None and excluded not in reads
    changed = ResourceNavigationRequest(**{**request.model_dump(), "cursor": first.next_cursor, "search_options": {}})
    with pytest.raises(ResourceFileOperationError) as failure:
        await search(changed)
    assert failure.value.reason == "invalid_cursor"
    reads.clear()
    exact = await search(request.model_copy(update={"uri": root + "/a.txt"}))
    assert not exact.matches and not exact.entries and not reads


@pytest.mark.parametrize("foreign", [
    "boxteam://workspace/another/resources/files/root/a.txt",
    "boxteam://another-gateway/workspace/{workspace}/resources/files/root/a.txt",
    "boxteam://workspace/{workspace}/resources/files/root/outside.txt",
])
async def test_coverage_rejects_foreign_owner_or_root_before_body_read(navigation_case, monkeypatch, foreign):
    owner, workspace, uri = navigation_case
    (workspace / "coverage-root").mkdir()
    (workspace / "coverage-root/a.txt").write_text("needle")
    reads = []
    monkeypatch.setattr(owner, "_read_bytes", lambda *args, **kwargs: reads.append(args))
    request = ResourceNavigationRequest(uri=uri + "/coverage-root", operation="search", search_mode="content", query="needle",
        search_options={"excluded_resource_uris": [foreign.format(workspace=owner._workspace_id)]})
    with pytest.raises(ResourceUriResolveError) as failure:
        await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert failure.value.reason_code == "scope_mismatch" and not reads


def test_coverage_is_a_canonical_uri_set_only_for_content_queries():
    uri = "boxteam://workspace/ws/resources/files/root/a.txt"
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle",
        search_options={"excluded_resource_uris": [uri, uri]})
    assert request.search_options.excluded_resource_uris == (uri,)
    for options in ({"excluded_resource_uris": ["/tmp/native.txt"]}, {"excluded_resource_uris": ["boxteam://invalid"]}):
        with pytest.raises(ValidationError):
            ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle", search_options=options)
    with pytest.raises(ValidationError, match="文本匹配选项"):
        ResourceNavigationRequest(uri=uri, operation="search", query="needle", search_options=request.search_options)


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("query,options,columns", [
    ("needle", {}, [8]),
    ("needle", {"case_mode": "insensitive", "all_matches": True}, [1, 8, 15]),
    ("needle", {"case_mode": "smart", "all_matches": True}, [1, 8, 15]),
    ("Needle", {"case_mode": "smart", "all_matches": True}, [1]),
    (r"n\De\b", {"case_mode": "smart", "is_regex": True, "all_matches": True}, [22, 26]),
    (r"N\De\b", {"case_mode": "smart", "is_regex": True, "all_matches": True}, [22]),
])
async def test_text_options_keep_literal_defaults_and_smart_regex_case(navigation_case, synchronous, query, options, columns):
    owner, workspace, uri = navigation_case
    (workspace / "text.txt").write_text("Needle needle NEEDLE Nxe nxe\n", encoding="utf-8")
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query=query,
        search_options=options)
    page = owner.navigate_sync(request, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert [match.column for match in page.matches] == columns
    assert all(match.line == match.end_line == 1 and match.end_column > match.column for match in page.matches)
    assert all(match.uri == uri + "/text.txt" for match in page.matches)


@pytest.mark.parametrize("separators,columns", [(None, [1, 25, 33]), ("._", [1, 17, 25]), (".", [1, 25])])
async def test_whole_word_uses_explicit_editor_separators(navigation_case, separators, columns):
    owner, workspace, uri = navigation_case
    (workspace / "text.txt").write_text("foo foobar xxxx foo_bar foo.bar foo🧪bar")
    page = await owner.navigate(ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="foo",
        search_options={"whole_word": True, "word_separators": separators, "all_matches": True}), local_gateway_id=GATEWAY_ID)
    assert [match.column for match in page.matches] == columns


@pytest.mark.parametrize("synchronous", [False, True])
async def test_multiline_crlf_emoji_positions_are_codepoints_not_snippet_offsets(navigation_case, synchronous):
    owner, workspace, uri = navigation_case
    (workspace / "text.txt").write_bytes("😀head\r\n🧪first NEEDLE\r\nsecond Ω tail\r\n".encode())
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query=r"NEEDLE\r?\nsecond Ω",
        search_options={"is_regex": True, "is_multiline": True, "all_matches": True}, max_snippet_bytes=16)
    page = owner.navigate_sync(query, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(query, local_gateway_id=GATEWAY_ID)
    match, = page.matches
    assert (match.line, match.column, match.end_line, match.end_column) == (2, 8, 3, 9)
    assert (match.utf16_column, match.utf16_end_column, match.snippet_start_utf16_column) == (9, 9, 7)
    assert match.text == "t NEEDLE" and match.snippet_start_column == 6
    single = query.model_copy(update={"search_options": query.search_options.model_copy(update={"is_multiline": False})})
    no_match = owner.navigate_sync(single, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(single, local_gateway_id=GATEWAY_ID)
    assert not no_match.matches and not no_match.truncated


async def test_clipped_snippet_keeps_utf16_coordinates_of_hidden_emoji_prefix(navigation_case):
    owner, workspace, uri = navigation_case
    (workspace / "text.txt").write_text("😀" * 100 + "needle")
    page = await owner.navigate(ResourceNavigationRequest(uri=uri, operation="search", search_mode="content",
        query="needle", max_snippet_bytes=16), local_gateway_id=GATEWAY_ID)
    match, = page.matches
    assert (match.column, match.end_column, match.snippet_start_column) == (101, 107, 99)
    assert (match.utf16_column, match.utf16_end_column, match.snippet_start_utf16_column) == (201, 207, 197)
    assert match.text == "😀😀needle" and match.truncated


@pytest.mark.parametrize("synchronous", [False, True])
async def test_artifact_capability_uses_same_multiline_options_and_ranges(artifact_case, synchronous):
    owner, uri, read, state = artifact_case
    state["data"] = "🧪 Hit hit\r\nNEXT Ω".encode()
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query=r"hit\r?\nNEXT Ω",
        search_options={"is_regex": True, "is_multiline": True, "case_mode": "insensitive", "include_globs": ["*.txt"]})
    page = (owner.navigate_sync(query, local_gateway_id=GATEWAY_ID, artifact_reader=read) if synchronous
            else await owner.navigate(query, local_gateway_id=GATEWAY_ID, artifact_reader=read))
    match, = page.matches
    assert (match.uri, match.line, match.column, match.end_line, match.end_column) == (uri, 1, 7, 2, 7)
    assert state["reads"] == [(uri, query.max_bytes)]


async def test_all_matches_page_within_one_line_and_bind_options(navigation_case):
    owner, workspace, uri = navigation_case
    (workspace / "text.txt").write_text("🧪 hit hit hit\n")
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="hit", limit=1,
        search_options={"all_matches": True})
    columns, cursor = [], None
    for _ in range(4):
        page = await owner.navigate(query.model_copy(update={"cursor": cursor}), local_gateway_id=GATEWAY_ID)
        columns.extend(match.column for match in page.matches)
        if not page.truncated:
            assert page.next_cursor is None
            break
        assert page.truncation_reason == "entry_limit" and page.next_cursor
        cursor = page.next_cursor
    else:
        pytest.fail("同一行的搜索续页未收敛")
    assert columns == [3, 7, 11]
    with pytest.raises(ResourceFileOperationError) as failure:
        await owner.navigate(query.model_copy(update={"cursor": cursor, "search_options": None}), local_gateway_id=GATEWAY_ID)
    assert failure.value.reason == "invalid_cursor"


@pytest.mark.parametrize("search_mode,query", [("content", "hit"), ("fuzzy", "mt")])
async def test_path_filters_prune_excluded_subtree_and_skip_nonincluded_binary(navigation_case, search_mode, query):
    owner, workspace, uri = navigation_case
    for name in ("src/main.txt", "excluded/ignored.txt", "src/ignored.skip", "binary.dat"):
        file = workspace / name
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b"hit\n" if name.endswith("main.txt") else b"\xffinvalid UTF8")
    page = await owner.navigate(ResourceNavigationRequest(uri=uri, operation="search", search_mode=search_mode, query=query,
        search_options={"include_globs": ["**/*.txt"], "exclude_globs": ["excluded/**"]}), local_gateway_id=GATEWAY_ID)
    assert [entry.uri for entry in page.entries] == [uri + "/src/main.txt"]


async def test_ignore_mode_preserves_default_and_explicit_none(navigation_case):
    owner, workspace, uri = navigation_case
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules/match.txt").write_text("hit")
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="hit")
    assert not (await owner.navigate(query, local_gateway_id=GATEWAY_ID)).matches
    enabled = ResourceNavigationRequest(**{**query.model_dump(), "search_options": {"ignore_mode": "none"}})
    assert (await owner.navigate(enabled, local_gateway_id=GATEWAY_ID)).matches[0].uri == uri + "/node_modules/match.txt"


@pytest.mark.parametrize("synchronous", [False, True])
async def test_invalid_regex_fails_even_for_empty_directory(navigation_case, synchronous):
    owner, _, uri = navigation_case
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="[", search_options={"is_regex": True})
    with pytest.raises(ResourceFileOperationError) as failure:
        if synchronous:
            owner.navigate_sync(request, local_gateway_id=GATEWAY_ID)
        else:
            await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert failure.value.reason == "invalid_search_pattern"


@pytest.mark.parametrize("synchronous", [False, True])
async def test_regex_work_uses_remaining_owner_deadline(navigation_case, synchronous):
    owner, workspace, uri = navigation_case
    (workspace / "text.txt").write_text("a" * 32000 + "!")
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="(a+)+$",
        timeout_ms=20, search_options={"is_regex": True})
    with pytest.raises(ResourceFileOperationError) as failure:
        if synchronous:
            owner.navigate_sync(query, local_gateway_id=GATEWAY_ID)
        else:
            await owner.navigate(query, local_gateway_id=GATEWAY_ID)
    assert (failure.value.code, failure.value.reason) == ("output_limit_exceeded", "deadline")


@pytest.mark.parametrize("extra", [{"only_open": True}, {"case_mode": "unknown"}, {"include_globs": [""]}])
def test_unimplemented_or_invalid_options_are_not_silently_dropped(extra):
    with pytest.raises(ValidationError):
        ResourceNavigationRequest(uri="not-yet-resolved", operation="search", search_mode="content", query="hit", search_options=extra)


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("context_lines", [0, 1, 100])
async def test_context_lines_share_read_and_deduplicate_overlapping_windows(navigation_case, monkeypatch, synchronous, context_lines):
    owner, workspace, uri = navigation_case
    file = workspace / "text.txt"
    file.write_bytes("🧪before\r\nhit hit\r\nbetween\r\nhit\r\nafter".encode())
    reads = []
    read = type(file).open

    def observe(path, *args, **kwargs):
        if path == file:
            reads.append(args)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(type(file), "open", observe)
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="hit",
        search_options={"all_matches": True, "context_lines": context_lines})
    page = owner.navigate_sync(request, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert len(reads) == 1 and len(page.matches) == 3
    expected = [] if context_lines == 0 else [(1, "🧪before"), (3, "between"), (5, "after")]
    if context_lines == 100:
        expected = [(1, "🧪before"), (3, "between"), (4, "hit"), (5, "after"), (2, "hit hit")]
    assert [(line.line, line.text) for line in page.contexts] == expected
    assert all(line.uri == uri + "/text.txt" and not line.truncated for line in page.contexts)


@pytest.mark.parametrize("synchronous", [False, True])
async def test_multiline_context_surrounds_whole_match_and_clips_utf8(artifact_case, synchronous):
    owner, uri, read, state = artifact_case
    state["data"] = ("😀" * 5 + "\r\nhit\r\nNEXT\r\nafter").encode()
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query=r"hit\r\nNEXT",
        max_snippet_bytes=16, search_options={"is_regex": True, "is_multiline": True, "context_lines": 100})
    page = (owner.navigate_sync(request, local_gateway_id=GATEWAY_ID, artifact_reader=read) if synchronous
            else await owner.navigate(request, local_gateway_id=GATEWAY_ID, artifact_reader=read))
    assert [(line.line, line.text, line.truncated) for line in page.contexts] == [(1, "😀" * 4, True), (4, "after", False)]
    assert (page.matches[0].line, page.matches[0].end_line) == (2, 3)
    assert state["reads"] == [(uri, request.max_bytes)]


@pytest.mark.parametrize("synchronous", [False, True])
async def test_context_pagination_budget_and_cursor_options(navigation_case, synchronous):
    owner, workspace, uri = navigation_case
    (workspace / "text.txt").write_text("before\nhit\nbetween\nhit\nafter")
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="hit", limit=1,
        search_options={"context_lines": 1}, max_response_bytes=2400)
    first = owner.navigate_sync(request, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert first.truncated and first.next_cursor
    assert [line.line for line in first.contexts] == [1, 3]
    assert len(first.model_dump_json().encode()) <= request.max_response_bytes
    continued = request.model_copy(update={"cursor": first.next_cursor})
    second = owner.navigate_sync(continued, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(continued, local_gateway_id=GATEWAY_ID)
    assert not second.truncated and [line.line for line in second.contexts] == [3, 5]
    changed = continued.model_copy(update={"search_options": request.search_options.model_copy(update={"context_lines": 0})})
    with pytest.raises(ResourceFileOperationError) as failure:
        if synchronous:
            owner.navigate_sync(changed, local_gateway_id=GATEWAY_ID)
        else:
            await owner.navigate(changed, local_gateway_id=GATEWAY_ID)
    assert failure.value.reason == "invalid_cursor"
    (workspace / "text.txt").write_text("a" * 3000 + "\nhit\n" + "b" * 3000)
    bounded = request.model_copy(update={"max_response_bytes": 2000})
    with pytest.raises(ResourceFileOperationError) as failure:
        if synchronous:
            owner.navigate_sync(bounded, local_gateway_id=GATEWAY_ID)
        else:
            await owner.navigate(bounded, local_gateway_id=GATEWAY_ID)
    assert failure.value.reason == "response_byte_limit"


@pytest.mark.parametrize("options,mode", [({"context_lines": -1}, "content"), ({"context_lines": 101}, "content"),
                                        ({"context_lines": 1.5}, "content"), ({"context_lines": 1}, "fuzzy")])
def test_context_rejects_invalid_count_or_nontext_mode(options, mode):
    with pytest.raises(ValidationError):
        ResourceNavigationRequest(uri="not-yet-resolved", operation="search", search_mode=mode, query="hit", search_options=options)
