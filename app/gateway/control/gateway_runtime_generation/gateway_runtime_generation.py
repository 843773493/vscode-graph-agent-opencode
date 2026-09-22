"""Gateway runtime generation 生命周期与其健康证明的唯一实现。

本模块承载单一垂直链路的唯一实现：

- ``gateway_runtime_generation`` 权威 generation 行（record / read / state CAS）；
- serving handoff 的 fencing CAS、旧 generation 排空与失败回滚；
- 幂等 close 与 health proof JSON 投影。

``GatewayRuntimeGenerationMixin`` 由 :class:`app.gateway.control.gateway_state.
GatewayStateStore` 继承装配；宿主负责 ``_GATEWAY_MIGRATIONS`` 中
``gateway_runtime_generation`` 表的 DDL 与迁移序号，本模块只承载读写方法族，
宿主提供 ``_database``。错误分类沿用 gateway_state 约定：``ValueError`` 输入
形态非法、``ConfigConflictError`` state/fencing CAS 冲突、``RuntimeError``
事务后读取失败。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    GatewayRuntimeGenerationRecord,
    dump_json,
    load_json_object,
)

__all__ = [
    "GatewayRuntimeGenerationMixin",
]


# gateway_runtime_generation 的完整行投影：单 generation 读取与 active 查询
# 共用同一列清单，新增列时只需改这里。
_RUNTIME_GENERATION_SELECT = """
SELECT config_domain, generation_id, process_id, loaded_source,
       candidate_id, active_revision, pending_revision,
       candidate_digest, effective_digest, secret_binding_digest,
       fencing_token, listener_state, state, health_proof_json,
       created_at, updated_at
