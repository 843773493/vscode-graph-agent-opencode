"""Terminal 领域 owner：创建与工具操作持有同一真实 lease。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.abstractions.terminal_resources import TerminalSnapshot
from app.abstractions.workspace_resources import (
    ResourceSessionReader,
    ResourceThreadResolver,
    ResourceTransport,
    RetentionScope,
)
from app.core.shielded_cleanup import run_cleanup_shielded
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources.owner import WorkspaceResourceOwner
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

logger = logging.getLogger(__name__)


class TerminalResourceOwner(WorkspaceResourceOwner):
    kind = "terminal"
    lease_kind = "terminal"

    def __init__(
        self, *, file_paths: WorkspaceFilePaths,
        sessions: ResourceSessionReader, threads: ResourceThreadResolver,
        transport: ResourceTransport, leases: ExternalResourceLeaseLedger,
        thread_release_fence: Callable[[str, str, int], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(
            workspace_root=file_paths.workspace, workspace_id=file_paths.workspace_id,
            sessions=sessions, threads=threads, transport=transport, leases=leases,
            thread_release_fence=thread_release_fence, clock=clock,
        )
        self._file_paths = file_paths

    def _resolve_workdir(self, cwd: str | None) -> str:
        """目录 URI 复用固定文件 owner；原生 Shell 路径保持目标平台语义。"""
        if cwd and cwd.startswith("boxteam://"):
            try:
                return str(self._file_paths.native(cwd))
            except (OSError, RuntimeError) as error:
                # resolve 的系统异常和符号链接环可携带本机路径；只在具名诊断日志保留。
                logger.exception("Terminal 目录 URI 解析失败: uri=%s", cwd)
                raise RuntimeError(
                    f"Terminal 目录 URI 解析失败: uri={cwd}, type={type(error).__name__}；"
                    "请查看工作区服务日志"
                ) from error
        # Shell 不施加普通文件树的私有目录/containment 限制；相对路径固定锚定工作区。
        requested = Path(cwd).expanduser() if cwd else self.workspace_root
        return str((requested if requested.is_absolute() else self.workspace_root / requested).resolve())

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
        resolved_cwd = self._resolve_workdir(cwd)
        return await self._create_record(
            ref=ref, retention_scope=retention_scope, title=title,
            holder_id=holder_id, operation_id=operation_id,
            created_by_turn_id=created_by_turn_id, lease_refs=lease_refs,
            create=lambda record: self._transport.create_terminal(
                terminal_id=record.resource_id, owner_revision=record.owner_revision,
                title=title, agent_id=agent_id, cwd=resolved_cwd,
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

