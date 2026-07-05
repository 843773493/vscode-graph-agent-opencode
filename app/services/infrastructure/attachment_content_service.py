from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from app.core.path_utils import validate_workspace_path
from app.schemas.public_v2.message import AttachmentRef

SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
DATA_URL_PREFIX = "data:"


def _attachment_content_type(attachment: AttachmentRef, file_path: Path) -> str:
    if attachment.content_type:
        return attachment.content_type
    guessed_type, _ = mimetypes.guess_type(str(file_path))
    if guessed_type:
        return guessed_type
    raise ValueError(
        f"无法识别附件 MIME 类型: file_id={attachment.file_id!r}。"
        "请在 attachments[].content_type 中显式传入 image/jpeg、image/png 等类型。"
    )


def _content_type_from_data_url(data_url: str) -> str:
    if not data_url.startswith(DATA_URL_PREFIX):
        raise ValueError("图片附件 data_url 必须以 data: 开头")
    header, sep, _ = data_url.partition(",")
    if not sep:
        raise ValueError("图片附件 data_url 缺少逗号分隔符")
    content_type = header.removeprefix(DATA_URL_PREFIX).split(";", 1)[0]
    if not content_type:
        raise ValueError("图片附件 data_url 缺少 MIME 类型")
    return content_type


def _build_image_content_block(attachment: AttachmentRef) -> dict[str, object]:
    if attachment.data_url:
        content_type = attachment.content_type or _content_type_from_data_url(
            attachment.data_url
        )
        if content_type not in SUPPORTED_IMAGE_MIME_TYPES:
            raise ValueError(
                f"不支持的图片附件类型: {content_type!r}，file_id={attachment.file_id!r}"
            )
        return {
            "type": "image_url",
            "image_url": {
                "url": attachment.data_url,
            },
        }

    file_path = validate_workspace_path(attachment.file_id)
    if not file_path.exists():
        raise FileNotFoundError(f"图片附件不存在: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"图片附件必须是文件: {file_path}")

    content_type = _attachment_content_type(attachment, file_path)
    if content_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ValueError(
            f"不支持的图片附件类型: {content_type!r}，file_id={attachment.file_id!r}"
        )

    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{content_type};base64,{encoded}",
        },
    }


def build_human_content(
    message: str,
    attachments: list[AttachmentRef],
) -> str | list[dict[str, object]]:
    if not attachments:
        return message

    content: list[dict[str, object]] = []
    if message:
        content.append({"type": "text", "text": message})
    for attachment in attachments:
        content.append(_build_image_content_block(attachment))
    return content
