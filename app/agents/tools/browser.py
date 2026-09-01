from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agents.custom_tools import CustomToolFactoryContext
from app.services.infrastructure.browser_manager_client import (
    BrowserManagerRequestError,
)


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _page_id(value: str) -> str:
    resolved = value.strip()
    if not resolved:
        raise ValueError("pageId 不能为空")
    return resolved


class OpenBrowserPageInput(BaseModel):
    url: str = Field(
        description=(
            "要在可附加浏览器中打开的 URL，可传完整 URL 或裸域名。"
            "当前 BoxTeam 工作台使用 http://127.0.0.1:8011/；工作区后端端口是 Gateway 动态代理目标，"
            "不要把 8010 当作浏览器验收入口。游戏验收应优先使用任务提供的预览地址（例如 8765）。"
        )
    )
    forceNew: bool = Field(
        default=False,
        description="为 false 时优先接管当前会话已有的运行中浏览器；为 true 时新建浏览器。",
    )


class ListBrowserPageInput(BaseModel):
    """只读浏览器页面列表无需调用参数。"""


class PageIdInput(BaseModel):
    pageId: str = Field(
        description=(
            "浏览器会话 pageId，由 openBrowserPage 返回，后续始终复用该值。"
            "不要把 readPage/navigatePage 返回的 activePageId、tabId 或底层 page_id 当作 pageId。"
        )
    )


class NavigatePageInput(PageIdInput):
    type: Literal[
        "url",
        "back",
        "forward",
        "reload",
        "new_tab",
        "activate_tab",
        "close_tab",
    ] = Field(
        default="url",
        description="导航类型；标签页 ID 可从 readPage 返回的 pages 获取。",
    )
    url: str | None = Field(default=None, description="type=url 时要打开的 URL，可传完整 URL 或裸域名。")
    tabId: str | None = Field(default=None, description="activate_tab/close_tab 时的标签页 ID。")


class ElementInput(PageIdInput):
    ref: str | None = Field(default=None, description="readPage 返回的元素 ref。")
    selector: str | None = Field(default=None, description="Playwright selector。")
    element: str | None = Field(default=None, description="人类可读的元素说明。")


class ClickElementInput(ElementInput):
    dblClick: bool = Field(default=False, description="是否双击。")
    button: Literal["left", "right", "middle"] = Field(default="left", description="鼠标按钮。")


class TypeInPageInput(ElementInput):
    text: str | None = Field(default=None, description="要输入的文本。")
    submit: bool = Field(default=False, description="输入后是否按 Enter。")
    key: str | None = Field(default=None, description="要按下的键或组合键。")


class DragElementInput(PageIdInput):
    fromRef: str | None = Field(default=None, description="拖拽来源元素 ref。")
    fromSelector: str | None = Field(default=None, description="拖拽来源 Playwright selector。")
    fromElement: str | None = Field(default=None, description="拖拽来源的人类可读说明。")
    toRef: str | None = Field(default=None, description="释放目标元素 ref。")
    toSelector: str | None = Field(default=None, description="释放目标 Playwright selector。")
    toElement: str | None = Field(default=None, description="释放目标的人类可读说明。")


class HandleDialogInput(PageIdInput):
    acceptModal: bool | None = Field(default=None, description="是否接受 modal 对话框。")
    promptText: str | None = Field(default=None, description="prompt 对话框输入文本。")
    selectFiles: list[str] | None = Field(default=None, description="文件选择对话框要选择的绝对路径。")


class ScreenshotPageInput(ElementInput):
    scrollIntoViewIfNeeded: bool = Field(default=False, description="截图前是否滚动目标元素到可见区域。")


class RunPlaywrightCodeInput(PageIdInput):
    code: str = Field(
        description="要执行的 Playwright JS 代码。必须通过 page 对象访问页面；超时后页面会被重置，可在 readPage 后重试。"
    )
    timeoutMs: int = Field(
        default=10000,
        ge=1,
        le=60000,
        description="最大等待毫秒数；超时会取消当前脚本、重置页面并返回可重试错误。",
    )


