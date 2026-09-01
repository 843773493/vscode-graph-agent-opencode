from __future__ import annotations

import asyncio
import json
import os
from copy import copy
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.core.path_utils import get_boxteam_root
from app.protocol.codecs.browser import browser_page_to_json, browser_page_to_proto
from app.services.infrastructure.config_service import ConfigService

DEFAULT_BROWSER_BACKEND_URL = "http://127.0.0.1:8015"
DEFAULT_BROWSER_REQUEST_TIMEOUT_SECONDS = 30
BROWSER_RUN_REQUEST_GRACE_SECONDS = 10


class BrowserManagerRequestError(RuntimeError):
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


class BrowserManagerClient:
    def __init__(
        self,
        *,
        backend_url: str | None = None,
        state_file: Path | None = None,
        actor: str | None = None,
        config_service: ConfigService | None = None,
    ) -> None:
        configured_backend_url = backend_url or os.environ.get("BOXTEAM_BROWSER_BACKEND_URL")
        self._backend_url = (
            configured_backend_url
            or (
                config_service.get_browser_backend_url()
                if config_service is not None
                else None
            )
            or DEFAULT_BROWSER_BACKEND_URL
        ).rstrip("/")
        self._state_file = state_file or get_boxteam_root() / "browser-manager" / "browsers.json"
        self._prefer_backend_listing = configured_backend_url is not None and state_file is None
        self._actor = actor

    def for_actor(self, actor: str) -> BrowserManagerClient:
        if not actor.strip():
            raise ValueError("browser manager actor 不能为空")
        client = copy(self)
        client._actor = actor
        return client

    @property
    def backend_url(self) -> str:
        return self._backend_url

    def list_browsers_from_state(self, session_id: str) -> list[dict[str, Any]]:
        if self._prefer_backend_listing:
            return self._list_browsers_from_backend(session_id)

        if not self._state_file.exists():
            return []
        raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        browsers = raw.get("browsers")
        if not isinstance(browsers, list):
            raise TypeError(f"浏览器状态文件格式错误: {self._state_file}")
        result = []
        for browser in browsers:
            if not isinstance(browser, dict):
                raise TypeError(f"浏览器状态文件包含非对象记录: {self._state_file}")
            if browser.get("session_id") == session_id:
                normalized = dict(browser)
                normalized.pop("attach_url", None)
                result.append(normalized)
        return sorted(
            result,
            key=lambda browser: str(browser.get("updated_at") or browser.get("created_at") or ""),
            reverse=True,
        )

    def _list_browsers_from_backend(self, session_id: str) -> list[dict[str, Any]]:
        response = self._json_request_sync(
            "GET",
            f"/api/browsers?session_id={quote(session_id)}",
            None,
        )
        data = response.get("data")
        if not isinstance(data, list):
            raise TypeError(f"浏览器管理器列表返回格式错误: {response}")
        result: list[dict[str, Any]] = []
        for browser in data:
            if not isinstance(browser, dict):
                raise TypeError(f"浏览器管理器列表包含非对象记录: {browser!r}")
            normalized = browser_page_to_json(browser_page_to_proto(browser))
            browser_id = normalized.get("browser_id")
            if not isinstance(browser_id, str) or not browser_id:
                raise RuntimeError(f"浏览器记录缺少 browser_id: {normalized}")
            normalized.pop("attach_url", None)
            result.append(normalized)
        return sorted(
            result,
            key=lambda browser: str(browser.get("updated_at") or browser.get("created_at") or ""),
            reverse=True,
        )

    async def create_browser(
        self,
        *,
        session_id: str,
        url: str,
        title: str = "Browser Page",
        viewport: dict[str, int] | None = None,
        device_profile: str | None = None,
        device_orientation: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "title": title,
            "url": url,
            "viewport": viewport or {"width": 1280, "height": 800},
        }
        if device_profile is not None:
            payload["device_profile"] = device_profile
        if device_orientation is not None:
            payload["device_orientation"] = device_orientation
        response = await self._json_request(
            "POST",
            "/api/browsers",
            payload,
        )
        return self._require_data(response)

    async def set_device_profile(
        self,
        *,
        browser_id: str,
        device_profile: str,
        device_orientation: str = "portrait",
    ) -> dict[str, Any]:
        response = await self._json_request(
            "PATCH",
            f"/api/browsers/{browser_id}/device-profile",
            {
                "device_profile": device_profile,
                "device_orientation": device_orientation,
            },
        )
        return self._require_data(response)

    async def get_browser(self, browser_id: str) -> dict[str, Any]:
        response = await self._json_request("GET", f"/api/browsers/{browser_id}")
        return self._require_data(response)

    async def read_page(self, browser_id: str) -> dict[str, Any]:
        response = await self._json_request("GET", f"/api/browsers/{browser_id}/read")
        return self._require_data(response)

    async def navigate_page(
        self,
        *,
        browser_id: str,
        navigation_type: str,
        url: str | None = None,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._json_request(
            "POST",
            f"/api/browsers/{browser_id}/navigate",
            {"type": navigation_type, "url": url, "tab_id": tab_id},
        )
        return self._require_data(response)

    async def click_element(self, browser_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/click", payload)
        return self._require_data(response)

    async def hover_element(self, browser_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/hover", payload)
        return self._require_data(response)

    async def type_in_page(self, browser_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/type", payload)
        return self._require_data(response)

    async def drag_element(self, browser_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/drag", payload)
        return self._require_data(response)

    async def handle_dialog(self, browser_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/dialog", payload)
        return self._require_data(response)

    async def screenshot_page(self, browser_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/screenshot", payload)
        return self._require_data(response)

    async def run_playwright_code(self, browser_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/run", payload)
        return self._require_data(response)

    async def close_browser(self, browser_id: str) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/close")
        return self._require_data(response)

    async def delete_browser(self, browser_id: str) -> dict[str, Any]:
        response = await self._json_request("DELETE", f"/api/browsers/{browser_id}")
        return self._require_data(response)

    async def set_resource_policy(self, browser_id: str, policy: str) -> dict[str, Any]:
        response = await self._json_request(
            "PATCH",
            f"/api/browsers/{browser_id}/resource-policy",
            {"policy": policy},
        )
        return self._require_data(response)

    async def freeze_browser(self, browser_id: str) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/freeze")
        return self._require_data(response)

    async def wake_browser(self, browser_id: str) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/wake")
        return self._require_data(response)

    async def discard_browser(self, browser_id: str) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/browsers/{browser_id}/discard")
        return self._require_data(response)

    def _require_data(self, response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise TypeError(f"浏览器管理器返回格式错误: {response}")
        normalized = self._normalize_data(data)
        normalized.pop("attach_url", None)
        return normalized

    @staticmethod
    def _normalize_data(data: dict[str, Any]) -> dict[str, Any]:
        required_fields = {"browser_id", "page_id", "session_id", "status"}
        if required_fields.issubset(data):
            return browser_page_to_json(browser_page_to_proto(data))
        return dict(data)

    async def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._json_request_sync,
                method,
                path,
                payload,
            )
        except TimeoutError as exc:
            timeout_seconds = self._request_timeout_seconds(method, path, payload)
            requested_timeout_ms = (
                payload.get("timeoutMs")
                if isinstance(payload, dict)
                else None
            )
            timeout_ms = (
                requested_timeout_ms
                if isinstance(requested_timeout_ms, int) and requested_timeout_ms > 0
                else timeout_seconds * 1000
            )
            raise BrowserManagerRequestError(
                method=method,
                path=path,
                status=408,
                payload={
                    "code": "browser_tool_timeout",
                    "error": (
                        "浏览器管理器操作超时: "
                        f"method={method}, path={path}, timeoutMs={timeout_ms}；"
                        "浏览器页面可能已重置，请重新 readPage 后重试"
                    ),
                    "retryable": True,
                    "recovery": "page_reset",
                    "timeout_ms": timeout_ms,
                },
            ) from exc

    def _json_request_sync(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._backend_url}{path}",
            data=body,
            method=method,
            headers={
                "content-type": "application/json",
                **(
                    {"X-BoxTeam-Actor": self._actor}
                    if self._actor is not None
                    else {}
                ),
            },
        )
        try:
            with urlopen(
                request,
                timeout=self._request_timeout_seconds(method, path, payload),
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(detail)
            except json.JSONDecodeError:
                error_payload = None
            if isinstance(error_payload, dict):
                raise BrowserManagerRequestError(
                    method=method,
                    path=path,
                    status=exc.code,
                    payload=error_payload,
                ) from exc
            raise RuntimeError(
                f"浏览器管理器请求失败: method={method}, path={path}, status={exc.code}, detail={detail}"
            ) from exc

    @staticmethod
    def _request_timeout_seconds(
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> int:
        if method == "POST" and path.endswith("/run"):
            requested_timeout_ms = (
                payload.get("timeoutMs")
                if isinstance(payload, dict)
                else None
            )
            if isinstance(requested_timeout_ms, int) and requested_timeout_ms > 0:
                # Browser 服务本身最多接受 60 秒；HTTP 客户端必须在该预算
                # 之外留出响应序列化和代理传输余量，否则 30 秒 urlopen
                # 会先于浏览器的 timeoutMs 把一次合法调用截断。
                return max(
                    DEFAULT_BROWSER_REQUEST_TIMEOUT_SECONDS,
                    requested_timeout_ms // 1000 + BROWSER_RUN_REQUEST_GRACE_SECONDS,
                )
        return DEFAULT_BROWSER_REQUEST_TIMEOUT_SECONDS
