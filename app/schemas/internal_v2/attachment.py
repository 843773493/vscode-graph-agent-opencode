from __future__ import annotations

from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    WrapSerializer,
)

from app.core.resource_uri import parse_attachment_uri, parse_message_attachment_uri


def _attachment_file_uri(value: str) -> str:
    return parse_message_attachment_uri(value).display_uri


def _attachment_directory_uri(value: str) -> str:
    parsed = parse_attachment_uri(value)
    if parsed.kind != "attachments" or len(parsed.tail_segments) != 1:
        raise ValueError("附件目录引用必须精确指向受管附件根")
    return parsed.display_uri


AttachmentFileURI = Annotated[str, AfterValidator(_attachment_file_uri), PlainSerializer(_attachment_file_uri)]
AttachmentDirectoryURI = Annotated[str, AfterValidator(_attachment_directory_uri), PlainSerializer(_attachment_directory_uri)]


class AttachmentRef(BaseModel):
    # 输入仍由原附件owner解析相对路径、绝对路径和内联上传，不能收窄为URI-only。
    file_id: str
    directory_uri: AttachmentDirectoryURI | None = None
    name: str | None = None
    content_type: str | None = None
    data_url: str | None = None


def _resolved_attachment(value: AttachmentRef) -> AttachmentRef:
    if value.directory_uri is not None:
        _attachment_directory_uri(value.directory_uri)
    return value.model_copy(update={"file_id": _attachment_file_uri(value.file_id)})


def _serialize_resolved_attachment(value: AttachmentRef, handler: SerializerFunctionWrapHandler):
    return handler(_resolved_attachment(value))


# 同一结构用于原生输入与规范化输出；输出增加业务校验，不复制公开字段。
ResolvedAttachmentRef = Annotated[
    AttachmentRef, AfterValidator(_resolved_attachment), WrapSerializer(_serialize_resolved_attachment),
]
