"""v2 cross-session fork identity/materialization owners。

所有复制操作只消费已提交 v2 storage state；source 坐标进入 lineage audit，
目标运行时只使用 target-local identity。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from uuid import uuid4

from app.domain.itemized.execution_admission import (
    user_turn_admission_payload_hash,
)
from app.services.infrastructure.rollout_context.fork.identity import (
    target_local_acceptance_identity,
)
from app.services.infrastructure.rollout_context.fork.lineage.legacy import (
    _legacy_source_identity_map,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    required_text,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
)


class ForkAcceptanceMappingMixin:
    """复制后的 ingress 与 acceptance key 本地化。"""

    def _map_copied_acceptance_identities(
        self,
        connection: sqlite3.Connection,
        *,
        source_session_id: str,
        target_session_id: str,
        fork_id: str,
        checkpoint_ns: str,
        timestamp: str,
    ) -> None:
        """把完整副本中的 acceptance identity 改为 target-local key。

        full rollout copy 先复制 SQLite 文件，因此 acceptance 表中暂时仍有
        source 的裸 key。这里在目标 fork 的收敛事务内完成一次性映射；JSONL
        payload 保持 immutable，source identity 通过 lineage/mapping 表审计。
        """
        source_session_id = required_text(source_session_id, field="source_session_id")
        target_session_id = required_text(target_session_id, field="target_session_id")
        fork_id = required_text(fork_id, field="fork_id")
        timestamp = required_text(timestamp, field="timestamp")
        source_thread_id = self._storage._resolve_thread_identity(source_session_id, None)
        target_thread_id = self._storage._resolve_thread_identity(target_session_id, None)
        rows = connection.execute(
            "SELECT accepted_ingress_id, acceptance_idempotency_key, turn_id, payload_hash, identity_origin FROM turn_acceptances"
        ).fetchall()
        legacy_source_map = _legacy_source_identity_map(
            connection,
            source_session_id=source_session_id,
            target_session_id=target_session_id,
        )
        for source_ingress, source_key, turn_id, payload_hash, identity_origin in rows:
            source_ingress_value = required_text(
                source_ingress, field="turn_acceptances.accepted_ingress_id"
            )
            source_key_value = required_text(
                source_key, field="turn_acceptances.acceptance_idempotency_key"
            )
            turn_id = required_text(turn_id, field="turn_acceptances.turn_id")
            payload_hash = required_text(
                payload_hash, field=f"turn_acceptances.payload_hash:{turn_id}"
            )
            identity_origin = required_text(
                identity_origin, field=f"turn_acceptances.identity_origin:{turn_id}"
            )
            lineage_source_ingress = legacy_source_map.get("accepted_ingress", {}).get(
                source_ingress_value, source_ingress_value
            )
            lineage_source_key = legacy_source_map.get(
                "acceptance_idempotency_key", {}
            ).get(source_key_value, source_key_value)
            target_ingress, target_key = target_local_acceptance_identity(
                target_session_id=target_session_id,
                fork_id=fork_id,
                source_accepted_ingress_id=source_ingress_value,
                source_acceptance_key=source_key_value,
            )
            existing_mapping = connection.execute(
                "SELECT target_local_id FROM fork_identity_mappings WHERE fork_id = ? AND entity_type = 'accepted_ingress' AND source_local_id = ?",
                # v1 migration 的 mapping source_local_id 是可审计的
                # legacy:v1:accepted-ingress:*，不是 copied table 当前暂存的
                # target-looking raw ingress。重复执行 full-copy staging 时
                # 必须按同一 lineage 坐标命中幂等记录。
                (fork_id, lineage_source_ingress),
            ).fetchone()
            if existing_mapping is not None:
                if (
                    strict_text(
                        existing_mapping[0],
                        field="fork_identity_mappings.target_local_id",
                    )
                    != target_ingress
                ):
                    raise ValueError("fork acceptance ingress mapping identity 冲突")
                continue
            if (
                connection.execute(
                    "SELECT 1 FROM turn_acceptances WHERE accepted_ingress_id = ? OR acceptance_idempotency_key = ?",
                    (target_ingress, target_key),
                ).fetchone()
                is not None
            ):
                raise ValueError("target acceptance identity 冲突")
            source_turn_id = turn_id
            target_turn_id = self._full_copy_identity(
                fork_id,
                target_session_id,
                "turn",
                source_turn_id,
            )
            execution_admissions: list[tuple[str, str, str, str, str]] = []
            source_executions = connection.execute(
                "SELECT execution_id, session_id, thread_id, turn_id, execution_origin, "
                "job_id, admission_idempotency_key, admission_payload_hash, "
                "accepted_ingress_id FROM executions "
                "WHERE session_id = ? AND thread_id = ? AND turn_id = ? "
                "ORDER BY execution_ordinal",
                (target_session_id, target_thread_id, source_turn_id),
            ).fetchall()
            if not source_executions:
                raise RuntimeError(
                    "full_rollout_copy Turn acceptance 缺少 execution: "
                    f"turn_id={source_turn_id}"
                )
            for source_execution in source_executions:
                (
                    raw_execution_id,
                    raw_execution_session_id,
                    raw_execution_thread_id,
                    raw_execution_turn_id,
                    raw_execution_origin,
                    raw_job_id,
                    raw_admission_key,
                    raw_admission_hash,
                    raw_execution_ingress,
                ) = source_execution
                source_execution_id = required_text(
                    raw_execution_id, field="source.execution_id"
                )
                if (
                    required_text(raw_execution_session_id, field="execution.session_id"),
                    required_text(raw_execution_thread_id, field="execution.thread_id"),
                    required_text(raw_execution_turn_id, field="execution.turn_id"),
                ) != (target_session_id, target_thread_id, source_turn_id):
                    raise RuntimeError(
                        "full_rollout_copy execution owner 与 Turn acceptance 不匹配: "
                        f"execution_id={source_execution_id}"
                    )
                if required_text(raw_execution_origin, field="execution.origin") != "user_turn":
                    raise RuntimeError(
                        "full_rollout_copy Turn 只能包含 user_turn execution: "
                        f"execution_id={source_execution_id}"
                    )
                source_job_id = required_text(raw_job_id, field="execution.job_id")
                source_admission_key = required_text(
                    raw_admission_key,
                    field="execution.admission_idempotency_key",
                )
                source_admission_hash = required_text(
                    raw_admission_hash,
                    field="execution.admission_payload_hash",
                )
                execution_ingress = required_text(
                    raw_execution_ingress,
                    field="execution.accepted_ingress_id",
                )
                if execution_ingress != source_ingress_value:
                    raise RuntimeError(
                        "full_rollout_copy execution ingress 与 Turn acceptance 不匹配: "
                        f"execution_id={source_execution_id}"
                    )
                expected_source_hash = user_turn_admission_payload_hash(
                    session_id=source_session_id,
                    thread_id=source_thread_id,
                    turn_id=source_turn_id,
                    execution_id=source_execution_id,
                    job_id=source_job_id,
                    accepted_ingress_id=source_ingress_value,
                    admission_idempotency_key=source_admission_key,
                    payload_hash=payload_hash,
                )
                if source_admission_hash != expected_source_hash:
                    raise RuntimeError(
                        "full_rollout_copy source execution admission preimage 不匹配: "
                        f"execution_id={source_execution_id}"
                    )
                target_execution_id = self._full_copy_identity(
                    fork_id,
                    target_session_id,
                    "execution",
                    source_execution_id,
                )
                admission_digest = hashlib.sha256(
                    (
                        f"{target_session_id}|{fork_id}|{target_execution_id}|"
                        f"{target_ingress}|{source_admission_key}"
                    ).encode()
                ).hexdigest()[:32]
                target_admission_key = (
                    f"fork-admission:{target_session_id}:{admission_digest}"
                )
                target_admission_hash = user_turn_admission_payload_hash(
                    session_id=target_session_id,
                    thread_id=target_thread_id,
                    turn_id=target_turn_id,
                    execution_id=target_execution_id,
                    job_id=source_job_id,
                    accepted_ingress_id=target_ingress,
                    admission_idempotency_key=target_admission_key,
                    payload_hash=payload_hash,
                )
                source_job_ref = {
                    "type": "GlobalEntityRef",
                    "session_id": source_session_id,
                    "thread_id": source_thread_id,
                    "entity_type": "job",
                    "local_id": source_job_id,
                }
                admission_lineage = {
                    "identity_mode": "remapped_target_local",
                    "fork_id": fork_id,
                    "source": {
                        "session_id": source_session_id,
                        "thread_id": source_thread_id,
                        "turn_id": source_turn_id,
                        "execution_id": source_execution_id,
                        "job_ref": source_job_ref,
                        "accepted_ingress_id": source_ingress_value,
                        "admission_idempotency_key": source_admission_key,
                        "admission_payload_hash": source_admission_hash,
                        "payload_hash": payload_hash,
                    },
                    "target": {
                        "session_id": target_session_id,
                        "thread_id": target_thread_id,
                        "turn_id": target_turn_id,
                        "execution_id": target_execution_id,
                        "job_ref": source_job_ref,
                        "accepted_ingress_id": target_ingress,
                        "admission_idempotency_key": target_admission_key,
                        "admission_payload_hash": target_admission_hash,
                        "payload_hash": payload_hash,
                    },
                }
                execution_admissions.append(
                    (
                        source_execution_id,
                        source_admission_key,
                        target_admission_key,
                        target_admission_hash,
                        _json(admission_lineage),
                    )
                )
            lineage = {
                "identity_origin": (
                    "legacy_migrated_fork_copied"
                    if source_ingress_value
                    in legacy_source_map.get("accepted_ingress", {})
                    else "fork_copied"
                ),
                "source": {
                    "session_id": source_session_id,
                    "accepted_ingress_id": lineage_source_ingress,
                    "acceptance_idempotency_key": lineage_source_key,
                },
                "target": {
                    "session_id": target_session_id,
                    "accepted_ingress_id": target_ingress,
                    "acceptance_idempotency_key": target_key,
                },
            }
            acceptance_result = connection.execute(
                "UPDATE turn_acceptances SET accepted_ingress_id = ?, acceptance_idempotency_key = ?, source_session_id = ?, source_accepted_ingress_id = ?, source_acceptance_idempotency_key = ?, identity_origin = 'fork_copied' WHERE accepted_ingress_id = ?",
                (
                    target_ingress,
                    target_key,
                    source_session_id,
                    lineage_source_ingress,
                    lineage_source_key,
                    source_ingress_value,
                ),
            )
            if acceptance_result.rowcount != 1:
                raise RuntimeError(
                    f"full_rollout_copy acceptance row 更新失败: {turn_id}"
                )
            turn_result = connection.execute(
                "UPDATE turn_records SET accepted_ingress_id = ?, acceptance_idempotency_key = ? WHERE turn_id = ?",
                (target_ingress, target_key, turn_id),
            )
            if turn_result.rowcount != 1:
                raise RuntimeError(
                    f"full_rollout_copy Turn acceptance 更新失败: {turn_id}"
                )
            for (
                source_execution_id,
                source_admission_key,
                target_admission_key,
                target_admission_hash,
                admission_lineage_json,
            ) in execution_admissions:
                result = connection.execute(
                    "UPDATE executions SET accepted_ingress_id = ?, "
                    "admission_idempotency_key = ?, admission_payload_hash = ?, "
                    "updated_at = ? WHERE session_id = ? AND thread_id = ? "
                    "AND turn_id = ? AND execution_id = ?",
                    (
                        target_ingress,
                        target_admission_key,
                        target_admission_hash,
                        timestamp,
                        target_session_id,
                        target_thread_id,
                        source_turn_id,
                        source_execution_id,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        "full_rollout_copy execution admission 更新失败: "
                        f"execution_id={source_execution_id}"
                    )
                mapping_result = connection.execute(
                    "INSERT INTO fork_identity_mappings(mapping_id, fork_id, "
                    "source_session_id, target_session_id, entity_type, "
                    "source_local_id, target_local_id, source_offset, target_offset, "
                    "lineage_json, created_at) VALUES (?, ?, ?, ?, "
                    "'admission_idempotency_key', ?, ?, NULL, NULL, ?, ?)",
                    (
                        uuid4().hex,
                        fork_id,
                        source_session_id,
                        target_session_id,
                        source_admission_key,
                        target_admission_key,
                        admission_lineage_json,
                        timestamp,
                    ),
                )
                if mapping_result.rowcount != 1:
                    raise RuntimeError(
                        "fork execution admission mapping 写入失败: "
                        f"execution_id={source_execution_id}"
                    )
            # acceptance commit 也属于 acceptance identity 的持久化索引。
            # full_rollout_copy 复制的是同一份 SQLite，若只改
            # turn_acceptances/turn_records 而保留 commit 的 source key，重启
            # 后 accept_turn 会误判为“acceptance 已存在但 commit 缺失”。
            commit_metadata_row = connection.execute(
                "SELECT metadata_json FROM storage_commits WHERE commit_kind = 'acceptance' AND subject_id = ? AND idempotency_key = ?",
                (turn_id, source_key_value),
            ).fetchone()
            if commit_metadata_row is None:
                if identity_origin != "checkpoint_origin":
                    raise RuntimeError(
                        "full_rollout_copy acceptance identity 缺少对应 storage commit: "
                        f"turn_id={turn_id}"
                    )
                checkpoint_commit = connection.execute(
                    "SELECT ic.commit_id, ic.content_hash "
                    "FROM item_catalog AS ic "
                    "JOIN turn_records AS tr ON tr.root_input_item_id = ic.item_id "
                    "WHERE tr.turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if (
                    checkpoint_commit is None
                    or checkpoint_commit[0] is None
                    or strict_text(
                        checkpoint_commit[1],
                        field=f"item_catalog.content_hash:{turn_id}",
                    )
                    != payload_hash
                ):
                    raise RuntimeError(
                        "full_rollout_copy checkpoint-origin acceptance 缺少一致的 root item commit: "
                        f"turn_id={turn_id}"
                    )
                # checkpoint-origin 的 acceptance 与 root item 在同一
                # item-bearing checkpoint commit 中建立；不能伪造第二条
                # acceptance commit，否则会破坏“一次事务一个 storage commit”
                # 与 immutable item locator 的合同。accept_turn 的幂等读取
                # 也会回到同一个 root item commit。
            else:
                commit_metadata_text = required_text(
                    commit_metadata_row[0],
                    field=f"storage_commits.metadata_json:{turn_id}",
                )
                try:
                    commit_metadata = json.loads(commit_metadata_text)
                except json.JSONDecodeError as error:
                    raise RuntimeError(
                        "full_rollout_copy acceptance commit metadata 无法解析: "
                        f"turn_id={turn_id}"
                    ) from error
                if not isinstance(commit_metadata, Mapping):
                    raise TypeError(
                        "full_rollout_copy acceptance commit metadata 必须是 object: "
                        f"turn_id={turn_id}"
                    )
                commit_metadata = {
                    **dict(commit_metadata),
                    "accepted_ingress_id": target_ingress,
                }
                commit_result = connection.execute(
                    "UPDATE storage_commits SET subject_id = ?, idempotency_key = ?, metadata_json = ? WHERE commit_kind = 'acceptance' AND subject_id = ? AND idempotency_key = ?",
                    (
                        turn_id,
                        target_key,
                        _json(commit_metadata),
                        turn_id,
                        source_key_value,
                    ),
                )
                if commit_result.rowcount != 1:
                    raise RuntimeError(
                        f"full_rollout_copy acceptance commit 更新失败: {turn_id}"
                    )
            ingress_mapping_result = connection.execute(
                "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at) VALUES (?, ?, ?, ?, 'accepted_ingress', ?, ?, NULL, NULL, ?, ?)",
                (
                    uuid4().hex,
                    fork_id,
                    source_session_id,
                    target_session_id,
                    lineage_source_ingress,
                    target_ingress,
                    _json(lineage),
                    timestamp,
                ),
            )
            if ingress_mapping_result.rowcount != 1:
                raise RuntimeError(
                    f"fork acceptance ingress mapping 写入失败: {turn_id}"
                )
            key_mapping_result = connection.execute(
                "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at) VALUES (?, ?, ?, ?, 'acceptance_idempotency_key', ?, ?, NULL, NULL, ?, ?)",
                (
                    uuid4().hex,
                    fork_id,
                    source_session_id,
                    target_session_id,
                    lineage_source_key,
                    target_key,
                    _json(lineage),
                    timestamp,
                ),
            )
            if key_mapping_result.rowcount != 1:
                raise RuntimeError(f"fork acceptance key mapping 写入失败: {turn_id}")
        internal_rows = connection.execute(
            "SELECT execution_id, job_id, admission_idempotency_key, admission_payload_hash, "
            "turn_id, accepted_ingress_id FROM executions WHERE execution_origin = 'internal'"
        ).fetchall()
        if not internal_rows:
            return
        with self._storage._connect(source_thread_id, checkpoint_ns, session_id=source_session_id,
                           read_only=True) as source_connection:
            for execution_id, job_id, key, payload_hash, turn_id, ingress in internal_rows:
                if turn_id is not None or ingress is not None:
                    raise RuntimeError("full copy internal execution 不得绑定 Turn/ingress")
                preimage = self._validate_fork_internal_admission(
                    source_connection, session_id=source_session_id, thread_id=source_thread_id,
                    checkpoint_ns=checkpoint_ns, execution_id=execution_id,
                    job_id=job_id, admission_key=key, admission_hash=payload_hash,
                )
                target_key = self._full_copy_identity(fork_id, target_session_id, "admission_idempotency_key", key)
                target_execution = self._full_copy_identity(fork_id, target_session_id, "execution", execution_id)
                job_ref = {"type": "GlobalEntityRef", "session_id": source_session_id,
                           "thread_id": source_thread_id, "entity_type": "job", "local_id": job_id}
                self._ensure_fork_identity_mapping(
                    connection, source_session_id=source_session_id, target_session_id=target_session_id,
                    fork_id=fork_id, entity_type="admission_idempotency_key", source_id=key, target_id=target_key,
                    lineage={"source": {"session_id": source_session_id, "thread_id": source_thread_id,
                                        "execution_id": execution_id, "execution_origin": "internal", "job_ref": job_ref,
                                        "turn_id": None, "accepted_ingress_id": None,
                                        "admission_idempotency_key": key, "admission_payload_hash": payload_hash},
                             "target": {"session_id": target_session_id, "thread_id": target_thread_id,
                                        "execution_id": target_execution, "execution_origin": "internal", "job_ref": job_ref,
                                        "turn_id": None, "accepted_ingress_id": None,
                                        "admission_idempotency_key": target_key, "admission_payload_hash": payload_hash},
                             "admission_preimage": preimage},
                    timestamp=timestamp,
                )
                result = connection.execute(
                    "UPDATE executions SET admission_idempotency_key = ?, updated_at = ? WHERE execution_id = ?",
                    (target_key, timestamp, execution_id),
                )
                if result.rowcount != 1:
                    raise RuntimeError("full copy internal admission key 更新失败")
