"""per-session session-control.sqlite 的跨 Session 通信 ledger owner。

本模块承载 outbox/inbox 这一条垂直链路的唯一实现（D5，2.4/4.7）：

- ``communication_outbox`` / ``communication_inbox`` 表 DDL、
  target_accepted 覆盖索引、列清单与行投影；
- source 侧 create-or-get outbox（operation 层 PK 幂等 + communication 层
  UNIQUE dedupe，任何 preimage 漂移 fail closed）与前向状态 CAS 迁移；
- target 侧 create-or-get inbox（main binding fresh 校验、admission
  identity 确定性派生）、admission 领取、execution bound 与失败记录；
- kind=reply 的双端因果证明（各自在本库对方表里要求方向相反的行）。

CommunicationLedgerMixin 由 app.core.session_control_store.SessionControlStore
继承装配；本模块只依赖宿主类提供的 database_path、_connection、_ensure_open()
与 _write_transaction()，不感知 thread catalog / creation record / execution
intent / operation lease / owner binding 等其它控制库职责。错误分类沿用
session_control_store 约定：KeyError 目标行缺失、RuntimeError 库被外部改动或
CAS 冲突、ValueError 输入形态非法、TypeError 输入类型错误。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.session_catalog_store import (
    validate_session_id,
    validate_thread_id,
)
from app.core.session_control_primitives import (
    EXECUTION_JOB_ID_PATTERN,
    SHA256_HEX_PATTERN,
    validate_claim_fields,
)

__all__ = [
    "COMMUNICATION_INBOX_TABLE_DDL",
    "COMMUNICATION_OUTBOX_TABLE_DDL",
    "IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL",
    "CommunicationInboxRecord",
    "CommunicationLedgerMixin",
    "CommunicationOutboxRecord",
    "derive_communication_admission_identity",
]


# 跨 Session 通信 outbox（2.4/4.7，D5）：source thread node 持久化的
# CommunicationOutboxRecord。幂等两层（design.md §592/§594）：PK =
# send_operation_id（operation 层 create-or-get）；communication_id
# UNIQUE（communication 层 dedupe）。同 operation 不同 (source, target,
# payload) 或同 communication_id 不同 preimage 一律 fail closed，不覆盖
# 不重基。payload_hash 为 sha256 小写 hex（target+content+kind+reply_to
# preimage 的数据库内形态，facade 边界负责与 typed 合同的 sha256: 前缀
# 互转）。
COMMUNICATION_OUTBOX_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS communication_outbox (
    send_operation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_gateway_id TEXT NOT NULL,
    source_workspace_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    communication_id TEXT NOT NULL UNIQUE,
    target_gateway_id TEXT NOT NULL,
    target_workspace_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('question', 'reply', 'progress', 'result')),
    reply_to_communication_id TEXT,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'accepted', 'routing', 'target_accepted', 'execution_bound',
        'terminal', 'failed', 'cancelled')),
    latest_receipt TEXT,
    abort_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((kind = 'reply') = (reply_to_communication_id IS NOT NULL))
)
"""


