"""独立 staging 中的 v2 item、Turn 和投影安装。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.migration.semantics import (
    candidate_audit,
    terminal_evidence,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


class LegacyImportBuilder:
    def build_import(
        self,
        source_thread_id: str,
        *,
        target_thread_id: str,
        report: dict[str, object],
        migration_id: str,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        """将 v1 source 安装到全新的 v2 target，保留 source 只读原件。"""
        from app.services.infrastructure.rollout_context.migration.legacy_adapter import (
            LegacyRolloutAdapter,
        )

        self.initialize(target_thread_id, checkpoint_ns)
        adapter = LegacyRolloutAdapter(source_thread_id)
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
                    "target_session_id": target_thread_id,
                    "candidate_key": candidate["candidate_key"],
                    "legacy_seed_hash": source_seed,
                }
            ).rsplit(":", 1)[1]
            target_turn = f"legacy-migrated:{target_identity_seed}"
            target_root_item_id = f"item-legacy:{target_identity_seed}:root"
            # adapter identity 只描述 source legacy 坐标，不能直接占用 target
            # session 的 acceptance namespace。target-local identity 必须同时
            # 对 ingress、幂等 key 和 initial execution 做确定性重映射；source
            # 值仅通过 metadata/legacy report 保留审计 lineage。
            target_accepted_ingress_id = f"legacy-ingress:{target_identity_seed}"
            target_acceptance_key = f"legacy-migration:{target_identity_seed}"
            target_initial_execution_id = f"legacy-execution:{target_identity_seed}"
            accepted = self.accept_turn(
                target_thread_id,
                accepted_ingress_id=target_accepted_ingress_id,
                acceptance_idempotency_key=target_acceptance_key,
                payload=root.payload,
                payload_kind=root.payload_kind,
                turn_id=target_turn,
                root_item_id=target_root_item_id,
                initial_execution_id=target_initial_execution_id,
                acceptance_metadata={
                    **dict(root.metadata),
                    "legacy_artifact_ref": report["raw_artifact_ref"],
                    "legacy_source_session_id": source_thread_id,
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
                identity_origin="legacy_synthetic",
                checkpoint_ns=checkpoint_ns,
            )
            target_items = [accepted]
            target_item_by_source_id = {root.item_id: target_root_item_id}
            target_members: list[CanonicalItemRecord] = []
            with self._connect(
                target_thread_id, checkpoint_ns, read_only=True
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
                                        "target_session_id": target_thread_id,
                                        "source_session_id": source_thread_id,
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
                    metadata={
                        **dict(item.metadata),
                        "legacy_artifact_ref": report["raw_artifact_ref"],
                        "legacy_source_item_id": item.item_id,
                        "target_session_id": target_thread_id,
                        "projection_message_id": f"legacy:{target_identity_seed}:{hashlib.sha256(item.item_id.encode()).hexdigest()}",
                    },
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
            self.converge_execution(
                target_thread_id,
                turn_id=target_turn,
                execution_id=accepted["initial_execution_id"],
                outcome=migrated_status,
                turn_status=migrated_status,
                items=terminal_members,
                final_item_id=final_item_id,
                checkpoint_ns=checkpoint_ns,
            )
            for notice in runtime_notices:
                self.append_item(target_thread_id, notice, checkpoint_ns=checkpoint_ns)
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
                                == item.metadata.get("legacy_source_ref", {}).get(
                                    "message_id"
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
            "target_session_id": target_thread_id,
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
        with (
            self._lock(target_thread_id, checkpoint_ns),
            self._connect(target_thread_id, checkpoint_ns) as connection,
        ):
            now = _now()
            connection.execute(
                "INSERT INTO legacy_migration_reports(migration_id, source_session_id, target_session_id, source_format_version, target_format_version, status, report_json, created_at, completed_at) VALUES (?, ?, ?, 1, 2, ?, ?, ?, ?)",
                (
                    migration_id,
                    source_thread_id,
                    target_thread_id,
                    result["status"],
                    _json(result),
                    now,
                    now,
                ),
            )
            connection.commit()
        return result
