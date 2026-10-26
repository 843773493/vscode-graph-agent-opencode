"""在同一次配置事务内冻结、确认并补偿真实产品进程的日志设置。"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import cast

import httpx

from app.core.logging_config import apply_runtime_logging, runtime_logging_state
from app.gateway.auth import get_gateway_local_token
from app.gateway.config.values import GatewayConfig
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerStage,
    GatewayRuntimeHealthProof,
)
from app.gateway.runtime.logging_control import RuntimeLoggingProcessControl
from app.gateway.runtime.process_lifecycle import ManagedProcessHandle
from app.schemas.runtime_logging import (
    RuntimeLoggingApply,
    RuntimeLoggingHttpService,
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
    status: Callable[[], Awaitable[RuntimeLoggingStatus]]
    apply: Callable[[RuntimeLoggingApply], Awaitable[RuntimeLoggingStatus]]
    alive: Callable[[], bool]
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

        async def gateway_status() -> RuntimeLoggingStatus:
            return RuntimeLoggingProcessControl._validated_status(asdict(runtime_logging_state()), "gateway", os.getpid())

        async def gateway_apply(request: RuntimeLoggingApply) -> RuntimeLoggingStatus:
            return RuntimeLoggingStatus.model_validate(asdict(apply_runtime_logging(**request.model_dump())))

        self._targets.append(LoggingTarget(gateway_status, gateway_apply, lambda: True))
        launcher = os.environ.get("BOXTEAM_LAUNCHER_CONTROL")
        if launcher is not None:
            endpoint = json.loads(launcher)
            if (not isinstance(endpoint, dict) or set(endpoint) != {"address", "pid"}
                or not isinstance(endpoint["address"], str) or type(endpoint["pid"]) is not int
                or endpoint["pid"] <= 0):
                raise ValueError("Launcher 交付的日志控制端点非法")
            address, pid = endpoint["address"], endpoint["pid"]
            self._targets.append(LoggingTarget(
                lambda: control.launcher_status(address=address, pid=pid),
                lambda request: control.launcher_apply(address=address, pid=pid, request=request),
                lambda: True,
            ))

        for owner in self._registry.backend_owners():
            if owner.process is not None and owner.process.is_alive():
                self._add_http_target(control, owner.process, "backend", owner.contract.backend_url)
        for target in self._registry.targets():
            if target.connection_kind != "local" or not target.managed or not self._registry.has_runtime(target.workspace_id):
                continue
            runtime = self._registry.managed_runtime(target.workspace_id)
            roles: dict[str, RuntimeLoggingHttpService] = {"terminal_manager": "terminal", "browser_manager": "browser"}
            for name, role in roles.items():
                handle = runtime.processes.get(name)
                if handle is not None and handle.is_alive():
                    self._add_http_target(control, handle, role, self._registry.runtime_service_urls(target.workspace_id)[name])

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

    def _add_http_target(
        self, control: RuntimeLoggingProcessControl, handle: ManagedProcessHandle,
        service: RuntimeLoggingHttpService, url: str,
    ) -> None:
        self._targets.append(LoggingTarget(
            lambda: control.status(base_url=url, service=service, pid=handle.pid),
            lambda request: control.apply(base_url=url, service=service, pid=handle.pid, request=request),
            handle.is_alive,
        ))

    async def _read(self, target: LoggingTarget) -> RuntimeLoggingStatus:
        if not target.alive():
            raise RuntimeError("日志目标进程已退出")
        state = await target.status()
        if not target.alive():
            raise RuntimeError("日志目标进程在确认期间退出")
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
                await target.apply(request)
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
        if any(target.phase != "applied" or not target.alive() for target in self._targets):
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
        await target.apply(request)
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
