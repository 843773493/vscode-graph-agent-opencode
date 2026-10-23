"""真实文件owner、SQLite受理、目录CAS与原operation恢复合同。"""

from __future__ import annotations

import asyncio
import io
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import workspace as workspace_api
from app.api.deps import get_request_id, get_workspace_service, verify_local_token
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceFileMutationIntentDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_files import operations as operations_module
from app.services.infrastructure.workspace_files.filesystem import (
    FileEffectUnverifiedError,
)
from app.services.infrastructure.workspace_files.store import (
    WorkspaceFileOperationConflictError,
)
from app.services.infrastructure.workspace_service import WorkspaceService


@pytest.fixture
async def file_case(empty_workspace_scope):
    _, workspace = empty_workspace_scope
    (workspace / "target").mkdir()
    (workspace / "other").mkdir()
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    config.get_workspace_preview_max_bytes.return_value = 1024 * 1024
    config.get_workspace_preview_binary_sample_bytes.return_value = 8192
    service = WorkspaceService(config_service=config, workspace_root=workspace)
    await service.start()
    case = SimpleNamespace(service=service, owner=service.file_operations, workspace=workspace)
    yield case
    await service.stop()
    service.close()


async def intent(case, path="target", **overrides):
    snapshot = await case.service.list_files(path=path)
    return WorkspaceFileMutationIntentDTO(client_operation_id="op_" + uuid4().hex, client_sequence=1,
                                           directory_key=snapshot.directory_key,
                                           expected_directory_revision=snapshot.directory_revision, **overrides)


async def terminal(case, receipt):
    for _ in range(300):
        result = case.owner.status([receipt.operation_id], actor="local").operations[0]
        if result.state in ("committed", "rejected", "cancelled", "dependency_failed"):
            return result
        if case.owner.worker_state == "failed":
            raise RuntimeError(case.owner.worker_error)
        await asyncio.sleep(0.01)
    raise AssertionError("文件操作未在有界时间收口")


async def test_durable_202_original_id_replay_and_terminal_directory_version(file_case, monkeypatch):
    case = file_case
    entered, release = Event(), Event()
    original = case.owner._execute
    def blocked(record):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("测试执行gate未释放")
        original(record)
    monkeypatch.setattr(case.owner, "_execute", blocked)
    app = FastAPI()
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.dependency_overrides[get_workspace_service] = lambda: case.service
    app.dependency_overrides[verify_local_token] = lambda: "测试已认证local-token"
    app.dependency_overrides[get_request_id] = lambda: "req_file_202"
    command = await intent(case)
    payload = {"name": "中文 空格.txt", "kind": "file", "intent": command.model_dump()}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://file.test") as client:
            response = await client.post("/api/v1/workspace/files/entries?path=target", json=payload)
            assert response.status_code == 202
            accepted = response.json()["data"]
            assert accepted["state"] == "queued" and not (case.workspace / "target/中文 空格.txt").exists()
            assert case.owner.store.get(command.client_operation_id, "local").receipt.state == "queued"
            replay = await client.post("/api/v1/workspace/files/entries?path=target", json=payload)
            assert replay.status_code == 202 and replay.json()["data"]["operation_id"] == accepted["operation_id"]
            conflict = await client.post("/api/v1/workspace/files/entries?path=target", json={**payload, "name": "漂移.txt"})
            assert conflict.status_code == 409
        release.set()
        result = await terminal(case, case.owner.store.get(command.client_operation_id, "local").receipt)
        assert result.state == "committed" and result.effect == "complete"
        listing = await case.service.list_files(path="target")
        assert [item.name for item in listing.items] == ["中文 空格.txt"]
        assert listing.directory_revision == next(value.directory_revision for value in result.directories if value.directory_key.path == listing.path)
        assert case.owner.store.events(0)[-1]["directories"]
    finally:
        release.set()


