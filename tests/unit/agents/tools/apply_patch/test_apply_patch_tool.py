from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.tools.apply_patch import (
    create_apply_patch_tool,
)
from app.core.model_delta_context import (
    AgentRunLifecycleCallbackHandler,
    model_tool_invocation_scope,
)
from app.core.path_utils import get_workspace_root
from app.core.resource_uri import parse_resource_uri
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.workspace_files import text_mutation, text_publication
from app.services.infrastructure.workspace_files.text_patch.executor import (
    apply_patch_text,
)
from app.services.infrastructure.workspace_files.text_patch.models import (
    DiffError,
    Fuzz,
    InvalidContextError,
)
from app.services.infrastructure.workspace_files.text_patch.parser import (
    identify_files_affected,
)


def _expected_versions(patch: str, root: Path) -> dict[str, str]:
    # 原功能用例在调用前明确观察原文件；冲突用例另传冻结的旧版本。
    versions = {}
    for name in identify_files_affected(patch):
        path = (root / name).resolve()
        versions[name] = "sha256:" + sha256(path.read_bytes()).hexdigest() if path.is_relative_to(root) and path.is_file() else "absent"
    return versions


def _apply(patch: str, *, explanation: str = "单元测试补丁", expected_versions: dict[str, str] | None = None) -> dict[str, object]:
    versions = _expected_versions(patch, get_workspace_root().resolve()) if expected_versions is None else expected_versions
    return apply_patch_text(patch, explanation=explanation, expected_versions=versions)


def test_apply_patch_schema_matches_vscode_required_parameters() -> None:
    tool = create_apply_patch_tool()
    schema = tool.args_schema.model_json_schema()

    assert schema["required"] == ["input", "explanation", "expected_versions"]
    assert "src/main.py" in tool.description
    assert "/absolute/path" not in tool.description
    with pytest.raises(ValidationError):
        tool.invoke({"input": "*** Begin Patch\n*** End Patch"})


@pytest.fixture
async def patch_lifecycle():
    loop = asyncio.get_running_loop()
    starts = []

    class Sink:
        async def wait_tool_start(self, model_call_id, tool_call_id, callback_run_id):
            starts.append((asyncio.get_running_loop(), model_call_id, tool_call_id, callback_run_id))

        def reject_tool_start(self, callback_run_id, error):
            raise AssertionError(f"工具身份交接失败: {callback_run_id}") from error

    return loop, starts, AgentRunLifecycleCallbackHandler(Sink())


async def test_async_patch_lifecycle_stays_on_owning_loop(workspace_scope, patch_lifecycle):
    loop, starts, callback = patch_lifecycle
    tool = create_apply_patch_tool(workspace_root=workspace_scope[1])
    with model_tool_invocation_scope("model_patch", "call_patch"):
        result = await tool.ainvoke({"type": "tool_call", "id": "call_patch", "name": "apply_patch", "args": {
            "input": "*** Begin Patch\n*** Add File: async.txt\n+async\n*** End Patch",
            "explanation": "异步条件发布", "expected_versions": {"async.txt": "absent"},
        }}, config={"callbacks": [callback]})
    assert json.loads(result.content)["effect"] == "completed"
    actual_loop, model_call_id, tool_call_id, run_id = starts[0]
    assert actual_loop is loop and model_call_id == "model_patch" and tool_call_id == "call_patch"
    assert len(starts) == 1 and run_id
    assert (workspace_scope[1] / "async.txt").read_bytes() == b"async"


@pytest.mark.parametrize("blocked", ["ancestor", "existing_parent_file"])
def test_invalid_parent_is_rejected_before_any_publication(workspace_scope, blocked):
    root = workspace_scope[1]
    if blocked == "existing_parent_file":
        (root / "parent").write_bytes(b"existing parent")
        patch = "*** Begin Patch\n*** Add File: first.txt\n+first\n*** Add File: parent/child.txt\n+child\n*** End Patch"
    else:
        patch = "*** Begin Patch\n*** Add File: parent\n+parent\n*** Add File: parent/child.txt\n+child\n*** End Patch"
    with pytest.raises(DiffError, match="父路径|祖先"):
        _apply(patch)
    assert not (root / "first.txt").exists()
    assert not (root / "parent/child.txt").exists()
    assert ((root / "parent").read_bytes() == b"existing parent") if blocked == "existing_parent_file" else not (root / "parent").exists()


