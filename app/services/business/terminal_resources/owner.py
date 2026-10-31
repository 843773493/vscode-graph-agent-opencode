"""Terminal 领域 owner：创建与工具操作持有同一真实 lease。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.abstractions.terminal.resources import (
    TerminalSnapshot,
    TerminalWorkingDirectories,
)
from app.abstractions.workspace_resources import (
    ResourceSessionReader,
    ResourceThreadResolver,
    ResourceTransport,
    RetentionScope,
)
from app.core.failures import failure_detail
from app.core.resource_uri import parse_resource_uri
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources.creation import create_resource
from app.services.business.workspace_resources.facts import STOPPED_STATUSES
from app.services.business.workspace_resources.owner import WorkspaceResourceOwner
from app.services.business.workspace_resources.records import (
    OwnedResourceRecord,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


class TerminalResourceOwner(WorkspaceResourceOwner):
    kind = "terminal"
    lease_kind = "terminal"

    def __init__(
        self, *, file_paths: WorkspaceFilePaths,
        working_directories: TerminalWorkingDirectories | None,
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
        self._working_directories = working_directories

    def _resolve_workdir(self, cwd: str | None) -> str:
        """目录 URI 复用固定文件 owner；原生 Shell 路径保持目标平台语义。"""
        uri_input = cwd if cwd and cwd.startswith("boxteam://") else None
        try:
            if uri_input is not None:
                return str(self._file_paths.native(uri_input))
            # Shell 不施加普通文件树的私有目录/containment 限制；相对路径固定锚定工作区。
            requested = Path(cwd).expanduser() if cwd else self.workspace_root
            return str((requested if requested.is_absolute() else self.workspace_root / requested).resolve())
        except (OSError, RuntimeError) as error:
            # 三种输入共享原生失败出口，不能将 resolve 的路径写入 Job 等执行记录。
            label = f"Terminal 目录 URI 解析失败: uri={uri_input}" if uri_input is not None else (
                f"Terminal 工作目录解析失败: workspace_id={self.workspace_id}"
            )
            detail = failure_detail(error, stage="terminal_workdir", label=label)
            if uri_input is None and isinstance(error, OSError):
                if (winerror := getattr(error, "winerror", None)) is not None:
                    raise type(error)(error.errno, detail, None, winerror) from error
                raise type(error)(error.errno, detail) from error
            raise RuntimeError(detail) from error

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
        operation: TerminalOperations,
        retention_scope: RetentionScope,
        title: str,
        agent_id: str | None,
        cwd: str | None,
        cols: int,
        rows: int,
    ) -> TerminalSnapshot:
        # 原生路径及目录 URI 都可能等待文件系统或同机 Gateway；原 holder 覆盖 worker 结算。
        # TODO(5.4)：原生 I/O 抢占与整个 operation／shutdown 期限仍须分别核实。
        resolved_cwd = await run_sync_shielded(self._resolve_workdir, cwd)

        async def prepare(record: OwnedResourceRecord) -> None:
            if self._working_directories is not None:
                async with self._working_directories.admit_working_directory(Path(resolved_cwd)) as uri:
                    if uri is not None:
                        record.working_directory_uri = uri
                        await self._store.save(record)
                        attachment_id = parse_resource_uri(uri).tail_segments[0]

                        def acquire_directory() -> None:
                            self._leases.register(resource_id=attachment_id, kind="attachment_directory", lifetime_scope="workspace")
                            # 绑定持久终端身份，工具调用/Turn 收尾不能释放此占用。
                            self._leases.acquire_operation(
                                resource_id=attachment_id, turn_stream_id=record.resource_id,
                                operation_id=record.resource_id, lease_id=self._directory_lease_id(record.resource_id, uri),
                                holder_session_id=operation._thread.session_id, holder_thread_id=operation._thread.thread_id,
                            )

                        # 原目录holder与资源锁覆盖排队、写盘和取消结算，不先标为外部已派发。
                        await run_sync_shielded(acquire_directory)

        return await create_resource(
            self, operation=operation, retention_scope=retention_scope, title=title, prepare=prepare,
            create=lambda record: self._transport.create_terminal(
                terminal_id=record.resource_id, owner_revision=record.owner_revision,
                title=title, agent_id=agent_id, cwd=resolved_cwd, cols=cols, rows=rows,
            ),
        )

    @staticmethod
    def _directory_lease_id(terminal_id: str, uri: str) -> str:
        attachment_id = parse_resource_uri(uri).tail_segments[0]
        return f"{terminal_id}:working-directory:{attachment_id}"

    async def _sync_ledger(self, record: OwnedResourceRecord) -> None:
        await super()._sync_ledger(record)
        if (record.working_directory_uri is not None and record.reconciliation_status == "verified"
                and record.status in STOPPED_STATUSES):
            lease = self._leases.get_lease(self._directory_lease_id(record.resource_id, record.working_directory_uri))
            if lease is not None and lease.status in {"active", "reconcile_required"}:
                await self._leases.settle(lease.lease_id)

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
        await self.recover(())
        async with self.admit_thread(session_id, thread_id) as ref:
            operations = TerminalOperations(
                self, ref, holder_id, operation_id, created_by_turn_id
            )
            try:
                yield operations
            finally:
                await run_cleanup_shielded(operations.close)

