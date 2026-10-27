"""配置所属 owner 的 HTTP Skill 获取；仅发布原始接受事实，不解释或执行指令。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextvars import Context
from dataclasses import dataclass, field
from typing import Literal

import httpx

from app.abstractions.resource_file_content import ResourceFileContent
from app.core.lifecycle import LifetimeScope
from app.core.resource_uri import parse_resource_uri
from app.schemas.gateway.skill_sources import GatewaySkillSourceEntryDTO
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.sources.observed_source import (
    MAX_STABLE_READ_BYTES,
)
from app.services.infrastructure.resource_platform.sources.skill_source_config import (
    SkillSourceConfig,
)
from app.services.infrastructure.resource_platform.sources.skill_source_identity import (
    http_skill_identity,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _HttpSourceState:
    config: SkillSourceConfig
    entry: GatewaySkillSourceEntryDTO
    etag: str | None = None
    last_modified: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sequence: int = 0


@dataclass(slots=True, eq=False)
class HttpSkillGeneration:
    sources: dict[str, _HttpSourceState]
    scope: LifetimeScope = field(default_factory=lambda: LifetimeScope("http-skill-sources"))
    activated: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    inflight: set[asyncio.Task[None]] = field(default_factory=set)


class HttpSkillSources:
    """每个实际配置 owner 持有唯一接受链路；候选代次只暂存事务所需的值。"""

    def __init__(self, *, scope: Literal["gateway", "workspace"], scope_id: str, channel: ResourceObservationChannel) -> None:
        self._scope: Literal["gateway", "workspace"] = scope
        self._scope_id, self._channel = scope_id, channel
        self.directory_id = "skill-directory:" + hashlib.sha256(f"skill-http-sources:{scope}:{scope_id}".encode()).hexdigest()
        self._client = httpx.AsyncClient(trust_env=False, follow_redirects=True)
        self._generations: set[HttpSkillGeneration] = set()
        self.current: HttpSkillGeneration | None = None

    def _empty_entry(self, config: SkillSourceConfig) -> GatewaySkillSourceEntryDTO:
        source_id, identity, uri = http_skill_identity(self._scope, self._scope_id, config.id)
        return GatewaySkillSourceEntryDTO(
            name=config.name, source_id=source_id, entry_identity=identity, display_uri=uri,
            revision=None, byte_length=0, content="", available=False, error_code="source-unavailable",
            layer="network", priority=config.priority,
        )

    async def prepare(self, configs: tuple[SkillSourceConfig, ...]) -> HttpSkillGeneration:
        states = {}
        for config in configs:
            previous = self.current.sources.get(config.id) if self.current is not None else None
            reusable = previous if previous is not None and previous.config.fetch == config.fetch else None
            states[config.id] = _HttpSourceState(
                config, reusable.entry.model_copy(update={"name": config.name, "priority": config.priority}) if reusable is not None else self._empty_entry(config),
                reusable.etag if reusable is not None else None, reusable.last_modified if reusable is not None else None,
            )
        generation = HttpSkillGeneration(states)
        self._generations.add(generation)
        try:
            await self._reconcile(generation, strict=True)
            for key, state in states.items():
                interval = state.config.updates.poll_seconds
                if interval is None:
                    continue

                async def poll(source_id: str = key, seconds: int = interval) -> None:
                    while True:
                        await generation.activated.wait()
                        await asyncio.sleep(seconds)
                        if generation is self.current:
                            await self._reconcile(generation, source_ids=(source_id,))

                task = asyncio.create_task(poll(), name=f"http-skill-source:{self._scope}:{self._scope_id}:{key}", context=Context())
                generation.tasks.append(task)

                async def stop(current: asyncio.Task[None] = task) -> None:
                    current.cancel()
                    try:
                        await current
                    except asyncio.CancelledError:
                        pass

                generation.scope.register(stop, label=f"http-skill-source:{key}")
        except BaseException:
            await self.close_generation(generation)
            raise
        return generation

    def activate(self, generation: HttpSkillGeneration) -> None:
        if generation not in self._generations:
            raise RuntimeError("HTTP Skill 候选不属于当前 owner")
        if self.current is not None:
            self.current.activated.clear()
        self.current = generation
        generation.activated.set()
        self._notify()

    def entries(self) -> tuple[GatewaySkillSourceEntryDTO, ...]:
        if self.current is None:
            raise RuntimeError("HTTP Skill source owner 尚未就绪")
        for task in self.current.tasks:
            if task.done():
                task.result()
                raise RuntimeError("HTTP Skill 观察任务意外退出")
        return tuple(state.entry for state in self.current.sources.values())

    def read(self, uri: str, _max_bytes: int) -> ResourceFileContent:
        parsed = parse_resource_uri(uri)
        if parsed.gateway_authority is not None or parsed.scope != self._scope or parsed.scope_id != self._scope_id:
            raise PermissionError("HTTP Skill 来源 owner 不符")
        entry = next((entry for entry in self.entries() if entry.display_uri == parsed.display_uri), None)
        if entry is None or not entry.available:
            raise FileNotFoundError("HTTP Skill 原始快照不存在或不可用")
        return ResourceFileContent(entry.content.encode("utf-8"), "text/markdown")

    async def refresh(self) -> None:
        if self.current is None:
            raise RuntimeError("HTTP Skill source owner 尚未就绪")
        await self._reconcile(self.current)

    async def _fetch(self, state: _HttpSourceState) -> tuple[GatewaySkillSourceEntryDTO, str | None, str | None]:
        headers = {}
        if state.etag is not None:
            headers["If-None-Match"] = state.etag
        if state.last_modified is not None:
            headers["If-Modified-Since"] = state.last_modified
        async with asyncio.timeout(state.config.fetch.timeout_seconds):
            async with self._client.stream("GET", state.config.fetch.url, headers=headers,
                                           timeout=state.config.fetch.timeout_seconds) as response:
                if response.status_code == 304:
                    if state.entry.revision is None or not headers:
                        raise ValueError("HTTP Skill 304 缺少已接受正文或条件请求")
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            raise ValueError("HTTP Skill 304 不得包含正文")
                    return state.entry.model_copy(update={"available": True, "error_code": None}), response.headers.get("ETag", state.etag), response.headers.get("Last-Modified", state.last_modified)
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("HTTP Skill 获取只接受完整 200 或条件 304")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_STABLE_READ_BYTES:
                        raise ValueError("HTTP Skill 正文超过来源字节上限")
                    body.extend(chunk)
                content = body.decode("utf-8", errors="strict")
                entry = state.entry.model_copy(update={
                    "content": content, "byte_length": len(body),
                    "revision": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "available": True, "error_code": None,
                })
                return entry, response.headers.get("ETag"), response.headers.get("Last-Modified")

    async def _reconcile(self, generation: HttpSkillGeneration, *, source_ids: tuple[str, ...] | None = None, strict: bool = False) -> None:
        async def update(key: str) -> None:
            state = generation.sources[key]
            sequence = state.sequence
            async with state.lock:
                if sequence != state.sequence:
                    return
                try:
                    entry, etag, modified = await self._fetch(state)
                except (httpx.HTTPError, OSError, ValueError, TimeoutError) as error:
                    logger.error("HTTP Skill 来源获取失败: scope=%s scope_id=%s source_id=%s error_type=%s", self._scope, self._scope_id, key, type(error).__name__, exc_info=error)
                    if strict:
                        raise RuntimeError(f"HTTP Skill source unavailable: source_id={key}, error={type(error).__name__}") from error
                    entry, etag, modified = state.entry.model_copy(update={"available": False, "error_code": "source-unavailable"}), state.etag, state.last_modified
                if not strict and generation is not self.current:
                    return
                changed = state.entry != entry
                state.entry, state.etag, state.last_modified = entry, etag, modified
                state.sequence += 1
                if changed and not strict:
                    self._notify()

        # 不让一个失败来源取消兄弟来源，也不遗留已失去候选 scope 的在途读取。
        tasks = {asyncio.create_task(update(key)) for key in (source_ids if source_ids is not None else tuple(generation.sources))}
        generation.inflight.update(tasks)
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            generation.inflight.difference_update(tasks)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("HTTP Skill 对账失败", errors)

    def _notify(self) -> None:
        self._channel.notify(ResourceObservation(uri=None, directory_id=self.directory_id, revision=None, available=all(entry.available for entry in self.entries())))

    async def close_generation(self, generation: HttpSkillGeneration) -> None:
        try:
            await generation.scope.close()
        finally:
            requests = tuple(generation.inflight)
            for task in requests:
                task.cancel()
            if requests:
                await asyncio.gather(*requests, return_exceptions=True)
            self._generations.discard(generation)
            if self.current is generation:
                self.current = None

    async def close(self) -> None:
        try:
            results = await asyncio.gather(*(self.close_generation(item) for item in tuple(self._generations)), return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise BaseExceptionGroup("HTTP Skill owner 关闭失败", errors)
        finally:
            await self._client.aclose()
