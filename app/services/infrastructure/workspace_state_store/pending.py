"""Workspace pending config candidate 方法族。

WorkspaceStateStorePendingMixin 只承载本族方法，由 WorkspaceStateStore 装配；
依赖宿主提供的 _database，不复制其它族 SQL。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    ConfigLifecycleState,
    ConfigPendingCandidateRecord,
    build_secret_binding_summary,
    dump_json,
    load_json_object,
    validate_state_transition,
)

__all__ = ["WorkspaceStateStorePendingMixin"]

if TYPE_CHECKING:
    from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


class WorkspaceStateStorePendingMixin:
    def list_unfinished_source_edit_candidates(self, *, config_domain: str, candidate_digest: str) -> tuple[ConfigPendingCandidateRecord, ...]:
        store = cast("WorkspaceStateStore", self)
        connection = store._database.connection()
        try:
            rows = connection.execute(
                "SELECT candidate_id FROM config_pending_candidate WHERE config_domain=? AND candidate_digest=? "
                "AND source_edit_json IS NOT NULL AND state NOT IN ('active','discarded') ORDER BY pending_revision DESC",
                (config_domain, candidate_digest),
            ).fetchall()
        finally:
            connection.close()
        candidates = []
        for row in rows:
            candidate = store.get_pending_config_candidate(config_domain=config_domain, candidate_id=str(row[0]))
            if candidate is None:
                raise RuntimeError("已登记的来源 pending candidate 读取时消失")
            candidates.append(candidate)
        return tuple(candidates)

    def get_pending_config_candidate(
        self,
        *,
        config_domain: str,
        candidate_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ConfigPendingCandidateRecord | None:
        if candidate_id is not None and idempotency_key is not None:
            raise ValueError("读取 pending candidate 不能同时指定两个身份")
        connection = self._database.connection()
        try:
            if candidate_id is None and idempotency_key is None:
                row = connection.execute(
                    """
                    SELECT config_domain, candidate_id, idempotency_key, pending_revision,
                           payload_json, source_baseline_json, candidate_digest,
                           effective_digest, target_generation, fencing_token, state,
                           last_error, created_at, last_attempt_id, last_apply_id,
                           candidate_ref, base_active_revision,
                           source_generation, secret_bindings_json, source_edit_json
                    FROM config_pending_candidate
                    WHERE config_domain = ?
                    ORDER BY pending_revision DESC
                    LIMIT 1
                    """,
                    (config_domain,),
                ).fetchone()
            else:
                field = (
                    "candidate_id" if candidate_id is not None else "idempotency_key"
                )
                value = candidate_id if candidate_id is not None else idempotency_key
                row = connection.execute(
                    f"""
                    SELECT config_domain, candidate_id, idempotency_key, pending_revision,
                           payload_json, source_baseline_json, candidate_digest,
                           effective_digest, target_generation, fencing_token, state,
                           last_error, created_at, last_attempt_id, last_apply_id,
                           candidate_ref, base_active_revision,
                           source_generation, secret_bindings_json, source_edit_json
                    FROM config_pending_candidate
                    WHERE config_domain = ? AND {field} = ?
                    """,
                    (config_domain, value),
                ).fetchone()
            if row is None:
                return None
            return ConfigPendingCandidateRecord(
                config_domain=str(row[0]),
                candidate_id=str(row[1]),
                idempotency_key=str(row[2]),
                pending_revision=int(row[3]),
                payload=load_json_object(
                    str(row[4]), field="pending candidate payload"
                ),
                source_baseline=load_json_object(
                    str(row[5]), field="pending candidate source baseline"
                ),
                candidate_digest=str(row[6]),
                effective_digest=str(row[7]),
                target_generation=(str(row[8]) if row[8] is not None else None),
                fencing_token=str(row[9]) if row[9] is not None else None,
                state=cast(ConfigLifecycleState, str(row[10])),
                last_error=str(row[11]) if row[11] is not None else None,
                created_at=datetime.fromisoformat(str(row[12])),
                last_attempt_id=str(row[13]) if row[13] is not None else None,
                last_apply_id=str(row[14]) if row[14] is not None else None,
                candidate_ref=str(row[15]) if row[15] is not None else None,
                base_active_revision=(int(row[16]) if row[16] is not None else None),
                source_generation=(int(row[17]) if row[17] is not None else None),
                secret_bindings=load_json_object(
                    str(row[18]), field="pending candidate secret bindings"
                ),
                source_edit=(load_json_object(str(row[19]), field="pending candidate source edit") if row[19] is not None else None),
            )
        finally:
            connection.close()

    def create_pending_config_candidate(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        idempotency_key: str,
        payload: dict[str, object],
        source_baseline: dict[str, object],
        candidate_digest: str,
        effective_digest: str,
        target_generation: str | None,
        fencing_token: str | None,
        state: ConfigLifecycleState,
        last_error: str | None = None,
        candidate_ref: str | None = None,
        base_active_revision: int | None = None,
        source_generation: int | None = None,
        secret_bindings: dict[str, object] | None = None,
        source_edit: dict[str, object] | None = None,
        event: ConfigEventInput | None = None,
    ) -> ConfigPendingCandidateRecord:
        if state != "candidate_validated":
            validate_state_transition("none", state)
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT candidate_id, payload_json, candidate_digest, state,
                       source_baseline_json, base_active_revision, source_edit_json
                FROM config_pending_candidate
                WHERE config_domain = ? AND idempotency_key = ?
                """,
                (config_domain, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing[0]) != candidate_id
                    or str(existing[1]) != dump_json(payload)
                    or str(existing[2]) != candidate_digest
                    or str(existing[4]) != dump_json(source_baseline)
                    or (str(existing[6]) if existing[6] is not None else None) != (dump_json(source_edit) if source_edit is not None else None)
                    or (
                        existing[5] is not None
                        and int(existing[5]) != base_active_revision
                    )
                ):
                    raise ConfigConflictError(
                        "idempotency_key 已绑定不同 pending candidate: "
                        f"domain={config_domain}, key={idempotency_key}"
                    )
                existing_state = cast(ConfigLifecycleState, str(existing[3]))
                if existing_state != state:
                    validate_state_transition(existing_state, state)
                    connection.execute(
                        """
                        UPDATE config_pending_candidate
                        SET state = ?, last_error = ?, candidate_ref = COALESCE(?, candidate_ref)
                        WHERE config_domain = ? AND candidate_id = ? AND state = ?
                        """,
                        (
                            state,
                            last_error,
                            candidate_ref,
                            config_domain,
                            candidate_id,
                            existing_state,
                        ),
                    )
                if event is not None:
                    self._insert_config_event(connection, event)
                connection.execute("COMMIT")
            else:
                pending_revision = self._next_config_revision(
                    connection,
                    config_domain=config_domain,
                )
                connection.execute(
                    """
                    INSERT INTO config_pending_candidate(
                        config_domain, candidate_id, idempotency_key, pending_revision,
                        payload_json, source_baseline_json, candidate_digest,
                        effective_digest, target_generation, fencing_token, state,
                        last_error, created_at, candidate_ref,
                        base_active_revision, source_generation,
                        secret_bindings_json, source_edit_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config_domain,
                        candidate_id,
                        idempotency_key,
                        pending_revision,
                        dump_json(payload),
                        dump_json(source_baseline),
                        candidate_digest,
                        effective_digest,
                        target_generation,
                        fencing_token,
                        state,
                        last_error,
                        utc_now_text(),
                        candidate_ref,
                        base_active_revision,
                        source_generation,
                        dump_json(
                            secret_bindings or build_secret_binding_summary(payload)
                        ),
                        dump_json(source_edit) if source_edit is not None else None,
                    ),
                )
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
            config_domain=config_domain,
            candidate_id=candidate_id,
        )
        if result is None:
            raise RuntimeError(f"pending candidate 提交后无法读取: {candidate_id}")
        return result

    def update_pending_config_candidate_state(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        expected_state: ConfigLifecycleState,
        state: ConfigLifecycleState,
        last_error: str | None = None,
        candidate_ref: str | None = None,
        target_generation: str | None = None,
        fencing_token: str | None = None,
        event: ConfigEventInput | None = None,
    ) -> ConfigPendingCandidateRecord:
        validate_state_transition(expected_state, state)
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT pending_revision
                FROM config_pending_candidate
                WHERE config_domain = ? AND candidate_id = ? AND state = ?
                """,
                (config_domain, candidate_id, expected_state),
            ).fetchone()
            if existing is None:
                raise ConfigConflictError(
                    "pending candidate 状态 CAS 失败: "
                    f"domain={config_domain}, candidate={candidate_id}, expected={expected_state}"
                )
            cursor = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = ?, last_error = ?,
                    candidate_ref = COALESCE(?, candidate_ref),
                    target_generation = COALESCE(?, target_generation),
                    fencing_token = COALESCE(?, fencing_token)
                WHERE config_domain = ? AND candidate_id = ? AND state = ?
                """,
                (
                    state,
                    last_error,
                    candidate_ref,
                    target_generation,
                    fencing_token,
                    config_domain,
                    candidate_id,
                    expected_state,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError(
                    "pending candidate 状态 CAS 失败: "
                    f"domain={config_domain}, candidate={candidate_id}, expected={expected_state}"
                )
            if event is not None:
                self._insert_config_event(
                    connection,
                    replace(
                        event,
                        pending_revision=(
                            event.pending_revision
                            if event.pending_revision is not None
                            else int(existing[0])
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
            config_domain=config_domain,
            candidate_id=candidate_id,
        )
        if result is None:
            raise RuntimeError(f"pending candidate 更新后无法读取: {candidate_id}")
        return result

    def complete_unchanged_config_candidate(self, *, config_domain: str, candidate_id: str) -> None:
        """仅完成来源幂等记录；有效配置未变时不提升 active、不产生配置事件。"""
        connection = cast("WorkspaceStateStore", self)._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT candidate_digest, base_active_revision, source_edit_json FROM config_pending_candidate "
                "WHERE config_domain=? AND candidate_id=? AND state='candidate_validated'",
                (config_domain, candidate_id),
            ).fetchone()
            active = connection.execute(
                "SELECT effective_digest, active_revision FROM config_active_snapshot WHERE config_domain=?",
                (config_domain,),
            ).fetchone()
            if row is None or row[2] is None or active is None or (row[0], row[1]) != (active[0], active[1]):
                raise ConfigConflictError("无有效变化的来源候选与当前 active 不一致")
            intent = load_json_object(str(row[2]), field="source edit intent")
            source = connection.execute("SELECT layer_digest FROM config_source_layers WHERE config_key=?", (intent["source_key"],)).fetchone()
            if source is None or source[0] != intent["file_digest"]:
                raise ConfigConflictError("无有效变化的来源候选与当前 layer 不一致")
            # 原状态机的两个转换在同一事务完成；这里没有待执行的运行资源副作用。
            transitions: tuple[tuple[ConfigLifecycleState, ConfigLifecycleState], ...] = (("candidate_validated", "applying"), ("applying", "active"))
            for previous, state in transitions:
                validate_state_transition(previous, state)
                connection.execute("UPDATE config_pending_candidate SET state=? WHERE config_domain=? AND candidate_id=? AND state=?",
                                   (state, config_domain, candidate_id, previous))
            connection.commit()
        finally:
            connection.close()

    def bind_source_edit_config_candidate(
        self, *, candidate: ConfigPendingCandidateRecord, payload: dict[str, object],
        source_baseline: dict[str, object], candidate_digest: str, source_generation: int,
        source_edit: dict[str, object],
    ) -> ConfigPendingCandidateRecord:
        """原来源 intent 绑定导入结果；尚未取得 claim 时才能推进同一候选行。"""
        store = cast("WorkspaceStateStore", self)
        connection = store._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, source_edit_json FROM config_pending_candidate WHERE config_domain=? AND candidate_id=?",
                (candidate.config_domain, candidate.candidate_id),
            ).fetchone()
            intent = load_json_object(str(row[1]), field="source edit intent") if row is not None and row[1] is not None else None
            if (row is None or row[0] != "candidate_validated" or intent is None
                    or intent.get("request_digest") != source_edit.get("request_digest")):
                raise ConfigConflictError("来源 intent 仅能在原 candidate_validated 行绑定导入结果")
            if candidate.candidate_digest != candidate_digest or candidate.payload != payload:
                raise ConfigConflictError("来源 intent 的原预检内容已改变，禁止绑定另一份有效配置")
            for key, baseline in candidate.source_baseline.items():
                if key != intent["source_key"] and source_baseline.get(key) != baseline:
                    raise ConfigConflictError("来源 intent 的其它来源层 CAS 基线已改变")
            if connection.execute("SELECT 1 FROM config_apply_claim WHERE config_domain=?", (candidate.config_domain,)).fetchone() is not None:
                raise ConfigConflictError("已有配置 apply claim，禁止重新绑定来源 intent")
            active = connection.execute(
                "SELECT active_revision, effective_digest FROM config_active_snapshot WHERE config_domain=?",
                (candidate.config_domain,),
            ).fetchone()
            if active is None or (active[0], active[1]) != (intent["expected_active_revision"], intent["expected_active_digest"]):
                raise ConfigConflictError("来源 intent 的原 active CAS 基线已改变")
            source = connection.execute(
                "SELECT layer_revision, layer_digest FROM config_source_layers WHERE config_key=?", (intent["source_key"],),
            ).fetchone()
            base_revision = intent["base_layer_revision"]
            target_revision = (int(base_revision) if isinstance(base_revision, int) else 0) + (intent["base_layer_digest"] != intent["file_digest"])
            if source is None or (source[0], source[1]) != (target_revision, intent["file_digest"]):
                raise ConfigConflictError("来源 intent 的目标层尚未精确导入或已被其它写入改变")
            imported = {**intent, "stage": "imported"}
            updated = connection.execute(
                "UPDATE config_pending_candidate SET payload_json=?, source_baseline_json=?, candidate_digest=?, "
                "effective_digest=?, source_generation=?, secret_bindings_json=?, source_edit_json=? "
                "WHERE config_domain=? AND candidate_id=? AND state='candidate_validated' AND source_edit_json=?",
                (dump_json(payload), dump_json(source_baseline), candidate_digest, candidate_digest, source_generation,
                 dump_json(build_secret_binding_summary(payload)), dump_json(imported), candidate.config_domain,
                 candidate.candidate_id, dump_json(intent)),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError("来源 intent 导入结果绑定 CAS 冲突")
            connection.commit()
        finally:
            connection.close()
        result = store.get_pending_config_candidate(config_domain=candidate.config_domain, candidate_id=candidate.candidate_id)
        if result is None:
            raise RuntimeError("来源 intent 绑定后无法读取原 pending candidate")
        return result

    def load_pending_config_candidate(
        self,
        *,
        candidate_ref: str,
        allow_recovery: bool = False,
        allow_discarded: bool = False,
    ) -> ConfigPendingCandidateRecord:
        """按不透明 ref 精确读取 Workspace pending，禁止回退到 active。"""

        if not candidate_ref:
            raise ValueError("Workspace candidate_ref 不能为空")
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_domain, candidate_id, idempotency_key, pending_revision,
                       payload_json, source_baseline_json, candidate_digest,
                       effective_digest, target_generation, fencing_token, state,
                       last_error, created_at, last_attempt_id, last_apply_id,
                       candidate_ref
                FROM config_pending_candidate
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ConfigConflictError(
                f"Workspace candidate_ref 不存在或不属于当前工作区: {candidate_ref}"
            )
        result = self.get_pending_config_candidate(
            config_domain="workspace",
            candidate_id=str(row[1]),
        )
        if result is None:
            raise RuntimeError("Workspace pending candidate 读取后消失")
        if result.candidate_ref != candidate_ref:
            raise ConfigConflictError("Workspace candidate_ref 读取校验失败")
        allowed_states = {"pending_restart", "applying"}
        if allow_recovery:
            allowed_states.add("recovery_required")
        if allow_discarded:
            allowed_states.add("discarded")
        if result.state not in allowed_states:
            raise ConfigConflictError(
                "Workspace candidate_ref 当前不允许启动: "
                f"candidate={result.candidate_id}, state={result.state}"
            )
        return result
