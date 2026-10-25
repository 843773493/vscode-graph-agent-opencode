from __future__ import annotations

import json
from pathlib import Path
from stat import S_ISDIR
from typing import Any

from app.core.atomic_fs import native_io_path
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.llm_request_log import LLMRequestLogRecordDTO


class LLMRequestLogService:
    """读取落盘的完整 LLM 请求/响应日志。"""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        path_resolver: SessionCatalogPathResolver | None = None,
    ) -> None:
        # 默认经 path_utils 工厂取得当前工作区的 SQLite catalog resolver；
        # 生产 container 可显式注入同一实例。
        self._path_resolver = path_resolver or get_session_path_resolver(sessions_dir)

    def list_session_logs(self, session_id: str) -> list[LLMRequestLogRecordDTO]:
        session_dir = native_io_path(
            self._path_resolver.resolve_session_node_for_runtime(session_id)
            / "logs"
            / "llm_requests"
        )
        try:
            session_dir_stat = session_dir.stat()
        except FileNotFoundError:
            return []
        if not S_ISDIR(session_dir_stat.st_mode):
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
        with native_io_path(log_file).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
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
        raw_job_id = raw.get("job_id")

        return LLMRequestLogRecordDTO(
            session_id=raw_session_id if isinstance(raw_session_id, str) else session_id,
            job_id=raw_job_id if isinstance(raw_job_id, str) else None,
            timestamp=timestamp,
            file_name=log_file.name,
            request=request,
            response=response,
            upstream=upstream,
        )

    def _record_field(
        self,
        raw: dict[str, Any],
        field_name: str,
        log_file: Path,
    ) -> dict[str, Any]:
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
