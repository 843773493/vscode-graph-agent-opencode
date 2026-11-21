from __future__ import annotations

from typing import Literal

from app.schemas.internal_v2.turn import (
    StaleTurnCursorErrorDTO,
    StaleTurnReferenceErrorDTO,
)


class StaleTurnCursorError(RuntimeError):
    def __init__(
        self,
        *,
        session_id: str,
        cursor_epoch: int,
        current_epoch: int,
        cursor_history_view_revision: int | None = None,
        current_history_view_revision: int | None = None,
    ) -> None:
        if (cursor_history_view_revision is None) != (
            current_history_view_revision is None
        ):
            raise ValueError("stale history view revision 必须同时提供游标与当前值")
        reason: Literal["projection_epoch", "history_view_revision"] = (
            "history_view_revision"
            if cursor_history_view_revision is not None
            else "projection_epoch"
        )
        self.detail = StaleTurnCursorErrorDTO(
            reason=reason,
            session_id=session_id,
            cursor_epoch=cursor_epoch,
            current_epoch=current_epoch,
            cursor_history_view_revision=cursor_history_view_revision,
            current_history_view_revision=current_history_view_revision,
            message=(
                "历史视图成员已变化，请重新加载最新历史"
                if reason == "history_view_revision"
                else "Turn 历史已发生破坏性重排，请重新加载最新历史"
            ),
        )
        super().__init__(self.detail.message)


class StaleTurnReferenceError(RuntimeError):
    def __init__(self, *, session_id: str, turn_ids: list[str]) -> None:
        self.detail = StaleTurnReferenceErrorDTO(
            session_id=session_id,
            turn_ids=turn_ids,
            message="请求的历史 Turn 已不属于当前上下文，请从当前视图重新加载",
        )
        super().__init__(self.detail.message)


class InvalidTurnCursorError(ValueError):
    pass
