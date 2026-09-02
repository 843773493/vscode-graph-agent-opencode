"""普通用户消息的 canonical content 构造。"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_content_service import (
    SUPPORTED_VIDEO_MIME_TYPES,
    AttachmentContentService,
)

ATTACHMENT_BLOCK_SCHEMA_VERSION = 1


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
        resolved: list[tuple[AttachmentRef, str, str]] = []
        for attachment in attachments:
            resolved.append(
                (
                    attachment,
                    self._attachment_service.resolve_content_type(attachment),
                    self._attachment_service.relative_path(attachment),
                )
            )

        for attachment, content_type, path in resolved:
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
                    preview_error = str(error)
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
                    preview_error = str(error)
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

        return UserContentBuildResult(blocks, list(attachments), diagnostics)

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
        if variant is not None:
            metadata["variant"] = variant
        return metadata
