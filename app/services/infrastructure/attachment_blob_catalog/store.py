"""workspace 附件内容寻址 blob store 的写入/读取/恢复/GC 垂直链路（8.3 附件链）。

写入是 pin 与 workspace ingest 之间的显式跨库 saga，**不假设两个 SQLite
原子提交**，也不跨正文写入或模型执行持有 gate：

1. 取得按 canonical session ID 互斥的 workspace `SessionLifecycleGate`，
   在 gate 内确认 owner Session catalog/thread/fence 仍为同一 active
   generation，并于 `session-control.sqlite` create-or-get 承担 operation
   lease 的 durable `AttachmentOperationPin`（`operation_kind=attachment`），
   冻结 generation、ingest operation、精确 session/thread 与 preimage；
2. `catalog.sqlite` create-or-get `AttachmentIngestRecord(state=preparing)`，
   冻结 pin identity、workspace/preimage、限制与受控 staging locator；
3. 正文只写入 record 指定的 staging，完成有界写入、hash/length 与 durability barrier；
4. catalog 事务内推进 record→`hashed` 并以 digest 唯一约束 create-or-get
   `BlobCommitClaim`；
5. 持有 digest 提交锁，把 staging 原子 rename 到 claim 冻结的最终 locator
   并复验正文；后续上传可完成尚未落盘的同内容 claim，不改写其身份；
6. 发布 availability/owner reference **必须再次取得同一 gate**，持锁期间
   复验 pin 有效、fence 仍为 pin 捕获的 active generation、thread 未失效，
   提交 attachment catalog 事务后才释放 gate 与 pin。

reader 只按 catalog 冻结的 record 取受检 locator，绝不扫盘、绝不把调用方
字符串拼成路径。同一 digest 的后续上传复用既有 blob 与首次 locator。
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import threading
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote_to_bytes

from PIL import Image, ImageOps

from app.abstractions.resource_file_content import ResourceFileContent
from app.core.identifier import create_uuid_hex
from app.core.path_utils import get_session_path_resolver
from app.core.phase_observer import observe_phase
from app.core.resource_uri import parse_session_artifact_uri, session_artifact_uri
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    OperationHolderGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
    SessionOperationLease,
)
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    ATTACHMENT_ROOT_DIRECTORY_NAME,
    BlobIdentity,
    compute_blob_identity,
)
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    CATALOG_DATABASE_NAME,
    AttachmentBlobCatalog,
    AttachmentOwnerRef,
    derive_attachment_id,
)
from app.services.infrastructure.attachment_blob_catalog.fork_store import (
    ForkAttachmentStoreMixin,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    ingest_staging_relative_locator,
    resolve_blob_path,
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.queries import (
    AttachmentBlobRetainedError,
)
from app.services.infrastructure.attachment_blob_catalog.recovery import (
    AttachmentIngestRecoveryMixin,
)

__all__ = [
    "ATTACHMENT_ROOT_DIRECTORY_NAME",
    "DERIVED_DIRECTORY_NAME",
    "MAX_ATTACHMENT_BYTES",
    "AttachmentBlobStore",
    "parse_logical_file_id",
]

DERIVED_DIRECTORY_NAME = ".derived"
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
_VERIFIED_BLOB_SIGNATURE_CACHE_SIZE = 256


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_logical_file_id(session_id: str, file_id: str) -> str:
    """解析逻辑引用并返回 attachment_id；不属于该 session 即失败。"""
    parsed = parse_session_artifact_uri(file_id)
    if parsed.session_artifact_kind != "attachments" or parsed.tail_segments[0] != session_id:
        raise ValueError("附件逻辑定位符不属于指定会话")
    return parsed.tail_segments[2]


class AttachmentBlobStore(ForkAttachmentStoreMixin, AttachmentIngestRecoveryMixin):
    """workspace 级内容寻址附件 store（唯一权威是 catalog.sqlite）。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        path_resolver: SessionCatalogPathResolver | None = None,
    ) -> None:
        self._workspace_root = workspace_root.expanduser().resolve()
        self._verified_blob_signatures: OrderedDict[
            tuple[str, str, str, int], tuple[int, int, int, int, int]
        ] = OrderedDict()
        self._verified_blob_signatures_lock = threading.Lock()
        self._sessions_root = self._workspace_root / ".boxteam" / "sessions"
        self._fork_imports = OperationHolderGate(self._sessions_root, namespace="attachment_fork_import")
        self._blob_commits = OperationHolderGate(self._sessions_root, namespace="attachment_blob_commit")
        self._attachments_root = (
            self._workspace_root / ".boxteam" / ATTACHMENT_ROOT_DIRECTORY_NAME
        )
        self._catalog = AttachmentBlobCatalog(
            self._attachments_root / CATALOG_DATABASE_NAME
        )
        self._path_resolver = path_resolver or get_session_path_resolver(
            self._sessions_root
        )
        self._gate = SessionLifecycleGate(self._sessions_root)
        self._topology_gate = NavigationTopologyGate(self._sessions_root)
        self.deletion_holder = OperationHolderGate(self._sessions_root, namespace="attachment_ingest")

    def reference(self, session_id: str, attachment_id: str) -> str:
        return session_artifact_uri(read_workspace_id(self._workspace_root), session_id, "attachments", attachment_id,
                                    gateway_authority=get_current_gateway_id())

    def _parse_logical_file_id(self, session_id: str, file_id: str) -> str:
        parsed = parse_session_artifact_uri(file_id, local_gateway_id=get_current_gateway_id())
        if parsed.gateway_authority is not None or parsed.scope_id != read_workspace_id(self._workspace_root):
            raise ValueError("附件资源引用不属于本工作区 owner")
        return parse_logical_file_id(session_id, file_id)

    def _parse_canonical_attachment_id(self, session_id: str, file_id: str) -> str:
        """解析已接受的历史引用；历史 Gateway 身份不充当本地正文访问路由。

        调用方仍须用精确 catalog owner ref/pin 核验使用事实；外部请求继续走
        _parse_logical_file_id 的当前 Gateway 校验，不能借此访问远端引用。
        """
        parsed = parse_session_artifact_uri(file_id)
        if (
            parsed.scope_id != read_workspace_id(self._workspace_root)
            or parsed.session_artifact_kind != "attachments"
            or parsed.tail_segments[0] != session_id
        ):
            raise ValueError("附件资源引用不属于本工作区 owner")
        return parse_logical_file_id(session_id, file_id)

    def close(self) -> None:
        """关闭 catalog 连接。"""
        self._catalog.close()

    # ------------------------------------------------------------------
    # 写入 saga
    # ------------------------------------------------------------------

    async def persist_inline(
        self,
        session_id: str,
        attachments: Sequence[AttachmentRef],
        *,
        thread_id: str,
        message_id: str,
    ) -> list[AttachmentRef]:
        """持久化带 `data_url` 的内联附件，返回逻辑引用。

        调用方先冻结消息身份；pin 保留到该消息的 canonical 接受提交完成。
        """
        if not isinstance(message_id, str) or not message_id or ":" in message_id:
            raise ValueError("附件上传必须绑定非空且不含冒号的 message_id")
        if not attachments:
            return []
        persisted: list[AttachmentRef] = []
        # 正文 I/O 只持长操作 holder；恢复和删除在 Session gate 外等待它退出。
        async with self.deletion_holder.lock_for(session_id, exclusive=False):
            for attachment in attachments:
                if not attachment.data_url:
                    persisted.append(attachment)
                    continue
                persisted.append(await self._persist_one(session_id, thread_id, message_id, attachment))
        return persisted

    async def _persist_one(
        self, session_id: str, thread_id: str, message_id: str, attachment: AttachmentRef
    ) -> AttachmentRef:
        content_type, data = _parse_data_url(attachment.data_url or "")
        declared_type = attachment.content_type or content_type
        if declared_type != content_type:
            raise ValueError(
                "附件 content_type 与 data_url MIME 不一致: "
                f"{declared_type!r} != {content_type!r}"
            )
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"附件超过 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB 限制"
            )
        identity = compute_blob_identity(data)
        # 上传尝试与正文身份分开；失败尝试终态不可重开，同内容新上传仍可复用 blob。
        idempotency_key = "attkey_" + create_uuid_hex(namespace="attachment_ingest")
        ingest_id = "ing_" + _sha256_hex(idempotency_key)[:32]
        staging_locator = ingest_staging_relative_locator(ingest_id)
        preimage_hash = _sha256_hex(
            f"{session_id}|{thread_id}|{message_id}|{identity.digest}|{identity.length}"
        )

        control = self._open_control_store(session_id)
        try:
            pin = await self._acquire_pin(
                session_id=session_id,
                thread_id=thread_id,
                message_id=message_id,
                control=control,
                operation_identity=idempotency_key,
                preimage_hash=preimage_hash,
            )
            phase_details = {
                "session_id": session_id, "thread_id": thread_id, "message_id": message_id,
                "blob_id": identity.blob_id, "lease_id": pin.lease_id,
            }
            await observe_phase(idempotency_key, "attachment.ingest.pin_acquired", details=phase_details)
            record = self._catalog.create_or_get_ingest_record(
                ingest_idempotency_key=idempotency_key,
                ingest_id=ingest_id,
                owner_session_id=session_id,
                owner_thread_id=thread_id,
                pin_lease_id=pin.lease_id,
                pin_fencing_token=pin.fencing_token,
                pin_captured_generation=pin.captured_lifecycle_generation,
                preimage_hash=preimage_hash,
                staging_relative_locator=staging_locator,
                max_bytes=MAX_ATTACHMENT_BYTES,
            )
            await observe_phase(idempotency_key, "attachment.ingest.preparing", details=phase_details)
            staging_path = resolve_staging_path(
                self._attachments_root, record.staging_relative_locator
            )
            try:
                self._write_staging(staging_path, data)
                await observe_phase(idempotency_key, "attachment.ingest.staging_durable", details=phase_details)
                async with self._blob_commits.lock_for(identity.blob_id):
                    _record, claim = self._catalog.mark_ingest_hashed(
                        ingest_idempotency_key=idempotency_key, identity=identity
                    )
                    await observe_phase(idempotency_key, "attachment.ingest.hashed", details=phase_details)
                    blob = self._catalog.get_blob(identity.blob_id)
                    if blob is None or blob.availability == "tombstoned":
                        # 同内容 staging 可完成原 claim；首次日期、locator 和活 winner 不变。
                        self._commit_staged_blob(
                            staging_path=staging_path,
                            final_path=resolve_blob_path(self._attachments_root, claim.final_relative_locator),
                            identity=identity,
                        )
                    else:
                        # 已发布正文损坏或丢失必须暴露，不能借重传静默修复。
                        published_path = self._verified_blob_path(identity.blob_id, claim.final_relative_locator)
                        self._verify_blob_content(published_path, identity.digest, identity.length, identity.blob_id)
                        _remove_file(staging_path)
                    await observe_phase(idempotency_key, "attachment.ingest.blob_committed", details=phase_details)
                # hashed record 在 GC 事务中保活；释放正文锁后才取得 Session gate。
                await self._publish(
                    session_id=session_id,
                    thread_id=thread_id,
                    control=control,
                    pin=pin,
                    identity=identity,
                    claim=claim,
                    record_key=idempotency_key,
                    attachment=attachment,
                    content_type=content_type,
                )
                await observe_phase(idempotency_key, "attachment.ingest.published", details=phase_details)
            except BaseException:
                # catalog 是 durable 主体；发布后的结算失败不能倒写为 failed。
                if self._catalog.get_ingest_record(idempotency_key).state == "published":
                    raise
                _remove_file(staging_path)
                self._catalog.abort_ingest_record(
                    ingest_idempotency_key=idempotency_key,
                    reason="附件写入或发布失败，清理 staging",
                )
                if control.verify_lease_token(lease_id=pin.lease_id, fencing_token=pin.fencing_token):
                    self._settle_pin(control, pin, outcome="failed")
                raise
            return self._attachment_ref_for_record(
                session_id, thread_id, identity.blob_id, attachment, content_type
            )
        finally:
            control.close()

    async def _acquire_pin(
        self,
        *,
        session_id: str,
        thread_id: str,
        message_id: str,
        control: SessionControlStore,
        operation_identity: str,
        preimage_hash: str,
    ) -> SessionOperationLease:
        """gate 内 fresh 校验 + create-or-get durable `AttachmentOperationPin`。"""
        async with self._topology_gate.shared(), self._gate.exclusive(session_id):
            generation = self._assert_session_active(session_id, thread_id)
            return control.create_or_get_lease(
                operation_kind="attachment",
                operation_identity=operation_identity,
                preimage_hash=preimage_hash,
                expected_generation=generation,
                recovery_ref=f"attachment:{session_id}:{thread_id}:{message_id}",
            )

    async def _publish(
        self,
        *,
        session_id: str,
        thread_id: str,
        control: SessionControlStore,
        pin: SessionOperationLease,
        identity: BlobIdentity,
        claim,
        record_key: str,
        attachment: AttachmentRef,
        content_type: str,
    ) -> AttachmentOwnerRef:
        """再次取得同一 gate，复验 pin/fence/thread 后原子发布。"""
        async with self._topology_gate.shared(), self._gate.exclusive(session_id):
            if not control.verify_lease_token(
                lease_id=pin.lease_id, fencing_token=pin.fencing_token
            ):
                raise SessionDeletionPendingError(
                    "附件 pin 已失效，拒绝发布 owner reference: "
                    f"session_id={session_id}, lease_id={pin.lease_id}"
                )
            generation = self._assert_session_active(session_id, thread_id)
            if generation != pin.captured_lifecycle_generation:
                raise SessionDeletionPendingError(
                    "Session lifecycle generation 已漂移，拒绝发布附件引用: "
                    f"session_id={session_id}, "
                    f"pinned={pin.captured_lifecycle_generation}, "
                    f"actual={generation}"
                )
            attachment_id = derive_attachment_id(
                blob_id=identity.blob_id,
                owner_session_id=session_id,
                owner_thread_id=thread_id,
            )
            ref = self._catalog.publish_blob_and_owner_ref(
                identity=identity,
                relative_locator=claim.final_relative_locator,
                ingest_idempotency_key=record_key,
                attachment_id=attachment_id,
                owner_session_id=session_id,
                owner_thread_id=thread_id,
                file_name=attachment.name,
                mime_type=content_type,
                max_bytes=MAX_ATTACHMENT_BYTES,
            )
            # 正文已发布，但对应消息尚未接受；pin 继续保留到 canonical 提交。
        return ref

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read(self, session_id: str, file_id: str, *, owner_thread_id: str | None = None) -> ResourceFileContent:
        """按逻辑引用读取正文；逐字节复验 digest/length。"""
        ref, blob = self._resolve_published(session_id, file_id, owner_thread_id=owner_thread_id)
        path = self._verified_blob_path(blob.blob_id, blob.relative_locator)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise OSError(error.errno, error.strerror, file_id) from error
        _verify_content(data, blob.digest, blob.length, blob.blob_id)
        content_type = ref.mime_type or _guess_content_type(path)
        return ResourceFileContent(data=data, content_type=content_type)

    def read_thumbnail(
        self, session_id: str, file_id: str, *, max_edge: int = 512
    ) -> ResourceFileContent:
        """读取缓存缩略图；首次请求时从原图派生 WebP。"""
        if max_edge < 64 or max_edge > 1024:
            raise ValueError("图片缩略图 max_edge 必须在 64 到 1024 之间")
        source = self.read(session_id, file_id)
        if not source.content_type.startswith("image/"):
            raise ValueError(f"附件不是图片，无法生成缩略图: {file_id}")
        attachment_id = self._parse_logical_file_id(session_id, file_id)
        derived_root = self._attachments_root / DERIVED_DIRECTORY_NAME
        target = derived_root / f"{attachment_id}-{max_edge}.webp"
        if not target.is_file():
            try:
                with Image.open(BytesIO(source.data)) as image:
                    normalized = ImageOps.exif_transpose(image)
                    normalized.thumbnail(
                        (max_edge, max_edge), Image.Resampling.LANCZOS
                    )
                    if normalized.mode not in {"RGB", "RGBA"}:
                        normalized = normalized.convert(
                            "RGBA" if "A" in normalized.mode else "RGB"
                        )
                    output = BytesIO()
                    normalized.save(output, format="WEBP", quality=78, method=4)
            except (OSError, ValueError) as error:
                # 损坏或被截断的图片必须给出可诊断的领域错误，而不是让 PIL 的
                # OSError 冒泡成 500。调用方据此返回 4xx。
                raise ValueError(
                    f"附件图片已损坏或不是有效图片，无法生成缩略图: {file_id}"
                ) from error
            try:
                derived_root.mkdir(parents=True, exist_ok=True)
                _write_once(target, output.getvalue())
            except OSError as error:
                raise OSError(error.errno, error.strerror, file_id) from error
        try:
            return ResourceFileContent(data=target.read_bytes(), content_type="image/webp")
        except OSError as error:
            raise OSError(error.errno, error.strerror, file_id) from error

    def relative_path(self, session_id: str, file_id: str) -> str:
        """返回供模型/canonical content 使用的逻辑引用（绝不暴露物理 locator）。

        附件正文的受控内容通过 variant/preview block 提供；manifest 只携带
        逻辑 ``attachment_id`` 引用，模型或 projector 不得拿到 blob 物理路径。
        """
        self._resolve_published(session_id, file_id)
        return file_id

    def _resolve_published(self, session_id: str, file_id: str, *, owner_thread_id: str | None = None):
        """按逻辑引用解析 owner reference 与 blob；越权/已 tombstone 一律失败。"""
        if owner_thread_id is not None:
            snapshot = self._catalog.fork_attachment_snapshot_for_alias(
                target_session_id=session_id, target_thread_id=owner_thread_id, source_file_id=file_id,
            )
            if snapshot is not None:
                self._assert_session_active(session_id, owner_thread_id)
                projected = self.project_attachment(session_id, owner_thread_id, file_id)
                if projected["availability"] != "available":
                    raise FileNotFoundError(f"当前 Thread 的冻结附件引用不可用: {file_id}")
                file_id, owner_thread_id = projected["file_id"], snapshot.target_owner_thread_id
        attachment_id = self._parse_logical_file_id(session_id, file_id)
        ref = self._catalog.get_owner_ref(attachment_id)
        if ref is None:
            raise FileNotFoundError(f"会话附件不存在: {file_id}")
        if ref.owner_session_id != session_id:
            raise ValueError("附件逻辑定位符不属于指定会话")
        if owner_thread_id is not None and ref.owner_thread_id != owner_thread_id:
            raise PermissionError("附件逻辑定位符不属于当前 SessionThread")
        if ref.state != "active":
            # 已释放（owner session/thread 删除）的 reference 只表示附件不再可用。
            raise FileNotFoundError(f"会话附件已释放: {file_id}")
        if self._catalog.get_reusable_attachment_owner(attachment_id) is None:
            raise FileNotFoundError(f"附件引用尚未完成发布: {file_id}")
        self._assert_session_active(session_id, ref.owner_thread_id)
        blob = self._catalog.get_blob(ref.blob_id)
        if blob is None or blob.availability != "available":
            raise FileNotFoundError(f"附件正文不可用: {file_id}")
        return ref, blob

    def _verified_blob_path(self, blob_id: str, relative_locator: str) -> Path:
        path = resolve_blob_path(self._attachments_root, relative_locator)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(
                "附件正文缺失或不是普通文件（fail closed）: "
                f"blob_id={blob_id}"
            )
        return path

    # ------------------------------------------------------------------
    # 恢复与 GC
    # ------------------------------------------------------------------

    def _verify_blob_content(self, path: Path, digest: str, length: int, blob_id: str) -> None:
        """冷读按固定缓冲核正文；仅复用同一文件未变化的完整校验结果。"""
        metadata = path.stat()
        signature = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
        key = (str(path), blob_id, digest, length)
        with self._verified_blob_signatures_lock:
            if self._verified_blob_signatures.get(key) == signature:
                self._verified_blob_signatures.move_to_end(key)
                return
        _verify_content(path, digest, length, blob_id)
        checked = path.stat()
        if (checked.st_dev, checked.st_ino, checked.st_size, checked.st_mtime_ns, checked.st_ctime_ns) != signature:
            raise RuntimeError(f"附件正文在完整性校验期间发生变化: blob_id={blob_id}, path={path}")
        with self._verified_blob_signatures_lock:
            self._verified_blob_signatures[key] = signature
            self._verified_blob_signatures.move_to_end(key)
            if len(self._verified_blob_signatures) > _VERIFIED_BLOB_SIGNATURE_CACHE_SIZE:
                self._verified_blob_signatures.popitem(last=False)

    async def collect_garbage(self, *, retention: timedelta) -> tuple[str, ...]:
        """定点回收无操作依赖的正文；不等待正在提交的 digest 分片。"""
        if not isinstance(retention, timedelta):
            raise TypeError(f"retention 必须是 timedelta: {retention!r}")
        await self.collect_fork_import_staging()
        before = datetime.now(UTC) - retention
        collected: list[str] = []
        for claim in self._catalog.list_unpublished_gc_claims(before=before):
            async with self._blob_commits.lock_for(claim.blob_id, wait=False) as holder:
                if not holder.acquired:
                    continue
                claim = self._catalog.abort_unused_blob_claim(claim.blob_id)
                if claim is None:
                    continue
                path = resolve_blob_path(self._attachments_root, claim.final_relative_locator)
                if path.is_symlink():
                    raise RuntimeError(f"未发布附件正文不得是符号链接: blob_id={claim.blob_id}")
                if path.exists():
                    self._verify_blob_content(path, claim.digest, claim.expected_length, claim.blob_id)
                    _remove_file(path)
                    collected.append(claim.blob_id)
        blobs = {blob.blob_id: blob for blob in (
            *self._catalog.list_gc_candidates(before=before), *self._catalog.list_tombstoned_blobs(),
        )}
        for blob in blobs.values():
            async with self._blob_commits.lock_for(blob.blob_id, wait=False) as holder:
                if not holder.acquired:
                    continue
                try:
                    # 事务内重查 ingest/fork/owner 保活；也重查已 tombstone 的删除重试。
                    self._catalog.tombstone_blob(blob.blob_id)
                except AttachmentBlobRetainedError:
                    continue
                path = resolve_blob_path(self._attachments_root, blob.relative_locator)
                existed = path.exists()
                _remove_file(path)
                if existed or blob.availability == "available":
                    collected.append(blob.blob_id)
        return tuple(collected)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

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
        session_dir = self._path_resolver.resolve_session_node(session_id)
        return SessionControlStore(session_dir / "session-control.sqlite")

    def _attachment_ref_for_record(
        self,
        session_id: str,
        thread_id: str,
        blob_id: str | None,
        attachment: AttachmentRef,
        content_type: str,
    ) -> AttachmentRef:
        if blob_id is None:
            raise RuntimeError(
                "ingest record 缺少 blob_id，无法构造逻辑引用: "
                f"session_id={session_id}"
            )
        attachment_id = derive_attachment_id(
            blob_id=blob_id,
            owner_session_id=session_id,
            owner_thread_id=thread_id,
        )
        return AttachmentRef(
            file_id=self.reference(session_id, attachment_id),
            name=attachment.name,
            content_type=content_type,
        )

    def _write_staging(self, staging_path: Path, data: bytes) -> None:
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        with open(staging_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(staging_path.parent)

    def _commit_staged_blob(
        self,
        *,
        staging_path: Path,
        final_path: Path,
        identity: BlobIdentity,
    ) -> None:
        """原子 rename 到最终 locator 并复验正文（已存在则复验后清理 staging）。"""
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.is_symlink():
            raise RuntimeError(f"attachment blob 不得是符号链接: {final_path}")
        if final_path.is_file():
            self._verify_blob_content(final_path, identity.digest, identity.length, identity.blob_id)
            _remove_file(staging_path)
            return
        os.replace(staging_path, final_path)
        _fsync_directory(final_path.parent)
        self._verify_blob_content(final_path, identity.digest, identity.length, identity.blob_id)



def _verify_content(data: bytes | Path, digest: str, length: int, blob_id: str) -> None:
    """逐字节复验正文与 catalog 冻结的 hash/length 一致（fail closed）。"""
    if isinstance(data, Path):
        hasher = hashlib.sha256()
        actual_length = 0
        with data.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
                actual_length += len(chunk)
        actual_digest = "sha256:" + hasher.hexdigest()
    else:
        actual_digest = "sha256:" + hashlib.sha256(data).hexdigest()
        actual_length = len(data)
    if actual_digest != digest or actual_length != length:
        raise RuntimeError(
            "附件正文与 catalog 冻结的 hash/length 不一致（拒绝以损坏正文冒充成功）: "
            f"blob_id={blob_id}, expected={digest}/{length}, "
            f"actual={actual_digest}/{actual_length}"
        )


def _guess_content_type(path: Path) -> str:
    content_type, _ = mimetypes.guess_type(path.name)
    return content_type or "application/octet-stream"


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _write_once(target: Path, data: bytes) -> None:
    if target.exists():
        return
    temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
    temporary.write_bytes(data)
    # TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, target)


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
