"""通信记录身份派生、不可变行投影 DTO 与行适配器(逐字搬迁)。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import Any


def derive_communication_admission_identity(
    communication_id: str,
    payload_hash: str,
) -> tuple[str, str]:
    """由 (communication_id, payload_hash) 确定性派生 admission identity。

    返回 (admission_id, wakeup_key) 二元组：同 communication 同 payload
    重试必然得到同 identity（不使用随机数、不依赖进程内存）；payload
    漂移时 identity 随之改变，与 preimage 冲突检查共同构成双保险。
    """
    digest = hashlib.sha256(
        f"communication-admission|{communication_id}|{payload_hash}".encode()
    ).hexdigest()[:32]
    return f"cadm_{digest}", f"cwake_{digest}"


@dataclass(frozen=True, slots=True)
class CommunicationOutboxRecord:
    """communication_outbox 表行的不可变投影（D5 通信 outbox）。

    state 闭集为 accepted/routing/target_accepted/execution_bound/
    terminal/failed/cancelled；创建流只写 accepted，前向迁移由
    advance_communication_outbox_state CAS 推进。latest_receipt 保存
    最近一次受认证 target receipt（JSON 文本，target_accepted 起非空）；
    abort_reason 仅 failed|cancelled 携带。
    """

    send_operation_id: str
    session_id: str
    source_gateway_id: str
    source_workspace_id: str
    source_thread_id: str
    communication_id: str
    target_gateway_id: str
    target_workspace_id: str
    target_session_id: str
    target_thread_id: str
    kind: str
    reply_to_communication_id: str | None
    payload_hash: str
    state: str
    latest_receipt: str | None
    abort_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CommunicationInboxRecord:
    """communication_inbox 表行的不可变投影（D5 通信 inbox）。

    state 闭集为 target_accepted/execution_bound/terminal/failed/
    cancelled；admission_id/wakeup_key 由 (communication_id,
    payload_hash) 确定性派生（重试不变）。admission_claim_owner/
    admission_claim_generation 是可恢复领取字段（无 TTL、不自动丢弃）；
    last_error 只记录明确错误，state 保持 target_accepted 可恢复。
    """

    communication_id: str
    session_id: str
    source_gateway_id: str
    source_workspace_id: str
    source_session_id: str
    source_thread_id: str
    target_thread_id: str
    kind: str
    reply_to_communication_id: str | None
    payload_hash: str
    admission_id: str
    wakeup_key: str
    state: str
    job_id: str | None
    turn_id: str | None
    admission_claim_owner: str | None
    admission_claim_generation: int | None
    last_error: str | None
    terminal_outcome: str | None
    created_at: str
    updated_at: str


# 行投影字段表：列名与 dataclass 字段名同名，故只列「需要非 str 还原」的列；
# 其余非空列统一 str()，空列统一 None（与原逐字投影语义一致）。
_OUTBOX_INT_FIELDS: frozenset[str] = frozenset()
_INBOX_INT_FIELDS = frozenset({"admission_claim_generation"})


def _row_to_field_values(
    record_type: type, row: sqlite3.Row, *, int_fields: frozenset[str]
) -> dict[str, object]:
    """按 dataclass 字段表还原行投影：空列 None，int 列 int()，其余 str()。"""
    values: dict[str, object] = {}
    for field in dataclass_fields(record_type):
        value: Any = row[field.name]
        if value is None:
            values[field.name] = None
        elif field.name in int_fields:
            values[field.name] = int(value)
        else:
            values[field.name] = str(value)
    return values


def _communication_outbox_from_row(row: sqlite3.Row) -> CommunicationOutboxRecord:
    """communication_outbox 行投影（无字段解释，读取即冻结视图）。"""
    values = _row_to_field_values(CommunicationOutboxRecord, row, int_fields=_OUTBOX_INT_FIELDS)
    return CommunicationOutboxRecord(**values)


def _communication_inbox_from_row(row: sqlite3.Row) -> CommunicationInboxRecord:
    """communication_inbox 行投影（claim_generation 恢复为 int|None）。"""
    values = _row_to_field_values(CommunicationInboxRecord, row, int_fields=_INBOX_INT_FIELDS)
    return CommunicationInboxRecord(**values)
