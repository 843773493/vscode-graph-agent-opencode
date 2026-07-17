from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.agents.tools.custom_invocation import create_custom_tool_invoker_tool
from app.agents.tools.session_history import (
    create_grep_session_context_jsonl_tool,
    create_read_session_recent_text_messages_tool,
)
from app.schemas.public_v2.session_context import (
    SessionContextGrepResultDTO,
    SessionContextSnapshotMetadataDTO,
    SessionRecentTextMessagesDTO,
)


def _snapshot() -> SessionContextSnapshotMetadataDTO:
    return SessionContextSnapshotMetadataDTO(
        snapshot_id="ckpt-remote",
        content_sha256="a" * 64,
        generated_at="2026-07-16T00:00:00+00:00",
        line_count=1,
        raw_message_count=1,
        byte_count=10,
        compacted=False,
        consistency="not_checked",
    )


class _FakeLocalQueryService:
    def __init__(self) -> None:
        self.recent_calls: list[tuple[str, int]] = []

    async def recent_text(
        self,
        session_id: str,
        *,
        rounds: int = 5,
    ) -> SessionRecentTextMessagesDTO:
        self.recent_calls.append((session_id, rounds))
        return SessionRecentTextMessagesDTO(
            session_id=session_id,
            rounds=rounds,
            user_message_count=0,
            context_snapshot=_snapshot(),
        )


class _FakeWorkspaceClient:
    def __init__(self) -> None:
        self.grep_calls: list[tuple[str, str, str]] = []

    async def grep_in_workspace(
        self,
        workspace_id: str,
        session_id: str,
        *,
        pattern: str,
        case_sensitive: bool = False,
        max_matches: int = 20,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextGrepResultDTO:
        del case_sensitive, max_matches, expected_snapshot_id
        self.grep_calls.append((workspace_id, session_id, pattern))
        return SessionContextGrepResultDTO(
            session_id=session_id,
            pattern=pattern,
            case_sensitive=False,
            context_snapshot=_snapshot(),
            total_matching_lines=0,
            returned_match_count=0,
            matches_truncated=False,
        )


class _FailingWorkspaceClient:
    async def recent_text_in_workspace(
        self,
        workspace_id: str,
        session_id: str,
        *,
        rounds: int = 5,
    ) -> SessionRecentTextMessagesDTO:
        del session_id, rounds
        raise WorkspaceSessionContextAccessError(
            f"Gateway 工作区不存在: {workspace_id}。"
            "请检查并修正 workspace_id 后重试；无法确认时请提醒用户"
        )


@pytest.mark.asyncio
async def test_recent_tool_without_workspace_uses_local_query_service():
    local_service = _FakeLocalQueryService()
    context = SimpleNamespace(
        session_context_query_service=local_service,
        workspace_session_context_client=_FakeWorkspaceClient(),
    )
    tool = create_read_session_recent_text_messages_tool(context)

    result = json.loads(
        await tool.ainvoke({"session_id": "ses_local", "rounds": 3})
    )

    assert result["session_id"] == "ses_local"
    assert local_service.recent_calls == [("ses_local", 3)]


@pytest.mark.asyncio
async def test_grep_tool_with_workspace_uses_gateway_client():
    workspace_client = _FakeWorkspaceClient()
    context = SimpleNamespace(
        session_context_query_service=_FakeLocalQueryService(),
        workspace_session_context_client=workspace_client,
    )
    tool = create_grep_session_context_jsonl_tool(context)

    result = json.loads(
        await tool.ainvoke(
            {
                "workspace_id": "gw_target",
                "session_id": "ses_remote",
                "pattern": "ALPHA",
            }
        )
    )

    assert result["session_id"] == "ses_remote"
    assert workspace_client.grep_calls == [
        ("gw_target", "ses_remote", "ALPHA")
    ]


@pytest.mark.asyncio
async def test_tool_rejects_blank_workspace_id():
    context = SimpleNamespace(
        session_context_query_service=_FakeLocalQueryService(),
        workspace_session_context_client=_FakeWorkspaceClient(),
    )
    tool = create_read_session_recent_text_messages_tool(context)

    result = await tool.ainvoke(
        {
            "type": "tool_call",
            "id": "call_blank_workspace",
            "name": tool.name,
            "args": {"workspace_id": "   ", "session_id": "ses_target"},
        }
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "workspace_id 不能是空字符串" in result.text


@pytest.mark.asyncio
async def test_custom_invoker_returns_gateway_access_error_to_ai_as_tool_message():
    context = SimpleNamespace(
        session_context_query_service=_FakeLocalQueryService(),
        workspace_session_context_client=_FailingWorkspaceClient(),
    )
    target_tool = create_read_session_recent_text_messages_tool(context)
    invoker = create_custom_tool_invoker_tool([target_tool])

    result = await invoker.ainvoke(
        {
            "type": "tool_call",
            "id": "call_cross_workspace",
            "name": invoker.name,
            "args": {
                "tool_name": target_tool.name,
                "arguments": {
                    "workspace_id": "gw_typo",
                    "session_id": "ses_target",
                },
            },
        }
    )

    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call_cross_workspace"
    assert result.status == "error"
    assert "gw_typo" in result.text
    assert "修正 workspace_id 后重试" in result.text
    assert "提醒用户" in result.text
