from __future__ import annotations

import shutil
from pathlib import Path


def _move_legacy_path(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if target.exists():
        raise FileExistsError(
            f"迁移目标已存在，拒绝覆盖: source={source} target={target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def migrate_user_storage_layout(
    *,
    home: Path,
    boxteam_home: Path,
    default_workspace_root: Path,
) -> None:
    """把旧用户级安装数据迁入统一全局目录。

    该入口只由配置安装维护命令显式调用，不属于工作区后端启动流程。
    """
    config_root = boxteam_home / "config"
    legacy_config_root = home / ".boxteam"
    _move_legacy_path(
        legacy_config_root / "boxteam.jsonc",
        config_root / "boxteam.jsonc",
    )
    _move_legacy_path(
        legacy_config_root / "config.schema.jsonc",
        boxteam_home / "state" / "migrated" / "legacy_config.schema.jsonc",
    )
    _move_legacy_path(
        default_workspace_root / ".boxteam" / "gateway",
        boxteam_home / "state" / "gateway",
    )
    legacy_ui_settings = legacy_config_root / "web_ui_settings.json"
    if legacy_ui_settings.exists():
        current_ui_settings = (
            boxteam_home / "state" / "gateway" / "web_ui_settings.json"
        )
        ui_target = (
            boxteam_home / "state" / "migrated" / "legacy_web_ui_settings.json"
            if current_ui_settings.exists()
            else current_ui_settings
        )
        _move_legacy_path(legacy_ui_settings, ui_target)
