"""联邦 RPC 访问工作区权威数据的端口实现。

两条能力各自绑定真实工作区端点，Gateway 不读取任何工作区 ``.boxteam``：

- cold catalog 查询复用既有 ``GET /api/v1/session-catalog/export``；
- main thread 解析要求工作区提供权威 main pointer。**8.6 尚未把 main thread
  暴露到任何工作区 HTTP 合同**，因此该端口在运行时以
  ``federation-session-main-unavailable`` 响亮失败，并点名缺失的合同；绝不猜测
  main thread、绝不返回虚假默认值、也不绕过工作区直接读盘。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable

import httpx

from app.gateway.auth import LOCAL_TOKEN
from app.gateway.federation.errors import (
    FEDERATION_SESSION_MAIN_UNAVAILABLE,
    FederationError,
)
from app.gateway.federation.rpc import FederationSpoke

logger = logging.getLogger(__name__)

#: 工作区权威 main pointer 的合同路径；8.6 落地后由工作区后端提供。
SESSION_MAIN_THREAD_ENDPOINT = "/api/v1/sessions/{session_id}/main-thread"


class FederationSpokeDirectory:
    """由组合根注入的 active spoke 目录；只暴露稳定身份，不泄露 credential。"""

    def __init__(self, *, provider: Callable[[], tuple[FederationSpoke, ...]]) -> None:
        self._provider = provider

    def active_spokes(self) -> tuple[FederationSpoke, ...]:
        return self._provider()


class WorkspaceCatalogPort:
    """按 backend_url 只读查询各本地工作区的 cold session catalog。"""

    def __init__(self, *, request_timeout: float = 5.0) -> None:
        self._request_timeout = request_timeout
        self._backend_urls: dict[str, str] = {}

    def register_workspace(self, *, workspace_id: str, backend_url: str) -> None:
        self._backend_urls[workspace_id] = backend_url.rstrip("/")

    async def _export(self, *, workspace_id: str) -> dict[str, object]:
        request_id = f"federation-catalog-{secrets.token_hex(8)}"
        backend_url = self._backend_urls.get(workspace_id)
        if backend_url is None:
            raise FederationError(
                FEDERATION_SESSION_MAIN_UNAVAILABLE,
                f"本地工作区后端未连接，无法查询 cold catalog: {workspace_id}",
                detail={"workspace_id": workspace_id},
            )
        async with httpx.AsyncClient(timeout=self._request_timeout) as client:
            response = await client.get(
                f"{backend_url}/api/v1/session-catalog/export",
                headers={"X-Local-Token": LOCAL_TOKEN, "X-Request-ID": request_id},
            )
        if response.status_code >= 400:
            raise FederationError(
                FEDERATION_SESSION_MAIN_UNAVAILABLE,
                f"工作区 cold catalog 查询失败: status={response.status_code}",
                detail={"workspace_id": workspace_id},
            )
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise FederationError(
                FEDERATION_SESSION_MAIN_UNAVAILABLE,
                "工作区 cold catalog 导出响应缺少 data",
            )
        return data

    async def local_session_workspaces(self, session_id: str) -> tuple[str, ...]:
        """返回包含该 session 的本地工作区；未授权与不存在不可区分。"""

        found: list[str] = []
        for workspace_id in sorted(self._backend_urls):
            try:
                data = await self._export(workspace_id=workspace_id)
            except FederationError:
                raise
            except Exception as error:  # 不可达即显式失败，不静默
                raise FederationError(
                    FEDERATION_SESSION_MAIN_UNAVAILABLE,
                    f"本地工作区 cold catalog 不可达: {workspace_id}: {error}",
                ) from error
            items = data.get("items")
            if not isinstance(items, list):
                raise FederationError(
                    FEDERATION_SESSION_MAIN_UNAVAILABLE,
                    "工作区 cold catalog 导出缺少 items 数组",
                )
            if any(
                isinstance(item, dict) and item.get("session_id") == session_id
                for item in items
            ):
                found.append(workspace_id)
        return tuple(found)


class WorkspaceSessionMainPort:
    """工作区权威 main thread 解析端口（8.6 合同就绪前 fail closed）。"""

    def __init__(self, *, request_timeout: float = 5.0) -> None:
        self._request_timeout = request_timeout
        self._backend_urls: dict[str, str] = {}

    def register_workspace(self, *, workspace_id: str, backend_url: str) -> None:
        self._backend_urls[workspace_id] = backend_url.rstrip("/")

    async def resolve_main_thread(
        self, *, gateway_id: str, workspace_id: str, session_id: str
    ) -> str:
        del gateway_id
        if workspace_id not in self._backend_urls:
            raise FederationError(
                FEDERATION_SESSION_MAIN_UNAVAILABLE,
                f"目标工作区未注册到当前 Gateway: {workspace_id}",
                detail={"workspace_id": workspace_id},
            )
        raise FederationError(
            FEDERATION_SESSION_MAIN_UNAVAILABLE,
            "工作区尚未暴露权威 main thread 合同，无法解析 target main pointer",
            detail={
                "workspace_id": workspace_id,
                "session_id": session_id,
                "required_endpoint": SESSION_MAIN_THREAD_ENDPOINT.format(
                    session_id=session_id
                ),
                "owner": "8.6",
            },
        )


__all__ = [
    "LOCAL_TOKEN",
    "SESSION_MAIN_THREAD_ENDPOINT",
    "FederationSpokeDirectory",
    "WorkspaceCatalogPort",
    "WorkspaceSessionMainPort",
]
