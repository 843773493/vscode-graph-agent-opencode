"""同一文件导航入口的显式文本选项、路径过滤和码点位置。"""

import pytest
from pydantic import ValidationError

from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)
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
@pytest.mark.parametrize("query,options,reason", [
    ("[", {"is_regex": True}, "invalid_search_pattern"),
    ("hit", {"ignore_mode": "respect"}, "unsupported_search_option"),
])
async def test_unsupported_or_invalid_search_fails_even_for_empty_directory(navigation_case, synchronous, query, options, reason):
    owner, _, uri = navigation_case
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query=query, search_options=options)
    with pytest.raises(ResourceFileOperationError) as failure:
        if synchronous:
            owner.navigate_sync(request, local_gateway_id=GATEWAY_ID)
        else:
            await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert failure.value.reason == reason


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
