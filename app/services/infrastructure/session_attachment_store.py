from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes

from app.core.path_utils import safe_join
from app.schemas.public_v2.message import AttachmentRef


MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
SUPPORTED_MEDIA_PREFIXES = ("image/", "audio/", "video/")


@dataclass(frozen=True, slots=True)
class StoredAttachmentContent:
    data: bytes
    content_type: str


class SessionAttachmentStore:
    """持久化并读取属于单个会话的媒体附件。"""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self._sessions_root = self._workspace_root / ".boxteam" / "sessions"

    def persist_inline(
        self,
        session_id: str,
        attachments: list[AttachmentRef],
    ) -> list[AttachmentRef]:
        return [self._persist_one(session_id, attachment) for attachment in attachments]

    def read(self, session_id: str, file_id: str) -> StoredAttachmentContent:
        if file_id.startswith("inline:"):
            return self._read_legacy_inline(session_id, file_id)
        attachments_root = self._attachments_root(session_id)
        file_path = safe_join(self._workspace_root, file_id)
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

    def _read_legacy_inline(
        self,
        session_id: str,
        file_id: str,
    ) -> StoredAttachmentContent:
        cache_prefix = f"legacy-{hashlib.sha256(file_id.encode('utf-8')).hexdigest()}"
        cached_files = list(self._attachments_root(session_id).glob(f"{cache_prefix}.*"))
        if len(cached_files) > 1:
            raise RuntimeError(f"旧附件恢复缓存不唯一: file_id={file_id!r}")
        if cached_files:
            return self._read_media_file(cached_files[0])

        matches = self._legacy_inline_matches(session_id, file_id)
        if not matches:
            raise FileNotFoundError(
                f"旧会话附件内容不存在，LLM 请求日志中也无法恢复: {file_id}"
            )
        unique_data_urls = {(content_type, data_url) for content_type, data_url in matches}
        if len(unique_data_urls) != 1:
            raise ValueError(f"旧会话附件存在多个不同内容，拒绝猜测: {file_id}")
        content_type, data_url = unique_data_urls.pop()
        parsed_type, data = self._parse_data_url(data_url)
        if parsed_type != content_type:
            raise ValueError(
                "旧会话附件日志中的 MIME 类型不一致: "
                f"{content_type!r} != {parsed_type!r}"
            )
        suffix = mimetypes.guess_extension(content_type)
        if not suffix:
            raise ValueError(f"无法确定旧会话附件扩展名: {content_type!r}")
        cache_root = self._attachments_root(session_id)
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_path = cache_root / f"{cache_prefix}{suffix}"
        self._write_once(cache_path, data)
        return StoredAttachmentContent(data=data, content_type=content_type)

    def _legacy_inline_matches(
        self,
        session_id: str,
        file_id: str,
    ) -> list[tuple[str, str]]:
        logs_root = safe_join(self._sessions_root, session_id) / "logs" / "llm_requests"
        if not logs_root.is_dir():
            return []
        matches: list[tuple[str, str]] = []
        for log_path in sorted(logs_root.glob("*.json")):
            raw = json.loads(log_path.read_text(encoding="utf-8"))
            request = raw.get("request")
            messages = request.get("messages") if isinstance(request, dict) else None
            if not isinstance(messages, list):
                continue
            for message in messages:
                match = self._match_legacy_message_attachment(message, file_id)
                if match is not None:
                    matches.append(match)
        return matches

    @staticmethod
    def _match_legacy_message_attachment(
        message: object,
        file_id: str,
    ) -> tuple[str, str] | None:
        if not isinstance(message, dict):
            return None
        metadata = message.get("response_metadata")
        attachments = metadata.get("attachments") if isinstance(metadata, dict) else None
        content = message.get("content")
        if not isinstance(attachments, list) or not isinstance(content, list):
            return None
        image_attachments = [
            attachment
            for attachment in attachments
            if isinstance(attachment, dict)
            and isinstance(attachment.get("content_type"), str)
            and attachment["content_type"].startswith("image/")
        ]
        image_blocks = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "image_url"
        ]
        for index, attachment in enumerate(image_attachments):
            if attachment.get("file_id") != file_id:
                continue
            if index >= len(image_blocks):
                raise ValueError(f"旧会话附件缺少对应图片块: {file_id}")
            image_url = image_blocks[index].get("image_url")
            data_url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
                raise ValueError(f"旧会话附件图片块不是 data URL: {file_id}")
            return str(attachment["content_type"]), data_url
        return None

    @staticmethod
    def _read_media_file(file_path: Path) -> StoredAttachmentContent:
        content_type, _ = mimetypes.guess_type(file_path.name)
        if not content_type or not content_type.startswith(SUPPORTED_MEDIA_PREFIXES):
            raise ValueError(f"无法识别会话媒体附件类型: {file_path}")
        return StoredAttachmentContent(
            data=file_path.read_bytes(),
            content_type=content_type,
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
            file_id=target.relative_to(self._workspace_root).as_posix(),
            name=attachment.name,
            content_type=content_type,
        )

    def _attachments_root(self, session_id: str) -> Path:
        session_root = safe_join(self._sessions_root, session_id)
        return session_root / "attachments"

    @staticmethod
    def _write_once(target: Path, data: bytes) -> None:
        if target.exists():
            return
        temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
        temporary.write_bytes(data)
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
