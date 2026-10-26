"""显式配置迁移的启动屏障；恢复操作只由配置安装器执行。"""

from __future__ import annotations

import json
from pathlib import Path


def config_migration_marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.runtime-logging-migration.json")


def require_settled_config_migration(path: Path) -> None:
    marker = config_migration_marker(path)
    if not marker.exists():
        return
    pointer = json.loads(marker.read_text(encoding="utf-8"))
    journal_path = Path(pointer["journal"])
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal["state"] not in {"completed", "restored"}:
        raise RuntimeError(
            "日志配置迁移尚未完成，禁止读取部分迁移状态；停止相关服务后执行 "
            "uv run python -m configs.boxteam migrate-runtime-logging 继续，"
            "或加 --restore 恢复原件。"
        )
