from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agents.custom_tools import CustomToolFactoryContext


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _page_id(value: str) -> str:
    resolved = value.strip()
    if not resolved:
        raise ValueError("pageId 不能为空")
    return resolved


class OpenBrowserPageInput(BaseModel):
    url: str = Field(description="要在可附加浏览器中打开的 URL，可传完整 URL 或 www.example.com 这类裸域名。")
    forceNew: bool = Field(default=False, description="保留给 VS Code 兼容语义；当前总是打开新页面。")


class PageIdInput(BaseModel):
    pageId: str = Field(description="浏览器页面 ID，由 openBrowserPage 返回。")


class NavigatePageInput(PageIdInput):
    type: Literal["url", "back", "forward", "reload"] = Field(
        default="url",
        description="导航类型。",
    )
    url: str | None = Field(default=None, description="type=url 时要打开的 URL，可传完整 URL 或裸域名。")


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
    code: str = Field(description="要执行的 Playwright JS 代码。必须通过 page 对象访问页面。")
    timeoutMs: int = Field(default=5000, ge=1, le=60000, description="最大等待毫秒数。")


def create_open_browser_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def open_browser_page(url: str, forceNew: bool = False) -> str:
        browser = await context.browser_manager_client.create_browser(
            session_id=context.session_id,
            title="Agent browser",
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
                "attach_url": browser.get("attach_url"),
                "forceNew": forceNew,
                "summary": page.get("summary"),
            }
        )

    return StructuredTool.from_function(
        coroutine=open_browser_page,
        name="openBrowserPage",
        description="在可附加浏览器中打开 URL，并返回 pageId、attach_url 和页面摘要。",
        args_schema=OpenBrowserPageInput,
    )


def create_read_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def read_page(pageId: str) -> str:
        page = await context.browser_manager_client.read_page(_page_id(pageId))
        return _json_result(page)

    return StructuredTool.from_function(
        coroutine=read_page,
        name="readPage",
        description="读取浏览器页面当前状态，返回文本摘要和可交互元素 ref。",
        args_schema=PageIdInput,
    )


def create_navigate_page_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def navigate_page(
        pageId: str,
        type: Literal["url", "back", "forward", "reload"] = "url",
        url: str | None = None,
    ) -> str:
        page = await context.browser_manager_client.navigate_page(
            browser_id=_page_id(pageId),
            navigation_type=type,
            url=url,
        )
        return _json_result(page)

    return StructuredTool.from_function(
        coroutine=navigate_page,
        name="navigatePage",
        description="让浏览器页面跳转 URL、后退、前进或刷新。",
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
        page = await context.browser_manager_client.click_element(
            _page_id(pageId),
            {
                "ref": ref,
                "selector": selector,
                "element": element,
                "dblClick": dblClick,
                "button": button,
            },
        )
        return _json_result(page)

    return StructuredTool.from_function(
        coroutine=click_element,
        name="clickElement",
        description="点击浏览器页面中的元素。优先使用 readPage 返回的 ref，也可使用 Playwright selector。",
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
        page = await context.browser_manager_client.type_in_page(
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
        page = await context.browser_manager_client.hover_element(
            _page_id(pageId),
            {"ref": ref, "selector": selector, "element": element},
        )
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
        page = await context.browser_manager_client.drag_element(
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
        result = await context.browser_manager_client.handle_dialog(
            _page_id(pageId),
            {
                "acceptModal": acceptModal,
                "promptText": promptText,
                "selectFiles": selectFiles,
            },
        )
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
        result = await context.browser_manager_client.screenshot_page(
            _page_id(pageId),
            {
                "ref": ref,
                "selector": selector,
                "element": element,
                "scrollIntoViewIfNeeded": scrollIntoViewIfNeeded,
            },
        )
        return _json_result(result)

    return StructuredTool.from_function(
        coroutine=screenshot_page,
        name="screenshotPage",
        description="捕获浏览器页面或元素截图，返回保存在工作区 .boxteam 下的图片路径。",
        args_schema=ScreenshotPageInput,
    )


def create_run_playwright_code_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def run_playwright_code(pageId: str, code: str, timeoutMs: int = 5000) -> str:
        result = await context.browser_manager_client.run_playwright_code(
            _page_id(pageId),
            {"code": code, "timeoutMs": timeoutMs},
        )
        return _json_result(result)

    return StructuredTool.from_function(
        coroutine=run_playwright_code,
        name="runPlaywrightCode",
        description="对浏览器页面执行一段 Playwright JS 代码。只有其它浏览器工具不足时使用。",
        args_schema=RunPlaywrightCodeInput,
    )
