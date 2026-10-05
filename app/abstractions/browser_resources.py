"""Browser owner 与 Playwright 运行事实的唯一边界。"""

from __future__ import annotations

from typing import Literal

from app.abstractions.workspace_resources import ResourceSnapshot, ResourceTransport

BrowserAction = Literal[
    "read", "navigate", "click", "hover", "type", "drag", "dialog", "screenshot", "run",
    "resource-policy", "freeze", "wake", "discard", "device-profile", "agent-lock",
]

class BrowserRequestError(RuntimeError):
    def __init__(
        self,
        *,
        method: str,
        path: str,
        status: int,
        payload: object,
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.payload = payload
        self.code = payload.get("code") if isinstance(payload, dict) else None
        self.retryable = (
            payload.get("retryable") if isinstance(payload, dict) else None
        )
        self.recovery = (
            payload.get("recovery") if isinstance(payload, dict) else None
        )
        self.timeout_ms = (
            payload.get("timeout_ms") if isinstance(payload, dict) else None
        )
        error_message = payload.get("error") if isinstance(payload, dict) else None
        detail = (
            error_message
            if isinstance(error_message, str) and error_message
            else f"浏览器管理器请求失败: method={method}, path={path}, status={status}"
        )
        metadata = []
        if isinstance(self.code, str):
            metadata.append(f"code={self.code}")
        if isinstance(self.retryable, bool):
            metadata.append(f"retryable={'true' if self.retryable else 'false'}")
        if isinstance(self.recovery, str):
            metadata.append(f"recovery={self.recovery}")
        if metadata:
            detail = f"{detail} ({', '.join(metadata)})"
        super().__init__(detail)



class BrowserTransport(ResourceTransport):
    async def create_browser(
        self, *, browser_id: str, owner_revision: int, title: str, url: str,
        viewport: dict[str, int] | None = None, device_profile: str | None = None,
        device_orientation: str | None = None,
    ) -> ResourceSnapshot: ...

    async def perform(
        self, browser_id: str, action: BrowserAction, payload: ResourceSnapshot,
        *, actor: str,
    ) -> ResourceSnapshot: ...
