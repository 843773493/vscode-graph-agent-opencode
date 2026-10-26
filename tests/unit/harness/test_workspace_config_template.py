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
        "config_version": 2,
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


@pytest.mark.parametrize("directory_mode", [0o500, 0o000, 0o700])
@pytest.mark.parametrize("full_copy", [False, True])
def test_prepare_replaces_readonly_tree_without_changing_template(tmp_path: Path, directory_mode: int, full_copy: bool) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / "new.txt").write_bytes(b"new fixture")
    (template / "new.txt").chmod(0o400)
    workspace = tmp_path / "workspace"
    original = workspace / "nested/original"
    original.mkdir(parents=True)
    source = original / "content"
    source.write_bytes(b"retained upload")
    source.chmod(0o400)
    original.chmod(directory_mode)
    try:
        if full_copy:
            workspaces.prepare_default_test_workspace(workspace_root=workspace, template_root=template)
        else:
            workspaces.prepare_test_workspace(workspace_root=workspace, template_root=template, template_items=("new.txt",))
        assert sorted(path.name for path in workspace.iterdir()) == ["new.txt"]
        assert (workspace / "new.txt").read_bytes() == b"new fixture"
        assert (template / "new.txt").read_bytes() == b"new fixture"
        assert (template / "new.txt").stat().st_mode & 0o200 == 0
    finally:
        # 红测也保留可清理产物，不把前次失败转成后续 fixture 准备失败。
        if original.exists():
            original.chmod(0o700)
            source.chmod(0o600)


@pytest.mark.parametrize("linked_directory", [False, True])
def test_prepare_does_not_follow_external_symlinks(tmp_path: Path, linked_directory: bool) -> None:
    external = tmp_path / "external"
    external.mkdir()
    content = external / "content"
    content.write_bytes(b"outside workspace")
    content.chmod(0o400)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "outside").symlink_to(external if linked_directory else content, target_is_directory=linked_directory)
    template = tmp_path / "template"
    template.mkdir()
    before = content.stat().st_mode
    workspaces.prepare_test_workspace(workspace_root=workspace, template_root=template, template_items=())
    assert list(workspace.iterdir()) == []
    assert content.read_bytes() == b"outside workspace"
    assert content.stat().st_mode == before


def test_prepare_does_not_hide_real_removal_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "blocked").write_bytes(b"existing output")
    template = tmp_path / "template"
    template.mkdir()
    original_unlink = os.unlink
    failure = OSError(5, "injected I/O error")

    def fail_unlink(path, *args, **kwargs):
        if str(path).endswith("blocked"):
            raise failure
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_unlink)
    with pytest.raises(OSError) as caught:
        workspaces.prepare_test_workspace(workspace_root=workspace, template_root=template, template_items=("new.txt",))
    assert caught.value is failure
    assert (workspace / "blocked").read_bytes() == b"existing output"
