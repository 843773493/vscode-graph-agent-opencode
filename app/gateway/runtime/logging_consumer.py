"""在同一次配置事务内冻结、确认并补偿真实产品进程的日志设置。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import httpx

from app.gateway.auth import get_gateway_local_token
from app.gateway.config.values import GatewayConfig
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerStage,
    GatewayRuntimeHealthProof,
)
from app.gateway.runtime.logging_control import (
    RuntimeLoggingProcess,
    RuntimeLoggingProcessControl,
    runtime_logging_processes,
)
from app.schemas.runtime_logging import (
    RuntimeLoggingApply,
    RuntimeLoggingStatus,
)

ProgressRecorder = Callable[[dict[str, object]], None]


def logging_progress_recorder(store: GatewayStateStore, fencing_token: str | None) -> ProgressRecorder:
    claim = store.get_config_apply_claim(config_domain="gateway")
    if claim is None or claim.fencing_token != fencing_token:
        raise RuntimeError("日志热应用缺少当前配置 claim")

    def record(detail: dict[str, object]) -> None:
        store.assert_config_apply_claim(config_domain="gateway", apply_id=claim.apply_id, fencing_token=claim.fencing_token)
        journal = store.get_config_apply_journal(apply_id=claim.apply_id)
        if journal is None or journal.state not in {"applying", "recovery_required"}:
            raise RuntimeError("日志进程应用账本已经结算")
        store.append_config_apply_side_effect(apply_id=claim.apply_id, side_effect=detail, expected_state=journal.state)

    return record


@dataclass(slots=True)
class LoggingTarget:
    process: RuntimeLoggingProcess
    before: RuntimeLoggingStatus | None = None
    current: RuntimeLoggingStatus | None = None
    touched: bool = False
    phase: str = "unprepared"
    error: str | None = None

    def detail(self) -> dict[str, object]:
        def public_state(state: RuntimeLoggingStatus | None) -> dict[str, object] | None:
            if state is None:
                return None
            # 配置账本不是原始诊断日志入口；文件身份足够核验，路径只留在原 owner。
            return state.model_dump(mode="json", exclude={"log_sink": {"path"}})

        return {
            "before": public_state(self.before), "current": public_state(self.current),
            "phase": self.phase, "error": self.error,
        }


class RuntimeLoggingConsumer:
    def __init__(
        self, *, registry: GatewayWorkspaceRegistry, candidate: GatewayConfig,
        generation: str, fencing_token_digest: str | None,
        fence_check: Callable[[], None] | None, record: ProgressRecorder,
    ) -> None:
        self._registry = registry
        self._candidate = candidate
        self._generation = generation
        self._fencing_token_digest = fencing_token_digest
        self._fence_check = fence_check
        self._record = record
        self._targets: list[LoggingTarget] = []
        self._locked = False
        self._client: httpx.AsyncClient | None = None

    def stage(self) -> GatewayRuntimeConsumerStage:
        return GatewayRuntimeConsumerStage(
            consumer_id="runtime-logging", generation=self._generation,
            prepare=self.prepare, apply=self.apply, health=self.health,
            promote=lambda: None, rollback=self.rollback, finalize=self.finalize,
            fencing_token_digest=self._fencing_token_digest, fence_check=self._fence_check,
            diagnostics=self.details,
        )

    def details(self) -> dict[str, object]:
        return {"processes": [target.detail() for target in self._targets]}

    def _progress(self, target: LoggingTarget, phase: str) -> None:
        target.phase = phase
        self._record({"resource": "runtime-logging", "action": phase, **target.detail()})

    async def prepare(self) -> None:
        # 一直持有到 SQLite promotion 或补偿结算后的 finalize；不能只锁枚举瞬间。
        await self._registry.backend_topology_lock.acquire()
        self._locked = True
        self._client = httpx.AsyncClient(trust_env=False)
        control = RuntimeLoggingProcessControl(client=self._client, token=get_gateway_local_token())

        self._targets = [LoggingTarget(process) for process in runtime_logging_processes(self._registry, control)]

        identities: set[tuple[str, int, str]] = set()
        prepared: list[LoggingTarget] = []
        for target in self._targets:
            state = await self._read(target)
            identity = (state.service, state.pid, state.runtime_instance_id)
            if identity in identities:
                continue
            identities.add(identity)
            target.before = target.current = state
            prepared.append(target)
            self._progress(target, "prepared")
        self._targets = prepared

    async def _read(self, target: LoggingTarget) -> RuntimeLoggingStatus:
        state = await target.process.read()
        if target.before is not None and (
            (state.service, state.pid, state.runtime_instance_id, state.log_sink)
            != (target.before.service, target.before.pid, target.before.runtime_instance_id, target.before.log_sink)
        ):
            raise RuntimeError("日志目标实例或输出去向已变化")
        return state

    async def apply(self) -> None:
        settings = cast(dict[str, object], cast(dict[str, object], self._candidate.payload["logging"])["runtime"])
        for target in self._targets:
            if self._fence_check is not None:
                self._fence_check()
            before = target.before
            if before is None:
                raise RuntimeError("日志目标尚未完成 prepare")
            request = RuntimeLoggingApply(
                runtime_instance_id=before.runtime_instance_id, expected_revision=before.applied_revision,
                revision=self._candidate.revision, level=cast(str, settings["level"]), pretty=cast(bool, settings["pretty"]),
            )
            # 请求发出前先记账；超时不能解释为没有发生副作用。
            self._progress(target, "applying")
            target.touched = True
            try:
                await target.process.apply(request)
                observed = await self._read(target)
                RuntimeLoggingProcessControl._confirmed_apply(observed, request)
                target.current = observed
            except Exception as error:
                target.current = None
                target.error = type(error).__name__
                self._progress(target, "outcome_unknown")
                raise
            self._progress(target, "applied")

    def health(self) -> GatewayRuntimeHealthProof:
        if any(target.phase != "applied" or not target.process.alive() for target in self._targets):
            raise RuntimeError("日志消费者未取得全部进程确认")
        return GatewayRuntimeHealthProof(
            consumer_id="runtime-logging", generation=self._generation, state="healthy",
            details=self.details(), fencing_token_digest=self._fencing_token_digest,
        )

    async def rollback(self) -> None:
        errors: list[Exception] = []
        for target in reversed(self._targets):
            if not target.touched:
                continue
            try:
                await self._restore(target)
            except Exception as error:  # noqa: BLE001 — 每个已触及进程都必须独立尝试补偿。
                target.error = type(error).__name__
                errors.append(error)
                try:
                    self._progress(target, "rollback_failed")
                except Exception as record_error:  # noqa: BLE001 — 账本故障也不能中断其余进程的补偿。
                    errors.append(record_error)
        if errors:
            raise ExceptionGroup("产品日志进程补偿失败", errors)

    async def _restore(self, target: LoggingTarget) -> None:
        before = target.before
        if before is None:
            raise RuntimeError("日志补偿缺少冻结状态")
        target.current = None
        observed = await self._read(target)
        target.current = observed
        if observed == before:
            self._progress(target, "restored")
            return
        if observed.applied_revision != self._candidate.revision:
            raise RuntimeError("日志状态已被其它候选修改，拒绝补偿覆盖")
        if before.applied_revision is None:
            raise RuntimeError("日志目标仍在 bootstrap，无法恢复未登记的有效 revision")
        self._progress(target, "restoring")
        request = RuntimeLoggingApply(
            runtime_instance_id=before.runtime_instance_id, expected_revision=observed.applied_revision,
            revision=before.applied_revision, level=before.level, pretty=before.pretty,
        )
        target.current = None
        await target.process.apply(request)
        target.current = await self._read(target)
        if target.current != before:
            raise RuntimeError("日志补偿后的实际状态与冻结状态不一致")
        self._progress(target, "restored")

    async def finalize(self) -> None:
        try:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
        finally:
            if self._locked:
                self._registry.backend_topology_lock.release()
                self._locked = False
