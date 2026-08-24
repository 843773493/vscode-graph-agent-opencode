from __future__ import annotations

from typing import Literal

from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.schemas.public_v2.session import SessionDTO, SessionGenerationOriginDTO
from app.services.business.session_service import SessionService


class SessionContextForkService:
    """协调会话节点创建，rollout 数据由 Saver 统一物化。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        checkpointer: RolloutCheckpointSaver,
    ) -> None:
        self._session_service = session_service
        self._checkpointer = checkpointer

    async def fork(
        self,
        source_session_id: str,
        *,
        generation_origin: SessionGenerationOriginDTO | None = None,
        title: str | None = None,
        parent_node_id: str | None = None,
        place_under_source: bool = False,
        pinned: bool = False,
        mode: Literal[
            "context_fork", "history_prefix_fork", "full_rollout_copy"
        ] = "context_fork",
        turn_id: str | None = None,
        anchor_mode: Literal["inclusive", "before"] = "inclusive",
        checkpoint_id: str | None = None,
        anchor: str | None = None,
        checkpoint_ns: str = "",
    ) -> SessionDTO:
        source_session = await self._session_service.get(source_session_id)
        physical_parent_node_id = (
            source_session_id if place_under_source else parent_node_id
        )
        physical_parent_session_id = (
            source_session_id
            if place_under_source
            else self._session_service.path_resolver.nearest_session_ancestor(
                parent_node_id
            )
        )
        child_session = await self._session_service.create_context_fork(
            title=title or f"{source_session.title}（上下文副本）",
            agent_id=source_session.current_agent_id,
            parent_session_id=physical_parent_session_id,
            context_source_session_id=source_session_id,
            generation_origin=generation_origin,
            parent_node_id=physical_parent_node_id,
        )

        try:
            await self._checkpointer.afork(
                source_session_id=source_session_id,
                target_session_id=child_session.session_id,
                mode=mode,
                turn_id=turn_id,
                anchor_mode=anchor_mode,
                checkpoint_id=checkpoint_id,
                anchor=anchor,
                relationship="pinned" if pinned else "detached",
                checkpoint_ns=checkpoint_ns,
            )
        except BaseException:
            await self._checkpointer.adelete_thread(child_session.session_id)
            await self._session_service.delete(child_session.session_id)
            raise

        return child_session


__all__ = ["SessionContextForkService"]
