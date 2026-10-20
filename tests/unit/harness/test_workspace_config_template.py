from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import workspaces


def test_render_preserves_config_values_and_normalizes_local_schema() -> None:
    source = '''{
        // 测试配置可以包含注释、中文与嵌套值。
        "$schema": "../../workspace_schema.jsonc",
        "config_version": 0,
        "model": {"name": "测试模型", "options": [1, 2]}
    }'''

    rendered = workspaces.render_test_workspace_config(source)

    assert json.loads(rendered) == {
        "$schema": "./workspace_schema.jsonc",
        "config_version": 1,
        "model": {"name": "测试模型", "options": [1, 2]},
    }
    assert "测试模型" in rendered
    assert rendered.endswith("\n")


def test_unchanged_source_is_parsed_once_and_changed_source_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse = workspaces.commentjson.loads
    parsed_sources: list[str] = []

    def observe_parse(source: str) -> object:
        parsed_sources.append(source)
        return parse(source)

    monkeypatch.setattr(workspaces.commentjson, "loads", observe_parse)
    original = '{"cache_probe": "original"}'
    changed = '{"cache_probe": "changed"}'

    first = workspaces.render_test_workspace_config(original)
    assert workspaces.render_test_workspace_config(original) == first
    updated = workspaces.render_test_workspace_config(changed)

    assert parsed_sources == [original, changed]
    assert json.loads(first)["cache_probe"] == "original"
    assert json.loads(updated)["cache_probe"] == "changed"


@pytest.mark.parametrize("case", ["first", "second"])
def test_autouse_config_remains_private_to_each_test(
    test_config_path: str,
    case: str,
) -> None:
    installed = Path(os.environ["BOXTEAM_HOME"]) / "config" / "workspace.jsonc"
    expected = workspaces.render_test_workspace_config(
        Path(test_config_path).read_text(encoding="utf-8"),
    )
    assert installed.read_text(encoding="utf-8") == expected

    installed.write_text(json.dumps({"mutated_by": case}), encoding="utf-8")
    parsed = json.loads(expected)
    parsed["mutated_by"] = case

    assert workspaces.render_test_workspace_config(
        Path(test_config_path).read_text(encoding="utf-8"),
    ) == expected
    assert "mutated_by" not in json.loads(expected)
