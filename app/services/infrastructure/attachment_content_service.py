from __future__ import annotations

import base64
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote_to_bytes

from app.core.path_utils import validate_workspace_path
from app.schemas.public_v2.message import AttachmentRef

SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-matroska",
}
DATA_URL_PREFIX = "data:"
VIDEO_FRAME_MIME_TYPE = "image/jpeg"
VIDEO_FRAME_COUNT = 6
VIDEO_FRAME_FPS = 1
VIDEO_FRAME_WIDTH = 512


def _attachment_content_type(attachment: AttachmentRef, file_path: Path) -> str:
    if attachment.content_type:
        return attachment.content_type
    guessed_type, _ = mimetypes.guess_type(str(file_path))
    if guessed_type:
        return guessed_type
    raise ValueError(
        f"无法识别附件 MIME 类型: file_id={attachment.file_id!r}。"
        "请在 attachments[].content_type 中显式传入 image/jpeg、video/mp4 等类型。"
    )


def _parse_data_url(data_url: str) -> tuple[str, bytes]:
    if not data_url.startswith(DATA_URL_PREFIX):
        raise ValueError("附件 data_url 必须以 data: 开头")
    header, sep, _ = data_url.partition(",")
    if not sep:
        raise ValueError("附件 data_url 缺少逗号分隔符")
    content_type = header.removeprefix(DATA_URL_PREFIX).split(";", 1)[0]
    if not content_type:
        raise ValueError("附件 data_url 缺少 MIME 类型")
    payload = data_url.split(",", 1)[1]
    if ";base64" in header:
        try:
            return content_type, base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise ValueError("附件 data_url 包含非法 base64 数据") from exc
    return content_type, unquote_to_bytes(payload)


def _content_type_from_data_url(data_url: str) -> str:
    content_type, _ = _parse_data_url(data_url)
    return content_type


def _file_suffix_for_content_type(content_type: str) -> str:
    suffix = mimetypes.guess_extension(content_type)
    if suffix:
        return suffix
    if content_type == "video/quicktime":
        return ".mov"
    raise ValueError(f"无法根据 MIME 类型确定临时文件扩展名: {content_type!r}")