def test_apply_patch_add_update_delete_and_records_paths(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]

    result = _apply(
        """*** Begin Patch
*** Add File: src/demo.txt
+one
+old
+three
*** End Patch"""
    )
    assert result["status"] == "success"
    assert (workspace_root / "src" / "demo.txt").read_text(encoding="utf-8") == "one\nold\nthree"

    update_patch = """*** Begin Patch
*** Update File: src/demo.txt
@@
 one
-old
+new
 three
*** End Patch"""
    assert identify_files_affected(update_patch) == ["src/demo.txt"]
    _apply(update_patch)
    assert (workspace_root / "src" / "demo.txt").read_text(encoding="utf-8") == "one\nnew\nthree"

    _apply("""*** Begin Patch
*** Delete File: src/demo.txt
*** End Patch""")
    assert not (workspace_root / "src" / "demo.txt").exists()


def test_apply_patch_uses_multiple_hints_to_target_repeated_code(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    target = workspace_root / "service.py"
    target.write_text(
        """class First:
    def run(self):
        return "same"

class Second:
    def run(self):
        return "same"
""",
        encoding="utf-8",
    )

    _apply(
        """*** Begin Patch
*** Update File: service.py
@@ class Second:
@@     def run(self):
-        return "same"
+        return "changed"
*** End Patch"""
    )

    assert target.read_text(encoding="utf-8") == (
        "class First:\n    def run(self):\n        return \"same\"\n\n"
        "class Second:\n    def run(self):\n        return \"changed\"\n"
    )


def test_apply_patch_matches_unicode_punctuation_and_trailing_whitespace(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    target = workspace_root / "copy.txt"
    target.write_text("title — old   \nnext\n", encoding="utf-8")

    result = _apply(
        """*** Begin Patch
*** Update File: copy.txt
@@
-title - old
+title - new
 next
*** End Patch"""
    )

    assert int(result["fuzz"]) & int(Fuzz.IGNORED_TRAILING_WHITESPACE)
    assert target.read_text(encoding="utf-8") == "title - new\nnext\n"


def test_apply_patch_uses_edit_distance_for_conservative_context_match(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    target = workspace_root / "fuzzy.txt"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")

    result = _apply(
        """*** Begin Patch
*** Update File: fuzzy.txt
@@
 first
-seconx
+changed
 third
*** End Patch"""
    )

    assert int(result["fuzz"]) & int(Fuzz.EDIT_DISTANCE_MATCH)
    assert target.read_text(encoding="utf-8") == "first\nchanged\nthird\n"


def test_apply_patch_normalizes_explicit_tab_prefixes(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    target = workspace_root / "tabs.py"
    target.write_text("def value():\n\treturn 1\n", encoding="utf-8")

    result = _apply(
        """*** Begin Patch
*** Update File: tabs.py
@@
 def value():
-\\treturn 1
+\\treturn 2
*** End Patch"""
    )

    assert int(result["fuzz"]) & int(Fuzz.NORMALIZED_EXPLICIT_TAB)
    assert target.read_text(encoding="utf-8") == "def value():\n\treturn 2\n"


def test_apply_patch_end_of_file_prefers_last_matching_context(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    target = workspace_root / "values.txt"
    target.write_text("item\nitem", encoding="utf-8")

    _apply(
        """*** Begin Patch
*** Update File: values.txt
@@
-item
+last
*** End of File
*** End Patch"""
    )

    assert target.read_text(encoding="utf-8") == "item\nlast"


def test_apply_patch_multi_file_move_overwrites_target(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    (workspace_root / "old.txt").write_text("old\n", encoding="utf-8")
    (workspace_root / "target.txt").write_text("replace me\n", encoding="utf-8")
    (workspace_root / "delete.txt").write_text("delete\n", encoding="utf-8")

    result = _apply(
        """*** Begin Patch
*** Update File: old.txt
*** Move to: target.txt
@@
-old
+moved
*** Delete File: delete.txt
*** Add File: created.txt
+created
*** End Patch"""
    )

    assert not (workspace_root / "old.txt").exists()
    assert not (workspace_root / "delete.txt").exists()
    assert (workspace_root / "target.txt").read_text(encoding="utf-8") == "moved\n"
    assert (workspace_root / "created.txt").read_text(encoding="utf-8") == "created"
    assert [(parse_resource_uri(item["path"]).tail_segments[-1], item["operation"]) for item in result["files"]] == [
        ("target.txt", "move"), ("old.txt", "move"), ("delete.txt", "delete"), ("created.txt", "add"),
    ]
    assert all(item["state"] == "published" for item in result["files"])


def test_apply_patch_invalid_later_file_leaves_all_files_unchanged(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    first = workspace_root / "first.txt"
    second = workspace_root / "second.txt"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")

    with pytest.raises(InvalidContextError):
        _apply(
            """*** Begin Patch
*** Update File: first.txt
@@
-first
+changed
*** Update File: second.txt
@@
-missing
+changed
*** End Patch"""
        )

    assert first.read_text(encoding="utf-8") == "first\n"
    assert second.read_text(encoding="utf-8") == "second\n"


def test_apply_patch_stops_after_partial_publication_without_rollback(workspace_scope, monkeypatch) -> None:
    root = workspace_scope[1]
    original = text_publication.rename_entry_exclusive
    calls = []

    def fail_second(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("simulated publish failure")
        original(source, target)

    monkeypatch.setattr(text_publication, "rename_entry_exclusive", fail_second)
    result = _apply("""*** Begin Patch
*** Add File: first-created.txt
+first
*** Add File: second-created.txt
+second
*** Add File: not-attempted.txt
+third
*** End Patch""")
    assert result["status"] == "error" and result["effect"] == "partial"
    assert [item["state"] for item in result["files"]] == ["published", "not_published", "not_attempted"]
    assert (root / "first-created.txt").read_bytes() == b"first"
    assert not (root / "second-created.txt").exists() and not (root / "not-attempted.txt").exists()
    assert len(calls) == 2 and result["retryable"] is False


def test_apply_patch_accepts_missing_end_marker_like_vscode(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    target = workspace_root / "value.txt"
    target.write_text("before\n", encoding="utf-8")

    _apply("""*** Begin Patch
*** Update File: value.txt
@@
-before
+after""")

    assert target.read_text(encoding="utf-8") == "after\n"


def test_apply_patch_rejects_duplicate_paths_and_escapes_workspace(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    (workspace_root / "same.txt").write_text("same\n", encoding="utf-8")

    with pytest.raises(DiffError, match="Duplicate Path"):
        _apply(
            """*** Begin Patch
*** Update File: same.txt
@@
-same
+one
*** Update File: same.txt
@@
-same
+two
*** End Patch"""
        )

    with pytest.raises(ValueError, match="相对路径无效"):
        _apply(
            """*** Begin Patch
*** Add File: ../outside.txt
+content
*** End Patch"""
        )

    outside_file = (workspace_root.parent / "outside.txt").resolve()
    with pytest.raises(ValueError, match="补丁内文件名必须相对固定根"):
        _apply(
            f"""*** Begin Patch
*** Add File: {outside_file}
+content
*** End Patch"""
        )
    assert not outside_file.exists()


def test_apply_patch_rejects_absolute_internal_filename_even_inside_workspace(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    absolute_inside = (workspace_root / "src" / "abs.txt").resolve()

    with pytest.raises(ValueError, match="补丁内文件名必须相对固定根"):
        _apply(
            f"""*** Begin Patch
*** Add File: {absolute_inside}
+content
*** End Patch"""
        )
    assert not absolute_inside.exists()


def test_apply_patch_tool_returns_json_result(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    tool = create_apply_patch_tool()

    result_text = tool.invoke(
        {
            "input": """*** Begin Patch
*** Add File: tool.txt
+created by tool
*** End Patch""",
            "explanation": "工具调用 smoke",
            "expected_versions": {"tool.txt": "absent"},
        }
    )

    result = json.loads(result_text)
    assert result["status"] == "success"
    journal_id = result["journal_id"]
    assert isinstance(journal_id, str)
    assert journal_id.startswith("text_")
    assert (workspace_root / ".boxteam" / "cache" / "text_mutations" / f"{journal_id}.json").is_file()
    published, = result["files"]
    assert published == {
        "path": result["target"] + "/tool.txt", "operation": "add", "state": "published",
        "expected_version": "absent", "intended_version": "sha256:" + sha256(b"created by tool").hexdigest(),
        "exists": True, "content_hash": "sha256:" + sha256(b"created by tool").hexdigest(),
    }
    assert (workspace_root / "tool.txt").read_text(encoding="utf-8") == "created by tool"
    assert result["effect"] == "completed"
    journal = json.loads((workspace_root / ".boxteam/cache/text_mutations" / f"{journal_id}.json").read_text())
    assert journal["files"] == result["files"]


def test_apply_patch_tool_rejects_success_when_write_does_not_change_bytes(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    (workspace_root / "same.txt").write_text("same\n", encoding="utf-8")
    tool = create_apply_patch_tool()

    result_text = tool.invoke(
        {
            "input": """*** Begin Patch
*** Update File: same.txt
@@
-same
+same
*** End Patch""",
            "explanation": "不应报告无变化补丁成功",
            "expected_versions": {"same.txt": "sha256:" + sha256(b"same\n").hexdigest()},
        }
    )

    result = json.loads(result_text)
    assert result["status"] == "error"
    assert "字节未发生变化" in result["error"]
    assert (workspace_root / "same.txt").read_text(encoding="utf-8") == "same\n"


def test_apply_patch_tool_rejects_success_when_written_bytes_are_not_expected(
    workspace_scope: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = workspace_scope[1]
    target = workspace_root / "target.txt"
    target.write_text("before\n", encoding="utf-8")

    real_replace = text_publication.os.replace

    def write_wrong_content(source, destination):
        real_replace(source, destination)
        target.write_bytes(b"wrong\n")

    monkeypatch.setattr(text_publication.os, "replace", write_wrong_content)
    tool = create_apply_patch_tool()

    result = json.loads(
        tool.invoke(
            {
                "input": """*** Begin Patch
*** Update File: target.txt
@@
-before
+after
*** End Patch""",
                "explanation": "拒绝错位写入结果",
                "expected_versions": {"target.txt": "sha256:" + sha256(b"before\n").hexdigest()},
            }
        )
    )

    assert result["status"] == "error"
    assert "实际效果未知" in result["error"]
    assert result["effect"] == "unknown"
    assert result["files"][0]["state"] == "unknown"
    assert target.read_text(encoding="utf-8") == "wrong\n"
    journal_root = workspace_root / ".boxteam" / "cache" / "text_mutations"
    assert len(list(journal_root.glob("*.json"))) == 1


def test_apply_patch_uses_explicit_workspace_for_write_and_journal(
    workspace_scope: tuple[str, Path],
) -> None:
    context_root = workspace_scope[1]
    explicit_root = context_root / "explicit"
    explicit_root.mkdir()
    load_or_create_workspace_id(explicit_root)
    tool = create_apply_patch_tool(workspace_root=explicit_root)

    result = json.loads(
        tool.invoke(
            {
                "input": """*** Begin Patch
*** Add File: explicit-root-target.txt
+written to explicit workspace
*** End Patch""",
                "explanation": "验证显式工作区路径",
                "expected_versions": {"explicit-root-target.txt": "absent"},
            }
        )
    )

    assert result["status"] == "success"
    assert (explicit_root / "explicit-root-target.txt").read_text(encoding="utf-8") == (
        "written to explicit workspace"
    )
    assert not (context_root / "explicit-root-target.txt").exists()
    explicit_journal = (
        explicit_root
        / ".boxteam"
        / "cache"
        / "text_mutations"
        / f"{result['journal_id']}.json"
    )
    context_journal = (
        context_root
        / ".boxteam"
        / "cache"
        / "text_mutations"
        / f"{result['journal_id']}.json"
    )
    assert explicit_journal.is_file()
    assert not context_journal.exists()


def test_apply_patch_tool_rejects_automatic_retry_for_invalid_patch(
    workspace_scope: tuple[str, Path],
) -> None:
    workspace_root = workspace_scope[1]
    (workspace_root / "same.txt").write_text("same\n", encoding="utf-8")
    tool = create_apply_patch_tool()

    result_text = tool.invoke(
        {
            "input": """*** Begin Patch
*** Update File: same.txt
@@
-same
+one
*** Update File: same.txt
@@
-same
+two
*** End Patch""",
            "explanation": "重复路径重试 smoke",
            "expected_versions": {"same.txt": "sha256:" + sha256(b"same\n").hexdigest()},
        }
    )

    result = json.loads(result_text)
    assert result["status"] == "error"
    assert result["error_type"] == "DiffError"
    assert result["retryable"] is False
    assert "Duplicate Path" in result["error"]
    assert (workspace_root / "same.txt").read_text(encoding="utf-8") == "same\n"


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_stale_source_version_rejects_entire_patch(workspace_scope, operation):
    root = workspace_scope[1]
    source = root / "source.txt"
    original = b"\xef\xbb\xbfold\r\n"
    source.write_bytes(original)
    declaration = "*** Delete File: source.txt" if operation == "delete" else "*** Update File: source.txt\n*** Move to: destination.txt\n@@\n-old\n+new"
    patch = "*** Begin Patch\n*** Add File: untouched.txt\n+must not publish\n" + declaration + "\n*** End Patch"
    versions = _expected_versions(patch, root)
    source.write_bytes(b"external\n")
    with pytest.raises(RuntimeError, match="版本冲突"):
        _apply(patch, expected_versions=versions)
    assert source.read_bytes() == b"external\n"
    assert not (root / "untouched.txt").exists() and not (root / "destination.txt").exists()
    assert not list((root / ".boxteam/cache/text_mutations").glob("*.json"))


def test_move_destination_absence_is_checked_before_any_publication(workspace_scope):
    root = workspace_scope[1]
    (root / "source.txt").write_bytes(b"old\n")
    (root / "destination.txt").write_bytes(b"do not overwrite\n")
    patch = "*** Begin Patch\n*** Update File: source.txt\n*** Move to: destination.txt\n@@\n-old\n+new\n*** End Patch"
    versions = _expected_versions(patch, root)
    versions["destination.txt"] = "absent"
    with pytest.raises(RuntimeError, match="版本冲突"):
        _apply(patch, expected_versions=versions)
    assert (root / "source.txt").read_bytes() == b"old\n"
    assert (root / "destination.txt").read_bytes() == b"do not overwrite\n"


def test_move_source_delete_failure_preserves_published_destination(workspace_scope, monkeypatch):
    root = workspace_scope[1]
    source = root / "source.txt"
    source.write_bytes(b"old\n")
    original = Path.unlink

    def reject_source_delete(path, *args, **kwargs):
        if path == source:
            raise PermissionError("source delete refused")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_source_delete)
    result = _apply("*** Begin Patch\n*** Update File: source.txt\n*** Move to: destination.txt\n@@\n-old\n+new\n*** Add File: untouched.txt\n+later\n*** End Patch")
    assert result["status"] == "error" and result["effect"] == "partial"
    assert [item["state"] for item in result["files"]] == ["published", "not_published", "not_attempted"]
    assert source.read_bytes() == b"old\n" and (root / "destination.txt").read_bytes() == b"new\n"
    assert not (root / "untouched.txt").exists()


def test_expected_absent_race_uses_exclusive_publish_without_overwrite(workspace_scope, monkeypatch):
    root = workspace_scope[1]
    original = text_publication.rename_entry_exclusive

    def race(source, destination):
        destination.write_bytes(b"external creator\n")
        original(source, destination)

    monkeypatch.setattr(text_publication, "rename_entry_exclusive", race)
    result = _apply("*** Begin Patch\n*** Add File: race.txt\n+draft\n*** End Patch")
    assert result["status"] == "error" and result["effect"] == "unknown"
    assert result["files"][0]["state"] == "unknown" and result["retryable"] is False
    assert (root / "race.txt").read_bytes() == b"external creator\n"
    assert not list(root.glob(".race.txt.*.tmp"))


def test_exclusive_create_race_with_identical_bytes_does_not_claim_publication(workspace_scope, monkeypatch):
    original = text_publication.rename_entry_exclusive

    def race(source, destination):
        destination.write_bytes(b"draft")
        original(source, destination)

    monkeypatch.setattr(text_publication, "rename_entry_exclusive", race)
    result = _apply("*** Begin Patch\n*** Add File: race.txt\n+draft\n*** End Patch")
    assert result["status"] == "error" and result["effect"] == "unknown"
    assert result["files"][0]["state"] == "unknown"
    assert (workspace_scope[1] / "race.txt").read_bytes() == b"draft"


@pytest.mark.parametrize("after_verification", [False, True])
def test_external_restore_after_publication_is_unknown_not_unexecuted(workspace_scope, monkeypatch, after_verification):
    target = workspace_scope[1] / "restored.txt"
    target.write_bytes(b"old\n")
    if after_verification:
        publish = text_mutation.publish_file_content

        def restore(path, *args, **kwargs):
            result = publish(path, *args, **kwargs)
            path.write_bytes(b"old\n")
            return result

        monkeypatch.setattr(text_mutation, "publish_file_content", restore)
    else:
        replace = text_publication.os.replace

        def restore(source, destination):
            replace(source, destination)
            destination.write_bytes(b"old\n")

        monkeypatch.setattr(text_publication.os, "replace", restore)
    result = _apply("*** Begin Patch\n*** Update File: restored.txt\n@@\n-old\n+new\n*** End Patch")
    assert result["status"] == "error" and result["effect"] == "unknown"
    assert result["files"][0]["state"] == "unknown"
    assert target.read_bytes() == b"old\n" and result["retryable"] is False


@pytest.mark.parametrize("input_form", ["relative", "absolute", "uri"])
def test_patch_root_inputs_share_owner_and_preserve_raw_crlf_version(workspace_scope, input_form):
    root = workspace_scope[1]
    directory = root / "nested"
    directory.mkdir()
    raw = b"\xef\xbb\xbfalpha\r\nbeta\r\n"
    target = directory / "file.txt"
    target.write_bytes(raw)
    from app.core.resource_uri import resource_display_uri
    from app.core.workspace_identity import load_or_create_workspace_id
    uri = resource_display_uri(scope="workspace", scope_id=load_or_create_workspace_id(root), kind="files", tail_segments=("root", "nested"))
    address = {"relative": "nested", "absolute": str(directory), "uri": uri}[input_form]
    patch = "*** Begin Patch\n*** Update File: file.txt\n@@\n-beta\n+gamma\n*** End Patch"
    tool = create_apply_patch_tool(workspace_root=root)
    wrong = json.loads(tool.invoke({"input": patch, "explanation": "拒绝显示文本版本", "target": address,
        "expected_versions": {"file.txt": "sha256:" + sha256(raw.replace(b"\r\n", b"\n")).hexdigest()}}))
    assert wrong["code"] == "revision_conflict" and wrong["effect"] == "not_executed"
    result = json.loads(tool.invoke({"input": patch, "explanation": "原字节条件更新", "target": address,
        "expected_versions": {"file.txt": "sha256:" + sha256(raw).hexdigest()}}))
    assert result["status"] == "success" and result["target"] == uri
    expected = b"\xef\xbb\xbfalpha\r\ngamma\r\n"
    assert target.read_bytes() == expected
    assert result["files"][0]["content_hash"] == "sha256:" + sha256(expected).hexdigest()
    assert str(root) not in json.dumps(result)


def test_patch_rejects_missing_move_destination_precondition(workspace_scope):
    root = workspace_scope[1]
    (root / "source.txt").write_bytes(b"old\n")
    with pytest.raises(DiffError, match="覆盖全部"):
        _apply("*** Begin Patch\n*** Update File: source.txt\n*** Move to: target.txt\n@@\n-old\n+new\n*** End Patch",
               expected_versions={"source.txt": "sha256:" + sha256(b"old\n").hexdigest()})
    assert (root / "source.txt").read_bytes() == b"old\n" and not (root / "target.txt").exists()


def test_lock_release_failure_does_not_erase_confirmed_publication(workspace_scope, monkeypatch):
    from contextlib import contextmanager

    original = text_mutation.workspace_file_mutation_lock

    @contextmanager
    def failed_release(root):
        with original(root):
            yield
        raise OSError("lock release failed after publication")

    monkeypatch.setattr(text_mutation, "workspace_file_mutation_lock", failed_release)
    result = _apply("*** Begin Patch\n*** Add File: confirmed.txt\n+published\n*** End Patch")
    assert result["status"] == "error" and result["effect"] == "completed"
    assert result["files"][0]["state"] == "published"
    assert "lock release failed" in result["lock_release_error"]
    assert (workspace_scope[1] / "confirmed.txt").read_bytes() == b"published"


@pytest.mark.parametrize("target", [".boxteam", "boxteam://workspace/bad/resources/files/root", "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/skills/demo"])
def test_patch_invalid_or_private_root_fails_before_publication(workspace_scope, target):
    root = workspace_scope[1]
    tool = create_apply_patch_tool(workspace_root=root)
    result = json.loads(tool.invoke({"input": "*** Begin Patch\n*** Add File: forbidden.txt\n+private\n*** End Patch", "explanation": "边界拒绝",
                                    "target": target, "expected_versions": {"forbidden.txt": "absent"}}))
    assert result["status"] == "error" and result["effect"] == "not_executed"
    assert not (root / "forbidden.txt").exists() and not (root / ".boxteam/forbidden.txt").exists()
    if target.startswith("boxteam:"):
        assert result["owner"] in {"resource_uri_grammar", "resource_uri_resolver"}
