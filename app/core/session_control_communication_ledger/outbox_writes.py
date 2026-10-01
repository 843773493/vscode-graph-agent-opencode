"""source 侧 outbox 写事务:create-or-get、状态 CAS 与 reply 因果证明。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.session_catalog_store import validate_session_id
from app.core.session_control_primitives import SHA256_HEX_PATTERN

from .reads import (
    _OUTBOX_REPLY_DIRECTION_FIELDS,
    _ensure_reply_direction,
    _fetch_inbox_row,
    _fetch_outbox_row_by_communication,
    _fetch_outbox_row_by_operation,
)
from .records import CommunicationOutboxRecord, _communication_outbox_from_row
from .schema import (
    _COMMUNICATION_ID_PATTERN,
    _COMMUNICATION_OUTBOX_COLUMNS,
    _COMMUNICATION_OUTBOX_TERMINAL_STATES,
    _COMMUNICATION_OUTBOX_TRANSITIONS,
)
from .validation import (
    _outbox_preimage_mismatches,
    _validate_communication_address,
    _validate_communication_kind_and_reply,
    _validate_communication_text,
)


class OutboxWritesMixin:
    """source 侧 outbox 写事务:create-or-get、状态 CAS 与 reply 因果证明。"""

    def create_or_get_communication_outbox(
        self,
        *,
        session_id: str,
        send_operation_id: str,
        communication_id: str,
        source_gateway_id: str,
        source_workspace_id: str,
        source_thread_id: str,
        target_gateway_id: str,
        target_workspace_id: str,
        target_session_id: str,
        target_thread_id: str,
        kind: str,
        reply_to_communication_id: str | None,
        payload_hash: str,
    ) -> tuple[CommunicationOutboxRecord, bool]:
        """create-or-get source outbox（gate 内短事务调用）。

        幂等两层（design.md §592/§594）：

        - operation 层：PK send_operation_id。同 operation 重试必须逐字段
          复现 (source, target, kind, reply_to, payload_hash,
          communication_id)，任何漂移 fail closed，不覆盖不重基。
        - communication 层：communication_id UNIQUE。同 communication_id
          绑定不同 operation 时，preimage (source/target/kind/reply_to/
          payload_hash) 完全一致 → dedupe 返回既有行；不一致 → fail
          closed。

        kind=reply 要求本库（source session）已存在被回复 communication
        的 inbox 行且方向相反（target inbox 用同构 outbox 证明）。
        返回 (record, created)。
        """
        validate_session_id(session_id)
        _validate_communication_text(
            send_operation_id, field="send_operation_id"
        )
        if _COMMUNICATION_ID_PATTERN.fullmatch(communication_id) is None:
            raise ValueError(
                f"communication_id 形态非法: {communication_id!r}"
            )
        _validate_communication_address(
            gateway_id=source_gateway_id,
            workspace_id=source_workspace_id,
            session_id=session_id,
            thread_id=source_thread_id,
            prefix="source",
        )
        _validate_communication_address(
            gateway_id=target_gateway_id,
            workspace_id=target_workspace_id,
            session_id=target_session_id,
            thread_id=target_thread_id,
            prefix="target",
        )
        _validate_communication_kind_and_reply(kind, reply_to_communication_id)
        if SHA256_HEX_PATTERN.fullmatch(payload_hash) is None:
            raise ValueError(
                f"payload_hash 必须是 sha256 小写 hex: {payload_hash!r}"
            )
        preimage: dict[str, object] = {
            "session_id": session_id,
            "source_gateway_id": source_gateway_id,
            "source_workspace_id": source_workspace_id,
            "source_thread_id": source_thread_id,
            "target_gateway_id": target_gateway_id,
            "target_workspace_id": target_workspace_id,
            "target_session_id": target_session_id,
            "target_thread_id": target_thread_id,
            "kind": kind,
            "reply_to_communication_id": reply_to_communication_id,
            "payload_hash": payload_hash,
        }
        with self._write_transaction() as connection:
            existing_by_operation = _fetch_outbox_row_by_operation(
                connection, send_operation_id
            )
            if existing_by_operation is not None:
                record = _communication_outbox_from_row(existing_by_operation)
                mismatches = _outbox_preimage_mismatches(record, preimage)
                if mismatches or record.communication_id != communication_id:
                    raise RuntimeError(
                        "同 send_operation_id 的 outbox 重试 preimage 漂移"
                        f"（fail closed）: send_operation_id={send_operation_id!r}, "
                        f"漂移字段={mismatches}, "
                        f"existing_communication_id={record.communication_id!r}, "
                        f"submitted_communication_id={communication_id!r}"
                    )
                return record, False
            existing_by_communication = _fetch_outbox_row_by_communication(
                connection, communication_id
            )
            if existing_by_communication is not None:
                record = _communication_outbox_from_row(
                    existing_by_communication
                )
                mismatches = _outbox_preimage_mismatches(record, preimage)
                if mismatches:
                    raise RuntimeError(
                        "同 communication_id 已绑定不同 preimage（fail "
                        f"closed）: communication_id={communication_id!r}, "
                        f"漂移字段={mismatches}"
                    )
                return record, False
            if kind == "reply":
                row = _fetch_inbox_row(connection, str(reply_to_communication_id))
                if row is None:
                    raise RuntimeError(
                        "kind=reply 无法在本 session inbox 中证明被回复 "
                        f"communication（fail closed）: session_id={session_id!r}, "
                        f"reply_to={reply_to_communication_id!r}"
                    )
                _ensure_reply_direction(
                    row,
                    _OUTBOX_REPLY_DIRECTION_FIELDS,
                    {
                        "session_id": session_id,
                        "source_gateway_id": source_gateway_id,
                        "source_workspace_id": source_workspace_id,
                        "source_thread_id": source_thread_id,
                        "target_gateway_id": target_gateway_id,
                        "target_workspace_id": target_workspace_id,
                        "target_thread_id": target_thread_id,
                    },
                    session_id=session_id,
                    reply_to_communication_id=str(reply_to_communication_id),
                    mismatch_message=(
                        "kind=reply 的被回复 communication 方向与本次 send 相同"
                        "（fail closed）"
                    ),
                )
            now_text = datetime.now(UTC).isoformat()
            connection.execute(
                f"INSERT INTO communication_outbox ({_COMMUNICATION_OUTBOX_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'accepted', NULL, NULL, ?, ?)",
                (
                    send_operation_id,
                    session_id,
                    source_gateway_id,
                    source_workspace_id,
                    source_thread_id,
                    communication_id,
                    target_gateway_id,
                    target_workspace_id,
                    target_session_id,
                    target_thread_id,
                    kind,
                    reply_to_communication_id,
                    payload_hash,
                    now_text,
                    now_text,
                ),
            )
            inserted = _fetch_outbox_row_by_operation(connection, send_operation_id)
            if inserted is None:
                raise RuntimeError(
                    "communication outbox 插入后不可见（事务异常）: "
                    f"send_operation_id={send_operation_id!r}"
                )
            return _communication_outbox_from_row(inserted), True

    def advance_communication_outbox_state(
        self,
        send_operation_id: str,
        *,
        new_state: str,
        receipt_json: str | None = None,
        abort_reason: str | None = None,
    ) -> CommunicationOutboxRecord:
        """CAS 推进 outbox 前向状态（design.md §592 迁移闭集）。

        - target_accepted/execution_bound/terminal 必须携带 receipt JSON；
        - failed/cancelled 必须携带 abort_reason；
        - 两类载荷列互斥：成功态不得夹带 abort_reason，失败态不得夹带
          receipt；非载荷中间态（routing）不得夹带任一载荷；
        - 失败/取消只写 abort_reason，**保留既有受认证 receipt**（行不变量
          “latest_receipt 记录最近一次受认证 target receipt，
          target_accepted 起非空”；design.md §957 要求 terminal 后仍保留
          receipt 验证字段供迟到重试返回原结果或冲突）；
        - 已终态不可再迁移；重复提交相同载荷幂等返回。
        """
        _validate_communication_text(send_operation_id, field="send_operation_id")
        if new_state not in _COMMUNICATION_OUTBOX_TRANSITIONS and (
            new_state not in _COMMUNICATION_OUTBOX_TERMINAL_STATES
        ):
            raise ValueError(f"outbox 新状态非法: {new_state!r}")
        success_state = new_state in (
            "target_accepted", "execution_bound", "terminal"
        )
        failure_state = new_state in ("failed", "cancelled")
        if success_state and receipt_json is None:
            raise ValueError(f"outbox 迁移到 {new_state!r} 必须携带 receipt JSON")
        if failure_state:
            if abort_reason is None:
                raise ValueError(
                    f"outbox 迁移到 {new_state!r} 必须携带 abort_reason"
                )
            if receipt_json is not None:
                raise ValueError(
                    f"outbox 迁移到 {new_state!r} 不得携带 receipt JSON"
                    "（受认证 receipt 由既有行保留，不接受覆盖）"
                )

        with self._write_transaction() as connection:
            row = _fetch_outbox_row_by_operation(connection, send_operation_id)
            if row is None:
                raise KeyError(
                    "communication outbox 不存在，无法推进状态: "
                    f"send_operation_id={send_operation_id!r}"
                )
            record = _communication_outbox_from_row(row)
            if record.state == new_state:
                # 幂等重入必须逐字复现本方法可写的两个载荷列（与 CAS 的
                # SET 子句一一对应）；任一漂移 fail closed，不静默接受。
                # failed/cancelled 只写 abort_reason（receipt 保留既有值），
                # 其余状态 receipt/abort_reason 都必须逐字复现提交值。
                if failure_state:
                    drifted = record.abort_reason != abort_reason
                else:
                    drifted = (
                        record.latest_receipt != receipt_json
                        or record.abort_reason != abort_reason
                    )
                if drifted:
                    raise RuntimeError(
                        "outbox 重复提交同状态但载荷漂移（fail closed）: "
                        f"send_operation_id={send_operation_id!r}, "
                        f"existing_receipt={record.latest_receipt!r}, "
                        f"submitted_receipt={receipt_json!r}, "
                        f"existing_abort_reason={record.abort_reason!r}, "
                        f"submitted_abort_reason={abort_reason!r}"
                    )
                return record
            allowed = _COMMUNICATION_OUTBOX_TRANSITIONS.get(record.state, ())
            if new_state not in allowed:
                raise RuntimeError(
                    "outbox 状态迁移非法（fail closed）: "
                    f"send_operation_id={send_operation_id!r}, "
                    f"current={record.state!r}, requested={new_state!r}, "
                    f"allowed={allowed!r}"
                )
            # 载荷列必须与目标状态匹配：成功态禁带 abort_reason，非载荷中间
            # 态（routing）两列都不得夹带。状态已一致的重入走上方幂等分支，
            # 保持既有 RuntimeError「载荷漂移」契约不变。
            if success_state and abort_reason is not None:
                raise ValueError(
                    f"outbox 迁移到 {new_state!r} 不得携带 abort_reason"
                    "（abort_reason 只属于 failed|cancelled）"
                )
            if not success_state and not failure_state and (
                receipt_json is not None or abort_reason is not None
            ):
                raise ValueError(
                    f"outbox 迁移到 {new_state!r} 不接受 receipt/"
                    "abort_reason 载荷"
                )
            # 失败/取消只写 abort_reason，保留既有受认证 receipt（行不变量：
            # latest_receipt 记录最近一次受认证 target receipt，
            # target_accepted 起非空）；成功态只写 receipt；非载荷中间态两列
            # 都为空。载荷列与目标状态的合法性已在上方统一校验。
            if failure_state:
                target_receipt = record.latest_receipt
                target_abort_reason: str | None = abort_reason
            elif success_state:
                target_receipt = receipt_json
                target_abort_reason = None
            else:
                target_receipt = None
                target_abort_reason = None
            cursor = connection.execute(
                "UPDATE communication_outbox SET state = ?, "
                "latest_receipt = ?, abort_reason = ?, updated_at = ? "
                "WHERE send_operation_id = ? AND state = ?",
                (
                    new_state,
                    target_receipt,
                    target_abort_reason,
                    datetime.now(UTC).isoformat(),
                    send_operation_id,
                    record.state,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "outbox 状态 CAS 失败（并发推进，fail closed）: "
                    f"send_operation_id={send_operation_id!r}"
                )
            updated = _fetch_outbox_row_by_operation(connection, send_operation_id)
            if updated is None:
                raise RuntimeError(
                    "communication outbox 推进后不可见（事务异常）: "
                    f"send_operation_id={send_operation_id!r}"
                )
            return _communication_outbox_from_row(updated)

