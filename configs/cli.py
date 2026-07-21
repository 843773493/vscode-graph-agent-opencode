from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.core.path_utils import resolve_boxteam_home
from app.core.storage_migration import migrate_user_storage_layout
from configs.development_assets import install_development_ssh_assets
from configs.installer import initialize_boxteam_config


DEVELOPMENT_ASSETS_ENV = "BOXTEAM_INSTALL_DEVELOPMENT_ASSETS"
GATEWAY_E2E_WORKSPACE_ENV = "BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE"


def _environment_flag(name: str) -> bool:
    raw_value = os.environ.get(name, "0").strip()
    if raw_value not in {"0", "1"}:
        raise ValueError(f"{name} 只允许 0 或 1，实际值: {raw_value!r}")
    return raw_value == "1"


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 BoxTeam 用户级配置")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument(
        "--force",
        action="store_true",
        help="显式重建已存在的用户配置",
    )
    args = parser.parse_args()

    development_assets = _environment_flag(DEVELOPMENT_ASSETS_ENV)
    gateway_e2e_workspace_enabled = _environment_flag(GATEWAY_E2E_WORKSPACE_ENV)
    boxteam_home = resolve_boxteam_home(args.home)
    configured_default_workspace = os.environ.get(
        "BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT"
    )
    default_workspace_root = Path(
        configured_default_workspace or boxteam_home / "boxteam_workspace"
    ).expanduser().resolve()
    migrate_user_storage_layout(
        home=args.home.expanduser().resolve(),
        boxteam_home=boxteam_home,
        default_workspace_root=default_workspace_root,
    )
    output = args.output or boxteam_home / "config" / "boxteam.jsonc"
    created = initialize_boxteam_config(
        output,
        development_assets=development_assets,
        gateway_e2e_workspace_enabled=gateway_e2e_workspace_enabled,
        project_root=args.project_root,
        force=args.force,
    )
    if development_assets:
        if args.project_root is None:
            raise ValueError("安装开发 SSH 资产必须提供 --project-root")
        install_development_ssh_assets(
            project_root=args.project_root.expanduser().resolve(),
            home=args.home,
        )
    print(
        json.dumps(
            {
                "config_path": str(output.expanduser().resolve()),
                "created": created,
                "development_assets": development_assets,
                "gateway_e2e_workspace_enabled": gateway_e2e_workspace_enabled,
            },
            ensure_ascii=False,
        )
    )
