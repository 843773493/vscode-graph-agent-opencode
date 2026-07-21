from __future__ import annotations

import hashlib
import re


MAX_MODEL_TOOL_NAME_LENGTH = 64
_UNSUPPORTED_CHARACTER_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")


def build_mcp_tool_id(server_id: str, remote_tool_name: str) -> str:
    normalized_server_id = _normalize_component(server_id)
    normalized_tool_name = _normalize_component(remote_tool_name)
    full_name = f"mcp__{normalized_server_id}__{normalized_tool_name}"
    if len(full_name) <= MAX_MODEL_TOOL_NAME_LENGTH:
        return full_name
    digest = hashlib.sha256(
        f"{server_id}\0{remote_tool_name}".encode("utf-8")
    ).hexdigest()[:10]
    prefix_length = MAX_MODEL_TOOL_NAME_LENGTH - len(digest) - 2
    return f"{full_name[:prefix_length]}__{digest}"


def _normalize_component(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("MCP 工具命名组件不能为空")
    normalized = _UNSUPPORTED_CHARACTER_PATTERN.sub("_", stripped).strip("_")
    if not normalized:
        raise ValueError(f"MCP 工具命名组件不包含可用字符: {value!r}")
    return normalized
