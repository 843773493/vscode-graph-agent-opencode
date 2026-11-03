from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISDIR
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.atomic_fs import native_io_path
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.llm_request_log import (
    LLMRequestLogLimit,
    LLMRequestLogLocationDTO,
    LLMRequestLogMatchDTO,
    LLMRequestLogRecordDTO,
    LLMRequestLogReferenceDTO,
)

MAX_LOG_RECORD_BYTES = 1024 * 1024
MAX_SEARCH_BYTES = 8 * 1024 * 1024
MAX_SEARCH_RECORDS = 128
# 正常写入方仅保留 128 条；也限制手工导入／损坏目录的元数据枚举。
MAX_DIRECTORY_FILES = 512


class LLMRequestLogError(ValueError):
    """原日志 owner 的续点／引用拒绝；不依赖提示文字判断原因。"""

    def __init__(self, code: Literal["invalid_request_log_cursor", "stale_request_log_cursor", "stale_request_log_reference"],
                 reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason

    def detail(self) -> dict[str, str]:
        return {"owner": "llm_request_log", "code": self.code, "reason": self.reason, "message": str(self)}


class LogSearchPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory_revision: str
    file_index: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)
    content_version: str | None = None


@dataclass
class LogSearchBudget:
    check: Callable[[], None]
    deadline: float = field(default_factory=lambda: monotonic() + 1.0)
    scanned_records: int = 0
    scanned_bytes: int = 0

    def limited_reason(self) -> LLMRequestLogLimit | None:
        self.check()
        if monotonic() >= self.deadline:
            return "deadline"
        if self.scanned_records >= MAX_SEARCH_RECORDS:
            return "scan_limit"
        return None


@dataclass
class LogSearchPage:
    items: list[LLMRequestLogMatchDTO]
    position: LogSearchPosition | None = None
    complete: bool = True
    limited_reason: LLMRequestLogLimit | None = None


