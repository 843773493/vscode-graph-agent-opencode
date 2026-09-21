from app.core.user_storage_migration import migrate_user_storage_layout


def test_migrate_user_storage_layout_moves_global_data(tmp_path, monkeypatch):
    home = tmp_path / "home"
    boxteam_home = home / ".boxteams"
    default_workspace = boxteam_home / "boxteam_workspace"
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    legacy_config = home / ".boxteam"
    legacy_config.mkdir(parents=True)
    (legacy_config / "boxteam.jsonc").write_text("{}", encoding="utf-8")
    legacy_gateway = default_workspace / ".boxteam" / "gateway"
    legacy_gateway.mkdir(parents=True)
    (legacy_gateway / "workspaces.json").write_text("{}", encoding="utf-8")

    migrate_user_storage_layout(
        home=home,
        boxteam_home=boxteam_home,
        default_workspace_root=default_workspace,
    )

    assert (boxteam_home / "config" / "boxteam.jsonc").is_file()
    assert (boxteam_home / "state" / "gateway" / "workspaces.json").is_file()
    assert not legacy_gateway.exists()
