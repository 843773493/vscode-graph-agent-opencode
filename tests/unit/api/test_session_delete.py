from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest

from app.api.sessions import delete_session
from app.schemas.internal_v2.session import DeleteSessionResultDTO

T = TypeVar("T")


class _Resolver:
    def __init__(self, session_ids: list[str]) -> None:
        self.session_ids = session_ids
        self.calls: list[tuple[str, bool]] = []

    def descendant_session_ids(
        self,
        session_id: str,
        *,
        include_self: bool = False,
    ) -> list[str]:
        self.calls.append((session_id, include_self))
        return list(self.session_ids)


class _SessionService:
    def __init__(self, resolver: _Resolver) -> None:
        self.path_resolver = resolver
        self.delete_calls: list[tuple[str, bool]] = []

    async def delete(
        self,
        session_id: str,
        *,
        cascade: bool = False,
    ) -> DeleteSessionResultDTO:
        self.delete_calls.append((session_id, cascade))
        return DeleteSessionResultDTO(session_id=session_id, status="deleted")


class _JobService:
    def __init__(self) -> None:
        self.admitted_session_ids: list[str] = []

    async def run_sessions_delete_operation(
        self,
        session_ids: list[str],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        self.admitted_session_ids = list(session_ids)
        return await operation()


@pytest.mark.asyncio
async def test_delete_session_admits_then_delegates_to_session_service() -> None:
    resolver = _Resolver(["ses_child", "ses_root"])
    session_service = _SessionService(resolver)
    job_service = _JobService()

    response = await delete_session(
        "ses_root",
        cascade=True,
        _="local",
        request_id="req_delete",
        session_service=session_service,
        job_service=job_service,
    )

    assert response.request_id == "req_delete"
    assert response.data is not None
    assert response.data.session_id == "ses_root"
    assert job_service.admitted_session_ids == ["ses_child", "ses_root"]
    assert resolver.calls == [("ses_root", True)]
    assert session_service.delete_calls == [("ses_root", True)]
