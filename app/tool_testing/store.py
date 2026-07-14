from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from typing import Any


_SAFE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class ToolTestStore:
    def __init__(self, *, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def reset_tool(self, tool_name: str) -> Path:
        target = self.tool_dir(tool_name)
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
            raise ValueError(f"不允许写入未知的测试文件: {file_name}")
        target = self.case_dir(tool_name, provider_id, case_id) / file_name
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
        if not target.parent.is_dir():
            raise FileNotFoundError(f"工具测试目录不存在: {target.parent}")
        _write_json_atomic(target, payload)

    def read_run(self, run_id: str) -> dict[str, Any]:
        for target in self.root.glob("*/run.json"):
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
        targets = (
            [self.tool_dir(tool_name) / "run.json"]
            if tool_name is not None
            else list(self.root.glob("*/run.json"))
        )
        records = [_read_json_object(target) for target in targets if target.is_file()]
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return records[:limit]


def _safe_segment(value: str, *, label: str) -> str:
    if not value or not _SAFE_SEGMENT_PATTERN.fullmatch(value):
        raise ValueError(f"{label} 不能安全用作目录名: {value!r}")
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
        raise TypeError(f"工具测试记录格式错误: {target}")
    return payload


def _json_default(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"测试记录包含不可序列化对象: {type(value).__name__}")
