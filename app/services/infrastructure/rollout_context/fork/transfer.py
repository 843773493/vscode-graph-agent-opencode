"""既有 captured artifact 的有界传输；不读取 live source 或持有目标 gate。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import Iterator
from dataclasses import asdict
from itertools import islice
from pathlib import Path, PurePosixPath

from app.abstractions.session_context import SessionForkTransportProtocol
from app.core.atomic_fs import fsync_directory
from app.core.resource_uri import parse_resource_uri
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.trace_middleware import require_current_gateway_id
from app.gateway.federation.grants import ForkGrantTarget
from app.schemas.internal_v2.session import SessionForkRequest
from app.schemas.internal_v2.session_fork_transfer import (
    FORK_TRANSFER_CHUNK_BYTES,
    FORK_TRANSFER_FILE_BATCH,
    ForkSourceCaptureDTO,
    ForkTransferFileDTO,
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)

from .source_capture import (
    capture_directory,
    read_captured_snapshot,
    remove_captured_snapshot,
)
from .source_snapshot import ForkSourceSnapshot

_TRANSFER_FILE_CONCURRENCY = 4


def source_transfer_request(
    request: SessionForkRequest, capture: ForkSourceCaptureRecord | None, *, action: str,
    cursor: int = 0, file_name: str | None = None, offset: int = 0,
    release_state: str | None = None, blob_id: str | None = None,
) -> SessionForkTransferRequest:
    origin = require_current_gateway_id("fork source artifact transport")
    source = parse_resource_uri(request.source_uri, local_gateway_id=origin)
    return SessionForkTransferRequest(
        source_gateway_id=origin,
        fork_target=ForkGrantTarget(gateway_id=source.gateway_authority or origin, uri=request.source_uri, fork_role="source"),
        action=action, request=request, capture=ForkSourceCaptureDTO.from_record(capture) if capture is not None else None,
        cursor=cursor, file_name=file_name, offset=offset, release_state=release_state, blob_id=blob_id,
    )


def _artifact_file(root: Path, record: ForkSourceCaptureRecord, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or relative.as_posix() != name or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("fork artifact 文件名必须是固定快照内的规范相对路径")
    if name != "captured.json" and (not relative.parts or relative.parts[0] != record.source_session_id):
        raise ValueError("fork artifact 文件不属于精确 source snapshot")
    current = root
    if current.is_symlink():
        raise RuntimeError("fork artifact 根目录不能是符号链接")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError("fork artifact 不能经过符号链接")
    return current


def _artifact_files(root: Path, record: ForkSourceCaptureRecord) -> Iterator[ForkTransferFileDTO]:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("fork source captured artifact 不存在或不是目录")
    for directory, names, files in os.walk(root, followlinks=False):
        names.sort()
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            path = _artifact_file(root, record, relative)
            if not path.is_file():
                raise RuntimeError("fork captured artifact 包含非普通文件")
            yield ForkTransferFileDTO(name=relative, size_bytes=path.stat().st_size)


def export_file_batch(sessions_root: Path, record: ForkSourceCaptureRecord, cursor: int) -> SessionForkTransferResultDTO:
    root = capture_directory(sessions_root, record)
    batch = list(islice(_artifact_files(root, record), cursor, cursor + FORK_TRANSFER_FILE_BATCH + 1))
    return SessionForkTransferResultDTO(
        capture=ForkSourceCaptureDTO.from_record(record), files=batch[:FORK_TRANSFER_FILE_BATCH],
        next_cursor=cursor + FORK_TRANSFER_FILE_BATCH if len(batch) > FORK_TRANSFER_FILE_BATCH else None,
    )


def export_file_chunk(sessions_root: Path, record: ForkSourceCaptureRecord, name: str, offset: int) -> SessionForkTransferResultDTO:
    path = _artifact_file(capture_directory(sessions_root, record), record, name)
    if not path.is_file() or offset > path.stat().st_size:
        raise ValueError("fork chunk 文件缺失或 bytes offset 超出冻结文件")
    with path.open("rb") as stream:
        stream.seek(offset)
        chunk = stream.read(FORK_TRANSFER_CHUNK_BYTES)
    return SessionForkTransferResultDTO(
        capture=ForkSourceCaptureDTO.from_record(record), offset=offset,
        chunk_base64=base64.b64encode(chunk).decode("ascii"), chunk_sha256=hashlib.sha256(chunk).hexdigest(),
    )


def verify_capture_identity(expected: ForkSourceCaptureRecord, received: ForkSourceCaptureDTO | None) -> ForkSourceCaptureRecord:
    if received is None:
        raise RuntimeError("fork transport 缺少 source capture 回执")
    record = received.to_record()
    ignored = {"state", "snapshot_sha256", "marker_sha256", "abort_reason"}
    if {key: value for key, value in asdict(expected).items() if key not in ignored} != {
        key: value for key, value in asdict(record).items() if key not in ignored
    }:
        raise RuntimeError("fork transport operation/preimage/snapshot identity 漂移")
    if expected.snapshot_sha256 not in {None, record.snapshot_sha256} or expected.marker_sha256 not in {None, record.marker_sha256}:
        raise RuntimeError("fork transport 冻结 hash 漂移")
    return record


def verify_capture_receipt(expected: ForkSourceCaptureRecord, received: ForkSourceCaptureDTO | None) -> ForkSourceCaptureRecord:
    record = verify_capture_identity(expected, received)
    if record.state != "source_captured" or record.snapshot_sha256 is None or record.marker_sha256 is None:
        raise RuntimeError("fork transport source 尚未完整 captured")
    return record


async def import_captured_artifact(
    transport: SessionForkTransportProtocol, request: SessionForkRequest,
    sessions_root: Path, record: ForkSourceCaptureRecord,
) -> tuple[ForkSourceSnapshot, str]:
    root = capture_directory(sessions_root, record)

    async def receive_file(file: ForkTransferFileDTO) -> None:
        path = _artifact_file(root, record, file.name)
        await receive_transfer_file(transport, request, frozen, path=path, size_bytes=file.size_bytes,
                                    action="chunk", file_name=file.name)

    try:
        reply = await transport.fork_transfer(source_transfer_request(request, record, action="capture"))
        frozen = verify_capture_receipt(record, reply.capture)
        cursor = 0
        marker = None
        while True:
            page = await transport.fork_transfer(source_transfer_request(request, frozen, action="files", cursor=cursor))
            verify_capture_receipt(frozen, page.capture)
            for file in page.files:
                if file.name == "captured.json":
                    if marker is not None:
                        raise RuntimeError("fork artifact 重复 captured marker")
                    marker = file
            files = (file for file in page.files if file.name != "captured.json")
            while batch := tuple(islice(files, _TRANSFER_FILE_CONCURRENCY)):
                transfers = [asyncio.create_task(receive_file(file)) for file in batch]
                try:
                    await asyncio.gather(*transfers)
                except BaseException:
                    # 先回收同页写入者，再由外层清理快照；保留原失败类型与身份。
                    for task in transfers:
                        task.cancel()
                    await asyncio.gather(*transfers, return_exceptions=True)
                    raise
            if page.next_cursor is None:
                break
            if page.next_cursor != cursor + FORK_TRANSFER_FILE_BATCH or len(page.files) != FORK_TRANSFER_FILE_BATCH:
                raise RuntimeError("fork artifact 文件分页未按冻结顺序前进")
            cursor = page.next_cursor
        if marker is None:
            raise RuntimeError("fork artifact 缺少 captured marker")
        await receive_file(marker)
        fsync_directory(root)
        snapshot, marker_hash = read_captured_snapshot(sessions_root, frozen)
        return snapshot, marker_hash
    except BaseException:
        remove_captured_snapshot(sessions_root, record)
        raise


async def receive_transfer_file(
    transport: SessionForkTransportProtocol, request: SessionForkRequest, capture: ForkSourceCaptureRecord,
    *, path: Path, size_bytes: int, action: str, file_name: str | None = None, blob_id: str | None = None,
) -> None:
    """快照文件与附件共用固定块 reader；正文总长度不影响单次内存。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        offset = 0
        while offset < size_bytes:
            chunk = await transport.fork_transfer(source_transfer_request(
                request, capture, action=action, file_name=file_name, blob_id=blob_id, offset=offset,
            ))
            verify_capture_receipt(capture, chunk.capture)
            if chunk.offset != offset or chunk.chunk_base64 is None or chunk.chunk_sha256 is None:
                raise RuntimeError("fork transport 分块 bytes offset 或正文回执缺失")
            payload = base64.b64decode(chunk.chunk_base64, validate=True)
            if not payload or len(payload) > min(FORK_TRANSFER_CHUNK_BYTES, size_bytes - offset) or hashlib.sha256(payload).hexdigest() != chunk.chunk_sha256:
                raise RuntimeError("fork transport 分块 hash/长度不一致")
            stream.write(payload)
            offset += len(payload)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)
