"""真实消息入口验证独立目录、canonical 接受和冷读，不调用模型。"""

from __future__ import annotations

import asyncio
import base64
import os
from contextlib import closing
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
from google.protobuf.json_format import ParseDict
from PIL import Image

from app.container import AppContainer
from app.core.exceptions import ForbiddenError
from app.core.hashing import sha256_hex
from app.core.resource_uri import (
    file_uri_child,
    parse_attachment_uri,
    parse_resource_uri,
    resource_display_uri,
    thread_resource_uri,
)
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.protocol.generated.boxteam.workspace.v2.message_pb2 import (
    AttachmentRef as ProtocolAttachmentRef,
)
from app.protocol.generated.boxteam.workspace.v2.public_pb2 import (
    TurnAttachmentDTO as ProtocolTurnAttachment,
)
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.schemas.internal_v2.session import (
    SessionCreateRequest,
    SessionForkResourceChoice,
)
from app.schemas.internal_v2.text_mutation import (
    CreateText,
    EditText,
    PatchText,
    TextMutationRequest,
)
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceFileMutationIntentDTO,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.workspace_files.text_publication import (
    WorkspaceFileConflictError,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    SourceForkOwnerTransport,
    _fork_request,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    remote_fork_target as remote_fork_target,  # noqa: PLC0414
)
from tests.support.message_service import accept_queued_message


def upload(data: bytes, *, mime: str = "text/plain") -> AttachmentRef:
    return AttachmentRef(file_id="inline:input", name="输入文件", content_type=mime,
                         data_url="data:" + mime + ";base64," + base64.b64encode(data).decode())


async def test_message_uploads_are_independent_and_canonical_acceptance_protects_them(startup_container: AppContainer) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="独立目录上传"))
    body = b"independent original bytes"
    first = await container.message_service.create(session.session_id, MessageCreateRequest(content="第一个", attachments=[upload(body), upload(body)]))
    second = await container.message_service.create(session.session_id, MessageCreateRequest(content="第二个", attachments=[upload(body)]))
    refs = [*first.attachments, *second.attachments]
    assert len({ref.file_id for ref in refs}) == len({ref.directory_uri for ref in refs}) == 3
    for ref in refs:
        parsed = parse_attachment_uri(ref.file_id, require_file=True)
        assert parsed.kind == "attachments" and parsed.scope_id == container.workspace_id
        assert ref.file_id == file_uri_child(ref.directory_uri, "original", "content")
        assert container.attachment_blob_store.read(session.session_id, ref.file_id).data == body
    accept_queued_message(container.message_service, first)
    accept_queued_message(container.message_service, second)
    history = container.checkpointer.load_history(session.session_id, first.thread_id, TurnHistoryLoadRequest(direction="tail"))
    persisted = {user.message_id: user for turn in history.items for user in turn.user_messages}
    assert set(persisted) == {first.message_id, second.message_id}
    for ref in first.attachments:
        wire = ref.model_dump(mode="json", exclude_none=True)
        assert ParseDict(wire, ProtocolAttachmentRef()).directory_uri == ref.directory_uri
    for message in (first, second):
        restored = persisted[message.message_id].attachments
        assert [(ref.file_id, ref.directory_uri) for ref in restored] == [(ref.file_id, ref.directory_uri) for ref in message.attachments]
        for ref in restored:
            assert ParseDict(ref.model_dump(mode="json", exclude_none=True), ProtocolTurnAttachment()).directory_uri == ref.directory_uri
    assert container.attachment_blob_store.recover_session(session.session_id, messages=container.message_service._checkpointer) == ()
    with closing(AttachmentBlobStore(container.workspace_root, path_resolver=container.session_service.path_resolver)) as cold:
        for ref in refs:
            assert cold.read(session.session_id, ref.file_id).data == body


