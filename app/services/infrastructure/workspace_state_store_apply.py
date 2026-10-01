"""Workspace config apply claim 与 apply journal 方法族。

WorkspaceStateStoreApplyMixin 组合 apply-claim 与 apply-journal 两族方法，
由 WorkspaceStateStore 装配；依赖宿主提供的 _database 与共享 SQL 常量。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigApplyClaimRecord,
    ConfigApplyJournalRecord,
    ConfigConflictError,
    ConfigLifecycleState,
    dump_json,
    load_json_object,
    new_config_id,
    validate_state_transition,
)
from app.services.infrastructure.workspace_state_store_sql import (
    _CONFIG_APPLY_CLAIM_UPSERT,
    _CONFIG_APPLY_JOURNAL_INSERT,
    _PENDING_CANDIDATE_SNAPSHOT_SELECT,
)

__all__ = ["WorkspaceStateStoreApplyMixin"]


class WorkspaceStateStoreApplyMixin:
    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._database.connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_config_apply_claim(
        self,
        *,
        config_domain: str,
    ) -> ConfigApplyClaimRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT config_domain, candidate_id, attempt_id, apply_id, owner,
                       base_active_revision, target_generation, lease_expires_at,
                       fencing_token, updated_at
                FROM config_apply_claim
                WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
        if row is None:
            return None
        return ConfigApplyClaimRecord(
            config_domain=str(row[0]),
            candidate_id=str(row[1]),
            attempt_id=str(row[2]),
            apply_id=str(row[3]),
            owner=str(row[4]),
            base_active_revision=(
                int(row[5]) if row[5] is not None else None
            ),
            target_generation=str(row[6]) if row[6] is not None else None,
            lease_expires_at=datetime.fromisoformat(str(row[7])),
            fencing_token=str(row[8]),
            updated_at=datetime.fromisoformat(str(row[9])),
        )

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
    ) -> ConfigApplyClaimRecord:
        if lease_seconds <= 0:
            raise ValueError("配置 apply lease 必须大于 0 秒")
        if not all((config_domain, candidate_id, attempt_id, apply_id, owner)):
            raise ValueError("配置 apply claim 的身份字段不能为空")
        with self._write_transaction() as connection:
            now = datetime.now(UTC)
            now_text = now.isoformat()
            existing = connection.execute(
                """
                SELECT candidate_id, attempt_id, apply_id, owner, base_active_revision,
                       target_generation, lease_expires_at, fencing_token
                FROM config_apply_claim
                WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            pending = connection.execute(
                _PENDING_CANDIDATE_SNAPSHOT_SELECT,
                (config_domain, candidate_id),
            ).fetchone()
            if pending is None or str(pending[1]) not in {
                "candidate_validated",
                "pending_restart",
            }:
                raise ConfigConflictError(
                    "配置 apply claim 只能绑定可应用的候选: "
                    f"domain={config_domain}, candidate={candidate_id}"
                )
            if pending[2] is not None and base_active_revision != int(pending[2]):
                raise ConfigConflictError("配置 apply claim 与候选 active 基线不一致")
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            current_active_revision = int(active[0]) if active is not None else None
            if current_active_revision != base_active_revision:
                raise ConfigConflictError(
                    "配置 apply claim 的 active revision 基线已变化: "
                    f"current={current_active_revision}, expected={base_active_revision}"
                )
            if existing is not None:
                same_apply = str(existing[2]) == apply_id
                lease_expires = datetime.fromisoformat(str(existing[6]))
                if not same_apply and lease_expires > now:
                    raise ConfigConflictError(
                        "配置 apply claim 仍由其他持有者租用: "
                        f"domain={config_domain}, apply_id={existing[2]}"
                    )
            fencing_token = (
                str(existing[7])
                if existing is not None and str(existing[2]) == apply_id
                else new_config_id("fence")
            )
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            connection.execute(
                _CONFIG_APPLY_CLAIM_UPSERT,
                (
                    config_domain,
                    candidate_id,
                    attempt_id,
                    apply_id,
                    owner,
                    base_active_revision,
                    target_generation,
                    lease_expires_at.isoformat(),
                    fencing_token,
                    now_text,
                ),
            )
            connection.execute(
                """
                UPDATE config_pending_candidate
                SET fencing_token = ?, last_attempt_id = ?, last_apply_id = ?
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (
                    fencing_token,
                    attempt_id,
                    apply_id,
                    config_domain,
                    candidate_id,
                ),
            )
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("配置 apply claim 提交后无法读取")
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
            raise ValueError("配置 apply lease 必须大于 0 秒")
        with self._write_transaction() as connection:
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
                raise ConfigConflictError("配置 apply claim fencing 校验失败")
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("配置 apply claim 更新后无法读取")
        return result

    def release_config_apply_claim(
        self,
        *,
        config_domain: str,
        apply_id: str,
        fencing_token: str,
    ) -> None:
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM config_apply_claim
                WHERE config_domain = ? AND apply_id = ? AND fencing_token = ?
                """,
                (config_domain, apply_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("配置 apply claim 释放 fencing 校验失败")

    def recover_expired_config_applies(
        self,
        *,
        config_domain: str,
    ) -> tuple[str, ...]:
        """把遗留的 applying 候选标为 recovery_required，避免启动时猜测。"""
        with self._write_transaction() as connection:
            now = datetime.now(UTC).isoformat()
            rows = connection.execute(
                """
                SELECT candidate_id
                FROM config_pending_candidate
                WHERE config_domain = ? AND state = 'applying'
                  AND candidate_id NOT IN (
                      SELECT candidate_id FROM config_apply_claim
                      WHERE config_domain = ? AND lease_expires_at > ?
                  )
                """,
                (config_domain, config_domain, now),
            ).fetchall()
            candidate_ids = tuple(str(row[0]) for row in rows)
            if candidate_ids:
                connection.executemany(
                    """
                    UPDATE config_pending_candidate
                    SET state = 'recovery_required',
                        last_error = '启动恢复发现 apply lease 已过期'
                    WHERE config_domain = ? AND candidate_id = ? AND state = 'applying'
                    """,
                    ((config_domain, candidate_id) for candidate_id in candidate_ids),
                )
                connection.executemany(
                    """
                    UPDATE config_apply_journal
                    SET state = 'recovery_required',
                        last_error = '启动恢复发现 apply lease 已过期',
                        updated_at = ?
                    WHERE config_domain = ? AND candidate_id = ? AND state = 'applying'
                    """,
                    (
                        (utc_now_text(), config_domain, candidate_id)
                        for candidate_id in candidate_ids
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM config_apply_claim
                    WHERE config_domain = ? AND lease_expires_at <= ?
                    """,
                    (config_domain, now),
                )
            return candidate_ids

    @staticmethod
    def _apply_journal_from_row(row: sqlite3.Row) -> ConfigApplyJournalRecord:
        side_effects = json.loads(str(row[10]))
        if not isinstance(side_effects, list) or not all(
            isinstance(item, dict) for item in side_effects
        ):
            raise TypeError("Workspace apply journal side_effects 结构无效")
        return ConfigApplyJournalRecord(
            config_domain=str(row[0]),
            apply_id=str(row[1]),
            candidate_id=str(row[2]),
            attempt_id=str(row[3]),
            owner=str(row[4]),
            base_active_revision=(int(row[5]) if row[5] is not None else None),
            pending_revision=(int(row[6]) if row[6] is not None else None),
            source_baseline=load_json_object(
                str(row[7]), field="Workspace apply journal source baseline"
            ),
            active_baseline=load_json_object(
                str(row[8]), field="Workspace apply journal active baseline"
            ),
            registry_revision=(int(row[9]) if row[9] is not None else None),
            side_effects=tuple(cast(dict[str, object], item) for item in side_effects),
            state=cast(str, row[11]),
            last_error=str(row[12]) if row[12] is not None else None,
            created_at=datetime.fromisoformat(str(row[13])),
            updated_at=datetime.fromisoformat(str(row[14])),
        )

    def get_config_apply_journal(
        self,
        *,
        apply_id: str,
    ) -> ConfigApplyJournalRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT config_domain, apply_id, candidate_id, attempt_id, owner,
                       base_active_revision, pending_revision, source_baseline_json,
                       active_baseline_json, registry_revision, side_effects_json,
                       state, last_error, created_at, updated_at
                FROM config_apply_journal WHERE apply_id = ?
                """,
                (apply_id,),
            ).fetchone()
        return self._apply_journal_from_row(row) if row is not None else None

    def list_config_apply_journals(
        self,
        *,
        config_domain: str,
        states: tuple[str, ...] = (),
    ) -> tuple[ConfigApplyJournalRecord, ...]:
        query = """
            SELECT config_domain, apply_id, candidate_id, attempt_id, owner,
                   base_active_revision, pending_revision, source_baseline_json,
                   active_baseline_json, registry_revision, side_effects_json,
                   state, last_error, created_at, updated_at
            FROM config_apply_journal WHERE config_domain = ?
        """
        params: list[object] = [config_domain]
        if states:
            placeholders = ",".join("?" for _ in states)
            query += f" AND state IN ({placeholders})"
            params.extend(states)
        query += " ORDER BY updated_at ASC, apply_id ASC"
        with self._read_connection() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(self._apply_journal_from_row(row) for row in rows)

    def start_config_apply_journal(
        self,
        *,
        config_domain: str,
        apply_id: str,
        candidate_id: str,
        attempt_id: str,
        owner: str,
        base_active_revision: int | None,
        pending_revision: int | None,
        source_baseline: dict[str, object],
        active_baseline: dict[str, object],
        registry_revision: int | None = None,
    ) -> ConfigApplyJournalRecord:
        if not all((config_domain, apply_id, candidate_id, attempt_id, owner)):
            raise ValueError("Workspace apply journal 身份字段不能为空")
        with self._write_transaction() as connection:
            existing = connection.execute(
                "SELECT apply_id FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if existing is None:
                now = utc_now_text()
                connection.execute(
                    _CONFIG_APPLY_JOURNAL_INSERT,
                    (
                        config_domain,
                        apply_id,
                        candidate_id,
                        attempt_id,
                        owner,
                        base_active_revision,
                        pending_revision,
                        dump_json(source_baseline),
                        dump_json(active_baseline),
                        registry_revision,
                        now,
                        now,
                    ),
                )
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Workspace apply journal 提交后无法读取")
        return result

    def update_config_apply_journal(
        self,
        *,
        apply_id: str,
        expected_state: str,
        state: str,
        side_effects: tuple[dict[str, object], ...] | None = None,
        last_error: str | None = None,
    ) -> ConfigApplyJournalRecord:
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE config_apply_journal
                SET state = ?, last_error = ?, side_effects_json = COALESCE(?, side_effects_json),
                    updated_at = ?
                WHERE apply_id = ? AND state = ?
                """,
                (
                    state,
                    last_error,
                    dump_json(list(side_effects)) if side_effects is not None else None,
                    utc_now_text(),
                    apply_id,
                    expected_state,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Workspace apply journal 状态 CAS 失败")
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Workspace apply journal 更新后无法读取")
        return result

    def append_config_apply_side_effect(
        self,
        *,
        apply_id: str,
        side_effect: dict[str, object],
        expected_state: str = "applying",
    ) -> ConfigApplyJournalRecord:
        """在 journal 中幂等追加一个已观测的外部副作用。"""

        if not side_effect:
            raise ValueError("Workspace apply journal 副作用记录不能为空")
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT state, side_effects_json FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if row is None or str(row[0]) != expected_state:
                raise ConfigConflictError(
                    "Workspace apply journal 副作用追加状态 CAS 失败"
                )
            side_effects = json.loads(str(row[1]))
            if not isinstance(side_effects, list) or not all(
                isinstance(item, dict) for item in side_effects
            ):
                raise TypeError("Workspace apply journal side_effects 结构无效")
            if side_effect not in side_effects:
                side_effects.append(side_effect)
            cursor = connection.execute(
                """
                UPDATE config_apply_journal
                SET side_effects_json = ?, updated_at = ?
                WHERE apply_id = ? AND state = ?
                """,
                (dump_json(side_effects), utc_now_text(), apply_id, expected_state),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError(
                    "Workspace apply journal 副作用追加状态 CAS 失败"
                )
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Workspace apply journal 副作用提交后无法读取")
        return result

    def record_config_apply_compensation(
        self,
        *,
        apply_id: str,
        compensation: dict[str, object],
        expected_state: str = "recovery_required",
    ) -> ConfigApplyJournalRecord:
        """记录外部副作用的补偿结果；不会假装 SQLite 能回滚外部资源。"""

        allowed_fields = {"resource", "action", "status", "error", "detail"}
        if not compensation or not set(compensation).issubset(allowed_fields):
            raise ValueError("Workspace 补偿记录只能包含资源、动作、状态和错误摘要")
        status = compensation.get("status")
        if status not in {"succeeded", "failed"}:
            raise ValueError("Workspace 补偿记录 status 必须是 succeeded 或 failed")
        if not isinstance(compensation.get("resource"), str) or not isinstance(
            compensation.get("action"), str
        ):
            raise ValueError("Workspace 补偿记录必须包含 resource 和 action")
        entry = {"phase": "compensation", **compensation}
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT state, side_effects_json FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError("Workspace 补偿记录关联的 apply journal 不存在")
            current_state = str(row[0])
            side_effects = json.loads(str(row[1]))
            if not isinstance(side_effects, list) or not all(
                isinstance(item, dict) for item in side_effects
            ):
                raise TypeError("Workspace apply journal side_effects 结构无效")
            if current_state == "compensated" and entry in side_effects:
                pass
            else:
                if current_state != expected_state:
                    raise ConfigConflictError(
                        "Workspace 补偿记录状态 CAS 失败: "
                        f"state={current_state}, expected={expected_state}"
                    )
                if entry not in side_effects:
                    side_effects.append(entry)
                next_state = "compensated" if status == "succeeded" else "recovery_required"
                last_error = (
                    None
                    if status == "succeeded"
                    else str(compensation.get("error") or "外部副作用补偿失败")
                )
                cursor = connection.execute(
                    """
                    UPDATE config_apply_journal
                    SET state = ?, side_effects_json = ?, last_error = ?, updated_at = ?
                    WHERE apply_id = ? AND state = ?
                    """,
                    (
                        next_state,
                        dump_json(side_effects),
                        last_error,
                        utc_now_text(),
                        apply_id,
                        expected_state,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConfigConflictError("Workspace 补偿记录状态 CAS 失败")
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Workspace 补偿记录提交后无法读取")
        return result

    def recover_config_apply_journals(
        self,
        *,
        config_domain: str,
    ) -> tuple[ConfigApplyJournalRecord, ...]:
        journals = self.list_config_apply_journals(
            config_domain=config_domain,
            states=("applying",),
        )
        for journal in journals:
            self.update_config_apply_journal(
                apply_id=journal.apply_id,
                expected_state="applying",
                state="recovery_required",
                last_error="进程恢复发现未完成的外部 apply journal",
            )
        return self.list_config_apply_journals(
            config_domain=config_domain,
            states=("recovery_required",),
        )

    def begin_config_apply(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        attempt_id: str,
        apply_id: str,
        owner: str,
        base_active_revision: int | None,
        target_generation: str | None,
        pending_revision: int,
        source_baseline: dict[str, object],
        active_baseline: dict[str, object],
        expected_candidate_state: ConfigLifecycleState = "candidate_validated",
        registry_revision: int | None = None,
        lease_seconds: float = 30,
    ) -> ConfigApplyClaimRecord:
        """原子创建 claim、apply journal 并把候选推进到 applying。"""

        if lease_seconds <= 0:
            raise ValueError("配置 apply lease 必须大于 0 秒")
        validate_state_transition(expected_candidate_state, "applying")
        if not all((config_domain, candidate_id, attempt_id, apply_id, owner)):
            raise ValueError("配置 apply 的身份字段不能为空")
        with self._write_transaction() as connection:
            pending = connection.execute(
                _PENDING_CANDIDATE_SNAPSHOT_SELECT,
                (config_domain, candidate_id),
            ).fetchone()
            if (
                pending is None
                or int(pending[0]) != pending_revision
                or str(pending[1]) != expected_candidate_state
            ):
                raise ConfigConflictError("Workspace begin apply 的候选状态 CAS 失败")
            if pending[2] is not None and base_active_revision != int(pending[2]):
                raise ConfigConflictError("Workspace begin apply 与候选 active 基线不一致")
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            current_active_revision = int(active[0]) if active is not None else None
            if current_active_revision != base_active_revision:
                raise ConfigConflictError(
                    "Workspace begin apply 的 active revision 基线已变化: "
                    f"current={current_active_revision}, expected={base_active_revision}"
                )
            existing = connection.execute(
                """
                SELECT attempt_id, apply_id, lease_expires_at, fencing_token
                FROM config_apply_claim
                WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            now = datetime.now(UTC)
            if existing is not None:
                same_apply = str(existing[1]) == apply_id
                if not same_apply and datetime.fromisoformat(str(existing[2])) > now:
                    raise ConfigConflictError(
                        "Workspace 配置 apply claim 仍由其他持有者租用: "
                        f"apply_id={existing[1]}"
                    )
            fencing_token = (
                str(existing[3])
                if existing is not None and str(existing[1]) == apply_id
                else new_config_id("fence")
            )
            connection.execute(
                _CONFIG_APPLY_CLAIM_UPSERT,
                (
                    config_domain,
                    candidate_id,
                    attempt_id,
                    apply_id,
                    owner,
                    base_active_revision,
                    target_generation,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    fencing_token,
                    now.isoformat(),
                ),
            )
            updated = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'applying', fencing_token = ?,
                    last_attempt_id = ?, last_apply_id = ?
                WHERE config_domain = ? AND candidate_id = ? AND state = ?
                """,
                (
                    fencing_token,
                    attempt_id,
                    apply_id,
                    config_domain,
                    candidate_id,
                    expected_candidate_state,
                ),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError("Workspace begin apply 的候选状态 CAS 失败")
            journal = connection.execute(
                "SELECT candidate_id, attempt_id, owner, pending_revision, source_baseline_json, active_baseline_json "
                "FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if journal is None:
                now_text = now.isoformat()
                connection.execute(
                    _CONFIG_APPLY_JOURNAL_INSERT,
                    (
                        config_domain,
                        apply_id,
                        candidate_id,
                        attempt_id,
                        owner,
                        base_active_revision,
                        pending_revision,
                        dump_json(source_baseline),
                        dump_json(active_baseline),
                        registry_revision,
                        now_text,
                        now_text,
                    ),
                )
            elif (
                str(journal[0]) != candidate_id
                or str(journal[1]) != attempt_id
                or str(journal[2]) != owner
                or int(journal[3]) != pending_revision
                or str(journal[4]) != dump_json(source_baseline)
                or str(journal[5]) != dump_json(active_baseline)
            ):
                raise ConfigConflictError("Workspace begin apply 的 journal 身份不一致")
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("Workspace begin apply 提交后缺少 claim")
        return result