# 跨 Session 通信 inbox（2.4/4.7，D5）：target main-thread node 持久化的
# CommunicationInboxRecord。PK = communication_id（target session 命名
# 空间即本库）；admission_id/wakeup_key 由 (communication_id,
# payload_hash) 确定性派生（重试不变）。state 闭集
# target_accepted → execution_bound → terminal，任一非终态可进
# failed|cancelled；claim owner/generation 是可恢复领取字段（无 TTL）。
COMMUNICATION_INBOX_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS communication_inbox (
    communication_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_gateway_id TEXT NOT NULL,
    source_workspace_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('question', 'reply', 'progress', 'result')),
    reply_to_communication_id TEXT,
    payload_hash TEXT NOT NULL,
    admission_id TEXT NOT NULL UNIQUE,
    wakeup_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'target_accepted', 'execution_bound', 'terminal', 'failed', 'cancelled')),
    job_id TEXT,
    turn_id TEXT,
    admission_claim_owner TEXT,
    admission_claim_generation INTEGER,
    last_error TEXT,
    terminal_outcome TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((kind = 'reply') = (reply_to_communication_id IS NOT NULL)),
    CHECK ((admission_claim_owner IS NULL) = (admission_claim_generation IS NULL)),
    CHECK (admission_claim_generation IS NULL OR admission_claim_generation >= 1),
    CHECK (job_id IS NULL OR state IN ('execution_bound', 'terminal'))
)
"""

# worker 只消费状态索引（2.4/4.7）：target_accepted 未绑定列表的覆盖索引。
IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_communication_inbox_target_accepted "
    "ON communication_inbox(state) WHERE state = 'target_accepted'"
)


_COMMUNICATION_OUTBOX_COLUMNS = (
    "send_operation_id, session_id, source_gateway_id, source_workspace_id, "
    "source_thread_id, communication_id, target_gateway_id, "
    "target_workspace_id, target_session_id, target_thread_id, kind, "
    "reply_to_communication_id, payload_hash, state, latest_receipt, "
    "abort_reason, created_at, updated_at"
)

_COMMUNICATION_INBOX_COLUMNS = (
    "communication_id, session_id, source_gateway_id, source_workspace_id, "
    "source_session_id, source_thread_id, target_thread_id, kind, "
    "reply_to_communication_id, payload_hash, admission_id, wakeup_key, "
    "state, job_id, turn_id, admission_claim_owner, "
    "admission_claim_generation, last_error, terminal_outcome, "
    "created_at, updated_at"
)


# 跨 Session 通信 identity 形态（D5，软件生成/派生、重试不变）：
# communication_id 由 send owner 分配（comm_ + 32 hex）；admission_id 与
# wakeup_key 由 (communication_id, payload_hash) 确定性派生。
_COMMUNICATION_ID_PATTERN = re.compile(r"^comm_[0-9a-f]{32}$")


# communication kind 闭集（design.md §602）。
_COMMUNICATION_KINDS = ("question", "reply", "progress", "result")

# outbox 前向迁移闭集（design.md §592）：accepted → routing →
# target_accepted → execution_bound → terminal；任一非终态可进
# failed|cancelled（带原因）。已终态不可再迁移。
_COMMUNICATION_OUTBOX_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "accepted": ("routing", "failed", "cancelled"),
    "routing": ("target_accepted", "failed", "cancelled"),
    "target_accepted": ("execution_bound", "failed", "cancelled"),
    "execution_bound": ("terminal", "failed", "cancelled"),
}
_COMMUNICATION_OUTBOX_TERMINAL_STATES = ("terminal", "failed", "cancelled")


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


def _communication_outbox_from_row(row: sqlite3.Row) -> CommunicationOutboxRecord:
    """communication_outbox 行投影（无字段解释，读取即冻结视图）。"""
    return CommunicationOutboxRecord(
        send_operation_id=str(row["send_operation_id"]),
        session_id=str(row["session_id"]),
        source_gateway_id=str(row["source_gateway_id"]),
        source_workspace_id=str(row["source_workspace_id"]),
        source_thread_id=str(row["source_thread_id"]),
        communication_id=str(row["communication_id"]),
        target_gateway_id=str(row["target_gateway_id"]),
        target_workspace_id=str(row["target_workspace_id"]),
        target_session_id=str(row["target_session_id"]),
        target_thread_id=str(row["target_thread_id"]),
        kind=str(row["kind"]),
        reply_to_communication_id=(
            None if row["reply_to_communication_id"] is None
            else str(row["reply_to_communication_id"])
        ),
        payload_hash=str(row["payload_hash"]),
        state=str(row["state"]),
        latest_receipt=(
            None if row["latest_receipt"] is None else str(row["latest_receipt"])
        ),
        abort_reason=(
            None if row["abort_reason"] is None else str(row["abort_reason"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _communication_inbox_from_row(row: sqlite3.Row) -> CommunicationInboxRecord:
    """communication_inbox 行投影（claim_generation 恢复为 int|None）。"""
    claim_generation = row["admission_claim_generation"]
    return CommunicationInboxRecord(
        communication_id=str(row["communication_id"]),
        session_id=str(row["session_id"]),
        source_gateway_id=str(row["source_gateway_id"]),
        source_workspace_id=str(row["source_workspace_id"]),
        source_session_id=str(row["source_session_id"]),
        source_thread_id=str(row["source_thread_id"]),
        target_thread_id=str(row["target_thread_id"]),
        kind=str(row["kind"]),
        reply_to_communication_id=(
            None if row["reply_to_communication_id"] is None
            else str(row["reply_to_communication_id"])
        ),
        payload_hash=str(row["payload_hash"]),
        admission_id=str(row["admission_id"]),
        wakeup_key=str(row["wakeup_key"]),
        state=str(row["state"]),
        job_id=None if row["job_id"] is None else str(row["job_id"]),
        turn_id=None if row["turn_id"] is None else str(row["turn_id"]),
        admission_claim_owner=(
            None if row["admission_claim_owner"] is None
            else str(row["admission_claim_owner"])
        ),
        admission_claim_generation=(
            None if claim_generation is None else int(claim_generation)
        ),
        last_error=None if row["last_error"] is None else str(row["last_error"]),
        terminal_outcome=(
            None if row["terminal_outcome"] is None
            else str(row["terminal_outcome"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


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
    "session_id",
    "source_gateway_id",
    "source_workspace_id",
    "source_thread_id",
    "target_gateway_id",
    "target_workspace_id",
    "target_session_id",
    "target_thread_id",
    "kind",
    "reply_to_communication_id",
    "payload_hash",
)


def _outbox_preimage_mismatches(
    record: CommunicationOutboxRecord,
    preimage: dict[str, object],
) -> list[str]:
    """逐字段对比既有 outbox 行与本次提交的 preimage，返回漂移字段名。"""
    return [
        field_name
        for field_name in _OUTBOX_PREIMAGE_FIELDS
        if getattr(record, field_name) != preimage[field_name]
    ]


def _inbox_preimage_mismatches(
    record: CommunicationInboxRecord,
    **preimage: object,
) -> list[str]:
    """逐字段对比既有 inbox 行与本次提交的身份字段，返回漂移字段名。"""
    return [
        field_name
        for field_name in (
            "session_id",
            "source_gateway_id",
            "source_workspace_id",
            "source_session_id",
            "source_thread_id",
            "target_thread_id",
            "kind",
            "reply_to_communication_id",
            "payload_hash",
            "admission_id",
            "wakeup_key",
        )
        if getattr(record, field_name) != preimage[field_name]
    ]




def _fetch_inbox_row(
    connection: sqlite3.Connection, communication_id: str
) -> sqlite3.Row | None:
    """按 communication_id 取 inbox 行投影；缺失返回 None。

    inbox 的十余处判态/回读共用本查询，避免同一 SELECT 在多处复制；
    各调用点仍各自决定缺失时的错误文案与分支。
    """
    return connection.execute(
        f"SELECT {_COMMUNICATION_INBOX_COLUMNS} "
        "FROM communication_inbox WHERE communication_id = ?",
        (communication_id,),
    ).fetchone()


def _fetch_outbox_row_by_operation(
    connection: sqlite3.Connection, send_operation_id: str
) -> sqlite3.Row | None:
    """按 send_operation_id（operation 层 PK）取 outbox 行；缺失返回 None。"""
    return connection.execute(
        f"SELECT {_COMMUNICATION_OUTBOX_COLUMNS} "
        "FROM communication_outbox WHERE send_operation_id = ?",
        (send_operation_id,),
    ).fetchone()


def _fetch_outbox_row_by_communication(
    connection: sqlite3.Connection, communication_id: str
) -> sqlite3.Row | None:
    """按 communication_id（communication 层 UNIQUE）取 outbox 行；缺失返回 None。"""
    return connection.execute(
        f"SELECT {_COMMUNICATION_OUTBOX_COLUMNS} "
        "FROM communication_outbox WHERE communication_id = ?",
        (communication_id,),
    ).fetchone()


class CommunicationLedgerMixin:
    """SessionControlStore 的跨 Session 通信 ledger 方法族。

    依赖宿主类提供 database_path、_connection、_ensure_open() 与
    _write_transaction()。
    """

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
                self._ensure_outbox_reply_direction(
                    connection,
                    session_id=session_id,
                    reply_to_communication_id=str(reply_to_communication_id),
                    source_gateway_id=source_gateway_id,
                    source_workspace_id=source_workspace_id,
                    source_thread_id=source_thread_id,
                    target_gateway_id=target_gateway_id,
                    target_workspace_id=target_workspace_id,
                    target_thread_id=target_thread_id,
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
        - 已终态不可再迁移；重复提交相同 (state, receipt) 幂等返回。
        """
        _validate_communication_text(send_operation_id, field="send_operation_id")
        if new_state not in _COMMUNICATION_OUTBOX_TRANSITIONS and (
            new_state not in _COMMUNICATION_OUTBOX_TERMINAL_STATES
        ):
            raise ValueError(f"outbox 新状态非法: {new_state!r}")
        if new_state in ("target_accepted", "execution_bound", "terminal") and (
            receipt_json is None
        ):
            raise ValueError(f"outbox 迁移到 {new_state!r} 必须携带 receipt JSON")
        if new_state in ("failed", "cancelled") and abort_reason is None:
            raise ValueError(
                f"outbox 迁移到 {new_state!r} 必须携带 abort_reason"
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
                if (
                    record.latest_receipt != receipt_json
                    or record.abort_reason != abort_reason
                ):
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
            cursor = connection.execute(
                "UPDATE communication_outbox SET state = ?, "
                "latest_receipt = ?, abort_reason = ?, updated_at = ? "
                "WHERE send_operation_id = ? AND state = ?",
                (
                    new_state,
                    receipt_json,
                    abort_reason,
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
                self._ensure_inbox_reply_direction(
                    connection,
                    session_id=session_id,
                    reply_to_communication_id=str(reply_to_communication_id),
                    source_gateway_id=source_gateway_id,
                    source_workspace_id=source_workspace_id,
                    source_session_id=source_session_id,
                    source_thread_id=source_thread_id,
                    target_thread_id=target_thread_id,
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

    def list_target_accepted_communication_inboxes(
        self,
    ) -> tuple[CommunicationInboxRecord, ...]:
        """worker 恢复用状态索引：只查 target_accepted（不扫目录）。"""
        self._ensure_open()
        rows = self._connection.execute(
            f"SELECT {_COMMUNICATION_INBOX_COLUMNS} "
            "FROM communication_inbox WHERE state = 'target_accepted' "
            "ORDER BY created_at, communication_id"
        ).fetchall()
        return tuple(_communication_inbox_from_row(row) for row in rows)

    def get_communication_inbox(
        self,
        communication_id: str,
    ) -> CommunicationInboxRecord:
        """读取单条 inbox 投影；不存在抛 KeyError。"""
        self._ensure_open()
        row = _fetch_inbox_row(self._connection, communication_id)
        if row is None:
            raise KeyError(
                "communication inbox 不存在: "
                f"communication_id={communication_id!r}"
            )
        return _communication_inbox_from_row(row)

    def _ensure_outbox_reply_direction(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        reply_to_communication_id: str,
        source_gateway_id: str,
        source_workspace_id: str,
        source_thread_id: str,
        target_gateway_id: str,
        target_workspace_id: str,
        target_thread_id: str,
    ) -> None:
        """source 侧 reply 因果证明：本库 inbox 的被回复行方向必须相反。"""
        row = _fetch_inbox_row(connection, reply_to_communication_id)
        if row is None:
            raise RuntimeError(
                "kind=reply 无法在本 session inbox 中证明被回复 "
                f"communication（fail closed）: session_id={session_id!r}, "
                f"reply_to={reply_to_communication_id!r}"
            )
        direction_reversed = (
            str(row["source_gateway_id"]) == target_gateway_id
            and str(row["source_workspace_id"]) == target_workspace_id
            and str(row["source_thread_id"]) == target_thread_id
            and str(row["session_id"]) == session_id
            and str(row["target_thread_id"]) == source_thread_id
        )
        if not direction_reversed:
            raise RuntimeError(
                "kind=reply 的被回复 communication 方向与本次 send 相同（fail "
                f"closed）: session_id={session_id!r}, "
                f"reply_to={reply_to_communication_id!r}"
            )

    def _ensure_inbox_reply_direction(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        reply_to_communication_id: str,
        source_gateway_id: str,
        source_workspace_id: str,
        source_session_id: str,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None:
        """target 侧 reply 因果证明：本库 outbox 的被回复行方向必须相反。"""
        row = _fetch_outbox_row_by_communication(connection, reply_to_communication_id)
        if row is None:
            raise RuntimeError(
                "kind=reply 无法在本 session outbox 中证明被回复 "
                f"communication（fail closed）: session_id={session_id!r}, "
                f"reply_to={reply_to_communication_id!r}"
            )
        direction_reversed = (
            str(row["target_gateway_id"]) == source_gateway_id
            and str(row["target_workspace_id"]) == source_workspace_id
            and str(row["target_session_id"]) == source_session_id
            and str(row["target_thread_id"]) == source_thread_id
            and str(row["session_id"]) == session_id
            and str(row["source_thread_id"]) == target_thread_id
        )
        if not direction_reversed:
            raise RuntimeError(
                "kind=reply 的被回复 communication 方向与本次方向不一致（fail "
                f"closed）: session_id={session_id!r}, "
                f"reply_to={reply_to_communication_id!r}"
            )
