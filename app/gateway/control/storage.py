from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def read_json_object(path: Path, *, default: dict[str, object]) -> dict[str, object]:
    if not path.exists():
        return dict(default)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Gateway 控制面文件必须是 JSON 对象: {path}")
    return value


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        # TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
        if os.name != "nt":
            temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
