from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.core.session_paths import SessionPathResolver
from app.schemas.public_v2.message import MessageRunAccepted


class SessionMessageIdempotencyStore:
    """在目标会话节点内保存跨会话消息的已接受结果。"""

    _FILE_NAME = "inter-agent-idempotency.json"

    def __init__(self, *, path_resolver: SessionPathResolver) -> None:
        self._path_resolver = path_resolver

    def get(self, session_id: str, idempotency_key: str) -> MessageRunAccepted | None:
        path = self._path(session_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"会话幂等索引必须是对象: {path}")
        value = data.get(idempotency_key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TypeError(f"会话幂等索引记录必须是对象: {path}")
        return MessageRunAccepted.model_validate(value)

    def put(
        self,
        session_id: str,
        idempotency_key: str,
        result: MessageRunAccepted,
    ) -> None:
        path = self._path(session_id)
        data: dict[str, object]
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise TypeError(f"会话幂等索引必须是对象: {path}")
            data = {str(key): value for key, value in loaded.items()}
        else:
            data = {}
        data[idempotency_key] = result.model_dump(mode="json")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(data, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, path)
        finally:
            temporary_path = Path(temporary_name)
            if temporary_path.exists():
                temporary_path.unlink()

    def _path(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node(session_id)
            / self._FILE_NAME
        )


__all__ = ["SessionMessageIdempotencyStore"]
