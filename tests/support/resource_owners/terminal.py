"""真实 Terminal owner 与租约账本，配合内存传输替身的测试装具。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from tests.support.resource_owners.sessions import (
    SESSION_A,
    WORKSPACE_ID,
    ResourceOwnerSessionsFake,
)


class TerminalTransportFake:
    """只模拟传输记录和回执，不启动真实终端进程。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: dict[str, dict[str, object]] = {}
        self.creates = 0
        self.deleted: list[str] = []
        self.revisions: dict[str, int] = {}
        self.grants: dict[str, tuple[str, int, str | None]] = {}
        self.fail_delete = False

    async def create_terminal(
        self, *, terminal_id: str, owner_revision: int, title: str, agent_id: str | None,
        cwd: str, cols: int, rows: int,
    ) -> dict[str, object]:
        self.creates += 1
        self.revisions[terminal_id] = owner_revision
        self.records[terminal_id] = {
            "terminal_id": terminal_id, "workspace_id": WORKSPACE_ID,
            "status": "running", "reconciliation_status": "verified",
            "title": title, "cwd_relative": os.path.relpath(cwd, self.root),
            "cols": cols, "rows": rows, "buffer": "", "sequence": 0,
            "created_at": "2026-10-05T00:00:00Z", "updated_at": "2026-10-05T00:00:00Z",
            "transport_owner_revision": owner_revision,
        }
        return dict(self.records[terminal_id])

    async def list_resources(self) -> list[dict[str, object]]:
        return [dict(record) for record in self.records.values()]

    async def get_resource(self, terminal_id: str) -> dict[str, object]:
        return dict(self.records[terminal_id])

    async def sync_owner_revision(self, terminal_id: str, owner_revision: int, *, for_stop: bool) -> None:
        self.revisions[terminal_id] = owner_revision
        self.records[terminal_id]["transport_owner_revision"] = owner_revision
        self.grants = {
            token: grant for token, grant in self.grants.items()
            if grant[0] != terminal_id or grant[1] == owner_revision
        }

    async def issue_transport_grant(
        self, terminal_id: str, *, owner_revision: int, association: str | None,
    ) -> dict[str, object]:
        token = f"grant-{terminal_id}-{owner_revision}"
        self.grants[token] = (terminal_id, owner_revision, association)
        return {
            "resource_id": terminal_id, "owner_revision": owner_revision,
            "grant_token": token, "expires_at": "2026-10-05T00:00:30Z",
        }

    async def stop_resource(self, terminal_id: str, *, reason: str | None = None) -> dict[str, object]:
        self.records[terminal_id]["status"] = "terminated"
        return {"killed": True, "terminal": dict(self.records[terminal_id])}

    async def delete_resource(self, terminal_id: str) -> dict[str, object]:
        if self.fail_delete:
            raise ConnectionError("删除请求发送后丢失回执")
        self.deleted.append(terminal_id)
        self.records[terminal_id]["status"] = "deleted"
        return {"deleted": True, "terminal": dict(self.records[terminal_id])}

    async def read_terminal(self, terminal_id: str) -> dict[str, object]:
        return {"terminal": dict(self.records[terminal_id]), "output": "", "sequence": 0}

    async def write_terminal(self, *, terminal_id: str, data: str, source: str, command: str | None) -> dict[str, object]:
        self.records[terminal_id]["buffer"] = data
        return dict(self.records[terminal_id])


@dataclass
class TerminalOwnerFixture:
    # 本装具创建的工作区路径，不是模板目录或整个测试输出根。
    root: Path
    sessions: ResourceOwnerSessionsFake
    transport: TerminalTransportFake
    # 可由用例推进的owner时钟，不表示资源创建时间。
    now: datetime
    leases: ExternalResourceLeaseLedger = field(init=False)
    owner: TerminalResourceOwner = field(init=False)

    def restart(self) -> TerminalResourceOwner:
        self.leases = ExternalResourceLeaseLedger(state_path=self.root / ".boxteam" / "resources.json")
        self.owner = TerminalResourceOwner(
            working_directories=None,
            file_paths=WorkspaceFilePaths(workspace=self.root, workspace_id=WORKSPACE_ID, bindings=None),
            sessions=self.sessions, threads=self.sessions, transport=self.transport,
            leases=self.leases, clock=lambda: self.now,
            thread_release_fence=self.sessions.accepts_release,
        )
        return self.owner

    async def create(self, scope: str = "thread", thread: str | None = None) -> dict[str, object]:
        return await self.owner.create_terminal(
            session_id=SESSION_A, thread_id=thread, retention_scope=scope,
            holder_id="request:owner-test", operation_id="request-owner-test",
        )

    async def command(self, resource_id: str, **arguments: object) -> dict[str, object]:
        return await self.owner.command(
            resource_id, holder_id="request:owner-command", operation_id="owner-command",
            **arguments,
        )


def build_terminal_owner_fixture(tmp_path: Path) -> TerminalOwnerFixture:
    root = tmp_path / "workspace"
    root.mkdir()
    result = TerminalOwnerFixture(
        root=root,
        sessions=ResourceOwnerSessionsFake(root),
        transport=TerminalTransportFake(root),
        now=datetime(2026, 10, 5, tzinfo=UTC),
    )
    result.restart()
    return result
