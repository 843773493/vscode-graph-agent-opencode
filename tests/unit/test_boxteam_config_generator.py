from __future__ import annotations

import json
from pathlib import Path

import pytest

from configs.boxteam import (
    SSH_BLOCK_BEGIN,
    SSH_BLOCK_END,
    SSH_KEY_NAME,
    build_boxteam_config,
    install_config_schema,
    install_development_ssh_assets,
    write_boxteam_config,
)


def test_build_boxteam_config_only_enables_development_features_when_requested() -> None:
    production = build_boxteam_config(development_assets=False)
    development = build_boxteam_config(development_assets=True)

    assert production["development"] == {"test_tools": False}
    assert production["gateway"] == {"workspaces": []}
    assert development["development"] == {"test_tools": True}
    assert development["gateway"]["workspaces"][0]["enabled"] is False
    custom_names = {
        item["name"]
        for item in development["agents"]["default"]["tools"]["custom"]
    }
    assert "test_tool_2" in custom_names


def test_build_boxteam_config_can_enable_gateway_e2e_workspace() -> None:
    development = build_boxteam_config(
        development_assets=True,
        gateway_e2e_workspace_enabled=True,
    )

    assert development["gateway"]["workspaces"][0]["enabled"] is True


def test_build_boxteam_config_rejects_gateway_workspace_without_assets() -> None:
    with pytest.raises(ValueError, match="必须先安装开发资产"):
        build_boxteam_config(
            development_assets=False,
            gateway_e2e_workspace_enabled=True,
        )


def test_write_boxteam_config_writes_valid_json(tmp_path: Path) -> None:
    output = tmp_path / ".boxteam" / "boxteam.jsonc"

    write_boxteam_config(output, development_assets=False)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["default_agent"] == "default"
    assert output.read_bytes().endswith(b"\n")
    assert output.stat().st_mode & 0o777 == 0o600


def test_generator_main_uses_boxteam_home(tmp_path: Path, monkeypatch) -> None:
    boxteam_home = tmp_path / "custom-boxteam-home"
    home = tmp_path / "home"
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.setenv("BOXTEAM_INSTALL_DEVELOPMENT_ASSETS", "0")
    monkeypatch.setenv("BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE", "0")
    monkeypatch.setattr(
        "sys.argv",
        ["boxteam", "--project-root", str(Path.cwd()), "--home", str(home)],
    )

    from configs.boxteam import main

    main()

    assert (boxteam_home / "config" / "boxteam.jsonc").is_file()
    assert (boxteam_home / "config" / "config.schema.jsonc").is_file()


def test_install_config_schema_copies_runtime_resource(tmp_path: Path) -> None:
    config_path = tmp_path / ".boxteam" / "boxteam.jsonc"

    target = install_config_schema(project_root=Path.cwd(), config_path=config_path)

    assert target == config_path.parent / "config.schema.jsonc"
    assert target.read_bytes() == (Path.cwd() / "configs" / "config.jsonc").read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600


def test_install_development_ssh_assets_is_idempotent(tmp_path: Path) -> None:
    project_root = Path.cwd().resolve()
    home = tmp_path / "home"

    install_development_ssh_assets(project_root=project_root, home=home)
    first_config = (home / ".ssh" / "config").read_text(encoding="utf-8")
    install_development_ssh_assets(project_root=project_root, home=home)
    second_config = (home / ".ssh" / "config").read_text(encoding="utf-8")

    assert second_config == first_config
    assert second_config.count(SSH_BLOCK_BEGIN) == 1
    assert second_config.count(SSH_BLOCK_END) == 1
    assert (home / ".ssh" / SSH_KEY_NAME).read_bytes() == (
        project_root / "asset" / "gateway_ssh" / SSH_KEY_NAME
    ).read_bytes()
    assert (home / ".ssh" / SSH_KEY_NAME).stat().st_mode & 0o777 == 0o600
    assert (home / ".ssh" / "config").stat().st_mode & 0o777 == 0o600
