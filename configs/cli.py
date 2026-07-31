from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from app.core.path_utils import resolve_boxteam_home
from app.core.storage_migration import migrate_user_storage_layout
from configs.gateway_development_assets import install_gateway_development_assets
from configs.installer import (
    install_source_development_configuration,
    install_user_configuration,
    resolve_config_resource_source,
)
from configs.layout_migrations import (
    migrate_legacy_user_configuration,
    migrate_legacy_workspace_configuration,
)

GATEWAY_DEVELOPMENT_ASSETS_ENV = "BOXTEAM_INSTALL_DEVELOPMENT_ASSETS"


def _environment_flag(name: str) -> bool:
    raw_value = os.environ.get(name, "0").strip()
    if raw_value not in {"0", "1"}:
        raise ValueError(f"{name} 只允许 0 或 1，实际值: {raw_value!r}")
    return raw_value == "1"


def _layout_schema_sources(
    project_root: Path | None,
) -> tuple[Path, Path]:
    return (
        resolve_config_resource_source(
            "gateway_config.jsonc",
            project_root=project_root,
        ),
        resolve_config_resource_source(
            "workspace_config.jsonc",
            project_root=project_root,
        ),
    )


def _prepare_storage(*, home: Path, boxteam_home: Path) -> None:
    configured_default_workspace = os.environ.get("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT")
    default_workspace_root = (
        Path(configured_default_workspace or boxteam_home / "boxteam_workspace")
        .expanduser()
        .resolve()
    )
    migrate_user_storage_layout(
        home=home,
        boxteam_home=boxteam_home,
        default_workspace_root=default_workspace_root,
    )


def _validate_installed_configuration(
    *,
    config_root: Path,
    workspace: Path | None,
) -> dict[str, object]:
    from app.gateway.config import load_gateway_config
    from app.services.infrastructure.config_service import ConfigService

    gateway = load_gateway_config(
        config_path=config_root / "gateway.jsonc",
        schema_path=config_root / "gateway_config.jsonc",
    )
    service = ConfigService(
        config_dir=config_root,
        config_path=config_root / "workspace.jsonc",
        workspace_root=workspace,
    )
    service.validate_workspace_config()
    return {
        "gateway_count": len(gateway.workspaces),
        "workspace_revision": service.get_revision(),
        "workspace_sources": [
            str(path) for path in service.get_snapshot().source_paths
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="管理 BoxTeam 双域 JSONC 配置")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("initialize", "install-source-development", "migrate", "doctor"),
        default="initialize",
    )
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument(
        "--force",
        action="store_true",
        help="显式用普通静态模板重建 Gateway 与 Workspace 用户配置",
    )
    args = parser.parse_args()

    home = args.home.expanduser().resolve()
    boxteam_home = resolve_boxteam_home(home)
    config_root = boxteam_home / "config"
    project_root = (
        args.project_root.expanduser().resolve()
        if args.project_root is not None
        else None
    )
    if args.action == "install-source-development":
        if project_root is None:
            raise ValueError("安装源码开发配置必须提供 --project-root")
        source_env_path = project_root / ".env"
        if not source_env_path.is_file():
            raise FileNotFoundError(f"源码环境配置不存在: {source_env_path}")
        load_dotenv(source_env_path, override=False)
    else:
        load_dotenv(config_root / ".env", override=False)

    _prepare_storage(home=home, boxteam_home=boxteam_home)
    gateway_schema_source, workspace_schema_source = _layout_schema_sources(
        project_root
    )

    if args.action in {"initialize", "install-source-development", "migrate"}:
        migrated_user_layout = migrate_legacy_user_configuration(
            config_root=config_root,
            gateway_schema_path=gateway_schema_source,
            workspace_schema_path=workspace_schema_source,
        )
    else:
        migrated_user_layout = False

    migrated_workspace_layout = False
    if args.workspace is not None and args.action == "migrate":
        migrated_workspace_layout = migrate_legacy_workspace_configuration(
            workspace_root=args.workspace,
            workspace_schema_path=workspace_schema_source,
        )

    if args.action == "install-source-development":
        installation = install_source_development_configuration(
            project_root=project_root,
            config_root=config_root,
        )
        gateway_development_assets = _environment_flag(
            GATEWAY_DEVELOPMENT_ASSETS_ENV
        )
        if gateway_development_assets:
            install_gateway_development_assets(
                project_root=project_root,
                home=home,
            )
        print(
            json.dumps(
                {
                    "action": args.action,
                    "env_path": str(installation.env_path),
                    "config_paths": [str(path) for path in installation.config_paths],
                    "schema_paths": [str(path) for path in installation.schema_paths],
                    "gateway_development_assets": gateway_development_assets,
                    "migrated_user_layout": migrated_user_layout,
                },
                ensure_ascii=False,
            )
        )
        return

    if args.action == "initialize":
        installation = install_user_configuration(
            config_root=config_root,
            profile="default",
            project_root=project_root,
            force=args.force,
        )
        print(
            json.dumps(
                {
                    "action": args.action,
                    "config_paths": [str(path) for path in installation.config_paths],
                    "schema_paths": [str(path) for path in installation.schema_paths],
                    "created_config_paths": [
                        str(path) for path in installation.created_config_paths
                    ],
                    "migrated_user_layout": migrated_user_layout,
                },
                ensure_ascii=False,
            )
        )
        return

    if args.action == "migrate":
        missing_paths = [
            config_root / f"{domain}.jsonc"
            for domain in ("gateway", "workspace")
            if not (config_root / f"{domain}.jsonc").is_file()
        ]
        if missing_paths:
            raise FileNotFoundError(
                f"没有可迁移的完整用户配置: {missing_paths}"
            )
        install_user_configuration(
            config_root=config_root,
            profile="default",
            project_root=project_root,
        )

    details = _validate_installed_configuration(
        config_root=config_root,
        workspace=(
            args.workspace.expanduser().resolve()
            if args.workspace is not None
            else None
        ),
    )
    print(
        json.dumps(
            {
                "action": args.action,
                "valid": True,
                "migrated_user_layout": migrated_user_layout,
                "migrated_workspace_layout": migrated_workspace_layout,
                **details,
            },
            ensure_ascii=False,
        )
    )
