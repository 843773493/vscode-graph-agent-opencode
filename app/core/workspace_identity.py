from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

WORKSPACE_IDENTITY_FILE_NAME = "workspace-identity.json"


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


def read_workspace_id(workspace_root: Path) -> str:
    """只读既有目标身份；资源解析不能顺带创建工作区。"""
    return _read_workspace_id(workspace_identity_path(workspace_root))


def _create_workspace_id(identity_path: Path, workspace_id: str | None = None) -> str:
    workspace_id = validate_workspace_id(workspace_id) if workspace_id else str(uuid4())
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


def bind_workspace_id(workspace_root: Path, workspace_id: str) -> str:
    """校验并绑定挂载契约中的 UUID；仅缺少身份文件时按显式 ID 初始化。"""

    expected_id = validate_workspace_id(workspace_id)
    identity_path = workspace_identity_path(workspace_root)
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    if identity_path.is_file():
        actual_id = _read_workspace_id(identity_path)
        if actual_id != expected_id:
            raise RuntimeError(
                "挂载工作区 UUID 与工作区身份文件不一致: "
                f"expected={expected_id}, actual={actual_id}, identity={identity_path}"
            )
        return actual_id
    if identity_path.exists():
        raise RuntimeError(f"Workspace 后端身份路径不是文件: {identity_path}")
    actual_id = _create_workspace_id(identity_path, expected_id)
    if actual_id != expected_id:
        raise RuntimeError(
            "并发初始化得到的 Workspace 后端 UUID 与 mount 契约不一致: "
            f"expected={expected_id}, actual={actual_id}, identity={identity_path}"
        )
    return actual_id


def _absolute_workspace_root(value: str) -> Path:
    workspace_root = Path(value)
    if not workspace_root.is_absolute():
        raise argparse.ArgumentTypeError(
            f"--workspace-root 必须是绝对路径: {value!r}"
        )
    return workspace_root


def _workspace_roots_from_stdin() -> tuple[Path, ...]:
    values = json.load(sys.stdin)
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) for value in values)
    ):
        raise TypeError("Workspace 根目录标准输入必须是非空字符串数组")
    return tuple(_absolute_workspace_root(value) for value in values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="读取或初始化显式 Workspace 的后端 UUID。"
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="只读取既有身份；缺失时直接失败，不初始化目录或 UUID",
    )
    root_options = parser.add_mutually_exclusive_group(required=True)
    root_options.add_argument(
        "--workspace-root",
        action="append",
        type=_absolute_workspace_root,
        help="Workspace 根目录绝对路径；可重复传入以按序读取多个工作区",
    )
    root_options.add_argument(
        "--workspace-roots-stdin",
        action="store_true",
        help="从标准输入读取 Workspace 根目录 JSON 数组",
    )
    args = parser.parse_args(argv)
    workspace_roots = (
        tuple(args.workspace_root)
        if args.workspace_root is not None
        else _workspace_roots_from_stdin()
    )
    identity_reader = (
        read_workspace_id if args.read_only else load_or_create_workspace_id
    )
    print("\n".join(identity_reader(root) for root in workspace_roots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
