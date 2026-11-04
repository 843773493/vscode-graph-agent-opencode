from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.failures import report_failure
from app.gateway.proxy_upstream import load_proxy_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.gateway_control import (
    GatewayResourceDTO,
    GatewayResourceListDTO,
    GatewayResourceScopeErrorDTO,
)
from app.schemas.internal_v2.workspace_resource import WorkspaceResourceListDTO

logger = logging.getLogger(__name__)


class GatewayResourceCatalogService:
    """通过工作区 API 聚合 Gateway 全局的可连接资源。

    Gateway 不读取工作区业务目录，只负责按注册表路由查询并附加作用域身份。
    """

    def __init__(
        self,
        *,
        registry: GatewayWorkspaceRegistry,
        http_client: httpx.AsyncClient,
        max_concurrency: int = 8,
        request_timeout_seconds: float = 8.0,
    ) -> None:
        self._registry = registry
        self._http_client = http_client
        self._max_concurrency = max_concurrency
        self._request_timeout_seconds = request_timeout_seconds

    async def list(self, *, request_id: str) -> GatewayResourceListDTO:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def load(target: WorkspaceTarget) -> tuple[
            list[GatewayResourceDTO],
            list[GatewayResourceScopeErrorDTO],
        ]:
            async with semaphore:
                return await self._load_workspace(target, request_id=request_id)

        results = await asyncio.gather(
            *(load(target) for target in self._registry.targets())
        )
        return GatewayResourceListDTO(
            items=[item for items, _errors in results for item in items],
            errors=[error for _items, errors in results for error in errors],
        )

    async def _load_workspace(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
    ) -> tuple[list[GatewayResourceDTO], list[GatewayResourceScopeErrorDTO]]:
        workspace_label = (
            f"{self._gateway_name(target)} · {target.name}"
        )
        try:
            data = await self._get_data(target, "resources", request_id=request_id)
            resources = WorkspaceResourceListDTO.model_validate(data)
            if resources.workspace_id != target.workspace_id:
                raise ValueError(
                    f"资源目录工作区身份冲突: expected={target.workspace_id}, actual={resources.workspace_id}"
                )
            for resource in resources.items:
                if resource.workspace_id != resources.workspace_id:
                    raise ValueError(
                        f"资源工作区身份冲突: expected={resources.workspace_id}, actual={resource.workspace_id}"
                    )
        except (httpx.HTTPError, LookupError, RuntimeError, TypeError, ValueError) as error:
            return [], [self._scope_error(f"{target.gateway_id}/{target.workspace_id}", workspace_label, error, request_id=request_id)]
        return [
            GatewayResourceDTO(
                gateway_id=target.gateway_id,                gateway_connection_id=self._registry.remote_connection_for_target(target).connection_id,
                gateway_name=self._gateway_name(target),
                workspace_id=target.workspace_id,
                workspace_name=target.name,
                connection_kind=target.connection_kind,
                session_id=resource.source_thread.session_id,
                session_title=resource.source_thread.session_id,
                resource=resource,
            )
            for resource in resources.items
        ], []

    async def _get_data(
        self,
        target: WorkspaceTarget,
        path: str,
        *,
        request_id: str,
        params: dict[str, str] | None = None,
    ) -> object:
        url, headers = self.target_request(target, path, request_id=request_id)
        response = await self._http_client.get(
            url,
            headers=headers,
            params=params,
            timeout=self._request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError(f"工作区 API 响应不是对象: {path}")
        data = payload.get("data")
        if data is None:
            raise ValueError(
                f"工作区 API 响应缺少 data: {path}, message={payload.get('message', '')}"
            )
        return data

    def target_request(
        self,
        target: WorkspaceTarget,
        path: str,
        *,
        request_id: str,
    ) -> tuple[str, dict[str, str]]:
        headers = self._registry.target_request_headers(target)
        headers["X-BoxTeam-Gateway-Id"] = load_proxy_gateway_id()
        headers["X-Request-ID"] = request_id
        if target.connection_kind == "remote_gateway":
            connection_id = self._registry.remote_connection_for_target(target).connection_id
            assert connection_id is not None
            return (
                f"{self._registry.remote_gateway_url(connection_id).rstrip('/')}/api/v1/{path}",
                headers,
            )
        return (
            f"{target.backend_url.rstrip('/')}/api/v1/{path}",
            headers,
        )

    def _gateway_name(self, target: WorkspaceTarget) -> str:
        if target.connection_kind != "remote_gateway":
            return "本机 Gateway"
        return " / ".join(self._registry.remote_gateway_connection(identity).name for identity in target.remote_gateway_connection_ids)

    @staticmethod
    def _scope_error(
        scope_key: str,
        label: str,
        error: BaseException,
        *,
        request_id: str,
    ) -> GatewayResourceScopeErrorDTO:
        message = f"工作区资源目录读取失败: {type(error).__name__}"
        if isinstance(error, httpx.HTTPStatusError):
            message += f", status={error.response.status_code}"
        return GatewayResourceScopeErrorDTO(
            scope_key=scope_key,
            label=label,
            message=report_failure(
                error, detail=message, diagnostic_logger=logger,
                extra={"workspace_id": scope_key, "request_id": request_id, "stage": "gateway-resource-catalog"},
            ),
        )
