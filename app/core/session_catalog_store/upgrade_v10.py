"""显式复制并转换 v10 catalog；原库不变，不自动安装或接管运行中的 owner。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import fields
from pathlib import Path

from app.core.file_permissions import restrict_private_path_access
from app.core.session_catalog_store.contracts import (
    ForkCapturedPoint,
    ForkSourceCaptureRecord,
)
from app.core.session_catalog_store.fork_source_capture import (
    _validate_capture_identity,
)
from app.core.session_catalog_store.queries import _register_session_list_functions
from app.core.session_catalog_store.schema_validation import require_catalog_schema
from app.core.session_catalog_store.sqlite_integrity import (
    verify_catalog_sqlite_integrity,
)
from app.core.sqlite_state import load_sql, sqlite_file_uri


def _freeze_old_point(request_json: str, preimage_hash: str, snapshot_hash: str | None, marker_hash: str | None) -> str:
    request = json.loads(request_json)
    return ForkCapturedPoint(
        source_view_id=request["source_view_id"], source_view_revision=request["source_view_revision"],
        anchor_item_id=request.get("anchor_item_id"),
    ).frozen_json(preimage_hash=preimage_hash, snapshot_sha256=snapshot_hash, marker_sha256=marker_hash)


def convert_catalog_v10(source: Path, destination: Path) -> None:
    """只转换一份一致副本；无法恢复精确旧坐标时整次回滚并保留原库。"""
    if not source.is_absolute() or not destination.is_absolute():
        raise ValueError("catalog 离线转换必须显式提供绝对路径")
    if source.is_symlink() or not source.is_file():
        raise ValueError("catalog 转换来源必须是既有普通文件")
    with destination.open("xb"):
        pass
    try:
        restrict_private_path_access(destination, is_directory=False)
        with closing(sqlite3.connect(sqlite_file_uri(source) + "?mode=ro", uri=True)) as original:
            original.row_factory = sqlite3.Row
            _register_session_list_functions(original)
            original.execute("BEGIN")
            if original.execute("PRAGMA user_version").fetchone()[0] != 10:
                raise ValueError("本转换只支持完整 v10 catalog")
            require_catalog_schema(original, database_path=source, current=10, database_label="conversion source")
            verify_catalog_sqlite_integrity(original, database_path=source, database_label="conversion source")
            for row in original.execute("SELECT * FROM fork_source_captures"):
                request = json.loads(row["request_json"])
                if not isinstance(request, dict):
                    raise TypeError("旧 fork capture request 必须是对象")
                point = _freeze_old_point(row["request_json"], row["preimage_hash"], row["snapshot_sha256"], row["marker_sha256"]) if (
                    request.get("source_view_id") is not None and request.get("source_view_revision") is not None
                ) else None
                _validate_capture_identity(ForkSourceCaptureRecord(**{
                    field.name: point if field.name == "source_point_json" else row[field.name]
                    for field in fields(ForkSourceCaptureRecord)
                }))
            with closing(sqlite3.connect(destination)) as converted:
                converted.row_factory = sqlite3.Row
                _register_session_list_functions(converted)
                converted.create_function("frozen_fork_point", 4, _freeze_old_point, deterministic=True)
                original.backup(converted)
                converted.execute("BEGIN IMMEDIATE")
                try:
                    for statement in load_sql(__package__, "sql/upgrade_v10.sql").split(";"):
                        if statement.strip():
                            converted.execute(statement)
                    for row in converted.execute("SELECT * FROM fork_source_captures"):
                        _validate_capture_identity(ForkSourceCaptureRecord(**{
                            field.name: row[field.name] for field in fields(ForkSourceCaptureRecord)
                        }))
                    require_catalog_schema(converted, database_path=destination, current=11, database_label="converted catalog")
                    verify_catalog_sqlite_integrity(converted, database_path=destination, database_label="converted catalog")
                except BaseException:
                    converted.rollback()
                    raise
                converted.commit()
    except BaseException:
        # 仅移除本次以独占创建取得的失败输出，原库及其 WAL 从未写入。
        destination.unlink()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    convert_catalog_v10(args.source, args.destination)


if __name__ == "__main__":
    main()
