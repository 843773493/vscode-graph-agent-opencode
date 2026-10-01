"""通信字段/地址/kind 校验与 preimage 漂移检测(逐字搬迁)。"""

from __future__ import annotations

from app.core.session_catalog_store import validate_session_id, validate_thread_id

from .records import CommunicationInboxRecord, CommunicationOutboxRecord
from .schema import _COMMUNICATION_KINDS


def _validate_communication_text(value: str, *, field: str) -> None:
    """通信自由文本字段：1-256 字符非空字符串（send_operation_id 等）。"""
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(
            f"{field} 必须是 1-256 个字符的非空字符串: {value!r}"
        )


def _validate_communication_address(
    *,
    gateway_id: str,
    workspace_id: str,
    session_id: str,
    thread_id: str,
    prefix: str,
) -> None:
    """通信地址端点校验：gateway/workspace 非空 + canonical ID 形态。"""
    _validate_communication_text(gateway_id, field=f"{prefix}.gateway_id")
    _validate_communication_text(workspace_id, field=f"{prefix}.workspace_id")
    validate_session_id(session_id)
    validate_thread_id(thread_id)


def _validate_communication_kind_and_reply(
    kind: str,
    reply_to_communication_id: str | None,
) -> None:
    """kind 闭集 + reply 字段闭合（reply 必带 reply_to，其它禁带）。"""
    if kind not in _COMMUNICATION_KINDS:
        raise ValueError(
            f"communication kind 非法: {kind!r}（闭集 {_COMMUNICATION_KINDS!r}）"
        )
    if (kind == "reply") != (reply_to_communication_id is not None):
        raise ValueError(
            "reply_to_communication_id 只允许与 kind=reply 同时出现: "
            f"kind={kind!r}, reply_to={reply_to_communication_id!r}"
        )


_OUTBOX_PREIMAGE_FIELDS = (
    "session_id", "source_gateway_id", "source_workspace_id", "source_thread_id",
    "target_gateway_id", "target_workspace_id", "target_session_id",
    "target_thread_id", "kind", "reply_to_communication_id", "payload_hash",
)

_INBOX_PREIMAGE_FIELDS = (
    "session_id", "source_gateway_id", "source_workspace_id", "source_session_id",
    "source_thread_id", "target_thread_id", "kind", "reply_to_communication_id",
    "payload_hash", "admission_id", "wakeup_key",
)


_CommunicationRecord = CommunicationOutboxRecord | CommunicationInboxRecord


def _preimage_mismatches(
    record: _CommunicationRecord,
    fields: tuple[str, ...],
    preimage: dict[str, object],
) -> list[str]:
    """逐字段对比既有行与本次提交的 preimage，返回漂移字段名。"""
    return [f for f in fields if getattr(record, f) != preimage[f]]


def _outbox_preimage_mismatches(
    record: CommunicationOutboxRecord, preimage: dict[str, object]
) -> list[str]:
    """逐字段对比既有 outbox 行与本次提交的 preimage，返回漂移字段名。"""
    return _preimage_mismatches(record, _OUTBOX_PREIMAGE_FIELDS, preimage)


def _inbox_preimage_mismatches(
    record: CommunicationInboxRecord, **preimage: object
) -> list[str]:
    """逐字段对比既有 inbox 行与本次提交的身份字段，返回漂移字段名。"""
    return _preimage_mismatches(record, _INBOX_PREIMAGE_FIELDS, preimage)
