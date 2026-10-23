"""真实 Browser owner 与租约账本，配合内存传输替身的测试装具。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.services.business.browser_resources.owner import BrowserResourceOwner
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.sessions import (
    SESSION_A,
    WORKSPACE_ID,
    ResourceOwnerSessionsFake,
)


class BrowserTransportFake:
    """只模拟传输记录和回执，不启动真实浏览器。"""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, str, dict[str, object], str]] = []
        self.creates = 0
        self.deleted: list[str] = []
        self.failure: BaseException | None = None

    async def create_browser(self, *, browser_id: str, owner_revision: int, **options: object) -> dict[str, object]:
        self.creates += 1
        self.records[browser_id] = {
            "browser_id": browser_id, "workspace_id": WORKSPACE_ID,
            "status": "running", "reconciliation_status": "verified",
            "transport_owner_revision": owner_revision,
            "created_at": "2026-10-05T00:00:00Z", "updated_at": "2026-10-05T00:00:00Z",
            "active_page_id": "page_internal", "pages": [{
                "page_id": "page_internal", "title": "Example", "url": "https://example.test", "active": True,
            }], **options,
        }
        return dict(self.records[browser_id])

    async def list_resources(self) -> list[dict[str, object]]:
        return [dict(item) for item in self.records.values()]

    async def get_resource(self, resource_id: str) -> dict[str, object]:
        return dict(self.records[resource_id])

    async def sync_owner_revision(self, resource_id: str, owner_revision: int) -> None:
        self.records[resource_id]["transport_owner_revision"] = owner_revision

    async def issue_transport_grant(self, resource_id: str, *, owner_revision: int, association: str | None) -> dict[str, object]:
        assert self.records[resource_id]["transport_owner_revision"] == owner_revision
        return {"resource_id": resource_id, "owner_revision": owner_revision, "grant_token": "once",
                "expires_at": "2026-10-05T00:00:30Z"}

    async def perform(self, resource_id: str, action: str, payload: dict[str, object], *, actor: str) -> dict[str, object]:
        self.calls.append((resource_id, action, payload, actor))
        if self.failure is not None:
            raise self.failure
        if action == "navigate":
            self.records[resource_id]["url"] = payload["url"]
        return {"browser": dict(self.records[resource_id]), "summary": "Example", "result": "ok"}

    async def stop_resource(self, resource_id: str, *, reason: str | None = None) -> dict[str, object]:
        self.records[resource_id]["status"] = "closed"
        return {"browser": dict(self.records[resource_id])}

    async def delete_resource(self, resource_id: str) -> dict[str, object]:
        if self.failure is not None:
            raise self.failure
        self.deleted.append(resource_id)
        self.records[resource_id]["status"] = "deleted"
        return {"deleted": True, "browser": dict(self.records[resource_id])}


@dataclass
class BrowserOwnerFixture:
    # 本装具创建的工作区路径，不是模板目录或整个测试输出根。
    root: Path
    sessions: ResourceOwnerSessionsFake
    transport: BrowserTransportFake
    # 可由用例推进的owner时钟，不表示资源创建时间。
    now: datetime
    leases: ExternalResourceLeaseLedger = field(init=False)
    owner: BrowserResourceOwner | None = field(init=False)

    def restart(self) -> BrowserResourceOwner:
        self.leases = ExternalResourceLeaseLedger(state_path=self.root / ".boxteam" / "resources.json")
        self.owner = BrowserResourceOwner(
            workspace_root=self.root, workspace_id=WORKSPACE_ID, sessions=self.sessions,
            threads=self.sessions, transport=self.transport, leases=self.leases,
            clock=lambda: self.now, thread_release_fence=self.sessions.accepts_release,
        )
        return self.owner

    async def create(self, scope: str = "thread", thread: str | None = None) -> dict[str, object]:
        assert self.owner is not None
        return await self.owner.create_browser(
            session_id=SESSION_A, thread_id=thread, retention_scope=scope,
            holder_id="request:browser-test", operation_id="browser-test",
        )

    async def command(self, resource_id: str, **arguments: object) -> dict[str, object]:
        assert self.owner is not None
        return await self.owner.command(resource_id, holder_id="request:browser-control",
                                        operation_id="browser-control", **arguments)


def build_browser_owner_fixture(root: Path) -> BrowserOwnerFixture:
    workspace = root / "workspace"
    workspace.mkdir()
    bundle = BrowserOwnerFixture(
        root=workspace,
        sessions=ResourceOwnerSessionsFake(workspace),
        transport=BrowserTransportFake(),
        now=datetime(2026, 10, 5, tzinfo=UTC),
    )
    bundle.restart()
    return bundle