async def test_four_entries_worker_only_and_upload_body_is_durable(file_case):
    case = file_case
    source = case.workspace / "other/源.txt"
    source.write_text("原正文", encoding="utf-8")
    directory = case.workspace / "other/源目录"
    directory.mkdir()
    (directory / "child.txt").write_text("目录正文", encoding="utf-8")
    commands = [
        ("create", {"name": "new", "kind": "directory"}),
        ("copy", {"source_path": "other/源.txt", "source_scope": "workspace"}),
        ("paste", {"source_paths": [str(directory)]}),
    ]
    for kind, data in commands:
        command = await intent(case)
        accepted = await case.owner.enqueue(kind=kind, directory_path="target", scope="workspace", intent=command, actor="local", data=data)
        assert accepted.state == "queued"
        assert (await terminal(case, accepted)).state == "committed"
    command = await intent(case)
    stream = io.BytesIO(b"durable bytes")
    receipt = await case.service.upload_file_entries(directory_path="target", scope="workspace", intent=command, actor="local",
                                                    entries=[("子文件夹/上传.txt", stream), ("独立.bin", io.BytesIO(b"\x00\x01"))])
    frozen = case.owner.store.get(receipt.operation_id, "local")
    incoming = case.owner._incoming_path(frozen)
    assert (incoming / "body/子文件夹/上传.txt").read_bytes() == b"durable bytes"
    stream.close()
    result = await terminal(case, receipt)
    assert result.state == "committed"
    assert (case.workspace / "target/子文件夹/上传.txt").read_bytes() == b"durable bytes"
    # terminal 先持久化再回收暂存，等待的是回收边界而非再次提交操作。
    for _ in range(300):
        if not incoming.exists():
            break
        if case.owner.worker_state == "failed":
            raise RuntimeError(case.owner.worker_error)
        await asyncio.sleep(0.01)
    assert not incoming.exists()


@pytest.mark.parametrize("invalid_path", [
    "/private-upload-marker/file.txt", "private-upload-marker/", "private-upload-marker//file.txt",
    r"C:\private-upload-marker\file.txt", r"C:private-upload-marker.txt",
    r"\\private-upload-marker\share\file.txt", "../private-upload-marker/file.txt",
    "private-upload-marker/./file.txt", "private-upload-marker/../file.txt",
    "private-upload-marker\x00.txt", "", ".",
])
async def test_upload_names_are_relative_and_fail_before_reading_any_body(file_case, invalid_path):
    case = file_case
    command = await intent(case)
    first, invalid = io.BytesIO(b"first"), io.BytesIO(b"invalid")
    with pytest.raises(ValueError, match="上传文件相对路径无效") as failure:
        await case.service.upload_file_entries(
            directory_path="target", scope="workspace", intent=command, actor="local",
            entries=[("first.txt", first), (invalid_path, invalid)],
        )
    assert "private-upload-marker" not in str(failure.value)
    assert first.tell() == invalid.tell() == 0
    assert not case.owner._incoming.exists()
    assert case.owner.store.pending() == ()
    with pytest.raises(KeyError):
        case.owner.store.get(command.client_operation_id, "local")
    assert list((case.workspace / "target").iterdir()) == []


async def test_duplicate_upload_paths_are_rejected_before_body_or_staging(file_case):
    case = file_case
    first, duplicate = io.BytesIO(b"first"), io.BytesIO(b"duplicate")
    with pytest.raises(FileExistsError, match="上传内容包含重复相对路径"):
        await case.service.upload_file_entries(
            directory_path="target", scope="workspace", intent=await intent(case), actor="local",
            entries=[("目录/文件.txt", first), (r"目录\文件.txt", duplicate)],
        )
    assert first.tell() == duplicate.tell() == 0
    assert not case.owner._incoming.exists()
    assert list((case.workspace / "target").iterdir()) == []


@pytest.mark.parametrize("name", ["/private-name-marker/file.txt", r"C:\private-name-marker\file.txt",
                                 r"\\private-name-marker\share", " private-name-marker ", "private-name-marker\x00.txt"])
async def test_invalid_entry_names_do_not_echo_native_input(file_case, name):
    case = file_case
    command = await intent(case)
    with pytest.raises(ValueError) as failure:
        await case.owner.enqueue(kind="create", directory_path="target", scope="workspace",
                                 intent=command, actor="local", data={"name": name, "kind": "file"})
    assert "private-name-marker" not in str(failure.value)
    assert case.owner.store.pending() == ()
    with pytest.raises(KeyError):
        case.owner.store.get(command.client_operation_id, "local")
    assert list((case.workspace / "target").iterdir()) == []


