from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NotRequired, Protocol, TypedDict, runtime_checkable

from app.core.background_task_registry import BackgroundTaskHandle
from app.schemas.public_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceDTO,
    SessionResourceKind,
)


@runtime_checkable
class TerminalManagerClientProtocol(Protocol):
    def list_terminals_from_state(self, session_id: str) -> list[dict[str, object]]: ...

    async def kill_terminal(self, terminal_id: str) -> dict[str, object]: ...

    async def delete_terminal(self, terminal_id: str) -> dict[str, object]: ...


class BrowserViewport(TypedDict, total=False):
    width: int
    height: int


class BrowserRecord(TypedDict, total=False):
    browser_id: str
    page_id: str
    session_id: str
    title: str
    url: str
    viewport: BrowserViewport
    status: str
    created_at: str
    updated_at: str
    started_at: str | None
    ended_at: str | None
    client_count: int
    sequence: int
    pending_dialog: object | None
    pending_file_chooser: bool


class BrowserToolResult(TypedDict, total=False):
    browser_id: str
    browser: BrowserRecord
    deleted: bool
    title: str
    url: str
    text: str
    content: str
    screenshot_path: str
    data_url: str
    output: object
    state: BrowserRecord
    result: object
    message: str


class BrowserActionPayload(TypedDict, total=False):
    selector: str
    text: str
    url: str
    type: str
    x: float
    y: float
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    timeout_ms: int
    wait_after_ms: int
    button: str
    force: bool
    accept: bool
    prompt_text: str
    full_page: bool
    code: str
    select_files: NotRequired[list[str]]


@runtime_checkable
class BrowserManagerClientProtocol(Protocol):
    def list_browsers_from_state(self, session_id: str) -> list[BrowserRecord]: ...

    async def create_browser(
        self,
        *,
        session_id: str,
        url: str,
        title: str = "Browser Page",
        viewport: dict[str, int] | None = None,
    ) -> BrowserRecord: ...

    async def read_page(self, browser_id: str) -> BrowserToolResult: ...

    async def navigate_page(
        self,
        *,
        browser_id: str,
        navigation_type: str,
        url: str | None = None,
    ) -> BrowserToolResult: ...

    async def click_element(self, browser_id: str, payload: BrowserActionPayload) -> BrowserToolResult: ...

    async def hover_element(self, browser_id: str, payload: BrowserActionPayload) -> BrowserToolResult: ...

    async def type_in_page(self, browser_id: str, payload: BrowserActionPayload) -> BrowserToolResult: ...

    async def drag_element(self, browser_id: str, payload: BrowserActionPayload) -> BrowserToolResult: ...

    async def handle_dialog(self, browser_id: str, payload: BrowserActionPayload) -> BrowserToolResult: ...

    async def screenshot_page(self, browser_id: str, payload: BrowserActionPayload) -> BrowserToolResult: ...

    async def run_playwright_code(self, browser_id: str, payload: BrowserActionPayload) -> BrowserToolResult: ...

    async def close_browser(self, browser_id: str) -> BrowserRecord: ...

    async def delete_browser(self, browser_id: str) -> BrowserToolResult: ...


@runtime_checkable
class HistoricalTerminalRecordReaderProtocol(Protocol):
    def read_records(
        self,
        *,
        session_id: str,
        active_terminals: Sequence[Mapping[str, object]],
        agent_state_records: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]: ...


@runtime_checkable
class BackgroundTaskRegistryProtocol(Protocol):
    def list_handles(self, session_id: str) -> list[BackgroundTaskHandle]: ...

    def list_closed_handles(self, session_id: str) -> list[BackgroundTaskHandle]: ...

    def get_handle(self, session_id: str, task_id: str) -> BackgroundTaskHandle | None: ...

    async def cancel(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def delete(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def delete_session(self, session_id: str) -> int: ...


@runtime_checkable
class SessionResourceMessageProtocol(Protocol):
    async def list_agent_state_records(
        self,
        session_id: str,
        *,
        strict: bool = False,
    ) -> list[dict[str, object]]: ...

    def append_system_reminder(
        self,
        *,
        session_id: str,
        reminder: str,
        response_metadata: dict[str, object],
        checkpoint_source: str,
        assistant_text: str = "",
        assistant_response_metadata: dict[str, object] | None = None,
    ) -> bool: ...


@runtime_checkable
class SessionResourceProviderProtocol(Protocol):
    """一种会话后台资源的显式生命周期提供者。"""

    kind: SessionResourceKind

    async def list_resources(self, session_id: str) -> list[SessionResourceDTO]: ...

    async def control(
        self,
        *,
        session_id: str,
        resource_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO: ...

    async def cleanup_session(self, session_id: str) -> int: ...
