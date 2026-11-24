from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services.infrastructure.config_service import ConfigService
from configs.installer import install_user_configuration
from configs.runtime import validate_config_document_payload
from tests.support import workspaces


def test_render_preserves_config_values_and_normalizes_local_schema() -> None:
    source = '''{
        // 测试配置可以包含注释、中文与嵌套值。
        "$schema": "../../workspace_schema.jsonc",
        "config_version": 4,
        "settings": {"model_options": {"name": "测试模型", "options": [1, 2]}}
    }'''

    rendered = workspaces.render_test_workspace_config(source)

    assert json.loads(rendered) == {
        "$schema": "./workspace_schema.jsonc",
        "config_version": 4,
        "settings": {"model_options": {"name": "测试模型", "options": [1, 2]}},
    }
    assert "测试模型" in rendered
    assert rendered.endswith("\n")


@pytest.mark.parametrize("endpoint", [None, "http://127.0.0.1:1/v1"])
def test_isolated_provider_keys_pass_schema_and_resolve_without_secrets(
    endpoint: str | None, workspace_config_schema: dict, tmp_path: Path,
) -> None:
    config = json.loads(workspaces.render_test_workspace_config(
        Path("configs/workspace_inline.jsonc").read_text(encoding="utf-8"),
    ))
    config["settings"]["workspace"]["llm"]["providers"]["primary"]["models"]["default"]["config_ref"] = "#/profiles/private-model"
    config["profiles"]["private-model"] = {"extends": "context:provider/primary", "settings": {
        "model_options": {"api_key": "${UNRESOLVED_TEST_MODEL_OVERRIDE}"},
    }}
    original_profiles = json.loads(json.dumps(config["profiles"]))
    workspaces.clear_test_provider_api_keys(config, endpoint=endpoint)
    validate_config_document_payload(config, schema=workspace_config_schema)
    install_user_configuration(config_root=tmp_path, profile="default")
    source = tmp_path / "workspace.jsonc"
    source.write_text(json.dumps(config), encoding="utf-8")
    service = ConfigService(config_dir=tmp_path, config_path=source)
    service.validate_workspace_config()
    with service.use_llm_credentials({}):
        models = service.list_llm_models()
    assert models and all(model["api_key"] == "" for model in models)
    for model in models:
        original = original_profiles[f"provider.{model['provider_id']}"]["settings"]["model_options"]
        assert model["endpoint"] == (endpoint or original["endpoint"])
        for key, value in original["api_mode"].items():
            if key == "model_info":
                assert model["api_mode"][key].items() >= value.items()
            else:
                assert model["api_mode"][key] == value
        assert model["api_mode"]["streaming"] is True
        assert model.get("auth") == original.get("auth")


def test_unchanged_source_is_parsed_once_and_changed_source_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse = workspaces.commentjson.loads
    parsed_sources: list[str] = []

    def observe_parse(source: str) -> object:
        parsed_sources.append(source)
        return parse(source)

    monkeypatch.setattr(workspaces.commentjson, "loads", observe_parse)
    original = '{"config_version":4,"settings":{"cache_probe":"original"}}'
    changed = '{"config_version":4,"settings":{"cache_probe":"changed"}}'

    first = workspaces.render_test_workspace_config(original)
    assert workspaces.render_test_workspace_config(original) == first
    updated = workspaces.render_test_workspace_config(changed)

    assert parsed_sources == [original, changed]
    assert json.loads(first)["settings"]["cache_probe"] == "original"
    assert json.loads(updated)["settings"]["cache_probe"] == "changed"


@pytest.mark.parametrize("source", ['{}', '{"config_version":3,"llm":{}}', '{"config_version":4}'])
def test_render_rejects_old_or_incomplete_documents(source: str) -> None:
    with pytest.raises(ValueError, match="v4"):
        workspaces.render_test_workspace_config(source)


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


def test_writable_walk_skips_junction_before_chmod_or_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    junction = tmp_path / "junction"
    junction.mkdir()
    original = junction / "original"
    original.write_bytes(b"outside workspace")
    original.chmod(0o400)
    before = original.stat().st_mode
    monkeypatch.setattr(Path, "is_junction", lambda path: path == junction)

    workspaces._make_test_tree_writable(junction)

    assert original.stat().st_mode == before
    assert original.read_bytes() == b"outside workspace"
