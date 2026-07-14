from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.abstractions.session_resources import SessionResourceProviderProtocol
from app.schemas.public_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceDTO,
    SessionResourceKind,
)


class SessionResourceProviderRegistry:
    """注册并分发会话后台资源提供者。"""

    def __init__(
        self,
        providers: Iterable[SessionResourceProviderProtocol] = (),
    ) -> None:
        self._providers: dict[SessionResourceKind, SessionResourceProviderProtocol] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: SessionResourceProviderProtocol) -> None:
        if provider.kind in self._providers:
            raise ValueError(f"后台资源 provider 重复注册: kind={provider.kind}")
        self._providers[provider.kind] = provider

    def providers(self) -> tuple[SessionResourceProviderProtocol, ...]:
        return tuple(self._providers.values())

    def get(self, kind: SessionResourceKind) -> SessionResourceProviderProtocol:
        provider = self._providers.get(kind)
        if provider is None:
            raise ValueError(f"后台资源类型没有注册 provider: kind={kind}")
        return provider

    async def list_resources(self, session_id: str) -> list[SessionResourceDTO]:
        providers = self.providers()
        provider_results = await asyncio.gather(
            *(provider.list_resources(session_id) for provider in providers)
        )
        resources: list[SessionResourceDTO] = []
        for provider, provided_resources in zip(providers, provider_results, strict=True):
            for resource in provided_resources:
                if resource.kind != provider.kind:
                    raise RuntimeError(
                        f"后台资源 provider 返回了错误类型: provider={provider.kind} "
                        f"resource={resource.kind} resource_id={resource.resource_id}"
                    )
            resources.extend(provided_resources)
        return resources

    async def control(
        self,
        *,
        session_id: str,
        kind: SessionResourceKind,
        resource_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        return await self.get(kind).control(
            session_id=session_id,
            resource_id=resource_id,
            action=action,
        )

    async def cleanup_session(self, session_id: str) -> dict[SessionResourceKind, int]:
        cleaned: dict[SessionResourceKind, int] = {}
        for provider in self.providers():
            cleaned[provider.kind] = await provider.cleanup_session(session_id)
        return cleaned
