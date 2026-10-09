"""独立 staging 中的 v2 item、Turn 和投影安装。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.migration.semantics import (
    candidate_audit,
    terminal_evidence,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore


class LegacyImportBuilder:
    def __init__(self, storage: RolloutContextStore) -> None:
        self.storage = storage

    def build_import(
        self,
        source_session_id: str,
        *,
        source_thread_id: str,
        target_session_id: str,
        target_thread_id: str,
        report: dict[str, object],
        migration_id: str,
        checkpoint_ns: str,
    ) -> dict[str, object]:
        """将 v1 source 安装到全新的 v2 target，保留 source 只读原件。"""
        from app.services.infrastructure.rollout_context.migration.legacy_adapter import (
            LegacyRolloutAdapter,
        )

        self.storage.storage.initialize(
            target_session_id, checkpoint_ns, thread_id=target_thread_id
        )
        adapter = LegacyRolloutAdapter(source_session_id)
        migrated: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        candidates = report["candidates"]
        if not isinstance(candidates, list):
            raise TypeError("migration candidates 必须是 list")
        seen_seeds: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise TypeError("legacy migration candidate 结构非法")
            if candidate.get("candidate_status") not in {
                "accepted",
                "legacy_missing_turn_id",
            }:
                rejected.append(dict(candidate))
                continue
            mapped = adapter.map_candidate(candidate, item_sequence_start=1)
            items = mapped.get("items")
            identity = mapped.get("identity")
            if (
                not isinstance(items, list)
                or not items
                or not isinstance(identity, Mapping)
            ):
                raise RuntimeError("legacy migration mapping 缺少 root/identity")
            root = items[0]
            if not isinstance(root, CanonicalItemRecord):
                raise TypeError("legacy migration root item 类型非法")
            source_seed = identity["legacy_seed_hash"]
            if source_seed in seen_seeds:
                raise RuntimeError("legacy seed collision: 拒绝重复 acceptance")
            seen_seeds.add(source_seed)
            target_identity_seed = sha256_jcs(
                {
                    "target_session_id": target_session_id,
                    "target_thread_id": target_thread_id,
                    "candidate_key": candidate["candidate_key"],
                    "legacy_seed_hash": source_seed,
                }
            ).rsplit(":", 1)[1]
            target_turn = f"legacy-migrated:{target_identity_seed}"
            target_root_item_id = f"item-legacy:{target_identity_seed}:root"
            # adapter identity 只描述 source legacy 坐标，不能直接占用 target
            # session 的 acceptance namespace。target-local identity 必须同时
            # 对 ingress、幂等 key 和 initial execution 做确定性重映射；source
            # 值仅通过 typed extensions/legacy report 保留审计 lineage。
            target_accepted_ingress_id = f"legacy-ingress:{target_identity_seed}"
            target_acceptance_key = f"legacy-migration:{target_identity_seed}"
            target_initial_execution_id = f"legacy-execution:{target_identity_seed}"
            accepted = self.storage.executions.accept_turn(
                target_session_id,
                thread_id=target_thread_id,
                accepted_ingress_id=target_accepted_ingress_id,
                acceptance_idempotency_key=target_acceptance_key,
                job_id=f"legacy-job:{target_identity_seed}",
                admission_idempotency_key=f"legacy-admission:{target_identity_seed}",
                payload=root.payload,
                provenance_relations=root.provenance_relations,
                payload_kind=root.payload_kind,
                turn_id=target_turn,
                root_item_id=target_root_item_id,
                initial_execution_id=target_initial_execution_id,
                extensions={
                    "boxteam.acceptance.annotations": {
                        "extension_version": "1",
                        "protection": "public",
                        "value": {
                            "legacy_annotations": root.extensions,
                            "legacy_artifact_ref": report["raw_artifact_ref"],
                            "legacy_source_session_id": source_session_id,
                            "legacy_source_thread_id": source_thread_id,
                            "legacy_source_turn_id": candidate.get("turn_id"),
                            "legacy_seed_hash": identity["legacy_seed_hash"],
                            "legacy_source_item_id": root.item_id,
                            "legacy_source_accepted_ingress_id": identity[
                                "accepted_ingress_id"
                            ],
                            "legacy_source_acceptance_idempotency_key": identity[
                                "acceptance_idempotency_key"
                            ],
                            "legacy_source_initial_execution_id": identity[
                                "initial_execution_id"
                            ],
                        },
                    }
                },
                identity_origin="legacy_synthetic",
                checkpoint_ns=checkpoint_ns,
            )
            target_items = [accepted]
            target_item_by_source_id = {root.item_id: target_root_item_id}
            target_members: list[CanonicalItemRecord] = []
            with self.storage.storage._connect(
                target_thread_id,
                checkpoint_ns,
                session_id=target_session_id,
                read_only=True,
            ) as connection:
                next_sequence = strict_non_negative_int(
                    connection.execute(
                        "SELECT last_item_sequence + 1 FROM database_meta WHERE singleton_id = 1"
                    ).fetchone()[0],
                    field="migration.next_item_sequence",
                )
            for item in items[1:]:
                if not isinstance(item, CanonicalItemRecord):
                    raise TypeError("legacy migration item 类型非法")
                payload = item.payload
                if item.semantic_kind in {"tool_call", "tool_result"}:
                    if not isinstance(payload, Mapping):
                        raise TypeError("legacy tool payload 必须是 object")
                    payload = dict(payload)
                    for field in (
                        "tool_call_id",
                        "tool_invocation_id",
                        "tool_attempt_id",
                        "result_id",
                    ):
                        if field in payload:
                            payload[field] = (
                                "legacy-tool:"
                                + sha256_jcs(
                                    {
                                        "target_session_id": target_session_id,
                                        "target_thread_id": target_thread_id,
                                        "source_session_id": source_session_id,
                                        "source_thread_id": source_thread_id,
                                        "field": field,
                                        "source_id": payload[field],
                                    }
                                ).rsplit(":", 1)[1]
                            )
                member = CanonicalItemRecord.create(
                    item_sequence=next_sequence,
                    item_id=(
                        f"item-legacy:{target_identity_seed}:member:"
                        f"{hashlib.sha256(item.item_id.encode('utf-8')).hexdigest()[:24]}"
                    ),
                    semantic_kind=item.semantic_kind,
                    payload_kind=item.payload_kind,
                    status=item.status,
                    producer_ref={
                        **dict(item.producer_ref),
                        "producer_id": (
                            "legacy-producer:"
                            f"{target_identity_seed}:"
                            f"{hashlib.sha256(item.item_id.encode('utf-8')).hexdigest()[:24]}"
                        ),
                    },
                    payload=payload,
                    created_at=item.created_at,
                    extensions={
                        **item.extensions,
                        "boxteam.legacy.import": {
                            "extension_version": "1",
                            "protection": "public",
                            "value": {
                                "legacy_artifact_ref": report["raw_artifact_ref"]
                            },
                        },
                    },
                    projection_identity=ItemProjectionIdentity(
                        projection_message_id=f"legacy:{target_identity_seed}:{hashlib.sha256(item.item_id.encode()).hexdigest()}",
                    ),
                    projection_group=item.projection_group,
                    tool_result_evidence=item.tool_result_evidence,
                    source_manifest=item.source_manifest,
                    provenance_relations=item.provenance_relations,
                    turn_id=target_turn if item.turn_id is not None else None,
                    turn_scope=item.turn_scope,
                    message_group_id=(
                        "legacy-group:"
                        f"{target_identity_seed}:"
                        f"{hashlib.sha256(str(item.message_group_id).encode('utf-8')).hexdigest()[:24]}"
                        if item.message_group_id is not None
                        else None
                    ),
                    wire_role=item.wire_role,
                )
                target_members.append(member)
                target_item_by_source_id[item.item_id] = member.item_id
                next_sequence += 1
            candidate_records = candidate["records"]
            migrated_status, final_source_item_id, final_reason = terminal_evidence(
                candidate_records, items
            )
            final_item_id = target_item_by_source_id.get(final_source_item_id)
            terminal_members = tuple(
                member
                for member in target_members
                if member.turn_id == target_turn
                and member.turn_scope.value == "turn_member"
            )
            runtime_notices = tuple(
                member for member in target_members if member not in terminal_members
            )
            self.storage.executions.converge_execution(
                target_session_id,
                thread_id=target_thread_id,
                turn_id=target_turn,
                execution_id=accepted["initial_execution_id"],
                outcome=migrated_status,
                turn_status=migrated_status,
                items=terminal_members,
                final_item_id=final_item_id,
                checkpoint_ns=checkpoint_ns,
            )
            for notice in runtime_notices:
                notice_seed = hashlib.sha256(notice.item_id.encode()).hexdigest()
                notice_execution_id = f"legacy-notice-execution:{notice_seed}"
                self.storage.executions.admit_internal_execution(
                    target_session_id,
                    thread_id=target_thread_id,
                    execution_id=notice_execution_id,
                    job_id=f"legacy-notice-job:{notice_seed}",
                    admission_idempotency_key=f"legacy-notice-admission:{notice_seed}",
                    runtime_notice_item=notice,
                    request_input={"kind": "runtime_notice", "payload": notice.payload},
                    checkpoint_ns=checkpoint_ns,
                )
                self.storage.executions.converge_execution(
                    target_session_id,
                    thread_id=target_thread_id,
                    turn_id=None,
                    execution_id=notice_execution_id,
                    outcome="unknown",
                    turn_status=None,
                    checkpoint_ns=checkpoint_ns,
                )
            target_items.extend((*terminal_members, *runtime_notices))
            migrated.append(
                {
                    "candidate_key": candidate.get("candidate_key"),
                    "source_turn_id": candidate.get("turn_id"),
                    "source_root_item_id": root.item_id,
                    "source_accepted_ingress_id": identity["accepted_ingress_id"],
                    "source_acceptance_idempotency_key": identity[
                        "acceptance_idempotency_key"
                    ],
                    "source_initial_execution_id": identity["initial_execution_id"],
                    "source_item_offsets": {
                        item.item_id: next(
                            (
                                record["jsonl_offset"]
                                for record in candidate_records
                                if isinstance(record, Mapping)
                                and record.get("message_id")
                                == next(
                                    (
                                        relation.target_id
                                        for relation in item.provenance_relations
                                        if relation.target_kind == "legacy_message"
                                    ),
                                    None,
                                )
                            ),
                            None,
                        )
                        for item in items
                        if isinstance(item, CanonicalItemRecord)
                    },
                    "target_turn_id": target_turn,
                    "root_item_id": target_root_item_id,
                    "target_accepted_ingress_id": target_accepted_ingress_id,
                    "target_acceptance_idempotency_key": target_acceptance_key,
                    "target_initial_execution_id": target_initial_execution_id,
                    "accepted": accepted,
                    "item_count": len(target_items),
                    "status": migrated_status,
                    "finalization_evidence": final_reason,
                    "identity_origin": "legacy_synthetic",
                    "legacy_seed_hash": source_seed,
                    "final_item_id": final_item_id,
                }
            )
        loss = [
            *report.get("loss", []),
            *(entry for candidate in candidates for entry in candidate.get("loss", [])),
        ]
        result = {
            **report,
            "candidates": [
                candidate_audit(candidate, report["raw_artifact_ref"])
                for candidate in candidates
            ],
            "source_thread_id": source_thread_id,
            "target_session_id": target_session_id,
            "target_thread_id": target_thread_id,
            "migration_id": migration_id,
            "status": "completed" if not rejected else "completed_with_rejections",
            "loss": loss,
            "lossless": not loss and not rejected,
            "migrated": migrated,
            "rejected": [
                candidate_audit(candidate, report["raw_artifact_ref"])
                for candidate in rejected
            ],
            "migration_quality": "partial" if loss or rejected else "lossless",
        }
        return result
