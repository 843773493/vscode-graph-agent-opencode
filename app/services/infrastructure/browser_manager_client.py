from __future__ import annotations

import asyncio
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.abstractions.browser_resources import BrowserAction, BrowserRequestError
from app.abstractions.workspace_resources import ResourceTransportGrantRejected
from app.core.workspace_identity import validate_workspace_id
from app.protocol.codecs.browser import browser_page_to_json, browser_page_to_proto
from app.services.infrastructure.config_service import ConfigService

DEFAULT_BROWSER_BACKEND_URL = "http://127.0.0.1:8015"
DEFAULT_BROWSER_REQUEST_TIMEOUT_SECONDS = 30
BROWSER_RUN_REQUEST_GRACE_SECONDS = 10
_ACTION_METHODS = {
    "read": "GET", "navigate": "POST", "click": "POST", "hover": "POST", "type": "POST",
    "drag": "POST", "dialog": "POST", "screenshot": "POST", "run": "POST",
    "resource-policy": "PATCH", "freeze": "POST", "wake": "POST", "discard": "POST",
    "device-profile": "PATCH", "agent-lock": "PATCH",
}


class BrowserManagerClient:
    def __init__(
        self, *, workspace_id: str, backend_url: str | None = None,
        config_service: ConfigService | None = None,
    ) -> None:
        configured = backend_url
        self._config_service = config_service if configured is None else None
        self._backend_url = (configured or DEFAULT_BROWSER_BACKEND_URL).rstrip("/")
        self._workspace_id = validate_workspace_id(workspace_id)

    @property
    def backend_url(self) -> str:
        if self._config_service is not None:
            return self._config_service.get_browser_backend_url().rstrip("/")
        return self._backend_url

    def _resolve_backend_url(self) -> str:
        return self.backend_url

    async def create_browser(
        self, *, browser_id: str, owner_revision: int, title: str, url: str,
        viewport: dict[str, int] | None = None, device_profile: str | None = None,
        device_orientation: str | None = None,
    ) -> dict[str, object]:
        return self._runtime(await self._request_data("POST", "/api/browsers", {
            "workspace_id": self._workspace_id, "browser_id": browser_id,
            "owner_revision": owner_revision, "title": title, "url": url,
            "viewport": viewport, "device_profile": device_profile,
            "device_orientation": device_orientation,
        }))

    async def get_resource(self, browser_id: str) -> dict[str, object]:
        return self._runtime(await self._request_data("GET", f"/api/browsers/{browser_id}"))

    async def list_resources(self) -> list[dict[str, object]]:
        response = await self._json_request("GET", "/api/browsers")
        data = response.get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise TypeError(f"Browser 运行列表格式错误: {response}")
        return [self._runtime(item) for item in data]

    async def perform(
        self, browser_id: str, action: BrowserAction, payload: dict[str, object], *, actor: str,
    ) -> dict[str, object]:
        if action not in _ACTION_METHODS:
            raise ValueError(f"Browser 不支持操作: {action}")
        result = await self._request_data(
            _ACTION_METHODS[action], f"/api/browsers/{browser_id}/{action}",
            None if action == "read" else payload, actor=actor,
        )
        return self._envelope(result)

    async def stop_resource(self, browser_id: str, *, reason: str | None = None) -> dict[str, object]:
        return self._envelope(await self._request_data("POST", f"/api/browsers/{browser_id}/close"))

    async def delete_resource(self, browser_id: str) -> dict[str, object]:
        return self._envelope(await self._request_data("DELETE", f"/api/browsers/{browser_id}"))

    async def sync_owner_revision(self, browser_id: str, owner_revision: int) -> None:
        await self._request_data("POST", f"/api/browsers/{browser_id}/owner-revision",
                                 {"owner_revision": owner_revision})

    async def issue_transport_grant(
        self, browser_id: str, *, owner_revision: int, association: str | None,
    ) -> dict[str, object]:
        path = f"/api/browsers/{browser_id}/transport-grant"
        try:
            return await self._request_data(
                "POST", path, {"owner_revision": owner_revision, "association": association},
            )
        except BrowserRequestError as error:
            payload = error.payload
            if (
                error.method != "POST" or error.path != path or error.status != 409
                or not isinstance(payload, dict)
                or payload.get("code") != "browser_transport_grant_rejected"
                or payload.get("operation_outcome") != "rejected"
            ):
                raise
            message = payload.get("error")
            browser = payload.get("browser")
            if not isinstance(message, str) or not message.strip() or not isinstance(browser, dict):
                raise
            # 正式拒绝也必须携带可核身份、状态与非旧版本；普通 409 没有这项证明。
            runtime = self._runtime(browser)
            revision = runtime["transport_owner_revision"]
            if (
                runtime.get("browser_id") != browser_id
                or runtime.get("workspace_id") != self._workspace_id
                or runtime.get("reconciliation_status") != "verified"
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < owner_revision
                or (runtime.get("status") == "running" and revision == owner_revision)
            ):
                raise
            raise ResourceTransportGrantRejected(message, snapshot=runtime) from error

    @staticmethod
    def _runtime(data: dict[str, object]) -> dict[str, object]:
        return browser_page_to_json(browser_page_to_proto(data))

    def _envelope(self, data: dict[str, object]) -> dict[str, object]:
        browser = data.get("browser")
        if not isinstance(browser, dict):
            raise TypeError("Browser 操作回执缺少 browser")
        return {**data, "browser": self._runtime(browser)}

    async def _request_data(
        self, method: str, path: str, payload: dict[str, object] | None = None,
        *, actor: str | None = None,
    ) -> dict[str, object]:
        response = await self._json_request(method, path, payload, actor=actor)
        data = response.get("data")
        if not isinstance(data, dict):
            raise TypeError(f"Browser 管理器返回格式错误: {response}")
        return data

    async def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *, actor: str | None = None,
    ) -> dict[str, object]:
        try:
            return await asyncio.to_thread(
                self._json_request_sync,
                method,
                path,
                payload,
                actor,
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
            raise BrowserRequestError(
                method=method,
                path=path,
                status=408,
                payload={
                    "code": "browser_tool_timeout",
                    "error": (
                        "浏览器管理器操作超时: "
                        f"method={method}, timeoutMs={timeout_ms}；"
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
        payload: dict[str, object] | None,
        actor: str | None = None,
    ) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._resolve_backend_url()}{path}",
            data=body,
            method=method,
            headers={
                "content-type": "application/json",
                "X-BoxTeam-Workspace-Id": self._workspace_id,
                "X-BoxTeam-Resource-Owner": "workspace-browser",
                **({"X-BoxTeam-Actor": actor} if actor is not None else {}),
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
                raise BrowserRequestError(
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
        payload: dict[str, object] | None,
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
