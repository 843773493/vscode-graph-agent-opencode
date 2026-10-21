from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.pending_request import (
    PendingRequestControl,
    PendingRequestDTO,
    PendingRequestSummaryDTO,
    PendingRequestSummaryListDTO,
)

_PENDING_HEADER_VERSION = 5
_PENDING_HEADER_MAX_BYTES = 64 * 1024
_PENDING_SUMMARY_LIMIT = 8


class _PendingRequestHeader(BaseModel):
    version: int = _PENDING_HEADER_VERSION
    request_count: int = Field(ge=0)
    snapshot_version: int = Field(default=0, ge=0)
    summaries: list[PendingRequestSummaryDTO] = Field(default_factory=list)


class _StoredPendingRequest(PendingRequestControl):
    # Provider 选择属于调度，不属于不可变用户正文。
    provider_id: str | None = None


class _PendingControlError(RuntimeError):
    """本 owner 格式检查的固定规则，不携带磁盘位置或未校验输入。"""

    def __init__(self, rule: str) -> None:
        self.rule = rule
        super().__init__(rule)


class PendingRequestStore:
    """在会话目录内持久化 FIFO 调度控制，正文只读 canonical owner。"""

    def __init__(
        self, *, sessions_dir: Path,
        input_reader: Callable[[PendingRequestControl], MessageDTO],
    ) -> None:
        self._sessions_dir = sessions_dir
        self._path_resolver = get_session_path_resolver(sessions_dir)
        self._input_reader = input_reader

    def _path(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node_for_runtime(session_id)
            / "pending_requests.json"
        )

    async def load(self, session_id: str) -> list[PendingRequestDTO]:
        path = self._existing_path(session_id)
        if path is None:
            return []
        try:
            with self._access(session_id, "read_control"):
                _header, references = await asyncio.to_thread(self._read_control_snapshot, session_id, path)
            with self._access(session_id, "project_input"):
                requests = [self._project_request(reference) for reference in references]
        except Exception as error:
            raise RuntimeError(f"待处理队列恢复失败: {error}") from error
        return requests

    def _project_request(self, reference: _StoredPendingRequest) -> PendingRequestDTO:
        message = self._input_reader(reference)
        if (message.session_id, message.thread_id, message.message_id) != (
            reference.session_id, reference.thread_id, reference.message_id,
        ):
            raise RuntimeError("待处理输入投影与持久队列引用不一致")
        return PendingRequestDTO(
            **reference.model_dump(exclude={"provider_id"}),
            content=message.content, attachments=message.attachments,
            message_created_at=message.created_at.isoformat(),
            message_metadata={**message.metadata, "boxteam_session_provider_id": reference.provider_id},
        )

    async def migrate_schema(self, session_id: str) -> None:
        path = self._existing_path(session_id)
        if path is None:
            return
        with self._access(session_id, "migrate"):
            if await asyncio.to_thread(self._is_current_schema, path):
                return
        raise RuntimeError(
            "检测到旧 pending kind 元数据；新 FIFO 实现不提供兼容迁移，"
            f"请在启用新实现前清理该队列文件: session_id={session_id}, "
            "artifact=pending_requests, stage=migrate"
        )

    async def migrate_all(self) -> int:
        migrated = 0
        for node in self._path_resolver.list_nodes():
            if node.kind != "session":
                continue
            path = self._existing_path(node.node_id)
            if path is None:
                continue
            with self._access(node.node_id, "migrate"):
                if await asyncio.to_thread(self._is_current_schema, path):
                    continue
            await self.migrate_schema(node.node_id)
            migrated += 1
        return migrated

    async def load_summaries(
        self,
        session_id: str,
        *,
        limit: int,
        thread_id: str | None = None,
    ) -> PendingRequestSummaryListDTO:
        if limit < 1 or limit > _PENDING_SUMMARY_LIMIT:
            raise ValueError(
                f"待处理摘要 limit 必须在 1..{_PENDING_SUMMARY_LIMIT} 范围内"
            )
        path = self._existing_path(session_id)
        if path is None:
            return PendingRequestSummaryListDTO(
                session_id=session_id,
                thread_id=thread_id,
                request_count=0,
            )
        if thread_id is None:
            with self._access(session_id, "read_header"):
                header = await asyncio.to_thread(self._read_header, path)
            items = header.summaries[:limit]
            request_count = header.request_count
        else:
            # 精确 thread 的冷摘要只读控制引用，不读取 canonical 正文或恢复 Job。
            with self._access(session_id, "read_control"):
                header, references = await asyncio.to_thread(self._read_control_snapshot, session_id, path)
            scoped = [item for item in references if item.thread_id == thread_id]
            request_count = len(scoped)
            items = [PendingRequestSummaryDTO(
                job_id=item.job_id, message_id=item.message_id, thread_id=item.thread_id,
                enqueue_sequence=item.enqueue_sequence, delivery_policy=item.delivery_policy,
                status=item.status, updated_at=item.updated_at,
            ) for item in scoped[:limit]]
        return PendingRequestSummaryListDTO(
            session_id=session_id,
            thread_id=thread_id,
            requests=items,
            request_count=request_count,
            snapshot_version=header.snapshot_version,
            truncated=len(items) < request_count,
        )

    async def save(
        self,
        session_id: str,
        requests: list[PendingRequestDTO],
    ) -> None:
        with self._access(session_id, "resolve"):
            path = self._path(session_id)
        self._validate_records(session_id, requests)
        with self._access(session_id, "validate_control"):
            references = [self._control_reference(item) for item in requests]
        with self._access(session_id, "project_input"):
            for reference in references:
                await asyncio.to_thread(self._project_request, reference)
        content = self._serialize_current(references)
        with self._access(session_id, "write_control"):
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._atomic_write, path, content)

    @staticmethod
    def _control_reference(item: PendingRequestDTO) -> _StoredPendingRequest:
        return _StoredPendingRequest.model_validate({
            **item.model_dump(mode="json", exclude={
                "content", "attachments", "message_created_at", "message_metadata",
            }),
            "provider_id": item.message_metadata.get("boxteam_session_provider_id"),
        })

    @staticmethod
    def _serialize_current(requests: list[_StoredPendingRequest]) -> bytes:
        detail = json.dumps(
            [item.model_dump(mode="json") for item in requests],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        header = _PendingRequestHeader(
            request_count=len(requests),
            snapshot_version=max(
                (item.snapshot_version for item in requests),
                default=0,
            ),
            summaries=[
                PendingRequestSummaryDTO(
                    job_id=item.job_id,
                    message_id=item.message_id,
                    thread_id=item.thread_id,
                    enqueue_sequence=item.enqueue_sequence,
                    delivery_policy=item.delivery_policy,
                    status=item.status,
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

    @staticmethod
    def _validate_records(
        session_id: str,
        requests: Sequence[PendingRequestControl],
    ) -> None:
        if any(request.session_id != session_id for request in requests):
            raise RuntimeError(f"待处理队列引用不属于当前 Session: {session_id}")
        sequences = [request.enqueue_sequence for request in requests]
        if len(sequences) != len(set(sequences)):
            raise RuntimeError(
                f"待处理队列存在重复入队序号: session_id={session_id}, sequences={sequences}"
            )
        if sequences != sorted(sequences):
            raise RuntimeError(
                f"待处理队列入队序号未严格递增: session_id={session_id}, sequences={sequences}"
            )

    async def delete(self, session_id: str) -> None:
        path = self._existing_path(session_id)
        if path is not None:
            with self._access(session_id, "delete"):
                await asyncio.to_thread(path.unlink)

    def _existing_path(self, session_id: str) -> Path | None:
        with self._access(session_id, "resolve"):
            path = self._path(session_id)
            return path if path.exists() else None

    @staticmethod
    @contextmanager
    def _access(session_id: str, stage: str) -> Iterator[None]:
        """存储诊断按 owner 身份定位，原生位置与校验输入只留在内部原因链。"""
        try:
            yield
        except Exception as error:
            detail = (
                f"session_id={session_id}, artifact=pending_requests, "
                f"stage={stage}, error={type(error).__name__}"
            )
            if isinstance(error, OSError):
                raise type(error)(error.errno, f"{detail}, errno={error.errno}") from error
            if isinstance(error, ValidationError):
                rules = sorted({item["type"] for item in error.errors(include_input=False)})
                detail += f", error_count={error.error_count()}, rules={','.join(rules)}"
            elif isinstance(error, json.JSONDecodeError):
                detail += f", line={error.lineno}, column={error.colno}"
            elif isinstance(error, UnicodeDecodeError):
                detail += f", start={error.start}, end={error.end}"
            elif isinstance(error, _PendingControlError):
                detail += f", rule={error.rule}"
            for category in (KeyError, ValueError, TypeError):
                if isinstance(error, category):
                    raise category(detail) from error
            raise RuntimeError(detail) from error

    @classmethod
    def _read_header(cls, path: Path) -> _PendingRequestHeader:
        with path.open("rb") as stream:
            line = stream.readline(_PENDING_HEADER_MAX_BYTES + 1)
        return cls._parse_header(line)

    @staticmethod
    def _parse_header(line: bytes) -> _PendingRequestHeader:
        if not line.endswith(b"\n") or len(line) > _PENDING_HEADER_MAX_BYTES:
            raise _PendingControlError("header_incomplete_or_oversized")
        header = _PendingRequestHeader.model_validate_json(line)
        if header.version != _PENDING_HEADER_VERSION:
            raise _PendingControlError("header_version_unsupported")
        if len(header.summaries) > _PENDING_SUMMARY_LIMIT:
            raise _PendingControlError("header_summary_limit_exceeded")
        return header

    @classmethod
    def _is_current_schema(cls, path: Path) -> bool:
        try:
            cls._read_header(path)
        except (RuntimeError, ValueError):
            return False
        return True

    @classmethod
    def _read_control_snapshot(
        cls, session_id: str, path: Path,
    ) -> tuple[_PendingRequestHeader, list[_StoredPendingRequest]]:
        # 同一个文件 handle 冻结原子发布版本，避免旧 header 与新 detail 混版。
        with path.open("rb") as stream:
            header = cls._parse_header(stream.readline(_PENDING_HEADER_MAX_BYTES + 1))
            raw = stream.read()
        if not raw.endswith(b"\n"):
            raise _PendingControlError("detail_incomplete")
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise TypeError("待处理消息文件必须是 JSON 数组")
        references = [_StoredPendingRequest.model_validate(item) for item in payload]
        cls._validate_records(session_id, references)
        return header, references

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
