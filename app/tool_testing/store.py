from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISREG
from typing import Any

from app.core.failures import report_failure
from app.core.trace_middleware import get_current_request_id
from app.tool_testing.definitions import tool_test_failure_detail

_SAFE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
logger = logging.getLogger(__name__)


class ToolTestStore:
    def __init__(self, *, root: Path) -> None:
        self.root = root
        with _store_access("initialize"):
            self.root.mkdir(parents=True, exist_ok=True)

    def reset_tool(self, tool_name: str) -> Path:
        target = self.tool_dir(tool_name)
        with _store_access("reset_tool"):
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
        return target

    def tool_dir(self, tool_name: str) -> Path:
        return self.root / _safe_segment(tool_name, label="工具名")

    def case_dir(self, tool_name: str, provider_id: str, case_id: str) -> Path:
        return (
            self.tool_dir(tool_name)
            / _safe_segment(provider_id, label="provider ID")
            / _safe_segment(case_id, label="测试用例 ID")
        )

    def prepare_case_dir(
        self,
        *,
        tool_name: str,
        provider_id: str,
        case_id: str,
    ) -> Path:
        target = self.case_dir(tool_name, provider_id, case_id)
        with _store_access("prepare_case"):
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
        return target

    def write_case_json(
        self,
        *,
        tool_name: str,
        provider_id: str,
        case_id: str,
        file_name: str,
        payload: dict[str, Any],
    ) -> Path:
        if file_name not in {"request.json", "response.json", "result.json"}:
            raise ValueError("不允许写入未知的测试文件")
        target = self.case_dir(tool_name, provider_id, case_id) / file_name
        with _store_access("write_case"):
            if not target.parent.is_dir():
                raise FileNotFoundError(f"测试用例目录不存在: {target.parent}")
            _write_json_atomic(target, payload)
        return target

    def write_run(self, run_id: str, payload: dict[str, Any]) -> None:
        if payload.get("run_id") != run_id:
            raise ValueError(f"工具测试 run_id 不一致: expected={run_id}")
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("工具测试记录缺少 tool_name")
        target = self.tool_dir(tool_name) / "run.json"
        with _store_access("write_run"):
            if not target.parent.is_dir():
                raise FileNotFoundError(f"工具测试目录不存在: {target.parent}")
            _write_json_atomic(target, payload)

    def read_run(self, run_id: str) -> dict[str, Any]:
        with _store_access("read_run"):
            for target in self._run_files():
                payload = _read_json_object(target)
                if payload.get("run_id") == run_id:
                    return payload
        raise FileNotFoundError(f"工具测试记录不存在或已被新测试覆盖: {run_id}")

    def list_runs(
        self,
        *,
        tool_name: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with _store_access("list_runs"):
            records = [_read_json_object(target) for target in self._run_files(tool_name)]
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return records[:limit]

    def _run_files(self, tool_name: str | None = None) -> Iterator[Path]:
        """查询共用完整扫描，根目录和扫描故障不能被 glob 当作空结果。"""
        selected = None if tool_name is None else self.tool_dir(tool_name).name
        with os.scandir(self.root) as entries:
            for entry in entries:
                if selected is not None and entry.name != selected:
                    continue
                if not entry.is_dir():
                    continue
                target = Path(entry.path) / "run.json"
                try:
                    mode = target.stat().st_mode
                except FileNotFoundError:
                    continue  # 尚无运行记录的工具目录不是读取失败。
                if not S_ISREG(mode):
                    error = TypeError("工具测试记录必须为普通文件")
                    error.add_note(f"工具测试记录位置: {target}")
                    detail = report_failure(
                        error, detail=str(error), diagnostic_logger=logger,
                        extra={"request_id": get_current_request_id()},
                    )
                    raise TypeError(detail) from error
                yield target


def _safe_segment(value: str, *, label: str) -> str:
    if not value or not _SAFE_SEGMENT_PATTERN.fullmatch(value):
        raise ValueError(f"{label} 不能安全用作目录名")
    return value


def _write_json_atomic(target: Path, payload: dict[str, Any]) -> None:
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    temporary.replace(target)


def _read_json_object(target: Path) -> dict[str, Any]:
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        error = TypeError("工具测试记录必须为 JSON object")
        error.add_note(f"工具测试记录位置: {target}")
        detail = report_failure(
            error, detail=str(error), diagnostic_logger=logger,
            extra={"request_id": get_current_request_id()},
        )
        raise TypeError(detail) from error
    return payload


def _json_default(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"测试记录包含不可序列化对象: {type(value).__name__}")


@contextmanager
def _store_access(stage: str) -> Iterator[None]:
    """由原存储出口交付失败事实，真实位置与损坏原件只留原因链和诊断。"""
    try:
        yield
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        detail = report_failure(
            error, detail=tool_test_failure_detail(error, stage=stage), diagnostic_logger=logger,
            extra={"request_id": get_current_request_id()},
        )
        if isinstance(error, OSError):
            raise type(error)(error.errno, detail, None, getattr(error, "winerror", None)) from error
        raise ValueError(detail) from error
