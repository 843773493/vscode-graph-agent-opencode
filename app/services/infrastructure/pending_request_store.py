from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.schemas.public_v2.pending_request import PendingRequestDTO


class PendingRequestStore:
    """在会话目录内持久化尚未执行的请求。"""

    def __init__(self, *, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir

    def _path(self, session_id: str) -> Path:
        return self._sessions_dir / session_id / "pending_requests.json"

    async def load(self, session_id: str) -> list[PendingRequestDTO]:
        path = self._path(session_id)
        if not path.exists():
            return []
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise TypeError(f"待处理消息文件必须是 JSON 数组: {path}")
        return [PendingRequestDTO.model_validate(item) for item in payload]

    async def save(
        self,
        session_id: str,
        requests: list[PendingRequestDTO],
    ) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            [item.model_dump(mode="json") for item in requests],
            ensure_ascii=False,
            indent=2,
        )
        temporary = path.with_suffix(".tmp")
        await asyncio.to_thread(temporary.write_text, content + "\n", encoding="utf-8")
        await asyncio.to_thread(temporary.replace, path)

    async def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            await asyncio.to_thread(path.unlink)
