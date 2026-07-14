from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.core.path_utils import get_boxteam_root


DEFAULT_BROWSER_BACKEND_URL = "http://127.0.0.1:8015"
DEFAULT_BROWSER_FRONTEND_URL = "http://127.0.0.1:8016"


class BrowserManagerClient:
    def __init__(
        self,
        *,
        backend_url: str | None = None,
        frontend_url: str | None = None,
        state_file: Path | None = None,
    ) -> None:
        configured_backend_url = backend_url or os.environ.get("BOXTEAM_BROWSER_BACKEND_URL")
        self._backend_url = (
            configured_backend_url
            or DEFAULT_BROWSER_BACKEND_URL
        ).rstrip("/")
        self._frontend_url = (
            frontend_url
            or os.environ.get("BOXTEAM_BROWSER_FRONTEND_URL")
            or DEFAULT_BROWSER_FRONTEND_URL
        ).rstrip("/")
        self._state_file = state_file or get_boxteam_root() / "browser-manager" / "browsers.json"
        self._prefer_backend_listing = configured_backend_url is not None and state_file is None

    @property
    def backend_url(self) -> str:
        return self._backend_url

    @property
    def frontend_url(self) -> str:
        return self._frontend_url

    def attach_url(self, browser_id: str) -> str:
        return f"{self._frontend_url}/?browserId={browser_id}"

    def list_browsers_from_state(self, session_id: str) -> list[dict[str, Any]]:
        if self._prefer_backend_listing:
            return self._list_browsers_from_backend(session_id)

        if not self._state_file.exists():
            return []
        raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        browsers = raw.get("browsers")
        if not isinstance(browsers, list):
            raise RuntimeError(f"浏览器状态文件格式错误: {self._state_file}")
        result = []
        for browser in browsers:
            if not isinstance(browser, dict):
                raise RuntimeError(f"浏览器状态文件包含非对象记录: {self._state_file}")
            if browser.get("session_id") == session_id:
                normalized = dict(browser)
                normalized["attach_url"] = self.attach_url(str(browser["browser_id"]))
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
            raise RuntimeError(f"浏览器管理器列表返回格式错误: {response}")
        result: list[dict[str, Any]] = []
        for browser in data:
            if not isinstance(browser, dict):
                raise RuntimeError(f"浏览器管理器列表包含非对象记录: {browser!r}")
            normalized = dict(browser)
            browser_id = normalized.get("browser_id")
            if not isinstance(browser_id, str) or not browser_id:
                raise RuntimeError(f"浏览器记录缺少 browser_id: {normalized}")
            normalized["attach_url"] = normalized.get("attach_url") or self.attach_url(browser_id)
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
    ) -> dict[str, Any]:
        response = await self._json_request(
            "POST",
            "/api/browsers",
            {
                "session_id": session_id,
                "title": title,
                "url": url,
                "viewport": viewport or {"width": 1280, "height": 800},
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
    ) -> dict[str, Any]:
        response = await self._json_request(
            "POST",
            f"/api/browsers/{browser_id}/navigate",
            {"type": navigation_type, "url": url},
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

    def _require_data(self, response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"浏览器管理器返回格式错误: {response}")
        return data

    async def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._json_request_sync, method, path, payload)

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
            headers={"content-type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"浏览器管理器请求失败: method={method}, path={path}, status={exc.code}, detail={detail}"
            ) from exc
