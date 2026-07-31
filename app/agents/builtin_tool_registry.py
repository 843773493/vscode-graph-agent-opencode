from __future__ import annotations

from types import MappingProxyType


_BUILTIN_TOOL_FACTORIES = MappingProxyType(
    {
        "read_context": "app.agents.tools.session_history:create_read_context_tool",
        "search_context": "app.agents.tools.session_history:create_search_context_tool",
        "web_search": "app.agents.tools.web:create_web_search_tool",
        "fetch_webpage": "app.agents.tools.web:create_fetch_webpage_tool",
        "listBrowserPage": "app.agents.tools.browser:create_list_browser_page_tool",
        "openBrowserPage": "app.agents.tools.browser:create_open_browser_page_tool",
        "readPage": "app.agents.tools.browser:create_read_page_tool",
        "navigatePage": "app.agents.tools.browser:create_navigate_page_tool",
        "clickElement": "app.agents.tools.browser:create_click_element_tool",
        "typeInPage": "app.agents.tools.browser:create_type_in_page_tool",
        "hoverElement": "app.agents.tools.browser:create_hover_element_tool",
        "dragElement": "app.agents.tools.browser:create_drag_element_tool",
        "handleDialog": "app.agents.tools.browser:create_handle_dialog_tool",
        "screenshotPage": "app.agents.tools.browser:create_screenshot_page_tool",
        "runPlaywrightCode": "app.agents.tools.browser:create_run_playwright_code_tool",
        "test_tool_2": "app.agents.tools.testing:create_test_tool_2",
        "large_test_output": "app.agents.tools.testing:create_large_test_output_tool",
    }
)


def builtin_tool_ids() -> tuple[str, ...]:
    """返回按注册顺序排列的稳定内置工具 ID。"""
    return tuple(_BUILTIN_TOOL_FACTORIES)


def resolve_builtin_tool_factory(tool_id: str) -> str:
    """将稳定工具 ID 解析为当前版本的工厂实现。"""
    try:
        return _BUILTIN_TOOL_FACTORIES[tool_id]
    except KeyError as exc:
        supported = ", ".join(_BUILTIN_TOOL_FACTORIES)
        raise ValueError(
            f"未知内置工具 ID: {tool_id!r}；支持的 ID: {supported}"
        ) from exc


def builtin_tool_id_for_factory(factory_path: str) -> str | None:
    """返回当前工厂路径对应的稳定 ID；自定义工厂返回 None。"""
    for tool_id, registered_path in _BUILTIN_TOOL_FACTORIES.items():
        if registered_path == factory_path:
            return tool_id
    return None
