from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

WORKSPACE_IDENTITY_FILE_NAME = "workspace-identity.json"
# TODO: 旧版本固定后端 ID 的存量迁移完成后，移除这些仅用于一次性修复的值。
LEGACY_BACKEND_WORKSPACE_IDS = frozenset({"ws_local", "ws_custom_tool_fixture"})


def workspace_identity_path(workspace_root: Path) -> Path:
    """返回当前工作区后端身份文件的绝对路径。"""

    return workspace_root.expanduser().resolve() / ".boxteam" / WORKSPACE_IDENTITY_FILE_NAME


def validate_workspace_id(workspace_id: str, *, source: Path | None = None) -> str:
    """校验并返回标准 UUID 文本，不接受 Gateway 工作区 ID。"""

    try:
        parsed = UUID(workspace_id)
    except (AttributeError, ValueError) as error:
        location = f": {source}" if source is not None else ""
        raise RuntimeError(
            f"Workspace 后端工作区 ID 必须是标准 UUID{location}: {workspace_id!r}"
        ) from error
    normalized = str(parsed)
    if normalized != workspace_id:
        location = f": {source}" if source is not None else ""
        raise RuntimeError(
            f"Workspace 后端工作区 ID 必须使用标准 UUID 文本{location}: {workspace_id!r}"
        )
    return normalized


def _read_workspace_id(identity_path: Path) -> str:
    try:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Workspace 后端身份文件不是有效 JSON: {identity_path}"
        ) from error
    if not isinstance(payload, dict):
        raise TypeError(f"Workspace 后端身份文件必须是 JSON object: {identity_path}")
    workspace_id = payload.get("workspace_id")
    if not isinstance(workspace_id, str):
        raise TypeError(
            f"Workspace 后端身份文件缺少 workspace_id: {identity_path}"
        )
    return validate_workspace_id(workspace_id, source=identity_path)


def _create_workspace_id(identity_path: Path) -> str:
    workspace_id = str(uuid4())
    try:
        descriptor = os.open(
            identity_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return _read_workspace_id(identity_path)

    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(
            {"workspace_id": workspace_id},
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    return workspace_id


def load_or_create_workspace_id(workspace_root: Path) -> str:
    """为工作区创建一次后端 UUID，并在后续启动中复用它。"""

    identity_path = workspace_identity_path(workspace_root)
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    if identity_path.is_file():
        return _read_workspace_id(identity_path)
    if identity_path.exists():
        raise RuntimeError(f"Workspace 后端身份路径不是文件: {identity_path}")
    return _create_workspace_id(identity_path)
