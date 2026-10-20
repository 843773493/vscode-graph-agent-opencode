from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.tools.apply_patch import (
    DiffError,
    Fuzz,
    InvalidContextError,
    apply_patch_text,
    create_apply_patch_tool,
    extract_apply_patch_file_paths,
)
from app.agents.tools.apply_patch import executor as apply_patch_executor


def _apply(patch: str, *, explanation: str = "单元测试补丁") -> dict[str, object]:
    return apply_patch_text(patch, explanation=explanation)


def test_apply_patch_schema_matches_vscode_required_parameters() -> None:
    tool = create_apply_patch_tool()
    schema = tool.args_schema.model_json_schema()

    assert schema["required"] == ["input", "explanation"]
    assert "src/main.py" in tool.description
    assert "/absolute/path" not in tool.description
    with pytest.raises(ValidationError):
        tool.invoke({"input": "*** Begin Patch\n*** End Patch"})


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
    assert extract_apply_patch_file_paths(update_patch) == ["src/demo.txt"]
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
    assert result["files"] == [
        {"path": "old.txt", "operation": "move"},
        {"path": "target.txt", "operation": "move"},
        {"path": "delete.txt", "operation": "delete"},
        {"path": "created.txt", "operation": "add"},
    ]


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


def test_apply_patch_rolls_back_when_filesystem_write_fails_mid_commit(
    workspace_scope: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = workspace_scope[1]
    real_replace = apply_patch_executor.os.replace
    replace_calls = 0

    def fail_second_replace(source: str | Path, target: str | Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated replace failure")
        real_replace(source, target)

    monkeypatch.setattr(apply_patch_executor.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        _apply(
            """*** Begin Patch
*** Add File: first-created.txt
+first
*** Add File: second-created.txt
+second
*** End Patch"""
        )

    assert not (workspace_root / "first-created.txt").exists()
    assert not (workspace_root / "second-created.txt").exists()


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

    with pytest.raises(ValueError, match="apply_patch 文件路径无效"):
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
        }
    )

    result = json.loads(result_text)
    assert result["status"] == "success"
    journal_id = result["journal_id"]
    assert isinstance(journal_id, str)
    assert journal_id.startswith("patch_")
    assert (workspace_root / ".boxteam" / "cache" / "apply_patch" / f"{journal_id}.json").is_file()
    assert result["files"] == [{"path": "tool.txt", "operation": "add"}]
    assert (workspace_root / "tool.txt").read_text(encoding="utf-8") == "created by tool"
    assert result["verification"] == [{
        "path": "/tool.txt",
        "exists": True,
        "size_bytes": len("created by tool"),
        "sha256": sha256(b"created by tool").hexdigest(),
    }]
    journal = json.loads(
        (workspace_root / ".boxteam" / "cache" / "apply_patch" / f"{journal_id}.json")
        .read_text(encoding="utf-8")
    )
    assert journal["verification"] == result["verification"]


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

    def write_wrong_content(commit, snapshots, workspace_root):
        del commit, snapshots
        (workspace_root / "target.txt").write_text("wrong\n", encoding="utf-8")
        return []

    monkeypatch.setattr(
        apply_patch_executor,
        "_apply_commit_transactionally",
        write_wrong_content,
    )
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
            }
        )
    )

    assert result["status"] == "error"
    assert "写入校验失败" in result["error"]
    assert target.read_text(encoding="utf-8") == "before\n"
    journal_root = workspace_root / ".boxteam" / "cache" / "apply_patch"
    assert not list(journal_root.glob("*.json"))


def test_apply_patch_uses_explicit_workspace_for_write_and_journal(
    workspace_scope: tuple[str, Path],
) -> None:
    context_root = workspace_scope[1]
    explicit_root = context_root / "explicit"
    explicit_root.mkdir()
    tool = create_apply_patch_tool(workspace_root=explicit_root)

    result = json.loads(
        tool.invoke(
            {
                "input": """*** Begin Patch
*** Add File: explicit-root-target.txt
+written to explicit workspace
*** End Patch""",
                "explanation": "验证显式工作区路径",
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
        / "apply_patch"
        / f"{result['journal_id']}.json"
    )
    context_journal = (
        context_root
        / ".boxteam"
        / "cache"
        / "apply_patch"
        / f"{result['journal_id']}.json"
    )
    assert explicit_journal.is_file()
    assert not context_journal.exists()


def test_apply_patch_tool_returns_retryable_error_for_invalid_patch(
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
        }
    )

    result = json.loads(result_text)
    assert result["status"] == "error"
    assert result["error_type"] == "DiffError"
    assert result["retryable"] is True
    assert "Duplicate Path" in result["error"]
    assert (workspace_root / "same.txt").read_text(encoding="utf-8") == "same\n"