FROM gateway_runtime_generation
"""


class GatewayRuntimeGenerationMixin:
    @staticmethod
    def _runtime_generation_from_row(
        row: sqlite3.Row,
    ) -> GatewayRuntimeGenerationRecord:
        proof = (
            load_json_object(str(row[13]), field="Gateway runtime health proof")
            if row[13] is not None
            else None
        )
        return GatewayRuntimeGenerationRecord(
            config_domain=str(row[0]),
            generation_id=str(row[1]),
            process_id=int(row[2]) if row[2] is not None else None,
            loaded_source=cast(str, row[3]),
            candidate_id=str(row[4]) if row[4] is not None else None,
            active_revision=int(row[5]) if row[5] is not None else None,
            pending_revision=int(row[6]) if row[6] is not None else None,
            candidate_digest=str(row[7]) if row[7] is not None else None,
            effective_digest=str(row[8]),
            secret_binding_digest=(str(row[9]) if row[9] is not None else None),
            fencing_token=str(row[10]) if row[10] is not None else None,
            listener_state=cast(str, row[11]),
            state=cast(str, row[12]),
            health_proof=proof,
            created_at=datetime.fromisoformat(str(row[14])),
            updated_at=datetime.fromisoformat(str(row[15])),
        )

    def get_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
    ) -> GatewayRuntimeGenerationRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                _RUNTIME_GENERATION_SELECT + "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
        finally:
            connection.close()
        return self._runtime_generation_from_row(row) if row is not None else None

    def record_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
        process_id: int | None,
        loaded_source: str,
        candidate_id: str | None,
        active_revision: int | None,
        pending_revision: int | None,
        candidate_digest: str | None,
        effective_digest: str,
        secret_binding_digest: str | None,
        fencing_token: str | None,
        listener_state: str,
        state: str,
        health_proof: dict[str, object] | None = None,
    ) -> GatewayRuntimeGenerationRecord:
        if loaded_source not in {"active", "pending"}:
            raise ValueError("Gateway runtime loaded_source 无效")
        if listener_state not in {"reserved", "serving", "draining", "closed"}:
            raise ValueError("Gateway runtime listener_state 无效")
        if state not in {"starting", "healthy", "active", "failed", "closed"}:
            raise ValueError("Gateway runtime generation state 无效")
        if not generation_id or not effective_digest:
            raise ValueError("Gateway runtime generation 身份不能为空")
        now = utc_now_text()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO gateway_runtime_generation(
                    config_domain, generation_id, process_id, loaded_source,
                    candidate_id, active_revision, pending_revision, candidate_digest,
                    effective_digest, secret_binding_digest, fencing_token,
                    listener_state, state, health_proof_json, created_at, updated_at
                ) VALUES ('gateway', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(generation_id) DO UPDATE SET
                    process_id=excluded.process_id,
                    loaded_source=excluded.loaded_source,
                    candidate_id=excluded.candidate_id,
                    active_revision=excluded.active_revision,
                    pending_revision=excluded.pending_revision,
                    candidate_digest=excluded.candidate_digest,
                    effective_digest=excluded.effective_digest,
                    secret_binding_digest=excluded.secret_binding_digest,
                    fencing_token=excluded.fencing_token,
                    listener_state=excluded.listener_state,
                    state=excluded.state,
                    health_proof_json=excluded.health_proof_json,
                    updated_at=excluded.updated_at
                """,
                (
                    generation_id,
                    process_id,
                    loaded_source,
                    candidate_id,
                    active_revision,
                    pending_revision,
                    candidate_digest,
                    effective_digest,
                    secret_binding_digest,
                    fencing_token,
                    listener_state,
                    state,
                    dump_json(health_proof) if health_proof is not None else None,
                    now,
                    now,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_runtime_generation(generation_id=generation_id)
        if result is None:
            raise RuntimeError("Gateway runtime generation 提交后无法读取")
        return result

    def update_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
        expected_state: str,
        state: str,
        listener_state: str,
        health_proof: dict[str, object] | None = None,
    ) -> GatewayRuntimeGenerationRecord:
        if state not in {"starting", "healthy", "active", "failed", "closed"}:
            raise ValueError("Gateway runtime generation state 无效")
        if listener_state not in {"reserved", "serving", "draining", "closed"}:
            raise ValueError("Gateway runtime listener_state 无效")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET state = ?, listener_state = ?, health_proof_json = COALESCE(?, health_proof_json),
                    updated_at = ?
                WHERE generation_id = ? AND state = ?
                """,
                (
                    state,
                    listener_state,
                    dump_json(health_proof) if health_proof is not None else None,
                    utc_now_text(),
                    generation_id,
                    expected_state,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway runtime generation 状态 CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_runtime_generation(generation_id=generation_id)
        if result is None:
            raise RuntimeError("Gateway runtime generation 更新后无法读取")
        return result

    def active_gateway_runtime_generation(
        self,
    ) -> GatewayRuntimeGenerationRecord | None:
        """返回最近一次完成 serving handoff 的 Gateway generation。"""

        connection = self._database.connection()
        try:
            row = connection.execute(
                _RUNTIME_GENERATION_SELECT
                + """WHERE config_domain = 'gateway'
                  AND state = 'active' AND listener_state = 'serving'
                ORDER BY updated_at DESC, generation_id DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
        return self._runtime_generation_from_row(row) if row is not None else None

    def handoff_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
        expected_old_generation: str | None,
        fencing_token: str,
    ) -> GatewayRuntimeGenerationRecord:
        """以 fencing CAS 将健康新 generation 切到 serving，并标记旧 generation 排空。"""

        if not generation_id or not fencing_token:
            raise ValueError("Gateway runtime handoff 缺少 generation 或 fencing token")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            new_row = connection.execute(
                """
                SELECT state, listener_state, fencing_token
                FROM gateway_runtime_generation
                WHERE generation_id = ? AND config_domain = 'gateway'
                """,
                (generation_id,),
            ).fetchone()
            if new_row is None:
                raise ConfigConflictError(
                    f"Gateway runtime handoff 找不到新 generation: {generation_id}"
                )
            if str(new_row[2]) != fencing_token:
                raise ConfigConflictError(
                    "Gateway runtime handoff fencing token 不匹配"
                )
            if str(new_row[0]) != "healthy" or str(new_row[1]) != "reserved":
                raise ConfigConflictError(
                    "Gateway runtime handoff 要求新 generation 为 healthy/reserved"
                )
            if expected_old_generation and expected_old_generation != generation_id:
                old_row = connection.execute(
                    """
                    SELECT state, listener_state
                    FROM gateway_runtime_generation
                    WHERE generation_id = ? AND config_domain = 'gateway'
                    """,
                    (expected_old_generation,),
                ).fetchone()
                if old_row is not None and str(old_row[0]) == "active":
                    if str(old_row[1]) != "serving":
                        raise ConfigConflictError(
                            "Gateway 旧 generation 不是 serving，不能执行 handoff"
                        )
                    cursor = connection.execute(
                        """
                        UPDATE gateway_runtime_generation
                        SET listener_state = 'draining', updated_at = ?
                        WHERE generation_id = ? AND state = 'active'
                          AND listener_state = 'serving'
                        """,
                        (utc_now_text(), expected_old_generation),
                    )
                    if cursor.rowcount != 1:
                        raise ConfigConflictError("Gateway 旧 generation 排空 CAS 失败")
            cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET state = 'active', listener_state = 'serving', updated_at = ?
                WHERE generation_id = ? AND state = 'healthy'
                  AND listener_state = 'reserved' AND fencing_token = ?
                """,
                (utc_now_text(), generation_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway 新 generation serving CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_runtime_generation(generation_id=generation_id)
        if result is None:
            raise RuntimeError("Gateway runtime handoff 后新 generation 消失")
        return result

    def rollback_gateway_runtime_handoff(
        self,
        *,
        generation_id: str,
        old_generation_id: str,
        fencing_token: str,
    ) -> tuple[GatewayRuntimeGenerationRecord, GatewayRuntimeGenerationRecord]:
        """新 generation 失败时恢复仍处于 draining 的旧 generation。"""

        if not all((generation_id, old_generation_id, fencing_token)):
            raise ValueError(
                "Gateway runtime rollback 缺少 generation 或 fencing token"
            )
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            new_cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET state = 'failed', listener_state = 'closed', updated_at = ?
                WHERE generation_id = ? AND state = 'active'
                  AND listener_state = 'serving' AND fencing_token = ?
                """,
                (utc_now_text(), generation_id, fencing_token),
            )
            if new_cursor.rowcount != 1:
                raise ConfigConflictError("Gateway 新 generation rollback CAS 失败")
            old_cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET listener_state = 'serving', updated_at = ?
                WHERE generation_id = ? AND state = 'active'
                  AND listener_state = 'draining'
                """,
                (utc_now_text(), old_generation_id),
            )
            if old_cursor.rowcount != 1:
                raise ConfigConflictError(
                    "Gateway 旧 generation 已无法恢复，必须进入 recovery_required"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        new_record = self.get_gateway_runtime_generation(generation_id=generation_id)
        old_record = self.get_gateway_runtime_generation(
            generation_id=old_generation_id
        )
        if new_record is None or old_record is None:
            raise RuntimeError("Gateway runtime rollback 后 generation 记录不完整")
        return new_record, old_record

    def close_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
        expected_states: tuple[str, ...] = ("starting", "healthy", "active"),
        fencing_token: str | None = None,
    ) -> GatewayRuntimeGenerationRecord:
        """幂等关闭已排空或已失败的 Gateway generation。"""

        if not generation_id or not expected_states:
            raise ValueError("Gateway runtime close 参数不能为空")
        placeholders = ", ".join("?" for _ in expected_states)
        params: list[object] = [utc_now_text(), generation_id, *expected_states]
        fencing_clause = ""
        if fencing_token is not None:
            fencing_clause = " AND fencing_token = ?"
            params.append(fencing_token)
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET state = 'closed', listener_state = 'closed', updated_at = ?
                WHERE generation_id = ? AND state IN ("""
                + placeholders
                + ")"
                + fencing_clause,
                params,
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway runtime close 状态 CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_runtime_generation(generation_id=generation_id)
        if result is None:
            raise RuntimeError("Gateway runtime close 后 generation 消失")
        return result
