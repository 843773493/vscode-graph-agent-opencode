"""目标安装域的稳定身份，与 Backend 进程组和运行代际分离。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import portalocker

from app.core.atomic_fs import atomic_write_bytes, native_io_path


def _identity_path(boxteam_home: Path) -> Path:
    return boxteam_home.expanduser().resolve() / "state" / "backend" / "identity.json"


def validate_backend_id(value: object) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Backend 安装域身份必须为标准 UUID")
    return value


def read_backend_id(boxteam_home: Path) -> str:
    """只读目标环境的身份；损坏或缺失均不能重新分配。"""
    path = _identity_path(boxteam_home)
    with native_io_path(path).open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "backend_id"}:
        raise ValueError(f"Backend 安装域身份结构非法: {path}")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError(f"Backend 安装域身份版本不支持: {path}")
    try:
        return validate_backend_id(payload["backend_id"])
    except ValueError as error:
        raise ValueError(f"Backend 安装域身份必须为标准 UUID: {path}") from error


def load_or_create_backend_id(boxteam_home: Path) -> str:
    """同一 BOXTEAM_HOME 只分配一次，跨进程首次创建共用文件锁。"""
    try:
        return read_backend_id(boxteam_home)
    except FileNotFoundError:
        pass
    path = _identity_path(boxteam_home)
    native_io_path(path.parent).mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(native_io_path(path.with_suffix(".lock")), mode="a+b", timeout=10):
        try:
            return read_backend_id(boxteam_home)
        except FileNotFoundError:
            backend_id = str(uuid4())
            atomic_write_bytes(path, (json.dumps({"schema_version": 1, "backend_id": backend_id}) + "\n").encode())
            return backend_id
