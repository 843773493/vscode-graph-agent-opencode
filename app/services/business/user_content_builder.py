"""普通用户消息的 canonical content 构造。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from pathlib import Path

from app.core.resource_uri import file_uri_child
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_content_service import (
    SUPPORTED_VIDEO_MIME_TYPES,
    AttachmentContentService,
)

ATTACHMENT_BLOCK_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


def _preview_failure(attachment: AttachmentRef, stage: str, error: Exception) -> str:
    detail = (
        f"附件预览生成失败: file_id={attachment.file_id}, "
        f"stage={stage}, error={type(error).__name__}"
    )
    if isinstance(error, OSError):
        for name in ("errno", "winerror"):
            value = getattr(error, name, None)
            if isinstance(value, int):
                detail += f", {name}={value}"
    # 原生位置和预览程序输出只进原始诊断，不复制到 canonical 内容及模型输入。
    logger.error("%s", detail, exc_info=error)
    return detail


def build_human_response_metadata(
    *,
    message_id: str,
    display_content: str | None,
    attachments: list[AttachmentRef],
    message_created_at: str,
    message_metadata: dict[str, object],
    attachment_diagnostics: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "message_id": message_id,
        "created_at": message_created_at,
        "updated_at": message_created_at,
        "message_metadata": dict(message_metadata),
    }
    if display_content is not None:
        metadata["display_content"] = display_content
    if attachments:
        metadata["attachments"] = [
            attachment.model_dump(mode="json", exclude={"data_url"})
            for attachment in attachments
        ]
    if attachment_diagnostics:
        metadata["attachment_diagnostics"] = [
            dict(diagnostic) for diagnostic in attachment_diagnostics
        ]
    return metadata


@dataclass(frozen=True, slots=True)
class UserContentBuildResult:
    content: str | list[dict[str, object]]
    attachments: list[AttachmentRef]
    diagnostics: list[dict[str, object]]


class UserContentBuilder:
    """生成普通用户 HumanMessage 的 canonical content blocks。"""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        attachment_service: AttachmentContentService | None = None,
    ) -> None:
        self._attachment_service = attachment_service or AttachmentContentService(
            workspace_root=workspace_root
        )

    def build(
        self,
        message: str,
        attachments: list[AttachmentRef],
    ) -> UserContentBuildResult:
        if not attachments:
            return UserContentBuildResult(message, [], [])

        blocks: list[dict[str, object]] = []
        if message:
            blocks.append({"type": "text", "text": message})
        diagnostics: list[dict[str, object]] = []
        resolved = [
            self._attachment_service.resolve_reference(attachment)
            for attachment in attachments
        ]

        for attachment in resolved:
            content_type, path = attachment.content_type, attachment.file_id
            assert content_type is not None
            preview_status = "not_applicable"
            preview_error: str | None = None
            preview_blocks: list[dict[str, object]] = []
            if content_type.startswith("image/"):
                try:
                    preview_url = self._attachment_service.image_preview_data_url(
                        attachment
                    )
                except (FileNotFoundError, OSError, ValueError) as error:
                    preview_status = "failed"
                    preview_error = _preview_failure(attachment, "image_preview", error)
                    diagnostics.append(
                        {
                            "kind": "preview_generation_failed",
                            "file_id": attachment.file_id,
                            "detail": preview_error,
                        }
                    )
                else:
                    preview_status = "available"
                    preview_blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": preview_url},
                            "metadata": self._block_metadata(
                                kind="attachment_preview",
                                attachment=attachment,
                                path=path,
                                name=attachment.name,
                                content_type=content_type,
                                variant="preview",
                                preview_status=preview_status,
                            ),
                        }
                    )
            elif content_type in SUPPORTED_VIDEO_MIME_TYPES:
                try:
                    frame_urls = self._attachment_service.video_preview_data_urls(
                        attachment
                    )
                except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
                    preview_status = "failed"
                    preview_error = _preview_failure(attachment, "video_preview", error)
                    diagnostics.append(
                        {
                            "kind": "preview_generation_failed",
                            "file_id": attachment.file_id,
                            "detail": preview_error,
                        }
                    )
                else:
                    preview_status = "available"
                    preview_blocks = [
                        {
                            "type": "image_url",
                            "image_url": {"url": frame_url},
                            "metadata": self._block_metadata(
                                kind="attachment_preview",
                                attachment=attachment,
                                path=path,
                                name=attachment.name,
                                content_type=content_type,
                                variant=f"frame-{frame_index}",
                                preview_status=preview_status,
                            ),
                        }
                        for frame_index, frame_url in enumerate(frame_urls, start=1)
                    ]

            manifest_attributes = {
                "id": attachment.file_id,
                "path": path,
                "name": attachment.name or attachment.file_id,
                "content_type": content_type,
                "preview_status": preview_status,
            }
            if attachment.directory_uri is not None:
                manifest_attributes.update(
                    directory_uri=attachment.directory_uri,
                    output_directory=file_uri_child(attachment.directory_uri, "derived"),
                    processing="保留原件，使用现有文件工具或 Shell 处理，输出写入新文件或子目录",
                )
            if preview_error:
                manifest_attributes["preview_error"] = preview_error
            manifest = " ".join(
                f'{key}="{escape(str(value), quote=True)}"'
                for key, value in manifest_attributes.items()
            )
            blocks.append(
                {
                    "type": "text",
                    "text": f"<attachment {manifest}>",
                    "metadata": self._block_metadata(
                        kind="attachment_manifest",
                        attachment=attachment,
                        path=path,
                        name=attachment.name,
                        content_type=content_type,
                        preview_status=preview_status,
                    ),
                }
            )
            blocks.extend(preview_blocks)

        return UserContentBuildResult(blocks, resolved, diagnostics)

    @staticmethod
    def _block_metadata(
        *,
        kind: str,
        attachment: AttachmentRef,
        path: str,
        name: str | None,
        content_type: str,
        variant: str | None = None,
        preview_status: str,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "origin": "generated",
            "kind": kind,
            "schema_version": ATTACHMENT_BLOCK_SCHEMA_VERSION,
            "file_id": attachment.file_id,
            "path": path,
            "name": name,
            "content_type": content_type,
            "preview_status": preview_status,
        }
        if attachment.directory_uri is not None:
            metadata["directory_uri"] = attachment.directory_uri
        if variant is not None:
            metadata["variant"] = variant
        return metadata
