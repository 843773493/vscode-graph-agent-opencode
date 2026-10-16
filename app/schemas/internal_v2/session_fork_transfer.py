"""双端 fork 的冻结 operation 与有界 artifact 分块合同。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.resource_uri import parse_resource_uri
from app.core.session_catalog_store.contracts import (
    ForkSourceCaptureRecord,
    SessionGenerationOriginDTO,
)
from app.gateway.federation.grants import ForkGrantTarget
from app.schemas.internal_v2.session import SessionForkRequest, SessionForkResultDTO

FORK_TRANSFER_CHUNK_BYTES = 64 * 1024
FORK_TRANSFER_FILE_BATCH = 128


class ForkSourceCaptureDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    copy_operation_id: str
    creation_key: str | None
    source_session_id: str
    source_thread_id: str
    target_session_id: str
    target_thread_id: str
    snapshot_id: str
    snapshot_relative_locator: str
    fork_id: str
    materialization_id: str
    request_json: str
    preimage_hash: str
    state: str
    snapshot_sha256: str | None
    marker_sha256: str | None
    abort_reason: str | None

    @classmethod
    def from_record(cls, record: ForkSourceCaptureRecord) -> ForkSourceCaptureDTO:
        return cls.model_validate(asdict(record))

    def to_record(self) -> ForkSourceCaptureRecord:
        return ForkSourceCaptureRecord(**self.model_dump())


class SessionForkTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_gateway_id: str
    fork_target: ForkGrantTarget
    action: Literal["intent", "capture", "files", "chunk", "attachment_chunk", "release", "create"]
    request: SessionForkRequest
    capture: ForkSourceCaptureDTO | None = None
    cursor: int = Field(default=0, ge=0)
    file_name: str | None = None
    offset: int = Field(default=0, ge=0, description="分块起点，单位 bytes；chunk 相对 file_name 冻结文件，attachment_chunk 相对 blob_id 冻结正文")
    generation_origin: SessionGenerationOriginDTO | None = None
    release_state: Literal["committed", "aborted"] | None = None
    blob_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> SessionForkTransferRequest:
        role = "target" if self.action == "create" else "source"
        if self.fork_target.fork_role != role:
            raise ValueError("fork transfer action 与授权 role 不一致")
        uri = self.request.target_container_uri if role == "target" else self.request.source_uri
        expected = parse_resource_uri(uri, local_gateway_id=self.fork_target.gateway_id)
        actual = parse_resource_uri(self.fork_target.uri, local_gateway_id=self.fork_target.gateway_id)
        if expected != actual:
            raise ValueError("fork transfer 固定 URI 与 request 不一致")
        if self.action in {"capture", "files", "chunk", "attachment_chunk", "release"} and self.capture is None:
            raise ValueError("fork artifact 操作必须绑定完整冻结 capture")
        if self.action == "chunk" and self.file_name is None:
            raise ValueError("fork chunk 必须指定冻结 artifact 文件")
        if self.action == "release" and self.release_state is None:
            raise ValueError("fork export 释放必须携带目标 commit/abort 回执")
        if self.action == "attachment_chunk" and self.blob_id is None:
            raise ValueError("fork 附件分块必须绑定冻结 blob_id")
        return self


class ForkTransferFileDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    size_bytes: int = Field(ge=0)


class SessionForkTransferResultDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capture: ForkSourceCaptureDTO | None = None
    intent: dict[str, object] | None = None
    files: list[ForkTransferFileDTO] = Field(default_factory=list, max_length=FORK_TRANSFER_FILE_BATCH)
    next_cursor: int | None = Field(default=None, ge=0)
    chunk_base64: str | None = Field(default=None, max_length=4 * ((FORK_TRANSFER_CHUNK_BYTES + 2) // 3))
    chunk_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    offset: int | None = Field(default=None, ge=0, description="响应块起点，单位 bytes；基准与请求的 file_name 冻结文件或 blob_id 冻结正文相同，下一位置为它加实际块长度")
    result: SessionForkResultDTO | None = None

    def validate_for_request(self, request: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        """按真实 action 核回执形状，拒绝其它阶段的伪成功结果。"""
        if request.action in {"create", "intent"}:
            required = {"result" if request.action == "create" else "intent"}
            allowed = required
        elif request.action in {"capture", "release"}:
            required = allowed = {"capture"}
        elif request.action == "files":
            required = {"capture"}
            allowed = required | {"files", "next_cursor"}
        else:
            required = allowed = {"capture", "chunk_base64", "chunk_sha256", "offset"}
        present = {name for name in type(self).model_fields if getattr(self, name) is not None
                   and (name != "files" or self.files)}
        if not required <= present or not present <= allowed:
            raise ValueError(f"fork transfer 回执字段与 action 不一致: action={request.action}, fields={sorted(present)}")
        if request.action in {"chunk", "attachment_chunk"} and self.offset != request.offset:
            raise ValueError("fork transfer 回执 offset 与冻结请求不一致")
        return self
