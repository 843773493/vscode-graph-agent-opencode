"""config apply claim 垂直链路。

承载 ``config_apply_claim`` 的读取/断言、fencing token CAS 获取、续约、释放
与过期回收。

错误分类沿用 gateway_state 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` CAS/并发冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigApplyClaimRecord,
    ConfigConflictError,
    new_config_id,
)


class ConfigApplyClaimMixin:
    """config apply claim 方法族（唯一实现点）。"""

    def acquire_config_apply_claim(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        attempt_id: str,
        apply_id: str,
        owner: str,
        base_active_revision: int | None,
        target_generation: str | None,
        lease_seconds: float = 30,
        fencing_token: str | None = None,
    ) -> ConfigApplyClaimRecord:
        if lease_seconds <= 0:
            raise ValueError("Gateway 配置 apply lease 必须大于 0 秒")
        if not all((config_domain, candidate_id, attempt_id, apply_id, owner)):
            raise ValueError("Gateway 配置 apply claim 的身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC)
            existing = connection.execute(
                """
                SELECT apply_id, lease_expires_at, fencing_token
                FROM config_apply_claim WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            pending = connection.execute(
                """
                SELECT pending_revision, state, base_active_revision
                FROM config_pending_candidate
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (config_domain, candidate_id),
            ).fetchone()
            if pending is None or str(pending[1]) not in {
                "candidate_validated",
                "pending_restart",
            }:
                raise ConfigConflictError(
                    "Gateway 配置 apply claim 只能绑定可应用的候选: "
                    f"domain={config_domain}, candidate={candidate_id}"
                )
            if pending[2] is not None and base_active_revision != int(pending[2]):
                raise ConfigConflictError(
                    "Gateway apply claim 与候选 active 基线不一致"
                )
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            current_active_revision = int(active[0]) if active is not None else None
            if current_active_revision != base_active_revision:
                raise ConfigConflictError(
                    "Gateway apply claim 的 active revision 基线已变化: "
                    f"current={current_active_revision}, expected={base_active_revision}"
                )
            if existing is not None:
                same_apply = str(existing[0]) == apply_id
                if not same_apply and datetime.fromisoformat(str(existing[1])) > now:
                    raise ConfigConflictError(
                        "Gateway 配置 apply claim 仍由其他持有者租用: "
                        f"domain={config_domain}, apply_id={existing[0]}"
                    )
            resolved_fencing_token = (
                fencing_token
                if fencing_token is not None
                else (
                    str(existing[2])
                    if existing is not None and str(existing[0]) == apply_id
                    else new_config_id("fence")
                )
            )
            connection.execute(
                """
                INSERT INTO config_apply_claim(
                    config_domain, candidate_id, attempt_id, apply_id, owner,
                    base_active_revision, target_generation, lease_expires_at,
                    fencing_token, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_domain) DO UPDATE SET
                    candidate_id=excluded.candidate_id,
                    attempt_id=excluded.attempt_id,
                    apply_id=excluded.apply_id,
                    owner=excluded.owner,
                    base_active_revision=excluded.base_active_revision,
                    target_generation=excluded.target_generation,
                    lease_expires_at=excluded.lease_expires_at,
                    fencing_token=excluded.fencing_token,
                    updated_at=excluded.updated_at
                """,
                (
                    config_domain,
                    candidate_id,
                    attempt_id,
                    apply_id,
                    owner,
                    base_active_revision,
                    target_generation,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    resolved_fencing_token,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE config_pending_candidate
                SET fencing_token = ?, last_attempt_id = ?, last_apply_id = ?
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (
                    resolved_fencing_token,
                    attempt_id,
                    apply_id,
                    config_domain,
                    candidate_id,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("Gateway 配置 apply claim 提交后无法读取")
        return result

    def renew_config_apply_claim(
        self,
        *,
        config_domain: str,
        apply_id: str,
        fencing_token: str,
        lease_seconds: float = 30,
    ) -> ConfigApplyClaimRecord:
        if lease_seconds <= 0:
            raise ValueError("Gateway 配置 apply lease 必须大于 0 秒")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC)
            cursor = connection.execute(
                """
                UPDATE config_apply_claim
                SET lease_expires_at = ?, updated_at = ?
                WHERE config_domain = ? AND apply_id = ? AND fencing_token = ?
                """,
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    config_domain,
                    apply_id,
                    fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway 配置 apply claim fencing 校验失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("Gateway 配置 apply claim 更新后无法读取")
        return result

    def release_config_apply_claim(
        self,
        *,
        config_domain: str,
        apply_id: str,
        fencing_token: str,
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM config_apply_claim
                WHERE config_domain = ? AND apply_id = ? AND fencing_token = ?
                """,
                (config_domain, apply_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError(
                    "Gateway 配置 apply claim 释放 fencing 校验失败"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired_config_applies(self, *, config_domain: str) -> tuple[str, ...]:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            rows = connection.execute(
                """
                SELECT candidate_id FROM config_pending_candidate
                WHERE config_domain = ? AND state = 'applying'
                  AND candidate_id NOT IN (
                      SELECT candidate_id FROM config_apply_claim
                      WHERE config_domain = ? AND lease_expires_at > ?
                  )
                """,
                (config_domain, config_domain, now),
            ).fetchall()
            candidate_ids = tuple(str(row[0]) for row in rows)
            connection.executemany(
                """
                UPDATE config_pending_candidate
                SET state = 'recovery_required',
                    last_error = 'Gateway 启动恢复发现 apply lease 已过期'
                WHERE config_domain = ? AND candidate_id = ? AND state = 'applying'
                """,
                ((config_domain, candidate_id) for candidate_id in candidate_ids),
            )
            if config_domain == "gateway" and candidate_ids:
                connection.executemany(
                    """
                    UPDATE gateway_restart_intent
                    SET state = 'recovery_required',
                        last_error = 'Gateway 启动恢复发现 apply lease 已过期',
                        updated_at = ?
                    WHERE candidate_id = ? AND state = 'applying'
                    """,
                    ((utc_now_text(), candidate_id) for candidate_id in candidate_ids),
                )
                connection.executemany(
                    """
                    UPDATE config_apply_journal
                    SET state = 'recovery_required',
                        last_error = 'Gateway 启动恢复发现 apply lease 已过期',
                        updated_at = ?
                    WHERE config_domain = ? AND candidate_id = ? AND state = 'applying'
                    """,
                    (
                        (utc_now_text(), config_domain, candidate_id)
                        for candidate_id in candidate_ids
                    ),
                )
            connection.execute("COMMIT")
            return candidate_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
