"""验证双域静态配置资源及安装器。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import commentjson
import jsonschema
import pytest

from configs.boxteam import (
    SSH_BLOCK_BEGIN,
    SSH_BLOCK_END,
    SSH_KEY_NAME,
    SSH_KNOWN_HOSTS_NAME,
    install_gateway_development_assets,
    install_source_development_configuration,
    install_user_configuration,
)


def _load(path: Path) -> dict[str, object]:
    payload = commentjson.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


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
def test_static_configuration_matches_domain_schema(domain: str, suffix: str) -> None:
    schema = _load(Path(f"configs/{domain}_schema.jsonc"))
    config = _load(Path(f"configs/{domain}{suffix}.jsonc"))

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(config, schema)


def test_development_templates_enable_development_capabilities_and_docker_gateway() -> None:
    gateway = _load(Path("configs/gateway_inline.jsonc"))
    gateway_dev = _load(Path("configs/gateway_dev.jsonc"))
    workspace = _load(Path("configs/workspace_inline.jsonc"))
    workspace_dev = _load(Path("configs/workspace_dev.jsonc"))

    assert gateway["workspaces"] == []
    assert gateway_dev["workspaces"][0]["enabled"] is True  # type: ignore[index]
    assert (
        gateway_dev["workspaces"][0]["ssh_config_host"]  # type: ignore[index]
        == "boxteam-container"
    )
    assert workspace["development"] == {"test_tools": False}
    assert workspace_dev["development"] == {"test_tools": True}
    assert workspace["mcp"] == {"servers": {}}
    assert "mcp" in workspace_dev
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
    assert result.created_config_paths == (config_root / "workspace.jsonc",)
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
        config_root / "workspace.jsonc",
    }
    assert "stale" not in _load(config_root / "gateway.jsonc")
    assert "stale" not in _load(config_root / "workspace.jsonc")


def test_install_source_development_configuration_replaces_all_runtime_files(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    configs_root = project_root / "configs"
    configs_root.mkdir(parents=True)
    (project_root / ".env").write_text("BOXTEAM_TEST=source\n", encoding="utf-8")
    for name in (
        "gateway_inline.jsonc",
        "gateway_dev.jsonc",
        "gateway_schema.jsonc",
        "workspace_inline.jsonc",
        "workspace_dev.jsonc",
        "workspace_schema.jsonc",
    ):
        shutil.copy2(Path.cwd() / "configs" / name, configs_root / name)
    config_root = tmp_path / "boxteam-home" / "config"
    config_root.mkdir(parents=True)
    (config_root / "gateway.jsonc").write_text('{"stale": true}\n')

    result = install_source_development_configuration(
        project_root=project_root,
        config_root=config_root,
    )

    assert result.env_path is not None
    assert result.env_path.read_text(encoding="utf-8") == "BOXTEAM_TEST=source\n"
    assert _load(config_root / "gateway.jsonc")["workspaces"][0]["enabled"] is True  # type: ignore[index]
    assert _load(config_root / "workspace.jsonc")["development"] == {"test_tools": True}


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
    assert len(output["config_paths"]) == 2
    assert (boxteam_home / "config" / "gateway.jsonc").is_file()
    assert (boxteam_home / "config" / "workspace.jsonc").is_file()


def test_cli_migrate_splits_legacy_user_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boxteam_home = tmp_path / "boxteam-home"
    config_root = boxteam_home / "config"
    config_root.mkdir(parents=True)
    legacy = _load(Path("configs/workspace_inline.jsonc"))
    legacy.pop("$schema")
    legacy["config_version"] = 4
    legacy["gateway"] = {"workspaces": []}
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

    main()

    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True
    assert output["migrated_user_layout"] is True
    assert (config_root / "gateway.jsonc").is_file()
    assert (config_root / "workspace.jsonc").is_file()
    assert not (config_root / "boxteam.jsonc").exists()


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
