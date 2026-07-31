from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.custom_tools import CustomToolFactoryContext
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.browser import (
    create_list_browser_page_tool,
    create_open_browser_page_tool,
)


def _context(browser_manager_client: MagicMock) -> CustomToolFactoryContext:
    return CustomToolFactoryContext(
        session_id="ses_user_browser",
        agent_id="default",
        sender_agent_id="default",
        workspace_root=Path.cwd(),
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
        job_event_bus=MagicMock(),
        job_service=MagicMock(),
        session_context_query_service=MagicMock(),
        workspace_session_context_client=MagicMock(),
        session_orchestrator=MagicMock(),
        config_service=MagicMock(),
        terminal_manager_client=MagicMock(),
        browser_manager_client=browser_manager_client,
        invocation_context=ToolInvocationContext(),
    )


@pytest.mark.asyncio
async def test_list_browser_page_is_read_only_and_returns_safe_summary() -> None:
    browser_client = MagicMock()
    browser_client.list_browsers_from_state.return_value = [
        {
            "browser_id": "browser_running",
            "session_id": "ses_user_browser",
            "status": "running",
            "resource_state": "frozen",
            "client_count": 1,
            "title": "Bilibili",
            "url": "https://www.bilibili.com/",
            "actual_url": "https://www.bilibili.com/video/BV1",
            "active_page_id": "page_video",
            "agent_access_locked": True,
            "updated_at": "2026-07-26T17:21:15.004Z",
            "attach_url": "http://127.0.0.1/private",
            "checkpoint": {"path": "/private/checkpoint.json"},
            "pages": [
                {
                    "page_id": "page_video",
                    "title": "Video",
                    "url": "https://www.bilibili.com/",
                    "actual_url": "https://www.bilibili.com/video/BV1",
                    "active": True,
                }
            ],
        },
        {
            "browser_id": "browser_deleted",
            "session_id": "ses_user_browser",
            "status": "deleted",
            "pages": [],
        },
    ]

    tool = create_list_browser_page_tool(_context(browser_client))
    result = json.loads(await tool.ainvoke({}))

    assert result == {
        "count": 1,
        "pages": [
            {
                "pageId": "browser_running",
                "browserId": "browser_running",
                "title": "Bilibili",
                "url": "https://www.bilibili.com/video/BV1",
                "status": "running",
                "resourceState": "frozen",
                "clientCount": 1,
                "activePageId": "page_video",
                "pages": [
                    {
                        "tabId": "page_video",
                        "title": "Video",
                        "url": "https://www.bilibili.com/video/BV1",
                        "active": True,
                    }
                ],
                "agentAccessLocked": True,
                "updatedAt": "2026-07-26T17:21:15.004Z",
            }
        ],
    }
    browser_client.list_browsers_from_state.assert_called_once_with(
        "ses_user_browser"
    )
    browser_client.create_browser.assert_not_called()
    browser_client.navigate_page.assert_not_called()
    browser_client.read_page.assert_not_called()


@pytest.mark.asyncio
async def test_open_browser_page_reuses_user_created_running_browser() -> None:
    browser_client = MagicMock()
    browser_client.list_browsers_from_state.return_value = [
        {
            "browser_id": "browser_user_created",
            "session_id": "ses_user_browser",
            "status": "running",
            "url": "about:blank",
            "title": "用户浏览器",
        }
    ]
    browser_client.create_browser = AsyncMock()
    browser_client.navigate_page = AsyncMock(
        return_value={
            "browser_id": "browser_user_created",
            "url": "https://example.com",
            "title": "Example",
        }
    )
    browser_client.read_page = AsyncMock(return_value={"summary": "Example"})

    tool = create_open_browser_page_tool(_context(browser_client))
    result = json.loads(
        await tool.ainvoke({"url": "https://example.com", "forceNew": False})
    )

    assert result["pageId"] == "browser_user_created"
    assert result["reused"] is True
    browser_client.create_browser.assert_not_awaited()
    browser_client.navigate_page.assert_awaited_once_with(
        browser_id="browser_user_created",
        navigation_type="url",
        url="https://example.com",
    )
