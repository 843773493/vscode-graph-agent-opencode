from __future__ import annotations

import pytest


class _TestSessionLifecycleGuard:
    def __init__(self) -> None:
        self.checked_session_ids: list[str] = []

    def __call__(self, session_id: str) -> None:
        prefix, separator, identity = session_id.partition("_")
        if prefix not in {"ses", "session"} or not separator or not identity:
            raise AssertionError(f"测试传入了无效的伪 Session identity: {session_id!r}")
        self.checked_session_ids.append(session_id)


@pytest.fixture
def session_lifecycle_guard() -> _TestSessionLifecycleGuard:
    """校验并记录单元测试使用的伪 Session identity。"""
    return _TestSessionLifecycleGuard()
