from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.schemas.internal_v2.session import SessionDTO
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from app.services.business.terminal_resources.owner import (
    TerminalResourceOwner,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


SESSION_A = "ses_019b94653e71714b8d406f83ab3d2b3b"


SESSION_B = "ses_019b94653e71714b8d406f83ab3d2b31"


THREAD_A = "thr_019b94653e71714b8d406f83ab3d2b3b"


THREAD_B = "thr_019b94653e71714b8d406f83ab3d2b31"


CHILD = "thr_019b94653e71714b8d406f83ab3d2b32"


class Sessions:
    workspace_id = WORKSPACE_ID

    def __init__(self, root: Path) -> None:
        self.root = root
        self.refs = {SESSION_A: [THREAD_A, CHILD], SESSION_B: [THREAD_B]}
        self.closed: set[str] = set()
        self.admitted: list[str] = []
        self.unloading_generation = 1

    def accepts_release(self, session_id: str, thread_id: str, generation: int) -> bool:
        return thread_id in self.refs[session_id] and generation == self.unloading_generation

    @asynccontextmanager
    async def session_write_admission(self, session_id: str) -> AsyncIterator[None]:
        if session_id in self.closed:
            raise ValueError("Session 准入已关闭")
        await self.get(session_id)
        self.admitted.append(session_id)
        yield

    async def get(self, session_id: str) -> SessionDTO:
        return SessionDTO(session_id=session_id, workspace_id=WORKSPACE_ID,
                          resource_identity=session_resource_identity(WORKSPACE_ID, session_id),
                          uri=session_resource_uri(WORKSPACE_ID, session_id), scope="workspace",
                          title="测试会话", current_agent_id="default",
                          created_at=datetime(2026, 10, 5, tzinfo=UTC),
                          updated_at=datetime(2026, 10, 5, tzinfo=UTC),
                          thread_id=self.refs[session_id][0])

    def main_thread_id(self, session_id: str) -> str:
        return self.refs[session_id][0]

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        if thread_id not in self.refs[session_id]:
            raise KeyError("目标 Thread 不在本 Session catalog 中")
        return self.root / session_id / thread_id


class Transport:
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

    async def sync_owner_revision(self, terminal_id: str, owner_revision: int) -> None:
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
class Bundle:
    root: Path
    sessions: Sessions
    transport: Transport
    leases: ExternalResourceLeaseLedger
    owner: TerminalResourceOwner
    now: datetime

    def restart(self) -> TerminalResourceOwner:
        self.leases = ExternalResourceLeaseLedger(state_path=self.root / ".boxteam" / "resources.json")
        self.owner = TerminalResourceOwner(
            workspace_root=self.root, workspace_id=WORKSPACE_ID,
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


def build_terminal_bundle(tmp_path: Path) -> Bundle:
    root = tmp_path / "workspace"
    root.mkdir()
    result = Bundle(root, Sessions(root), Transport(root), ExternalResourceLeaseLedger(), None,
                    datetime(2026, 10, 5, tzinfo=UTC))
    result.restart()
    return result
