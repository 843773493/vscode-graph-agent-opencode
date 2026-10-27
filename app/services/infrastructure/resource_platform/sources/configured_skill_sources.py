"""配置来源的唯一接受 owner；按代码登记的获取和通知端口组合，不解释指令。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Mapping
from contextvars import Context
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from app.abstractions.resource_file_content import ResourceFileContent
from app.core.lifecycle import LifetimeScope
from app.core.resource_uri import parse_resource_uri
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.gateway.skill_sources import GatewaySkillSourceEntryDTO
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.sources.observed_source import (
    MAX_STABLE_READ_BYTES,
)
from app.services.infrastructure.resource_platform.sources.skill_fetch_contracts import (
    SkillChangeSubscription,
    SkillFetchError,
    SkillFetchRuntime,
)
from app.services.infrastructure.resource_platform.sources.skill_source_config import (
    SkillSourceConfig,
    skill_fetch_capability,
)
from app.services.infrastructure.resource_platform.sources.skill_source_identity import (
    configured_skill_identity,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ConfiguredSourceState:
    config: SkillSourceConfig
    entry: GatewaySkillSourceEntryDTO
    validators: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sequence: int = 0


@dataclass(slots=True, eq=False)
class ConfiguredSkillGeneration:
    sources: dict[str, _ConfiguredSourceState]
    scope: LifetimeScope = field(default_factory=lambda: LifetimeScope("configured-skill-sources"))
    activated: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    inflight: set[asyncio.Task[None]] = field(default_factory=set)


class ConfiguredSkillSources:
    """每个实际配置 owner 持有唯一接受链路；候选代次只暂存事务所需的值。"""

    def __init__(self, *, scope: Literal["gateway", "workspace"], scope_id: str, channel: ResourceObservationChannel) -> None:
        self._scope: Literal["gateway", "workspace"] = scope
        self.owner_id, self._channel = scope_id, channel
        self.directory_id = "skill-directory:" + hashlib.sha256(f"skill-http-sources:{scope}:{scope_id}".encode()).hexdigest()
        self._runtimes: dict[str, SkillFetchRuntime] = {}
        self._generations: set[ConfiguredSkillGeneration] = set()
        self.current: ConfiguredSkillGeneration | None = None

    def _empty_entry(self, config: SkillSourceConfig) -> GatewaySkillSourceEntryDTO:
        source_id, identity, uri = configured_skill_identity(self._scope, self.owner_id, config.id)
        return GatewaySkillSourceEntryDTO(
            name=config.name, source_id=source_id, entry_identity=identity, display_uri=uri,
            revision=None, byte_length=0, content="", available=False, error_code="source-unavailable",
            layer="network", priority=config.priority,
        )

    async def prepare(self, configs: tuple[SkillSourceConfig, ...]) -> ConfiguredSkillGeneration:
        states = {}
        for config in configs:
            previous = self.current.sources.get(config.id) if self.current is not None else None
            reusable = previous if previous is not None and previous.config.fetch == config.fetch else None
            states[config.id] = _ConfiguredSourceState(
                config, reusable.entry.model_copy(update={"name": config.name, "priority": config.priority}) if reusable is not None else self._empty_entry(config),
                reusable.validators if reusable is not None else MappingProxyType({}),
            )
        generation = ConfiguredSkillGeneration(states)
        self._generations.add(generation)
        try:
            subscriptions: dict[str, SkillChangeSubscription] = {}
            for key, state in states.items():
                capability = skill_fetch_capability(state.config.fetch.protocol)
                state.config.updates.require_event_capability(capability.subscribe is not None)
                runtime = self._runtime(state.config.fetch.protocol)
                if state.config.updates.uses_events:
                    assert capability.subscribe is not None
                    subscription = await capability.subscribe(runtime, state.config.fetch)
                    subscriptions[key] = subscription
                    generation.scope.register(subscription.close, label=f"skill-source-events:{key}")
            # 先订阅再取得初始快照；激活前到达的变化保持在原订阅中。
            await self._reconcile(generation, strict=True)
            for key, state in states.items():
                interval = state.config.updates.poll_seconds
                if not state.config.updates.automatic:
                    continue
                task = asyncio.create_task(self._observe(generation, key, interval, subscriptions.get(key)),
                    name=f"configured-skill-source:{self._scope}:{self.owner_id}:{key}", context=Context())
                generation.tasks[key] = task
                task.add_done_callback(lambda finished, source_id=key: self._observer_finished(generation, source_id, finished))

                async def stop(current: asyncio.Task[None] = task) -> None:
                    current.cancel()
                    try:
                        await current
                    except asyncio.CancelledError:
                        pass

                generation.scope.register(stop, label=f"skill-source-observer:{key}")
        except BaseException:
            await self.close_generation(generation)
            raise
        return generation

    def _runtime(self, protocol: str) -> SkillFetchRuntime:
        if protocol not in self._runtimes:
            self._runtimes[protocol] = skill_fetch_capability(protocol).create()
        return self._runtimes[protocol]

    def _observer_finished(self, generation: ConfiguredSkillGeneration, key: str, task: asyncio.Task[None]) -> None:
        error = None if task.cancelled() else task.exception()
        if generation.scope.state != "open":
            return
        logger.error("Skill 来源观察任务停止: scope=%s scope_id=%s source_id=%s", self._scope, self.owner_id, key,
                     exc_info=error if error is not None else RuntimeError("Skill 来源观察任务意外退出"))
        if generation is self.current:
            self._notify()

    async def _observe(
        self, generation: ConfiguredSkillGeneration, key: str, interval: int | None,
        subscription: SkillChangeSubscription | None,
    ) -> None:
        event: asyncio.Task[None] | None = None
        try:
            while True:
                await generation.activated.wait()
                if subscription is None:
                    assert interval is not None
                    await asyncio.sleep(interval)
                else:
                    if event is None:
                        event = asyncio.create_task(subscription.wait())
                    done, _ = await asyncio.wait((event,), timeout=interval)
                    if event in done:
                        event.result()
                        event = None
                # 结算前退役代次只休眠，保留已收到的触发；回滚后继续同一对账。
                while True:
                    await generation.activated.wait()
                    await self._reconcile(generation, source_ids=(key,))
                    if generation is self.current:
                        break
        finally:
            if event is not None:
                event.cancel()
                await asyncio.gather(event, return_exceptions=True)

    def activate(self, generation: ConfiguredSkillGeneration) -> None:
        if generation not in self._generations:
            raise RuntimeError("配置 Skill 候选不属于当前 owner")
        if self.current is not None:
            self.current.activated.clear()
        self.current = generation
        generation.activated.set()
        self._notify()

    def entries(self) -> tuple[GatewaySkillSourceEntryDTO, ...]:
        if self.current is None:
            raise RuntimeError("配置 Skill source owner 尚未就绪")
        return tuple(state.entry.model_copy(update={"available": False, "error_code": "source-unavailable"})
                     if key in self.current.tasks and self.current.tasks[key].done() else state.entry
                     for key, state in self.current.sources.items())

    @property
    def publication_revision(self) -> str:
        return sha256_jcs({"owner": self.directory_id, "entries": [entry.model_dump(exclude={"content"}) for entry in self.entries()]})

    async def prepare_config(self, configs: tuple[SkillSourceConfig, ...], scope: LifetimeScope) -> Callable[[], None]:
        """配置 shadow 持有所选代次；无关重载复用当前接受事实，不触发 manual 获取。"""
        previous = self.current
        unchanged = previous is not None and tuple(state.config for state in previous.sources.values()) == configs
        generation = previous if unchanged else await self.prepare(configs)
        assert generation is not None

        async def release() -> None:
            # 每次 shadow 都持有当前选择：失败释放候选，提交后释放退役代次。
            # 最终仍在使用的代次由进程 owner.close 释放。
            if self.current is not generation:
                await self.close_generation(generation)

        scope.register(release, label="workspace-configured-skill-generation")

        def activate() -> None:
            if self.current is not generation:
                self.activate(generation)

        return activate

    def read(self, uri: str, _max_bytes: int) -> ResourceFileContent:
        parsed = parse_resource_uri(uri)
        if parsed.gateway_authority is not None or parsed.scope != self._scope or parsed.scope_id != self.owner_id:
            raise PermissionError("配置 Skill 来源 owner 不符")
        entry = next((entry for entry in self.entries() if entry.display_uri == parsed.display_uri), None)
        if entry is None or not entry.available:
            raise FileNotFoundError("配置 Skill 原始快照不存在或不可用")
        return ResourceFileContent(entry.content.encode("utf-8"), "text/markdown")

    async def refresh(self) -> None:
        if self.current is None:
            raise RuntimeError("配置 Skill source owner 尚未就绪")
        await self._reconcile(self.current)

    async def _fetch(self, state: _ConfiguredSourceState) -> tuple[GatewaySkillSourceEntryDTO, Mapping[str, str]]:
        result = await self._runtime(state.config.fetch.protocol).fetch(state.config.fetch,
            validators=state.validators, has_snapshot=state.entry.revision is not None, max_bytes=MAX_STABLE_READ_BYTES)
        update: dict[str, object] = {"available": True, "error_code": None}
        if result.body is None:
            if state.entry.revision is None:
                raise ValueError("Skill 获取返回未变化，但没有 accepted 正文")
        else:
            if len(result.body) > MAX_STABLE_READ_BYTES:
                raise ValueError("Skill 正文超过来源字节上限")
            update.update(content=result.body.decode("utf-8", errors="strict"), byte_length=len(result.body),
                          revision="sha256:" + hashlib.sha256(result.body).hexdigest())
        return state.entry.model_copy(update=update), MappingProxyType(dict(result.validators))

    async def _reconcile(self, generation: ConfiguredSkillGeneration, *, source_ids: tuple[str, ...] | None = None, strict: bool = False) -> None:
        async def update(key: str) -> None:
            state = generation.sources[key]
            sequence = state.sequence
            async with state.lock:
                if sequence != state.sequence:
                    return
                try:
                    entry, validators = await self._fetch(state)
                except (SkillFetchError, OSError, ValueError, TimeoutError) as error:
                    logger.error("配置 Skill 来源获取失败: scope=%s scope_id=%s source_id=%s error_type=%s", self._scope, self.owner_id, key, type(error).__name__, exc_info=error)
                    if strict:
                        raise RuntimeError(f"配置 Skill source unavailable: source_id={key}, error={type(error).__name__}") from error
                    entry, validators = state.entry.model_copy(update={"available": False, "error_code": "source-unavailable"}), state.validators
                if not strict and generation is not self.current:
                    return
                changed = state.entry != entry
                state.entry, state.validators = entry, validators
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
            raise BaseExceptionGroup("配置 Skill 对账失败", errors)

    def _notify(self) -> None:
        self._channel.notify(ResourceObservation(uri=None, directory_id=self.directory_id, revision=None, available=all(entry.available for entry in self.entries())))

    async def close_generation(self, generation: ConfiguredSkillGeneration) -> None:
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
        errors: list[BaseException] = []
        try:
            results = await asyncio.gather(*(self.close_generation(item) for item in tuple(self._generations)), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
        except BaseException as error:  # noqa: BLE001 - 取消也须结算并释放实际获取端口。
            # 调用者取消也不能跳过实际获取端口的释放。
            errors.append(error)
        finally:
            results = await asyncio.gather(*(runtime.close() for runtime in self._runtimes.values()), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
            if errors:
                raise BaseExceptionGroup("Skill 来源与获取能力关闭失败", errors)
