from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote_to_bytes

from PIL import Image, ImageOps

from app.core.path_utils import get_session_path_resolver, safe_join
from app.schemas.internal_v2.message import AttachmentRef

MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
SUPPORTED_MEDIA_PREFIXES = ("image/", "audio/", "video/")
SESSION_ATTACHMENT_SCHEME = "boxteam-session://"


@dataclass(frozen=True, slots=True)
class StoredAttachmentContent:
    data: bytes
    content_type: str


class SessionAttachmentStore:
    """持久化并读取属于单个会话的媒体附件。"""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self._sessions_root = self._workspace_root / ".boxteam" / "sessions"
        self._path_resolver = get_session_path_resolver(self._sessions_root)

    def persist_inline(
        self,
        session_id: str,
        attachments: list[AttachmentRef],
    ) -> list[AttachmentRef]:
        return [self._persist_one(session_id, attachment) for attachment in attachments]

    def read(self, session_id: str, file_id: str) -> StoredAttachmentContent:
        attachments_root = self._attachments_root(session_id)
        file_path = self._resolve_file_id(session_id, file_id)
        if file_path.parent != attachments_root.resolve():
            raise ValueError("附件路径不属于指定会话")
        if not file_path.is_file():
            raise FileNotFoundError(f"会话附件不存在: {file_id}")
        content_type, _ = mimetypes.guess_type(file_path.name)
        if not content_type or not content_type.startswith(SUPPORTED_MEDIA_PREFIXES):
            raise ValueError(f"无法识别会话媒体附件类型: {file_id}")
        return StoredAttachmentContent(
            data=file_path.read_bytes(),
            content_type=content_type,
        )

    def read_thumbnail(
        self,
        session_id: str,
        file_id: str,
        *,
        max_edge: int = 384,
    ) -> StoredAttachmentContent:
        """读取缓存缩略图；首次请求时从原图派生 WebP。"""
        if max_edge < 64 or max_edge > 1024:
            raise ValueError("图片缩略图 max_edge 必须在 64 到 1024 之间")
        source = self.read(session_id, file_id)
        if not source.content_type.startswith("image/"):
            raise ValueError(f"附件不是图片，无法生成缩略图: {file_id}")

        digest = hashlib.sha256(file_id.encode("utf-8")).hexdigest()
        derived_root = self._attachments_root(session_id) / "derived"
        derived_root.mkdir(parents=True, exist_ok=True)
        target = derived_root / f"{digest}-{max_edge}.webp"
        if not target.is_file():
            with Image.open(BytesIO(source.data)) as image:
                normalized = ImageOps.exif_transpose(image)
                normalized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                if normalized.mode not in {"RGB", "RGBA"}:
                    normalized = normalized.convert(
                        "RGBA" if "A" in normalized.mode else "RGB"
                    )
                output = BytesIO()
                normalized.save(output, format="WEBP", quality=78, method=4)
            self._write_once(target, output.getvalue())
        return StoredAttachmentContent(
            data=target.read_bytes(),
            content_type="image/webp",
        )

    def _persist_one(self, session_id: str, attachment: AttachmentRef) -> AttachmentRef:
        if not attachment.data_url:
            return attachment
        content_type, data = self._parse_data_url(attachment.data_url)
        declared_type = attachment.content_type or content_type
        if declared_type != content_type:
            raise ValueError(
                "附件 content_type 与 data_url MIME 不一致: "
                f"{declared_type!r} != {content_type!r}"
            )
        if not content_type.startswith(SUPPORTED_MEDIA_PREFIXES):
            raise ValueError(f"不支持持久化的媒体附件类型: {content_type!r}")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"媒体附件超过 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB 限制"
            )

        suffix = mimetypes.guess_extension(content_type)
        if not suffix:
            raise ValueError(f"无法根据 MIME 类型确定附件扩展名: {content_type!r}")
        digest = hashlib.sha256(data).hexdigest()
        attachments_root = self._attachments_root(session_id)
        attachments_root.mkdir(parents=True, exist_ok=True)
        target = attachments_root / f"{digest}{suffix}"
        self._write_once(target, data)

        return AttachmentRef(
            file_id=(
                f"{SESSION_ATTACHMENT_SCHEME}{session_id}/attachments/{target.name}"
            ),
            name=attachment.name,
            content_type=content_type,
        )

    def _attachments_root(self, session_id: str) -> Path:
        session_root = self._path_resolver.resolve_session_node(session_id)
        return session_root / "attachments"

    def _resolve_file_id(self, session_id: str, file_id: str) -> Path:
        prefix = f"{SESSION_ATTACHMENT_SCHEME}{session_id}/attachments/"
        if not file_id.startswith(SESSION_ATTACHMENT_SCHEME):
            raise ValueError(f"附件必须使用会话逻辑定位符: {file_id}")
        if not file_id.startswith(prefix):
            raise ValueError("附件逻辑定位符不属于指定会话")
        relative_name = file_id.removeprefix(prefix)
        if not relative_name or "/" in relative_name or "\\" in relative_name:
            raise ValueError(f"附件逻辑定位符格式无效: {file_id}")
        return safe_join(self._attachments_root(session_id), relative_name)

    @staticmethod
    def _write_once(target: Path, data: bytes) -> None:
        if target.exists():
            return
        temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
        temporary.write_bytes(data)
        # TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, target)

    @staticmethod
    def _parse_data_url(data_url: str) -> tuple[str, bytes]:
        if not data_url.startswith("data:"):
            raise ValueError("附件 data_url 必须以 data: 开头")
        header, separator, payload = data_url.partition(",")
        if not separator:
            raise ValueError("附件 data_url 缺少逗号分隔符")
        content_type = header.removeprefix("data:").split(";", 1)[0]
        if not content_type:
            raise ValueError("附件 data_url 缺少 MIME 类型")
        if ";base64" not in header:
            return content_type, unquote_to_bytes(payload)
        try:
            return content_type, base64.b64decode(payload, validate=True)
        except ValueError as error:
            raise ValueError("附件 data_url 包含非法 base64 数据") from error