@pytest.mark.parametrize("size", [(900, 600), (120, 80)])
async def test_preview_generation_uses_same_directory_and_preserves_original(startup_container: AppContainer, size: tuple[int, int]) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="目录图片预览"))
    source = BytesIO()
    Image.new("RGB", size, (12, 24, 36)).save(source, format="PNG")
    original = source.getvalue()
    message = await container.message_service.create(session.session_id, MessageCreateRequest(content="图片", attachments=[upload(original, mime="image/png")]))
    ref, = message.attachments
    preview = container.attachment_blob_store.read_thumbnail(session.session_id, ref.file_id)
    assert preview.content_type == "image/webp"
    with Image.open(BytesIO(preview.data)) as image:
        assert max(image.size) == min(512, max(size))
    assert container.attachment_blob_store.read_thumbnail(session.session_id, ref.file_id).data == preview.data
    assert container.attachment_blob_store.read(session.session_id, ref.file_id).data == original
    assert container.attachment_blob_store.read(session.session_id, file_uri_child(ref.directory_uri, "preview", "512.webp")).data == preview.data
    preview_uri = file_uri_child(ref.directory_uri, "preview", "512.webp")
    for uri, expected_mime, expected_data in ((ref.file_id, "image/png", original), (preview_uri, "image/webp", preview.data)):
        result = await container.resource_navigation.navigate(ResourceNavigationRequest(uri=uri, operation="read", read_format="auto"),
            local_gateway_id="gateway_6c8b2f9317a5a2c6c4569958b1e23d84")
        assert result.media_type == "image" and result.mime_type == expected_mime
        assert base64.b64decode(result.content) == expected_data
    accept_queued_message(container.message_service, message)


async def test_upload_retry_keeps_identity_and_conflict_does_not_change_original(startup_container: AppContainer) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="上传幂等"))
    body = b"frozen upload input"
    original = upload(body)
    message = await container.message_service.create(session.session_id, MessageCreateRequest(content="同一个消息", attachments=[original]))
    store = container.attachment_blob_store
    retried = await store.persist_inline(session.session_id, [original], thread_id=message.thread_id, message_id=message.message_id)
    assert retried == message.attachments
    before = tuple(tuple(row) for row in store._catalog.connection.execute("SELECT * FROM attachment_directory_ingests"))
    with pytest.raises(RuntimeError):
        await store.persist_inline(session.session_id, [upload(b"different input")], thread_id=message.thread_id, message_id=message.message_id)
    assert tuple(tuple(row) for row in store._catalog.connection.execute("SELECT * FROM attachment_directory_ingests")) == before
    assert store.read(session.session_id, message.attachments[0].file_id).data == body
    accept_queued_message(container.message_service, message)
    assert await store.persist_inline(session.session_id, [original], thread_id=message.thread_id, message_id=message.message_id) == message.attachments


@pytest.mark.parametrize("accepted", [False, True])
async def test_recovery_distinguishes_unused_upload_and_durable_queued_input(startup_container: AppContainer, accepted: bool) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="目录上传恢复"))
    message = await container.message_service.create(session.session_id, MessageCreateRequest(content="排队附件", attachments=[upload(b"queued input")]))
    if accepted:
        accept_queued_message(container.message_service, message)
    store = container.attachment_blob_store
    await store.recover_idle_session(session.session_id, messages=container.message_service._checkpointer)
    ref, = message.attachments
    identity = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    association = store._catalog.connection.execute("SELECT state FROM attachment_directory_associations WHERE attachment_id=?", (identity,)).fetchone()
    assert association[0] == ("active" if accepted else "released")
    record = store._catalog.get_directory(identity)
    assert record.availability == "available"
    assert (record.unattached_since is None) == accepted
    assert (record.gc_after is None) == accepted
    assert await store.collect_garbage(leases=container.external_resource_leases) == ()
    assert store.read(session.session_id, ref.file_id).data == b"queued input"
    assert await store.recover_idle_session(session.session_id, messages=container.message_service._checkpointer) == ()