def _read_workspace_attachment_bytes(
    attachment: AttachmentRef,
    *,
    supported_types: set[str],
    media_name: str,
) -> tuple[str, bytes]:
    file_path = validate_workspace_path(attachment.file_id)
    if not file_path.exists():
        raise FileNotFoundError(f"{media_name}附件不存在: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"{media_name}附件必须是文件: {file_path}")

    content_type = _attachment_content_type(attachment, file_path)
    if content_type not in supported_types:
        raise ValueError(
            f"不支持的{media_name}附件类型: {content_type!r}，file_id={attachment.file_id!r}"
        )
    return content_type, file_path.read_bytes()


def _read_inline_attachment_bytes(
    attachment: AttachmentRef,
    *,
    supported_types: set[str],
    media_name: str,
) -> tuple[str, bytes]:
    if not attachment.data_url:
        raise ValueError(f"{media_name}附件缺少 data_url")

    content_type, data = _parse_data_url(attachment.data_url)
    resolved_content_type = attachment.content_type or content_type
    if resolved_content_type != content_type:
        raise ValueError(
            f"{media_name}附件 content_type 与 data_url MIME 不一致: "
            f"{resolved_content_type!r} != {content_type!r}"
        )
    if resolved_content_type not in supported_types:
        raise ValueError(
            f"不支持的{media_name}附件类型: {resolved_content_type!r}，file_id={attachment.file_id!r}"
        )
    return resolved_content_type, data


def _frame_data_url(frame_path: Path) -> str:
    encoded = base64.b64encode(frame_path.read_bytes()).decode("ascii")
    return f"data:{VIDEO_FRAME_MIME_TYPE};base64,{encoded}"


def _attachment_display_name(attachment: AttachmentRef) -> str:
    return attachment.name or attachment.file_id


def _media_kind_name(content_type: str) -> str:
    if content_type in SUPPORTED_IMAGE_MIME_TYPES:
        return "图片"
    if content_type in SUPPORTED_VIDEO_MIME_TYPES:
        return "视频"
    return "附件"


def _build_attachment_label_block(
    attachment: AttachmentRef,
    *,
    content_type: str,
    index: int,
    total: int,
) -> dict[str, object]:
    name = _attachment_display_name(attachment)
    kind_name = _media_kind_name(content_type)
    return {
        "type": "text",
        "text": (
            f"附件 {index}/{total}：{name}（{kind_name}，{content_type}）。"
            "请把后续媒体内容视为这个附件；如果用户要求逐个附件说明，"
            "请按附件编号和文件名逐项回应。"
        ),
    }


def _resolve_attachment_content_type(attachment: AttachmentRef) -> str:
    content_type = attachment.content_type
    if content_type is None and attachment.data_url:
        content_type = _content_type_from_data_url(attachment.data_url)
    if content_type is None:
        file_path = validate_workspace_path(attachment.file_id)
        content_type = _attachment_content_type(attachment, file_path)
    return content_type


def _build_attachment_manifest_block(
    attachment_content_types: list[tuple[AttachmentRef, str]],
) -> dict[str, object]:
    lines = [
        f"本消息包含 {len(attachment_content_types)} 个附件。请按下面清单逐个处理，"
        "最终回复中如果需要提及附件，请保留附件编号和文件名，不能把多个附件合并成未命名附件。"
    ]
    for index, (attachment, content_type) in enumerate(
        attachment_content_types,
        start=1,
    ):
        name = _attachment_display_name(attachment)
        kind_name = _media_kind_name(content_type)
        lines.append(f"{index}. {name}（{kind_name}，{content_type}）")
    return {"type": "text", "text": "\n".join(lines)}


def _extract_video_frame_data_urls(
    *,
    video_bytes: bytes,
    content_type: str,
    attachment_name: str,
) -> list[str]:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("处理视频附件需要系统安装 ffmpeg，并确保它位于 PATH 中")

    with tempfile.TemporaryDirectory(prefix="boxteam-video-") as temp_dir:
        temp_path = Path(temp_dir)
        input_path = temp_path / f"input{_file_suffix_for_content_type(content_type)}"
        input_path.write_bytes(video_bytes)
        frame_pattern = temp_path / "frame-%03d.jpg"
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            f"fps={VIDEO_FRAME_FPS},scale={VIDEO_FRAME_WIDTH}:-2",
            "-frames:v",
            str(VIDEO_FRAME_COUNT),
            "-q:v",
            "3",
            str(frame_pattern),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "无 ffmpeg 输出"
            raise RuntimeError(f"视频附件 {attachment_name!r} 抽帧失败: {detail}")

        frame_paths = sorted(temp_path.glob("frame-*.jpg"))
        if not frame_paths:
            raise RuntimeError(f"视频附件 {attachment_name!r} 未能抽取任何关键帧")
        return [_frame_data_url(path) for path in frame_paths]


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


def _build_video_content_blocks(attachment: AttachmentRef) -> list[dict[str, object]]:
    if attachment.data_url:
        content_type, video_bytes = _read_inline_attachment_bytes(
            attachment,
            supported_types=SUPPORTED_VIDEO_MIME_TYPES,
            media_name="视频",
        )
    else:
        content_type, video_bytes = _read_workspace_attachment_bytes(
            attachment,
            supported_types=SUPPORTED_VIDEO_MIME_TYPES,
            media_name="视频",
        )

    attachment_name = _attachment_display_name(attachment)
    frame_urls = _extract_video_frame_data_urls(
        video_bytes=video_bytes,
        content_type=content_type,
        attachment_name=attachment_name,
    )
    blocks: list[dict[str, object]] = [
        {
            "type": "text",
            "text": (
                f"视频附件 {attachment_name} 已抽取为 {len(frame_urls)} 个按时间顺序排列的关键帧。"
                "请把这些关键帧视为同一个视频的时间线进行分析。"
            ),
        }
    ]
    for index, frame_url in enumerate(frame_urls, start=1):
        blocks.extend(
            [
                {
                    "type": "text",
                    "text": f"视频关键帧 {index}/{len(frame_urls)}：",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": frame_url,
                    },
                },
            ]
        )
    return blocks


def _build_attachment_content_blocks(
    attachment: AttachmentRef,
    *,
    content_type: str,
    index: int,
    total: int,
) -> list[dict[str, object]]:
    label_block = _build_attachment_label_block(
        attachment,
        content_type=content_type,
        index=index,
        total=total,
    )
    if content_type in SUPPORTED_IMAGE_MIME_TYPES:
        return [label_block, _build_image_content_block(attachment)]
    if content_type in SUPPORTED_VIDEO_MIME_TYPES:
        return [label_block, *_build_video_content_blocks(attachment)]
    raise ValueError(
        f"不支持的附件类型: {content_type!r}，file_id={attachment.file_id!r}。"
        "当前支持图片和视频附件。"
    )


def build_human_content(
    message: str,
    attachments: list[AttachmentRef],
) -> str | list[dict[str, object]]:
    if not attachments:
        return message

    content: list[dict[str, object]] = []
    if message:
        content.append({"type": "text", "text": message})
    attachment_content_types = [
        (attachment, _resolve_attachment_content_type(attachment))
        for attachment in attachments
    ]
    content.append(_build_attachment_manifest_block(attachment_content_types))
    total = len(attachment_content_types)
    for index, (attachment, content_type) in enumerate(
        attachment_content_types,
        start=1,
    ):
        content.extend(
            _build_attachment_content_blocks(
                attachment,
                content_type=content_type,
                index=index,
                total=total,
            )
        )
    return content
