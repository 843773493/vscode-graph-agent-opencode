from __future__ import annotations

import base64
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.core.path_utils import (
    get_session_path_resolver,
    get_workspace_root,
    safe_join,
    validate_workspace_path,
)
from app.services.infrastructure.session_attachment_store import (
    SESSION_ATTACHMENT_SCHEME,
)
from app.schemas.internal_v2.message import AttachmentRef

SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-matroska",
}
VIDEO_FRAME_MIME_TYPE = "image/jpeg"
VIDEO_FRAME_COUNT = 6
VIDEO_FRAME_FPS = 1
VIDEO_FRAME_WIDTH = 512


def _resolve_attachment_file(file_id: str, workspace_root: Path | None) -> Path:
    if not file_id.startswith(SESSION_ATTACHMENT_SCHEME):
        if workspace_root is not None:
            resolved_workspace_root = workspace_root.resolve()
            candidate = Path(file_id)
            if not candidate.is_absolute():
                return safe_join(resolved_workspace_root, file_id)
            resolved_candidate = candidate.resolve()
            if not resolved_candidate.is_relative_to(resolved_workspace_root):
                raise ValueError(
                    "附件路径越出显式工作区: "
                    f"workspace={resolved_workspace_root}, path={resolved_candidate}"
                )
            return resolved_candidate
        return validate_workspace_path(file_id)
    relative_locator = file_id.removeprefix(SESSION_ATTACHMENT_SCHEME)
    session_id, separator, relative_path = relative_locator.partition("/")
    if not separator or not session_id or not relative_path.startswith("attachments/"):
        raise ValueError(f"会话附件逻辑定位符格式无效: {file_id}")
    resolved_workspace_root = (workspace_root or get_workspace_root()).resolve()
    resolver = get_session_path_resolver(
        resolved_workspace_root / ".boxteam" / "sessions"
    )
    return safe_join(resolver.resolve_session_node(session_id), relative_path)


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
    workspace_root: Path | None,
) -> tuple[str, bytes]:
    file_path = _resolve_attachment_file(attachment.file_id, workspace_root)
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


def _resolve_attachment_content_type(
    attachment: AttachmentRef,
    workspace_root: Path | None,
) -> str:
    content_type = attachment.content_type
    if content_type is None:
        file_path = _resolve_attachment_file(attachment.file_id, workspace_root)
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


def _build_image_content_block(
    attachment: AttachmentRef,
    workspace_root: Path | None,
) -> dict[str, object]:
    if attachment.data_url:
        raise ValueError(
            "Agent 运行阶段不接受 data_url 附件；浏览器上传内容必须先持久化为 "
            f"boxteam-session 定位符: file_id={attachment.file_id!r}"
        )

    file_path = _resolve_attachment_file(attachment.file_id, workspace_root)
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


def _build_video_content_blocks(
    attachment: AttachmentRef,
    workspace_root: Path | None,
) -> list[dict[str, object]]:
    if attachment.data_url:
        raise ValueError(
            "Agent 运行阶段不接受 data_url 附件；浏览器上传内容必须先持久化为 "
            f"boxteam-session 定位符: file_id={attachment.file_id!r}"
        )
    content_type, video_bytes = _read_workspace_attachment_bytes(
        attachment,
        supported_types=SUPPORTED_VIDEO_MIME_TYPES,
        media_name="视频",
        workspace_root=workspace_root,
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
    workspace_root: Path | None,
) -> list[dict[str, object]]:
    label_block = _build_attachment_label_block(
        attachment,
        content_type=content_type,
        index=index,
        total=total,
    )
    if content_type in SUPPORTED_IMAGE_MIME_TYPES:
        return [label_block, _build_image_content_block(attachment, workspace_root)]
    if content_type in SUPPORTED_VIDEO_MIME_TYPES:
        return [
            label_block,
            *_build_video_content_blocks(attachment, workspace_root),
        ]
    raise ValueError(
        f"不支持的附件类型: {content_type!r}，file_id={attachment.file_id!r}。"
        "当前支持图片和视频附件。"
    )


def build_human_content(
    message: str,
    attachments: list[AttachmentRef],
    *,
    workspace_root: Path | None = None,
) -> str | list[dict[str, object]]:
    if not attachments:
        return message

    content: list[dict[str, object]] = []
    if message:
        content.append({"type": "text", "text": message})
    attachment_content_types = [
        (attachment, _resolve_attachment_content_type(attachment, workspace_root))
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
                workspace_root=workspace_root,
            )
        )
    return content
