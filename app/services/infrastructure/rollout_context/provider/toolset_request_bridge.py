"""唯一的 ToolSetRef 到外部 provider tools bridge。"""

from __future__ import annotations

import copy
from collections.abc import Mapping

from app.domain.itemized.refs import ToolSetRef


def project_tool_set_ref(
    tool_set_ref: ToolSetRef,
    *,
    target_format: str,
) -> list[dict[str, object]]:
    """将 sealed ToolSetRef 转为 provider tools，不从 registry 猜测正文。"""
    if target_format not in {"chat_completions", "responses"}:
        raise ValueError(f"不支持的工具请求格式: {target_format!r}")
    if tool_set_ref.assembly_id is None:
        raise ValueError("plan-order-integrity: ToolSetRef 未绑定 sealed assembly")
    tool_set_ref.validate_manifest()
    if tool_set_ref.availability != "available":
        raise ValueError(
            "detail-unavailable: ToolSetRef 不可用于 provider dispatch: "
            f"{tool_set_ref.ref_id}"
        )
    if tool_set_ref.content_hash is None:
        raise ValueError(
            "detail-unavailable: protected/redacted ToolSetRef 没有可发送的 manifest: "
            f"{tool_set_ref.ref_id}"
        )
    projected: list[dict[str, object]] = []
    for tool in tool_set_ref.tools:
        if tool.get("type") == "function" and isinstance(tool.get("function"), Mapping):
            function = copy.deepcopy(dict(tool["function"]))
            projected.append(
                {"type": "function", **function}
                if target_format == "responses"
                else {"type": "function", "function": function}
            )
            continue
        name = tool.get("name", tool.get("tool_id"))
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"source-mismatch: ToolSetRef tool 缺少 name: {tool_set_ref.ref_id}"
            )
        parameters = tool.get("parameters", tool.get("input_schema", {}))
        if not isinstance(parameters, Mapping):
            raise TypeError(
                f"source-mismatch: ToolSetRef tool schema 非 object: {name}"
            )
        function = {
            "name": name,
            "description": str(tool.get("description", "")),
            "parameters": copy.deepcopy(dict(parameters)),
        }
        if "strict" in tool:
            function["strict"] = tool["strict"]
        projected.append(
            {"type": "function", **function}
            if target_format == "responses"
            else {"type": "function", "function": function}
        )
    return projected
