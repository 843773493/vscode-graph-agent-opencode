from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import quote

from httpx import AsyncClient, HTTPStatusError, RequestError, TimeoutException

from app.abstractions.terminal.resources import (
    TerminalPluginDisabled,
    TerminalRequestError,
)
from app.core.failures import mark_public_failure
from app.core.trace_middleware import get_current_request_id
from app.core.workspace_identity import validate_workspace_id
from app.gateway.auth import get_gateway_local_token
from app.protocol.codecs.terminal import (
    terminal_session_to_json,
    terminal_session_to_proto,
)
from app.runtime.http_tls import auxiliary_service_tls_context
from app.schemas.gateway.service_plugin import PluginBinding, PluginServiceReference
from app.services.infrastructure.config.workspace_plugins import (
    workspace_plugin_bindings,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection

DEFAULT_TERMINAL_REQUEST_TIMEOUT_SECONDS = 10
logger = logging.getLogger(__name__)


class TerminalManagerClient:
    def __init__(
        self, *, workspace_id: str, config_service: ConfigService | None = None,
        gateway_url: str | None = None, binding: PluginBinding | None = None,
    ) -> None:
        self._workspace_id = validate_workspace_id(workspace_id)
        if binding is not None and (binding.package_id, binding.plugin_id) != ("boxteam.developer-tools", "terminal"):
            raise ValueError("Terminal 客户端只能消费 boxteam.developer-tools/terminal 插件绑定")
        self._config_service = config_service
        self._connection = GatewayTransportConnection(
            gateway_url=gateway_url or os.environ.get("BOXTEAM_GATEWAY_URL"), config_service=config_service,
        )
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
            if self._config_service is None:
                raise ValueError("Terminal 必须使用原工作区配置中的明确插件绑定")
            snapshot = self._config_service.get_snapshot()
            if self._revision != snapshot.revision:
                bindings = workspace_plugin_bindings(snapshot.to_dict(), workspace_id=self._workspace_id)
                self._binding = next((item for item in bindings if item.package_id == "boxteam.developer-tools"
                    and item.plugin_id == "terminal"), None)
                self._revision = snapshot.revision
            binding = self._binding
        if binding is None or not binding.enabled or binding.workspace_id != self._workspace_id:
            raise TerminalPluginDisabled("当前工作区 Terminal 插件未绑定或已关闭")
        return binding

    async def create_terminal(
        self,
        *,
        terminal_id: str,
        owner_revision: int,
        title: str,
        cwd: str,
        agent_id: str | None = None,
        cols: int = 100,
        rows: int = 30,
    ) -> dict[str, object]:
        payload = {
            "terminal_id": terminal_id,
            "owner_revision": owner_revision,
            "workspace_id": self._workspace_id,
            "agent_id": agent_id,
            "title": title,
            "cwd": cwd,
            "cols": cols,
            "rows": rows,
        }
        response = await self._json_request("POST", "/api/terminals", payload)
        return self._require_normalized_terminal_record(response)

    async def get_resource(self, resource_id: str) -> dict[str, object]:
        response = await self._json_request("GET", f"/api/terminals/{resource_id}")
        return self._require_normalized_terminal_record(response)

    async def list_resources(self) -> list[dict[str, object]]:
        path = "/api/terminals"
        response = await self._json_request("GET", path)
        data = response.get("data")
        if not isinstance(data, list):
            raise self._protocol_error("list.data", response)
        result: list[dict[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                raise self._protocol_error("list.item", response)
            result.append(self._normalize_terminal_record(item))
        return result

    async def read_terminal(self, terminal_id: str) -> dict[str, object]:
        response = await self._json_request(
            "POST", f"/api/terminals/{terminal_id}/read"
        )
        return self._require_terminal_envelope(response)

    async def mark_terminal_backgrounded(self, terminal_id: str) -> dict[str, object]:
        response = await self._json_request(
            "POST", f"/api/terminals/{terminal_id}/background"
        )
        return self._require_normalized_terminal_record(response)

    async def claim_terminal_steering(
        self, terminal_id: str, *, completion_event_id: str, command_start_sequence: int,
    ) -> dict[str, object]:
        response = await self._json_request(
            "POST", f"/api/terminals/{terminal_id}/claim-steering",
            {"completion_event_id": completion_event_id, "command_start_sequence": command_start_sequence},
        )
        return self._require_terminal_envelope(response)

    async def finish_terminal_steering(
        self,
        terminal_id: str,
        *,
        completion_event_id: str,
        command_start_sequence: int,
        dispatched: bool,
    ) -> dict[str, object]:
        response = await self._json_request(
            "POST",
            f"/api/terminals/{terminal_id}/finish-steering",
            {"completion_event_id": completion_event_id, "command_start_sequence": command_start_sequence,
             "dispatched": dispatched},
        )
        return self._require_terminal_envelope(response)

    async def write_terminal(
        self,
        *,
        terminal_id: str,
        data: str,
        source: str = "agent",
        command: str | None = None,
    ) -> dict[str, object]:
        response = await self._json_request(
            "POST",
            f"/api/terminals/{terminal_id}/write",
            {
                "data": data,
                "source": source,
                "command": command,
            },
        )
        return self._require_normalized_terminal_record(response)

    async def stop_resource(
        self,
        resource_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, object]:
        response = await self._json_request(
            "POST",
            f"/api/terminals/{resource_id}/kill",
            None if reason is None else {"reason": reason},
        )
        return self._require_terminal_envelope(response)

    async def delete_resource(self, resource_id: str) -> dict[str, object]:
        response = await self._json_request("DELETE", f"/api/terminals/{resource_id}")
        return self._require_terminal_envelope(response)

    async def cancel_creation(self, resource_id: str, *, owner_revision: int) -> dict[str, object]:
        response = await self._json_request("POST", f"/api/terminals/{resource_id}/cancel-creation",
                                            {"owner_revision": owner_revision})
        data = response.get("data")
        if not isinstance(data, dict):
            raise self._protocol_error("cancel_creation.data", response)
        return data

    async def sync_owner_revision(self, resource_id: str, owner_revision: int, *, for_stop: bool) -> None:
        await self._json_request(
            "POST", f"/api/terminals/{resource_id}/owner-revision",
            {"owner_revision": owner_revision, "for_stop": for_stop},
        )

    async def issue_transport_grant(
        self, resource_id: str, *, owner_revision: int, association: str | None,
    ) -> dict[str, object]:
        service: list[PluginServiceReference] = []
        result = await self._json_request(
            "POST", f"/api/terminals/{resource_id}/transport-grant",
            {"owner_revision": owner_revision, "association": association}, service=service,
        )
        data = result.get("data")
        if not isinstance(data, dict):
            raise self._protocol_error("grant.data", result)
        if len(service) != 1:
            raise self._protocol_error("grant.service", result)
        return {**data, "service": service[0].model_dump(mode="json")}

    def _require_normalized_terminal_record(
        self, response: dict[str, object]
    ) -> dict[str, object]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise self._protocol_error("record.data", response)
        return self._normalize_terminal_record(data)

    def _require_terminal_envelope(self, response: dict[str, object]) -> dict[str, object]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise self._protocol_error("envelope.data", response)
        terminal = data.get("terminal")
        if not isinstance(terminal, dict):
            raise self._protocol_error("envelope.terminal", response)
        terminal_envelope = dict(data)
        terminal_envelope["terminal"] = self._normalize_terminal_record(terminal)
        return terminal_envelope

    def _normalize_terminal_record(self, data: dict[str, object]) -> dict[str, object]:
        try:
            normalized = terminal_session_to_json(terminal_session_to_proto(data))
        except (TypeError, ValueError, OverflowError) as exc:
            raise self._protocol_error("terminal", data, cause=exc) from exc
        normalized.pop("attach_url", None)
        return normalized

    @staticmethod
    def _protocol_error(
        stage: str, response: object, *, cause: Exception | None = None,
    ) -> TypeError | ValueError | OverflowError:
        logger.error("终端传输响应协议错误: stage=%s response=%r", stage, response, exc_info=cause)
        message = f"终端管理器响应协议错误: stage={stage}；请查看工作区终端服务日志"
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
        *, service: list[PluginServiceReference] | None = None,
    ) -> dict[str, object]:
        binding = self._current_binding()
        base_path = (f"/api/gateway/service-plugins/{quote(binding.package_id, safe='')}/"
                     f"{quote(binding.plugin_id, safe='')}/services/http")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        try:
            # 单次传输共用总预算，取消时关闭 socket，不遗留线程池请求。
            if self._http_client is None:
                self._http_client = AsyncClient(timeout=DEFAULT_TERMINAL_REQUEST_TIMEOUT_SECONDS,
                    trust_env=False, follow_redirects=False, verify=auxiliary_service_tls_context())
            headers = {
                "content-type": "application/json", "X-Local-Token": get_gateway_local_token(),
                "X-BoxTeam-Workspace-Id": self._workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal",
            }
            if request_id := get_current_request_id():
                headers["X-Request-ID"] = request_id
            async with asyncio.timeout(DEFAULT_TERMINAL_REQUEST_TIMEOUT_SECONDS):
                response = await self._http_client.request(
                    method, self._connection.resolve_gateway_url() + base_path + path, content=body,
                    params={"package_version": binding.package_version, "workspace_id": self._workspace_id},
                    headers=headers,
                )
                response.raise_for_status()
                response_body = response.content
        except HTTPStatusError as exc:
            detail = exc.response.content.decode("utf-8", errors="replace")
            logger.exception(
                "终端传输 HTTP 请求失败: method=%s path=%s status=%s response=%s",
                method, path, exc.response.status_code, detail,
            )
            error = TerminalRequestError(
                method=method, path=path, status=exc.response.status_code, response_body=detail,
            )
            if method == "POST" and path == "/api/terminals":
                try:
                    payload = json.loads(detail)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict):
                    error.creation_receipt = payload.get("creation_receipt")
            raise error from exc
        except TimeoutException as exc:
            raise TimeoutError("终端管理器传输超时") from exc
        except RequestError as exc:
            raise ConnectionError("终端管理器传输失败；请查看工作区终端服务日志") from exc
        if service is not None:
            instance_id = response.headers.get("X-BoxTeam-Plugin-Instance-Id")
            gateway_id = response.headers.get("X-BoxTeam-Plugin-Gateway-Id")
            if not instance_id or not gateway_id:
                raise self._protocol_error("grant.service", response_body)
            service.append(PluginServiceReference(gateway_id=gateway_id, package_id=binding.package_id,
                package_version=binding.package_version, plugin_id=binding.plugin_id,
                instance_id=instance_id, service_id="socket", workspace_id=self._workspace_id))
        try:
            result = json.loads(response_body.decode("utf-8"))
        except ValueError as exc:
            raise self._protocol_error("json", response_body, cause=exc) from exc
        if not isinstance(result, dict):
            raise self._protocol_error("response", result)
        return result