def _browser_page_summary(browser: dict[str, Any]) -> dict[str, Any]:
    browser_id = browser.get("browser_id")
    if not isinstance(browser_id, str) or not browser_id:
        raise RuntimeError(f"浏览器记录缺少 browser_id: {browser!r}")

    raw_pages = browser.get("pages")
    if raw_pages is None:
        raw_pages = []
    if not isinstance(raw_pages, list):
        raise TypeError(f"浏览器记录 pages 不是数组: browser_id={browser_id}")
    pages: list[dict[str, Any]] = []
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict):
            raise TypeError(f"浏览器标签页记录不是对象: browser_id={browser_id}")
        page_id = raw_page.get("page_id")
        if not isinstance(page_id, str) or not page_id:
            raise RuntimeError(f"浏览器标签页缺少 page_id: browser_id={browser_id}")
        pages.append(
            {
                "tabId": page_id,
                "title": raw_page.get("title"),
                "url": raw_page.get("actual_url") or raw_page.get("url"),
                "active": raw_page.get("active") is True,
            }
        )

    return {
        "pageId": browser_id,
        "browserId": browser_id,
        "title": browser.get("title"),
        "url": browser.get("actual_url") or browser.get("url"),
        "status": browser.get("status"),
        "resourceState": browser.get("resource_state"),
        "clientCount": browser.get("client_count", 0),
        "activePageId": browser.get("active_page_id"),
        "pages": pages,
        "agentAccessLocked": browser.get("agent_access_locked") is True,
        "updatedAt": browser.get("updated_at"),
    }


def _stable_page_tool_result(
    browser: dict[str, Any],
    *,
    browser_id: str | None = None,
) -> dict[str, Any]:
    """保留页面详情，同时把浏览器会话句柄与内部标签页 ID 明确分开。"""
    resolved_browser_id = browser_id or browser.get("browser_id")
    if not isinstance(resolved_browser_id, str) or not resolved_browser_id:
        raise RuntimeError(f"浏览器记录缺少 browser_id: {browser!r}")
    result = dict(browser)
    result.pop("page_id", None)
    result.pop("active_page_id", None)
    if "pages" in browser:
        summary_browser = {
            "browser_id": resolved_browser_id,
            "title": browser.get("title"),
            "actual_url": browser.get("actual_url"),
            "url": browser.get("url"),
            "status": browser.get("status"),
            "resource_state": browser.get("resource_state"),
            "client_count": browser.get("client_count", 0),
            "active_page_id": browser.get("active_page_id"),
            "pages": browser.get("pages"),
            "agent_access_locked": browser.get("agent_access_locked"),
            "updated_at": browser.get("updated_at"),
        }
        result.update(_browser_page_summary(summary_browser))
    else:
        result["activePageId"] = browser.get("active_page_id")
    result["pageId"] = resolved_browser_id
    result["browserId"] = resolved_browser_id
    return result


def _retryable_browser_error_result(error: BrowserManagerRequestError) -> str:
    payload: dict[str, Any] = {
        "status": "error",
        "error": str(error),
        "retryable": True,
    }
    if isinstance(error.code, str) and error.code:
        payload["code"] = error.code
    if isinstance(error.recovery, str) and error.recovery:
        payload["recovery"] = error.recovery
    timeout_ms = getattr(error, "timeout_ms", None)
    if isinstance(timeout_ms, int) and timeout_ms > 0:
        payload["timeoutMs"] = timeout_ms
    return _json_result(payload)


async def _run_retryable_browser_operation(
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any] | str:
    try:
        return await operation()
    except BrowserManagerRequestError as error:
        if error.retryable is not True:
            raise
        return _retryable_browser_error_result(error)


def create_list_browser_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def list_browser_page() -> str:
        browsers = context.browser_manager_client.list_browsers_from_state(
            context.session_id
        )
        pages = [
            _browser_page_summary(dict(browser))
            for browser in browsers
            if browser.get("status") != "deleted"
        ]
        return _json_result({"count": len(pages), "pages": pages})

    return StructuredTool.from_function(
        coroutine=list_browser_page,
        name="listBrowserPage",
        description=(
            "只读列出当前 Session 中未删除的可附加浏览器页面，返回 pageId、"
            "网址、标题、资源状态、连接人数和标签页摘要；不会创建、唤醒或导航页面。"
        ),
        args_schema=ListBrowserPageInput,
    )


