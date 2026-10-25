from __future__ import annotations

from pathlib import Path

import pytest

from app.tool_testing.store import ToolTestStore


def test_tool_test_store_lists_readable_run_files(tmp_path: Path) -> None:
    store = ToolTestStore(root=tmp_path / "tool_tests")
    store.reset_tool("apply_patch")
    store.write_run(
        "run_a", {"run_id": "run_a", "tool_name": "apply_patch", "created_at": "2"}
    )
    store.reset_tool("edit_file")
    store.write_run(
        "run_b", {"run_id": "run_b", "tool_name": "edit_file", "created_at": "1"}
    )

    assert [item["run_id"] for item in store.list_runs(tool_name="apply_patch")] == [
        "run_a"
    ]

    store.reset_tool("apply_patch")
    store.write_run(
        "run_c", {"run_id": "run_c", "tool_name": "apply_patch", "created_at": "3"}
    )
    with pytest.raises(FileNotFoundError, match="已被新测试覆盖"):
        store.read_run("run_a")
    assert [item["run_id"] for item in store.list_runs(tool_name="apply_patch")] == [
        "run_c"
    ]
