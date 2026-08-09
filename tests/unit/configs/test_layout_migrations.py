from __future__ import annotations

import json
from pathlib import Path

import commentjson
import pytest

from configs.installer import atomic_write
from configs.layout_migrations import (
    migrate_legacy_user_configuration,
    migrate_legacy_workspace_configuration,
)


def _legacy_user_config() -> dict[str, object]:
    workspace = commentjson.loads(
        Path("configs/workspace_inline.jsonc").read_text(encoding="utf-8")
    )
    assert isinstance(workspace, dict)
    workspace.pop("$schema")
    workspace["config_version"] = 4
    workspace["gateway"] = {
        "workspaces": [
            {
                "kind": "remote_gateway",
                "host": "remote.example.com",
                "username": "developer",
                "private_key_path": "~/.ssh/id_ed25519",
            }
        ]
    }
    return workspace


def _schema_paths() -> tuple[Path, Path]:
    return Path("configs/gateway_schema.jsonc"), Path("configs/workspace_schema.jsonc")


def test_migrate_legacy_user_configuration_splits_domains(tmp_path: Path) -> None:
    legacy_path = tmp_path / "boxteam.jsonc"
    legacy_path.write_text(json.dumps(_legacy_user_config()), encoding="utf-8")
    gateway_schema, workspace_schema = _schema_paths()

    assert migrate_legacy_user_configuration(
        config_root=tmp_path,
        gateway_schema_path=gateway_schema,
        workspace_schema_path=workspace_schema,
    )

    gateway = json.loads(
        (tmp_path / "gateway.jsonc").read_text(encoding="utf-8")
    )
    workspace = json.loads(
        (tmp_path / "workspace.jsonc").read_text(encoding="utf-8")
    )
    assert gateway["config_version"] == 1
    assert gateway["workspaces"][0]["host"] == "remote.example.com"
    assert workspace["config_version"] == 1
    assert "gateway" not in workspace
    assert not legacy_path.exists()


def test_user_layout_migration_recovers_after_one_target_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_path = tmp_path / "boxteam.jsonc"
    legacy_path.write_text(json.dumps(_legacy_user_config()), encoding="utf-8")
    gateway_schema, workspace_schema = _schema_paths()
    writes = 0

    def interrupt_third_write(path: Path, content: bytes, mode: int) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("模拟第二个目标提交前中断")
        atomic_write(path, content, mode)

    monkeypatch.setattr(
        "configs.layout_migrations.atomic_write",
        interrupt_third_write,
    )
    with pytest.raises(OSError, match="模拟第二个目标"):
        migrate_legacy_user_configuration(
            config_root=tmp_path,
            gateway_schema_path=gateway_schema,
            workspace_schema_path=workspace_schema,
        )

    monkeypatch.setattr("configs.layout_migrations.atomic_write", atomic_write)
    assert migrate_legacy_user_configuration(
        config_root=tmp_path,
        gateway_schema_path=gateway_schema,
        workspace_schema_path=workspace_schema,
    )
    assert (tmp_path / "gateway.jsonc").is_file()
    assert (tmp_path / "workspace.jsonc").is_file()
    assert not (tmp_path / ".config-layout-migration.json").exists()


def test_user_layout_migration_rejects_conflicting_new_target(tmp_path: Path) -> None:
    (tmp_path / "boxteam.jsonc").write_text(
        json.dumps(_legacy_user_config()), encoding="utf-8"
    )
    (tmp_path / "gateway.jsonc").write_text('{"user": "owned"}\n')
    gateway_schema, workspace_schema = _schema_paths()

    with pytest.raises(RuntimeError, match="新旧配置布局同时存在"):
        migrate_legacy_user_configuration(
            config_root=tmp_path,
            gateway_schema_path=gateway_schema,
            workspace_schema_path=workspace_schema,
        )


def test_workspace_layout_migration_rejects_gateway_field(tmp_path: Path) -> None:
    legacy_path = tmp_path / ".boxteam" / "boxteam.jsonc"
    legacy_path.parent.mkdir()
    legacy_path.write_text(
        json.dumps({"config_version": 4, "gateway": {"workspaces": []}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Gateway 配置只能迁移"):
        migrate_legacy_workspace_configuration(
            workspace_root=tmp_path,
            workspace_schema_path=Path("configs/workspace_schema.jsonc"),
        )
