"""正式附件上传和真实 PTY 验证工作目录准入的 worker、holder 及持久占用。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import psutil
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    owner_mount as owner_mount,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_release import (
    resource_api as resource_api,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.mark.parametrize("stage", ["lookup", "available"])
async def test_attachment_cwd_admission_keeps_peer_http_and_publishes_real_process_lease(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, stage: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner, store = container.terminal_resource_owner, container.attachment_blob_store
        message = await container.message_service.create(sid, MessageCreateRequest(
            content="附件目录作为 Terminal 工作目录", attachments=[AttachmentRef(
                file_id="inline:cwd.txt", name="cwd.txt", content_type="text/plain",
                data_url="data:text/plain;base64,Y3dk",
            )],
        ))
        ref, = message.attachments
        assert ref.directory_uri is not None
        parsed = parse_resource_uri(ref.directory_uri)
        attachment_id = parsed.tail_segments[0]
        target = store.resolve_directory_uri(parsed)
        loop = asyncio.get_running_loop()
        event_thread = threading.get_ident()
        entered, release = asyncio.Event(), threading.Event()
        expired: list[bool] = []
        request_id = f"attachment-cwd-{stage}"
        gateway_id = "gateway_00000000000040008000000000000099"
        reference, available = store.reference_for_path, store._catalog.require_available_directory

        def wait() -> None:
            assert get_current_gateway_id() == gateway_id and get_current_request_id() == request_id
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                expired.append(True)
                raise TimeoutError("附件目录准入阻塞了同一服务的 HTTP 查询")
            assert threading.get_ident() != event_thread

        def lookup(path: Path) -> str | None:
            if path == target and stage == "lookup":
                wait()
            return reference(path)

        def require(identity: str):
            if identity == attachment_id and stage == "available":
                wait()
            return available(identity)

        async with resource_api(container, "terminal") as client:
            response = await client.post("/api/v1/resources/terminals", json={
                "session_id": sid, "retention_scope": "thread",
            })
            assert response.status_code == 200, response.text
            peer = response.json()["data"]["resource_id"]
            original = await owner._transport.get_resource(peer)
            with monkeypatch.context() as patch:
                patch.setattr(store, "reference_for_path", lookup)
                patch.setattr(store._catalog, "require_available_directory", require)
                creating = asyncio.create_task(client.post("/api/v1/resources/terminals", json={
                    "session_id": sid, "retention_scope": "thread", "cwd_relative": str(target),
                }, headers={"X-Request-ID": request_id, "X-BoxTeam-Gateway-Id": gateway_id}))
                try:
                    await asyncio.wait_for(entered.wait(), 7)
                    assert not expired and not creating.done()
                    healthy = await asyncio.wait_for(client.get(f"/api/v1/resources/{peer}"), 2)
                    assert healthy.status_code == 200, healthy.text
                    assert healthy.headers["X-Request-ID"] == healthy.json()["request_id"]
                    assert healthy.json()["data"]["status"] == "running"
                    assert (await owner._transport.get_resource(peer))["os_pid"] == original["os_pid"]
                    async with owner.deletion_holder.lock_for(sid, wait=False) as probe:
                        assert not probe.acquired
                    async with store._directory_operations.lock_for(attachment_id, wait=False) as probe:
                        assert probe.acquired is (stage == "lookup")
                    intents = [owner._store.get(rid) for rid in owner._store.ids() if rid != peer]
                    intent, = intents
                    assert intent is not None and intent.status == "creating"
                    assert intent.working_directory_uri is None
                    assert owner._leases.leases_for_resource(intent.resource_id)
                    assert owner._leases.get(attachment_id) is None
                    assert {item["terminal_id"] for item in await owner._transport.list_resources()} == {peer}
                    release.set()
                    response = await asyncio.wait_for(creating, 10)
                    assert response.status_code == 200, response.text
                    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
                finally:
                    release.set()
                    await asyncio.gather(creating, return_exceptions=True)
            created = response.json()["data"]
            rid = created["resource_id"]
            runtime = await owner._transport.get_resource(rid)
            pid = runtime["os_pid"]
            assert isinstance(pid, (int, float)) and not isinstance(pid, bool) and pid > 0 and pid == int(pid)
            assert Path(psutil.Process(int(pid)).cwd()).resolve() == target
            record = owner._store.get(rid)
            assert record is not None and record.working_directory_uri == store.directory_uri(attachment_id)
            directory_lease, = owner._leases.leases_for_resource(attachment_id)
            assert directory_lease.status == "active" and directory_lease.turn_stream_id == directory_lease.operation_id == rid
            assert directory_lease.holder_session_id == sid and directory_lease.holder_thread_id == message.thread_id
            assert not owner._leases.leases_for_resource(rid)
            assert (target / "original/content").read_bytes() == b"cwd"
            assert str(root) not in owner._store.path.read_text(encoding="utf-8")
            detached = await client.post(f"/api/v1/resources/{rid}/detach", json={
                "expected_revision": created["owner_revision"], "thread": created["source_thread"],
            })
            assert detached.status_code == 200, detached.text
            assert detached.json()["data"]["thread_associations"] == []
            deleted = await client.post(f"/api/v1/resources/{rid}/delete", json={
                "expected_revision": detached.json()["data"]["owner_revision"],
            })
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["data"]["resource_id"] == rid
            assert deleted.json()["data"]["status"] == "deleted"
            assert deleted.json()["data"]["reconciliation_status"] == "verified"
            settled = owner._leases.get_lease(directory_lease.lease_id)
            assert settled is not None and settled.status == "settled"
            assert not owner._leases.leases_for_resource(attachment_id)
        (run.artifacts_dir / f"attachment-cwd-{stage}.json").write_text(json.dumps({
            "attachment": ref.model_dump(mode="json"), "created": created, "runtime": runtime,
            "lease_id": directory_lease.lease_id, "deleted": deleted.json(),
            "scope": "正式消息附件上传、ASGI资源创建及真实Node HTTP/PTY；慢识别/复核期间peer可读，原intent/lease与holder保持；非模型执行、Gateway/Web或Backend重启验收",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
