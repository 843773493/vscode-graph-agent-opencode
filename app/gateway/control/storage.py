from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from app.core.file_permissions import restrict_private_path_access

logger = logging.getLogger(__name__)


def read_json_object(path: Path, *, default: dict[str, object]) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(default)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Gateway 控制面文件必须是 JSON 对象: {path}")
    return payload


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    write_error: Exception | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            restrict_private_path_access(
                temporary_path,
                descriptor=file.fileno(),
            )
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except Exception as error:
        write_error = error
        raise
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except Exception as cleanup_error:
            if write_error is None:
                raise
            write_error.add_note(
                f"临时文件清理失败: {temporary_path}: {cleanup_error!r}"
            )
            logger.warning(
                "Gateway JSON 写入失败后的临时文件清理也失败: path=%s, error=%s",
                temporary_path,
                cleanup_error,
                exc_info=cleanup_error,
            )
