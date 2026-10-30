from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISREG
from typing import Never
from uuid import UUID, uuid4

from app.core.atomic_fs import native_io_path
from app.core.failures import report_failure

WORKSPACE_IDENTITY_FILE_NAME = "workspace-identity.json"

def _identity_failure(identity_path: Path, stage: str, reason: str, *, error: BaseException) -> str:
    message = f"{reason}: artifact=workspace_identity, stage={stage}"
    error.add_note(f"Workspace 身份位置: {identity_path}")
    return report_failure(error, detail=message)


def _reject_identity(
    identity_path: Path, stage: str, reason: str, error_type: type[RuntimeError | TypeError] = RuntimeError,
) -> Never:
    error = error_type(reason)
    raise error_type(_identity_failure(identity_path, stage, reason, error=error)) from error


@contextmanager
def _identity_access(identity_path: Path, stage: str) -> Iterator[None]:
    try:
        yield
    except OSError as error:
        raise _identity_io_error(identity_path, stage, error) from error


def _identity_io_error(identity_path: Path, stage: str, error: OSError) -> OSError:
    message = _identity_failure(identity_path, stage,
        f"Workspace 后端身份访问失败: error={type(error).__name__}, errno={error.errno}, winerror={getattr(error, 'winerror', None)}",
        error=error)
    return type(error)(error.errno, message, None, getattr(error, "winerror", None))


def workspace_identity_path(workspace_root: Path) -> Path:
    """返回当前工作区后端身份文件的绝对路径。"""

    try:
        with _identity_access(workspace_root, "resolve"):
            return workspace_root.expanduser().resolve() / ".boxteam" / WORKSPACE_IDENTITY_FILE_NAME
    except RuntimeError as error:
        message = _identity_failure(workspace_root, "resolve", "Workspace 后端身份路径解析失败: error=RuntimeError", error=error)
        raise RuntimeError(message) from error


def validate_workspace_id(workspace_id: str, *, source: Path | None = None) -> str:
    """校验并返回标准 UUID 文本，不接受 Gateway 工作区 ID。"""

    try:
        parsed = UUID(workspace_id)
    except (AttributeError, ValueError) as error:
        failure = error
        message = "Workspace 后端工作区 ID 必须是标准 UUID"
    else:
        normalized = str(parsed)
        if normalized == workspace_id:
            return normalized
        failure = ValueError("Workspace 身份非规范文本")
        message = "Workspace 后端工作区 ID 必须使用标准 UUID 文本"
    failure.add_note(f"Workspace 身份格式拒绝: source={source} value={workspace_id!r}")
    raise RuntimeError(report_failure(failure, detail=message)) from failure


def _read_workspace_id(identity_path: Path) -> str:
    try:
        with _identity_access(identity_path, "read"):
            payload = json.loads(native_io_path(identity_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(_identity_failure(identity_path, "decode",
            f"Workspace 后端身份文件不是有效 JSON: line={error.lineno}, column={error.colno}", error=error)) from error
    except UnicodeDecodeError as error:
        message = _identity_failure(identity_path, "decode", "Workspace 后端身份文件不是有效 UTF-8", error=error)
        raise UnicodeDecodeError(error.encoding, error.object, error.start, error.end, message) from error
    if not isinstance(payload, dict):
        _reject_identity(identity_path, "validate", "Workspace 后端身份文件必须是 JSON object", TypeError)
    workspace_id = payload.get("workspace_id")
    if not isinstance(workspace_id, str):
        _reject_identity(identity_path, "validate", "Workspace 后端身份文件缺少 workspace_id", TypeError)
    return validate_workspace_id(workspace_id, source=identity_path)


def read_workspace_id(workspace_root: Path) -> str:
    """只读既有目标身份；资源解析不能顺带创建工作区。"""
    return _read_workspace_id(workspace_identity_path(workspace_root))


def _create_workspace_id(identity_path: Path, workspace_id: str | None = None) -> str:
    workspace_id = validate_workspace_id(workspace_id) if workspace_id else str(uuid4())
    try:
        descriptor = os.open(
            native_io_path(identity_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return _read_workspace_id(identity_path)
    except OSError as error:
        raise _identity_io_error(identity_path, "create", error) from error

    primary_error: BaseException | None = None
    try:
        with _identity_access(identity_path, "flush"):
            file = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None  # stream创建成功才交付关闭责任，失败仍由本次创建负责。
            with file:
                json.dump({"workspace_id": workspace_id}, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_error = _identity_io_error(identity_path, "close_descriptor", error)
                cleanup_error.__cause__ = error
                if primary_error is not None:
                    raise BaseExceptionGroup(
                        "Workspace 身份创建与收尾均失败: artifact=workspace_identity, stage=close_descriptor",
                        [primary_error, cleanup_error],
                    ) from primary_error
                raise cleanup_error from error
    return workspace_id


def _read_existing_workspace_id(identity_path: Path) -> str | None:
    with _identity_access(identity_path, "stat"):
        try:
            identity_stat = native_io_path(identity_path).stat()
        except FileNotFoundError:
            return None
    if not S_ISREG(identity_stat.st_mode):
        _reject_identity(identity_path, "validate", "Workspace 后端身份路径不是文件")
    return _read_workspace_id(identity_path)


def load_or_create_workspace_id(workspace_root: Path) -> str:
    """为工作区创建一次后端 UUID，并在后续启动中复用它。"""

    identity_path = workspace_identity_path(workspace_root)
    with _identity_access(identity_path, "directory"):
        native_io_path(identity_path.parent).mkdir(parents=True, exist_ok=True)
    existing_id = _read_existing_workspace_id(identity_path)
    return existing_id if existing_id is not None else _create_workspace_id(identity_path)


def bind_workspace_id(workspace_root: Path, workspace_id: str) -> str:
    """校验并绑定挂载契约中的 UUID；仅缺少身份文件时按显式 ID 初始化。"""

    expected_id = validate_workspace_id(workspace_id)
    identity_path = workspace_identity_path(workspace_root)
    with _identity_access(identity_path, "directory"):
        native_io_path(identity_path.parent).mkdir(parents=True, exist_ok=True)
    existing_id = _read_existing_workspace_id(identity_path)
    if existing_id is not None:
        actual_id = existing_id
        if actual_id != expected_id:
            _reject_identity(identity_path, "bind",
                f"挂载工作区 UUID 与工作区身份文件不一致: expected={expected_id}, actual={actual_id}")
        return actual_id
    actual_id = _create_workspace_id(identity_path, expected_id)
    if actual_id != expected_id:
        _reject_identity(identity_path, "bind",
            f"并发初始化得到的 Workspace 后端 UUID 与 mount 契约不一致: expected={expected_id}, actual={actual_id}")
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
