"""一次受信工具调用持有的 Terminal operation lease 引用。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.abstractions.terminal_resources import TerminalSnapshot
from app.abstractions.workspace_resources import RetentionScope
from app.services.business.workspace_resources.facts import (
    observe_resource,
    resource_projection,
)
from app.services.business.workspace_resources.operations import ResourceOperations

if TYPE_CHECKING:
    from app.services.business.terminal_resources.owner import TerminalResourceOwner


class TerminalOperations(ResourceOperations):
    _owner: TerminalResourceOwner

    async def create_terminal(
        self, *, retention_scope: RetentionScope, title: str,
        agent_id: str | None = None, cwd: str | None = None,
        cols: int = 100, rows: int = 30,
    ) -> TerminalSnapshot:
        self._assert_open()
        if retention_scope not in {"thread", "workspace"}:
            raise ValueError(f"Terminal 保留范围无效: {retention_scope}")
        terminal = await self._owner._create(
            ref=self._thread, retention_scope=retention_scope, title=title,
            agent_id=agent_id, cwd=cwd, cols=cols, rows=rows,
            holder_id=self._holder_id, operation_id=self._operation_id,
            created_by_turn_id=self._created_by_turn_id,
            lease_refs=self._lease_refs,
        )
        return terminal

    async def get_resource(self, terminal_id: str) -> TerminalSnapshot:
        return await self._call(terminal_id, lambda: self._owner._transport.get_resource(terminal_id))

    async def list_resources(self) -> list[TerminalSnapshot]:
        self._assert_open()
        return await self._owner.list_resources(
            session_id=self._thread.session_id, thread_id=self._thread.thread_id,
        )

    async def read_terminal(self, terminal_id: str) -> TerminalSnapshot:
        return await self._call(terminal_id, lambda: self._owner._transport.read_terminal(terminal_id))

    async def write_terminal(
        self, *, terminal_id: str, data: str, source: str = "agent", command: str | None = None,
    ) -> TerminalSnapshot:
        return await self._call(terminal_id, lambda: self._owner._transport.write_terminal(
            terminal_id=terminal_id, data=data, source=source, command=command,
        ))

    async def mark_terminal_backgrounded(self, terminal_id: str) -> TerminalSnapshot:
        return await self._call(terminal_id, lambda: self._owner._transport.mark_terminal_backgrounded(terminal_id))

    async def claim_terminal_steering(
        self, terminal_id: str, *, completion_event_id: str, command_start_sequence: int,
    ) -> TerminalSnapshot:
        return await self._call(terminal_id, lambda: self._owner._transport.claim_terminal_steering(
            terminal_id, completion_event_id=completion_event_id,
            command_start_sequence=command_start_sequence,
        ))

    async def finish_terminal_steering(
        self, terminal_id: str, *, completion_event_id: str, command_start_sequence: int,
        dispatched: bool,
    ) -> TerminalSnapshot:
        return await self._call(terminal_id, lambda: self._owner._transport.finish_terminal_steering(
            terminal_id, completion_event_id=completion_event_id,
            command_start_sequence=command_start_sequence, dispatched=dispatched,
        ))

    async def kill_terminal(self, terminal_id: str, *, reason: str = "model_requested") -> TerminalSnapshot:
        if reason not in {"model_requested", "terminal_cancel"}:
            raise ValueError(f"终端停止原因无效: {reason}")
        async with self._resource(terminal_id) as record:
            result = await self._owner._command_record(
                record, "stop", record.owner_revision, self._thread,
                self._holder_id, self._operation_id, self._lease_refs[terminal_id],
                stop_reason=reason,
            )
        return {"killed": result["status"] != "running", "terminal": result}

    async def complete_terminal(self, terminal_id: str) -> TerminalSnapshot:
        """命令结束只回收来源 Thread 的临时执行；workspace 记录始终保留。"""
        async with self._resource(terminal_id) as record:
            if record.retention_scope == "workspace":
                return resource_projection(record, await self._owner._transport.get_resource(terminal_id), self._owner.kind)
            if record.source_thread != self._thread:
                raise ValueError("只有来源 Thread 能回收临时命令执行")
            if any(lease.lease_id != self._lease_refs[terminal_id]
                   for lease in self._owner._leases.leases_for_resource(terminal_id)):
                raise ValueError("临时命令仍有其它 operation lease，不能回收")
            record.thread_associations = []
            return await self._owner._stop_or_delete(record, delete=True, lease_id=self._lease_refs[terminal_id])

    async def _call(
        self, terminal_id: str, call: Callable[[], Awaitable[TerminalSnapshot]],
    ) -> TerminalSnapshot:
        async with self._resource(terminal_id) as record:
            try:
                result = await call()
                runtime = result.get("terminal", result)
                if not isinstance(runtime, dict):
                    raise TypeError("Terminal operation 缺少 PTY 完整事实")
                observe_resource(record, runtime, self._owner.kind)
                self._owner._store.save(record)
                self._owner._sync_ledger(record)
                projection = resource_projection(record, runtime, self._owner.kind)
                return {**result, "terminal": projection} if "terminal" in result else projection
            except BaseException as error:
                self._owner._unknown(record, error, lease_id=self._lease_refs[terminal_id])
                raise
