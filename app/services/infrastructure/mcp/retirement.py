"""原 MCP Owner 的退役连接排空，不保存第二份持久租约。"""

from __future__ import annotations

import asyncio
from _thread import RLock
from dataclasses import dataclass
from time import monotonic

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.session_runtime import McpServerRuntime


@dataclass(frozen=True, slots=True)
class _RetiredConnection:
    runtime: McpServerRuntime
    deadline: float
    admits_sealed: bool


class McpConnectionRetirement:
    """只保留原 runtime，持有事实每次读取唯一 ExternalResourceLeaseLedger。"""

    def __init__(self, *, leases: ExternalResourceLeaseLedger, lock: RLock, timeout_seconds: int) -> None:
        self._leases = leases
        self._lock = lock
        self._timeout_seconds = timeout_seconds
        self._retired: dict[str, _RetiredConnection] = {}

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

    async def _drain(self, connection_ref: str, retired: _RetiredConnection) -> None:
        # TODO(7.4): 将异步清理失败接回既有配置／活动诊断；当前由原
        # LifetimeScope 明确报告后台失败并保留引用，shutdown 仍重报释放失败。
        while True:
            with self._lock:
                # 未知操作是持久待核实事实，不因关闭 socket 被结算；没有本地
                # 活跃占用时不为它永久保留无用连接。
                active = any(lease.status == "active" for lease in self._leases.leases_for_resource(connection_ref))
            if not active or monotonic() >= retired.deadline:
                break
            await asyncio.sleep(max(0, min(0.1, retired.deadline - monotonic())))
        async with asyncio.timeout(self._timeout_seconds):
            await retired.runtime.scope.close()
        with self._lock:
            self._retired.pop(connection_ref, None)