async def test_directory_revision_pins_cursor_and_ignores_unrelated_change(file_case):
    case = file_case
    for name in ("a", "b", "c"):
        (case.workspace / "target" / name).touch()
    first = await case.service.list_files(path="target", limit=1)
    assert (await case.service.list_files(path="target")).directory_revision == first.directory_revision
    (case.workspace / "other/unrelated").touch()
    assert (await case.service.list_files(path="target")).directory_revision == first.directory_revision
    (case.workspace / "target/new").touch()
    with pytest.raises(WorkspaceFileOperationConflictError, match="游标"):
        await case.service.list_files(path="target", limit=1, cursor=first.next_cursor)


async def test_external_source_and_target_conflicts_do_not_overwrite(file_case, monkeypatch):
    case = file_case
    release = Event()
    original = case.owner._execute
    def blocked(record):
        if not release.wait(10):
            raise RuntimeError("测试gate未释放")
        original(record)
    monkeypatch.setattr(case.owner, "_execute", blocked)
    source = case.workspace / "other/source.txt"
    source.write_text("before", encoding="utf-8")
    accepted = await case.service.copy_file_entry(directory_path="target", scope="workspace", source_path="other/source.txt",
                                                source_scope="workspace", intent=await intent(case), actor="local")
    source.write_text("changed", encoding="utf-8")
    (case.workspace / "target/source.txt").write_text("外部目标", encoding="utf-8")
    release.set()
    result = await terminal(case, accepted)
    assert result.state == "rejected" and result.effect == "none"
    assert (case.workspace / "target/source.txt").read_text(encoding="utf-8") == "外部目标"


async def test_created_by_dependency_uses_original_result_and_fails_without_it(file_case, monkeypatch):
    case = file_case
    first = await case.service.create_file_entry(directory_path="target", scope="workspace", name="created", kind="directory",
                                               intent=await intent(case), actor="local")
    successor = WorkspaceFileMutationIntentDTO(client_operation_id="op_" + uuid4().hex, client_sequence=2,
                                               directory_key=case.owner.store.get(first.operation_id, "local").payload["directory_key"] | {"path": case.service.file_paths.reference(case.workspace / "target/created")},
                                               depends_on=[first.operation_id], created_by_operation_id=first.operation_id,
                                               directory_revision_from_operation_id=first.operation_id)
    second = await case.service.create_file_entry(directory_path="", scope="workspace", name="child.txt", kind="file", intent=successor, actor="local")
    assert (await terminal(case, first)).state == "committed"
    assert (await terminal(case, second)).state == "committed"
    assert (case.workspace / "target/created/child.txt").is_file()


async def test_partial_publish_retains_each_effect_and_independent_intent_progresses(file_case, monkeypatch):
    case = file_case
    original = operations_module.publish_step
    calls = 0
    def conflict_after_first(stage, target, kind):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_text("外部新增", encoding="utf-8")
        original(stage, target, kind)
    monkeypatch.setattr(operations_module, "publish_step", conflict_after_first)
    receipt = await case.service.upload_file_entries(directory_path="target", scope="workspace", intent=await intent(case), actor="local",
                                                    entries=[("a.txt", io.BytesIO(b"a")), ("b.txt", io.BytesIO(b"b"))])
    result = await terminal(case, receipt)
    assert result.state == "rejected" and result.effect == "partial"
    assert [item.state for item in result.items] == ["published", "conflict"]
    assert (case.workspace / "target/a.txt").read_bytes() == b"a"
    assert (case.workspace / "target/b.txt").read_text(encoding="utf-8") == "外部新增"
    independent = await case.service.create_file_entry(directory_path="other", scope="workspace", name="independent", kind="file",
                                                     intent=await intent(case, "other"), actor="local")
    assert (await terminal(case, independent)).state == "committed"


async def test_frozen_payload_damage_is_explicit_not_rebuilt(file_case):
    case = file_case
    command = await intent(case)
    result = await case.service.create_file_entry(directory_path="target", scope="workspace", name="frozen", kind="file", intent=command, actor="local")
    await terminal(case, result)
    with case.owner.store.transaction() as db:
        payload = case.owner.store.get(result.operation_id, "local").payload
        payload["target"] = str(case.workspace / "other")
        db.execute("UPDATE operations SET payload=? WHERE operation_id=?", (json.dumps(payload), result.operation_id))
    with pytest.raises(RuntimeError, match="冻结内容损坏"):
        case.owner.status([result.operation_id], actor="local")


