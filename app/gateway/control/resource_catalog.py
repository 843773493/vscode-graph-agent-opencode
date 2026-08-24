from __future__ import annotations

import asyncio

import httpx

from app.core.path_utils import get_gateway_root
from app.gateway.auth import LOCAL_TOKEN
from app.schemas.gateway_control import (
    GatewayResourceDTO,
    GatewayResourceListDTO,
    GatewayResourceScopeErrorDTO,
)
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.public_v2.common import CursorPage
from app.schemas.public_v2.session import SessionDTO
from app.schemas.public_v2.session_resource import SessionResourceListDTO


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
            sessions = await self._list_sessions(target, request_id=request_id)
        except (httpx.HTTPError, LookupError, RuntimeError, TypeError, ValueError) as error:
            return [], [self._scope_error(target.workspace_id, workspace_label, error)]

        resource_results = await asyncio.gather(
            *(
                self._load_session_resources(
                    target,
                    session,
                    request_id=request_id,
                )
                for session in sessions
            ),
            return_exceptions=True,
        )
        items: list[GatewayResourceDTO] = []
        errors: list[GatewayResourceScopeErrorDTO] = []
        for session, result in zip(sessions, resource_results, strict=True):
            if isinstance(result, BaseException):
                errors.append(
                    self._scope_error(
                        f"{target.workspace_id}:{session.session_id}",
                        f"{workspace_label} · {session.title}",
                        result,
                    )
                )
                continue
            items.extend(result)
        return items, errors

    async def _list_sessions(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
    ) -> list[SessionDTO]:
        sessions: list[SessionDTO] = []
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = await self._get_data(
                target,
                "sessions",
                request_id=request_id,
                params=params,
            )
            page = CursorPage[SessionDTO].model_validate(data)
            sessions.extend(page.items)
            if not page.has_more or not page.next_cursor:
                return sessions
            cursor = page.next_cursor

    async def _load_session_resources(
        self,
        target: WorkspaceTarget,
        session: SessionDTO,
        *,
        request_id: str,
    ) -> list[GatewayResourceDTO]:
        data = await self._get_data(
            target,
            f"sessions/{session.session_id}/resources",
            request_id=request_id,
        )
        resource_list = SessionResourceListDTO.model_validate(data)
        return [
            GatewayResourceDTO(
                gateway_connection_id=target.remote_gateway_connection_id,
                gateway_name=self._gateway_name(target),
                workspace_id=target.workspace_id,
                workspace_name=target.name,
                connection_kind=target.connection_kind,
                session_id=session.session_id,
                session_title=session.title,
                resource=resource,
            )
            for resource in resource_list.items
            if resource.kind in {"browser", "terminal"}
        ]

    async def _get_data(
        self,
        target: WorkspaceTarget,
        path: str,
        *,
        request_id: str,
        params: dict[str, str] | None = None,
    ) -> object:
        url, headers = self._target_request(target, path, request_id=request_id)
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

    def _target_request(
        self,
        target: WorkspaceTarget,
        path: str,
        *,
        request_id: str,
    ) -> tuple[str, dict[str, str]]:
        if target.connection_kind == "remote_gateway":
            connection_id = target.remote_gateway_connection_id
            remote_workspace_id = target.remote_workspace_id
            if connection_id is None or remote_workspace_id is None:
                raise RuntimeError(f"远程工作区投影缺少路由信息: {target.workspace_id}")
            credential = FederationCredentialStore(
                storage_path=get_gateway_root() / "credentials" / "federation.json"
            ).get(connection_id)
            return (
                f"{self._registry.remote_gateway_url(connection_id).rstrip('/')}/api/v1/{path}",
                {
                    "X-BoxTeam-Workspace-Id": remote_workspace_id,
                    "X-BoxTeam-Federation-Token": credential.token,
                    "X-Request-ID": request_id,
                },
            )
        return (
            f"{target.backend_url.rstrip('/')}/api/v1/{path}",
            {"X-Local-Token": LOCAL_TOKEN, "X-Request-ID": request_id},
        )

    def _gateway_name(self, target: WorkspaceTarget) -> str:
        if target.connection_kind != "remote_gateway":
            return "本机 Gateway"
        connection_id = target.remote_gateway_connection_id
        if connection_id is None:
            return "远程 Gateway"
        return self._registry.remote_gateway_connection(connection_id).name

    @staticmethod
    def _scope_error(
        scope_key: str,
        label: str,
        error: BaseException,
    ) -> GatewayResourceScopeErrorDTO:
        return GatewayResourceScopeErrorDTO(
            scope_key=scope_key,
            label=label,
            message=f"{type(error).__name__}: {error}",
        )
