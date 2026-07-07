from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.path_utils import get_logs_dir, safe_join
from app.schemas.public_v2.llm_request_log import LLMRequestLogRecordDTO


class LLMRequestLogService:
    """读取落盘的完整 LLM 请求/响应日志。"""

    def __init__(self, logs_dir: Path | None = None) -> None:
        self._logs_dir = logs_dir

    def _base_dir(self) -> Path:
        return (self._logs_dir or get_logs_dir()) / "llm_requests"

    def list_session_logs(self, session_id: str) -> list[LLMRequestLogRecordDTO]:
        session_dir = safe_join(self._base_dir(), session_id)
        if not session_dir.exists():
            return []
        if not session_dir.is_dir():
            raise NotADirectoryError(f"LLM 请求日志路径不是目录: {session_dir}")

        records: list[LLMRequestLogRecordDTO] = []
        for log_file in sorted(session_dir.glob("*.json"), key=self._log_sort_key):
            records.append(self._read_log_file(log_file, session_id))
        return records

    def _read_log_file(
        self,
        log_file: Path,
        session_id: str,
    ) -> LLMRequestLogRecordDTO:
        with log_file.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError(f"LLM 请求日志不是 JSON object: {log_file}")

        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, int):
            timestamp = self._timestamp_from_file(log_file)

        request = self._record_field(raw, "request", log_file)
        response = self._record_field(raw, "response", log_file)
        raw_session_id = raw.get("session_id")
        raw_job_id = raw.get("job_id")

        return LLMRequestLogRecordDTO(
            session_id=raw_session_id if isinstance(raw_session_id, str) else session_id,
            job_id=raw_job_id if isinstance(raw_job_id, str) else None,
            timestamp=timestamp,
            file_name=log_file.name,
            file_path=str(log_file),
            request=request,
            response=response,
        )

    def _record_field(
        self,
        raw: dict[str, Any],
        field_name: str,
        log_file: Path,
    ) -> dict[str, Any]:
        value = raw.get(field_name)
        if not isinstance(value, dict):
            raise ValueError(f"LLM 请求日志缺少 {field_name} object: {log_file}")
        return value

    def _log_sort_key(self, log_file: Path) -> tuple[int, str]:
        return (self._timestamp_from_file(log_file), log_file.name)

    def _timestamp_from_file(self, log_file: Path) -> int:
        try:
            return int(log_file.stem)
        except ValueError as exc:
            raise ValueError(f"LLM 请求日志文件名不是毫秒时间戳: {log_file}") from exc

