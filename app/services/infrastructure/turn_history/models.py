from __future__ import annotations

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
    ) -> None:
        self.detail = StaleTurnCursorErrorDTO(
            session_id=session_id,
            cursor_epoch=cursor_epoch,
            current_epoch=current_epoch,
            message="Turn 历史已发生破坏性重排，请重新加载最新历史",
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