class LLMRequestLogService:
    """读取和有界搜索同一份 Session 级 LLM 请求／响应日志。"""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        path_resolver: SessionCatalogPathResolver | None = None,
    ) -> None:
        self._path_resolver = path_resolver or get_session_path_resolver(sessions_dir)

    def _directory(self, session_id: str) -> Path:
        return native_io_path(self._path_resolver.resolve_session_node_for_runtime(session_id) / "logs" / "llm_requests")

    def list_session_logs(self, session_id: str) -> list[LLMRequestLogRecordDTO]:
        directory = self._directory(session_id)
        try:
            stat = directory.stat()
        except FileNotFoundError:
            return []
        if not S_ISDIR(stat.st_mode):
            raise NotADirectoryError(f"LLM 请求日志路径不是目录: {directory}")
        return [self._read_log_file(path, session_id) for path in sorted(directory.glob("*.json"), key=self._log_sort_key)]

    def search_session_logs(
        self, session_id: str, query: str, *, limit: int,
        position: LogSearchPosition | None, budget: LogSearchBudget,
    ) -> LogSearchPage:
        directory = self._directory(session_id)
        metadata: list[tuple[int, str, int, int]] = []
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            if position is not None:
                raise LLMRequestLogError("stale_request_log_cursor", "directory_deleted", "请求日志目录已删除，请重新查询") from None
            return LogSearchPage([])
        with entries:
            for entry in entries:
                reason = budget.limited_reason()
                if reason:
                    return LogSearchPage([], position, complete=False, limited_reason=reason)
                if not entry.name.endswith(".json"):
                    continue
                if len(metadata) >= MAX_DIRECTORY_FILES:
                    return LogSearchPage([], complete=False, limited_reason="scan_limit")
                try:
                    stat = entry.stat()
                except FileNotFoundError as error:
                    raise LLMRequestLogError("stale_request_log_cursor", "record_deleted", "请求日志在查询期间被裁剪，请重新查询") from error
                metadata.append((self._timestamp_ns_from_filename(Path(entry.name)), entry.name, stat.st_size, stat.st_mtime_ns))
        metadata.sort()
        revision = hashlib.sha256(json.dumps(metadata).encode()).hexdigest()
        if position is not None and position.directory_revision != revision:
            raise LLMRequestLogError("stale_request_log_cursor", "directory_changed", "请求日志已更新或裁剪，请重新查询")
        index = position.file_index if position else 0
        offset = position.offset if position else 0
        if index > len(metadata) or index == len(metadata) and offset:
            raise LLMRequestLogError("invalid_request_log_cursor", "position_out_of_bounds", "请求日志 cursor 位置越界")
        items: list[LLMRequestLogMatchDTO] = []
        while index < len(metadata):
            reason = budget.limited_reason()
            next_position = LogSearchPosition(directory_revision=revision, file_index=index, offset=offset,
                                              content_version=position.content_version if position and offset else None)
            if reason or len(items) >= limit:
                return LogSearchPage(items, next_position, False, reason or "result_limit")
            _, name, size, _ = metadata[index]
            if size > MAX_LOG_RECORD_BYTES:
                return LogSearchPage(items, complete=False, limited_reason="item_size_limit")
            if size > MAX_SEARCH_BYTES - budget.scanned_bytes:
                return LogSearchPage(items, next_position, False, "byte_limit")
            try:
                raw = self._read_bytes(directory / name, min(MAX_LOG_RECORD_BYTES, MAX_SEARCH_BYTES - budget.scanned_bytes))
            except FileNotFoundError as error:
                raise LLMRequestLogError("stale_request_log_cursor", "record_deleted", "请求日志在查询期间被裁剪，请重新查询") from error
            budget.scanned_bytes += len(raw)
            budget.scanned_records += 1
            record = self._decode_log(raw, directory / name, session_id)
            version = hashlib.sha256(raw).hexdigest()
            if offset and (position is None or position.content_version != version):
                raise LLMRequestLogError("stale_request_log_cursor", "record_changed", "请求日志正文已变化，请重新查询")
            text = self._search_text(record)
            if offset > len(text):
                raise LLMRequestLogError("invalid_request_log_cursor", "position_out_of_bounds", "请求日志 cursor 正文位置越界")
            while (start := text.find(query, offset)) >= 0:
                end = start + len(query)
                preview_start = max(0, start - 80)
                items.append(LLMRequestLogMatchDTO(
                    reference=LLMRequestLogReferenceDTO(session_id=session_id, file_name=name, content_version=version,
                                                       start_codepoint=start, end_codepoint=end),
                    timestamp=record.timestamp, preview=text[preview_start:min(len(text), end + 80)],
                    preview_start_codepoint=preview_start,
                ))
                offset = end
                reason = budget.limited_reason()
                if len(items) >= limit or reason:
                    # 当前正文已在内存中；已知没有后续匹配时直接越过，避免续页重复读同一记录。
                    if text.find(query, offset) < 0:
                        index += 1
                        if index == len(metadata):
                            return LogSearchPage(items)
                        return LogSearchPage(items, LogSearchPosition(directory_revision=revision, file_index=index),
                                             False, "result_limit" if len(items) >= limit else reason)
                    return LogSearchPage(items, LogSearchPosition(directory_revision=revision, file_index=index,
                                                                 offset=offset, content_version=version),
                                         False, "result_limit" if len(items) >= limit else reason)
            index += 1
            offset = 0
        return LogSearchPage(items)

    def locate(self, reference: LLMRequestLogReferenceDTO) -> LLMRequestLogLocationDTO:
        path = self._directory(reference.session_id) / reference.file_name
        try:
            raw = self._read_bytes(path, MAX_LOG_RECORD_BYTES)
        except FileNotFoundError as error:
            raise LLMRequestLogError("stale_request_log_reference", "record_deleted", "命中的请求日志已删除或被留存策略裁剪") from error
        if hashlib.sha256(raw).hexdigest() != reference.content_version:
            raise LLMRequestLogError("stale_request_log_reference", "record_changed", "命中的请求日志已变化")
        record = self._decode_log(raw, path, reference.session_id)
        text = self._search_text(record)
        if reference.end_codepoint > len(text):
            raise LLMRequestLogError("stale_request_log_reference", "position_out_of_bounds", "请求日志命中位置已失效")
        return LLMRequestLogLocationDTO(reference=reference, record=record, text=text)

    @staticmethod
    def _search_text(record: LLMRequestLogRecordDTO) -> str:
        return json.dumps({"request": record.request, "response": record.response, "upstream": record.upstream},
                          ensure_ascii=False, indent=2)

    @staticmethod
    def _read_bytes(path: Path, max_bytes: int) -> bytes:
        with native_io_path(path).open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise LLMRequestLogError("stale_request_log_reference", "record_size_limit", "LLM 请求日志超出本次读取字节上限，无法核验原版本")
        return raw

    def _read_log_file(self, log_file: Path, session_id: str) -> LLMRequestLogRecordDTO:
        return self._decode_log(native_io_path(log_file).read_bytes(), log_file, session_id)

    def _decode_log(self, raw_bytes: bytes, log_file: Path, session_id: str) -> LLMRequestLogRecordDTO:
        raw = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(raw, dict):
            raise TypeError(f"LLM 请求日志不是 JSON object: {log_file}")
        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            raise TypeError(f"LLM 请求日志 timestamp 必须是 epoch 毫秒整数: {log_file}")
        request = self._record_field(raw, "request", log_file)
        response = self._record_field(raw, "response", log_file)
        upstream = raw.get("upstream")
        if upstream is None:
            upstream = {"attempts": []}
        if not isinstance(upstream, dict):
            raise TypeError(f"LLM 请求日志 upstream 必须是 object: {log_file}")
        raw_session_id = raw.get("session_id")
        if raw_session_id is not None and raw_session_id != session_id:
            raise ValueError(f"LLM 请求日志 Session 归属不一致: {log_file}")
        raw_job_id = raw.get("job_id")
        return LLMRequestLogRecordDTO(
            session_id=session_id, job_id=raw_job_id if isinstance(raw_job_id, str) else None,
            timestamp=timestamp, file_name=log_file.name, request=request, response=response, upstream=upstream,
        )

    def _record_field(self, raw: dict[str, Any], field_name: str, log_file: Path) -> dict[str, Any]:
        value = raw.get(field_name)
        if not isinstance(value, dict):
            raise TypeError(f"LLM 请求日志缺少 {field_name} object: {log_file}")
        return value

    def _log_sort_key(self, log_file: Path) -> tuple[int, str]:
        return (self._timestamp_ns_from_filename(log_file), log_file.name)

    def _timestamp_ns_from_filename(self, log_file: Path) -> int:
        """文件名中的 epoch 纳秒只用于排序，不回填正文的毫秒时间。"""
        try:
            return int(log_file.stem)
        except ValueError as exc:
            raise ValueError(f"LLM 请求日志文件名不是 epoch 纳秒整数: {log_file}") from exc
