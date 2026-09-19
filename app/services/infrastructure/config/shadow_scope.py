"""配置域 owner 的 shadow generation 生命周期。

candidate 始终先在独立 :class:LifetimeScope 中完成校验、来源 reconcile 和
health 检查；只有全部通过后才会按 generation fence 原子发布。旧 active
scope 在新 generation 发布后排空。任何失败都只回收 candidate，不提供空配置
或旧字段 fallback。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.hashing import sha256_jcs

ShadowReconcile = Callable[
    [Mapping[str, object], LifetimeScope], Awaitable[None]
]
ShadowPublish = Callable[["ConfigShadowGeneration"], Awaitable[None]]
ShadowValidator = Callable[[Mapping[str, object]], None]
ShadowReadiness = Literal["cold", "ready", "failed", "closed"]


class ConfigShadowLifecycleError(RuntimeError):
    """shadow 配置生命周期显式失败；reason_code 供调用方诊断。"""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"[{reason_code}] {message}")
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ConfigShadowGeneration:
    """一次已发布配置和其来源订阅/worker 的所有权记录。"""

    generation: int
    revision: str
    config: Mapping[str, object]
    scope: LifetimeScope


@dataclass(frozen=True, slots=True)
class ConfigShadowApplyResult:
    """candidate 发布结果；unchanged 不递增 generation。"""

    status: Literal["published", "unchanged"]
    generation: int
    revision: str


class ConfigShadowLifecycleOwner:
    """配置域唯一的 shadow validate/reconcile/publish/drain 编排器。"""

    def __init__(
        self,
        *,
        validator: ShadowValidator,
        reconcile: ShadowReconcile,
        publish: ShadowPublish,
        bootstrap_guard_keys: tuple[str, ...] = (),
    ) -> None:
        if not callable(validator):
            raise TypeError("validator 必须可调用")
        if not callable(reconcile):
            raise TypeError("reconcile 必须可调用")
        if not callable(publish):
            raise TypeError("publish 必须可调用")
        if any(not isinstance(key, str) or not key for key in bootstrap_guard_keys):
            raise ValueError("bootstrap_guard_keys 必须是非空字符串元组")
        self._validator = validator
        self._reconcile = reconcile
        self._publish = publish
        self._bootstrap_guard_keys = tuple(dict.fromkeys(bootstrap_guard_keys))
        self._lock = asyncio.Lock()
        self._active: ConfigShadowGeneration | None = None
        self._readiness: ShadowReadiness = "cold"

    @property
    def readiness(self) -> ShadowReadiness:
        """startup/reconnect gap 的 readiness gate。"""

        return self._readiness

    @property
    def generation(self) -> int:
        if self._active is None:
            raise ConfigShadowLifecycleError(
                "not_ready", "配置 generation 尚未发布"
            )
        return self._active.generation

    @property
    def active(self) -> ConfigShadowGeneration:
        if self._active is None or self._readiness != "ready":
            raise ConfigShadowLifecycleError(
                "readiness_gate_closed",
                "有效配置 generation 尚未就绪，不能提供 active 配置",
            )
        return self._active

    async def start(self, config: Mapping[str, object]) -> ConfigShadowGeneration:
        """bootstrap 冷启动；没有 valid 配置时直接失败，不进入 degraded。"""

        async with self._lock:
            if self._readiness != "cold":
                raise ConfigShadowLifecycleError(
                    "already_started", "配置 shadow lifecycle 已启动"
                )
            scope: LifetimeScope | None = None
            try:
                self._validator(config)
                self._validate_bootstrap(config)
                scope = LifetimeScope("config-generation-bootstrap")
                await self._reconcile(config, scope)
                active = ConfigShadowGeneration(
                    generation=1,
                    revision=sha256_jcs(dict(config)),
                    config=dict(config),
                    scope=scope,
                )
                await self._publish(active)
            except BaseException:
                self._readiness = "failed"
                await self._close_scope_quietly_if_owned(scope)
                raise
            self._active = active
            self._readiness = "ready"
            return active

    async def apply_candidate(
        self,
        config: Mapping[str, object],
        *,
        expected_generation: int,
    ) -> ConfigShadowApplyResult:
        """在 shadow 中验证 candidate，再按 expected generation fence 发布。"""

        async with self._lock:
            active = self.active
            if expected_generation != active.generation:
                raise ConfigShadowLifecycleError(
                    "generation_fence_conflict",
                    "candidate 基线过期: "
                    f"expected={expected_generation} active={active.generation}",
                )
            try:
                self._validator(config)
            except Exception as error:
                raise ConfigShadowLifecycleError(
                    "candidate_invalid",
                    f"配置 candidate 校验失败: {error}",
                ) from error
            self._validate_bootstrap(config)
            revision = sha256_jcs(dict(config))
            if revision == active.revision:
                return ConfigShadowApplyResult(
                    status="unchanged",
                    generation=active.generation,
                    revision=revision,
                )
            next_generation = active.generation + 1
            candidate_scope = LifetimeScope(
                f"config-generation-candidate-{next_generation}"
            )
            try:
                await self._reconcile(config, candidate_scope)
                candidate = ConfigShadowGeneration(
                    generation=next_generation,
                    revision=revision,
                    config=dict(config),
                    scope=candidate_scope,
                )
                await self._publish(candidate)
            except BaseException as error:  # noqa: BLE001
                await self._close_candidate_with_error(candidate_scope, error)
            old_generation = active
            self._active = candidate
            # 原子发布完成后再排空旧 scope；release 失败必须显式暴露。
            await old_generation.scope.close()
            return ConfigShadowApplyResult(
                status="published",
                generation=next_generation,
                revision=revision,
            )

    async def close(self) -> None:
        """排空当前 active scope；关闭后不再接受 candidate。"""

        async with self._lock:
            active = self._active
            if self._readiness == "closed":
                return
            self._readiness = "closed"
            if active is not None:
                await active.scope.close()
                self._active = None

    def _validate_bootstrap(self, config: Mapping[str, object]) -> None:
        if not config:
            raise ConfigShadowLifecycleError(
                "empty_bootstrap", "bootstrap 配置不能为空"
            )
        missing = [key for key in self._bootstrap_guard_keys if key not in config]
        if missing:
            raise ConfigShadowLifecycleError(
                "bootstrap_removed",
                f"candidate 试图移除固定 bootstrap 配置键: {missing}",
            )

    @staticmethod
    async def _close_scope_quietly_if_owned(
        scope: LifetimeScope | None,
    ) -> None:
        """启动失败时回收 scope；原始错误仍作为主异常抛出。"""

        if scope is None:
            return
        try:
            await scope.close()
        except Exception as close_error:
            raise RuntimeError("bootstrap scope 回收失败") from close_error

    @staticmethod
    async def _close_candidate_with_error(
        scope: LifetimeScope, original_error: BaseException
    ) -> None:
        """candidate 失败只回收 candidate，不覆盖 active，也不吞掉 close 错误。"""

        try:
            await scope.close()
        except Exception as close_error:  # noqa: BLE001
            raise ExceptionGroup(
                "配置 candidate 失败且 scope 回收失败", [original_error, close_error]
            ) from original_error
        raise original_error
