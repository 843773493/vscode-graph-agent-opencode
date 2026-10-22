"""v5 pending 控制引用的显式一次性解码；正常 runtime 不读取此文件。"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from app.schemas.internal_v2.pending_request import (
    PendingRequestControl,
    PendingRequestSummaryDTO,
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


class PendingQueueV5Migration:
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
        if header.request_count != len(references):
            raise _PendingControlError("request_count_mismatch")
        return header, references
