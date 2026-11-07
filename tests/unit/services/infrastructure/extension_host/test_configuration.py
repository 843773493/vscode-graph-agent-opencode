"""经真实宿主 owner 初始化配置文件；只替换进程边界，保留磁盘与异步 watcher。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationProfileViewRequest,
    ExtensionConfigurationTargetRequest,
    ExtensionConfigurationViewRequest,
    ExtensionConfigurationWriteOptions,
)
from app.schemas.gateway.extension_host import ExtensionHostWorkspaceDTO
from app.services.infrastructure.extension_host import configuration
from app.services.infrastructure.extension_host import owner as owner_module
from app.services.infrastructure.extension_host.configuration import (
    ExtensionConfigurationFiles,
    native_path,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementClient,
    ExtensionManagementError,
    NativeFileStat,
)
from app.services.infrastructure.extension_host.owner import (
    NodeExtensionHostOwner,
    NodeHostConnection,
)
from tests.unit.services.infrastructure.extension_host.test_owner import Case
from tests.unit.services.infrastructure.extension_host.test_owner import (
    case as case,  # noqa: PLC0414
)

GATEWAY = "gateway_" + "a" * 32


@pytest.fixture
def write_options() -> ExtensionConfigurationWriteOptions:
    return ExtensionConfigurationWriteOptions(create=True, overwrite=True, unlock=False, atomic=False, append=False)


@pytest.fixture
async def sibling_owner(case: Case) -> AsyncIterator[NodeExtensionHostOwner]:
    backend_id = str(uuid4())
    management = ExtensionManagementClient(backend_id=backend_id, gateway_url="http://127.0.0.1:8014",
        token="test-token", operation_timeout=2)
    owner = NodeExtensionHostOwner(backend_id=backend_id, mounted_workspaces=case.owner._mounts,
        runtime=case.owner.runtime, home=case.owner._home / "sibling", environment={}, startup_timeout=2, management=management)
    try:
        yield owner
    finally:
        await owner.close()


@pytest.fixture
def concurrent_publication(case: Case, monkeypatch: pytest.MonkeyPatch) -> bytes:
    roots = [case.owner._mounts.resolve(workspace_id).workspace_root.as_uri() for workspace_id in case.workspace_ids]
    edited = ("/* 首个发布者的后续编辑必须保留 */\n" + json.dumps({"folders": [{"uri": root} for root in roots],
        "settings": {"probe.saved": True}}) + "\n").encode()
    barrier, mutex = threading.Barrier(2), threading.Lock()
    original = configuration.rename_entry_exclusive

    def publish(source: Path, target: Path) -> None:
        barrier.wait(timeout=5)
        with mutex:
            original(source, target)
            target.write_bytes(edited)

    monkeypatch.setattr(configuration, "rename_entry_exclusive", publish)
    return edited


@pytest.fixture
def blocked_write(case: Case, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[threading.Event, threading.Event, list[bytes]]]:
    started, release = threading.Event(), threading.Event()
    calls: list[bytes] = []
    original = configuration._write_bytes

    def write(path: Path, content: bytes, options: ExtensionConfigurationWriteOptions) -> NativeFileStat:
        assert path.parent == case.profile
        calls.append(content)
        started.set()
        if not release.wait(5):
            raise TimeoutError("测试未释放原配置写 worker")
        return original(path, content, options)

    monkeypatch.setattr(configuration, "_write_bytes", write)
    try:
        yield started, release, calls
    finally:
        release.set()


@pytest.fixture
def blocked_initialization(case: Case, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[threading.Event, threading.Event]]:
    started, release = threading.Event(), threading.Event()
    original = owner_module.initialize_configuration_files

    def initialize(settings_uri: str, workspace_home_uri: str, workspace: ExtensionHostWorkspaceDTO) -> str | None:
        assert native_path(settings_uri).parent == case.profile
        started.set()
        if not release.wait(5):
            raise TimeoutError("测试未释放原配置初始化 worker")
        return original(settings_uri, workspace_home_uri, workspace)

    monkeypatch.setattr(owner_module, "initialize_configuration_files", initialize)
    try:
        yield started, release
    finally:
        release.set()


async def _files(owner: NodeExtensionHostOwner, connection: NodeHostConnection) -> ExtensionConfigurationFiles:
    identity = connection.identity
    return await owner.configuration_files(ExtensionConfigurationViewRequest(protocol_version=1,
        gateway_id=identity.gateway_id, backend_id=identity.backend_id, workspace_ids=list(identity.workspace_ids),
        view_id=identity.view_id, generation=identity.generation), client_id=identity.client_id)


async def test_stable_view_reuses_jsonc_bytes_with_relative_roots(case: Case, write_options: ExtensionConfigurationWriteOptions) -> None:
    async with case.owner.connect(gateway_id=GATEWAY, expected_backend_id=case.owner.backend_id,
            client_id="first", view_id="persist", workspace_ids=case.workspace_ids) as first:
        resource = first.initialization.workspace.configuration
        assert resource is not None
        path = native_path(resource.native_uri)
        folders = [{"path": os.path.relpath(native_path(folder.native_uri), path.parent), "name": "显示名"}
                   for folder in first.initialization.workspace.folders]
        content = ("\ufeff/* 保留块注释 */\n{\n// 保留行注释\n\"folders\": " + json.dumps(folders)
            + ',\n"settings": {"probe.saved": "字节不变", "probe.large": 1e400, "probe.escape": "\\ud800",},\n}\n').encode()
        files = await _files(case.owner, first)
        await files.write(resource.resource_uri, b64encode(content).decode(), write_options)
        assert b64decode((await files.read(resource.resource_uri)).content_base64) == content
    async with case.owner.connect(gateway_id=GATEWAY, expected_backend_id=case.owner.backend_id,
            client_id="reopened", view_id="persist", workspace_ids=case.workspace_ids) as second:
        assert second.initialization.workspace.configuration == resource
        assert second.identity.generation != first.identity.generation
        assert path.read_bytes() == content


@pytest.mark.parametrize("damage", ["reordered", "malformed-uri", "hash-comment", "single-quotes", "invalid-json", "invalid-utf8", "nan", "unclosed-comment"])
async def test_existing_view_damage_never_rewrites_the_file(case: Case, damage: str) -> None:
    async with case.owner.connect(gateway_id=GATEWAY, expected_backend_id=case.owner.backend_id,
            client_id="first", view_id="damaged", workspace_ids=case.workspace_ids) as first:
        resource = first.initialization.workspace.configuration
        assert resource is not None
        path = native_path(resource.native_uri)
        value = json.loads(path.read_bytes())
        valid = json.dumps(value)
    value["folders"].reverse()
    body = {"reordered": json.dumps(value).encode(), "hash-comment": ("# invalid\n" + valid).encode(),
            "malformed-uri": b'{"folders": [{"uri": "file://[invalid"}]}',
            "single-quotes": valid.replace('"', "'").encode(), "invalid-json": b"{", "invalid-utf8": b"\xff",
            "nan": (valid[:-1] + ', "settings": {"probe": NaN}}').encode(),
            "unclosed-comment": (valid + "/*").encode()}[damage]
    path.write_bytes(body)
    with pytest.raises(ExtensionManagementError) as rejected:
        async with case.owner.connect(gateway_id=GATEWAY, expected_backend_id=case.owner.backend_id,
                client_id="second", view_id="damaged", workspace_ids=case.workspace_ids):
            pytest.fail("损坏文件不能发布宿主")
    assert rejected.value.code == "Unknown"
    assert path.read_bytes() == body and len(case.hosts) == 1 and not case.owner.active


async def test_concurrent_first_view_publication_never_overwrites_a_completed_edit(case: Case, concurrent_publication: bytes) -> None:
    async with AsyncExitStack() as stack:
        connections = await asyncio.gather(*(stack.enter_async_context(case.owner.connect(gateway_id=GATEWAY,
            expected_backend_id=case.owner.backend_id, client_id=client, view_id="same", workspace_ids=case.workspace_ids))
            for client in ("one", "two")))
        first, second = (connection.initialization.workspace.configuration for connection in connections)
        assert first is not None and second == first
        path = native_path(first.native_uri)
        assert path.read_bytes() == concurrent_publication
        assert list(path.parent.iterdir()) == [path]


async def test_profile_files_are_shared_across_backend_owners(case: Case, sibling_owner: NodeExtensionHostOwner,
                                                            write_options: ExtensionConfigurationWriteOptions) -> None:
    async with AsyncExitStack() as stack:
        connections = [await stack.enter_async_context(owner.connect(gateway_id=GATEWAY,
            expected_backend_id=owner.backend_id, client_id="client", view_id="single", workspace_ids=case.workspace_ids[:1]))
            for owner in (case.owner, sibling_owner)]
        resources = [connection.initialization.environment.remote_settings for connection in connections]
        assert resources[0].native_uri == resources[1].native_uri
        assert resources[0].resource_uri != resources[1].resource_uri
        files = [await _files(owner, connection) for owner, connection in zip((case.owner, sibling_owner), connections, strict=True)]
        content = b'{"probe.shared": true}\n'
        first = await files[0].write(resources[0].resource_uri, b64encode(content).decode(), write_options)
        assert b64decode((await files[1].read(resources[1].resource_uri)).content_base64) == content
        second = await files[1].stat(resources[1].resource_uri)
        assert (first.ctime, first.mtime, first.size) == (second.ctime, second.mtime, second.size)


async def test_file_write_flags_missing_resources_and_permissions(case: Case, write_options: ExtensionConfigurationWriteOptions) -> None:
    async with case.owner.connect(gateway_id=GATEWAY, expected_backend_id=case.owner.backend_id,
            client_id="flags", view_id="single", workspace_ids=case.workspace_ids[:1]) as connection:
        resource = connection.initialization.environment.remote_settings
        files, path = await _files(case.owner, connection), native_path(resource.native_uri)
        for operation in (files.stat, files.read):
            with pytest.raises(ExtensionManagementError) as caught:
                await operation(resource.resource_uri)
            assert caught.value.code == "EntryNotFound" and not path.exists()
        for overwrite in (False, True):
            with pytest.raises(ExtensionManagementError) as caught:
                await files.write(resource.resource_uri, "eA==", write_options.model_copy(update={"create": False, "overwrite": overwrite}))
            assert caught.value.code == "EntryNotFound" and not path.exists()
        created = await files.write(resource.resource_uri, "eA==", write_options.model_copy(update={"overwrite": False}))
        for create in (False, True):
            with pytest.raises(ExtensionManagementError) as caught:
                await files.write(resource.resource_uri, "eQ==", write_options.model_copy(update={"create": create, "overwrite": False}))
            assert caught.value.code == "EntryExists" and path.read_bytes() == b"x"
        await files.write(resource.resource_uri, "eQ==", write_options.model_copy(update={"create": False, "append": True}))
        assert path.read_bytes() == b"xy"
        os.utime(path, ns=(1_700_000_000_123_750_000, 1_700_000_000_123_750_000))
        assert (await files.stat(resource.resource_uri)).mtime == 1_700_000_000_124
        path.chmod(0o555)
        assert (await files.stat(resource.resource_uri)).permissions == 6
        saved = await files.write(resource.resource_uri, "eg==", write_options.model_copy(update={"unlock": True}))
        assert saved.permissions == 4 and saved.ctime == created.ctime and path.read_bytes() == b"z"
        with pytest.raises(ExtensionManagementError, match="原子写入"):
            await files.write(resource.resource_uri, "eA==", write_options.model_copy(update={"atomic": True}))
        assert path.read_bytes() == b"z"


@pytest.mark.parametrize("cancel", [False, True], ids=["normal-worker", "cancelled-worker"])
async def test_owner_close_settles_original_write_and_rejects_queued_operations(case: Case,
    write_options: ExtensionConfigurationWriteOptions, blocked_write: tuple[threading.Event, threading.Event, list[bytes]], cancel: bool) -> None:
    started, release, calls = blocked_write
    async with case.owner.connect(gateway_id=GATEWAY, expected_backend_id=case.owner.backend_id,
            client_id="write", view_id="single", workspace_ids=case.workspace_ids[:1]) as connection:
        files = await _files(case.owner, connection)
        resource = connection.initialization.environment.remote_settings
        writing = asyncio.create_task(files.write(resource.resource_uri, "eA==", write_options))
        pending: list[asyncio.Task[object]] = []
        try:
            assert await asyncio.to_thread(started.wait, 3)
            queued = asyncio.create_task(files.write(resource.resource_uri, "eQ==", write_options))
            closing = asyncio.create_task(case.owner.close())
            pending.extend((queued, closing))
            await asyncio.wait_for(connection.stopping.wait(), 3)
            if cancel:
                writing.cancel()
                await asyncio.sleep(0)
            assert not writing.done() and not closing.done() and not queued.done()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await writing
            else:
                await writing
            with pytest.raises(ValueError, match="已停止"):
                await queued
            await closing
            assert calls == [b"x"] and native_path(resource.native_uri).read_bytes() == b"x"
            with pytest.raises(ValueError, match="已停止"):
                await files.read(resource.resource_uri)
            assert case.owner.drained
        finally:
            release.set()
            await asyncio.gather(writing, *pending, return_exceptions=True)


async def test_owner_close_waits_for_initial_view_publication(case: Case,
    blocked_initialization: tuple[threading.Event, threading.Event]) -> None:
    started, release = blocked_initialization

    async def open_host() -> None:
        async with case.owner.connect(gateway_id=GATEWAY, expected_backend_id=case.owner.backend_id,
                client_id="initializing", view_id="multi", workspace_ids=case.workspace_ids):
            pytest.fail("关闭期间不能发布宿主")

    opening = asyncio.create_task(open_host())
    closing: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 3)
        closing = asyncio.create_task(case.owner.close())
        session = next(iter(case.owner._sessions.values()))
        await asyncio.wait_for(session.stopping.wait(), 3)
        assert not closing.done() and not opening.done() and not case.hosts
        release.set()
        with pytest.raises(RuntimeError, match="撤销准入"):
            await opening
        await closing
        assert len(tuple(case.views.glob("*/workspace.code-workspace"))) == 1
        assert case.owner.drained and not case.hosts
    finally:
        release.set()
        await asyncio.gather(*(task for task in (opening, closing) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("ending", ["stop", "cancel"])
async def test_configuration_watchers_filter_share_and_release(case: Case, write_options: ExtensionConfigurationWriteOptions, ending: str) -> None:
    async with case.owner.connect(gateway_id=GATEWAY, expected_backend_id=case.owner.backend_id,
            client_id="watch", view_id="single", workspace_ids=case.workspace_ids[:1]) as connection:
        files = await _files(case.owner, connection)
        resource = connection.initialization.environment.remote_settings
        first, second = files.events(), files.events()
        try:
            ready = await asyncio.wait_for(asyncio.gather(anext(first), anext(second)), 3)
            assert all(event.overflow and not event.changes for event in ready)
            watchers = case.owner._configuration_watch._watchers
            assert len(watchers) == 1
            watcher = next(iter(watchers.values()))
            assert len(watcher.subscribers) == 2
            (case.profile / "unrelated.json").write_text("{}")
            await files.write(resource.resource_uri, "eA==", write_options)
            events = await asyncio.wait_for(asyncio.gather(anext(first), anext(second)), 3)
            assert all([change.resource_uri for change in event.changes] == [resource.resource_uri] for event in events)
            await first.aclose()
            assert len(watcher.subscribers) == 1 and not watcher.task.done()
            pending = asyncio.create_task(anext(second))
            await asyncio.sleep(0)
            if ending == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                await case.owner.close()
                with pytest.raises(StopAsyncIteration):
                    await pending
            assert not watchers and watcher.task.done()
        finally:
            await first.aclose()
            await second.aclose()


async def test_standalone_prepare_reuses_environment_and_never_recreates_deleted_view(case: Case,
    write_options: ExtensionConfigurationWriteOptions, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ExtensionManagementClient.environment
    calls = []

    async def environment(management: ExtensionManagementClient):
        calls.append(management.backend_id)
        return await original(management)

    monkeypatch.setattr(ExtensionManagementClient, "environment", environment)
    case.owner.runtime = None
    target = ExtensionConfigurationTargetRequest(protocol_version=1, gateway_id=GATEWAY,
        backend_id=case.owner.backend_id, workspace_ids=list(case.workspace_ids), view_id="settings")
    view = await case.owner.prepare_configuration(target, client_id="settings-client")
    assert view.context.workspace.configuration is not None
    resource = view.context.workspace.configuration
    path = native_path(resource.native_uri)
    content = path.read_bytes().replace(b'"folders"', b'/* user comment */ "folders"')
    path.write_bytes(content)
    assert await case.owner.prepare_configuration(target, client_id="another-client") == view
    assert path.read_bytes() == content
    request = ExtensionConfigurationProfileViewRequest(**target.model_dump(), profile_id=view.profile_id)
    files = await case.owner.configuration_files(request, client_id="settings-client")
    assert b64decode((await files.read(resource.resource_uri)).content_base64) == content
    path.unlink()
    for operation in (files.stat, files.read):
        with pytest.raises(ExtensionManagementError) as failure:
            await operation(resource.resource_uri)
        assert failure.value.code == "EntryNotFound" and not path.exists()
    other = await case.owner.configuration_files(request, client_id="another-client")
    assert not path.exists()
    await other.write(view.context.remote_settings.resource_uri, "eA==", write_options)
    assert calls == [case.owner.backend_id] and not case.hosts and case.owner.active == ()


async def test_standalone_prepare_shutdown_waits_for_original_initialization_worker(case: Case,
    blocked_initialization: tuple[threading.Event, threading.Event],
) -> None:
    started, release = blocked_initialization
    target = ExtensionConfigurationTargetRequest(protocol_version=1, gateway_id=GATEWAY,
        backend_id=case.owner.backend_id, workspace_ids=list(case.workspace_ids), view_id="settings")
    preparing = asyncio.create_task(case.owner.prepare_configuration(target, client_id="settings-client"))
    closing = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        closing = asyncio.create_task(case.owner.close())
        await asyncio.sleep(0)
        assert case.owner._admission_closed and not closing.done()
        release.set()
        with pytest.raises(ValueError, match="正在关闭"):
            await preparing
        await closing
        assert case.owner.drained and case.owner.active == ()
    finally:
        release.set()
        await asyncio.gather(preparing, *([closing] if closing else []), return_exceptions=True)


async def test_independent_settings_clients_share_native_watcher_without_sharing_session(case: Case,
    write_options: ExtensionConfigurationWriteOptions,
) -> None:
    target = ExtensionConfigurationTargetRequest(protocol_version=1, gateway_id=GATEWAY,
        backend_id=case.owner.backend_id, workspace_ids=list(case.workspace_ids[:1]), view_id="settings")
    view = await case.owner.prepare_configuration(target, client_id="first")
    request = ExtensionConfigurationProfileViewRequest(**target.model_dump(), profile_id=view.profile_id)
    first_files = await case.owner.configuration_files(request, client_id="first")
    second_files = await case.owner.configuration_files(request, client_id="second")
    assert first_files is not second_files and case.owner.active == ()
    first, second = first_files.events(), second_files.events()
    try:
        ready = await asyncio.wait_for(asyncio.gather(anext(first), anext(second)), 3)
        assert all(event.overflow for event in ready)
        watchers = case.owner._configuration_watch._watchers
        assert len(watchers) == 1
        watcher = next(iter(watchers.values()))
        assert len(watcher.subscribers) == 2
        (case.profile / "unrelated.json").write_text("{}")
        uri = view.context.remote_settings.resource_uri
        await first_files.write(uri, "eA==", write_options)
        events = await asyncio.wait_for(asyncio.gather(anext(first), anext(second)), 3)
        assert all([change.resource_uri for change in event.changes] == [uri] for event in events)
        await first.aclose()
        assert len(watcher.subscribers) == 1 and not watcher.task.done()
        await second.aclose()
        assert watcher.task.done() and not watchers
    finally:
        await first.aclose()
        await second.aclose()
