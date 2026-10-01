from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.message import MessageRunAccepted
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutOperationLock,
)


class SessionMessageIdempotencyStore:
    """在目标会话节点内保存跨会话消息的已接受结果。"""

    _FILE_NAME = "inter-agent-idempotency.json"
    _LOCK_FILE_NAME = ".inter-agent-idempotency.lock"

    def __init__(self, *, path_resolver: SessionCatalogPathResolver) -> None:
        self._path_resolver = path_resolver

    def get(self, session_id: str, idempotency_key: str) -> MessageRunAccepted | None:
        path = self._path(session_id)
        # 与 put 的读改写共用同一把跨进程锁：读路径可能与其他执行根的在途
        # 写在 os.replace 之前读到旧文件，持锁消除该 TOCTOU 窗口。
        with self._lock(session_id):
            data = self._read(path)
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
        # 读改写必须整体持跨进程锁：GUI/worker/CLI 等执行根各自持有独立连接
        # 时，两个 put 并发执行会各自读到同一份旧文件、各自写入，后落盘的
        # os.replace 覆盖前者，丢失其 idempotency_key 记录（lost update）。
        with self._lock(session_id):
            data = self._read(path)
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

    def _read(self, path: Path) -> dict[str, object]:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"会话幂等索引必须是对象: {path}")
        return {str(key): value for key, value in data.items()}

    def _lock(self, session_id: str) -> _RolloutOperationLock:
        # 同一进程可重入、跨进程独占，且带 10s 有界超时（失联写者不会永久阻塞）。
        path = self._path(session_id).parent / self._LOCK_FILE_NAME
        return _RolloutOperationLock(path)

    def _path(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node(session_id)
            / self._FILE_NAME
        )


__all__ = ["SessionMessageIdempotencyStore"]
