"""Gateway 控制面 Skill 文件的原始快照 owner，不解释 Skill 语义。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.gateway.skill_sources import (
    GatewaySkillSourceEntryDTO,
    GatewaySkillSourcesDTO,
)
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)


@dataclass(frozen=True, slots=True)
class _GatewaySkillPublication:
    revision: str
    entries: tuple[GatewaySkillSourceEntryDTO, ...]
    available: bool


class GatewaySkillSourceOwner:
    """只从已接受文件事实派生可跨进程传输的无路径快照。"""

    def __init__(self, *, gateway_id: str, skills_root: Path) -> None:
        self.gateway_id = gateway_id
        self._navigation = ResourceNavigation(gateway_id=gateway_id, gateway_skills_root=skills_root)
        self._scope = LifetimeScope("gateway-skill-sources")
        watcher = WorkspaceFileWatchService(workspace_root=skills_root)
        self.file_registry = WorkspaceFileResourceRegistry(
            workspace_root=None, gateway_skills_root=skills_root, watch_service=watcher,
        )
        self._scope.register(watcher.shutdown, label="gateway-skill-file-watcher")
        self._scope.register(self.file_registry.stop, label="gateway-skill-files")
        self._directory_id = self.file_registry.register_skill_directory(
            root=skills_root, scope="gateway", scope_id=gateway_id,
        ).directory_id
        self._publication: _GatewaySkillPublication | None = None
        self._task: asyncio.Task[None] | None = None

    def _publish(self) -> None:
        directory = self.file_registry.skill_directory_snapshot(self._directory_id)
        entries = []
        for entry in directory.entries:
            raw = self.file_registry.snapshot(entry.uri)
            entries.append(GatewaySkillSourceEntryDTO(
                name=entry.name,
                entry_identity=f"skill-entry:gateway:{self.gateway_id}:{entry.name}",
                source_id=f"gateway-skill-source:{self.gateway_id}:{entry.name}",
                display_uri=entry.uri, revision=raw.revision,
                byte_length=len(raw.content.encode("utf-8")), content=raw.content,
                available=raw.available,
                error_code=None if raw.available else "source-unavailable",
            ))
        available = directory.available and all(entry.available for entry in entries)
        # revision 只组合 owner 已接受的原始 revision，不重新读取或 hash 全套正文。
        revision = "sha256:" + hashlib.sha256(canonical_json_bytes({
            "gateway_id": self.gateway_id, "available": available,
            "entries": [(entry.source_id, entry.revision, entry.available) for entry in entries],
        })).hexdigest()
        self._publication = _GatewaySkillPublication(revision, tuple(entries), available)

    async def navigate(self, request: ResourceNavigationRequest, *, deadline: float | None = None) -> ResourceNavigationResultDTO:
        return await self._navigation.navigate(request, local_gateway_id=self.gateway_id, deadline=deadline)

    async def start(self) -> None:
        subscription = self.file_registry.subscribe_observation(label="Gateway raw Skill publication")

        def unsubscribe() -> None:
            self.file_registry.unsubscribe_observation(subscription)

        self._scope.register(
            unsubscribe,
            label="gateway-skill-observation",
        )
        await self.file_registry.start()
        self._publish()

        async def observe() -> None:
            while True:
                await subscription.next()
                self._publish()

        task = asyncio.create_task(observe(), name="gateway-raw-skill-publication")

        async def stop_publication() -> None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._scope.register(stop_publication, label="gateway-skill-publication")
        self._task = task

    def snapshot(self, *, workspace_id: str) -> GatewaySkillSourcesDTO:
        if self._publication is None:
            raise RuntimeError("Gateway Skill source owner 尚未就绪")
        if self._task is not None and self._task.done():
            self._task.result()
            raise RuntimeError("Gateway Skill source publication 意外退出")
        if self._task is not None:
            self.file_registry.check_running()
        publication = self._publication
        return GatewaySkillSourcesDTO(
            gateway_id=self.gateway_id, workspace_id=workspace_id,
            revision=publication.revision, entries=publication.entries,
            available=publication.available,
            error_code=None if publication.available else "source-unavailable",
        )

    async def close(self) -> None:
        await self._scope.close()

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[GatewaySkillSourceOwner]:
        try:
            await self.start()
            yield self
        finally:
            await self.close()