def create_open_browser_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def open_browser_page(url: str, forceNew: bool = False) -> str:
        browser = None
        if not forceNew:
            browser = next(
                (
                    candidate
                    for candidate in context.browser_manager_client.list_browsers_from_state(
                        context.session_id
                    )
                    if candidate.get("status") == "running"
                ),
                None,
            )
        reused = browser is not None
        if browser is None:
            browser = await context.browser_manager_client.create_browser(
                session_id=context.session_id,
                title="Agent browser",
                url=url,
            )
        else:
            browser = await context.browser_manager_client.navigate_page(
                browser_id=str(browser["browser_id"]),
                navigation_type="url",
                url=url,
            )
        browser_id = str(browser["browser_id"])
        page = await context.browser_manager_client.read_page(browser_id)
        return _json_result(
            {
                "pageId": browser_id,
                "browserId": browser_id,
                "url": browser.get("url"),
                "title": browser.get("title"),
                "forceNew": forceNew,
                "reused": reused,
                "summary": page.get("summary"),
            }
        )

    return StructuredTool.from_function(
        coroutine=open_browser_page,
        name="openBrowserPage",
        description=(
            "在可附加浏览器中打开 URL；默认接管当前会话已有页面，forceNew=true 时新建。"
            "BoxTeam 工作台请使用 8011，工作区应用请使用已提供的预览端口（如 8765）；"
            "不要直接导航动态 Workspace backend 的 8010。"
        ),
        args_schema=OpenBrowserPageInput,
    )


def create_read_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def read_page(pageId: str) -> str:
        resolved_page_id = _page_id(pageId)
        page = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.read_page(resolved_page_id)
        )
        if isinstance(page, str):
            return page
        return _json_result(
            _stable_page_tool_result(page, browser_id=resolved_page_id)
        )

    return StructuredTool.from_function(
        coroutine=read_page,
        name="readPage",
        description="读取浏览器页面当前状态，返回文本摘要和可交互元素 ref；导航或页面更新后必须先重新读取再使用旧 ref。",
        args_schema=PageIdInput,
    )


def create_navigate_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def navigate_page(
        pageId: str,
        type: Literal[
            "url",
            "back",
            "forward",
            "reload",
            "new_tab",
            "activate_tab",
            "close_tab",
        ] = "url",
        url: str | None = None,
        tabId: str | None = None,
    ) -> str:
        page = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.navigate_page(
                browser_id=_page_id(pageId),
                navigation_type=type,
                url=url,
                tab_id=tabId,
            )
        )
        if isinstance(page, str):
            return page
        return _json_result(_stable_page_tool_result(page))

    return StructuredTool.from_function(
        coroutine=navigate_page,
        name="navigatePage",
        description="让浏览器跳转、后退、前进、刷新，或新建、激活、关闭标签页。",
        args_schema=NavigatePageInput,
    )


def create_click_element_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def click_element(
        pageId: str,
        ref: str | None = None,
        selector: str | None = None,
        element: str | None = None,
        dblClick: bool = False,
        button: Literal["left", "right", "middle"] = "left",
    ) -> str:
        page = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.click_element(
                _page_id(pageId),
                {
                    "ref": ref,
                    "selector": selector,
                    "element": element,
                    "dblClick": dblClick,
                    "button": button,
                },
            )
        )
        if isinstance(page, str):
            return page
        return _json_result(page)

    return StructuredTool.from_function(
        coroutine=click_element,
        name="clickElement",
        description=(
            "点击浏览器页面中的元素。优先使用 readPage 返回的 ref，也可使用 Playwright selector；"
            "导航后目标可能短暂重建，selector 会进行一次有限重新定位，ref 失效时请重新 readPage。"
        ),
        args_schema=ClickElementInput,
    )


