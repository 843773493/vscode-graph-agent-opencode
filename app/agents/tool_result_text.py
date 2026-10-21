from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal


def serialize_tool_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def declared_tool_payload_status(parsed: Mapping[str, object]) -> Literal["success", "error"] | None:
    """解析工具返回体中显式声明的执行状态，未声明时返回 None。

    工作区工具结果用两种显式声明表达同一事实：`status` 取 error/success，
    `ok` 取布尔值。二者都是可信的执行结论，缺一不可；只认其中一种会把
    另一种显式失败静默当成成功。
    """
    result_status = parsed.get("status")
    if result_status == "error":
        return "error"
    if result_status == "success":
        return "success"
    result_ok = parsed.get("ok")
    if isinstance(result_ok, bool):
        return "success" if result_ok else "error"
    return None


def tool_output_status(output: object) -> Literal["success", "error"]:
    """合并 ToolMessage 状态与工具返回体中的状态。"""
    status = getattr(output, "status", None)
    if status == "error":
        return "error"
    content = getattr(output, "content", None)
    text = serialize_tool_value(content if content is not None else output).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        payload_status = declared_tool_payload_status(parsed)
        if payload_status is not None:
            return payload_status
    if status == "success":
        return "success"
    return "success" if text and not text.startswith("Error:") else "error"