@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_local_fork_retains_directory_without_rewriting_canonical_history(
    startup_container: AppContainer, fork_gateway_request_context: None,
    monkeypatch: pytest.MonkeyPatch, creation_kind: str,
) -> None:
    from unittest.mock import AsyncMock

    container = startup_container
    # 只替换未使用的外部 PTY/Browser 清单传输；fork 与附件 owner 均使用真实实现。
    for owner in (container.terminal_resource_owner, container.browser_resource_owner):
        monkeypatch.setattr(owner._transport, "list_resources", AsyncMock(return_value=[]))
    session = await container.session_service.create(SessionCreateRequest(title="目录 fork 来源"))
    message = await container.message_service.create(session.session_id, MessageCreateRequest(content="带目录的历史输入", attachments=[upload(b"fork retained directory")]))
    accept_queued_message(container.message_service, message)
    context = _production_fork_context(container)
    before = container.checkpointer._storage.storage.read_items(session.session_id, message.thread_id)
    request = await _fork_request(context, session.session_id, mode="full_rollout_copy", creation_kind=creation_kind)
    result = await context.forks.fork(request)
    ref, = message.attachments
    assert len(result.resources) == 1
    assert result.resources[0].resource_uri == ref.file_id
    assert result.resources[0].availability == "available"
    assert result.resources[0].target_uri == ref.file_id
    assert container.attachment_blob_store.read(result.session.session_id, ref.file_id, owner_thread_id=result.thread_id).data == b"fork retained directory"
    after = container.checkpointer._storage.storage.read_items(result.session.session_id, result.thread_id)
    assert [item.to_dict() for item in after] == [item.to_dict() for item in before]
    identity = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    associations = container.attachment_blob_store._catalog.connection.execute(
        "SELECT session_id,thread_id,state FROM attachment_directory_associations WHERE attachment_id=? ORDER BY session_id,thread_id", (identity,),
    ).fetchall()
    assert sorted(tuple(row) for row in associations) == sorted([
        (session.session_id, message.thread_id, "active"), (result.session.session_id, result.thread_id, "active"),
    ])


