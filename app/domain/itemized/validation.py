"""itemized v2 的跨记录状态转移校验。"""

from __future__ import annotations

from app.domain.itemized.enums import TurnStatus
from app.domain.itemized.errors import ItemSchemaError

TURN_TRANSITIONS: dict[str, frozenset[str]] = {
    TurnStatus.OPEN: frozenset(
        {TurnStatus.ACTIVE, TurnStatus.CANCELLED, TurnStatus.FAILED, TurnStatus.UNKNOWN}
    ),
    TurnStatus.ACTIVE: frozenset(
        {
            TurnStatus.COMPLETED,
            TurnStatus.COMPLETED_EMPTY,
            TurnStatus.INTERRUPTED,
            TurnStatus.CANCELLED,
            TurnStatus.FAILED,
            TurnStatus.UNKNOWN,
        }
    ),
    TurnStatus.INTERRUPTED: frozenset({TurnStatus.ACTIVE}),
    TurnStatus.UNKNOWN: frozenset({TurnStatus.ACTIVE}),
    TurnStatus.COMPLETED: frozenset(),
    TurnStatus.COMPLETED_EMPTY: frozenset(),
    TurnStatus.CANCELLED: frozenset(),
    TurnStatus.FAILED: frozenset(),
}


def validate_turn_transition(
    current: str,
    target: str,
    *,
    explicit_resume: bool = False,
    execution_lost: bool = False,
) -> None:
    """验证 Turn.status 的闭合转移表，防止 terminal 被隐式重开。"""
    if current not in TURN_TRANSITIONS or target not in TURN_TRANSITIONS:
        raise ItemSchemaError(f"未知 Turn.status 转移: {current}->{target}")
    if target == TurnStatus.ACTIVE:
        if current == TurnStatus.INTERRUPTED and explicit_resume:
            return
        if current == TurnStatus.UNKNOWN and explicit_resume and execution_lost:
            return
        raise ItemSchemaError(f"只有显式 resume 可以恢复 Turn: {current}->{target}")
    if target not in TURN_TRANSITIONS[current]:
        raise ItemSchemaError(f"非法 Turn.status 转移: {current}->{target}")
