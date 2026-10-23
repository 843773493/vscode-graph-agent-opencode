"""经当前 Gateway 与原有联邦 channel 传输通信，不直连远端后端。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

import httpx

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import require_current_request_id
from app.gateway.auth import get_gateway_local_token
from app.gateway.federation.errors import FEDERATION_ERROR_CODES
from app.services.business.communication.addresses import (
    GlobalThreadAddress,
    ResolvedMainTargetBinding,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection

logger = logging.getLogger(__name__)


class GatewayCommunicationClient:
    def __init__(self, *, gateway_url: str | None = None, timeout_seconds: float | None = None, config_service: ConfigService | None = None) -> None:
        self._connection = GatewayTransportConnection(gateway_url=gateway_url, timeout_seconds=timeout_seconds, config_service=config_service)

    async def wait_ready(self, *, gateway_id: str, workspace_id: str) -> None:
        """startup 恢复等待原 Gateway 与源 backend 可达；不阻塞 lifespan。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._connection.resolve_timeout_seconds()
        request_id = require_current_request_id("通信 startup Gateway ready")
        headers = {"X-Local-Token": get_gateway_local_token(), "X-Request-ID": request_id}
        async with httpx.AsyncClient(base_url=self._connection.resolve_gateway_url(), trust_env=False, headers=headers) as client:
            for path in ("/api/gateway/health", "/api/gateway/workspaces?check_health=true"):
                last_error = "尚未收到就绪响应"
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise RuntimeError(f"通信 startup 恢复等待超时: gateway_id={gateway_id}, workspace_id={workspace_id}, path={path}, detail={last_error}")
                    try:
                        response = await client.get(path, timeout=remaining)
                    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                        last_error = str(error)
                        await asyncio.sleep(min(0.1, max(deadline - loop.time(), 0)))
                        continue
                    response.raise_for_status()
                    value = response.json()
                    if not isinstance(value, dict) or value.get("request_id") != request_id or response.headers.get("X-Request-ID") != request_id or not isinstance(value.get("data"), dict):
                        raise RuntimeError("通信 startup 恢复响应的 request_id/data 漂移")
                    data = value["data"]
                    if path == "/api/gateway/health":
                        if data.get("gateway_id") != gateway_id:
                            raise RuntimeError("通信 startup 恢复连接的 Gateway 身份漂移")
                        break
                    items = data.get("items")
                    if not isinstance(items, list):
                        raise TypeError("通信 startup 恢复缺少工作区就绪列表")
                    matches = [item for item in items if isinstance(item, dict) and item.get("connection_kind") == "local" and item.get("backend_workspace_id") == workspace_id]
                    if len(matches) != 1:
                        raise RuntimeError("通信 startup 恢复的源 backend 身份未唯一注册")
                    status = matches[0].get("status")
                    if status == "ready":
                        break
                    if status != "offline":
                        raise RuntimeError(f"通信 startup 恢复的源 backend 状态非法: {status!r}")
                    last_error = str(matches[0].get("services", {}).get("workspace_api"))
                    await asyncio.sleep(min(0.1, max(deadline - loop.time(), 0)))

    async def _request(self, path: str, body: dict[str, object], *, timeout: float | None = None) -> dict[str, object]:
        request_id = require_current_request_id("GatewayCommunicationClient")
        async with httpx.AsyncClient(
            base_url=self._connection.resolve_gateway_url(), trust_env=False,
            timeout=timeout if timeout is not None else self._connection.resolve_timeout_seconds(),
            headers={"X-Local-Token": get_gateway_local_token(), "X-Request-ID": request_id},
        ) as client:
            response = await client.post(path, json=body)
        if not response.is_success:
            from app.core.session_control_communication_ledger.observations import (
                RemoteObservationError,
                observation_error_code,
            )
            message = f"Gateway 通信失败: path={path}, request_id={request_id}, stage=http_response, status={response.status_code}"
            observation_code = None
            if response.headers.get("content-type", "").startswith("application/json"):
                try:
                    payload = response.json()
                except ValueError:
                    # 错误正文损坏仍按原 HTTP 拒绝处理，不改变重试分类。
                    payload = None
                observation_code = observation_error_code(payload)
                detail = payload.get("detail") if isinstance(payload, dict) else None
                code = detail.get("code") if isinstance(detail, dict) else None
                if isinstance(code, str) and code in FEDERATION_ERROR_CODES:
                    message += f", code={code}"
            logger.error("%s；原始响应=%s", message, response.text[:2000])
            if observation_code is not None:
                raise RemoteObservationError(observation_code)
            raise WorkspaceSessionContextAccessError(message)
        data = response.json()
        if not isinstance(data, dict) or data.get("request_id") != request_id or not isinstance(data.get("data"), dict):
            raise RuntimeError("Gateway 通信响应缺少匹配的 request_id/data")
        if response.headers.get("X-Request-ID") != request_id:
            raise RuntimeError("Gateway 通信响应头 request_id 漂移")
        return data["data"]

    async def resolve_main(
        self, *, source: GlobalThreadAddress, session_id: str,
        workspace_id: str | None = None,
    ) -> ResolvedMainTargetBinding:
        gateway_id = None
        if session_id.startswith("boxteam://"):
            parsed = parse_resource_uri(session_id, local_gateway_id=source.gateway_id)
            if parsed.scope != "workspace" or parsed.kind != "session" or len(parsed.tail_segments) != 1 or parsed.tail_segments == ("inventory",):
                raise ValueError("通信 target URI 必须指向单个 workspace Session")
            if workspace_id is not None and workspace_id != parsed.scope_id:
                raise ValueError("通信 target URI 与 workspace_id 冲突")
            gateway_id = parsed.gateway_authority or source.gateway_id
            session_id, workspace_id = parsed.tail_segments[0], parsed.scope_id
        data = await self._request("/api/gateway/federation/discovery", {
            "source": asdict(source), "session_id": session_id, "workspace_id": workspace_id,
            "gateway_id": gateway_id,
        })
        target = GlobalThreadAddress(**data["target"])
        return ResolvedMainTargetBinding(target.session_id, data["main_thread_id"], target)

    async def operation(
        self, *, operation: str, target: GlobalThreadAddress,
        body: dict[str, object], timeout: float | None = None,
    ) -> dict[str, object]:
        return await self._request("/api/gateway/federation/relay", {
            "operation": operation, "target": asdict(target), "body": body,
        }, timeout=timeout)