async def test_list_observation_cannot_publish_an_old_scan_after_worker_publication(file_case, monkeypatch):
    case = file_case
    ready, publish_allowed, scanned, scan_allowed, waiting, published = (Event() for _ in range(6))
    original_check, original_scan = case.owner._check_preconditions, case.service._scan_directory
    original_publish, original_lock = operations_module.publish_step, case.owner.observation_lock
    checks = 0
    scan_once = True

    def check(record):
        nonlocal checks
        original_check(record)
        checks += 1
        if checks == 2:
            ready.set()
            if not publish_allowed.wait(10):
                raise RuntimeError("测试发布屏障未释放")

    def scan(*args):
        nonlocal scan_once
        value = original_scan(*args)
        if ready.is_set() and scan_once:
            scan_once = False
            scanned.set()
            if not scan_allowed.wait(10):
                raise RuntimeError("测试旧扫描屏障未释放")
        return value

    def publish(*args):
        published.set()
        return original_publish(*args)

    baseline = await case.service.list_files(path="target")
    monkeypatch.setattr(case.owner, "_check_preconditions", check)
    monkeypatch.setattr(case.service, "_scan_directory", scan)
    # 只观察原 RLock 的竞争，不替代生产锁或文件事实。
    class LockObserver:
        def __enter__(self):
            if scanned.is_set() and not scan_allowed.is_set():
                waiting.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    monkeypatch.setattr(case.owner, "observation_lock", LockObserver())
    monkeypatch.setattr(operations_module, "publish_step", publish)
    accepted = await case.service.create_file_entry(directory_path="target", scope="workspace",
        name="race.txt", kind="file", intent=await intent(case), actor="local")
    listing = None
    try:
        assert await asyncio.to_thread(ready.wait, 5), "worker 必须先完成 staging"
        listing = asyncio.create_task(case.service.list_files(path="target"))
        assert await asyncio.to_thread(scanned.wait, 5), "列表必须先捕获旧目录"
        publish_allowed.set()
        assert await asyncio.to_thread(waiting.wait, 5), "发布必须等待同一观察锁"
        assert not published.is_set(), "旧扫描尚未提交时不得发布文件副作用"
        scan_allowed.set()
        old = await listing
        result = await terminal(case, accepted)
        current = await case.service.list_files(path="target")
        committed = next(value for value in result.directories if value.directory_key == current.directory_key)
        assert old.items == [] and old.directory_revision == baseline.directory_revision
        assert [item.name for item in current.items] == ["race.txt"]
        assert current.directory_revision == committed.directory_revision > old.directory_revision
    finally:
        publish_allowed.set()
        scan_allowed.set()
        if listing is not None:
            await listing


async def test_created_directory_replaced_by_same_named_external_directory_is_rejected(file_case, monkeypatch):
    case = file_case
    creator = await case.service.create_file_entry(directory_path="target", scope="workspace",
        name="created", kind="directory", intent=await intent(case), actor="local")
    creator_result = await terminal(case, creator)
    key = next(value.directory_key for value in creator_result.directories if value.directory_key.path == case.service.file_paths.reference(case.workspace / "target/created"))
    entered, release = Event(), Event()
    original = case.owner._execute

    def blocked(record):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("测试后继屏障未释放")
        original(record)

    monkeypatch.setattr(case.owner, "_execute", blocked)
    child = WorkspaceFileMutationIntentDTO(client_operation_id="op_" + uuid4().hex, client_sequence=2,
        directory_key=key, depends_on=[creator.operation_id], created_by_operation_id=creator.operation_id,
        directory_revision_from_operation_id=creator.operation_id)
    accepted = await case.service.create_file_entry(directory_path="", scope="workspace",
        name="child.txt", kind="file", intent=child, actor="local")
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        original_directory = case.workspace / "other/original-created"
        (case.workspace / "target/created").rename(original_directory)
        (case.workspace / "target/created").mkdir()
        release.set()
        result = await terminal(case, accepted)
        assert result.state == "rejected" and result.effect == "none"
        assert "被替换" in result.error_detail
        assert list((case.workspace / "target/created").iterdir()) == []
        assert list(original_directory.iterdir()) == []
    finally:
        release.set()


