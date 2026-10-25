"""附件引用、读取与预览变体等基础设施能力。

上传附件统一由工作区目录 owner 管理，目录与文件使用规范资源 URI，
物理 locator 仅由 catalog 解析。旧 Session 附件 URI 通过迁移来源绑定读取。
普通文件附件接受原生路径或 files URI，经同一文件 owner 解析后，
只向模型和持久化层提供 URI。
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

from app.core.atomic_fs import native_io_path
from app.core.path_utils import get_workspace_root
from app.core.resource_uri import (
    parse_resource_uri,
    resource_display_uri,
)
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

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
# 视频抽帧必须在有界时间内返回：该路径在事件循环线程内同步执行（runner 在
# 协程中直接调用 UserContentBuilder.build），无界子进程会连同任务取消一起卡死。
VIDEO_FRAME_TIMEOUT_SECONDS = 30


def _session_id_from_file_id(file_id: str) -> str | None:
    """从逻辑引用取出 owner session id（形态非法即失败）。"""
    parsed = parse_resource_uri(file_id)
    if parsed.kind == "attachments":
        return None
    if parsed.session_artifact_kind != "attachments":
        raise ValueError("附件引用不是附件 owner 的单文件资源")
    return parsed.tail_segments[0]


def _is_managed_attachment(file_id: str) -> bool:
    if not file_id.startswith("boxteam://"):
        return False
    parsed = parse_resource_uri(file_id)
    return parsed.kind == "attachments" or parsed.session_artifact_kind == "attachments"


def _attachment_content_type(attachment: AttachmentRef, file_path: Path, uri: str) -> str:
    if attachment.content_type:
        return attachment.content_type
    guessed_type, _ = mimetypes.guess_type(str(file_path))
    if guessed_type:
        return guessed_type
    raise ValueError(
        f"无法识别附件 MIME 类型: file_id={uri!r}。"
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
    encoded = base64.b64encode(native_io_path(frame_path).read_bytes()).decode("ascii")
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

    def resolve_reference(self, attachment: AttachmentRef) -> AttachmentRef:
        """新输入在进入 DTO、Job 和 canonical 前冻结为同一逻辑引用。"""
        if _is_managed_attachment(attachment.file_id):
            self._reject_data_url(attachment)
            session_id = _session_id_from_file_id(attachment.file_id)
            return self._attachment_store.describe_reference(attachment, session_id=session_id)
        resolved, _file_path = self._resolve_workspace_attachment(attachment)
        return resolved

    @staticmethod
    def _reject_data_url(attachment: AttachmentRef) -> None:
        if attachment.data_url:
            raise ValueError(
                "Agent 运行阶段不接受 data_url 附件；浏览器上传内容必须先持久化为 "
                "资源 URI"
            )

    def _resolve_workspace_attachment(
        self,
        attachment: AttachmentRef,
    ) -> tuple[AttachmentRef, Path]:
        self._reject_data_url(attachment)
        if attachment.directory_uri is not None:
            raise ValueError("普通文件附件不能伪造受管附件目录身份")
        file_path, uri = self._workspace_file(attachment.file_id)
        resolved = attachment.model_copy(
            update={
                "file_id": uri,
                "directory_uri": None,
                "name": attachment.name or file_path.name,
                "content_type": _attachment_content_type(attachment, file_path, uri),
            }
        )
        return resolved, file_path

    def _workspace_file(self, file_id: str) -> tuple[Path, str]:
        paths = WorkspaceFilePaths(
            workspace=self._workspace_root,
            workspace_id=read_workspace_id(self._workspace_root),
            bindings=None,
        )
        file_path, uri = paths.resolve(file_id, allow_empty=False, allow_external=False)
        if not native_io_path(file_path).is_file():
            raise FileNotFoundError(f"附件不存在或不是文件: {uri}")
        parsed = parse_resource_uri(uri)
        return file_path, resource_display_uri(
            scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
            tail_segments=parsed.tail_segments,
            gateway_authority=get_current_gateway_id(),
        )

    def _read_workspace_attachment(self, attachment: AttachmentRef) -> bytes:
        file_path, uri = self._workspace_file(attachment.file_id)
        try:
            return native_io_path(file_path).read_bytes()
        except OSError as error:
            # 对外用精确资源身份和 errno 报错；原异常保留在服务端因果链中。
            raise OSError(error.errno, error.strerror, uri) from error

    def image_preview_data_url(self, attachment: AttachmentRef) -> str:
        if _is_managed_attachment(attachment.file_id):
            session_id = _session_id_from_file_id(attachment.file_id)
            preview = self._attachment_store.read_thumbnail(
                session_id,
                attachment.file_id,
                max_edge=ATTACHMENT_PREVIEW_MAX_EDGE,
            )
            preview_type = preview.content_type
            preview_data = preview.data
        else:
            file_path, uri = self._workspace_file(attachment.file_id)
            try:
                image_stream = native_io_path(file_path).open("rb")
            except OSError as error:
                raise OSError(error.errno, error.strerror, uri) from error
            with image_stream, Image.open(image_stream) as image:
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
        if _is_managed_attachment(attachment.file_id):
            content_type, video_source = self._read_attachment(attachment)
            attachment_uri = attachment.file_id
        else:
            resolved, video_source = self._resolve_workspace_attachment(attachment)
            assert resolved.content_type is not None
            content_type = resolved.content_type
            attachment_uri = resolved.file_id
        if content_type not in SUPPORTED_VIDEO_MIME_TYPES:
            raise ValueError(
                "不支持的视频附件类型: "
                f"{content_type!r}，file_id={attachment.file_id!r}"
            )
        return _extract_video_frame_data_urls(
            video_source=video_source,
            content_type=content_type,
            attachment_name=attachment.name or attachment.file_id,
            attachment_uri=attachment_uri,
        )

    def _read_session_attachment(self, attachment: AttachmentRef):
        session_id = _session_id_from_file_id(attachment.file_id)
        return self._attachment_store.read(session_id, attachment.file_id)

    def _read_attachment(self, attachment: AttachmentRef) -> tuple[str, bytes]:
        if _is_managed_attachment(attachment.file_id):
            stored = self._read_session_attachment(attachment)
            return stored.content_type, stored.data
        resolved = self.resolve_reference(attachment)
        assert resolved.content_type is not None
        return resolved.content_type, self._read_workspace_attachment(resolved)


def _extract_video_frame_data_urls(
    *,
    video_source: bytes | Path,
    content_type: str,
    attachment_name: str,
    attachment_uri: str,
) -> list[str]:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("处理视频附件需要系统安装 ffmpeg，并确保它位于 PATH 中")

    with tempfile.TemporaryDirectory(prefix="boxteam-video-") as temp_dir:
        temp_path = Path(temp_dir)
        input_path = temp_path / f"input{_file_suffix_for_content_type(content_type)}"
        if isinstance(video_source, Path):
            try:
                shutil.copyfile(
                    native_io_path(video_source),
                    native_io_path(input_path),
                )
            except OSError as error:
                detail = error.strerror or type(error).__name__
                raise OSError(
                    error.errno,
                    f"视频附件暂存失败: uri={attachment_uri}, "
                    f"error={type(error).__name__}, detail={detail}",
                    attachment_uri,
                ) from error
        else:
            native_io_path(input_path).write_bytes(video_source)
        frame_pattern = temp_path / "frame-%03d.jpg"
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            input_path.name,
            "-vf",
            f"fps={VIDEO_FRAME_FPS},scale={VIDEO_FRAME_WIDTH}:-2",
            "-frames:v",
            str(VIDEO_FRAME_COUNT),
            "-q:v",
            "3",
            frame_pattern.name,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=VIDEO_FRAME_TIMEOUT_SECONDS,
                cwd=temp_path,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"视频附件 {attachment_name!r} 抽帧在 "
                f"{VIDEO_FRAME_TIMEOUT_SECONDS} 秒内未完成，已终止"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "无 ffmpeg 输出"
            raise RuntimeError(f"视频附件 {attachment_name!r} 抽帧失败: {detail}")

        frame_paths = sorted(temp_path.glob("frame-*.jpg"))
        if not frame_paths:
            raise RuntimeError(f"视频附件 {attachment_name!r} 未能抽取任何关键帧")
        return [_frame_data_url(path) for path in frame_paths]
