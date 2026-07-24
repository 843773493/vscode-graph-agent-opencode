from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.core.session_tree.support import (
    FOLDER_MANIFEST_NAME,
    PHYSICAL_LAYOUT_VERSION,
    SESSION_ALLOCATION_MARKER_NAME,
    SESSION_MANIFEST_NAME,
    SessionPhysicalNode,
    _atomic_write_json,
    _parse_datetime,
    _process_matches_identity,
    _read_json_object,
    display_name_from_segment,
)


def recover_allocation_marker(
    session_dir: Path,
    *,
    has_manifest: bool,
) -> bool:
    """恢复崩溃遗留标记；返回 True 表示整个空分配目录已回收。"""
    marker_path = session_dir / SESSION_ALLOCATION_MARKER_NAME
    marker = _read_json_object(marker_path)
    session_id = marker.get("session_id")
    pid = marker.get("pid")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError(f"会话分配标记缺少 session_id: {marker_path}")
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError(f"会话分配标记缺少合法 pid: {marker_path}")
    if _process_matches_identity(pid, marker.get("process_identity")):
        return False
    if has_manifest:
        manifest = _read_json_object(session_dir / SESSION_MANIFEST_NAME)
        if manifest.get("session_id") != session_id:
            raise RuntimeError(
                "崩溃遗留会话分配标记与 manifest ID 不一致: "
                f"marker={session_id}, manifest={manifest.get('session_id')}, "
                f"path={session_dir}"
            )
        marker_path.unlink()
        return False
    unmanaged_entries = [entry for entry in session_dir.iterdir() if entry != marker_path]
    if unmanaged_entries:
        raise RuntimeError(
            "发现崩溃遗留且包含未完成数据的会话分配目录，拒绝静默忽略: "
            f"session_id={session_id}, path={session_dir}, "
            f"entries={','.join(str(entry) for entry in unmanaged_entries)}"
        )
    marker_path.unlink()
    session_dir.rmdir()
    return True

def nearest_session_ancestor_from_nodes(
    parent_node_id: str | None,
    nodes: dict[str, SessionPhysicalNode],
) -> str | None:
    current_id = parent_node_id
    visited: set[str] = set()
    while current_id is not None:
        if current_id in visited:
            raise RuntimeError(f"物理目录索引包含循环: {current_id}")
        visited.add(current_id)
        current = nodes.get(current_id)
        if current is None:
            raise RuntimeError(f"物理会话节点父节点不存在: {current_id}")
        if current.kind == "session":
            return current.node_id
        current_id = current.parent_node_id
    return None

def read_folder_node(
    path: Path,
    manifest_path: Path,
    parent_node_id: str | None,
) -> SessionPhysicalNode:
    raw = _read_json_object(manifest_path)
    if raw.get("schema_version") != PHYSICAL_LAYOUT_VERSION:
        raise RuntimeError(f"不支持的会话文件夹 manifest 版本: {manifest_path}")
    folder_id = raw.get("folder_id")
    if not isinstance(folder_id, str) or not folder_id:
        raise RuntimeError(f"会话文件夹 manifest 缺少 folder_id: {manifest_path}")
    created_at = _parse_datetime(raw.get("created_at"), manifest_path)
    stat = path.stat()
    return SessionPhysicalNode(
        node_id=folder_id,
        kind="folder",
        path=path,
        parent_node_id=parent_node_id,
        name=display_name_from_segment(path.name, folder_id),
        created_at=created_at,
        updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )

def read_session_node(
    path: Path,
    manifest_path: Path,
    parent_node_id: str | None,
) -> SessionPhysicalNode:
    raw = _read_json_object(manifest_path)
    session_id = raw.get("session_id")
    title = raw.get("title")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError(f"会话 manifest 缺少 session_id: {manifest_path}")
    if not isinstance(title, str):
        raise RuntimeError(f"会话 manifest 缺少 title: {manifest_path}")
    return SessionPhysicalNode(
        node_id=session_id,
        kind="session",
        path=path,
        parent_node_id=parent_node_id,
        name=title,
        created_at=_parse_datetime(raw.get("created_at"), manifest_path),
        updated_at=_parse_datetime(raw.get("updated_at"), manifest_path),
    )

def write_folder_manifest(
    folder_path: Path,
    *,
    folder_id: str,
    created_at: datetime,
) -> None:
    _atomic_write_json(
        folder_path / FOLDER_MANIFEST_NAME,
        {
            "schema_version": PHYSICAL_LAYOUT_VERSION,
            "folder_id": folder_id,
            "created_at": created_at.isoformat(),
        },
    )
