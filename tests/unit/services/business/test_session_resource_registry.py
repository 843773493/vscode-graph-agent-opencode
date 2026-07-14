from __future__ import annotations

from datetime import datetime

import pytest

from app.schemas.public_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceDTO,
    SessionResourceKind,
)
from app.services.business.session_resource_registry import (
    SessionResourceProviderRegistry,
)


class FakeResourceProvider:
    def __init__(
        self,
        *,
        kind: SessionResourceKind,
        returned_kind: SessionResourceKind | None = None,
    ) -> None:
        self.kind = kind
        self.returned_kind = returned_kind or kind
        self.control_calls: list[tuple[str, str, SessionResourceAction]] = []
        self.cleanup_calls: list[str] = []

    async def list_resources(self, session_id: str) -> list[SessionResourceDTO]:
        now = datetime(2026, 7, 13, 12, 0, 0)
        return [
            SessionResourceDTO(
                resource_id=f"{self.kind}_1",
                session_id=session_id,
                kind=self.returned_kind,
                name=f"测试资源 {self.kind}",
                status="running",
                created_at=now,
                updated_at=now,
            )
        ]

    async def control(
        self,
        *,
        session_id: str,
        resource_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        self.control_calls.append((session_id, resource_id, action))
        return SessionResourceControlResultDTO(
            session_id=session_id,
            resource_id=resource_id,
            kind=self.kind,
            action=action,
            status="cancelled",
        )

    async def cleanup_session(self, session_id: str) -> int:
        self.cleanup_calls.append(session_id)
        return 1


@pytest.fixture
def providers() -> tuple[FakeResourceProvider, FakeResourceProvider]:
    return (
        FakeResourceProvider(kind="background_task"),
        FakeResourceProvider(kind="terminal"),
    )


@pytest.mark.asyncio
async def test_registry_dispatches_registered_providers(
    providers: tuple[FakeResourceProvider, FakeResourceProvider],
) -> None:
    background_provider, terminal_provider = providers
    registry = SessionResourceProviderRegistry(providers)

    resources = await registry.list_resources("ses_registry")
    control_result = await registry.control(
        session_id="ses_registry",
        kind="terminal",
        resource_id="terminal_1",
        action="cancel",
    )
    cleaned = await registry.cleanup_session("ses_registry")

    assert [resource.kind for resource in resources] == ["background_task", "terminal"]
    assert control_result.kind == "terminal"
    assert background_provider.control_calls == []
    assert terminal_provider.control_calls == [
        ("ses_registry", "terminal_1", "cancel")
    ]
    assert cleaned == {"background_task": 1, "terminal": 1}


def test_registry_rejects_duplicate_kind(
    providers: tuple[FakeResourceProvider, FakeResourceProvider],
) -> None:
    background_provider, _ = providers
    registry = SessionResourceProviderRegistry([background_provider])

    with pytest.raises(ValueError, match="重复注册"):
        registry.register(FakeResourceProvider(kind="background_task"))


@pytest.mark.asyncio
async def test_registry_rejects_provider_returning_wrong_kind() -> None:
    registry = SessionResourceProviderRegistry(
        [FakeResourceProvider(kind="browser", returned_kind="terminal")]
    )

    with pytest.raises(RuntimeError, match="错误类型"):
        await registry.list_resources("ses_wrong_kind")
