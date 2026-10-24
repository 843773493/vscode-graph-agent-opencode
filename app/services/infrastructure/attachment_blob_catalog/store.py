"""附件目录上传、读取与回收的唯一链路；每次独立上传拥有独立可变目录。"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote_to_bytes

from PIL import Image, ImageOps

from app.abstractions.resource_file_content import ResourceFileContent
from app.core.atomic_fs import fsync_directory
from app.core.file_permissions import restrict_private_path_access
from app.core.path_utils import get_session_path_resolver
from app.core.phase_observer import observe_phase, observe_phase_sync
from app.core.resource_uri import (
    ParsedResourceUri,
    file_uri_child,
    parse_resource_uri,
    resource_display_uri,
)
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    OperationHolderGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
)
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    CATALOG_DATABASE_NAME,
    AttachmentBlobCatalog,
    AttachmentDirectoryRetainedError,
)
from app.services.infrastructure.attachment_blob_catalog.directories import (
    protect_original,
    remove_directory,
    verify_content,
)
from app.services.infrastructure.attachment_blob_catalog.fork_store import (
    ForkAttachmentStoreMixin,
)
from app.services.infrastructure.attachment_blob_catalog.legacy_references import (
    resolve_legacy_attachment,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
    resolve_attachment_file,
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentFileVersion,
    DirectoryIngestRecord,
)
from app.services.infrastructure.attachment_blob_catalog.recovery import (
    AttachmentIngestRecoveryMixin,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.workspace_files.mutation_lock import (
    workspace_file_mutation_lock,
)

ATTACHMENT_ROOT_DIRECTORY_NAME = "attachments"
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024


class AttachmentBlobStore(ForkAttachmentStoreMixin, AttachmentIngestRecoveryMixin):
    """原附件服务的目录实现；不再写入或读取旧 blob 表。"""

    def __init__(self, workspace_root: Path, *, path_resolver: SessionCatalogPathResolver | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._workspace_root = workspace_root.expanduser().resolve()
        self.workspace_id = read_workspace_id(self._workspace_root)
        self._clock = clock
        self._sessions_root = self._workspace_root / ".boxteam/sessions"
        self._attachments_root = self._workspace_root / ".boxteam/attachments"
        self._catalog = AttachmentBlobCatalog(self._attachments_root / CATALOG_DATABASE_NAME, workspace_id=self.workspace_id)
        self._path_resolver = path_resolver or get_session_path_resolver(self._sessions_root)
        self._gate = SessionLifecycleGate(self._sessions_root)
        self._topology_gate = NavigationTopologyGate(self._sessions_root)
        self.deletion_holder = OperationHolderGate(self._sessions_root, namespace="attachment_ingest")
        self._fork_imports = OperationHolderGate(self._sessions_root, namespace="attachment_fork_import")
        self._directory_operations = OperationHolderGate(self._sessions_root, namespace="attachment_directory")

    def close(self) -> None:
        self._catalog.close()

    def directory_uri(self, attachment_id: str) -> str:
        return resource_display_uri(scope="workspace", scope_id=self.workspace_id, kind="attachments", tail_segments=(attachment_id,), gateway_authority=get_current_gateway_id())

    def reference(self, attachment_id: str) -> str:
        return file_uri_child(self.directory_uri(attachment_id), "original", "content")

    def reference_for_path(self, target: Path) -> str | None:
        """仅按路径中的稳定 ID 核 catalog；不扫描日期桶或吸收未知目录。"""
        if not target.is_relative_to(self._attachments_root):
            return None
        parts = target.relative_to(self._attachments_root).parts
        if len(parts) < 4:
            return None
        record = self._catalog.get_directory(parts[3])
        if record is None or tuple(record.relative_locator.split("/")) != parts[:4]:
            return None
        directory = resolve_attachment_directory(self._attachments_root, record.relative_locator)
        resolve_attachment_file(directory, parts[4:])
        return file_uri_child(self.directory_uri(record.attachment_id), *parts[4:])

    def _resource_id(self, file_id: str, *, session_id: str | None, thread_id: str | None = None, canonical: bool = False) -> tuple[str, tuple[str, ...]]:
        parsed = parse_resource_uri(file_id, local_gateway_id=parse_resource_uri(file_id).gateway_authority if canonical else get_current_gateway_id())
        if session_id is not None and thread_id is not None:
            snapshot = self._catalog.fork_attachment_snapshot_for_alias(
                target_session_id=session_id, target_thread_id=thread_id, source_file_id=file_id,
            )
            if snapshot is not None:
                target = parse_resource_uri(snapshot.target_file_id, local_gateway_id=get_current_gateway_id())
                if target.scope != "workspace" or target.scope_id != self.workspace_id or (target.gateway_authority is not None and not canonical):
                    raise ValueError("附件目录 alias 不属于本工作区 owner")
                claim = self._catalog.connection.execute(
                    "SELECT state FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,),
                ).fetchone()
                if snapshot.availability != "available" or claim is None or claim[0] != "committed":
                    raise FileNotFoundError("历史附件没有已发布的目标目录")
                children = parsed.tail_segments[1:] if parsed.kind == "attachments" else ("original", "content")
                return snapshot.target_attachment_id, children
        if parsed.kind == "attachments":
            if parsed.scope != "workspace" or parsed.scope_id != self.workspace_id or (parsed.gateway_authority is not None and not canonical):
                raise ValueError("附件目录 URI 不属于固定目标工作区")
            return parsed.tail_segments[0], parsed.tail_segments[1:]
        if parsed.session_artifact_kind != "attachments":
            raise ValueError("附件引用必须指向目录文件或已迁移的旧历史附件")
        if session_id is None:
            raise ValueError("旧 Session 附件解析必须提供真实消费 Session")
        if thread_id is None:
            # 无消费 Thread 的原附件 HTTP 入口只能解析其精确本地旧 owner。
            owner = self._catalog.connection.execute("SELECT source_thread_id FROM attachment_legacy_bindings WHERE attachment_id=? AND source_session_id=?", (parsed.tail_segments[2], session_id)).fetchone()
            if owner is None:
                raise FileNotFoundError("旧附件引用没有精确消费 owner")
            thread_id = owner[0]
        reference = resolve_legacy_attachment(self._catalog.connection, session_id=session_id, thread_id=thread_id, file_id=file_id, workspace_id=self.workspace_id, gateway_id=parse_resource_uri(file_id).gateway_authority if canonical else get_current_gateway_id())
        if reference is None:
            raise FileNotFoundError("旧附件引用没有迁入的目录来源绑定")
        return reference.attachment_id, ("original", "content")

    def _parse_canonical_attachment_id(self, session_id: str, file_id: str, *, thread_id: str | None = None) -> str:
        return self._resource_id(file_id, session_id=session_id, thread_id=thread_id, canonical=True)[0]

    def _parse_logical_file_id(self, session_id: str, file_id: str) -> str:
        return self._resource_id(file_id, session_id=session_id)[0]

    async def persist_inline(self, session_id: str, attachments: Sequence[AttachmentRef], *, thread_id: str, message_id: str) -> list[AttachmentRef]:
        if not message_id or ":" in message_id:
            raise ValueError("附件上传必须绑定非空且不含冒号的 message_id")
        self._catalog.acquire_access()
        persisted = []
        async with self.deletion_holder.lock_for(session_id, exclusive=False):
            for ordinal, attachment in enumerate(attachments):
                if attachment.data_url:
                    attachment = await self._persist_one(session_id, thread_id, message_id, ordinal, attachment)
                else:
                    await self._pin_reference(session_id, thread_id, message_id, ordinal, attachment)
                persisted.append(attachment)
            if persisted:
                # 普通读取按工作区授权；消息上传完成仍须核对真实消费 Thread 的准入。
                async with self._topology_gate.shared(), self._gate.exclusive(session_id):
                    self._assert_session_active(session_id, thread_id)
        return persisted

    async def _pin_reference(self, session_id: str, thread_id: str, message_id: str, ordinal: int, attachment: AttachmentRef) -> None:
        """既有 URI 输入也先取得原消息 pin，覆盖读正文到 canonical 接受的窗口。"""
        if not attachment.file_id.startswith("boxteam://"):
            return
        parsed = parse_resource_uri(attachment.file_id, local_gateway_id=get_current_gateway_id())
        if parsed.kind != "attachments" and parsed.session_artifact_kind != "attachments":
            return
        attachment_id, children = self._resource_id(attachment.file_id, session_id=session_id, thread_id=thread_id)
        if not children:
            raise IsADirectoryError("附件输入必须指向目录内的文件")
        operation_id = "attref_" + hashlib.sha256(f"{session_id}|{thread_id}|{message_id}|{ordinal}".encode()).hexdigest()
        preimage = hashlib.sha256(canonical_json_bytes([session_id, thread_id, message_id, operation_id, attachment_id, attachment.file_id])).hexdigest()
        control = self._open_control_store(session_id)
        try:
            async with self._directory_operations.lock_for(attachment_id), self._topology_gate.shared(), self._gate.exclusive(session_id):
                generation = self._assert_session_active(session_id, thread_id)
                self._catalog.require_available_directory(attachment_id)
                pin = control.create_or_get_lease(operation_kind="attachment", operation_identity=operation_id,
                    preimage_hash=preimage, expected_generation=generation,
                    recovery_ref=f"attachment-ref:{session_id}:{thread_id}:{message_id}:{attachment_id}")
                if pin.state == "active":
                    self._catalog.attach_directory(attachment_id, session_id=session_id, thread_id=thread_id,
                        binding_token=f"reference:{operation_id}", now=self._clock())
                elif pin.state != "completed":
                    raise RuntimeError("附件引用 pin 已终止，不能重开原操作")
        finally:
            control.close()

    async def _persist_one(self, session_id: str, thread_id: str, message_id: str, ordinal: int, attachment: AttachmentRef) -> AttachmentRef:
        mime_type, data = _parse_data_url(attachment.data_url or "")
        if attachment.content_type is not None and attachment.content_type != mime_type:
            raise ValueError("附件 content_type 与 data_url MIME 不一致")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"附件超过 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB 限制")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        # 操作按明确消息及序号幂等；字节相同的不同消息/序号仍是独立上传。
        operation_id = "attkey_" + hashlib.sha256(f"{session_id}|{thread_id}|{message_id}|{ordinal}".encode()).hexdigest()
        preimage = hashlib.sha256(canonical_json_bytes([session_id, thread_id, message_id, operation_id, digest, len(data), attachment.name, mime_type])).hexdigest()
        control = self._open_control_store(session_id)
        try:
            async with self._topology_gate.shared(), self._gate.exclusive(session_id):
                generation = self._assert_session_active(session_id, thread_id)
                pin = control.create_or_get_lease(operation_kind="attachment", operation_identity=operation_id, preimage_hash=preimage, expected_generation=generation, recovery_ref=f"attachment:{session_id}:{thread_id}:{message_id}")
            details = {"session_id": session_id, "thread_id": thread_id, "message_id": message_id, "lease_id": pin.lease_id}
            await observe_phase(operation_id, "attachment.ingest.pin_acquired", details=details)
            record = self._catalog.create_ingest(operation_id=operation_id, ingest_id="ing_" + hashlib.sha256(operation_id.encode()).hexdigest()[:32], session_id=session_id, thread_id=thread_id, message_id=message_id, pin=pin, digest=digest, length=len(data), file_name=attachment.name, mime_type=mime_type, now=self._clock())
            if pin.state != "active":
                if pin.state != "completed" or record.state != "published":
                    raise RuntimeError("附件上传 pin 已终止，不能重开原操作")
                self._catalog.require_available_directory(record.attachment_id)
                verify_content(resolve_attachment_file(resolve_attachment_directory(self._attachments_root, record.relative_locator), ("original", "content")), record.digest, record.length, record.attachment_id)
                return AttachmentRef(file_id=self.reference(record.attachment_id), directory_uri=self.directory_uri(record.attachment_id), name=record.file_name, content_type=record.mime_type)
            details["attachment_id"] = record.attachment_id
            await observe_phase(operation_id, "attachment.ingest.preparing", details=details)
            try:
                async with self._directory_operations.lock_for(record.attachment_id):
                    await run_sync_shielded(self._write_directory, record, data)
                    self._catalog.mark_ingest_durable(operation_id, now=self._clock())
                    await observe_phase(operation_id, "attachment.ingest.directory_durable", details=details)
                async with self._topology_gate.shared(), self._gate.exclusive(session_id):
                    if not control.verify_lease_token(lease_id=pin.lease_id, fencing_token=pin.fencing_token) or self._assert_session_active(session_id, thread_id) != pin.captured_lifecycle_generation:
                        raise SessionDeletionPendingError("附件 pin 或 Session 代次已失效，拒绝发布")
                    record = self._catalog.publish_ingest(operation_id, now=self._clock())
                await observe_phase(operation_id, "attachment.ingest.published", details=details)
            except BaseException:
                if self._catalog.get_ingest_record(operation_id).state == "published":
                    raise
                self._catalog.abort_ingest_record(ingest_idempotency_key=operation_id, reason="附件写入或发布失败", now=self._clock())
                # writer 已退出才清理本 intent 的目录，不能碰其它上传的正文。
                remove_directory(resolve_staging_path(self._attachments_root, record.staging_relative_locator))
                remove_directory(resolve_attachment_directory(self._attachments_root, record.relative_locator))
                if control.verify_lease_token(lease_id=pin.lease_id, fencing_token=pin.fencing_token):
                    self._settle_pin(control, pin, outcome="failed")
                raise
            return AttachmentRef(file_id=self.reference(record.attachment_id), directory_uri=self.directory_uri(record.attachment_id), name=record.file_name, content_type=record.mime_type)
        finally:
            control.close()

    def _write_directory(self, record: DirectoryIngestRecord, data: bytes) -> None:
        destination = resolve_attachment_directory(self._attachments_root, record.relative_locator)
        if destination.exists():
            verify_content(resolve_attachment_file(destination, ("original", "content")), record.digest, record.length, record.attachment_id)
            return
        staging = resolve_staging_path(self._attachments_root, record.staging_relative_locator)
        if staging.exists():
            raise RuntimeError("上传 staging 已存在；须先沿原恢复入口核实，不能覆盖")
        staging.mkdir(parents=True, mode=0o700)
        restrict_private_path_access(staging, is_directory=True)
        for name in ("original", "preview", "derived"):
            child = staging / name
            child.mkdir(mode=0o700)
            restrict_private_path_access(child, is_directory=True)
        original = staging / "original/content"
        with original.open("xb") as stream:
            restrict_private_path_access(original, descriptor=stream.fileno())
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        protect_original(staging)
        for name in ("original", "preview", "derived"):
            fsync_directory(staging / name)
        fsync_directory(staging)
        details = {"session_id": record.owner_session_id, "thread_id": record.owner_thread_id,
                   "message_id": record.message_id, "attachment_id": record.attachment_id, "lease_id": record.pin_lease_id}
        observe_phase_sync(record.ingest_idempotency_key, "attachment.ingest.directory_staged", details=details)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, destination)
        fsync_directory(destination.parent)
        fsync_directory(staging.parent)
        observe_phase_sync(record.ingest_idempotency_key, "attachment.ingest.directory_renamed", details=details)
        verify_content(resolve_attachment_file(destination, ("original", "content")), record.digest, record.length, record.attachment_id)

    def resolve_directory_uri(self, parsed: ParsedResourceUri, *, for_write: bool = False) -> Path:
        if parsed.kind != "attachments" or parsed.scope != "workspace" or parsed.scope_id != self.workspace_id or parsed.gateway_authority is not None:
            raise ValueError("附件目录 URI 不属于本机固定工作区")
        record = self._catalog.require_available_directory(parsed.tail_segments[0])
        children = parsed.tail_segments[1:]
        if for_write and (not children or children[0] != "derived"):
            raise PermissionError("附件原件和预览只读，处理结果必须写入 derived 目录")
        root = resolve_attachment_directory(self._attachments_root, record.relative_locator)
        if not root.is_dir():
            raise FileNotFoundError("附件目录的物理正文缺失")
        return resolve_attachment_file(root, children)

    def read(self, session_id: str | None, file_id: str, *, owner_thread_id: str | None = None) -> ResourceFileContent:
        with workspace_file_mutation_lock(self._workspace_root):
            attachment_id, children = self._resource_id(file_id, session_id=session_id, thread_id=owner_thread_id)
            record = self._catalog.require_available_directory(attachment_id)
            path = resolve_attachment_file(resolve_attachment_directory(self._attachments_root, record.relative_locator), children)
            if not path.is_file():
                raise FileNotFoundError(f"附件文件不存在：{file_id}")
            try:
                data = path.read_bytes()
            except OSError as error:
                raise OSError(error.errno, error.strerror, file_id) from error
            version = self._catalog.file_version(attachment_id, "/".join(children))
            if children and children[0] in {"original", "preview"}:
                if version is None:
                    raise RuntimeError("附件原件或预览缺少已登记版本")
                verify_content(data, version.digest, version.length, attachment_id)
            mime_type = version.mime_type if version is not None else mimetypes.guess_type(path.name)[0]
            return ResourceFileContent(data, mime_type or "application/octet-stream")

    def describe_reference(self, attachment: AttachmentRef, *, session_id: str | None) -> AttachmentRef:
        attachment_id, children = self._resource_id(attachment.file_id, session_id=session_id)
        self._catalog.require_available_directory(attachment_id)
        if not children:
            raise IsADirectoryError("附件输入必须明确指向目录内的文件")
        directory_uri = self.directory_uri(attachment_id)
        if attachment.directory_uri is not None and parse_resource_uri(attachment.directory_uri, local_gateway_id=get_current_gateway_id()).display_uri != parse_resource_uri(directory_uri, local_gateway_id=get_current_gateway_id()).display_uri:
            raise ValueError("附件 directory_uri 与原件身份不一致")
        version = self._catalog.file_version(attachment_id, "/".join(children))
        return attachment.model_copy(update={
            "directory_uri": directory_uri,
            "name": attachment.name or (version.file_name if version is not None else children[-1]),
            "content_type": attachment.content_type or self.read(session_id, attachment.file_id).content_type,
        })

    def read_thumbnail(self, session_id: str | None, file_id: str, *, max_edge: int = 512) -> ResourceFileContent:
        if not 64 <= max_edge <= 1024:
            raise ValueError("图片缩略图 max_edge 必须在 64 到 1024 之间")
        attachment_id, _ = self._resource_id(file_id, session_id=session_id)
        uri = file_uri_child(self.directory_uri(attachment_id), "preview", "512.webp")
        if max_edge == 512 and self._catalog.file_version(attachment_id, "preview/512.webp") is not None:
            return self.read(session_id, uri)
        source = self.read(session_id, file_id)
        if not source.content_type.startswith("image/"):
            raise ValueError(f"附件不是图片，无法生成缩略图：{file_id}")
        try:
            with Image.open(BytesIO(source.data)) as image:
                normalized = ImageOps.exif_transpose(image)
                normalized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                if normalized.mode not in {"RGB", "RGBA"}:
                    normalized = normalized.convert("RGBA" if "A" in normalized.mode else "RGB")
                output = BytesIO()
                normalized.save(output, format="WEBP", quality=78, method=4)
        except (OSError, ValueError) as error:
            raise ValueError(f"附件图片已损坏或不是有效图片：{file_id}") from error
        content = output.getvalue()
        if max_edge == 512:
            with workspace_file_mutation_lock(self._workspace_root):
                path = self.resolve_directory_uri(parse_resource_uri(uri, local_gateway_id=get_current_gateway_id()))
                if path.exists():
                    # 未登记的成品只能与本次确定生成结果一致，不能吸收半写或损坏字节。
                    verify_content(path, "sha256:" + hashlib.sha256(content).hexdigest(), len(content), attachment_id)
                else:
                    with path.open("xb") as stream:
                        restrict_private_path_access(path, descriptor=stream.fileno())
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    fsync_directory(path.parent)
                self._catalog.publish_file_version(AttachmentFileVersion(attachment_id, "preview/512.webp", "preview", "sha256:" + hashlib.sha256(content).hexdigest(), len(content), None, "image/webp"))
        return ResourceFileContent(content, "image/webp")

    @asynccontextmanager
    async def admit_working_directory(self, target: Path) -> AsyncIterator[str | None]:
        """短时保护准入；真实进程的持久 lease 由 Terminal owner 在此边界内发布。"""
        reference = self.reference_for_path(target)
        if reference is None:
            yield None
            return
        attachment_id = parse_resource_uri(reference).tail_segments[0]
        async with self._directory_operations.lock_for(attachment_id):
            self._catalog.require_available_directory(attachment_id)
            if self.reference_for_path(target) != reference or not target.is_dir():
                raise FileNotFoundError("Shell 工作目录的附件版本不可用")
            # 公开引用带当前 Gateway 身份，持久终端 intent 保存同一目标的本机规范 URI。
            yield parse_resource_uri(self.directory_uri(attachment_id), local_gateway_id=get_current_gateway_id()).display_uri

    async def collect_garbage(self, *, leases: ExternalResourceLeaseLedger) -> tuple[str, ...]:
        await self.collect_fork_import_staging()
        collected = []
        for candidate in self._catalog.gc_candidates(now=self._clock()):
            async with self._directory_operations.lock_for(candidate.attachment_id, wait=False) as holder:
                if not holder.acquired:
                    continue
                if leases.get(candidate.attachment_id) is not None and leases.leases_for_resource(candidate.attachment_id):
                    continue
                try:
                    # 与普通文件写入/读取共用真实临界区；不另建一套文件执行链。
                    await run_sync_shielded(self._delete_directory, candidate.attachment_id)
                except AttachmentDirectoryRetainedError:
                    continue
                if leases.get(candidate.attachment_id) is not None:
                    leases.update_external_state(candidate.attachment_id, lifetime_scope="workspace", status="stopped")
                collected.append(candidate.attachment_id)
        return tuple(collected)

    def _delete_directory(self, attachment_id: str) -> None:
        with workspace_file_mutation_lock(self._workspace_root):
            record = self._catalog.begin_delete(attachment_id, now=self._clock())
            remove_directory(resolve_attachment_directory(self._attachments_root, record.relative_locator))
            self._catalog.finish_delete(attachment_id, now=self._clock())
    def _assert_session_active(self, session_id: str, thread_id: str) -> int:
        """gate 内 fresh 校验 catalog 节点 active、fence active 且 thread 未失效。"""
        node = self._path_resolver.catalog_store.get_node(session_id)
        if node.state != "active":
            raise SessionDeletionPendingError(
                "Session catalog 节点非 active，拒绝附件准入/发布: "
                f"session_id={session_id}, state={node.state!r}"
            )
        control = self._open_control_store(session_id)
        try:
            state, generation = control.get_fence()
            if state != "active":
                raise SessionDeletionPendingError(
                    "Session fence 非 active，拒绝附件准入/发布: "
                    f"session_id={session_id}, fence_state={state!r}"
                )
            main_thread_id = self._path_resolver.main_thread_id(session_id)
            if thread_id == main_thread_id:
                control.verify_matches_catalog_main_thread(thread_id)
            else:
                control.get_published_child_thread_locator(thread_id)
            return generation
        except KeyError as error:
            raise SessionDeletionPendingError(
                "Session 控制库缺少 fence/main row，拒绝附件准入: "
                f"session_id={session_id}: {error}"
            ) from error
        finally:
            control.close()

    def _open_control_store(self, session_id: str) -> SessionControlStore:
        self._catalog.acquire_access()
        session_dir = self._path_resolver.resolve_session_node(session_id)
        return SessionControlStore(session_dir / "session-control.sqlite")


def _parse_data_url(data_url: str) -> tuple[str, bytes]:
    if not data_url.startswith("data:"):
        raise ValueError("附件 data_url 必须以 data: 开头")
    header, separator, payload = data_url.partition(",")
    if not separator:
        raise ValueError("附件 data_url 缺少逗号分隔符")
    content_type = header.removeprefix("data:").split(";", 1)[0]
    if not content_type:
        raise ValueError("附件 data_url 缺少 MIME 类型")
    if ";base64" not in header:
        return content_type, unquote_to_bytes(payload)
    try:
        return content_type, base64.b64decode(payload, validate=True)
    except ValueError as error:
        raise ValueError("附件 data_url 包含非法 base64 数据") from error
