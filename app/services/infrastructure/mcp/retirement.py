"""原 MCP Owner 的退役连接排空，不保存第二份持久租约。"""

from __future__ import annotations

import asyncio
from _thread import RLock
from dataclasses import dataclass, replace
from time import monotonic
from typing import Literal

from app.core.lifecycle import LifetimeScope
from app.schemas.internal_v2.config import McpCleanupDTO
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.session_runtime import McpServerRuntime


@dataclass(frozen=True, slots=True)
class _RetiredConnection:
    runtime: McpServerRuntime
    deadline: float
    admits_sealed: bool
    phase: Literal["draining", "closing", "cleanup_failed"] = "draining"
    error_type: str | None = None


class McpConnectionRetirement:
    """只保留原 runtime，持有事实每次读取唯一 ExternalResourceLeaseLedger。"""

    def __init__(self, *, leases: ExternalResourceLeaseLedger, lock: RLock, timeout_seconds: int,
                 publisher: ResourceStateEventPublisher) -> None:
        self._leases = leases
        self._lock = lock
        self._timeout_seconds = timeout_seconds
        self._retired: dict[str, _RetiredConnection] = {}
        self._publisher = publisher

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    def retire(self, connection_ref: str, runtime: McpServerRuntime, *,
               admits_sealed: bool, owner_scope: LifetimeScope) -> None:
        retired = _RetiredConnection(runtime, monotonic() + self._timeout_seconds, admits_sealed)
        self._retired[connection_ref] = retired
        owner_scope.start_task(lambda: self._drain(connection_ref, retired), label=f"drain:{connection_ref}")

    def retained(self, connection_ref: str | None) -> McpServerRuntime | None:
        with self._lock:
            retired = self._retired.get(connection_ref) if connection_ref is not None else None
            if retired is None or not retired.admits_sealed or monotonic() >= retired.deadline:
                return None
            return retired.runtime

    def revoke_server(self, server_id: str) -> None:
        """后续禁用／移除也撤销更早退役代际的未来准入。"""
        for connection_ref, retired in tuple(self._retired.items()):
            if retired.runtime.server.server_id == server_id:
                self._retired[connection_ref] = _RetiredConnection(retired.runtime, retired.deadline, False)

    def clear_closed(self) -> None:
        with self._lock:
            if any(not retired.runtime.scope.is_closed for retired in self._retired.values()):
                raise RuntimeError("MCP 退役 scope 尚未核实关闭")
            self._retired.clear()

    def diagnostics(self) -> tuple[McpCleanupDTO, ...]:
        with self._lock:
            return tuple(McpCleanupDTO(
                server_id=retired.runtime.server.server_id, connection_ref=connection_ref,
                phase=retired.phase, release_verified=retired.runtime.scope.is_closed,
                error_type=retired.error_type,
            ) for connection_ref, retired in sorted(self._retired.items()))

    def require_replaceable(self, server_ids: set[str]) -> None:
        """只阻止与未核实关闭的失败连接冲突的 Server 再次替换。"""
        with self._lock:
            conflicts = [connection_ref for connection_ref, retired in self._retired.items()
                         if retired.runtime.server.server_id in server_ids
                         and retired.phase == "cleanup_failed" and not retired.runtime.scope.is_closed]
            if conflicts:
                raise RuntimeError(f"MCP 退役连接尚未核实释放，拒绝冲突替换: {conflicts}")

    async def _drain(self, connection_ref: str, retired: _RetiredConnection) -> None:
        while True:
            with self._lock:
                # 未知操作是持久待核实事实，不因关闭 socket 被结算；没有本地
                # 活跃占用时不为它永久保留无用连接。
                active = any(lease.status == "active" for lease in self._leases.leases_for_resource(connection_ref))
            if not active or monotonic() >= retired.deadline:
                break
            await asyncio.sleep(max(0, min(0.1, retired.deadline - monotonic())))
        with self._lock:
            self._retired[connection_ref] = replace(self._retired[connection_ref], phase="closing")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await retired.runtime.scope.close()
        except Exception as error:
            with self._lock:
                self._retired[connection_ref] = replace(
                    self._retired[connection_ref], phase="cleanup_failed", error_type=type(error).__name__,
                )
                self._publisher.publish(resource_id=connection_ref, state="release_failed")
            # 原 scope 继续持有失败 task／资源并报告完整堆栈，不能伪报释放成功。
            raise
        with self._lock:
            self._retired.pop(connection_ref, None)
