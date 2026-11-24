from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from typing import cast

from boxteam_plugin_sdk.logging_contract import load_runtime_log_contract
from dotenv import load_dotenv

from app.core.path_utils import resolve_boxteam_home
from app.core.user_storage_migration import migrate_user_storage_layout
from configs.diagnostics import diagnose_configuration, diagnostic_error
from configs.gateway_development_assets import install_gateway_development_assets
from configs.installer import (
    install_bundled_plugins,
    install_source_development_configuration,
    install_user_configuration,
    resolve_config_resource_source,
)

GATEWAY_DEVELOPMENT_ASSETS_ENV = "BOXTEAM_INSTALL_DEVELOPMENT_ASSETS"
DEV_LOCAL_ONLY_ENV = "BOXTEAM_DEV_LOCAL_ONLY"


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
            "gateway_schema.jsonc",
            project_root=project_root,
        ),
        resolve_config_resource_source(
            "workspace_schema.jsonc",
            project_root=project_root,
        ),
    )


def _prepare_storage(*, home: Path, boxteam_home: Path, preview: bool = False) -> None:
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
        preview=preview,
    )


def _prepare_startup_configuration(
    *,
    home: Path,
    boxteam_home: Path,
    config_root: Path,
    workspace: Path | None,
    project_root: Path | None,
    preview: bool = False,
) -> dict[str, object]:
    from configs.maintenance.layout import (
        migrate_legacy_user_configuration,
        migrate_legacy_workspace_configuration,
        plan_legacy_user_configuration,
        plan_legacy_workspace_configuration,
    )
    from configs.maintenance.runtime_logging_commit import _lock_databases
    from configs.maintenance.startup import _registry, prepare_startup_migrations

    gateway_schema, workspace_schema = _layout_schema_sources(project_root)
    gateway_root = (Path(os.environ["BOXTEAM_GATEWAY_ROOT"]) if os.environ.get("BOXTEAM_GATEWAY_ROOT")
                    else boxteam_home / "state/gateway").expanduser().resolve()
    default_home = boxteam_home == (home / ".boxteams").resolve()
    default_root = Path(os.environ.get("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT") or boxteam_home / "boxteam_workspace")
    legacy_gateway_root = default_root.expanduser().resolve() / ".boxteam/gateway"
    old_sources = [home / ".boxteam" / name for name in ("boxteam.jsonc", "config.schema.jsonc", "web_ui_settings.json")]
    old_sources.append(legacy_gateway_root)
    journals = (config_root / ".runtime-logging-migration.json", gateway_root / ".workspace-identity-migration.json")
    if any(path.exists() and json.loads(path.read_text(encoding="utf-8"))["state"] != "completed" for path in journals):
        return {"migrated_user_layout": False, "migrated_workspace_layout": False,
                "upgrades": prepare_startup_migrations(
                    boxteam_home=boxteam_home, project_root=project_root or Path.cwd(),
                    gateway_root=gateway_root, workspaces=(workspace,) if workspace is not None else (),
                    preview=preview,
                )}
    user_plan = plan_legacy_user_configuration(
        config_root=config_root, gateway_schema_path=gateway_schema,
        workspace_schema_path=workspace_schema,
        legacy_path=(home / ".boxteam/boxteam.jsonc" if default_home
                     and (home / ".boxteam/boxteam.jsonc").exists() else None),
    )
    # 预检读取尚未搬动的原 owner；真正启动仍使用同一个目标 Gateway 根。
    source_gateway_root = (legacy_gateway_root if default_home and legacy_gateway_root.exists()
                           and gateway_root == boxteam_home / "state/gateway" else gateway_root)
    gateway_sources = [source_gateway_root]
    if default_home and legacy_gateway_root.exists() and legacy_gateway_root not in gateway_sources:
        gateway_sources.append(legacy_gateway_root)
    roots = [workspace] if workspace is not None else []
    for source_root in gateway_sources:
        registry = _registry(source_root)
        if registry is not None:
            roots.extend(Path(item["root_path"]) for item in registry["targets"] if item.get("connection_kind") == "local")
    roots = list(dict.fromkeys(root.expanduser().resolve() for root in roots))
    workspace_plans = [plan for root in roots if (plan := plan_legacy_workspace_configuration(
        workspace_root=root, workspace_schema_path=workspace_schema,
    )) is not None]
    if default_home:
        _prepare_storage(home=home, boxteam_home=boxteam_home, preview=True)
    layout_required = user_plan is not None or bool(workspace_plans) or default_home and any(path.exists() for path in old_sources)
    migrated_user = migrated_workspace = False
    if layout_required:
        # 全来源版本、日志取舍和目标候选先在内存中确认；首个搬动或拆分之前不提交其他资产。
        plans = ([user_plan] if user_plan is not None else []) + workspace_plans
        planned_files = {path: content for plan in plans for path, content in plan.targets}
        upgrades = prepare_startup_migrations(
            boxteam_home=boxteam_home, project_root=project_root or Path.cwd(),
            gateway_root=source_gateway_root, workspaces=tuple(roots), preview=True,
            planned_files=planned_files,
        )
        for source_root in gateway_sources[1:]:
            prepare_startup_migrations(
                boxteam_home=boxteam_home, project_root=project_root or Path.cwd(),
                gateway_root=source_root, workspaces=tuple(roots), preview=True,
                planned_files=planned_files,
            )
        upgrades["layout"] = {"state": "migration_required"}
        if preview:
            return {"migrated_user_layout": False, "migrated_workspace_layout": False,
                    "upgrades": upgrades}
        with ExitStack() as stack:
            # 布局写入也必须先取得原数据库 owner；不能拆分完才发现服务仍在写。
            _lock_databases([root / ".boxteam/state/workspace.sqlite" for root in roots], stack)
            if source_gateway_root != legacy_gateway_root:
                _lock_databases([gateway_root / "gateway.sqlite"], stack)
            if default_home:
                # Windows 不能搬仍有文件句柄的目录；先由原 owner 核实旧 writer 已退出。
                if legacy_gateway_root.exists():
                    with ExitStack() as source_access:
                        _lock_databases([legacy_gateway_root / "gateway.sqlite"], source_access)
                _prepare_storage(home=home, boxteam_home=boxteam_home)
            if source_gateway_root == legacy_gateway_root:
                _lock_databases([gateway_root / "gateway.sqlite"], stack)
            migrated_user = migrate_legacy_user_configuration(
                config_root=config_root, gateway_schema_path=gateway_schema,
                workspace_schema_path=workspace_schema,
            )
            for root in roots:
                migrated_workspace = migrate_legacy_workspace_configuration(
                    workspace_root=root, workspace_schema_path=workspace_schema,
                ) or migrated_workspace
    upgrades = prepare_startup_migrations(
        boxteam_home=boxteam_home, project_root=project_root or Path.cwd(),
        gateway_root=gateway_root, workspaces=tuple(roots),
        preview=preview,
    )
    return {"migrated_user_layout": migrated_user, "migrated_workspace_layout": migrated_workspace,
            "upgrades": upgrades}


