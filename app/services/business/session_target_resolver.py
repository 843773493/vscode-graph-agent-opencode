from __future__ import annotations

from app.abstractions.session_context import (
    WorkspaceSessionContextClientProtocol,
)
from app.abstractions.session_target import (
    SessionTarget,
    SessionTargetResolutionError,
    SessionTargetResolverProtocol,
)
from app.core.exceptions import NotFoundError
from app.core.trace_middleware import require_current_gateway_id
from app.schemas.gateway import GatewayWorkspaceListDTO
from app.schemas.internal_v2.session_context import SessionContextSearchRequest
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    parse_session_context_ref,
    session_resource_identity,
)
from app.services.business.session_service import SessionService


class SessionTargetResolver(SessionTargetResolverProtocol):
    """解析短 session ID，并把 backend workspace identity 映射为 Gateway 路由 ID。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        workspace_session_context_client: WorkspaceSessionContextClientProtocol,
    ) -> None:
        self._session_service = session_service
        self._workspace_session_context_client = workspace_session_context_client

    async def resolve_session(
        self,
        session_id: str,
        *,
        gateway_workspace_id: str | None = None,
        backend_workspace_id: str | None = None,
    ) -> SessionTarget:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise SessionTargetResolutionError("session_id 不能为空")
        normalized_gateway_workspace_id = (
            gateway_workspace_id.strip()
            if gateway_workspace_id is not None
            else None
        )
        if normalized_gateway_workspace_id == "":
            raise SessionTargetResolutionError("gateway_workspace_id 不能为空")
        if normalized_gateway_workspace_id is not None:
            return SessionTarget(
                session_id=normalized_session_id,
                workspace_id=normalized_gateway_workspace_id,
            )

        normalized_backend_workspace_id = (
            backend_workspace_id.strip()
            if backend_workspace_id is not None
            else None
        )
        if normalized_backend_workspace_id == "":
            raise SessionTargetResolutionError("backend_workspace_id 不能为空")

        if normalized_backend_workspace_id is None:
            try:
                await self._session_service.get(normalized_session_id)
            except NotFoundError:
                return await self._resolve_from_gateway(normalized_session_id)
            return SessionTarget(session_id=normalized_session_id)

        try:
            local_session = await self._session_service.get(normalized_session_id)
        except NotFoundError:
            local_session = None
        if (
            local_session is not None
            and local_session.workspace_id == normalized_backend_workspace_id
        ):
            return SessionTarget(session_id=normalized_session_id)

        gateway_workspace_id = await self.resolve_backend_workspace(
            normalized_backend_workspace_id
        )
        return SessionTarget(
            session_id=normalized_session_id,
            workspace_id=gateway_workspace_id,
        )

    async def resolve_backend_workspace(
        self,
        backend_workspace_id: str,
    ) -> str:
        normalized_backend_workspace_id = backend_workspace_id.strip()
        if not normalized_backend_workspace_id:
            raise SessionTargetResolutionError("backend_workspace_id 不能为空")
        inventory = await self._workspace_session_context_client.list_gateway_workspaces()
        return _gateway_workspace_id_for_backend(
            inventory,
            normalized_backend_workspace_id,
        )

    async def _resolve_from_gateway(self, session_id: str) -> SessionTarget:
        gateway_id = require_current_gateway_id("SessionTargetResolver")
        request = SessionContextSearchRequest(
            resource_identity=gateway_session_inventory_identity(gateway_id),
            uri=gateway_session_inventory_uri(gateway_id),
            scope="gateway",
            query=session_id,
            sources=["session_catalog"],
            max_results=200,
            max_chars=65_536,
        )
        result = await self._workspace_session_context_client.search_gateway_context(
            request
        )
        if (
            result.resource_identity != request.resource_identity
            or result.uri != request.uri
            or result.scope != request.scope
        ):
            raise RuntimeError(
                "Gateway session search 返回的查询身份与请求不一致: "
                f"resource_identity={result.resource_identity!r}, uri={result.uri!r}, "
                f"scope={result.scope!r}"
            )

        candidates: set[tuple[str, str]] = set()
        for match in result.matches:
            try:
                resource = parse_session_context_ref(
                    resource_identity=match.resource_identity,
                    uri=match.uri,
                    scope=match.scope,
                )
            except ValueError as error:
                raise RuntimeError(
                    "Gateway 返回了无法解析的结构化 session match: "
                    f"resource_identity={match.resource_identity!r}, "
                    f"uri={match.uri!r}, scope={match.scope!r}"
                ) from error
            if (
                resource.scope != "workspace"
                or resource.kind != "session"
                or resource.session_id != session_id
            ):
                continue
            expected_identity = session_resource_identity(
                resource.scope_id,
                resource.session_id,
            )
            if resource.resource_identity != expected_identity:
                raise RuntimeError(
                    "Gateway session match identity 与 backend workspace 和 session ID 不一致: "
                    f"resource_identity={resource.resource_identity!r}, "
                    f"backend_workspace_id={resource.scope_id!r}, "
                    f"session_id={resource.session_id!r}"
                )
            candidates.add((resource.scope_id, resource.session_id))

        if (
            result.truncated
            or result.partial_errors
            or result.omitted_partial_error_count
        ):
            partial_error_text = "; ".join(
                f"{item.resource_identity}: {item.error}"
                for item in result.partial_errors
            )
            raise SessionTargetResolutionError(
            f"Gateway session 搜索不完整，不能自动选择可见候选: "
                f"session_id={session_id}, truncated={result.truncated}, "
                "omitted_partial_error_count="
                f"{result.omitted_partial_error_count}"
                + (f"；部分错误：{partial_error_text}" if partial_error_text else "")
            )

        if len(candidates) == 1:
            backend_workspace_id, resolved_session_id = candidates.pop()
            gateway_workspace_id = await self.resolve_backend_workspace(
                backend_workspace_id
            )
            return SessionTarget(
                session_id=resolved_session_id,
                workspace_id=gateway_workspace_id,
            )
        if len(candidates) > 1:
            candidate_text = ", ".join(
                f"backend_workspace_id={backend_workspace}, session_id={candidate_session}"
                for backend_workspace, candidate_session in sorted(candidates)
            )
            raise SessionTargetResolutionError(
                f"session_id={session_id} 在多个工作区中冲突，请补传 gateway_workspace_id；"
                f"候选：{candidate_text}"
            )

        error_text = "; ".join(
            f"{item.resource_identity}: {item.error}" for item in result.partial_errors
        )
        detail = f"；Gateway 部分错误：{error_text}" if error_text else ""
        raise SessionTargetResolutionError(
            f"未找到 session_id={session_id}；请检查 session_id 或补传 gateway_workspace_id"
            f"{detail}"
        )


def _gateway_workspace_id_for_backend(
    inventory: GatewayWorkspaceListDTO,
    backend_workspace_id: str,
) -> str:
    matches = [
        workspace
        for workspace in inventory.items
        if workspace.backend_workspace_id == backend_workspace_id
    ]
    gateway_workspace_ids = {workspace.workspace_id for workspace in matches}
    if not gateway_workspace_ids:
        raise SessionTargetResolutionError(
            "Gateway inventory 中没有对应 backend_workspace_id: "
            f"backend_workspace_id={backend_workspace_id}"
        )
    if len(gateway_workspace_ids) > 1:
        raise SessionTargetResolutionError(
            "多个 Gateway workspace 指向相同 backend_workspace_id: "
            f"backend_workspace_id={backend_workspace_id}, "
            f"workspace_ids={sorted(gateway_workspace_ids)}"
        )
    return next(iter(gateway_workspace_ids))


__all__ = ["SessionTargetResolver"]