@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_remote_fork_copies_frozen_directory_after_live_source_is_removed(
    production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    monkeypatch: pytest.MonkeyPatch, creation_kind: str,
) -> None:
    from unittest.mock import AsyncMock

    from app.services.infrastructure.attachment_blob_catalog.locator import (
        resolve_attachment_directory,
    )
    from app.services.infrastructure.rollout_context.fork.source_capture import (
        capture_directory,
    )

    source = production_fork_container
    target, transport = remote_fork_target
    for container in (source, target):
        for owner in (container.terminal_resource_owner, container.browser_resource_owner):
            monkeypatch.setattr(owner._transport, "list_resources", AsyncMock(return_value=[]))
    source_token = set_current_gateway_id("gateway_4aae160f813ed87bcf65a8bb8bfd3729")
    try:
        session = await source.session_service.create(SessionCreateRequest(title="跨工作区目录来源"))
        message = await source.message_service.create(session.session_id, MessageCreateRequest(
            content="冻结后移离原件", attachments=[upload(b"captured directory original")],
        ))
        accept_queued_message(source.message_service, message)
        request = await _fork_request(_production_fork_context(source), session.session_id,
                                      creation_kind=creation_kind, mode="full_rollout_copy")
    finally:
        reset_current_gateway_id(source_token)
    ref, = message.attachments
    identity = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    store = source.attachment_blob_store
    directory = store._catalog.get_directory(identity)
    live = resolve_attachment_directory(store._attachments_root, directory.relative_locator)
    offline = live.with_name(live.name + ".offline-after-capture")
    before = source.checkpointer._storage.storage.read_items(session.session_id, message.thread_id)
    transfer = transport.fork_transfer

    async def transfer_and_remove_live(payload):
        reply = await transfer(payload)
        if payload.action == "capture":
            live.rename(offline)
        return reply

    monkeypatch.setattr(transport, "fork_transfer", transfer_and_remove_live)
    target_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    try:
        parent = await target.session_service.create(SessionCreateRequest(title="跨工作区目标容器"))
        request = request.model_copy(update={
            "source_uri": thread_resource_uri(
                source.workspace_id, session.session_id, message.thread_id,
                gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
            ),
            "target_container_uri": resource_display_uri(
                scope="workspace", scope_id=target.workspace_id, kind="session",
                tail_segments=(parent.session_id,) if creation_kind == "new_child_thread" else (),
                gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
            ),
            "resources": [SessionForkResourceChoice(resource_uri=ref.file_id, action="copy_snapshot")],
        })
        result = await target.session_context_fork_service.fork(request)
        assert offline.exists() and not live.exists()
        assert result.export_release.state == "confirmed"
        resource, = result.resources
        assert (resource.resource_uri, resource.action, resource.availability) == (ref.file_id, "copy_snapshot", "available")
        parsed = parse_attachment_uri(resource.target_uri, require_file=True)
        assert parsed.scope_id == target.workspace_id and parsed.attachment_id != identity
        assert target.attachment_blob_store.read(result.session.session_id, resource.target_uri, owner_thread_id=result.thread_id).data == b"captured directory original"
        assert target.attachment_blob_store.read(result.session.session_id, ref.file_id, owner_thread_id=result.thread_id).data == b"captured directory original"
        with pytest.raises(ValueError, match="固定目标工作区"):
            target.attachment_blob_store.read(parent.session_id, ref.file_id, owner_thread_id=await target.session_service.resolve_main_thread(parent.session_id))
        target_root = target.attachment_blob_store.resolve_directory_uri(parse_attachment_uri(
            resource.target_uri, local_gateway_id="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ))
        if os.name == "posix":
            assert target_root.stat().st_mode & 0o200 == 0
            assert target_root.parent.stat().st_mode & 0o200 == 0
        with closing(AttachmentBlobStore(target.workspace_root, path_resolver=target.session_service.path_resolver)) as cold:
            assert cold.read(result.session.session_id, ref.file_id, owner_thread_id=result.thread_id).data == b"captured directory original"
        after = target.checkpointer._storage.storage.read_items(result.session.session_id, result.thread_id)
        assert [item.to_dict() for item in after] == [item.to_dict() for item in before]
        operation = target.session_context_fork_service._creation_key(request.idempotency_key)
        for container in (source, target):
            capture = container.session_service.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=operation)
            assert capture.state == "materialized"
            assert not capture_directory(container.attachment_blob_store._sessions_root, capture).exists()
        assert [call.action for call in transport.calls].count("capture") == 1
        assert [call.action for call in transport.calls].count("release") == 1
        calls = len(transport.calls)
        replay = await target.session_context_fork_service.fork(request)
        assert (replay.session.session_id, replay.thread_id) == (result.session.session_id, result.thread_id)
        assert len(transport.calls) == calls
    finally:
        reset_current_gateway_id(target_token)
        if offline.exists():
            offline.rename(live)


@pytest.fixture
async def directory_file_case(startup_container: AppContainer):
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="普通文件 owner 访问附件"))
    message = await container.message_service.create(session.session_id, MessageCreateRequest(
        content="保留原件并生成处理结果", attachments=[upload("原件 Ω\n".encode())],
    ))
    accept_queued_message(container.message_service, message)
    await container.workspace_service.start()
    try:
        yield container, session, message.attachments[0]
    finally:
        await container.workspace_service.stop()


