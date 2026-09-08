"""v2 显式 replay-as-new-turn 操作 owner。

历史回放和恢复 execution 不属于 acceptance 的同一生命周期：本模块只负责
从既有 Turn 创建新的 target-local Turn/root，并记录 replay lineage。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from app.domain.itemized.hashing import content_hash as item_content_hash
from app.domain.itemized.runtime import ProvenanceEdge
from app.services.infrastructure.rollout_context.storage.transaction import (
    CommittedStorageCommit,
    load_committed_storage_commit,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutReplayMixin:
    """只拥有 replay_as_new_turn，不复用 source Turn/root identity。"""

    def replay_as_new_turn(
        self,
        thread_id: str,
        *,
        source_turn_id: str,
        checkpoint_ns: str = "",
        branch_id: str | None = None,
        acceptance_idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """从 source Turn 创建独立的新 Turn，不 dispatch 原 Turn。

        source root 只作为新输入的默认 payload 和 lineage；新 Turn 拥有全新的
        acceptance、root 与 initial execution。被取消的 source Turn 也只能走
        这个显式 API，不能通过 ``resume_turn`` 或原 turn_id dispatch。
        """
        source_turn_id = strict_text(source_turn_id, field="source_turn_id")
        branch_id = strict_optional_text(branch_id, field="branch_id")
        acceptance_idempotency_key = strict_optional_text(
            acceptance_idempotency_key,
            field="acceptance_idempotency_key",
        )
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            source = connection.execute(
                "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
                (source_turn_id,),
            ).fetchone()
            if source is None:
                raise KeyError(f"Turn 不存在: {source_turn_id}")
            root_item_id = strict_text(
                source[0], field=f"turn_records.root_input_item_id: {source_turn_id}"
            )
        roots = self.read_items(
            thread_id,
            item_ids=(root_item_id,),
            checkpoint_ns=checkpoint_ns,
        )
        if len(roots) != 1:
            raise RuntimeError(f"source Turn root item 不可恢复: {source_turn_id}")
        source_root = roots[0]
        source_payload_hash = item_content_hash(
            source_root.payload_kind,
            source_root.payload,
        )
        existing_commit: CommittedStorageCommit | None = None
        if acceptance_idempotency_key is not None:
            with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
                active_branch_row = connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                expected_branch = (
                    strict_text(
                        active_branch_row[0],
                        field="checkpoint_namespace_state.active_branch_id",
                    )
                    if branch_id is None
                    and active_branch_row is not None
                    else branch_id
                )
                existing = connection.execute(
                    """
                    SELECT ta.accepted_ingress_id, ta.turn_id, ta.payload_hash,
                           tr.source_branch_id, tr.replay_of_turn_id,
                           tr.root_input_item_id, tr.initial_execution_id,
                           tr.status, tr.final_item_id, sc.commit_id, sc.status
                    FROM turn_acceptances AS ta
                    JOIN turn_records AS tr ON tr.turn_id = ta.turn_id
                    LEFT JOIN storage_commits AS sc
                      ON sc.commit_kind = 'acceptance'
                     AND sc.subject_id = ta.turn_id
                     AND sc.idempotency_key = ta.acceptance_idempotency_key
                    WHERE ta.acceptance_idempotency_key = ?
                    ORDER BY sc.commit_id
                    LIMIT 1
                    """,
                    (acceptance_idempotency_key,),
                ).fetchone()
                if existing is not None and existing[9] is not None:
                    existing_commit = load_committed_storage_commit(
                        connection,
                        commit_id=strict_non_negative_int(
                            existing[9],
                            field="storage_commits.commit_id",
                        ),
                    )
            if existing is not None:
                existing_turn_id = strict_text(
                    existing[1], field="turn_acceptances.turn_id"
                )
                existing_payload_hash = strict_text(
                    existing[2], field="turn_acceptances.payload_hash"
                )
                existing_source_branch = strict_text(
                    existing[3], field="turn_records.source_branch_id"
                )
                existing_replay_source = strict_optional_text(
                    existing[4], field="turn_records.replay_of_turn_id"
                )
                existing_root_item_id = strict_text(
                    existing[5], field="turn_records.root_input_item_id"
                )
                existing_execution_id = strict_text(
                    existing[6], field="turn_records.initial_execution_id"
                )
                existing_status = strict_text(existing[7], field="turn_records.status")
                existing_final_item_id = strict_optional_text(
                    existing[8], field="turn_records.final_item_id"
                )
                if existing_replay_source != source_turn_id:
                    raise ValueError(
                        "replay acceptance_idempotency_key 冲突：已绑定其它 source Turn"
                    )
                if existing_payload_hash != source_payload_hash:
                    raise ValueError(
                        "replay acceptance_idempotency_key 冲突：source payload 不一致"
                    )
                if expected_branch is not None and existing_source_branch != expected_branch:
                    raise ValueError(
                        "replay acceptance_idempotency_key 冲突：source branch 不一致"
                    )
                if existing_commit is None:
                    raise RuntimeError(
                        "replay acceptance 已存在但 acceptance commit 缺失"
                    )
                return {
                    "turn_id": existing_turn_id,
                    "root_input_item_id": existing_root_item_id,
                    "initial_execution_id": existing_execution_id,
                    "status": existing_status,
                    "final_item_id": existing_final_item_id,
                    "commit_id": existing_commit.commit_id,
                    "idempotent": True,
                    "replay_of_turn_id": source_turn_id,
                    "operation": "replay_as_new_turn",
                }
        replay_id = (
            hashlib.sha256(
                f"{thread_id}:{source_turn_id}:{acceptance_idempotency_key}".encode()
            ).hexdigest()[:32]
            if acceptance_idempotency_key is not None
            else uuid4().hex
        )
        new_turn_id = f"turn-replay-{replay_id}"
        accepted_ingress_id = f"replay-ingress:{thread_id}:{replay_id}"
        acceptance_key = acceptance_idempotency_key or (
            f"replay:{thread_id}:{source_turn_id}:{replay_id}"
        )
        result = self.accept_turn(
            thread_id,
            accepted_ingress_id=accepted_ingress_id,
            acceptance_idempotency_key=acceptance_key,
            payload=source_root.payload,
            payload_kind=source_root.payload_kind,
            checkpoint_ns=checkpoint_ns,
            branch_id=branch_id,
            turn_id=new_turn_id,
            root_item_id=f"item-replay-{replay_id}",
            acceptance_metadata={
                "replay_of_turn_id": source_turn_id,
                "source_root_item_id": root_item_id,
            },
            replay_of_turn_id=source_turn_id,
            identity_origin="replay_as_new_turn",
        )
        self.register_provenance_edge(
            thread_id,
            ProvenanceEdge(
                edge_id=f"replay-lineage:{replay_id}",
                relation="replay_of",
                source_ref=f"{thread_id}:turn:{source_turn_id}",
                target_ref=f"{thread_id}:turn:{new_turn_id}",
                replay_input=True,
            ),
            checkpoint_ns=checkpoint_ns,
        )
        return {
            **result,
            "replay_of_turn_id": source_turn_id,
            "operation": "replay_as_new_turn",
        }


__all__ = ["RolloutReplayMixin"]
