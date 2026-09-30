"""Workspace pending config restart 与 discard 方法族。

WorkspaceStateStoreRestartMixin 只承载本族方法，由 WorkspaceStateStore 装配；
本族通过 _database 与其它族方法交互，不复制 SQL。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    ConfigPendingCandidateRecord,
    load_json_object,
    new_config_id,
    validate_state_transition,
)
from app.services.infrastructure.workspace_state_store_sql import (
    _SOURCE_LAYER_BASELINE_SELECT,
)

__all__ = ["WorkspaceStateStoreRestartMixin"]


class WorkspaceStateStoreRestartMixin:
    def retry_pending_config_restart(
        self,
        *,
        candidate_ref: str,
        target_generation: str,
    ) -> ConfigPendingCandidateRecord:
        """复用 pending candidate 重试，且为新 generation 轮换 fencing token。"""

        if not candidate_ref or not target_generation:
            raise ValueError("Workspace pending retry 的身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT candidate_id, state, base_active_revision
                FROM config_pending_candidate
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError(
                    f"Workspace pending candidate 不存在: {candidate_ref}"
                )
            if str(row[1]) not in {"pending_restart", "recovery_required"}:
                raise ConfigConflictError(
                    "Workspace pending retry 只允许 pending_restart/recovery_required: "
                    f"state={row[1]}"
                )
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = 'workspace'"
            ).fetchone()
            active_revision = int(active[0]) if active is not None else None
            if active_revision != (
                int(row[2]) if row[2] is not None else None
            ):
                raise ConfigConflictError(
                    "Workspace pending retry 的 active revision 基线已变化"
                )
            claim = connection.execute(
                """
                SELECT lease_expires_at FROM config_apply_claim
                WHERE config_domain = 'workspace'
                """
            ).fetchone()
            now = datetime.now(UTC)
            if claim is not None:
                if datetime.fromisoformat(str(claim[0])) > now:
                    raise ConfigConflictError(
                        "Workspace pending retry 仍有未过期的 apply claim"
                    )
                connection.execute(
                    "DELETE FROM config_apply_claim WHERE config_domain = 'workspace'"
                )
            updated = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'pending_restart', target_generation = ?,
                    fencing_token = ?, last_error = NULL
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                  AND state IN ('pending_restart', 'recovery_required')
                """,
                (target_generation, new_config_id("fence"), candidate_ref),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError("Workspace pending retry 状态 CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.load_pending_config_candidate(candidate_ref=candidate_ref)
        return result

    def discard_pending_config_candidate(
        self,
        *,
        candidate_ref: str,
        expected_active_revision: int,
        expected_active_digest: str,
        expected_source_baseline: dict[str, object],
        event: ConfigEventInput | None = None,
        reason: str = "用户显式丢弃 pending candidate",
    ) -> ConfigPendingCandidateRecord:
        """在已确认旧 active 和来源基线安全时原子丢弃 pending。

        recovery_required 不是可以盲目清理的垃圾状态。只有旧 active 仍然完整、所有
        source layer 仍与候选基线一致、且没有未排除的 apply claim/外部副作用时，才允许
        将候选转为 discarded。这样调用方不会用一次普通的删除请求掩盖未知运行时状态。
        """

        if not candidate_ref or not expected_active_digest:
            raise ValueError("Workspace pending discard 的身份和 active digest 不能为空")
        if expected_active_revision < 0:
            raise ValueError("Workspace pending discard 的 active revision 不能为负数")
        if not reason:
            raise ValueError("Workspace pending discard 原因不能为空")
        validate_state_transition("pending_restart", "discarded")
        validate_state_transition("recovery_required", "discarded")

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                """
                SELECT candidate_id, pending_revision, state, last_apply_id,
                       source_baseline_json
                FROM config_pending_candidate
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
            if pending is None:
                raise ConfigConflictError(
                    f"Workspace pending candidate 不存在: {candidate_ref}"
                )
            candidate_id = str(pending[0])
            pending_revision = int(pending[1])
            current_state = str(pending[2])
            if current_state == "discarded":
                connection.execute("COMMIT")
                result = self.get_pending_config_candidate(
                    config_domain="workspace", candidate_id=candidate_id
                )
                if result is None:
                    raise RuntimeError("Workspace discarded candidate 读取后消失")
                return result
            if current_state not in {"pending_restart", "recovery_required"}:
                raise ConfigConflictError(
                    "Workspace pending discard 只允许 pending_restart/recovery_required: "
                    f"state={current_state}"
                )

            active = connection.execute(
                """
                SELECT active_revision, effective_digest, state
                FROM config_active_snapshot
                WHERE config_domain = 'workspace'
                """
            ).fetchone()
            if (
                active is None
                or str(active[2]) != "active"
                or int(active[0]) != expected_active_revision
                or str(active[1]) != expected_active_digest
            ):
                raise ConfigConflictError(
                    "Workspace pending discard 缺少匹配的安全 active 基线"
                )

            claim = connection.execute(
                """
                SELECT apply_id, lease_expires_at
                FROM config_apply_claim
                WHERE config_domain = 'workspace'
                """
            ).fetchone()
            if claim is not None:
                raise ConfigConflictError(
                    "Workspace pending discard 仍有 apply claim，必须先完成恢复或补偿: "
                    f"apply_id={claim[0]}"
                )

            stored_baseline = load_json_object(
                str(pending[4]), field="Workspace pending source baseline"
            )
            expected_sources = {
                str(key): detail
                for key, detail in expected_source_baseline.items()
                if isinstance(detail, dict)
                and detail.get("layer_revision") is not None
            }
            if stored_baseline != expected_source_baseline:
                raise ConfigConflictError(
                    "Workspace pending discard 的 source baseline 与候选不一致"
                )
            current_rows = connection.execute(
                _SOURCE_LAYER_BASELINE_SELECT
            ).fetchall()
            if {str(row[0]) for row in current_rows} != set(expected_sources):
                raise ConfigConflictError(
                    "Workspace pending discard 的 source layer 集合已变化"
                )
            for row in current_rows:
                detail = expected_sources[str(row[0])]
                actual_vrn = str(row[1]) if row[1] is not None else None
                expected_vrn = (
                    str(detail.get("vrn")) if detail.get("vrn") is not None else None
                )
                if (
                    expected_vrn != actual_vrn
                    or str(detail.get("presence")) != str(row[2])
                    or int(detail["layer_revision"]) != int(row[3])
                    or (
                        str(detail.get("layer_digest"))
                        if detail.get("layer_digest") is not None
                        else None
                    )
                    != (str(row[4]) if row[4] is not None else None)
                    or int(detail.get("source_generation", 0)) != int(row[5])
                ):
                    raise ConfigConflictError(
                        "Workspace pending discard 的 source layer 基线已变化: "
                        f"key={row[0]}"
                    )

            apply_id = str(pending[3]) if pending[3] is not None else None
            if apply_id is not None:
                journal = connection.execute(
                    """
                    SELECT state, side_effects_json
                    FROM config_apply_journal
                    WHERE apply_id = ?
                    """,
                    (apply_id,),
                ).fetchone()
                if journal is not None:
                    side_effects = json.loads(str(journal[1]))
                    if not isinstance(side_effects, list) or not all(
                        isinstance(item, dict) for item in side_effects
                    ):
                        raise TypeError("Workspace apply journal 副作用结构无效")
                    if side_effects and str(journal[0]) != "compensated":
                        raise ConfigConflictError(
                            "Workspace pending discard 缺少外部副作用补偿证明"
                        )
                    if str(journal[0]) in {
                        "applying",
                        "failed",
                        "recovery_required",
                    }:
                        connection.execute(
                            """
                            UPDATE config_apply_journal
                            SET state = 'compensated', last_error = ?, updated_at = ?
                            WHERE apply_id = ? AND state = ?
                            """,
                            (
                                reason,
                                utc_now_text(),
                                apply_id,
                                str(journal[0]),
                            ),
                        )

            validate_state_transition(
                current_state,  # type: ignore[arg-type]
                "discarded",
            )
            updated = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'discarded', last_error = ?
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                  AND state = ? AND pending_revision = ?
                """,
                (reason, candidate_ref, current_state, pending_revision),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError("Workspace pending discard 状态 CAS 失败")
            if event is not None:
                self._insert_config_event(
                    connection,
                    replace(
                        event,
                        pending_revision=(
                            event.pending_revision
                            if event.pending_revision is not None
                            else pending_revision
                        ),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_pending_config_candidate(
            config_domain="workspace", candidate_id=candidate_id
        )
        if result is None:
            raise RuntimeError("Workspace pending discard 提交后无法读取")
        return result
