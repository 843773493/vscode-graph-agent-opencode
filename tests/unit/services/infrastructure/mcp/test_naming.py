from __future__ import annotations

from app.services.infrastructure.mcp.naming import (
    MAX_MODEL_TOOL_NAME_LENGTH,
    build_mcp_tool_id,
)


def test_build_mcp_tool_id_adds_server_namespace() -> None:
    assert build_mcp_tool_id("tui-mcp", "list/sessions") == (
        "mcp__tui-mcp__list_sessions"
    )


def test_build_mcp_tool_id_truncates_with_stable_hash() -> None:
    long_remote_tool_name = "tool-" + ("x" * 100)
    first = build_mcp_tool_id("server", long_remote_tool_name)
    second = build_mcp_tool_id("server", long_remote_tool_name)

    assert first == second
    assert len(first) == MAX_MODEL_TOOL_NAME_LENGTH
    assert first.startswith("mcp__server__tool-")
