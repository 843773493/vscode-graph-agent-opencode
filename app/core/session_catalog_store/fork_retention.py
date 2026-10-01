"""fork_retention_claims 一条垂直链路（pinned retention 占位）。

承载 claim 行投影、create-or-get 准入（source 必须 active，删除先行则零
副作用失败）、preparing→active 的 CAS 激活、released 终结、按 target 批量
释放、按 source 列未释放 claim，以及整树删除前的 fail-closed 预检。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime

from app.core.session_catalog_store._schema import _FORK_RETENTION_CLAIM_COLUMNS
from app.core.session_catalog_store.contracts import (
    ForkRetentionClaim,
    SourceRetainedByForkError,
    SourceRetentionOperationPendingError,
    _validate_workspace_id,
)
from app.core.session_catalog_store.validators import validate_session_id


class ForkRetentionMixin:
    """fork retention claim 方法族（唯一实现点）。"""

    @staticmethod
    def _fork_retention_claim_from_row(row: sqlite3.Row) -> ForkRetentionClaim:
        return ForkRetentionClaim(
            fork_retention_claim_id=str(row["fork_retention_claim_id"]),
            workspace_id=str(row["workspace_id"]),
            source_session_id=str(row["source_session_id"]),
            target_session_id=str(row["target_session_id"]),
            source_lifecycle_generation=int(row["source_lifecycle_generation"]),
            state=str(row["state"]),
            release_reason=(
                str(row["release_reason"])
                if row["release_reason"] is not None
                else None
            ),
            record_created_at=str(row["record_created_at"]),
            record_updated_at=str(row["record_updated_at"]),
        )

    @staticmethod
    def _fetch_fork_retention_claim(
        connection: sqlite3.Connection,
        claim_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_FORK_RETENTION_CLAIM_COLUMNS} FROM fork_retention_claims "
            "WHERE fork_retention_claim_id = ?",
            (claim_id,),
        ).fetchone()

    def create_or_get_fork_retention_claim(
        self,
        *,
        claim_id: str,
        workspace_id: str,
        source_session_id: str,
        target_session_id: str,
        source_lifecycle_generation: int,
        connection: sqlite3.Connection | None = None,
    ) -> ForkRetentionClaim:
        """create-or-get pinned fork retention 占位（8.1-D，``state=preparing``）。

        在 source capture **之前**建立、不依赖 target 提交。同 claim_id 已存在
        且 preimage（workspace/source/target/generation）一致 → 幂等返回既有
        record（崩溃恢复不重复建 claim）；不一致 → ``RuntimeError``（冲突）。
        新插入路径要求 source Session 节点存在且 active（deleting 源拒绝建立
        claim，即「删除先行则 fork 零副作用失败」的准入侧）。

        ``connection`` 非 None 时在调用方写事务连接上执行，不自行 BEGIN/COMMIT
        （供 fork journal 与 claim 占位原子提交）。
        """
        if not isinstance(claim_id, str) or not claim_id:
            raise ValueError(f"claim_id 不能为空: {claim_id!r}")
        _validate_workspace_id(workspace_id)
        validate_session_id(source_session_id)
        validate_session_id(target_session_id)
        if not isinstance(source_lifecycle_generation, int) or isinstance(
            source_lifecycle_generation, bool
        ):
            raise TypeError(
                f"source_lifecycle_generation 必须是整数: "
                f"{source_lifecycle_generation!r}"
            )
        transaction = (
            self.write_transaction()
            if connection is None
            else nullcontext(connection)
        )
        with transaction as active:
            return self._register_fork_retention_claim(
                active,
                claim_id=claim_id,
                workspace_id=workspace_id,
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                source_lifecycle_generation=source_lifecycle_generation,
            )

    def _register_fork_retention_claim(
        self,
        connection: sqlite3.Connection,
        *,
        claim_id: str,
        workspace_id: str,
        source_session_id: str,
        target_session_id: str,
        source_lifecycle_generation: int,
    ) -> ForkRetentionClaim:
        """claim 占位的 create-or-get 主体（单写事务内，由公开入口调用）。"""
        existing = self._fetch_fork_retention_claim(connection, claim_id)
        if existing is not None:
            record = self._fork_retention_claim_from_row(existing)
            if (
                record.workspace_id != workspace_id
                or record.source_session_id != source_session_id
                or record.target_session_id != target_session_id
                or record.source_lifecycle_generation != source_lifecycle_generation
            ):
                raise RuntimeError(
                    "fork retention claim preimage 冲突（同 claim_id 不同 "
                    "workspace/source/target/generation，拒绝复用）: "
                    f"claim_id={claim_id!r}, existing_source="
                    f"{record.source_session_id!r}, existing_target="
                    f"{record.target_session_id!r}, requested_source="
                    f"{source_session_id!r}, requested_target="
                    f"{target_session_id!r}"
                )
            return record
        source_node = self._require_node(connection, source_session_id)
        if str(source_node["workspace_id"]) != workspace_id:
            raise RuntimeError(
                "fork source 属于其他 workspace，拒绝建立 retention claim: "
                f"source={source_session_id}, "
                f"source_workspace={source_node['workspace_id']!r}, "
                f"requested_workspace={workspace_id!r}"
            )
        if str(source_node["state"]) != "active":
            # 8.1-D 删除先行语义：source 已 deleting（删除先提交了 catalog
            # deleting）→ fork 不得建立 claim，零副作用失败。
            raise RuntimeError(
                "source Session 非 active，拒绝建立 fork retention claim"
                "（删除先行则 fork 零副作用失败）: "
                f"claim_id={claim_id!r}, source_session_id={source_session_id}, "
                f"source_state={source_node['state']!r}"
            )
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO fork_retention_claims ("
            "fork_retention_claim_id, workspace_id, source_session_id, "
            "target_session_id, source_lifecycle_generation, state, "
            "release_reason, record_created_at, record_updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'preparing', NULL, ?, ?)",
            (
                claim_id,
                workspace_id,
                source_session_id,
                target_session_id,
                source_lifecycle_generation,
                now,
                now,
            ),
        )
        inserted = self._fetch_fork_retention_claim(connection, claim_id)
        if inserted is None:
            raise RuntimeError(
                f"fork retention claim 插入后不可见（事务异常）: {claim_id!r}"
            )
        return self._fork_retention_claim_from_row(inserted)

    def activate_fork_retention_claim(
        self,
        claim_id: str,
        *,
        expected_generation: int,
    ) -> ForkRetentionClaim:
        """把 ``preparing`` claim CAS 为 ``active``（target 提交后激活同一 claim）。

        校验 claim 存在、``state=preparing``、``source_lifecycle_generation``
        等于 ``expected_generation``（漂移 → ``RuntimeError``）。已 ``active``
        幂等返回；``released`` → ``RuntimeError``（已终结，不可复活）。
        """
        with self.write_transaction() as connection:
            row = self._fetch_fork_retention_claim(connection, claim_id)
            if row is None:
                raise KeyError(f"fork retention claim 不存在: {claim_id!r}")
            state = str(row["state"])
            if state == "active":
                return self._fork_retention_claim_from_row(row)
            if state != "preparing":
                raise RuntimeError(
                    "fork retention claim 状态不允许激活: "
                    f"claim_id={claim_id!r}, state={state!r}"
                )
            actual_generation = int(row["source_lifecycle_generation"])
            if actual_generation != expected_generation:
                raise RuntimeError(
                    "fork retention claim 激活失败：source generation 已漂移: "
                    f"claim_id={claim_id!r}, "
                    f"expected_generation={expected_generation}, "
                    f"actual_generation={actual_generation}"
                )
            connection.execute(
                "UPDATE fork_retention_claims SET state = 'active', "
                "record_updated_at = ? WHERE fork_retention_claim_id = ?",
                (datetime.now(UTC).isoformat(), claim_id),
            )
            updated = self._fetch_fork_retention_claim(connection, claim_id)
            if updated is None:
                raise RuntimeError(
                    f"fork retention claim 激活后不可见（事务异常）: {claim_id!r}"
                )
            return self._fork_retention_claim_from_row(updated)

    def release_fork_retention_claim(
        self,
        claim_id: str,
        reason: str,
    ) -> ForkRetentionClaim:
        """把 claim 终结为 ``released``（abort/target 删除的 source 侧释放）。

        ``preparing``/``active`` → ``released``；已 ``released`` 幂等返回既有
        record（不覆盖原 reason）。
        """
        if not isinstance(reason, str):
            raise TypeError(f"release reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("release reason 不能为空")
        with self.write_transaction() as connection:
            row = self._fetch_fork_retention_claim(connection, claim_id)
            if row is None:
                raise KeyError(f"fork retention claim 不存在: {claim_id!r}")
            if str(row["state"]) == "released":
                return self._fork_retention_claim_from_row(row)
            self._mark_claim_released(connection, claim_id, reason)
            updated = self._fetch_fork_retention_claim(connection, claim_id)
            if updated is None:
                raise RuntimeError(
                    f"fork retention claim 释放后不可见（事务异常）: {claim_id!r}"
                )
            return self._fork_retention_claim_from_row(updated)

    @staticmethod
    def _mark_claim_released(
        connection: sqlite3.Connection,
        claim_id: str,
        reason: str,
    ) -> None:
        """把指定 claim 置为 ``released``（唯一释放 SQL 实现）。"""
        connection.execute(
            "UPDATE fork_retention_claims SET state = 'released', "
            "release_reason = ?, record_updated_at = ? "
            "WHERE fork_retention_claim_id = ?",
            (reason, datetime.now(UTC).isoformat(), claim_id),
        )

    def get_fork_retention_claim(self, claim_id: str) -> ForkRetentionClaim:
        """按 claim_id 返回 claim 投影；不存在抛 KeyError。"""
        with self.read_transaction() as connection:
            row = self._fetch_fork_retention_claim(connection, claim_id)
            if row is None:
                raise KeyError(f"fork retention claim 不存在: {claim_id!r}")
            return self._fork_retention_claim_from_row(row)

    def release_fork_retention_claims_for_target(
        self,
        target_session_id: str,
        reason: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        """释放全部以 ``target_session_id`` 为 target 的未释放 claim。

        用于 target 会话被删除时的 source 侧释放：删除 target 先去本地记录
        ``ForkRetentionReleaseRecord``，再取 source gate 释放精确 claim（本方法
        承担 source 侧一步）。返回被释放的 claim_id 元组（已 released 的跳过）。
        """
        validate_session_id(target_session_id)
        if not isinstance(reason, str) or not reason:
            raise ValueError("release reason 不能为空")
        transaction = (
            self.write_transaction()
            if connection is None
            else nullcontext(connection)
        )
        with transaction as active:
            rows = active.execute(
                f"SELECT {_FORK_RETENTION_CLAIM_COLUMNS} FROM fork_retention_claims "
                "WHERE target_session_id = ? AND state IN ('preparing', 'active') "
                "ORDER BY fork_retention_claim_id",
                (target_session_id,),
            ).fetchall()
            released: list[str] = []
            for row in rows:
                claim_id = str(row["fork_retention_claim_id"])
                self._mark_claim_released(active, claim_id, reason)
                released.append(claim_id)
            return tuple(released)

    def list_pinned_claims_for_source(
        self,
        source_session_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[ForkRetentionClaim]:
        """返回 source Session 上全部未释放（preparing/active）pinned claim。

        供整树删除在提交 catalog deleting **之前**做拓扑预检；``released``
        claim 不属于 blocker。按 claim_id 稳定排序。
        """
        validate_session_id(source_session_id)
        transaction = (
            self.read_transaction()
            if connection is None
            else nullcontext(connection)
        )
        with transaction as active:
            rows = active.execute(
                f"SELECT {_FORK_RETENTION_CLAIM_COLUMNS} FROM fork_retention_claims "
                "WHERE source_session_id = ? AND state IN ('preparing', 'active') "
                "ORDER BY fork_retention_claim_id",
                (source_session_id,),
            ).fetchall()
            return [self._fork_retention_claim_from_row(row) for row in rows]

    @staticmethod
    def _raise_for_source_claims(
        claims: list[ForkRetentionClaim],
    ) -> None:
        """任一未释放 claim 即 fail closed：active 优先于 preparing 报告。"""
        for claim in claims:
            if claim.state == "active":
                raise SourceRetainedByForkError(
                    source_session_id=claim.source_session_id,
                    claim_id=claim.fork_retention_claim_id,
                    target_session_id=claim.target_session_id,
                )
        for claim in claims:
            raise SourceRetentionOperationPendingError(
                source_session_id=claim.source_session_id,
                claim_id=claim.fork_retention_claim_id,
                target_session_id=claim.target_session_id,
            )

