from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.custom_tools import CustomToolFactoryContext
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.browser import (
    RunPlaywrightCodeInput,
    create_list_browser_page_tool,
    create_navigate_page_tool,
    create_open_browser_page_tool,
    create_read_page_tool,
    create_run_playwright_code_tool,
    create_screenshot_page_tool,
)
from app.services.infrastructure.browser_manager_client import (
    BrowserManagerClient,
    BrowserManagerRequestError,
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


def test_run_playwright_code_default_timeout_matches_browser_service() -> None:
    assert RunPlaywrightCodeInput.model_fields["timeoutMs"].default == 10000


@pytest.mark.asyncio
async def test_run_playwright_code_forwards_the_default_timeout() -> None:
    browser_client = MagicMock()
    browser_client.run_playwright_code = AsyncMock(return_value={"result": "ok"})

    tool = create_run_playwright_code_tool(_context(browser_client))
    result = json.loads(
        await tool.ainvoke({"pageId": "browser_test", "code": "return 'ok';"})
    )

    assert result == {"result": "ok"}
    browser_client.run_playwright_code.assert_awaited_once_with(
        "browser_test",
        {"code": "return 'ok';", "timeoutMs": 10000},
    )


@pytest.mark.asyncio
async def test_navigate_page_preserves_browser_page_id_separate_from_tab_id() -> None:
    browser_client = MagicMock()
    browser_client.navigate_page = AsyncMock(
        return_value={
            "browser_id": "browser_test",
            "page_id": "page_internal",
            "session_id": "ses_user_browser",
            "status": "running",
            "active_page_id": "page_internal",
            "pages": [
                {
                    "page_id": "page_internal",
                    "title": "Export",
                    "url": "http://127.0.0.1:8787/",
                    "active": True,
                }
            ],
        }
    )

    tool = create_navigate_page_tool(_context(browser_client))
    result = json.loads(
        await tool.ainvoke(
            {
                "pageId": "browser_test",
                "type": "url",
                "url": "http://127.0.0.1:8787/",
            }
        )
    )

    assert result["pageId"] == "browser_test"
    assert result["browserId"] == "browser_test"
    assert result["activePageId"] == "page_internal"
    assert "page_id" not in result


@pytest.mark.asyncio
async def test_read_page_does_not_leak_internal_page_handle_as_browser_page_id() -> None:
    browser_client = MagicMock()
    browser_client.read_page = AsyncMock(
        return_value={
            "page_id": "page_recovered_internal",
            "status": "running",
            "active_page_id": "page_recovered_internal",
            "pages": [
                {
                    "page_id": "page_recovered_internal",
                    "title": "Export",
                    "actual_url": "http://127.0.0.1:8765/parry_arena.html",
                    "active": True,
                }
            ],
        }
    )

    tool = create_read_page_tool(_context(browser_client))
    result = json.loads(await tool.ainvoke({"pageId": "browser_test"}))

    assert result["pageId"] == "browser_test"
    assert result["browserId"] == "browser_test"
    assert result["activePageId"] == "page_recovered_internal"
    assert result["pages"] == [
        {
            "tabId": "page_recovered_internal",
            "title": "Export",
            "url": "http://127.0.0.1:8765/parry_arena.html",
            "active": True,
        }
    ]
    assert "page_id" not in result
    assert "active_page_id" not in result


@pytest.mark.asyncio
async def test_retryable_browser_timeout_is_a_recoverable_tool_result() -> None:
    browser_client = MagicMock()
    browser_client.run_playwright_code = AsyncMock(
        side_effect=BrowserManagerRequestError(
            method="POST",
            path="/api/browsers/browser_test/run",
            status=408,
            payload={
                "code": "browser_tool_timeout",
                "error": "Playwright 代码执行超时: 15000ms；浏览器页面已重置，可重试",
                "retryable": True,
                "recovery": "page_reset",
                "timeout_ms": 15000,
            },
        )
    )

    tool = create_run_playwright_code_tool(_context(browser_client))
    result = json.loads(
        await tool.ainvoke(
            {
                "pageId": "browser_test",
                "code": "await page.waitForTimeout(20000);",
                "timeoutMs": 15000,
            }
        )
    )

    assert result == {
        "status": "error",
        "error": "Playwright 代码执行超时: 15000ms；浏览器页面已重置，可重试 (code=browser_tool_timeout, retryable=true, recovery=page_reset)",
        "retryable": True,
        "code": "browser_tool_timeout",
        "recovery": "page_reset",
        "timeoutMs": 15000,
    }


@pytest.mark.asyncio
async def test_retryable_missing_browser_page_is_a_recoverable_tool_result() -> None:
    browser_client = MagicMock()
    browser_client.run_playwright_code = AsyncMock(
        side_effect=BrowserManagerRequestError(
            method="POST",
            path="/api/browsers/page_internal/run",
            status=404,
            payload={
                "code": "browser_page_not_found",
                "error": "浏览器页面不存在: page_internal",
                "retryable": True,
                "recovery": "list_or_open_browser_page",
            },
        )
    )

    tool = create_run_playwright_code_tool(_context(browser_client))
    result = json.loads(
        await tool.ainvoke(
            {"pageId": "page_internal", "code": "return await page.title();"}
        )
    )

    assert result == {
        "status": "error",
        "error": "浏览器页面不存在: page_internal (code=browser_page_not_found, retryable=true, recovery=list_or_open_browser_page)",
        "retryable": True,
        "code": "browser_page_not_found",
        "recovery": "list_or_open_browser_page",
    }


@pytest.mark.asyncio
async def test_retryable_read_page_timeout_is_a_recoverable_tool_result() -> None:
    browser_client = MagicMock()
    browser_client.read_page = AsyncMock(
        side_effect=BrowserManagerRequestError(
            method="GET",
            path="/api/browsers/browser_test/read",
            status=408,
            payload={
                "code": "browser_tool_timeout",
                "error": "浏览器页面读取超时: 10000ms；浏览器页面已重置，可重试",
                "retryable": True,
                "recovery": "page_reset",
                "timeout_ms": 10000,
            },
        )
    )

    tool = create_read_page_tool(_context(browser_client))
    result = json.loads(await tool.ainvoke({"pageId": "browser_test"}))

    assert result == {
        "status": "error",
        "error": "浏览器页面读取超时: 10000ms；浏览器页面已重置，可重试 (code=browser_tool_timeout, retryable=true, recovery=page_reset)",
        "retryable": True,
        "code": "browser_tool_timeout",
        "recovery": "page_reset",
        "timeoutMs": 10000,
    }


@pytest.mark.asyncio
async def test_transport_read_page_timeout_is_a_recoverable_tool_result() -> None:
    browser_client = BrowserManagerClient(backend_url="http://browser.test")
    tool = create_read_page_tool(_context(browser_client))

    with patch.object(
        browser_client,
        "_json_request_sync",
        side_effect=TimeoutError("timed out"),
    ):
        result = json.loads(await tool.ainvoke({"pageId": "browser_test"}))

    assert result["status"] == "error"
    assert result["code"] == "browser_tool_timeout"
    assert result["retryable"] is True
    assert result["recovery"] == "page_reset"
    assert result["timeoutMs"] == 30000
    assert "浏览器管理器操作超时" in result["error"]


@pytest.mark.asyncio
async def test_retryable_screenshot_timeout_is_a_recoverable_tool_result() -> None:
    browser_client = MagicMock()
    browser_client.screenshot_page = AsyncMock(
        side_effect=BrowserManagerRequestError(
            method="POST",
            path="/api/browsers/browser_test/screenshot",
            status=408,
            payload={
                "code": "browser_tool_timeout",
                "error": "浏览器截图超时: 10000ms；浏览器页面已重置，可重试",
                "retryable": True,
                "recovery": "page_reset",
                "timeout_ms": 10000,
            },
        )
    )

    tool = create_screenshot_page_tool(_context(browser_client))
    result = json.loads(await tool.ainvoke({"pageId": "browser_test"}))

    assert result == {
        "status": "error",
        "error": "浏览器截图超时: 10000ms；浏览器页面已重置，可重试 (code=browser_tool_timeout, retryable=true, recovery=page_reset)",
        "retryable": True,
        "code": "browser_tool_timeout",
        "recovery": "page_reset",
        "timeoutMs": 10000,
    }


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
