"""通过附件 owner 的冻结引用投影消息；不改写 canonical 正文。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from html import escape
from xml.etree.ElementTree import fromstring

from app.core.resource_uri import parse_session_artifact_uri


class RolloutAttachmentProjection:
    def __init__(
        self,
        attachment_ref_projector: Callable[[str, str, str], dict[str, str]] | None,
    ) -> None:
        self._attachment_ref_projector = attachment_ref_projector

    def project_message_attachments(self, session_id: str, thread_id: str, message: dict[str, object]) -> dict[str, object]:
        """投影附件 owner 冻结的引用和可用性，不改 canonical bytes/hash。"""
        if self._attachment_ref_projector is None:
            return message
        data = message.get("data")
        if not isinstance(data, Mapping):
            return message
        projected = dict(data)
        mappings: dict[str, dict[str, str]] = {}
        for field in ("content", "response_metadata"):
            if field in data:
                projected[field] = self.project_attachment_payload(session_id, thread_id, data[field], mappings=mappings)
        return {**message, "data": projected}

    def project_attachment_payload(
        self, session_id: str, thread_id: str, payload: object, *,
        mappings: dict[str, dict[str, str]] | None = None,
    ) -> object:
        """只投影 typed 附件坐标及系统生成的 block，用户原始正文不参与替换。"""
        if self._attachment_ref_projector is None:
            return payload
        cache = {} if mappings is None else mappings

        def project(file_id: str) -> dict[str, str]:
            if file_id not in cache:
                cache[file_id] = self._attachment_ref_projector(session_id, thread_id, file_id)
            return cache[file_id]

        if isinstance(payload, Mapping):
            projected = dict(payload)
            if "attachments" in payload:
                attachments = payload["attachments"]
                if not isinstance(attachments, list):
                    raise TypeError("message attachments 必须是 typed list")
                mapped = []
                for attachment in attachments:
                    if not isinstance(attachment, Mapping) or not isinstance(attachment.get("file_id"), str):
                        raise TypeError("message attachment projection 必须使用 typed logical file_id")
                    mapped.append({**attachment, **project(attachment["file_id"])})
                projected["attachments"] = mapped
            for field in ("content", "attachment_blocks"):
                if field in payload:
                    projected[field] = self.project_attachment_payload(session_id, thread_id, payload[field], mappings=cache)
            return projected
        if not isinstance(payload, list):
            return payload
        blocks = []
        for block in payload:
            metadata = block.get("metadata") if isinstance(block, Mapping) else None
            if not isinstance(metadata, Mapping) or metadata.get("origin") != "generated" or metadata.get("kind") not in {"attachment_manifest", "attachment_preview"}:
                blocks.append(block)
                continue
            file_id = metadata.get("file_id")
            if not isinstance(file_id, str):
                raise TypeError("generated attachment block 必须有 typed file_id")
            if "://" not in file_id:
                blocks.append(block)
                continue
            if parse_session_artifact_uri(file_id).session_artifact_kind != "attachments":
                raise ValueError("generated attachment block 不是附件资源 URI")
            if metadata.get("schema_version") != 1 or metadata.get("path") != file_id:
                raise ValueError("source-mismatch: generated attachment block 缺少精确 logical reference")
            reference = project(file_id)
            projected_block = {**block, "metadata": {**metadata, **reference, "path": reference["file_id"]}}
            if metadata["kind"] == "attachment_manifest" and reference["file_id"] != file_id:
                text = block.get("text")
                if not isinstance(text, str):
                    raise TypeError("generated attachment manifest 必须有 text")
                element = fromstring(text + "</attachment>")
                if element.tag != "attachment" or element.attrib.get("id") != file_id or element.attrib.get("path") != file_id:
                    raise ValueError("source-mismatch: attachment manifest 与 metadata 不一致")
                element.attrib.update(id=reference["file_id"], path=reference["file_id"])
                attributes = " ".join(f'{key}="{escape(value, quote=True)}"' for key, value in element.attrib.items())
                projected_block["text"] = f"<attachment {attributes}>"
            blocks.append(projected_block)
        return blocks
