"""附件定位、读取与预览变体等基础设施能力。

Session/thread 附件正文统一走 workspace 内容寻址 blob store
（:class:`AttachmentBlobStore`）：逻辑引用是
`boxteam-session://{session_id}/attachments/{attachment_id}`，物理 locator
只由 catalog 冻结，调用方不得拼路径。非 session 的工作区文件附件仍按显式
工作区相对/绝对路径解析。

旧「按 session 目录拼 `attachments/{sha256}{suffix}`」的物理定位已物理下线。
"""

from __future__ import annotations

import base64
import mimetypes
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from app.core.path_utils import (
    get_workspace_root,
    safe_join,
    validate_workspace_path,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.store import (
    SESSION_ATTACHMENT_SCHEME,
    AttachmentBlobStore,
)

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
ATTACHMENT_PREVIEW_MAX_EDGE = 512


def _session_id_from_file_id(file_id: str) -> str:
    """从逻辑引用取出 owner session id（形态非法即失败）。"""
    if not file_id.startswith(SESSION_ATTACHMENT_SCHEME):
        raise ValueError(f"附件必须使用会话逻辑定位符: {file_id!r}")
    remainder = file_id[len(SESSION_ATTACHMENT_SCHEME):]
    session_id, separator, _tail = remainder.partition("/")
    if not separator or not session_id:
        raise ValueError(f"附件逻辑定位符格式无效: {file_id!r}")
    return session_id


def _resolve_workspace_file(file_id: str, workspace_root: Path | None) -> Path:
    """解析非 session 的工作区文件附件路径。"""
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


def _attachment_content_type(attachment: AttachmentRef, file_path: Path) -> str:
    if attachment.content_type:
        return attachment.content_type
    guessed_type, _ = mimetypes.guess_type(str(file_path))
    if guessed_type:
        return guessed_type
    raise ValueError(
        f"无法识别附件 MIME 类型: file_id={attachment.file_id!r}。"
        "请在 attachments[].content_type 中显式传入 MIME 类型。"
    )


def _file_suffix_for_content_type(content_type: str) -> str:
    suffix = mimetypes.guess_extension(content_type)
    if suffix:
        return suffix
    if content_type == "video/quicktime":
        return ".mov"
    raise ValueError(f"无法根据 MIME 类型确定临时文件扩展名: {content_type!r}")


def _frame_data_url(frame_path: Path) -> str:
    encoded = base64.b64encode(frame_path.read_bytes()).decode("ascii")
    return f"data:{VIDEO_FRAME_MIME_TYPE};base64,{encoded}"


class AttachmentContentService:
    """提供附件定位、读取和预览变体等基础设施能力。"""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        attachment_store: AttachmentBlobStore | None = None,
    ) -> None:
        self._workspace_root = (workspace_root or get_workspace_root()).resolve()
        self._attachment_store = attachment_store or AttachmentBlobStore(
            self._workspace_root
        )

    def resolve_content_type(self, attachment: AttachmentRef) -> str:
        content_type = attachment.content_type
        if content_type is not None:
            return content_type
        if attachment.file_id.startswith(SESSION_ATTACHMENT_SCHEME):
            return self._read_session_attachment(attachment).content_type
        file_path = _resolve_workspace_file(
            attachment.file_id, self._workspace_root
        )
        return _attachment_content_type(attachment, file_path)

    def relative_path(self, attachment: AttachmentRef) -> str:
        if attachment.data_url:
            raise ValueError(
                "Agent 运行阶段不接受 data_url 附件；浏览器上传内容必须先持久化为 "
                f"boxteam-session 定位符: file_id={attachment.file_id!r}"
            )
        if attachment.file_id.startswith(SESSION_ATTACHMENT_SCHEME):
            session_id = _session_id_from_file_id(attachment.file_id)
            return self._attachment_store.relative_path(
                session_id, attachment.file_id
            )
        file_path = _resolve_workspace_file(attachment.file_id, self._workspace_root)
        if not file_path.is_file():
            raise FileNotFoundError(f"附件不存在: {file_path}")
        return file_path.relative_to(self._workspace_root).as_posix()

    def image_preview_data_url(self, attachment: AttachmentRef) -> str:
        if attachment.file_id.startswith(SESSION_ATTACHMENT_SCHEME):
            session_id = _session_id_from_file_id(attachment.file_id)
            preview = self._attachment_store.read_thumbnail(
                session_id,
                attachment.file_id,
                max_edge=ATTACHMENT_PREVIEW_MAX_EDGE,
            )
            preview_type = preview.content_type
            preview_data = preview.data
        else:
            raw = self._read_workspace_attachment(attachment)
            with Image.open(BytesIO(raw)) as image:
                normalized = ImageOps.exif_transpose(image)
                normalized.thumbnail(
                    (ATTACHMENT_PREVIEW_MAX_EDGE, ATTACHMENT_PREVIEW_MAX_EDGE),
                    Image.Resampling.LANCZOS,
                )
                if normalized.mode not in {"RGB", "RGBA"}:
                    normalized = normalized.convert(
                        "RGBA" if "A" in normalized.mode else "RGB"
                    )
                output = BytesIO()
                normalized.save(output, format="WEBP", quality=78, method=4)
            preview_type = "image/webp"
            preview_data = output.getvalue()
        encoded = base64.b64encode(preview_data).decode("ascii")
        return f"data:{preview_type};base64,{encoded}"

    def video_preview_data_urls(self, attachment: AttachmentRef) -> list[str]:
        content_type, video_bytes = self._read_attachment(attachment)
        if content_type not in SUPPORTED_VIDEO_MIME_TYPES:
            raise ValueError(
                "不支持的视频附件类型: "
                f"{content_type!r}，file_id={attachment.file_id!r}"
            )
        return _extract_video_frame_data_urls(
            video_bytes=video_bytes,
            content_type=content_type,
            attachment_name=attachment.name or attachment.file_id,
        )

    def _read_workspace_attachment(self, attachment: AttachmentRef) -> bytes:
        file_path = _resolve_workspace_file(
            attachment.file_id, self._workspace_root
        )
        if not file_path.is_file():
            raise FileNotFoundError(f"附件不存在: {file_path}")
        return file_path.read_bytes()

    def _read_session_attachment(self, attachment: AttachmentRef):
        session_id = _session_id_from_file_id(attachment.file_id)
        return self._attachment_store.read(session_id, attachment.file_id)

    def _read_attachment(self, attachment: AttachmentRef) -> tuple[str, bytes]:
        if attachment.file_id.startswith(SESSION_ATTACHMENT_SCHEME):
            stored = self._read_session_attachment(attachment)
            return stored.content_type, stored.data
        file_path = _resolve_workspace_file(
            attachment.file_id, self._workspace_root
        )
        if not file_path.exists():
            raise FileNotFoundError(f"附件不存在: {file_path}")
        if not file_path.is_file():
            raise ValueError(f"附件必须是文件: {file_path}")
        content_type = _attachment_content_type(attachment, file_path)
        return content_type, file_path.read_bytes()


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
