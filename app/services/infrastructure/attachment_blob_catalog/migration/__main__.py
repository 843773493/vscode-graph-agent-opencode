"""显式准备或核验附件目录迁移备份，不切换线上存储。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.infrastructure.attachment_blob_catalog.migration.backup import (
    prepare_backup,
    verify_backup,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="停机后准备 schema 3 的只读一致备份")
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="核验已完成备份，不访问线上工作区")
    verify.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    result = prepare_backup(args.workspace, args.output) if args.command == "prepare" else verify_backup(args.backup)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
