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
from app.schemas.public_v2.session_context import SessionContextSearchRequest
from app.services.business.session_context_resource import (
    parse_session_context_resource,
)
from app.services.business.session_service import SessionService


class SessionTargetResolver(SessionTargetResolverProtocol):
    """解析模型给出的短会话 ID，并在本地不存在时查询 Gateway。"""

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
        workspace_id: str | None = None,
    ) -> SessionTarget:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise SessionTargetResolutionError("session_id 不能为空")
        normalized_workspace_id = (
            workspace_id.strip() if workspace_id is not None else None
        )
        if normalized_workspace_id == "":
            raise SessionTargetResolutionError("workspace_id 不能为空")
        if normalized_workspace_id is not None:
            return SessionTarget(
                session_id=normalized_session_id,
                workspace_id=normalized_workspace_id,
            )

        try:
            await self._session_service.get(normalized_session_id)
        except NotFoundError:
            return await self._resolve_from_gateway(normalized_session_id)
        return SessionTarget(session_id=normalized_session_id)

    async def _resolve_from_gateway(self, session_id: str) -> SessionTarget:
        result = await self._workspace_session_context_client.search_gateway_context(
            SessionContextSearchRequest(
                resource="boxteam://gateway",
                query=session_id,
                sources=["session_catalog"],
                max_results=200,
                max_chars=65_536,
            )
        )
        candidates: set[tuple[str, str]] = set()
        for match in result.matches:
            try:
                resource = parse_session_context_resource(match.locator)
            except ValueError as error:
                raise RuntimeError(
                    "Gateway 返回了无法解析的 session locator: "
                    f"locator={match.locator!r}"
                ) from error
            if (
                resource.kind == "session"
                and resource.workspace_id is not None
                and resource.session_id == session_id
            ):
                candidates.add((resource.workspace_id, resource.session_id))

        if len(candidates) == 1:
            resolved_workspace_id, resolved_session_id = candidates.pop()
            return SessionTarget(
                session_id=resolved_session_id,
                workspace_id=resolved_workspace_id,
            )
        if len(candidates) > 1:
            candidate_text = ", ".join(
                f"workspace_id={workspace}, session_id={candidate_session}"
                for workspace, candidate_session in sorted(candidates)
            )
            raise SessionTargetResolutionError(
                f"session_id={session_id} 在多个工作区中冲突，请补传 workspace_id；"
                f"候选：{candidate_text}"
            )

        error_text = "; ".join(
            f"{item.resource}: {item.error}" for item in result.partial_errors
        )
        detail = f"；Gateway 部分错误：{error_text}" if error_text else ""
        raise SessionTargetResolutionError(
            f"未找到 session_id={session_id}；请检查 session_id 或补传 workspace_id"
            f"{detail}"
        )


__all__ = ["SessionTargetResolver"]
