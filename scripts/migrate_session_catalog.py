"""一次性会话目录迁移 runner（8.2 显式 operator 入口，R19）。

把旧 ``session-catalog-index.json`` + 嵌套 Session/Folder/children 物理树
一次性迁移到 SQLite catalog 权威（``navigation/session-catalog.sqlite`` +
日期桶物理布局 + per-session session-control 初始化）。这是面向运维的
显式一次性维护命令：必须在 workspace maintenance 窗口内单人执行
（先 quiesce execution/communication/attachment 等 mutation）。

用法::

    uv run python scripts/migrate_session_catalog.py --workspace-root /abs/path/to/workspace
    uv run python scripts/migrate_session_catalog.py --workspace-root /abs/path --json

行为：

- ``--workspace-root`` 必填，指向工作区根目录（其下 ``.boxteam/`` 为
  业务数据根）；``--workspace-id`` 可选（默认读/建
  ``.boxteam/workspace-identity.json``，与生产 resolver 同源）；
- 成功打印迁移结果（session/folder 计数、quarantine 清单、journal
  路径），退出码 0；``--json`` 输出机器可读 JSON；
- 失败（含迁移 fail-closed）打印明确错误到 stderr 并以非零退出码结束，
  绝不静默、不重试、不扫盘吸收。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 该文件作为 workspace root 下的一次性运维命令直接运行。直接运行时
# Python 会把 scripts/ 放在 sys.path[0]；按项目约定从当前工作目录取得
# 显式仓库根，禁止通过文件位置向上猜测根目录。
if __package__ in {None, ""}:
    _repo_root = Path.cwd()
    if not (_repo_root / "app").is_dir():
        raise RuntimeError(
            "migrate_session_catalog 必须从项目根目录运行，且当前目录缺少 app/"
        )
    sys.path.insert(0, str(_repo_root))

from app.core.session_catalog_migration import migrate_workspace_session_catalog
from app.core.workspace_identity import (
    WORKSPACE_IDENTITY_FILE_NAME,
    validate_workspace_id,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "显式执行一次性会话目录迁移"
            "（旧 JSON index + 物理树 → SQLite catalog 权威，OpenSpec 8.2）"
        )
    )
    parser.add_argument(
        "--workspace-root",
        required=True,
        type=Path,
        help="工作区根目录绝对或相对路径（其下 .boxteam/ 为业务数据根）",
    )
    parser.add_argument(
        "--workspace-id",
        default=None,
        help=(
            "显式工作区 ID（标准 UUID 文本）。默认自动读取"
            f" .boxteam/{WORKSPACE_IDENTITY_FILE_NAME}（与生产 resolver 同源）；"
            "显式指定必须与该文件一致，否则迁移产物无法被生产 resolver 使用"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以机器可读 JSON 输出迁移结果",
    )
    return parser


def _check_workspace_id(workspace_root: Path, workspace_id: str) -> None:
    """快速失败：显式 workspace_id 必须是标准 UUID 且与 identity 文件一致。

    生产 resolver 一律经 identity API 取 workspace_id；显式指定与既有
    identity 文件不一致的 ID 会产出无法被生产使用的 catalog，必须在此
    拒绝（不静默、不事后补救）。
    """
    normalized = validate_workspace_id(workspace_id)
    identity_path = workspace_root / ".boxteam" / WORKSPACE_IDENTITY_FILE_NAME
    if not identity_path.is_file():
        raise SystemExit(
            f"工作区尚无 identity 文件（{identity_path}）：请先启动一次工作区"
            "后端或省略 --workspace-id（默认自动读取/创建），不要显式指定"
            "与生产 identity 脱节的 ID"
        )
    try:
        identity_payload = json.loads(identity_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(
            f"identity 文件无法解析: {identity_path}: {error}"
        ) from error
    identity_id = (
        identity_payload.get("workspace_id")
        if isinstance(identity_payload, dict)
        else None
    )
    if identity_id != normalized:
        raise SystemExit(
            "显式 --workspace-id 与 identity 文件不一致，拒绝迁移"
            f"（迁移产物将无法被生产 resolver 使用）: given={normalized!r}, "
            f"identity={identity_id!r}, file={identity_path}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace_root = args.workspace_root.expanduser().resolve()
    if not workspace_root.is_dir():
        raise SystemExit(f"workspace-root 不存在或不是目录: {workspace_root}")
    if args.workspace_id is not None:
        _check_workspace_id(workspace_root, args.workspace_id)
    try:
        result = asyncio.run(
            migrate_workspace_session_catalog(
                workspace_root=workspace_root,
                workspace_id=args.workspace_id,
            )
        )
    except Exception as error:  # noqa: BLE001 —— CLI 边界：任何失败都打印完整错误（含类型与详情）并以非零退出，绝不静默吞掉；不重试、不降级。
        print(
            f"会话目录迁移失败（旧树与隔离区保持原样，供人工核账）: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    payload = {
        "workspace_root": str(workspace_root),
        "migrated_session_nodes": result.migrated_session_nodes,
        "migrated_folder_nodes": result.migrated_folder_nodes,
        "quarantined_nodes": [
            {"node_id": item.node_id, "reason": item.reason}
            for item in result.quarantined_nodes
        ],
        "journal_path": str(result.journal_path),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"会话目录迁移完成: workspace_root={workspace_root}")
        print(f"  迁移 session 节点: {result.migrated_session_nodes}")
        print(f"  迁移 folder 节点: {result.migrated_folder_nodes}")
        print(f"  quarantine 节点: {len(result.quarantined_nodes)}")
        for item in result.quarantined_nodes:
            print(f"    - {item.node_id}（原因: {item.reason}）")
        print(f"  迁移 journal: {result.journal_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