async def test_directory_file_owner_navigation_mutation_and_revision_share_original_chain(directory_file_case) -> None:
    container, session, ref = directory_file_case
    service, navigation = container.workspace_service, container.resource_navigation
    gateway = "gateway_6c8b2f9317a5a2c6c4569958b1e23d84"
    root = parse_resource_uri(ref.directory_uri, local_gateway_id=gateway).display_uri
    derived = file_uri_child(root, "derived")
    result_uri = file_uri_child(derived, "处理结果.txt")
    listing = await service.list_files(path=root)
    assert {node.name for node in listing.items} == {"original", "preview", "derived"}
    assert listing.root_path == root and listing.directory_key.root == root
    assert {node.uri for node in (await navigation.navigate(ResourceNavigationRequest(
        uri=root, operation="list"), local_gateway_id=gateway)).entries} == {node.path for node in listing.items}
    original = await service.get_file_content(path=ref.file_id)
    assert original.content == "原件 Ω\n"
    with pytest.raises(PermissionError, match="只读"):
        await service.update_file_content(path=ref.file_id, content="不得覆盖", expected_revision=original.revision)
    created = container.text_mutations.execute(CreateText(target=result_uri, content="first Ω\n", expected_version="absent"))
    assert created["status"] == "success" and created["files"][0]["path"] == result_uri
    before = await service.get_file_content(path=result_uri)
    saved = await service.update_file_content(path=result_uri, content="second Ω\n", expected_revision=before.revision)
    with pytest.raises(WorkspaceFileConflictError):
        await service.update_file_content(path=result_uri, content="过时写入", expected_revision=before.revision)
    edited = await container.text_mutations.execute_async(EditText(target=result_uri, old_string="second", new_string="third",
        expected_version="sha256:" + saved.revision))
    assert edited["status"] == "success"
    version = "sha256:" + sha256_hex("third Ω\n".encode())
    patched = container.text_mutations.execute(PatchText(target=root,
        input="*** Begin Patch\n*** Update File: derived/处理结果.txt\n@@\n-third Ω\n+final Ω\n*** End Patch",
        expected_versions={"derived/处理结果.txt": version}, explanation="只改处理输出"))
    assert patched["status"] == "success"
    assert TextMutationRequest.from_command(CreateText(target=result_uri, content="x", expected_version="absent")).command.target == result_uri
    for sync in (False, True):
        request = ResourceNavigationRequest(uri=result_uri, operation="read")
        result = navigation.navigate_sync(request, local_gateway_id=gateway) if sync else await navigation.navigate(request, local_gateway_id=gateway)
        assert result.content == "final Ω\n" and result.content_hash == "sha256:" + sha256_hex("final Ω\n".encode())
    search = await navigation.navigate(ResourceNavigationRequest(uri=root, operation="search", search_mode="content", query="final"), local_gateway_id=gateway)
    assert [(match.uri, match.line) for match in search.matches] == [(result_uri, 1)]
    archive = service.prepare_file_download(path=root)
    try:
        with ZipFile(archive.path) as files:
            assert files.read(root.rsplit("/", 1)[-1] + "/derived/处理结果.txt") == "final Ω\n".encode()
    finally:
        archive.path.unlink()
    assert container.attachment_blob_store.read(session.session_id, ref.file_id).data == "原件 Ω\n".encode()
    assert str(container.workspace_root) not in listing.model_dump_json() + saved.model_dump_json() + str(patched)