def create_type_in_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def type_in_page(
        pageId: str,
        text: str | None = None,
        submit: bool = False,
        key: str | None = None,
        ref: str | None = None,
        selector: str | None = None,
        element: str | None = None,
    ) -> str:
        page = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.type_in_page(
                _page_id(pageId),
                {
                    "text": text,
                    "submit": submit,
                    "key": key,
                    "ref": ref,
                    "selector": selector,
                    "element": element,
                },
            )
        )
        if isinstance(page, str):
            return page
        return _json_result(page)

    return StructuredTool.from_function(
        coroutine=type_in_page,
        name="typeInPage",
        description="在浏览器页面中输入文本或按键。",
        args_schema=TypeInPageInput,
    )


def create_hover_element_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def hover_element(
        pageId: str,
        ref: str | None = None,
        selector: str | None = None,
        element: str | None = None,
    ) -> str:
        page = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.hover_element(
                _page_id(pageId),
                {"ref": ref, "selector": selector, "element": element},
            )
        )
        if isinstance(page, str):
            return page
        return _json_result(page)

    return StructuredTool.from_function(
        coroutine=hover_element,
        name="hoverElement",
        description="将鼠标悬停到浏览器页面中的元素上。",
        args_schema=ElementInput,
    )


def create_drag_element_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def drag_element(
        pageId: str,
        fromRef: str | None = None,
        fromSelector: str | None = None,
        fromElement: str | None = None,
        toRef: str | None = None,
        toSelector: str | None = None,
        toElement: str | None = None,
    ) -> str:
        page = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.drag_element(
                _page_id(pageId),
                {
                    "fromRef": fromRef,
                    "fromSelector": fromSelector,
                    "fromElement": fromElement,
                    "toRef": toRef,
                    "toSelector": toSelector,
                    "toElement": toElement,
                },
            )
        )
        if isinstance(page, str):
            return page
        return _json_result(page)

    return StructuredTool.from_function(
        coroutine=drag_element,
        name="dragElement",
        description="将浏览器页面中的一个元素拖拽到另一个元素上。",
        args_schema=DragElementInput,
    )


def create_handle_dialog_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def handle_dialog(
        pageId: str,
        acceptModal: bool | None = None,
        promptText: str | None = None,
        selectFiles: list[str] | None = None,
    ) -> str:
        result = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.handle_dialog(
                _page_id(pageId),
                {
                    "acceptModal": acceptModal,
                    "promptText": promptText,
                    "selectFiles": selectFiles,
                },
            )
        )
        if isinstance(result, str):
            return result
        return _json_result(result)

    return StructuredTool.from_function(
        coroutine=handle_dialog,
        name="handleDialog",
        description="响应浏览器页面中的 alert/confirm/prompt 或文件选择对话框。",
        args_schema=HandleDialogInput,
    )


def create_screenshot_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def screenshot_page(
        pageId: str,
        ref: str | None = None,
        selector: str | None = None,
        element: str | None = None,
        scrollIntoViewIfNeeded: bool = False,
    ) -> str:
        result = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.screenshot_page(
                _page_id(pageId),
                {
                    "ref": ref,
                    "selector": selector,
                    "element": element,
                    "scrollIntoViewIfNeeded": scrollIntoViewIfNeeded,
                },
            )
        )
        if isinstance(result, str):
            return result
        return _json_result(result)

    return StructuredTool.from_function(
        coroutine=screenshot_page,
        name="screenshotPage",
        description="捕获浏览器页面或元素截图，返回保存在工作区 .boxteam 下的图片路径。",
        args_schema=ScreenshotPageInput,
    )


def create_run_playwright_code_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def run_playwright_code(pageId: str, code: str, timeoutMs: int = 10000) -> str:
        result = await _run_retryable_browser_operation(
            lambda: context.browser_manager_client.run_playwright_code(
                _page_id(pageId),
                {"code": code, "timeoutMs": timeoutMs},
            )
        )
        if isinstance(result, str):
            return result
        return _json_result(result)

    return StructuredTool.from_function(
        coroutine=run_playwright_code,
        name="runPlaywrightCode",
        description=(
            "对浏览器页面执行一段 Playwright JS 代码。只有其它浏览器工具不足时使用。"
            "pageId 始终使用 openBrowserPage 返回的浏览器会话 ID；超时会返回 status=error、"
            "retryable=true 和 page_reset，可先 readPage 再把长等待拆成有界步骤重试。"
        ),
        args_schema=RunPlaywrightCodeInput,
    )
