"""per-session ``session-control.sqlite`` 的 collaboration 成员账本垂直链路。

承载 ``collaboration_ledger``/``collaboration_members`` 两张表的唯一实现：
member 登记幂等准入（create-or-get）、ledger revision 单调推进与读取、member
投影读取。member 与 creation record 由创建 owner 在同一事务内登记；publish
事务内的 registering→published 转正与 abort 内的定点取消由
``thread_creation_publish`` 族在同一事务内完成，本模块提供事务内登记原语与读取面。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.session_catalog_store.validators import validate_session_id
from app.core.session_control_primitives import validate_thread_creation_key
from app.core.session_control_store.sql import (
    SELECT_COLLABORATION_LEDGER_REVISION,
)

# collaboration ledger（8.5-B，R25）：owner session 内 delegation/member
# 的权威协作账本。revision 随 member 登记、发布、取消与 board 变更单调推进；
# thread creation record 冻结登记 revision 与 membership preimage，publish
# 校验账本未回退及本 member 冻结内容一致；member 行随 thread catalog publish
# 在同一事务内 registering → published（原子可见性）。
_COLLABORATION_LEDGER_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS collaboration_ledger (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL
)
"""

_COLLABORATION_MEMBERS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS collaboration_members (
    delegation_id TEXT PRIMARY KEY,
    creation_idempotency_key TEXT NOT NULL UNIQUE,
    membership_preimage_hash TEXT NOT NULL,
    registered_revision INTEGER NOT NULL CHECK (registered_revision > 0),
    coordinator_session_id TEXT NOT NULL,
    coordinator_thread_id TEXT NOT NULL,
    child_thread_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    subagent_type TEXT NOT NULL,
    title TEXT NOT NULL,
    task_seed TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('registering', 'published', 'cancelled')),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_COLLABORATION_MEMBER_COLUMNS = (
    "delegation_id, creation_idempotency_key, membership_preimage_hash, "
    "registered_revision, coordinator_session_id, coordinator_thread_id, "
    "child_thread_id, role, subagent_type, title, task_seed, state, "
    "registered_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class CollaborationMemberInput:
    """创建请求中显式冻结的成员定义；child/creation/revision 由同库 owner 分配。"""

    coordinator_session_id: str
    coordinator_thread_id: str
    role: str
    subagent_type: str
    title: str


def membership_preimage_hash(
    *, creation_idempotency_key: str, creation_preimage_hash: str,
    delegation_id: str, child_thread_id: str, task_seed: str, task_reference: str,
    member: CollaborationMemberInput, registered_revision: int,
) -> str:
    """冻结成员内容及其精确创建归属；状态变化不改变这个 preimage。"""
    value = {
        "creation_idempotency_key": creation_idempotency_key,
        "creation_preimage_hash": creation_preimage_hash,
        "delegation_id": delegation_id, "child_thread_id": child_thread_id,
        "coordinator_session_id": member.coordinator_session_id,
        "coordinator_thread_id": member.coordinator_thread_id,
        "role": member.role, "subagent_type": member.subagent_type,
        "title": member.title, "task_seed": task_seed, "task_reference": task_reference,
        "registered_revision": registered_revision,
    }
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def prepare_collaboration_member(
    connection: sqlite3.Connection, *, creation_idempotency_key: str,
    creation_preimage_hash: str, delegation_id: str, child_thread_id: str,
    task_seed: str, task_reference: str, member: CollaborationMemberInput,
) -> tuple[int, str]:
    """只在 creation owner 的原事务内登记，禁止独立 member 准入。"""
    if not connection.in_transaction:
        raise RuntimeError("collaboration member 必须随 creation 在同一事务准备")
    if connection.execute(
        "SELECT 1 FROM collaboration_members WHERE delegation_id=? OR creation_idempotency_key=?",
        (delegation_id, creation_idempotency_key),
    ).fetchone() is not None:
        raise RuntimeError("collaboration member 已绑定其他 creation，拒绝换绑")
    connection.execute("UPDATE collaboration_ledger SET revision=revision+1 WHERE id=1")
    revision = int(connection.execute(SELECT_COLLABORATION_LEDGER_REVISION).fetchone()[0])
    frozen_hash = membership_preimage_hash(
        creation_idempotency_key=creation_idempotency_key,
        creation_preimage_hash=creation_preimage_hash, delegation_id=delegation_id,
        child_thread_id=child_thread_id, task_seed=task_seed, task_reference=task_reference, member=member,
        registered_revision=revision,
    )
    now = datetime.now(UTC).isoformat()
    connection.execute(
        f"INSERT INTO collaboration_members ({_COLLABORATION_MEMBER_COLUMNS}) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'registering',?,?)",
        (delegation_id, creation_idempotency_key, frozen_hash, revision,
         member.coordinator_session_id, member.coordinator_thread_id, child_thread_id,
         member.role, member.subagent_type, member.title, task_seed, now, now),
    )
    return revision, frozen_hash


def validate_collaboration_member(
    connection: sqlite3.Connection, row: sqlite3.Row | dict[str, object],
) -> None:
    """所有读取、发布及执行准入都比较同一 creation 冻结值，不接纳漂移成员。"""
    creation = connection.execute(
        "SELECT * FROM thread_creation_records WHERE thread_creation_idempotency_key=?",
        (row["creation_idempotency_key"],),
    ).fetchone()
    if creation is None:
        raise RuntimeError("collaboration member 缺少原 creation record")
    if (creation["delegation_id"] is None or creation["task_seed"] is None
        or creation["task_reference"] is None
        or creation["collaboration_precondition_revision"] is None
        or creation["membership_preimage_hash"] is None):
        raise RuntimeError("collaboration member 的 creation 缺少冻结 membership 字段")
    member = CollaborationMemberInput(
        coordinator_session_id=str(row["coordinator_session_id"]),
        coordinator_thread_id=str(row["coordinator_thread_id"]),
        role=str(row["role"]), subagent_type=str(row["subagent_type"]),
        title=str(row["title"]),
    )
    try:
        reference = json.loads(creation["task_reference"])
    except ValueError as error:
        raise RuntimeError("collaboration member 的 creation task reference 损坏") from error
    if not isinstance(reference, dict):
        raise RuntimeError("collaboration member 的 creation task reference 必须是对象")  # noqa: TRY004
    expected = membership_preimage_hash(
        creation_idempotency_key=str(creation["thread_creation_idempotency_key"]),
        creation_preimage_hash=str(creation["preimage_hash"]),
        delegation_id=str(creation["delegation_id"]),
        child_thread_id=str(creation["child_thread_id"]), task_seed=str(creation["task_seed"]),
        task_reference=str(creation["task_reference"]),
        member=member, registered_revision=int(creation["collaboration_precondition_revision"]),
    )
    if (
        row["delegation_id"] != creation["delegation_id"]
        or row["child_thread_id"] != creation["child_thread_id"]
        or row["task_seed"] != creation["task_seed"]
        or row["registered_revision"] != creation["collaboration_precondition_revision"]
        or row["membership_preimage_hash"] != expected
        or creation["membership_preimage_hash"] != expected
        or reference.get("parent_session_id") != member.coordinator_session_id
        or reference.get("parent_thread_id") != member.coordinator_thread_id
        or reference.get("delegation_id") != row["delegation_id"]
    ):
        raise RuntimeError("collaboration member 与 creation 冻结 membership preimage 不一致")
    states = {"preparing": "registering", "published": "published", "aborted": "cancelled"}
    if row["state"] != states[creation["state"]]:
        raise RuntimeError("collaboration member 与 creation publication 状态不一致")
    ledger = connection.execute(SELECT_COLLABORATION_LEDGER_REVISION).fetchone()
    if ledger is None or int(ledger[0]) < int(row["registered_revision"]):
        raise RuntimeError("collaboration member 的 ledger revision 已回退或缺失")
    if connection.execute(
        "SELECT 1 FROM thread_catalog WHERE thread_id=?", (member.coordinator_thread_id,),
    ).fetchone() is None:
        raise RuntimeError("collaboration member 的 parent thread 不在同 owner catalog")



@dataclass(frozen=True, slots=True)
class CollaborationMember:
    """collaboration_members 表行的不可变投影（8.5-B 协作账本）。

    ``state`` 闭集为 ``registering/published/cancelled``；member 随
    thread catalog publish 在同一事务内转正（child 身份在 prepare 时冻结，
    原子可见性）；record abort 定点取消且同 delegation 不换绑。
    """

    delegation_id: str
    creation_idempotency_key: str
    membership_preimage_hash: str
    registered_revision: int
    coordinator_session_id: str
    coordinator_thread_id: str
    child_thread_id: str
    role: str
    subagent_type: str
    title: str
    task_seed: str
    state: str
    registered_at: str
    updated_at: str


class CollaborationMixin:
    """collaboration ledger 与 member 账本方法族（装配进 SessionControlStore）。"""

    # ------------------------------------------------------------------
    # collaboration ledger（8.5-B，R25：delegation/member 权威账本）
    # ------------------------------------------------------------------

    def _collaboration_member_from_row(self, row: sqlite3.Row) -> CollaborationMember:
        validate_collaboration_member(self._connection, row)
        return CollaborationMember(
            delegation_id=str(row["delegation_id"]),
            creation_idempotency_key=str(row["creation_idempotency_key"]),
            membership_preimage_hash=str(row["membership_preimage_hash"]),
            registered_revision=int(row["registered_revision"]),
            coordinator_session_id=str(row["coordinator_session_id"]),
            coordinator_thread_id=str(row["coordinator_thread_id"]),
            child_thread_id=str(row["child_thread_id"]),
            role=str(row["role"]),
            subagent_type=str(row["subagent_type"]),
            title=str(row["title"]),
            task_seed=str(row["task_seed"]),
            state=str(row["state"]),
            registered_at=str(row["registered_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_collaboration_ledger_revision(self) -> int:
        """返回 collaboration ledger 当前单调 revision。"""
        self._ensure_open()
        row = self._connection.execute(
            SELECT_COLLABORATION_LEDGER_REVISION
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "collaboration ledger row 缺失（库被外部改动，fail closed）: "
                f"path={self.database_path}"
            )
        return int(row["revision"])

    def get_collaboration_member(self, delegation_id: str) -> CollaborationMember:
        """按 delegation 幂等键返回 member 投影；不存在抛 KeyError。"""
        validate_thread_creation_key(delegation_id)
        self._ensure_open()
        row = self._connection.execute(
            f"SELECT {_COLLABORATION_MEMBER_COLUMNS} "
            "FROM collaboration_members WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"collaboration member 不存在: delegation_id={delegation_id!r}"
            )
        return self._collaboration_member_from_row(row)

    def list_collaboration_members(
        self,
        *,
        coordinator_session_id: str | None = None,
    ) -> tuple[CollaborationMember, ...]:
        """列出 member（可选按 coordinator session 过滤；确定性排序）。"""
        self._ensure_open()
        if coordinator_session_id is not None:
            validate_session_id(coordinator_session_id)
        rows = self._connection.execute(
            f"SELECT {_COLLABORATION_MEMBER_COLUMNS} FROM collaboration_members "
            "ORDER BY registered_at, delegation_id"
        ).fetchall()
        if coordinator_session_id is not None and any(
            row["coordinator_session_id"] != coordinator_session_id for row in rows
        ):
            raise RuntimeError("collaboration member 与读取 Session owner 不一致")
        return tuple(
            self._collaboration_member_from_row(row) for row in rows
        )
