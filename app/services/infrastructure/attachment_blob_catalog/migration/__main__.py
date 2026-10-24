"""显式准备、核验、安装或撤销附件目录迁移。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.infrastructure.attachment_blob_catalog.migration.backup import (
    prepare_backup,
    verify_backup,
)
from app.services.infrastructure.attachment_blob_catalog.migration.catalog import (
    prepare_converted_catalog,
    verify_converted_catalog,
)
from app.services.infrastructure.attachment_blob_catalog.migration.directories import (
    convert_backup,
    verify_directories,
)
from app.services.infrastructure.attachment_blob_catalog.migration.install import (
    install_converted_catalog,
    rollback_install,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="停机后准备 schema 3 的只读一致备份")
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="核验已完成备份，不访问线上工作区")
    verify.add_argument("--backup", type=Path, required=True)
    convert = commands.add_parser("convert", help="在已核验备份内准备独立目录；可重复执行以续跑")
    convert.add_argument("--backup", type=Path, required=True)
    directories = commands.add_parser("verify-directories", help="核验已准备目录与来源备份，不切换线上存储")
    directories.add_argument("--backup", type=Path, required=True)
    catalog = commands.add_parser("prepare-catalog", help="从已核验目录准备隔离的 schema 4 catalog")
    catalog.add_argument("--backup", type=Path, required=True)
    verify_catalog = commands.add_parser("verify-catalog", help="核验目标 catalog 与冻结旧引用，不切换线上 owner")
    verify_catalog.add_argument("--backup", type=Path, required=True)
    for name, help_text in (("install", "停机安装已核验目录，沿原记录继续中断操作"), ("rollback", "撤销未被后续写入的安装并保全两份目录")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_backup(args.workspace, args.output)
    elif args.command in {"install", "rollback"}:
        result = {"install": install_converted_catalog, "rollback": rollback_install}[args.command](args.workspace, args.backup)
    else:
        result = {"verify": verify_backup, "convert": convert_backup, "verify-directories": verify_directories,
                  "prepare-catalog": prepare_converted_catalog, "verify-catalog": verify_converted_catalog}[args.command](args.backup)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
