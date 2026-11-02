"""目标文件 owner 的 ignore 继承、剪枝、预算和续页新鲜度。"""

from pathlib import Path

import pytest

from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from tests.unit.services.infrastructure.resource_platform.sources.test_resource_navigation import (
    GATEWAY_ID,
)
from tests.unit.services.infrastructure.resource_platform.sources.test_resource_navigation import (
    navigation_case as navigation_case,  # noqa: PLC0414 - 复用原文件 owner 的隔离装配
)


@pytest.fixture
def ignored_tree(navigation_case):
    owner, workspace, uri = navigation_case
    files = {"keep.txt": "needle", "root-only.txt": "needle", "debug.log": "needle", "keep.log": "needle",
             "nested/root-only.txt": "needle", "nested/keep.txt": "needle", "nested/child.txt": "needle",
             "nested/other.log": "needle", "cache/binary.txt": "needle"}
    for name, content in files.items():
        file = workspace / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    (workspace / ".gitignore").write_bytes(b"\xef\xbb\xbf/root-only.txt\r\n/cache/\r\nnested/*\r\n!nested/\r\n!nested/keep.txt\r\n")
    (workspace / ".ignore").write_text("*.log\n!keep.log\n")
    (workspace / "nested/.gitignore").write_text("!child.txt\n!root-only.txt\n!other.log\n")
    (workspace / "cache/binary.txt").write_bytes(b"\xff")
    return owner, workspace, uri


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("mode,query", [("content", "needle"), ("fuzzy", "txt"), ("glob", "*.txt")])
async def test_local_rules_inherit_negate_and_prune_before_read(ignored_tree, synchronous, mode, query):
    owner, _, uri = ignored_tree
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode=mode, query=query,
        search_options={"ignore_mode": "respect"})
    result = owner.navigate_sync(request, local_gateway_id=GATEWAY_ID) if synchronous else await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    expected = {uri + "/" + name for name in ("keep.txt", "nested/child.txt", "nested/keep.txt", "nested/root-only.txt")}
    if mode == "content":
        expected.add(uri + "/keep.log")
        assert {match.uri for match in result.matches} == expected
    else:
        assert {entry.uri for entry in result.entries} == expected
    assert not result.truncated


async def test_explicit_root_does_not_read_parent_rules_and_explicit_file_remains_searchable(navigation_case):
    owner, workspace, uri = navigation_case
    (workspace / ".gitignore").write_text("selected/\n*.txt\n")
    (workspace / "selected").mkdir()
    (workspace / "selected/file.txt").write_text("needle")
    for target in (uri + "/selected", uri + "/selected/file.txt"):
        result = await owner.navigate(ResourceNavigationRequest(uri=target, operation="search", search_mode="content", query="needle",
            search_options={"ignore_mode": "respect"}), local_gateway_id=GATEWAY_ID)
        assert [match.uri for match in result.matches] == [uri + "/selected/file.txt"]


@pytest.mark.parametrize("change", ["rewrite", "create", "delete"])
async def test_ignored_rule_file_still_protects_cursor_prefix(navigation_case, change):
    owner, workspace, uri = navigation_case
    rule = workspace / ".gitignore"
    if change != "create":
        rule.write_text("unused-a\n")
    for name in ("first.txt", "second.txt"):
        (workspace / name).write_text("needle")
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle", limit=1,
        search_options={"ignore_mode": "respect", "exclude_globs": [".gitignore"]})
    first = await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert first.next_cursor
    if change == "delete":
        rule.unlink()
    else:
        rule.write_text("unused-b\n")
    with pytest.raises(ResourceFileOperationError) as caught:
        await owner.navigate(request.model_copy(update={"cursor": first.next_cursor}), local_gateway_id=GATEWAY_ID)
    assert caught.value.reason == "stale_cursor"


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("invalid,reason", [(False, "ignore_byte_limit"), (True, "ignore_utf8")])
async def test_rule_bytes_have_one_cumulative_budget_and_strict_encoding(navigation_case, synchronous, invalid, reason):
    owner, workspace, uri = navigation_case
    (workspace / ".gitignore").write_bytes(b"\xff" if invalid else b"unused-a\n")
    (workspace / "nested").mkdir()
    (workspace / "nested/.ignore").write_text("unused-b\n")
    (workspace / "nested/file.txt").write_text("needle")
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle", max_bytes=16,
        search_options={"ignore_mode": "respect", "include_globs": ["*.txt"]})
    with pytest.raises(ResourceFileOperationError) as caught:
        if synchronous:
            owner.navigate_sync(request, local_gateway_id=GATEWAY_ID)
        else:
            await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert caught.value.reason == reason


async def test_rules_read_once_per_directory_and_never_from_relay_or_outside(navigation_case, monkeypatch):
    owner, workspace, uri = navigation_case
    rule = workspace / ".gitignore"
    rule.write_text("ignored/\n")
    for index in range(10):
        (workspace / f"file-{index}.txt").write_text("needle")
    reads = []
    original_open = Path.open

    def observe(path, *args, **kwargs):
        if path.name in {".gitignore", ".ignore"}:
            reads.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    request = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="needle",
        search_options={"ignore_mode": "respect", "include_globs": ["*.txt"]})
    result = await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert len(result.matches) == 10 and reads == [rule]
    rule.unlink()
    rule.symlink_to(workspace.parent / "outside-rules")
    (workspace.parent / "outside-rules").write_text("*.txt\n")
    reads.clear()
    with pytest.raises(ResourceUriResolveError) as caught:
        await owner.navigate(request, local_gateway_id=GATEWAY_ID)
    assert caught.value.reason_code == "capability_denied" and reads == []
