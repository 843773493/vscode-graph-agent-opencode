"""真实上传在跨库 durable 边界硬退出后，恢复只结算同一 operation。"""

from __future__ import annotations

import asyncio
import base64
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

SESSION = "ses_019b781159d47374805bc4427d081a14"
BODY = b"durable-upload-crash-boundary"

_UPLOAD_WORKER = r'''
import asyncio
import base64
import os
import sys
from pathlib import Path

from app.core.path_utils import build_session_catalog_components
from app.core.session_control_store import SessionControlStore
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.store import AttachmentBlobStore

root, session, boundary = sys.argv[1:]
components = build_session_catalog_components(Path(root) / ".boxteam" / "sessions", read_workspace_id(Path(root)))
store = AttachmentBlobStore(Path(root), path_resolver=components.resolver)
thread = store._path_resolver.main_thread_id(session)
if boundary == "pin":
    store._catalog.create_or_get_ingest_record = lambda **kwargs: os._exit(73)
elif boundary in {"staging", "rename"}:
    name = "_write_staging" if boundary == "staging" else "_commit_staged_blob"
    original = getattr(store, name)
    def stop(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    setattr(store, name, stop)
else:
    original = SessionControlStore.mark_lease_settling
    def stop(control, **kwargs):
        if boundary == "published-settling":
            original(control, **kwargs)
        os._exit(73)
    SessionControlStore.mark_lease_settling = stop
body = base64.b64encode(b"durable-upload-crash-boundary").decode()
attachment = AttachmentRef(file_id="inline:crash", name="crash.txt", content_type="text/plain",
                           data_url="data:text/plain;base64," + body)
asyncio.run(store.persist_inline(session, [attachment], thread_id=thread))
raise AssertionError("上传没有经过要求的崩溃边界")
'''


@dataclass
class IngestCase:
    root: Path
    store: AttachmentBlobStore

    def crash(self, boundary: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", _UPLOAD_WORKER, str(self.root), SESSION, boundary],
            capture_output=True, text=True, timeout=20, check=False,
        )
        assert result.returncode == 73, result.stdout + result.stderr

    def control(self) -> SessionControlStore:
        return SessionControlStore(self.store._path_resolver.resolve_session_node(SESSION) / "session-control.sqlite")


@pytest.fixture
def ingest_case(tmp_path, session_bundle_factory):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", SESSION)
    case = IngestCase(tmp_path, AttachmentBlobStore(tmp_path))
    yield case
    case.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["pin", "staging", "rename", "published-active", "published-settling"])
async def test_restart_recovers_upload_pin_at_real_durable_boundary(ingest_case, boundary):
    case = ingest_case
    case.crash(boundary)
    with closing(case.control()) as control:
        (before,) = control.list_non_terminal_leases()
    records = case.store._catalog.list_non_terminal_ingest_records(owner_session_id=SESSION)
    staging = [resolve_staging_path(case.store._attachments_root, row.staging_relative_locator) for row in records]
    published = boundary.startswith("published")
    refs = case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True)
    assert bool(refs) == published

    assert await case.store.recover_pending_ingests() == 1
    assert await case.store.recover_pending_ingests() == 0
    with closing(case.control()) as control:
        after = control.get_lease(before.lease_id)
        assert control.list_non_terminal_leases() == ()
    assert after.operation_identity == before.operation_identity
    assert after.preimage_hash == before.preimage_hash
    assert after.fencing_token == before.fencing_token + 1
    assert after.state == ("completed" if published else "failed")
    assert case.store._catalog.list_non_terminal_ingest_records(owner_session_id=SESSION) == ()
    assert all(not path.exists() for path in staging)
    assert case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True) == refs
    if published:
        assert case.store.read(SESSION, case.store.reference(SESSION, refs[0].attachment_id)).data == BODY


@pytest.mark.asyncio
async def test_recovery_cannot_take_over_live_upload_holder(ingest_case):
    case = ingest_case
    case.crash("pin")
    with closing(case.control()) as control:
        (before,) = control.list_non_terminal_leases()
    async with case.store.deletion_holder.lock_for(SESSION, exclusive=False):
        assert await asyncio.wait_for(case.store.recover_pending_ingests(), timeout=1) == 0
        with closing(case.control()) as control:
            assert control.get_lease(before.lease_id) == before
    assert await case.store.recover_pending_ingests() == 1


@pytest.mark.asyncio
async def test_thread_delete_waits_for_real_attachment_publication(ingest_case, monkeypatch):
    """删除必须先排空上传，避免先删 rollout 再留下晚到的 active owner-ref。"""
    case = ingest_case
    thread = case.store._path_resolver.main_thread_id(SESSION)
    publishing = asyncio.Event()
    release = asyncio.Event()
    publish = case.store._publish

    async def paused_publish(**kwargs):
        publishing.set()
        await release.wait()
        return await publish(**kwargs)

    monkeypatch.setattr(case.store, "_publish", paused_publish)
    attachment = AttachmentRef(
        file_id="inline:delete-race", name="race.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode(BODY).decode(),
    )
    with RolloutCheckpointSaver(case.root / ".boxteam/sessions", attachment_store=case.store) as saver:
        saver._storage.storage.initialize(SESSION, thread_id=thread)

        async def delete_thread():
            owner = MutationIntentOwner(session_id=SESSION, thread_id=thread)
            with session_thread_owner_scope(owner, langgraph_thread_id=thread):
                await saver.adelete_thread(thread)

        upload = asyncio.create_task(case.store.persist_inline(SESSION, [attachment], thread_id=thread))
        deletion = None
        try:
            await asyncio.wait_for(publishing.wait(), timeout=2)
            deletion = asyncio.create_task(delete_thread())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(deletion), timeout=0.15)
        finally:
            release.set()
            await asyncio.gather(upload, *([deletion] if deletion is not None else []))
        assert case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True) == ()
        assert not saver._storage.storage.root(SESSION, thread_id=thread).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["mark_lease_settling", "settle_lease"])
async def test_settlement_error_preserves_durable_published_subject(ingest_case, monkeypatch, method):
    case = ingest_case
    original = getattr(SessionControlStore, method)
    failed = False

    def fail_once(control, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected attachment settlement failure")
        return original(control, **kwargs)

    monkeypatch.setattr(SessionControlStore, method, fail_once)
    attachment = AttachmentRef(
        file_id="inline:settle", name="settle.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode(BODY).decode(),
    )
    with pytest.raises(RuntimeError, match="injected attachment settlement failure"):
        await case.store.persist_inline(SESSION, [attachment], thread_id=case.store._path_resolver.main_thread_id(SESSION))
    (ref,) = case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True)
    with closing(case.control()) as control:
        (pin,) = control.list_non_terminal_leases()
    assert pin.state == ("active" if method == "mark_lease_settling" else "settling")
    assert case.store._catalog.get_ingest_record(pin.operation_identity).state == "published"
    assert await case.store.recover_pending_ingests() == 1
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id).state == "completed"
    assert case.store.read(SESSION, case.store.reference(SESSION, ref.attachment_id)).data == BODY