async def test_directory_file_operations_copy_both_directions_and_reject_original_targets(directory_file_case) -> None:
    container, session, ref = directory_file_case
    service = container.workspace_service
    root = parse_resource_uri(ref.directory_uri).display_uri
    derived = file_uri_child(root, "derived")

    async def command(path: str) -> WorkspaceFileMutationIntentDTO:
        listing = await service.list_files(path=path)
        return WorkspaceFileMutationIntentDTO(client_operation_id="op_" + uuid4().hex, client_sequence=1,
            directory_key=listing.directory_key, expected_directory_revision=listing.directory_revision)

    async def completed(receipt):
        async with asyncio.timeout(5):
            while True:
                current = service.file_operations.status([receipt.operation_id], actor="local").operations[0]
                if current.state != "queued" and current.state != "running":
                    assert current.state == "committed", current
                    return current
                service.file_operations.require_ready()
                await asyncio.sleep(0.01)

    created = await service.create_file_entry(directory_path=derived, scope="workspace", name="outputs", kind="directory",
        intent=await command(derived), actor="local")
    await completed(created)
    outputs = file_uri_child(derived, "outputs")
    uploaded = await service.upload_file_entries(directory_path=outputs, scope="workspace", entries=[("a.txt", BytesIO(b"A")), ("b.txt", BytesIO(b"B"))],
        intent=await command(outputs), actor="local")
    await completed(uploaded)
    # 使用不同正文核对多成员上传和复制的来源不混用。
    assert (await service.get_file_content(path=file_uri_child(outputs, "a.txt"))).content == "A"
    assert (await service.get_file_content(path=file_uri_child(outputs, "b.txt"))).content == "B"
    copied = await service.copy_file_entry(directory_path=".", scope="workspace", source_path=outputs, source_scope="workspace",
        intent=await command("."), actor="local")
    await completed(copied)
    assert (container.workspace_root / "outputs/a.txt").read_bytes() == b"A"
    assert (container.workspace_root / "outputs/b.txt").read_bytes() == b"B"
    pasted = await service.paste_file_entries(directory_path=derived, scope="workspace", source_paths=["outputs/a.txt", "outputs/b.txt"],
        intent=await command(derived), actor="local")
    await completed(pasted)
    assert (await service.get_file_content(path=file_uri_child(derived, "a.txt"))).content == "A"
    assert (await service.get_file_content(path=file_uri_child(derived, "b.txt"))).content == "B"
    original = file_uri_child(root, "original")
    with pytest.raises(PermissionError, match="只读"):
        await service.create_file_entry(directory_path=original, scope="workspace", name="must-not-exist", kind="file",
            intent=await command(original), actor="local")
    assert container.attachment_blob_store.read(session.session_id, ref.file_id).data == "原件 Ω\n".encode()


async def test_directory_file_access_does_not_open_native_private_paths_or_follow_aliases(directory_file_case) -> None:
    container, _, ref = directory_file_case
    service = container.workspace_service
    root = parse_resource_uri(ref.directory_uri).display_uri
    native = service.file_paths.native(root)
    files_root = service.file_paths.workspace_uri
    for target in (str(native), file_uri_child(files_root, *native.relative_to(container.workspace_root).parts)):
        with pytest.raises(ForbiddenError):
            await service.list_files(path=target)
    with pytest.raises(PermissionError):
        container.text_mutations.execute(CreateText(target=file_uri_child(root, "preview", "bad.txt"), content="x", expected_version="absent"))
    derived = file_uri_child(root, "derived")
    private = native / "derived/.boxteam"
    private.mkdir()
    (private / "secret.txt").write_text("private")
    assert not (await service.list_files(path=derived)).items
    with pytest.raises(ForbiddenError):
        await service.get_file_content(path=file_uri_child(derived, ".boxteam", "secret.txt"))
    outside = container.workspace_root / "outside.txt"
    outside.write_text("outside")
    (native / "derived/link.txt").symlink_to(outside)
    try:
        with pytest.raises(RuntimeError, match="符号链接"):
            await service.get_file_content(path=file_uri_child(derived, "link.txt"))
    finally:
        (native / "derived/link.txt").unlink()
    with pytest.raises(ValueError, match="固定工作区"):
        await service.list_files(path=root.replace(container.workspace_id, str(uuid4())))
    with pytest.raises(ValueError, match="本机固定工作区"):
        await service.list_files(path=resource_display_uri(scope="workspace", scope_id=container.workspace_id, kind="attachments",
            tail_segments=parse_resource_uri(root).tail_segments, gateway_authority="gateway_0123456789abcdef0123456789abcdef"))