def _validate_installed_configuration(
    *,
    config_root: Path,
    workspace: Path | None,
) -> dict[str, object]:
    from app.gateway.config import load_gateway_config
    from app.services.infrastructure.config_service import ConfigService

    gateway = load_gateway_config(
        config_path=config_root / "gateway.jsonc",
        entry_path=config_root / "gateway_local.jsonc",
        schema_path=config_root / "gateway_schema.jsonc",
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


class _DiagnosticLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        error = record.exc_info[1] if record.exc_info is not None else None
        facts = (
            diagnostic_error(error, stage="diagnostic_log")
            if error is not None
            else "详情见诊断 JSON"
        )
        return f"{record.levelname}: logger={record.name}, {facts}"


def _print_configuration_diagnostic(
    *,
    home: Path,
    project_root: Path | None,
    workspace_root: Path | None,
) -> None:
    # 离线终端没有原始服务日志例外；JSON 报告错误事实，日志只发布事件归属。
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers
    handler = logging.StreamHandler()
    handler.setFormatter(_DiagnosticLogFormatter())
    root_logger.handlers = [handler]
    try:
        boxteam_home = resolve_boxteam_home(home.expanduser().resolve())
        config_root = boxteam_home / "config"
        project_root = (
            project_root.expanduser().resolve() if project_root is not None else None
        )
        gateway_schema_source, workspace_schema_source = _layout_schema_sources(
            project_root
        )
        print(
            json.dumps(
                diagnose_configuration(
                    config_root=config_root,
                    gateway_inline_path=resolve_config_resource_source(
                        "gateway_inline.jsonc", project_root=project_root
                    ),
                    gateway_schema_path=(
                        config_root / "gateway_schema.jsonc"
                        if (config_root / "gateway_schema.jsonc").is_file()
                        else gateway_schema_source
                    ),
                    workspace_inline_path=resolve_config_resource_source(
                        "workspace_inline.jsonc", project_root=project_root
                    ),
                    workspace_schema_path=(
                        config_root / "workspace_schema.jsonc"
                        if (config_root / "workspace_schema.jsonc").is_file()
                        else workspace_schema_source
                    ),
                    gateway_sqlite_path=(
                        Path(os.environ["BOXTEAM_GATEWAY_ROOT"])
                        if os.environ.get("BOXTEAM_GATEWAY_ROOT")
                        else boxteam_home / "state" / "gateway"
                    )
                    / "gateway.sqlite",
                    workspace_root=(
                        workspace_root.expanduser().resolve()
                        if workspace_root is not None
                        else None
                    ),
                ),
                ensure_ascii=False,
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(
            json.dumps(
                {
                    "action": "diagnose",
                    "read_only": True,
                    "error": diagnostic_error(error, stage="select_resources"),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    finally:
        root_logger.handlers = original_handlers
        handler.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="管理 BoxTeam 双域 JSONC 配置")
    parser.add_argument(
        "action",
        nargs="?",
        choices=(
            "initialize",
            "install-source-development",
            "migrate",
            "migrate-runtime-config",
            "preview-workspace-identity",
            "migrate-workspace-identity",
            "doctor",
            "diagnose",
        ),
        default="initialize",
    )
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--bundled-plugins", type=json.loads, default=[])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--migration-workspace", type=Path, action="append", default=[])
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--verify-remote", action="store_true", help="身份预检时通过既有凭据和 SSH 隧道只读核验远端，不自动配对或启动服务")
    parser.add_argument("--recovery-output", type=Path, help="身份迁移成功后导出供客户端显式导入的恢复材料")
    parser.add_argument("--restore", action="store_true", help="恢复对应维护操作最近一次迁移的原件")
    parser.add_argument("--level")
    parser.add_argument("--pretty", choices=("true", "false"))
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument(
        "--force",
        action="store_true",
        help="显式用普通静态模板重建 Gateway 与 Workspace 用户配置",
    )
    args = parser.parse_args()
    if args.verify_remote and args.action not in {"preview-workspace-identity", "migrate-workspace-identity"}:
        parser.error("--verify-remote 只用于工作区身份维护")
    if args.recovery_output is not None and args.action != "migrate-workspace-identity":
        parser.error("--recovery-output 只用于 migrate-workspace-identity")
    if args.action != "migrate-runtime-config" and (
        args.preview and args.action != "initialize"
        or args.restore and args.action != "migrate-workspace-identity"
        or args.level is not None
        or args.pretty is not None
        or args.migration_workspace
    ):
        parser.error("--preview 用于 initialize/migrate-runtime-config；--restore 用于原迁移；其它选择参数只用于 migrate-runtime-config")
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
    if args.action in {"doctor", "diagnose"}:
        _print_configuration_diagnostic(
            home=home, project_root=project_root, workspace_root=args.workspace,
        )
        return
    startup = {}
    if args.action in {"initialize", "install-source-development"}:
        startup = _prepare_startup_configuration(
            home=home,
            boxteam_home=boxteam_home,
            config_root=config_root,
            workspace=args.workspace,
            project_root=project_root,
            preview=args.preview,
        )
        if args.preview:
            print(json.dumps({"action": args.action, "read_only": True,
                              "upgrade_required": bool(startup["upgrades"]), **startup}, ensure_ascii=False))
            return
    if args.action in {"preview-workspace-identity", "migrate-workspace-identity"}:
        from configs.maintenance.workspace_identity_migration import (
            preview_workspace_identity_migration,
            verify_remote_workspace_identities,
        )

        gateway_root = (Path(os.environ["BOXTEAM_GATEWAY_ROOT"]) if os.environ.get("BOXTEAM_GATEWAY_ROOT")
                        else boxteam_home / "state/gateway").expanduser().resolve()
        try:
            if args.action == "migrate-workspace-identity":
                from configs.maintenance.workspace_identity_commit import (
                    commit_workspace_identity_migration,
                )

                # 继续与回退使用已冻结计划；不再次要求旧格式预检或已离线远端。
                journal = gateway_root / ".workspace-identity-migration.json"
                verified = None
                if args.verify_remote and not args.restore and (not journal.exists() or json.loads(journal.read_text())["state"] == "restored"):
                    verified = asyncio.run(verify_remote_workspace_identities(
                        preview_workspace_identity_migration(gateway_root=gateway_root, project_root=project_root or Path.cwd()),
                        gateway_root=gateway_root, log_dir=load_runtime_log_contract().root_for(boxteam_home),
                    ))
                report = commit_workspace_identity_migration(
                    gateway_root=gateway_root, project_root=project_root or Path.cwd(), restore=args.restore,
                    verified_preview=verified, recovery_output=args.recovery_output,
                )
                print(json.dumps({"action": args.action, **report}, ensure_ascii=False))
                return
            preview = preview_workspace_identity_migration(
                gateway_root=gateway_root, project_root=project_root or Path.cwd(),
            )
            if args.verify_remote:
                log_dir = load_runtime_log_contract().root_for(boxteam_home)
                preview = asyncio.run(verify_remote_workspace_identities(
                    preview, gateway_root=gateway_root, log_dir=log_dir,
                ))
            report = preview.report()
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as error:
            print(json.dumps({"action": args.action, "read_only": args.action == "preview-workspace-identity",
                              "error": str(error),
                              "diagnostic": diagnostic_error(error, stage="registry_identity_preflight")}, ensure_ascii=False))
            raise SystemExit(1) from None
        print(json.dumps({"action": args.action, **report}, ensure_ascii=False))
        if report["blockers"]:
            raise SystemExit(1)
        return
    if args.action == "migrate-runtime-config":
        from configs.maintenance.runtime_logging_commit import (
            commit_runtime_logging_migration,
        )
        from configs.maintenance.runtime_logging_migration import (
            preview_runtime_logging_migration,
        )

        workspaces = tuple(args.migration_workspace)
        if args.workspace is not None:
            workspaces = (args.workspace, *workspaces)
        if args.preview and args.restore:
            parser.error("--preview 与 --restore 不能同时使用")
        options = {
            "boxteam_home": boxteam_home, "workspaces": workspaces,
            "level": args.level, "pretty": None if args.pretty is None else args.pretty == "true",
            "project_root": project_root,
            "gateway_sqlite_path": (
                Path(os.environ["BOXTEAM_GATEWAY_ROOT"]) / "gateway.sqlite"
                if os.environ.get("BOXTEAM_GATEWAY_ROOT") else None
            ),
        }
        report = (preview_runtime_logging_migration(**options).report() if args.preview
                  else commit_runtime_logging_migration(**options, restore=args.restore))
        print(json.dumps({"action": args.action, "preview": args.preview, **report}, ensure_ascii=False))
        return
    if args.action == "migrate":
        _prepare_storage(home=home, boxteam_home=boxteam_home)
    gateway_schema_source, workspace_schema_source = _layout_schema_sources(
        project_root
    )

    if args.action == "migrate":
        from configs.maintenance.layout import (
            migrate_legacy_user_configuration,
            migrate_legacy_workspace_configuration,
        )

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
        local_only = _environment_flag(DEV_LOCAL_ONLY_ENV)
        development_root = cast(Path, project_root)
        installation = install_source_development_configuration(
            project_root=development_root,
            config_root=config_root,
            local_only=local_only,
            force=args.force,
            bundled_packages=args.bundled_plugins,
            workspace_root=args.workspace,
        )
        gateway_development_assets = _environment_flag(
            GATEWAY_DEVELOPMENT_ASSETS_ENV
        )
        if gateway_development_assets:
            install_gateway_development_assets(
                project_root=development_root,
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
                    "local_only": local_only,
                    **startup,
                },
                ensure_ascii=False,
            )
        )
        return

    if args.action == "initialize":
        bundled_plugins = install_bundled_plugins(boxteam_home=boxteam_home, packages=args.bundled_plugins)
        installation = install_user_configuration(
            config_root=config_root,
            profile="default",
            project_root=project_root,
            force=args.force,
            bundled_packages=args.bundled_plugins,
            workspace_root=args.workspace,
        )
        print(
            json.dumps(
                {
                    "action": args.action,
                    "installed_bundled_plugins": bundled_plugins,
                    "config_paths": [str(path) for path in installation.config_paths],
                    "schema_paths": [str(path) for path in installation.schema_paths],
                    "created_config_paths": [
                        str(path) for path in installation.created_config_paths
                    ],
                    **startup,
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
            workspace_root=args.workspace,
        )
        from configs.maintenance.runtime_logging_commit import (
            commit_runtime_logging_migration,
        )

        commit_runtime_logging_migration(
            boxteam_home=boxteam_home,
            workspaces=(args.workspace,) if args.workspace is not None else (),
            project_root=project_root,
            gateway_sqlite_path=(Path(os.environ["BOXTEAM_GATEWAY_ROOT"]) / "gateway.sqlite"
                                 if os.environ.get("BOXTEAM_GATEWAY_ROOT") else None),
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
