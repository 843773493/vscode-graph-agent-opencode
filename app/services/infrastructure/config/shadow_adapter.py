"""workspace 配置域 shadow lifecycle 的生产适配器。

bootstrap/start 与 apply_candidate 统一经 ConfigShadowLifecycleOwner
编排；published/unchanged/failed/closed 通过 EventChannelService 的
config.lifecycle/{domain} typed 轻量事件发布。事件只携带
domain/generation identity，不携带配置正文或宿主机路径。

事件失败不得伪造业务成功：published 事件在 owner 的原子发布回调内
发出，通道不可用时发布步骤本身失败，owner 只回收 candidate 并保留旧
active generation。失败路径先投递 failed 事件再原样抛出原始错误；
failed 事件本身也失败时以 ExceptionGroup 同时暴露两个错误。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from app.services.infrastructure.config.shadow_scope import (
    ConfigShadowApplyResult,
    ConfigShadowGeneration,
    ConfigShadowLifecycleError,
    ConfigShadowLifecycleOwner,
    ShadowReadiness,
    ShadowReconcile,
    ShadowValidator,
)
from app.services.infrastructure.events.channel_events import (
    ConfigLifecycleEvent,
    assert_config_lifecycle_event_is_lightweight,
    config_lifecycle_channel_name,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
    EventChannelSpec,
)

ShadowGenerationPublishHook = Callable[[ConfigShadowGeneration], Awaitable[None]]

# 这些 owner 拒绝没有评估任何 candidate：发布 failed 事件会让订阅者误以为
# 有候选配置被拒绝，因此只向上抛出原始错误，不发事件。
_NON_CANDIDATE_FAILURE_REASONS = frozenset(
    {
        "generation_fence_conflict",
        "readiness_gate_closed",
        "not_ready",
        "already_started",
    }
)


class ConfigShadowLifecycleAdapter:
    """把配置 shadow lifecycle 的生命周期结果发布为 config.lifecycle 事件。

    这是配置域 owner 的 shadow lifecycle 生产入口：validator/reconcile 由
    调用方注入，发布结果统一落到 config.lifecycle/{domain} channel。
    """

    def __init__(
        self,
        *,
        validator: ShadowValidator,
        reconcile: ShadowReconcile,
        event_service: EventChannelService | None = None,
        bootstrap_guard_keys: tuple[str, ...] = (),
        domain: str = "workspace",
    ) -> None:
        self._domain = domain
        self._event_service = event_service or EventChannelService()
        self._channel = self._event_service.ensure_channel(
            EventChannelSpec(
                name=config_lifecycle_channel_name(domain),
                overflow_policy="gap",
                max_queue_size=64,
                history_size=0,
            )
        )
        self._owner = ConfigShadowLifecycleOwner(
            validator=validator,
            reconcile=reconcile,
            publish=self._publish_generation,
            bootstrap_guard_keys=bootstrap_guard_keys,
        )
        self._operation_lock = asyncio.Lock()
        self._publish_hook: ShadowGenerationPublishHook | None = None
        self._closed_event_published = False

    @property
    def channel_name(self) -> str:
        return self._channel.name

    @property
    def readiness(self) -> ShadowReadiness:
        return self._owner.readiness

    @property
    def generation(self) -> int:
        return self._owner.generation

    @property
    def active(self) -> ConfigShadowGeneration:
        return self._owner.active

    async def bootstrap(self, config: Mapping[str, object]) -> ConfigShadowGeneration:
        """冷启动配置 generation；失败发布 failed 事件后原样抛出。"""
        async with self._operation_lock:
            try:
                return await self._owner.start(config)
            except BaseException as error:
                await self._publish_failure_or_group(error)
                raise

    async def apply_candidate(
        self,
        config: Mapping[str, object],
        *,
        expected_generation: int,
        publish_hook: ShadowGenerationPublishHook | None = None,
    ) -> ConfigShadowApplyResult:
        """按 generation fence 在 shadow 中校验并原子发布 candidate。

        unchanged 不递增 generation；校验/reconcile/发布/旧 scope 排空失败
        都先投递 failed 事件再抛出，旧 active generation 保持权威。
        """
        async with self._operation_lock:
            self._publish_hook = publish_hook
            try:
                result = await self._owner.apply_candidate(
                    config,
                    expected_generation=expected_generation,
                )
                if result.status == "unchanged":
                    self._publish_event("unchanged", generation=result.generation)
                    if publish_hook is not None:
                        await publish_hook(self._owner.active)
                return result
            except ConfigShadowLifecycleError as error:
                if error.reason_code in _NON_CANDIDATE_FAILURE_REASONS:
                    raise
                await self._publish_failure_or_group(error)
                raise
            except BaseException as error:
                await self._publish_failure_or_group(error)
                raise
            finally:
                self._publish_hook = None

    async def close(self) -> None:
        """排空当前 generation scope 并发布 closed 事件；重复调用幂等。

        closed 事件只发一次；发布失败时保持未发布状态，下次 close 重试。
        """
        async with self._operation_lock:
            generation = self._current_generation()
            await self._owner.close()
            if self._closed_event_published:
                return
            self._publish_event("closed", generation=generation)
            self._closed_event_published = True

    async def _publish_generation(
        self,
        generation: ConfigShadowGeneration,
    ) -> None:
        """先发布轻量通知，再执行 ConfigService 的权威快照提交回调。

        通道不可用时回调不会执行；后续提交失败时通知也不能充当 durable
        事实，owner 不会切换 active generation。
        """
        self._publish_event("published", generation=generation.generation)
        if self._publish_hook is not None:
            await self._publish_hook(generation)

    async def _publish_failure_or_group(
        self,
        original_error: BaseException,
    ) -> None:
        """发布 failed 事件；事件也失败时与原始错误一起显式抛出。"""
        try:
            self._publish_event("failed", generation=self._current_generation())
        except (RuntimeError, ValueError) as event_error:
            raise BaseExceptionGroup(
                "配置生命周期失败且 failed 事件发布也失败",
                [original_error, event_error],
            ) from original_error

    def _publish_event(self, kind: str, *, generation: int | None) -> None:
        event = ConfigLifecycleEvent(
            domain=self._domain,
            kind=kind,
            generation=str(generation) if generation is not None else None,
        )
        assert_config_lifecycle_event_is_lightweight(event)
        self._channel.publish(event)

    def _current_generation(self) -> int | None:
        try:
            return self._owner.generation
        except ConfigShadowLifecycleError:
            return None


__all__ = ["ConfigShadowLifecycleAdapter"]