@pytest.mark.parametrize("creator_kind", ["copy", "upload", "upload_many"])
async def test_copy_and_upload_directory_output_owns_nested_successor(file_case, creator_kind):
    case = file_case
    if creator_kind == "copy":
        source = case.workspace / "other/copy-tree/nested"
        source.mkdir(parents=True)
        (source / "seed.bin").write_bytes(b"copy")
        creator = await case.service.copy_file_entry(directory_path="target", scope="workspace",
            source_path="other/copy-tree", source_scope="workspace", intent=await intent(case), actor="local")
        path, relative = "target/copy-tree/nested", "nested"
    else:
        entries = [("one/nested/seed.bin", io.BytesIO(b"upload"))]
        if creator_kind == "upload_many":
            entries.append(("two/seed.bin", io.BytesIO(b"second")))
        creator = await case.service.upload_file_entries(directory_path="target", scope="workspace",
            intent=await intent(case), actor="local", entries=entries)
        path, relative = "target/one/nested", "one/nested" if creator_kind == "upload_many" else "nested"
    creator_result = await terminal(case, creator)
    key = next(value.directory_key for value in creator_result.directories if value.directory_key.path == case.service.file_paths.reference(case.workspace / path))
    successor = WorkspaceFileMutationIntentDTO(client_operation_id="op_" + uuid4().hex, client_sequence=2,
        directory_key=key, depends_on=[creator.operation_id], created_by_operation_id=creator.operation_id,
        directory_revision_from_operation_id=creator.operation_id, relative_child_path=relative)
    accepted = await case.service.create_file_entry(directory_path="", scope="workspace",
        name="successor.txt", kind="file", intent=successor, actor="local")
    assert (await terminal(case, accepted)).state == "committed"
    assert (case.workspace / path / "successor.txt").is_file()
    assert (case.workspace / path / "seed.bin").read_bytes() == (b"copy" if creator_kind == "copy" else b"upload")


async def test_failed_creator_terminates_dependency_and_keeps_independent_intent(file_case, monkeypatch):
    case = file_case
    entered, release = Event(), Event()
    original = case.owner._execute

    def blocked(record):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("测试受理屏障未释放")
        original(record)

    monkeypatch.setattr(case.owner, "_execute", blocked)
    creator = await case.service.create_file_entry(directory_path="target", scope="workspace",
        name="created", kind="directory", intent=await intent(case), actor="local")
    key = case.owner.directory_key("target/created", "workspace")[0]
    successor = WorkspaceFileMutationIntentDTO(client_operation_id="op_" + uuid4().hex, client_sequence=2,
        directory_key=key, depends_on=[creator.operation_id], created_by_operation_id=creator.operation_id,
        directory_revision_from_operation_id=creator.operation_id)
    child = await case.service.create_file_entry(directory_path="", scope="workspace",
        name="child.txt", kind="file", intent=successor, actor="local")
    independent = await case.service.create_file_entry(directory_path="other", scope="workspace",
        name="independent.txt", kind="file", intent=await intent(case, "other"), actor="local")
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        (case.workspace / "target/created").mkdir()
        (case.workspace / "target/created/external.txt").write_text("外部目录", encoding="utf-8")
        release.set()
        assert (await terminal(case, creator)).state == "rejected"
        result = await terminal(case, child)
        assert result.state == "dependency_failed" and result.effect == "none"
        assert (await terminal(case, independent)).state == "committed"
        assert not (case.workspace / "target/created/child.txt").exists()
        assert (case.workspace / "target/created/external.txt").read_text(encoding="utf-8") == "外部目录"
    finally:
        release.set()


