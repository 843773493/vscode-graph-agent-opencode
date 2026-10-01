"""跨 Session 通信 outbox/inbox 表 DDL、索引、列清单与状态闭集常量(逐字搬迁)。"""

from __future__ import annotations

import re

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
