"""一次性 v1 rollout 到 v2 session 的显式 import 命令。

这个脚本是唯一面向运维的 legacy message-line 入口。正常 workspace/runtime
不会导入 legacy reader；报告模式只读 source，导入模式先写入 target staging
状态，成功后才把 target 置为 active。source artifact 始终保持不变。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 该文件既作为 ``python -m scripts...`` 模块运行，也作为 workspace root
# 下的一次性运维命令直接运行。直接运行时 Python 会把 scripts/ 放在
# sys.path[0]；按项目约定从当前工作目录取得显式仓库根，禁止通过文件
# 位置向上猜测根目录。
if __package__ in {None, ""}:
    _workspace_root = Path.cwd()
    if not (_workspace_root / "app").is_dir():
        raise RuntimeError(
            "legacy_import_v1_to_v2 必须从项目根目录运行，且当前目录缺少 app/"
        )
    sys.path.insert(0, str(_workspace_root))

from app.services.infrastructure.rollout_context.migration.store import (
    LegacyMigrationStorage,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式执行一次性 legacy_import_v1_to_v2 rollout migration"
    )
    parser.add_argument(
        "--sessions-dir",
        required=True,
        type=Path,
        help="workspace .boxteam/sessions 绝对或相对路径",
    )
    parser.add_argument("--source-session-id", required=True)
    parser.add_argument("--target-session-id")
    parser.add_argument("--checkpoint-ns", default="")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="只读取 source 并输出 migration report，不创建 target",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.report_only and not args.target_session_id:
        raise SystemExit("导入模式必须指定 --target-session-id")
    storage = LegacyMigrationStorage(args.sessions_dir)
    if args.report_only:
        result = storage.legacy_migration_report(
            args.source_session_id,
            checkpoint_ns=args.checkpoint_ns,
        )
    else:
        result = storage.migrate_legacy_to_v2(
            args.source_session_id,
            target_thread_id=args.target_session_id,
            checkpoint_ns=args.checkpoint_ns,
        )
    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
