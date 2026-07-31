from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.path_utils import get_session_path_resolver
from app.schemas.public_v2.pending_request import (
    PendingRequestDTO,
    PendingRequestSummaryDTO,
    PendingRequestSummaryListDTO,
)

_PENDING_HEADER_VERSION = 1
_PENDING_HEADER_MAX_BYTES = 64 * 1024
_PENDING_SUMMARY_LIMIT = 8


class _PendingRequestHeader(BaseModel):
    version: int = _PENDING_HEADER_VERSION
    request_count: int = Field(ge=0)
    summaries: list[PendingRequestSummaryDTO] = Field(default_factory=list)


class PendingRequestStore:
    """在会话目录内持久化尚未执行的请求。"""

    def __init__(self, *, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._path_resolver = get_session_path_resolver(sessions_dir)

    def _path(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node(session_id)
            / "pending_requests.json"
        )

    async def load(self, session_id: str) -> list[PendingRequestDTO]:
        path = self._path(session_id)
        if not path.exists():
            return []
        payload = await asyncio.to_thread(self._read_detail, path)
        if not isinstance(payload, list):
            raise TypeError(f"待处理消息文件必须是 JSON 数组: {path}")
        return [PendingRequestDTO.model_validate(item) for item in payload]

    async def migrate_schema(self, session_id: str) -> None:
        path = self._path(session_id)
        if not path.exists():
            return
        if await asyncio.to_thread(self._is_current_schema, path):
            return
        requests = await asyncio.to_thread(self._read_legacy_requests, path)
        content = self._serialize_current(requests)
        await asyncio.to_thread(self._atomic_write, path, content)

    async def migrate_all(self) -> int:
        migrated = 0
        for node in self._path_resolver.list_nodes():
            if node.kind != "session":
                continue
            path = self._path(node.node_id)
            if not path.exists() or await asyncio.to_thread(
                self._is_current_schema,
                path,
            ):
                continue
            await self.migrate_schema(node.node_id)
            migrated += 1
        return migrated

    async def load_summaries(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> PendingRequestSummaryListDTO:
        if limit < 1 or limit > _PENDING_SUMMARY_LIMIT:
            raise ValueError(
                f"待处理摘要 limit 必须在 1..{_PENDING_SUMMARY_LIMIT} 范围内"
            )
        path = self._path(session_id)
        if not path.exists():
            return PendingRequestSummaryListDTO(
                session_id=session_id,
                request_count=0,
            )
        header = await asyncio.to_thread(self._read_header, path)
        items = header.summaries[:limit]
        return PendingRequestSummaryListDTO(
            session_id=session_id,
            requests=items,
            request_count=header.request_count,
            truncated=len(items) < header.request_count,
        )

    async def save(
        self,
        session_id: str,
        requests: list[PendingRequestDTO],
    ) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self._serialize_current(requests)
        await asyncio.to_thread(self._atomic_write, path, content)

    @staticmethod
    def _serialize_current(requests: list[PendingRequestDTO]) -> bytes:
        detail = json.dumps(
            [item.model_dump(mode="json") for item in requests],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        header = _PendingRequestHeader(
            request_count=len(requests),
            summaries=[
                PendingRequestSummaryDTO(
                    job_id=item.job_id,
                    message_id=item.message_id,
                    updated_at=item.updated_at,
                )
                for item in requests[:_PENDING_SUMMARY_LIMIT]
            ],
        )
        header_line = header.model_dump_json().encode("utf-8") + b"\n"
        if len(header_line) > _PENDING_HEADER_MAX_BYTES:
            raise RuntimeError(
                "待处理摘要 header 超过固定上限: "
                f"bytes={len(header_line)}, max={_PENDING_HEADER_MAX_BYTES}"
            )
        return header_line + detail.encode("utf-8") + b"\n"

    async def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            await asyncio.to_thread(path.unlink)

    @staticmethod
    def _read_header(path: Path) -> _PendingRequestHeader:
        with path.open("rb") as stream:
            line = stream.readline(_PENDING_HEADER_MAX_BYTES + 1)
        if not line.endswith(b"\n") or len(line) > _PENDING_HEADER_MAX_BYTES:
            raise RuntimeError(f"待处理摘要 header 损坏或超过上限: {path}")
        header = _PendingRequestHeader.model_validate_json(line)
        if header.version != _PENDING_HEADER_VERSION:
            raise RuntimeError(
                f"待处理摘要 header 版本不支持: {path}, version={header.version}"
            )
        if len(header.summaries) > _PENDING_SUMMARY_LIMIT:
            raise RuntimeError(f"待处理摘要 header 条目超过上限: {path}")
        return header

    @classmethod
    def _is_current_schema(cls, path: Path) -> bool:
        try:
            cls._read_header(path)
        except (RuntimeError, ValueError):
            return False
        return True

    @staticmethod
    def _read_legacy_requests(path: Path) -> list[PendingRequestDTO]:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise TypeError(f"旧待处理消息文件必须是 JSON 数组: {path}")
        return [PendingRequestDTO.model_validate(item) for item in payload]

    @classmethod
    def _read_detail(cls, path: Path) -> object:
        cls._read_header(path)
        with path.open("rb") as stream:
            stream.readline(_PENDING_HEADER_MAX_BYTES + 1)
            raw = stream.read()
        if not raw.endswith(b"\n"):
            raise RuntimeError(f"待处理消息 detail 不完整: {path}")
        return json.loads(raw)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
