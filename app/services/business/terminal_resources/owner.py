"""Terminal 领域 owner：创建与工具操作持有同一真实 lease。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.abstractions.terminal_resources import TerminalSnapshot
from app.abstractions.workspace_resources import RetentionScope
from app.core.shielded_cleanup import run_cleanup_shielded
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources.owner import WorkspaceResourceOwner
from app.services.business.workspace_resources.records import ThreadRef


class TerminalResourceOwner(WorkspaceResourceOwner):
    kind = "terminal"
    lease_kind = "terminal"

    async def create_terminal(
        self,
        *,
        session_id: str,
        thread_id: str | None = None,
        retention_scope: RetentionScope = "thread",
        title: str = "Terminal",
        agent_id: str | None = None,
        cwd: str | None = None,
        cols: int = 100,
        rows: int = 30,
        holder_id: str,
        operation_id: str,
        created_by_turn_id: str | None = None,
    ) -> TerminalSnapshot:
        if retention_scope not in {"thread", "workspace"}:
            raise ValueError(f"Terminal retention_scope 无效: {retention_scope}")
        async with self.operation(
            session_id=session_id,
            thread_id=thread_id,
            holder_id=holder_id,
            operation_id=operation_id,
            created_by_turn_id=created_by_turn_id,
        ) as operation:
            return await operation.create_terminal(
                retention_scope=retention_scope,
                title=title,
                agent_id=agent_id,
                cwd=cwd,
                cols=cols,
                rows=rows,
            )

    async def _create(
        self,
        *,
        ref: ThreadRef,
        retention_scope: RetentionScope,
        title: str,
        agent_id: str | None,
        cwd: str | None,
        cols: int,
        rows: int,
        holder_id: str,
        operation_id: str,
        created_by_turn_id: str | None,
        lease_refs: dict[str, str],
    ) -> TerminalSnapshot:
        return await self._create_record(
            ref=ref, retention_scope=retention_scope, title=title,
            holder_id=holder_id, operation_id=operation_id,
            created_by_turn_id=created_by_turn_id, lease_refs=lease_refs,
            create=lambda record: self._transport.create_terminal(
                terminal_id=record.resource_id, owner_revision=record.owner_revision,
                title=title, agent_id=agent_id, cwd=cwd or str(self.workspace_root),
                cols=cols, rows=rows,
            ),
        )

    @asynccontextmanager
    async def operation(
        self,
        *,
        session_id: str,
        thread_id: str | None,
        holder_id: str,
        operation_id: str,
        created_by_turn_id: str | None = None,
    ) -> AsyncIterator[TerminalOperations]:
        await self.recover()
        async with self.admit_thread(session_id, thread_id) as ref:
            operations = TerminalOperations(
                self, ref, holder_id, operation_id, created_by_turn_id
            )
            try:
                yield operations
            finally:
                await run_cleanup_shielded(operations.close)

