"""受管 cwd 的本地准备失败不能冒充已向 Terminal 派发创建。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.core.resource_uri import resource_display_uri
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A, WORKSPACE_ID
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)

ATTACHMENT_ID = "att_" + "a" * 32


@dataclass
class DirectoryPreparation:
    stage: str = ""
    failure: OSError | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    writer_threads: list[int] = field(default_factory=list)
    intercepted: bool = False
    uri: str = field(default_factory=lambda: resource_display_uri(
        scope="workspace", scope_id=WORKSPACE_ID, kind="attachments", tail_segments=(ATTACHMENT_ID,),
    ))

    @asynccontextmanager
    async def admit_working_directory(self, target: Path) -> AsyncIterator[str | None]:
        if self.stage == "admission":
            self.entered.set()
            if self.failure is not None:
                raise self.failure
            await self.resume.wait()
        yield self.uri
        if self.stage == "admission-exit":
            self.entered.set()
            await self.resume.wait()


@pytest.fixture
def terminal_owner(tmp_path: Path) -> TerminalOwnerFixture:
    return build_terminal_owner_fixture(tmp_path)


@pytest.fixture
async def preparation(
    terminal_owner: TerminalOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[DirectoryPreparation]:
    """真实 owner/账本写入，仅替换目录准入端口并在原原子发布点设置故障。"""
    prepared = DirectoryPreparation()
    owner = terminal_owner.owner
    owner._working_directories = prepared
    loop, replace = asyncio.get_running_loop(), Path.replace

    def publishing(source: Path, target: Path) -> Path:
        selected = False
        if not prepared.intercepted and prepared.stage in {"reference", "register", "acquire"}:
            if target == owner._store.path and prepared.stage == "reference":
                selected = prepared.uri in source.read_text()
            elif target == terminal_owner.leases._state_path:
                payload = json.loads(source.read_text())
                registered = any(record["resource_id"] == ATTACHMENT_ID for record in payload["records"])
                acquired = any(lease["resource_id"] == ATTACHMENT_ID for lease in payload["leases"])
                selected = registered and (acquired if prepared.stage == "acquire" else not acquired)
        if selected:
            prepared.intercepted = True
            prepared.writer_threads.append(get_ident())
            loop.call_soon_threadsafe(prepared.entered.set)
            assert prepared.release.wait(5), "测试未释放原目录准备写入"
            if prepared.failure is not None:
                raise prepared.failure
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", publishing)
    try:
        yield prepared
    finally:
        prepared.release.set()
        prepared.resume.set()


def assert_unstarted_closed(bundle: TerminalOwnerFixture, resource_id: str) -> None:
    record = bundle.owner._store.get(resource_id)
    assert record is not None and record.status == "deleted" and record.reconciliation_status == "verified"
    assert record.owner_revision == 1 and not record.thread_associations
    assert bundle.transport.creates == 0 and not bundle.transport.records and not bundle.transport.deleted
    cold = ExternalResourceLeaseLedger(state_path=bundle.leases._state_path)
    original = [lease for lease in cold._leases.values() if lease.resource_id == resource_id]
    assert len(original) == 1 and original[0].status == "settled"
    assert original[0].operation_id == "prepare-original" and original[0].turn_stream_id == "prepare-holder"
    external = cold.get(resource_id)
    assert external is not None and external.status == "stopped"
    assert not cold.leases_for_resource(resource_id)
    for lease in cold._leases.values():
        if lease.resource_id == ATTACHMENT_ID:
            assert lease.status == "settled" and lease.turn_stream_id == resource_id
    assert not bundle.owner._resource_lock(resource_id).locked()


@pytest.mark.parametrize("stage", ["admission", "reference", "register", "acquire"])
async def test_pre_dispatch_failure_closes_original_intent_without_native_creation(
    terminal_owner: TerminalOwnerFixture, preparation: DirectoryPreparation, stage: str,
) -> None:
    preparation.stage, preparation.failure = stage, OSError(28, "原目录准备失败")
    preparation.release.set()
    with pytest.raises(OSError) as caught:
        await terminal_owner.owner.create_terminal(
            session_id=SESSION_A, thread_id=THREAD_A,
            holder_id="prepare-holder", operation_id="prepare-original",
        )
    assert caught.value is preparation.failure or caught.value.__cause__ is preparation.failure
    rid, = terminal_owner.owner._store.ids()
    assert_unstarted_closed(terminal_owner, rid)
    cold = terminal_owner.restart()
    assert await cold.list_resources() == []
    previous = cold._store.get(rid)
    preparation.stage = ""
    cold._working_directories = preparation
    retry = await cold.create_terminal(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="prepare-retry", operation_id="prepare-retry",
    )
    assert retry["resource_id"] != rid and retry["reconciliation_status"] == "verified"
    assert terminal_owner.transport.creates == 1
    assert cold._store.get(rid) == previous


@pytest.mark.parametrize("stage", ["admission", "reference", "register", "acquire", "admission-exit"])
@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_interrupted_preparation_keeps_original_lock_and_never_dispatches_late(
    terminal_owner: TerminalOwnerFixture, preparation: DirectoryPreparation, stage: str, ending: str,
) -> None:
    preparation.stage = stage
    async with terminal_owner.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="prepare-holder", operation_id="prepare-original",
    ) as operation:
        creating = asyncio.create_task(operation.create_terminal(retention_scope="thread", title="原目录准备"))
        closing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(preparation.entered.wait(), 2)
            rid, = terminal_owner.owner._store.ids()
            lease, = terminal_owner.leases.leases_for_resource(rid)
            assert lease.status == "active" and terminal_owner.transport.creates == 0
            assert terminal_owner.owner._resource_lock(rid).locked()
            if stage in {"reference", "register", "acquire"}:
                assert preparation.writer_threads and preparation.writer_threads[0] != get_ident()
            if ending == "cancel":
                creating.cancel("原目录准备取消")
                await asyncio.sleep(0)
                if stage in {"reference", "register", "acquire"}:
                    creating.cancel("重复取消不能遗弃准备")
            closing = asyncio.create_task(operation.close())
            if ending == "close" or stage in {"reference", "register", "acquire"}:
                async with asyncio.timeout(2):
                    while not operation._closed:
                        await asyncio.sleep(0)
                assert not closing.done() and not creating.done()
                assert terminal_owner.owner._resource_lock(rid).locked()
            preparation.release.set()
            preparation.resume.set()
            if ending == "cancel":
                with pytest.raises(asyncio.CancelledError) as caught:
                    await creating
                assert caught.value.args == ("原目录准备取消",)
            else:
                with pytest.raises(RuntimeError, match="operation 已关闭"):
                    await creating
            await closing
            assert not operation._lease_refs and not operation._pending_resources
            assert_unstarted_closed(terminal_owner, rid)
        finally:
            preparation.release.set()
            preparation.resume.set()
            await asyncio.gather(*([creating] if closing is None else [creating, closing]), return_exceptions=True)


@pytest.mark.parametrize("lose_receipt", [False, True])
async def test_native_dispatch_requires_prepared_directory_and_preserves_unknown_after_dispatch(
    terminal_owner: TerminalOwnerFixture, preparation: DirectoryPreparation,
    monkeypatch: pytest.MonkeyPatch, lose_receipt: bool,
) -> None:
    original = terminal_owner.transport.create_terminal
    seen: list[str] = []
    failure = ConnectionError("外部创建回执丢失")

    async def create(
        *, terminal_id: str, owner_revision: int, title: str, agent_id: str | None,
        cwd: str, cols: int, rows: int,
    ) -> dict[str, object]:
        rid = terminal_id
        seen.append(rid)
        record = terminal_owner.owner._store.get(rid)
        assert record is not None and record.working_directory_uri == preparation.uri
        lease, = terminal_owner.leases.leases_for_resource(rid)
        assert lease.status == "dispatching"
        directory, = terminal_owner.leases.leases_for_resource(ATTACHMENT_ID)
        assert directory.status == "active" and directory.turn_stream_id == rid
        result = await original(
            terminal_id=terminal_id, owner_revision=owner_revision, title=title,
            agent_id=agent_id, cwd=cwd, cols=cols, rows=rows,
        )
        if lose_receipt:
            raise failure
        return result

    monkeypatch.setattr(terminal_owner.transport, "create_terminal", create)
    if lose_receipt:
        with pytest.raises(ConnectionError) as caught:
            await terminal_owner.owner.create_terminal(
                session_id=SESSION_A, thread_id=THREAD_A,
                holder_id="prepare-holder", operation_id="prepare-original",
            )
        assert caught.value is failure
    else:
        await terminal_owner.owner.create_terminal(
            session_id=SESSION_A, thread_id=THREAD_A,
            holder_id="prepare-holder", operation_id="prepare-original",
        )
    rid, = seen
    assert terminal_owner.transport.creates == 1
    cold = terminal_owner.restart()
    result = await cold.get_resource(rid)
    assert result["status"] == "running"
    assert result["reconciliation_status"] == ("unknown" if lose_receipt else "verified")
    directory, = terminal_owner.leases.leases_for_resource(ATTACHMENT_ID)
    assert directory.status == "active" and directory.turn_stream_id == rid
    if lose_receipt:
        lease, = terminal_owner.leases.leases_for_resource(rid)
        assert lease.status == "reconcile_required" and lease.operation_id == "prepare-original"
    else:
        assert not terminal_owner.leases.leases_for_resource(rid)
    assert terminal_owner.transport.creates == 1
