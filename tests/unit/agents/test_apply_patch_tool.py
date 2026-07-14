from __future__ import annotations

import json
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    result = _apply(
        """*** Begin Patch
*** Add File: src/demo.txt
+one
+old
+three
*** End Patch"""
    )
    assert result["status"] == "success"
    assert (tmp_path / "src" / "demo.txt").read_text(encoding="utf-8") == "one\nold\nthree"

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
    assert (tmp_path / "src" / "demo.txt").read_text(encoding="utf-8") == "one\nnew\nthree"

    _apply("""*** Begin Patch
*** Delete File: src/demo.txt
*** End Patch""")
    assert not (tmp_path / "src" / "demo.txt").exists()


def test_apply_patch_uses_multiple_hints_to_target_repeated_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "service.py"
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "copy.txt"
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "fuzzy.txt"
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "tabs.py"
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "values.txt"
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "old.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "target.txt").write_text("replace me\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("delete\n", encoding="utf-8")

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

    assert not (tmp_path / "old.txt").exists()
    assert not (tmp_path / "delete.txt").exists()
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "moved\n"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created"
    assert result["files"] == [
        {"path": "old.txt", "operation": "move"},
        {"path": "target.txt", "operation": "move"},
        {"path": "delete.txt", "operation": "delete"},
        {"path": "created.txt", "operation": "add"},
    ]


def test_apply_patch_invalid_later_file_leaves_all_files_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
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

    assert not (tmp_path / "first-created.txt").exists()
    assert not (tmp_path / "second-created.txt").exists()


def test_apply_patch_accepts_missing_end_marker_like_vscode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")

    _apply("""*** Begin Patch
*** Update File: value.txt
@@
-before
+after""")

    assert target.read_text(encoding="utf-8") == "after\n"


def test_apply_patch_rejects_duplicate_and_absolute_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "same.txt").write_text("same\n", encoding="utf-8")

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
    with pytest.raises(ValueError, match="必须是工作区相对路径"):
        _apply(
            """*** Begin Patch
*** Add File: /absolute.txt
+content
*** End Patch"""
        )

    with pytest.raises(ValueError, match="文件路径超出工作区"):
        _apply(
            """*** Begin Patch
*** Add File: ../outside.txt
+content
*** End Patch"""
        )


def test_apply_patch_tool_returns_json_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
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
    assert result["files"] == [{"path": "tool.txt", "operation": "add"}]
    assert (tmp_path / "tool.txt").read_text(encoding="utf-8") == "created by tool"
