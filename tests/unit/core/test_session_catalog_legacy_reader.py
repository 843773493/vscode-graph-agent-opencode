"""schema v3 会话 catalog 迁移 reader 的只读与 fail-closed 测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_catalog_legacy_reader import (
    SessionCatalogLegacyReader,
    SessionCatalogLegacyReaderError,
)
from app.core.session_tree.support import (
    FOLDER_MANIFEST_NAME,
    SESSION_CHILDREN_DIR_NAME,
    SESSION_MANIFEST_NAME,
)

CREATED_AT = datetime(2026, 6, 1, 12, tzinfo=UTC).isoformat()
UPDATED_AT = datetime(2026, 6, 2, 12, tzinfo=UTC).isoformat()
FOLDER_ID = "fld_1234567890abcdef1234567890abcdef"
ROOT_SESSION_ID = "ses_1234567890abcdef1234567890abcdef"
CHILD_SESSION_ID = "ses_abcdef1234567890abcdef1234567890"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_valid_authority(root: Path) -> tuple[Path, Path]:
    sessions_root = root / "sessions"
    navigation_root = root / "navigation"
    folder_path = sessions_root / FOLDER_ID
    child_session_path = (
        folder_path / ROOT_SESSION_ID / SESSION_CHILDREN_DIR_NAME / CHILD_SESSION_ID
    )
    _write_json(
        folder_path / FOLDER_MANIFEST_NAME,
        {
            "schema_version": 1,
            "folder_id": FOLDER_ID,
            "created_at": CREATED_AT,
        },
    )
    _write_json(
        folder_path / ROOT_SESSION_ID / SESSION_MANIFEST_NAME,
        {
            "session_id": ROOT_SESSION_ID,
            "title": "父会话",
            "created_at": CREATED_AT,
            "updated_at": UPDATED_AT,
            "parent_session_id": None,
        },
    )
    _write_json(
        child_session_path / SESSION_MANIFEST_NAME,
        {
            "session_id": CHILD_SESSION_ID,
            "title": "子会话",
            "created_at": CREATED_AT,
            "updated_at": UPDATED_AT,
            "parent_session_id": ROOT_SESSION_ID,
        },
    )
    index_path = navigation_root / "session-catalog-index.json"
    _write_json(
        index_path,
        {
            "schema_version": 3,
            "revision": 1,
            "nodes": [
                {
                    "node_id": FOLDER_ID,
                    "kind": "folder",
                    "name": "文件夹",
                    "parent_node_id": None,
                    "created_at": CREATED_AT,
                    "updated_at": UPDATED_AT,
                },
                {
                    "node_id": ROOT_SESSION_ID,
                    "kind": "session",
                    "name": "父会话",
                    "parent_node_id": FOLDER_ID,
                    "created_at": CREATED_AT,
                    "updated_at": UPDATED_AT,
                },
                {
                    "node_id": CHILD_SESSION_ID,
                    "kind": "session",
                    "name": "子会话",
                    "parent_node_id": ROOT_SESSION_ID,
                    "created_at": CREATED_AT,
                    "updated_at": UPDATED_AT,
                },
            ],
        },
    )
    return sessions_root, index_path


def _snapshot_files(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_reader_projects_index_without_mutating_source_tree(tmp_path: Path) -> None:
    sessions_root, index_path = _write_valid_authority(tmp_path)
    before = _snapshot_files(tmp_path)

    nodes = SessionCatalogLegacyReader(sessions_root, index_path).read()

    assert [node.node_id for node in nodes] == [
        FOLDER_ID,
        ROOT_SESSION_ID,
        CHILD_SESSION_ID,
    ]
    assert nodes[2].path == (
        sessions_root
        / FOLDER_ID
        / ROOT_SESSION_ID
        / SESSION_CHILDREN_DIR_NAME
        / CHILD_SESSION_ID
    )
    assert _snapshot_files(tmp_path) == before


def test_reader_rejects_unknown_root_directory_without_deleting_it(tmp_path: Path) -> None:
    sessions_root, index_path = _write_valid_authority(tmp_path)
    unknown = sessions_root / "unknown-node"
    unknown.mkdir()
    (unknown / "payload.txt").write_text("保留", encoding="utf-8")
    before = _snapshot_files(tmp_path)

    with pytest.raises(SessionCatalogLegacyReaderError, match="物理容器不一致"):
        SessionCatalogLegacyReader(sessions_root, index_path).read()

    assert unknown.is_dir()
    assert _snapshot_files(tmp_path) == before


def test_reader_rejects_manifest_parent_drift_without_recovery(tmp_path: Path) -> None:
    sessions_root, index_path = _write_valid_authority(tmp_path)
    manifest_path = (
        sessions_root
        / FOLDER_ID
        / ROOT_SESSION_ID
        / SESSION_CHILDREN_DIR_NAME
        / CHILD_SESSION_ID
        / SESSION_MANIFEST_NAME
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parent_session_id"] = None
    _write_json(manifest_path, manifest)
    before = _snapshot_files(tmp_path)

    with pytest.raises(SessionCatalogLegacyReaderError, match="父关系不一致"):
        SessionCatalogLegacyReader(sessions_root, index_path).read()

    assert _snapshot_files(tmp_path) == before
