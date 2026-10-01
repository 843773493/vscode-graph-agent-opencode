"""per-session ``session-control.sqlite`` 的 collaboration 成员账本垂直链路。

承载 ``collaboration_ledger``/``collaboration_members`` 两张表的唯一实现：
member 登记幂等准入（create-or-get）、ledger revision 单调推进与读取、member
投影读取。publish 事务内的 registering→published 转正与 abort 内的定点取消由
``thread_creation_publish`` 族在同一事务内完成，本模块只提供登记入口与读取面。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.session_catalog_store import validate_session_id
from app.core.session_control_primitives import validate_thread_creation_key
from app.core.session_control_store.sql import (
    SELECT_COLLABORATION_LEDGER_REVISION,
)

# collaboration ledger（8.5-B，R25）：owner session 内 delegation/member
# 的权威协作账本。revision 是账本单调版本（member 登记唯一推进点），
# thread creation record 冻结 collaboration_precondition_revision 并在
# publish 时 CAS 校验未漂移；member 行随 thread catalog publish 在同一
# 事务内 registering → published（原子可见性）。
_COLLABORATION_LEDGER_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS collaboration_ledger (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL
)
"""

_COLLABORATION_MEMBERS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS collaboration_members (
    delegation_id TEXT PRIMARY KEY,
    coordinator_session_id TEXT NOT NULL,
    coordinator_thread_id TEXT NOT NULL,
    child_thread_id TEXT,
    role TEXT NOT NULL,
    subagent_type TEXT NOT NULL,
    title TEXT NOT NULL,
    task_seed TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('registering', 'published', 'cancelled')),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# collaboration member 状态闭集（8.5-B ledger）：registering（登记未发布）、
# published（随 thread catalog publish 原子转正）、cancelled（record abort
# 定点取消；同 delegation 不换绑，重试必须换新 delegation）。
_COLLABORATION_MEMBER_STATES = ("registering", "published", "cancelled")

_COLLABORATION_MEMBER_COLUMNS = (
    "delegation_id, coordinator_session_id, coordinator_thread_id, "
    "child_thread_id, role, subagent_type, title, task_seed, state, "
    "registered_at, updated_at"
)



@dataclass(frozen=True, slots=True)
class CollaborationMember:
    """collaboration_members 表行的不可变投影（8.5-B 协作账本）。

    ``state`` 闭集为 ``registering/published/cancelled``；member 随
    thread catalog publish 在同一事务内转正（``child_thread_id`` 回填，
    原子可见性）；record abort 定点取消且同 delegation 不换绑。
    """

    delegation_id: str
    coordinator_session_id: str
    coordinator_thread_id: str
    child_thread_id: str | None
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

    @staticmethod
    def _collaboration_member_from_row(row: sqlite3.Row) -> CollaborationMember:
        return CollaborationMember(
            delegation_id=str(row["delegation_id"]),
            coordinator_session_id=str(row["coordinator_session_id"]),
            coordinator_thread_id=str(row["coordinator_thread_id"]),
            child_thread_id=(
                str(row["child_thread_id"])
                if row["child_thread_id"] is not None
                else None
            ),
            role=str(row["role"]),
            subagent_type=str(row["subagent_type"]),
            title=str(row["title"]),
            task_seed=str(row["task_seed"]),
            state=str(row["state"]),
            registered_at=str(row["registered_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_collaboration_ledger_revision(self) -> int:
        """返回 collaboration ledger 当前 revision（member 登记推进）。"""
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

    def register_collaboration_member(
        self,
        *,
        delegation_id: str,
        coordinator_session_id: str,
        coordinator_thread_id: str,
        role: str,
        subagent_type: str,
        title: str,
        task_seed: str,
    ) -> int:
        """create-or-get collaboration member（幂等；返回当前 ledger revision）。

        - 新 delegation → 插入 ``registering`` member 并把 ledger
          revision +1（revision 的唯一推进点）；
        - 同 delegation 同内容重入 → 幂等 no-op（revision 不变；published
          member 重入同样幂等，供 publish 后/terminal 前崩溃恢复）；
        - 同 delegation 内容漂移或已 ``cancelled``（同 delegation 不换
          绑）→ ``RuntimeError`` fail closed。
        """
        validate_thread_creation_key(delegation_id)
        for name, value in (
            ("coordinator_session_id", coordinator_session_id),
            ("coordinator_thread_id", coordinator_thread_id),
            ("role", role),
            ("subagent_type", subagent_type),
            ("title", title),
            ("task_seed", task_seed),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} 不能为空: {value!r}")
        with self._write_transaction() as connection:
            existing = connection.execute(
                f"SELECT {_COLLABORATION_MEMBER_COLUMNS} "
                "FROM collaboration_members WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["coordinator_session_id"])
                    != coordinator_session_id
                    or str(existing["coordinator_thread_id"])
                    != coordinator_thread_id
                    or str(existing["role"]) != role
                    or str(existing["subagent_type"]) != subagent_type
                    or str(existing["title"]) != title
                    or str(existing["task_seed"]) != task_seed
                ):
                    raise RuntimeError(
                        "collaboration member 幂等冲突（同 delegation 不同"
                        f"内容，拒绝换绑）: delegation_id={delegation_id!r}"
                    )
                if str(existing["state"]) == "cancelled":
                    raise RuntimeError(
                        "collaboration member 已取消，同 delegation 不换绑"
                        "（fail closed，重试须换新 delegation）: "
                        f"delegation_id={delegation_id!r}"
                    )
                return self.get_collaboration_ledger_revision()
            connection.execute(
                f"INSERT INTO collaboration_members "
                f"({_COLLABORATION_MEMBER_COLUMNS}) VALUES "
                "(?, ?, ?, NULL, ?, ?, ?, ?, 'registering', ?, ?)",
                (
                    delegation_id,
                    coordinator_session_id,
                    coordinator_thread_id,
                    role,
                    subagent_type,
                    title,
                    task_seed,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(
                "UPDATE collaboration_ledger SET revision = revision + 1 "
                "WHERE id = 1"
            )
            return self.get_collaboration_ledger_revision()

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
        if coordinator_session_id is None:
            rows = self._connection.execute(
                f"SELECT {_COLLABORATION_MEMBER_COLUMNS} "
                "FROM collaboration_members "
                "ORDER BY registered_at, delegation_id"
            ).fetchall()
        else:
            validate_session_id(coordinator_session_id)
            rows = self._connection.execute(
                f"SELECT {_COLLABORATION_MEMBER_COLUMNS} "
                "FROM collaboration_members "
                "WHERE coordinator_session_id = ? "
                "ORDER BY registered_at, delegation_id",
                (coordinator_session_id,),
            ).fetchall()
        return tuple(
            self._collaboration_member_from_row(row) for row in rows
        )
