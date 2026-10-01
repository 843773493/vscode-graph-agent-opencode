"""target 侧 inbox 写事务:create-or-get、admission 领取/绑定/失败记录与 reply 因果证明。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.session_catalog_store import validate_session_id, validate_thread_id
from app.core.session_control_primitives import (
    EXECUTION_JOB_ID_PATTERN,
    SHA256_HEX_PATTERN,
    validate_claim_fields,
)

from .reads import (
    _INBOX_REPLY_DIRECTION_FIELDS,
    _ensure_reply_direction,
    _fetch_inbox_row,
    _fetch_outbox_row_by_communication,
)
from .records import (
    CommunicationInboxRecord,
    _communication_inbox_from_row,
    derive_communication_admission_identity,
)
from .schema import _COMMUNICATION_ID_PATTERN, _COMMUNICATION_INBOX_COLUMNS
from .validation import (
    _inbox_preimage_mismatches,
    _validate_communication_address,
    _validate_communication_kind_and_reply,
    _validate_communication_text,
)


class InboxWritesMixin:
    """target 侧 inbox 写事务:create-or-get、admission 领取/绑定/失败记录与 reply 因果证明。"""

    def create_or_get_communication_inbox(
        self,
        *,
        session_id: str,
        communication_id: str,
        source_gateway_id: str,
        source_workspace_id: str,
        source_session_id: str,
        source_thread_id: str,
        target_thread_id: str,
        kind: str,
        reply_to_communication_id: str | None,
        payload_hash: str,
    ) -> tuple[CommunicationInboxRecord, bool]:
        """create-or-get target inbox（gate 内短事务调用）。

        - PK = communication_id（target session 命名空间即本库）；同
          communication_id 重试必须逐字段复现全部身份字段，漂移 fail
          closed。
        - main binding：target_thread_id 必须等于 thread_catalog 唯一
          main row（同事务内 fresh 校验，catalog 漂移立即失败）。
        - admission_id/wakeup_key 由 (communication_id, payload_hash)
          确定性派生。
        - kind=reply 要求本库（target session）已存在被回复 communication
          的 outbox 行且方向与本次相反。
        返回 (record, created)。
        """
        validate_session_id(session_id)
        validate_session_id(source_session_id)
        if _COMMUNICATION_ID_PATTERN.fullmatch(communication_id) is None:
            raise ValueError(
                f"communication_id 形态非法: {communication_id!r}"
            )
        _validate_communication_address(
            gateway_id=source_gateway_id,
            workspace_id=source_workspace_id,
            session_id=source_session_id,
            thread_id=source_thread_id,
            prefix="source",
        )
        validate_thread_id(target_thread_id)
        _validate_communication_kind_and_reply(kind, reply_to_communication_id)
        if SHA256_HEX_PATTERN.fullmatch(payload_hash) is None:
            raise ValueError(
                f"payload_hash 必须是 sha256 小写 hex: {payload_hash!r}"
            )
        admission_id, wakeup_key = derive_communication_admission_identity(
            communication_id, payload_hash
        )
        with self._write_transaction() as connection:
            main_row = connection.execute(
                "SELECT thread_id FROM thread_catalog WHERE kind = 'main'"
            ).fetchall()
            if len(main_row) != 1:
                raise RuntimeError(
                    "session control main row 缺失或不唯一，拒绝建立 "
                    f"communication inbox（fail closed）: path={self.database_path}, "
                    f"main_rows={len(main_row)}"
                )
            main_thread_id = str(main_row[0]["thread_id"])
            if main_thread_id != target_thread_id:
                raise RuntimeError(
                    "communication inbox 目标必须解析 main thread（main "
                    f"binding 漂移，fail closed）: session_id={session_id!r}, "
                    f"main_thread_id={main_thread_id!r}, "
                    f"target_thread_id={target_thread_id!r}"
                )
            if kind == "reply":
                row = _fetch_outbox_row_by_communication(
                    connection, str(reply_to_communication_id)
                )
                if row is None:
                    raise RuntimeError(
                        "kind=reply 无法在本 session outbox 中证明被回复 "
                        f"communication（fail closed）: session_id={session_id!r}, "
                        f"reply_to={reply_to_communication_id!r}"
                    )
                _ensure_reply_direction(
                    row,
                    _INBOX_REPLY_DIRECTION_FIELDS,
                    {
                        "session_id": session_id,
                        "source_gateway_id": source_gateway_id,
                        "source_workspace_id": source_workspace_id,
                        "source_session_id": source_session_id,
                        "source_thread_id": source_thread_id,
                        "target_thread_id": target_thread_id,
                    },
                    session_id=session_id,
                    reply_to_communication_id=str(reply_to_communication_id),
                    mismatch_message=(
                        "kind=reply 的被回复 communication 方向与本次方向不一致"
                        "（fail closed）"
                    ),
                )
            existing = _fetch_inbox_row(connection, communication_id)
            if existing is not None:
                record = _communication_inbox_from_row(existing)
                mismatches = _inbox_preimage_mismatches(
                    record,
                    session_id=session_id,
                    source_gateway_id=source_gateway_id,
                    source_workspace_id=source_workspace_id,
                    source_session_id=source_session_id,
                    source_thread_id=source_thread_id,
                    target_thread_id=target_thread_id,
                    kind=kind,
                    reply_to_communication_id=reply_to_communication_id,
                    payload_hash=payload_hash,
                    admission_id=admission_id,
                    wakeup_key=wakeup_key,
                )
                if mismatches:
                    raise RuntimeError(
                        "同 communication_id 的 inbox 重试身份漂移（fail "
                        f"closed）: communication_id={communication_id!r}, "
                        f"漂移字段={mismatches}"
                    )
                return record, False
            now_text = datetime.now(UTC).isoformat()
            connection.execute(
                f"INSERT INTO communication_inbox ({_COMMUNICATION_INBOX_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'target_accepted', NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                (
                    communication_id,
                    session_id,
                    source_gateway_id,
                    source_workspace_id,
                    source_session_id,
                    source_thread_id,
                    target_thread_id,
                    kind,
                    reply_to_communication_id,
                    payload_hash,
                    admission_id,
                    wakeup_key,
                    now_text,
                    now_text,
                ),
            )
            inserted = _fetch_inbox_row(connection, communication_id)
            if inserted is None:
                raise RuntimeError(
                    "communication inbox 插入后不可见（事务异常）: "
                    f"communication_id={communication_id!r}"
                )
            return _communication_inbox_from_row(inserted), True

    def claim_communication_inbox_admission(
        self,
        communication_id: str,
        *,
        claim_owner: str,
        claim_generation: int,
    ) -> CommunicationInboxRecord:
        """领取 inbox admission（worker 并发闸门；契约同初始 execution intent）。

        - 未领取 → 写入 (claim_owner, claim_generation) 并返回；
        - 相同 claim 重入 → 幂等返回；
        - 同 owner 更高 generation → CAS 推进（恢复 owner 接管）；
        - 同 owner 更低 generation 或不同 owner → RuntimeError；
        - state 非 target_accepted → RuntimeError；不存在 → KeyError。
        """
        validate_claim_fields(claim_owner, claim_generation)
        with self._write_transaction() as connection:
            row = _fetch_inbox_row(connection, communication_id)
            if row is None:
                raise KeyError(
                    "communication inbox 不存在，无法领取 admission: "
                    f"communication_id={communication_id!r}"
                )
            state = str(row["state"])
            if state != "target_accepted":
                raise RuntimeError(
                    "communication inbox 非 target_accepted，拒绝领取（fail "
                    f"closed）: communication_id={communication_id!r}, "
                    f"state={state!r}"
                )
            existing_owner = row["admission_claim_owner"]
            existing_generation = row["admission_claim_generation"]
            if existing_owner is None:
                connection.execute(
                    "UPDATE communication_inbox SET "
                    "admission_claim_owner = ?, admission_claim_generation = ?, "
                    "updated_at = ? WHERE communication_id = ?",
                    (
                        claim_owner,
                        claim_generation,
                        datetime.now(UTC).isoformat(),
                        communication_id,
                    ),
                )
            elif str(existing_owner) == claim_owner:
                held_generation = int(existing_generation)
                if claim_generation > held_generation:
                    cursor = connection.execute(
                        "UPDATE communication_inbox SET "
                        "admission_claim_generation = ?, updated_at = ? "
                        "WHERE communication_id = ? AND admission_claim_owner = ? "
                        "AND admission_claim_generation = ?",
                        (
                            claim_generation,
                            datetime.now(UTC).isoformat(),
                            communication_id,
                            claim_owner,
                            held_generation,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "inbox claim generation CAS 推进失败（并发改动，"
                            f"fail closed）: communication_id={communication_id!r}"
                        )
                elif claim_generation < held_generation:
                    raise RuntimeError(
                        "inbox claim generation 过期（不得低于当前持有 "
                        f"generation，fail closed）: communication_id="
                        f"{communication_id!r}, held={held_generation}, "
                        f"requested={claim_generation}"
                    )
            else:
                raise RuntimeError(
                    "communication inbox admission 已被其他 claim 持有（不同 "
                    f"claim 冲突，fail closed）: communication_id="
                    f"{communication_id!r}, held_owner={existing_owner!r}, "
                    f"requested_owner={claim_owner!r}"
                )
            updated = _fetch_inbox_row(connection, communication_id)
            if updated is None:
                raise RuntimeError(
                    "communication inbox claim 后不可见（事务异常）: "
                    f"communication_id={communication_id!r}"
                )
            return _communication_inbox_from_row(updated)

    def mark_communication_inbox_execution_bound(
        self,
        communication_id: str,
        *,
        job_id: str,
        turn_id: str | None,
        claim_owner: str,
        claim_generation: int,
    ) -> CommunicationInboxRecord:
        """CAS target_accepted → execution_bound（admission 绑定提交点）。

        校验 claim 与当前持有 claim 一致；已 bound 且提交 identity 完全
        一致 → 幂等返回（崩溃恢复补写同一 binding 的契约面）；identity
        漂移 → RuntimeError。
        """
        if EXECUTION_JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise ValueError(f"job_id 形态非法: {job_id!r}")
        if turn_id is not None:
            _validate_communication_text(turn_id, field="turn_id")
        validate_claim_fields(claim_owner, claim_generation)
        with self._write_transaction() as connection:
            row = _fetch_inbox_row(connection, communication_id)
            if row is None:
                raise KeyError(
                    "communication inbox 不存在，无法标记 execution_bound: "
                    f"communication_id={communication_id!r}"
                )
            state = str(row["state"])
            if row["admission_claim_owner"] is None:
                raise RuntimeError(
                    "communication inbox 未被领取，拒绝 mark bound: "
                    f"communication_id={communication_id!r}"
                )
            if (
                str(row["admission_claim_owner"]) != claim_owner
                or int(row["admission_claim_generation"]) != claim_generation
            ):
                raise RuntimeError(
                    "mark bound 的 claim 与当前持有 claim 不一致（fail "
                    f"closed）: communication_id={communication_id!r}"
                )
            if state == "execution_bound":
                frozen_turn = (
                    None if row["turn_id"] is None else str(row["turn_id"])
                )
                if str(row["job_id"]) != job_id or frozen_turn != turn_id:
                    raise RuntimeError(
                        "communication inbox 已 execution_bound 且提交 "
                        f"identity 漂移（fail closed）: communication_id="
                        f"{communication_id!r}, frozen_job={row['job_id']!r}, "
                        f"submitted_job={job_id!r}"
                    )
                return _communication_inbox_from_row(row)
            if state != "target_accepted":
                raise RuntimeError(
                    "communication inbox 非 target_accepted，拒绝 mark "
                    f"bound（fail closed）: communication_id={communication_id!r}, "
                    f"state={state!r}"
                )
            cursor = connection.execute(
                "UPDATE communication_inbox SET state = 'execution_bound', "
                "job_id = ?, turn_id = ?, last_error = NULL, updated_at = ? "
                "WHERE communication_id = ? AND state = 'target_accepted'",
                (
                    job_id,
                    turn_id,
                    datetime.now(UTC).isoformat(),
                    communication_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "inbox mark bound CAS 失败（已被并发推进，fail "
                    f"closed）: communication_id={communication_id!r}"
                )
            updated = _fetch_inbox_row(connection, communication_id)
            if updated is None:
                raise RuntimeError(
                    "communication inbox mark bound 后不可见（事务异常）: "
                    f"communication_id={communication_id!r}"
                )
            return _communication_inbox_from_row(updated)

    def record_communication_inbox_admission_failure(
        self,
        communication_id: str,
        *,
        claim_owner: str,
        claim_generation: int,
        last_error: str,
    ) -> CommunicationInboxRecord:
        """记录明确错误并保留 target_accepted 可恢复事实（不推进 state）。"""
        validate_claim_fields(claim_owner, claim_generation)
        # 只校验非空，不设长度上限：本列是诊断真值，全库既有口径是在
        # 展示/传输边界截断（session_information_service._truncate_text
        # 对 last_error 走 _DIAGNOSTIC_TEXT_LIMIT=2048 并带 _truncated
        # 标志；bounded_json 在边界加截断标记），store 层写入不截断以免
        # 丢失可诊断信息。若将来要限长，应统一在边界层做，而不是在此处。
        if not isinstance(last_error, str) or not last_error:
            raise ValueError(f"last_error 不能为空: {last_error!r}")
        with self._write_transaction() as connection:
            row = _fetch_inbox_row(connection, communication_id)
            if row is None:
                raise KeyError(
                    "communication inbox 不存在，无法记录 admission 失败: "
                    f"communication_id={communication_id!r}"
                )
            if str(row["state"]) != "target_accepted":
                raise RuntimeError(
                    "communication inbox 非 target_accepted，无失败可记录（"
                    f"fail closed）: communication_id={communication_id!r}, "
                    f"state={row['state']!r}"
                )
            if (
                row["admission_claim_owner"] is None
                or str(row["admission_claim_owner"]) != claim_owner
                or int(row["admission_claim_generation"]) != claim_generation
            ):
                raise RuntimeError(
                    "record failure 的 claim 与当前持有 claim 不一致（fail "
                    f"closed）: communication_id={communication_id!r}"
                )
            cursor = connection.execute(
                "UPDATE communication_inbox SET last_error = ?, updated_at = ? "
                "WHERE communication_id = ? AND state = 'target_accepted' "
                "AND admission_claim_owner = ? AND admission_claim_generation = ?",
                (
                    last_error,
                    datetime.now(UTC).isoformat(),
                    communication_id,
                    claim_owner,
                    claim_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "inbox record failure CAS 失败（已被并发推进，fail "
                    f"closed）: communication_id={communication_id!r}"
                )
            updated = _fetch_inbox_row(connection, communication_id)
            if updated is None:
                raise RuntimeError(
                    "communication inbox record failure 后不可见（事务异常）: "
                    f"communication_id={communication_id!r}"
                )
            return _communication_inbox_from_row(updated)