@pytest.mark.parametrize("column,value,detail", [
    ("steps", '[{"state":"published"}]', "发布证明损坏"),
    ("token", -1, "执行 token 损坏"),
])
async def test_damaged_steps_or_token_fail_without_rebuilding(file_case, column, value, detail):
    case = file_case
    accepted = await case.service.create_file_entry(directory_path="target", scope="workspace",
        name="proof.txt", kind="file", intent=await intent(case), actor="local")
    await terminal(case, accepted)
    async with asyncio.timeout(5):
        while case.owner.store.cleanup_pending():
            await asyncio.sleep(0.01)
    with case.owner.store.transaction() as db:
        db.execute(f"UPDATE operations SET {column}=? WHERE operation_id=?", (value, accepted.operation_id))
    with pytest.raises(RuntimeError, match=detail):
        case.owner.status([accepted.operation_id], actor="local")
    assert (case.workspace / "target/proof.txt").is_file()


async def test_stale_token_cannot_claim_publish_or_settle_original_operation(file_case, monkeypatch):
    case = file_case
    entered, release = Event(), Event()
    original = case.owner._execute

    def blocked(record):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("测试 token 屏障未释放")
        original(case.owner.store.get(record.operation_id, record.actor))

    monkeypatch.setattr(case.owner, "_execute", blocked)
    accepted = await case.service.create_file_entry(directory_path="target", scope="workspace",
        name="cas.txt", kind="file", intent=await intent(case), actor="local")
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        queued = case.owner.store.get(accepted.operation_id, "local")
        first = case.owner.store.claim(queued)
        current = case.owner.store.claim(first)
        with pytest.raises(RuntimeError, match="token CAS"):
            case.owner.store.claim(queued)
        with pytest.raises(RuntimeError, match="owner/token"):
            case.owner.store.save_steps(first, [])
        with pytest.raises(RuntimeError, match="owner/token"):
            case.owner.store.settle(first, state="committed", effect="none", items=[], observations=[], snapshots=[])
        assert case.owner.store.get(accepted.operation_id, "local") == current
        release.set()
        assert (await terminal(case, accepted)).state == "committed"
        with pytest.raises(RuntimeError, match="owner/token"):
            case.owner.store.complete_cleanup(first)
        assert (case.workspace / "target/cas.txt").is_file()
    finally:
        release.set()


async def test_unverified_effect_stops_worker_without_cleanup_or_successor_execution(file_case, monkeypatch):
    case = file_case
    entered, release = Event(), Event()
    original, execute = operations_module.publish_step, case.owner._execute

    def blocked(record):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("测试未知效果屏障未释放")
        execute(record)

    def lost_proof(stage, target, kind):
        original(stage, target, kind)
        target.unlink()
        target.write_bytes(b"external replacement")
        raise OSError("原发布后文件被外部替换")

    monkeypatch.setattr(operations_module, "publish_step", lost_proof)
    monkeypatch.setattr(case.owner, "_execute", blocked)
    accepted = await case.service.upload_file_entries(directory_path="target", scope="workspace",
        intent=await intent(case), actor="local", entries=[("unknown.bin", io.BytesIO(b"original"))])
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        successor = await case.service.create_file_entry(directory_path="other", scope="workspace",
            name="never.txt", kind="file", intent=await intent(case, "other"), actor="local")
        release.set()
        async with asyncio.timeout(5):
            while case.owner.worker_state != "failed":
                await asyncio.sleep(0.01)
        frozen = case.owner.store.get(accepted.operation_id, "local")
        result = frozen.receipt
        assert result.state == "running" and result.effect == "unverified"
        assert result.error_code == "reconcile_required" and result.items[0].state == "unverified"
        assert frozen.token > 0 and case.owner._incoming_path(frozen).is_dir()
        assert all(case.owner._stage_root(frozen, index).is_dir() for index in range(len(frozen.steps)))
        assert case.owner.store.cleanup_pending() == ()
        assert not any(value.get("operation_id") == accepted.operation_id for value in case.owner.store.events(0))
        assert case.owner.store.get(successor.operation_id, "local").state == "queued"
        assert not (case.workspace / "other/never.txt").exists()
        assert (case.workspace / "target/unknown.bin").read_bytes() == b"external replacement"
        with pytest.raises(RuntimeError, match="worker 不可用"):
            case.owner.require_ready()
        with pytest.raises(FileEffectUnverifiedError, match="原效果待核实"):
            await case.service.stop()
    finally:
        release.set()
