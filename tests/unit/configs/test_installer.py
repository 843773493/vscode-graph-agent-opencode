"""验证双域静态配置资源及安装器。"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path
from unittest.mock import Mock

import commentjson
import jsonschema
import pytest

from configs import installer
from configs.boxteam import (
    SSH_BLOCK_BEGIN,
    SSH_BLOCK_END,
    SSH_KEY_NAME,
    SSH_KNOWN_HOSTS_NAME,
    install_gateway_development_assets,
    install_source_development_configuration,
    install_user_configuration,
)
from configs.installer import install_bundled_plugins, validate_config_resource_pair


def _load_jsonc_object(path: Path) -> dict[str, object]:
    payload = commentjson.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _legacy_aggregate_config() -> dict[str, object]:
    return {
        "config_version": 4,
        "gateway": {"workspaces": []},
        "llm": {"providers": {"primary": {
            "defaults": {"endpoint": "https://example.invalid/v1", "custom_llm_provider": "openai", "api_key": ""},
            "models": {"default": {"model": "example-model"}},
        }}},
        "default_agent": "default",
        "agents": {"default": {
            "name": "Default", "instructions": {"system_prompt": "Test agent."},
            "model": {"primary": {"provider_id": "primary", "model_id": "default"}},
        }},
    }


def test_bundled_plugin_install_is_idempotent_and_does_not_change_switches(tmp_path: Path) -> None:
    archive = tmp_path / "builtin.zip"
    manifest = {"manifest_version": 1, "package_id": "example.builtin", "version": "1.0.0",
                "name": "Builtin", "extension_contract_version": 1,
                "plugins": [{"id": "guide", "name": "Guide", "instance_scope": "workspace",
                    "requires": [{"extension_point": "skills", "version": 1}],
                    "contributes": {"skills": [{"id": "guide", "path": "guide"}]}}]}
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("plugin.jsonc", json.dumps(manifest))
        output.writestr("guide/SKILL.md", "---\nname: guide\ndescription: Test guidance\n---\nUse guide.\n")
    home = tmp_path / "home"
    entries = [{"package_id": "example.builtin", "package_version": "1.0.0", "archive": str(archive)}]
    assert install_bundled_plugins(boxteam_home=home, packages=entries) == ["example.builtin@1.0.0"]
    config = home / "config/gateway.jsonc"
    config.parent.mkdir()
    switches = '{"config_version":6,"extends":"builtin:gateway","settings":{"gateway":{"plugins":{"workspace_defaults":{"entries":[]}}}}}\n'
    config.write_text(switches)
    # 重复核实同一来源仍幂等，也不把空模板重新变成启用。
    assert install_bundled_plugins(boxteam_home=home, packages=entries) == []
    assert config.read_text() == switches


def test_development_restart_preserves_plugin_choices_and_environment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("NEW_VALUE=1\n")
    shutil.copytree(Path.cwd() / "configs", project / "configs", ignore=shutil.ignore_patterns("__pycache__"))
    config_root = tmp_path / "home/config"
    install_source_development_configuration(project_root=project, config_root=config_root)
    document = _load_jsonc_object(config_root / "gateway.jsonc")
    document["settings"].setdefault("gateway", {}).setdefault("plugins", {})["workspace_defaults"] = {"entries": []}
    saved = json.dumps(document)
    (config_root / "gateway.jsonc").write_text(saved)
    (config_root / ".env").write_text("USER_VALUE=2\n")
    install_source_development_configuration(project_root=project, config_root=config_root)
    assert (config_root / "gateway.jsonc").read_text() == saved
    assert (config_root / ".env").read_text() == "USER_VALUE=2\n"


def test_first_install_uses_bundled_version_without_rewriting_existing_defaults(tmp_path: Path) -> None:
    config_root = tmp_path / "home/config"
    bundled = [{"package_id": "boxteam.developer-tools", "package_version": "0.1.0-dev.actual-content"}]
    install_user_configuration(config_root=config_root, profile="default", bundled_packages=bundled)
    document = _load_jsonc_object(config_root / "gateway.jsonc")
    entries = document["settings"]["gateway"]["plugins"]["workspace_defaults"]["entries"]
    assert entries and all(entry["package_version"] == bundled[0]["package_version"] for entry in entries)
    entries[0]["enabled"] = False
    saved = json.dumps(document)
    (config_root / "gateway.jsonc").write_text(saved)
    bundled[0]["package_version"] = "0.1.0-dev.other-content"
    install_user_configuration(config_root=config_root, profile="default", bundled_packages=bundled)
    assert (config_root / "gateway.jsonc").read_text() == saved


def _default_custom_tool_names(config: dict[str, object]) -> set[str]:
    agents = config["agents"]
    assert isinstance(agents, dict)
    default_agent = agents["default"]
    assert isinstance(default_agent, dict)
    tools = default_agent["tools"]
    assert isinstance(tools, dict)
    custom = tools["custom"]
    assert isinstance(custom, list)
    return {
        str(spec.get("name") or spec.get("tool_id"))
        for spec in custom
        if isinstance(spec, dict)
    }


@pytest.mark.parametrize("domain", ["gateway", "workspace"])
@pytest.mark.parametrize("suffix", ["_inline", "_dev"])
def test_static_configuration_matches_domain_schema(
    domain: str,
    suffix: str,
    static_schema_validators: dict[str, jsonschema.Draft202012Validator],
) -> None:
    config = _load_jsonc_object(Path(f"configs/{domain}{suffix}.jsonc"))
    static_schema_validators[domain].validate(config)


def test_config_resource_pair_rejects_invalid_configuration(tmp_path: Path) -> None:
    schema_source = tmp_path / "config-schema.json"
    config_source = tmp_path / "config.json"
    schema_source.write_text(
        '{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        '"type":"object","required":["enabled"],'
        '"properties":{"enabled":{"type":"boolean"}}}',
        encoding="utf-8",
    )
    config_source.write_text('{"enabled":"true"}', encoding="utf-8")

    with pytest.raises(jsonschema.ValidationError):
        validate_config_resource_pair(config_source, schema_source)


@pytest.fixture(scope="module")
def static_schema_validators() -> dict[str, jsonschema.Draft202012Validator]:
    validators: dict[str, jsonschema.Draft202012Validator] = {}
    for domain in ("gateway", "workspace"):
        schema = _load_jsonc_object(Path(f"configs/{domain}_schema.jsonc"))
        jsonschema.Draft202012Validator.check_schema(schema)
        validators[domain] = jsonschema.Draft202012Validator(schema)
    return validators


def test_development_templates_enable_development_capabilities_and_docker_gateway() -> (
    None
):
    gateway = _load_jsonc_object(Path("configs/gateway_inline.jsonc"))
    gateway_dev = _load_jsonc_object(Path("configs/gateway_dev.jsonc"))
    workspace = _load_jsonc_object(Path("configs/workspace_inline.jsonc"))
    workspace_dev = _load_jsonc_object(Path("configs/workspace_dev.jsonc"))
    gateway = gateway["settings"]["gateway"]
    gateway_dev = gateway_dev["settings"]["gateway"]
    workspace = workspace["settings"]["workspace"]
    workspace_dev = workspace_dev["settings"]["workspace"]

    assert gateway["workspaces"] == []
    assert gateway_dev["workspaces"][0]["enabled"] is True  # type: ignore[index]
    assert (
        gateway_dev["workspaces"][0]["ssh_config_host"]  # type: ignore[index]
        == "boxteam-container"
    )
    assert workspace["development"] == {"test_tools": False}
    assert workspace_dev["development"] == {"test_tools": True}
    assert workspace["mcp"] == {"supervision": {"sealed_call_timeout_seconds": 1800}, "servers": {}}
    assert "mcp" in workspace_dev
    assert workspace_dev["runtime"]["debug"] == workspace["runtime"]["debug"]  # type: ignore[index]
    debugging_tools = {
        "start_debugging",
        "stop_debugging",
        "step_over",
        "step_into",
        "step_out",
        "continue_execution",
        "pause_execution",
        "restart_debugging",
        "add_breakpoint",
        "add_logpoint",
        "remove_breakpoint",
        "clear_all_breakpoints",
        "list_breakpoints",
        "list_variable_names",
        "get_variables_values",
        "evaluate_expression",
    }
    assert debugging_tools <= _default_custom_tool_names(workspace)
    assert debugging_tools <= _default_custom_tool_names(workspace_dev)


def test_initialize_user_configuration_only_creates_missing_configs(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    existing = config_root / "gateway.jsonc"
    existing.write_text('{"owned_by_user": true}\n', encoding="utf-8")

    result = install_user_configuration(
        config_root=config_root,
        profile="default",
        project_root=Path.cwd(),
    )

    assert existing.read_text(encoding="utf-8") == '{"owned_by_user": true}\n'
    assert result.created_config_paths == (
        config_root / "gateway_local.jsonc", config_root / "workspace.jsonc", config_root / "workspace_local.jsonc",
    )
    assert (config_root / "gateway_schema.jsonc").is_file()
    assert (config_root / "workspace_schema.jsonc").is_file()


def test_force_rebuild_replaces_both_user_configs(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    for name in ("gateway.jsonc", "workspace.jsonc"):
        (config_root / name).write_text('{"stale": true}\n', encoding="utf-8")

    result = install_user_configuration(
        config_root=config_root,
        profile="default",
        project_root=Path.cwd(),
        force=True,
    )

    assert set(result.created_config_paths) == {
        config_root / "gateway.jsonc",
        config_root / "gateway_local.jsonc",
        config_root / "workspace.jsonc",
        config_root / "workspace_local.jsonc",
    }
    assert "stale" not in _load_jsonc_object(config_root / "gateway.jsonc")
    assert "stale" not in _load_jsonc_object(config_root / "workspace.jsonc")


def test_repeated_install_does_not_revalidate_or_rewrite_unchanged_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    first = install_user_configuration(
        config_root=config_root, profile="default", project_root=Path.cwd(),
    )
    validate = Mock(wraps=installer.validate_config_resource_pair)
    write = Mock(wraps=installer.atomic_write)
    monkeypatch.setattr(installer, "validate_config_resource_pair", validate)
    monkeypatch.setattr(installer, "atomic_write", write)

    second = install_user_configuration(
        config_root=config_root, profile="default", project_root=Path.cwd(),
    )

    assert second.config_paths == first.config_paths
    assert second.schema_paths == first.schema_paths
    assert second.created_config_paths == ()
    validate.assert_not_called()
    write.assert_not_called()


def test_explicit_workspace_initialization_writes_only_its_default_parent_chain(tmp_path: Path) -> None:
    config_root = tmp_path / "home/config"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "unselected-workspace"
    other.mkdir()
    install_user_configuration(config_root=config_root, profile="default", workspace_root=workspace)
    assert _load_jsonc_object(config_root / "workspace.jsonc") == {
        "$schema": "./workspace_schema.jsonc", "config_version": 4,
        "extends": "builtin:workspace", "settings": {},
    }
    assert _load_jsonc_object(config_root / "workspace_local.jsonc") == {
        "$schema": "./workspace_schema.jsonc", "config_version": 4,
        "extends": "user:workspace", "settings": {},
    }
    path = workspace / ".boxteam/workspace.jsonc"
    assert _load_jsonc_object(path) == {
        "config_version": 4, "extends": "user:workspace_local", "settings": {},
    }
    assert not (other / ".boxteam").exists()
    custom = '{"config_version":4,"extends":"custom.jsonc","settings":{}}\n'
    path.write_text(custom)
    install_user_configuration(config_root=config_root, profile="default", workspace_root=workspace)
    assert path.read_text() == custom


def test_changed_schema_is_validated_before_replacing_installed_resources(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    sources = project / "configs"
    sources.mkdir(parents=True)
    for domain in ("gateway", "workspace"):
        source = ('{"config_version": 6, "settings": {}}'
                  if domain == "gateway" else '{"config_version":4,"settings":{}}')
        (sources / f"{domain}_inline.jsonc").write_text(source)
        (sources / f"{domain}_schema.jsonc").write_text('{"type": "object"}')
    config_root = tmp_path / "config"
    install_user_configuration(config_root=config_root, profile="default", project_root=project)
    before = {path.name: path.read_bytes() for path in config_root.iterdir()}
    (sources / "workspace_schema.jsonc").write_text('{"type": "array"}')

    with pytest.raises(jsonschema.ValidationError):
        install_user_configuration(config_root=config_root, profile="default", project_root=project)

    assert {path.name: path.read_bytes() for path in config_root.iterdir()} == before


def test_source_development_install_uses_dev_profiles_and_source_env(
    tmp_path: Path,
) -> None:
    source_project_root = tmp_path / "source-project"
    source_configs_root = source_project_root / "configs"
    source_configs_root.mkdir(parents=True)
    (source_project_root / ".env").write_text(
        "BOXTEAM_TEST=source\n",
        encoding="utf-8",
    )
    for config_resource_name in (
        "gateway_dev.jsonc",
        "gateway_schema.jsonc",
        "workspace_dev.jsonc",
        "workspace_schema.jsonc",
    ):
        shutil.copyfile(
            Path.cwd() / "configs" / config_resource_name,
            source_configs_root / config_resource_name,
        )
    config_root = tmp_path / "boxteam-home" / "config"
    config_root.mkdir(parents=True)
    (config_root / "gateway.jsonc").write_text('{"stale": true}\n')

    result = install_source_development_configuration(
        project_root=source_project_root,
        config_root=config_root,
        force=True,
    )

    assert result.env_path is not None
    assert result.env_path.read_text(encoding="utf-8") == "BOXTEAM_TEST=source\n"
    assert _load_jsonc_object(config_root / "gateway.jsonc")["settings"]["gateway"]["workspaces"][0]["enabled"] is True  # type: ignore[index]
    assert _load_jsonc_object(config_root / "workspace.jsonc")["settings"]["workspace"]["development"] == {"test_tools": True}


def test_install_source_development_configuration_requires_source_env(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="源码环境配置不存在"):
        install_source_development_configuration(
            project_root=tmp_path,
            config_root=tmp_path / "home" / "config",
        )


def test_generator_main_installs_both_domains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boxteam_home = tmp_path / "boxteam-home"
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.setattr(
        "sys.argv",
        ["boxteam", "--project-root", str(Path.cwd()), "--home", str(tmp_path)],
    )

    from configs.boxteam import main

    main()

    output = json.loads(capsys.readouterr().out)
    assert len(output["config_paths"]) == 4
    assert (boxteam_home / "config" / "gateway.jsonc").is_file()
    assert (boxteam_home / "config" / "workspace.jsonc").is_file()


def test_cli_migrate_preserves_legacy_user_configuration_without_release_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boxteam_home = tmp_path / "boxteam-home"
    config_root = boxteam_home / "config"
    config_root.mkdir(parents=True)
    legacy = _legacy_aggregate_config()
    (config_root / "boxteam.jsonc").write_text(
        json.dumps(legacy, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.setattr(
        "sys.argv",
        [
            "boxteam",
            "migrate",
            "--project-root",
            str(Path.cwd()),
            "--home",
            str(tmp_path),
        ],
    )

    from configs.boxteam import main

    before = {path: path.read_bytes() for path in config_root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="缺少已验收升级基线"):
        main()
    assert {path: path.read_bytes() for path in config_root.rglob("*") if path.is_file()} == before


def test_cli_initialize_preserves_legacy_layout_without_release_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boxteam_home = tmp_path / "boxteam-home"
    config_root = boxteam_home / "config"
    config_root.mkdir(parents=True)
    legacy_path = config_root / "boxteam.jsonc"
    legacy = _legacy_aggregate_config()
    original = json.dumps(legacy).encode()
    legacy_path.write_bytes(original)
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.setattr(
        "sys.argv",
        [
            "boxteam",
            "initialize",
            "--project-root",
            str(Path.cwd()),
            "--home",
            str(tmp_path),
        ],
    )

    from configs.boxteam import main

    with pytest.raises(ValueError, match="缺少已验收升级基线"):
        main()
    assert list(config_root.iterdir()) == [legacy_path]
    assert legacy_path.read_bytes() == original


def test_cli_initialize_rejects_old_home_layout_without_moving_any_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "old-home"
    boxteam_home = home / ".boxteams"
    legacy_config = home / ".boxteam" / "boxteam.jsonc"
    legacy_schema = home / ".boxteam" / "config.schema.jsonc"
    legacy_settings = home / ".boxteam" / "web_ui_settings.json"
    legacy_gateway = boxteam_home / "boxteam_workspace" / ".boxteam" / "gateway"
    for path, payload in (
        (legacy_config, '{"legacy": "config"}\n'),
        (legacy_schema, '{"legacy": "schema"}\n'),
        (legacy_settings, '{"legacy": "settings"}\n'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    legacy_gateway.mkdir(parents=True)
    (legacy_gateway / "gateway.sqlite").write_bytes(b"legacy database")
    original_files = {
        path: path.read_bytes()
        for path in (legacy_config, legacy_schema, legacy_settings, legacy_gateway / "gateway.sqlite")
    }
    monkeypatch.delenv("BOXTEAM_HOME", raising=False)
    monkeypatch.delenv("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["boxteam", "initialize", "--home", str(home)],
    )

    from configs.boxteam import main

    with pytest.raises(ValueError, match="缺少已验收升级基线"):
        main()

    assert {path: path.read_bytes() for path in original_files} == original_files
    assert not (boxteam_home / "config").exists()
    assert legacy_gateway.is_dir()


def test_cli_initialize_custom_boxteam_home_ignores_unrelated_old_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "user-home"
    custom_home = tmp_path / "isolated-boxteams"
    legacy = home / ".boxteam" / "boxteam.jsonc"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"legacy": true}\n', encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_HOME", str(custom_home))
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--home", str(home)])

    from configs.boxteam import main

    main()

    assert json.loads(capsys.readouterr().out)["action"] == "initialize"
    assert legacy.read_text(encoding="utf-8") == '{"legacy": true}\n'
    assert (custom_home / "config" / "gateway.jsonc").is_file()


def test_install_gateway_development_assets_is_idempotent(tmp_path: Path) -> None:
    project_root = Path.cwd().resolve()
    home = tmp_path / "home"

    install_gateway_development_assets(project_root=project_root, home=home)
    first_config = (home / ".ssh" / "config").read_text(encoding="utf-8")
    install_gateway_development_assets(project_root=project_root, home=home)
    second_config = (home / ".ssh" / "config").read_text(encoding="utf-8")

    assert second_config == first_config
    assert second_config.count(SSH_BLOCK_BEGIN) == 1
    assert second_config.count(SSH_BLOCK_END) == 1
    private_key = home / ".ssh" / SSH_KEY_NAME
    assert private_key.is_file()
    if os.name != "nt":
        assert private_key.stat().st_mode & 0o777 == 0o600
    assert (home / ".ssh" / SSH_KNOWN_HOSTS_NAME).is_file()


def test_bundled_directory_and_zip_keep_original_source_identity(tmp_path: Path) -> None:
    directory = tmp_path / "package"
    directory.mkdir()
    manifest = {"manifest_version": 1, "package_id": "example.sources", "version": "1.0.0",
                "name": "Sources", "extension_contract_version": 1,
                "plugins": [{"id": "guide", "name": "Guide", "instance_scope": "workspace",
                    "requires": [{"extension_point": "skills", "version": 1}],
                    "contributes": {"skills": [{"id": "guide", "path": "guide"}]}}]}
    (directory / "plugin.jsonc").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "guide").mkdir()
    (directory / "guide/SKILL.md").write_text("---\nname: guide\ndescription: Guide\n---\nGuide.\n", encoding="utf-8")
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for source in directory.rglob("*"):
            if source.is_file():
                output.write(source, source.relative_to(directory).as_posix())
    identity = {"package_id": manifest["package_id"], "package_version": manifest["version"]}
    for source, other in (({"directory": str(directory)}, {"archive": str(archive)}),
                          ({"archive": str(archive)}, {"directory": str(directory)})):
        home = tmp_path / next(iter(source))
        assert install_bundled_plugins(boxteam_home=home, packages=[identity | source]) == ["example.sources@1.0.0"]
        assert install_bundled_plugins(boxteam_home=home, packages=[identity | source]) == []
        with pytest.raises(RuntimeError, match="摘要冲突"):
            install_bundled_plugins(boxteam_home=home, packages=[identity | other])
        assert install_bundled_plugins(boxteam_home=home, packages=[identity | source]) == []
