from pathlib import Path

from tests.support.custom_tool_test_workspace import custom_tool_workspace_template_items


def test_template_copy_omits_generated_fixture_tree(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("workspace rules", encoding="utf-8")
    (tmp_path / "rollout-fixture.json").write_text("{}", encoding="utf-8")
    (tmp_path / "fixture").mkdir()
    (tmp_path / "fixture" / "0001.json").write_text("stale fixture", encoding="utf-8")
    (tmp_path / ".boxteam").mkdir()
    (tmp_path / ".boxteam" / "AGENTS.md").write_text("boxteam rules", encoding="utf-8")
    (tmp_path / ".boxteam" / "skills").mkdir()

    assert custom_tool_workspace_template_items(tmp_path) == (
        ".boxteam/AGENTS.md",
        ".boxteam/skills",
        "AGENTS.md",
        "rollout-fixture.json",
    )
