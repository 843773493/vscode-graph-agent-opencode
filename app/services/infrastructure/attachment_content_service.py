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
    get_session_path_resolver,
    get_workspace_root,
    safe_join,
    validate_workspace_path,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.session_attachment_store import (
    SESSION_ATTACHMENT_SCHEME,
    SessionAttachmentStore,
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
        "请在 attachments[].content_type 中显式传入 MIME 类型。"
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


class AttachmentContentService:
    """提供附件定位、读取和预览变体等基础设施能力。"""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        attachment_store: SessionAttachmentStore | None = None,
    ) -> None:
        self._workspace_root = (workspace_root or get_workspace_root()).resolve()
        self._attachment_store = attachment_store or SessionAttachmentStore(
            self._workspace_root
        )

    def resolve_content_type(self, attachment: AttachmentRef) -> str:
        return _resolve_attachment_content_type(attachment, self._workspace_root)

    def relative_path(self, attachment: AttachmentRef) -> str:
        if attachment.data_url:
            raise ValueError(
                "Agent 运行阶段不接受 data_url 附件；浏览器上传内容必须先持久化为 "
                f"boxteam-session 定位符: file_id={attachment.file_id!r}"
            )
        if attachment.file_id.startswith(SESSION_ATTACHMENT_SCHEME):
            session_id = attachment.file_id.removeprefix(
                SESSION_ATTACHMENT_SCHEME
            ).split("/", 1)[0]
            return self._attachment_store.relative_path(session_id, attachment.file_id)
        file_path = _resolve_attachment_file(attachment.file_id, self._workspace_root)
        if not file_path.is_file():
            raise FileNotFoundError(f"附件不存在: {file_path}")
        return file_path.relative_to(self._workspace_root).as_posix()

    def image_preview_data_url(self, attachment: AttachmentRef) -> str:
        raw = self._read_attachment(attachment)
        if attachment.file_id.startswith(SESSION_ATTACHMENT_SCHEME):
            session_id = attachment.file_id.removeprefix(
                SESSION_ATTACHMENT_SCHEME
            ).split("/", 1)[0]
            preview = self._attachment_store.read_thumbnail(
                session_id,
                attachment.file_id,
                max_edge=ATTACHMENT_PREVIEW_MAX_EDGE,
            )
            preview_type = preview.content_type
            preview_data = preview.data
        else:
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
        content_type, video_bytes = _read_workspace_attachment_bytes(
            attachment,
            supported_types=SUPPORTED_VIDEO_MIME_TYPES,
            media_name="视频",
            workspace_root=self._workspace_root,
        )
        return _extract_video_frame_data_urls(
            video_bytes=video_bytes,
            content_type=content_type,
            attachment_name=attachment.name or attachment.file_id,
        )

    def _read_attachment(self, attachment: AttachmentRef) -> bytes:
        if attachment.file_id.startswith(SESSION_ATTACHMENT_SCHEME):
            session_id = attachment.file_id.removeprefix(
                SESSION_ATTACHMENT_SCHEME
            ).split("/", 1)[0]
            return self._attachment_store.read(session_id, attachment.file_id).data
        file_path = _resolve_attachment_file(attachment.file_id, self._workspace_root)
        if not file_path.is_file():
            raise FileNotFoundError(f"附件不存在: {file_path}")
        return file_path.read_bytes()


def _resolve_attachment_content_type(
    attachment: AttachmentRef,
    workspace_root: Path | None,
) -> str:
    content_type = attachment.content_type
    if content_type is None:
        file_path = _resolve_attachment_file(attachment.file_id, workspace_root)
        content_type = _attachment_content_type(attachment, file_path)
    return content_type


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
