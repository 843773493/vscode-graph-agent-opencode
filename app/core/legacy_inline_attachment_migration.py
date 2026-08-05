from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote_to_bytes

SUPPORTED_MEDIA_PREFIXES = ("image/", "audio/", "video/")
SESSION_ATTACHMENT_SCHEME = "boxteam-session://"


def materialize_legacy_inline_attachments(
    *,
    session_id: str,
    session_path: Path,
) -> dict[str, str]:
    """从历史请求日志物化旧 inline 附件，返回旧 ID 到稳定定位符的映射。"""
    candidates = _collect_candidates(session_path)
    attachments_root = session_path / "attachments"
    locators: dict[str, str] = {}
    for file_id, variants in candidates.items():
        if len(variants) != 1:
            raise RuntimeError(
                f"旧会话附件存在多个不同内容，拒绝猜测: file_id={file_id!r}"
            )
        declared_type, data_url = next(iter(variants))
        content_type, data = _parse_data_url(data_url)
        if declared_type != content_type:
            raise RuntimeError(
                "旧会话附件日志中的 MIME 类型不一致: "
                f"file_id={file_id!r}, declared={declared_type!r}, "
                f"actual={content_type!r}"
            )
        if not content_type.startswith(SUPPORTED_MEDIA_PREFIXES):
            raise RuntimeError(
                f"旧会话附件类型不受支持: file_id={file_id!r}, type={content_type!r}"
            )
        suffix = mimetypes.guess_extension(content_type)
        if not suffix:
            raise RuntimeError(
                f"无法确定旧会话附件扩展名: file_id={file_id!r}, type={content_type!r}"
            )
        digest = hashlib.sha256(data).hexdigest()
        target = attachments_root / f"{digest}{suffix}"
        _write_once(target, data)
        locators[file_id] = (
            f"{SESSION_ATTACHMENT_SCHEME}{session_id}/attachments/{target.name}"
        )
    return locators


def _collect_candidates(session_path: Path) -> dict[str, set[tuple[str, str]]]:
    logs_root = session_path / "logs" / "llm_requests"
    if not logs_root.is_dir():
        return {}
    candidates: dict[str, set[tuple[str, str]]] = {}
    for log_path in sorted(logs_root.glob("*.json")):
        raw = json.loads(log_path.read_text(encoding="utf-8"))
        request = raw.get("request") if isinstance(raw, dict) else None
        messages = request.get("messages") if isinstance(request, dict) else None
        if not isinstance(messages, list):
            continue
        for message in messages:
            for file_id, content_type, data_url in _message_candidates(message):
                candidates.setdefault(file_id, set()).add((content_type, data_url))
    return candidates


def _message_candidates(message: object) -> list[tuple[str, str, str]]:
    if not isinstance(message, dict):
        return []
    metadata = message.get("response_metadata")
    attachments = metadata.get("attachments") if isinstance(metadata, dict) else None
    content = message.get("content")
    if not isinstance(attachments, list) or not isinstance(content, list):
        return []
    image_attachments = [
        attachment
        for attachment in attachments
        if isinstance(attachment, dict)
        and isinstance(attachment.get("file_id"), str)
        and str(attachment["file_id"]).startswith("inline:")
        and isinstance(attachment.get("content_type"), str)
        and str(attachment["content_type"]).startswith("image/")
    ]
    image_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    results: list[tuple[str, str, str]] = []
    for index, attachment in enumerate(image_attachments):
        file_id = str(attachment["file_id"])
        if index >= len(image_blocks):
            raise RuntimeError(f"旧会话附件缺少对应图片块: file_id={file_id!r}")
        image_url = image_blocks[index].get("image_url")
        data_url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
            raise RuntimeError(f"旧会话附件图片块不是 data URL: file_id={file_id!r}")
        results.append((file_id, str(attachment["content_type"]), data_url))
    return results


def _parse_data_url(data_url: str) -> tuple[str, bytes]:
    if not data_url.startswith("data:"):
        raise RuntimeError("旧会话附件 data URL 必须以 data: 开头")
    header, separator, payload = data_url.partition(",")
    if not separator:
        raise RuntimeError("旧会话附件 data URL 缺少逗号分隔符")
    content_type = header.removeprefix("data:").split(";", 1)[0]
    if not content_type:
        raise RuntimeError("旧会话附件 data URL 缺少 MIME 类型")
    if ";base64" not in header:
        return content_type, unquote_to_bytes(payload)
    try:
        return content_type, base64.b64decode(payload, validate=True)
    except ValueError as error:
        raise RuntimeError("旧会话附件 data URL 包含非法 base64 数据") from error


def _write_once(target: Path, data: bytes) -> None:
    if target.exists():
        if target.read_bytes() != data:
            raise RuntimeError(f"附件摘要路径内容冲突: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        # TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
