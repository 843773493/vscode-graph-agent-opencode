from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import quote

from httpx import AsyncClient, HTTPStatusError, RequestError, TimeoutException

from app.abstractions.browser_resources import (
    BrowserAction,
    BrowserPluginDisabled,
    BrowserRequestError,
)
from app.abstractions.workspace_resources import ResourceTransportGrantRejected
from app.core.failures import mark_public_failure
from app.core.trace_middleware import get_current_request_id
from app.core.workspace_identity import validate_workspace_id
from app.gateway.auth import get_gateway_local_token
from app.protocol.codecs.browser import browser_page_to_json, browser_page_to_proto
from app.runtime.http_tls import auxiliary_service_tls_context
from app.schemas.gateway.service_plugin import PluginBinding, PluginServiceReference
from app.services.infrastructure.config.workspace_plugins import (
    workspace_plugin_bindings,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection

DEFAULT_BROWSER_REQUEST_TIMEOUT_SECONDS = 30
BROWSER_RUN_REQUEST_GRACE_SECONDS = 10
logger = logging.getLogger(__name__)
_ACTION_METHODS = {
    "read": "GET", "navigate": "POST", "click": "POST", "hover": "POST", "type": "POST",
    "drag": "POST", "dialog": "POST", "screenshot": "POST", "run": "POST",
    "resource-policy": "PATCH", "freeze": "POST", "wake": "POST", "discard": "POST",
    "device-profile": "PATCH", "agent-lock": "PATCH",
}


class BrowserManagerClient:
    def __init__(
        self, *, workspace_id: str, config_service: ConfigService,
        gateway_url: str | None = None, binding: PluginBinding | None = None,
    ) -> None:
        self._workspace_id = validate_workspace_id(workspace_id)
        self._config_service = config_service
        self._connection = GatewayTransportConnection(
            gateway_url=gateway_url or os.environ.get("BOXTEAM_GATEWAY_URL"), config_service=config_service,
        )
        if binding is not None and (binding.package_id, binding.plugin_id) != ("boxteam.developer-tools", "browser"):
            raise ValueError("Browser 客户端只能消费 boxteam.developer-tools/browser 插件绑定")
        self._explicit_binding = binding
        self._binding: PluginBinding | None = None
        self._revision: str | None = None
        self._http_client: AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _current_binding(self) -> PluginBinding:
        if self._explicit_binding is not None:
            binding = self._explicit_binding
        else:
            snapshot = self._config_service.get_snapshot()
            if self._revision != snapshot.revision:
                bindings = workspace_plugin_bindings(snapshot.to_dict(), workspace_id=self._workspace_id)
                self._binding = next((item for item in bindings if item.package_id == "boxteam.developer-tools"
                    and item.plugin_id == "browser"), None)
                self._revision = snapshot.revision
            binding = self._binding
        if binding is None or not binding.enabled or binding.workspace_id != self._workspace_id:
            raise BrowserPluginDisabled("当前工作区 Browser 插件未绑定或已关闭")
        return binding

    async def create_browser(
        self, *, browser_id: str, owner_revision: int, title: str, url: str,
        viewport: dict[str, int] | None = None, device_profile: str | None = None,
        device_orientation: str | None = None,
    ) -> dict[str, object]:
        browser_record = await self._request_data(
            "POST",
            "/api/browsers",
            {
                "workspace_id": self._workspace_id, "browser_id": browser_id,
                "owner_revision": owner_revision, "title": title, "url": url,
                "viewport": viewport, "device_profile": device_profile,
                "device_orientation": device_orientation,
            },
        )
        return self._normalize_browser_record(browser_record)

    async def cancel_creation(self, resource_id: str, *, owner_revision: int) -> dict[str, object]:
        return await self._request_data("POST", f"/api/browsers/{resource_id}/cancel-creation",
                                        {"owner_revision": owner_revision})

    async def get_resource(self, browser_id: str) -> dict[str, object]:
        browser_record = await self._request_data(
            "GET", f"/api/browsers/{browser_id}"
        )
        return self._normalize_browser_record(browser_record)

    async def list_resources(self) -> list[dict[str, object]]:
        response = await self._json_request("GET", "/api/browsers")
        data = response.get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise self._protocol_error("list.data", response)
        return [self._normalize_browser_record(item) for item in data]

    async def perform(
        self, browser_id: str, action: BrowserAction, payload: dict[str, object], *, actor: str,
    ) -> dict[str, object]:
        if action not in _ACTION_METHODS:
            raise ValueError(f"Browser 不支持操作: {action}")
        result = await self._request_data(
            _ACTION_METHODS[action], f"/api/browsers/{browser_id}/{action}",
            None if action == "read" else payload, actor=actor,
        )
        return self._normalize_browser_record_envelope(result)

    async def stop_resource(
        self, browser_id: str, *, reason: str | None = None
    ) -> dict[str, object]:
        close_receipt = await self._request_data(
            "POST", f"/api/browsers/{browser_id}/close"
        )
        return self._normalize_browser_record_envelope(close_receipt)

    async def delete_resource(self, browser_id: str) -> dict[str, object]:
        delete_receipt = await self._request_data(
            "DELETE", f"/api/browsers/{browser_id}"
        )
        return self._normalize_browser_record_envelope(delete_receipt)

    async def sync_owner_revision(self, browser_id: str, owner_revision: int, *, for_stop: bool) -> None:
        data = await self._request_data("POST", f"/api/browsers/{browser_id}/owner-revision",
                                        {"owner_revision": owner_revision, "for_stop": for_stop})
        browser = self._normalize_browser_record(data)
        if (
            browser["browser_id"] != browser_id or browser["workspace_id"] != self._workspace_id
            or browser["transport_owner_revision"] != owner_revision
        ):
            raise self._protocol_error("owner_revision.receipt", data)

    async def issue_transport_grant(
        self, browser_id: str, *, owner_revision: int, association: str | None,
    ) -> dict[str, object]:
        path = f"/api/browsers/{browser_id}/transport-grant"
        service: list[PluginServiceReference] = []
        try:
            grant = await self._request_data(
                "POST", path, {"owner_revision": owner_revision, "association": association}, service=service,
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
            browser_record = self._normalize_browser_record(browser)
            revision = browser_record["transport_owner_revision"]
            if (
                browser_record.get("browser_id") != browser_id
                or browser_record.get("workspace_id") != self._workspace_id
                or browser_record.get("reconciliation_status") != "verified"
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < owner_revision
                or (browser_record.get("status") == "running" and revision == owner_revision)
            ):
                raise
            raise ResourceTransportGrantRejected(message, snapshot=browser_record) from error
        if (
            grant.get("resource_id") != browser_id
            or type(grant.get("owner_revision")) is not int
            or grant["owner_revision"] != owner_revision
            or any(not isinstance(grant.get(key), str) or not grant[key] for key in ("grant_token", "expires_at"))
        ):
            raise self._protocol_error("grant.receipt", grant)
        if len(service) != 1:
            raise self._protocol_error("grant.service", grant)
        return {**grant, "service": service[0].model_dump(mode="json")}

    def _normalize_browser_record(self, data: dict[str, object]) -> dict[str, object]:
        try:
            return browser_page_to_json(browser_page_to_proto(data))
        except (TypeError, ValueError, OverflowError) as exc:
            raise self._protocol_error("browser", data, cause=exc) from exc

    def _normalize_browser_record_envelope(
        self, data: dict[str, object]
    ) -> dict[str, object]:
        browser = data.get("browser")
        if not isinstance(browser, dict):
            raise self._protocol_error("envelope.browser", data)
        return {**data, "browser": self._normalize_browser_record(browser)}

    async def _request_data(
        self, method: str, path: str, payload: dict[str, object] | None = None,
        *, actor: str | None = None, service: list[PluginServiceReference] | None = None,
    ) -> dict[str, object]:
        response = await self._json_request(method, path, payload, actor=actor, service=service)
        data = response.get("data")
        if not isinstance(data, dict):
            raise self._protocol_error("record.data", response)
        if service is not None and len(service) != 1:
            raise self._protocol_error("grant.service", response)
        return data

    @staticmethod
    def _protocol_error(
        stage: str, response: object, *, cause: Exception | None = None,
    ) -> TypeError | ValueError | OverflowError:
        logger.error("Browser 传输响应协议错误: stage=%s response=%r", stage, response, exc_info=cause)
        message = f"浏览器管理器响应协议错误: stage={stage}；请查看工作区浏览器服务日志"
        if isinstance(cause, ValueError):
            return mark_public_failure(ValueError(message))
        if isinstance(cause, OverflowError):
            return mark_public_failure(OverflowError(message))
        return mark_public_failure(TypeError(message))

    async def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *, actor: str | None = None, service: list[PluginServiceReference] | None = None,
    ) -> dict[str, object]:
        binding = self._current_binding()
        base_path = (f"/api/gateway/service-plugins/{quote(binding.package_id, safe='')}/"
                     f"{quote(binding.plugin_id, safe='')}/services/http")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        timeout_seconds = self._request_timeout_seconds(method, path, payload)
        try:
            if self._http_client is None:
                self._http_client = AsyncClient(timeout=timeout_seconds, trust_env=False, follow_redirects=False,
                    verify=auxiliary_service_tls_context())
            headers = {
                "content-type": "application/json", "X-Local-Token": get_gateway_local_token(),
                "X-BoxTeam-Workspace-Id": self._workspace_id,
                "X-BoxTeam-Resource-Owner": "workspace-browser",
                **({"X-BoxTeam-Actor": actor} if actor is not None else {}),
            }
            if request_id := get_current_request_id():
                headers["X-Request-ID"] = request_id
            # 传输截止只表示回执未知，不能据此宣称服务端操作已经终止。
            async with asyncio.timeout(timeout_seconds):
                response = await self._http_client.request(
                    method, self._connection.resolve_gateway_url() + base_path + path, content=body,
                    params={"package_version": binding.package_version, "workspace_id": self._workspace_id},
                    headers=headers, timeout=timeout_seconds,
                )
                response.raise_for_status()
                response_body = response.content
        except (TimeoutError, TimeoutException) as exc:
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
        except HTTPStatusError as exc:
            detail = exc.response.content.decode("utf-8", errors="replace")
            logger.exception(
                "Browser 传输 HTTP 请求失败: method=%s path=%s status=%s response=%s",
                method, path, exc.response.status_code, detail,
            )
            try:
                error_payload = json.loads(detail)
            except json.JSONDecodeError:
                error_payload = None
            if isinstance(error_payload, dict):
                raise BrowserRequestError(
                    method=method,
                    path=path,
                    status=exc.response.status_code,
                    payload=error_payload,
                ) from exc
            error = RuntimeError(
                f"浏览器管理器请求失败: method={method}, status={exc.response.status_code}；请查看工作区浏览器服务日志"
            )
            raise mark_public_failure(error) from exc
        except RequestError as exc:
            raise ConnectionError("浏览器管理器传输失败；请查看工作区浏览器服务日志") from exc
        try:
            result = json.loads(response_body.decode("utf-8"))
        except ValueError as exc:
            raise self._protocol_error("json", response_body, cause=exc) from exc
        if not isinstance(result, dict):
            raise self._protocol_error("response", result)
        if service is not None:
            instance_id = response.headers.get("X-BoxTeam-Plugin-Instance-Id")
            gateway_id = response.headers.get("X-BoxTeam-Plugin-Gateway-Id")
            if instance_id and gateway_id:
                target_gateway = response.headers.get("X-BoxTeam-Target-Gateway-Id")
                if target_gateway and target_gateway != gateway_id:
                    raise self._protocol_error("grant.gateway", response_body)
                service.append(PluginServiceReference(gateway_id=gateway_id, package_id=binding.package_id,
                    package_version=binding.package_version, plugin_id=binding.plugin_id,
                    instance_id=instance_id, service_id="socket", workspace_id=self._workspace_id))
        return result

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
                # 之外留出响应序列化和代理传输余量，否则默认 30 秒 HTTP 预算
                # 会先于浏览器的 timeoutMs 把一次合法调用截断。
                return max(
                    DEFAULT_BROWSER_REQUEST_TIMEOUT_SECONDS,
                    requested_timeout_ms // 1000 + BROWSER_RUN_REQUEST_GRACE_SECONDS,
                )
        return DEFAULT_BROWSER_REQUEST_TIMEOUT_SECONDS
