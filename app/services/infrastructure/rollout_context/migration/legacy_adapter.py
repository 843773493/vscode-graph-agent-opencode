"""一次性 legacy v1 import 的 envelope 校验与候选适配器。

该模块只能由显式 legacy_import_v1_to_v2 staging/import 流程调用。正常
runtime、history、provider 和 checkpoint 路径不得导入它。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.hashing import _ensure_json_value, sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.migration.semantics import (
    final_marker,
    legacy_hashes,
    payload_supported,
    record_loss,
    tool_identity_conflict,
)


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value


def validate_envelope(value: Mapping[str, object], expected_format: int = 2) -> None:
    if (
        type(expected_format) is not int
        or type(value.get("format_version")) is not int
        or value.get("format_version") != expected_format
    ):
        raise FormatDispatchError(
            f"JSONL format_version={value.get('format_version')!r}, expected={expected_format}"
        )
    if expected_format == 2:
        CanonicalItemRecord.from_dict(value)
    elif expected_format == 1:
        if value.get("record_type") != "message":
            raise FormatDispatchError("v1 envelope record_type 必须是 message")
        required = (
            "message_sequence",
            "message_id",
            "turn_id",
            "role",
            "message",
            "metadata",
        )
        missing = [name for name in required if name not in value]
        if missing:
            raise FormatDispatchError(
                "v1 message envelope 缺少字段: " + ",".join(missing)
            )
        sequence = value["message_sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise FormatDispatchError("v1 message_sequence 必须是正整数")
        _non_empty_string(value["message_id"], "v1 message_id")
        _non_empty_string(value["role"], "v1 role")
        if value["turn_id"] is not None:
            _non_empty_string(value["turn_id"], "v1 turn_id")
        if not isinstance(value["message"], Mapping):
            raise FormatDispatchError("v1 message 必须是 object")
        if not isinstance(value["metadata"], Mapping):
            raise FormatDispatchError("v1 metadata 必须是 object")
        _ensure_json_value(value["message"], "v1 message")
        _ensure_json_value(value["metadata"], "v1 metadata")
    else:
        raise FormatDispatchError(
            f"不支持的 envelope format_version: {expected_format}"
        )


class LegacyRolloutAdapter:
    """按确定性 user-root window 读取 v1，不修改 source artifact。"""

    def __init__(self, source_session_id: str) -> None:
        _non_empty_string(source_session_id, "source_session_id")
        self.source_session_id = source_session_id

    @staticmethod
    def _role(record: Mapping[str, object]) -> str:
        role = record.get("role")
        return role if isinstance(role, str) and role else "unknown"

    @staticmethod
    def _turn_id(record: Mapping[str, object]) -> str | None:
        value = record.get("turn_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _trusted_system_reminder(record: Mapping[str, object]) -> bool:
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            return False
        return metadata.get("internal") is True and (
            metadata.get("checkpoint") is True
            or metadata.get("source") in {"interrupt", "runtime", "system_reminder"}
        )

    @staticmethod
    def _explicit_final_marker(record: Mapping[str, object]) -> bool:
        """只接受 legacy 自身写入的终态 marker，不把最后一条消息当 final。"""
        return final_marker(record)

    def group_candidates(
        self, records: Sequence[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        """按 user root window 产生确定性 candidate。

        ``message_sequence`` 只用于排序和识别窗口边界；Turn 身份来自每个
        window 的既有 turn_id 或 user root hash，绝不从最后一条 assistant、
        wire role 或任意物理邻接推断。
        """
        raw_records = list(records)
        invalid_records = [
            record
            for record in raw_records
            if not isinstance(record, Mapping)
            or not isinstance(record.get("message_sequence"), int)
            or isinstance(record.get("message_sequence"), bool)
            or record["message_sequence"] <= 0
            or not isinstance(record.get("message_id"), str)
            or not record.get("message_id")
        ]
        if invalid_records:
            return [
                {
                    "candidate_key": "legacy-rollout:invalid-coordinate-or-identity",
                    "candidate_status": "legacy_identity_conflict",
                    "turn_id": None,
                    "records": raw_records,
                }
            ]
        ordered = sorted(raw_records, key=lambda record: record["message_sequence"])
        if len({record["message_sequence"] for record in ordered}) != len(ordered):
            return [
                {
                    "candidate_key": "legacy-rollout:duplicate-coordinate",
                    "candidate_status": "legacy_identity_conflict",
                    "turn_id": None,
                    "records": list(ordered),
                }
            ]
        message_ids = [
            record.get("message_id")
            for record in ordered
            if isinstance(record.get("message_id"), str) and record.get("message_id")
        ]
        if len(set(message_ids)) != len(message_ids):
            return [
                {
                    "candidate_key": "legacy-rollout:duplicate-message-identity",
                    "candidate_status": "legacy_identity_conflict",
                    "turn_id": None,
                    "records": list(ordered),
                }
            ]

        def seed_for(user: Mapping[str, object]) -> str:
            return legacy_hashes(self.source_session_id, user)[1]

        user_indexes = [
            index
            for index, record in enumerate(ordered)
            if self._role(record) == "user"
        ]
        if not user_indexes:
            orphan: list[Mapping[str, object]] = []
            unsupported: list[Mapping[str, object]] = []
            for record in ordered:
                role = self._role(record)
                if (
                    role == "system_reminder"
                    and not self._trusted_system_reminder(record)
                ) or role not in {"system", "developer"}:
                    unsupported.append(record)
                else:
                    orphan.append(record)
            candidates: list[dict[str, object]] = []
            if orphan:
                candidates.append(
                    {
                        "candidate_key": "legacy-orphan:" + sha256_jcs(orphan),
                        "candidate_status": "legacy_orphan",
                        "turn_id": None,
                        "records": list(orphan),
                    }
                )
            if unsupported:
                candidates.append(
                    {
                        "candidate_key": "legacy-unsupported-role:"
                        + sha256_jcs(unsupported),
                        "candidate_status": "legacy_unsupported_role",
                        "turn_id": None,
                        "records": list(unsupported),
                    }
                )
            return candidates

        # 一个 user 行开启一个确定的 root window，窗口截止于下一条 user
        # 行之前。物理序号只定义这个显式窗口边界；Turn identity 仍由
        # 窗口内的既有 turn_id 或 root seed 决定，绝不从最后 assistant 猜测。
        windows: list[tuple[Mapping[str, object], list[Mapping[str, object]]]] = []
        prefix = ordered[: user_indexes[0]]
        for position, start in enumerate(user_indexes):
            end = (
                user_indexes[position + 1]
                if position + 1 < len(user_indexes)
                else len(ordered)
            )
            window = ordered[start:end]
            windows.append((window[0], list(window)))

        candidates = []
        prefix_orphan = list(prefix)
        if prefix_orphan:
            candidates.append(
                {
                    "candidate_key": "legacy-orphan:" + sha256_jcs(prefix_orphan),
                    "candidate_status": "legacy_orphan",
                    "turn_id": None,
                    "records": prefix_orphan,
                }
            )

        for root, window in windows:
            role_ids = {
                turn_id
                for record in window
                if (turn_id := self._turn_id(record)) is not None
            }
            seed = seed_for(root)
            if len(role_ids) == 0:
                candidate_status = "legacy_missing_turn_id"
                candidate_turn_id = None
                candidate_key = f"legacy-missing-turn:{legacy_hashes(self.source_session_id, root)[0]}"
            elif len(role_ids) == 1:
                candidate_status = "accepted"
                candidate_turn_id = next(iter(role_ids))
                candidate_key = f"legacy-turn:{candidate_turn_id}"
            else:
                candidate_status = "legacy_turn_group_ambiguous"
                candidate_turn_id = None
                candidate_key = "legacy-ambiguous-window:" + seed

            request_context: list[Mapping[str, object]] = []
            unsupported = any(not payload_supported(record) for record in window)
            for record in window[1:]:
                role = self._role(record)
                record_turn_id = self._turn_id(record)
                if record_turn_id is not None and len(role_ids) == 1:
                    expected_turn_id = next(iter(role_ids))
                    if record_turn_id != expected_turn_id:
                        candidate_status = "legacy_turn_group_ambiguous"
                        candidate_turn_id = None
                if role in {"assistant", "tool", "function"}:
                    continue
                if role in {"system", "developer"}:
                    request_context.append(record)
                    continue
                if role == "system_reminder" and self._trusted_system_reminder(record):
                    continue
                unsupported = True

            if unsupported:
                candidate_status = "legacy_unsupported_role"
                candidate_turn_id = None
            elif tool_identity_conflict(window):
                candidate_status = "legacy_identity_conflict"
                candidate_turn_id = None
            candidates.append(
                {
                    "candidate_key": candidate_key,
                    "candidate_status": candidate_status,
                    "turn_id": candidate_turn_id,
                    "legacy_seed_hash": seed,
                    "records": window,
                    "request_context": sorted(
                        request_context,
                        key=lambda value: value["message_sequence"],
                    ),
                    "window_start_sequence": root["message_sequence"],
                    "window_end_sequence": window[-1]["message_sequence"],
                    "source_turn_ids": sorted(role_ids),
                    "loss": [
                        {
                            "message_id": record["message_id"],
                            "fields": fields,
                            "reason": "legacy_fields_preserved_raw",
                        }
                        for record in window
                        if payload_supported(record) and (fields := record_loss(record))
                    ],
                }
            )

        # 同一 legacy turn_id 跨越多个 user-root window 不是两个 Turn；整个
        # ID 组拒绝，且不把 source payload 相同的窗口合并或任选其一。
        candidates_by_turn: dict[str, list[dict[str, object]]] = {}
        for candidate in candidates:
            for turn_id in candidate.get("source_turn_ids", []):
                candidates_by_turn.setdefault(turn_id, []).append(candidate)
        for turn_id, grouped in candidates_by_turn.items():
            if len(grouped) < 2:
                continue
            for candidate in grouped:
                candidate["candidate_status"] = "legacy_multiple_user_messages"
                candidate["turn_id"] = None
                candidate["candidate_key"] = (
                    f"legacy-multiple-user:{turn_id}:{candidate['legacy_seed_hash']}"
                )

        return candidates

    def map_candidate(
        self,
        candidate: Mapping[str, object],
        *,
        item_sequence_start: int = 1,
    ) -> dict[str, object]:
        """把已接受 candidate 转成可安装的 v2 identity/item 计划。"""
        if (
            not isinstance(item_sequence_start, int)
            or isinstance(item_sequence_start, bool)
            or item_sequence_start <= 0
        ):
            raise ItemSchemaError("legacy item_sequence_start 必须是正整数")
        status = candidate.get("candidate_status")
        if status not in {"accepted", "legacy_missing_turn_id"}:
            raise ItemSchemaError(f"legacy candidate 不可迁移: {status}")
        records = candidate.get("records")
        if not isinstance(records, list) or not records:
            raise ItemSchemaError("legacy candidate 缺少 records")
        user = records[0]
        if not isinstance(user, Mapping) or self._role(user) != "user":
            raise ItemSchemaError("legacy candidate root 必须是唯一 user message")
        for record in records:
            if not isinstance(record, Mapping):
                raise ItemSchemaError("legacy candidate records 必须是 object")
            validate_envelope(record, expected_format=1)
        legacy_message_hash, seed = legacy_hashes(self.source_session_id, user)
        if candidate.get("legacy_seed_hash", seed) != seed:
            raise ItemSchemaError("source-mismatch: legacy candidate seed/hash 冲突")
        legacy_turn_id = candidate.get("turn_id")
        if legacy_turn_id is not None:
            legacy_turn_id = _non_empty_string(
                legacy_turn_id, "legacy candidate turn_id"
            )
        target_turn_id = (
            f"legacy-turn:{legacy_turn_id}"
            if isinstance(legacy_turn_id, str) and legacy_turn_id
            else f"legacy-missing-turn:{seed}"
        )
        identity = {
            "accepted_ingress_id": f"legacy-ingress:{seed}",
            "acceptance_idempotency_key": f"legacy-migration:{seed}",
            "initial_execution_id": f"legacy-execution:{seed}",
            "legacy_seed_hash": seed,
            "legacy_message_hash": legacy_message_hash,
            "identity_origin": "legacy_synthetic",
        }
        items: list[CanonicalItemRecord] = []
        tool_names = {
            call["id"]: call["name"]
            for record in records
            for call in record["message"]["data"].get("tool_calls", [])
        }
        sequence = item_sequence_start
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise ItemSchemaError("legacy record 必须是 object")
            role = self._role(record)
            message = record.get("message")
            data = message.get("data") if isinstance(message, Mapping) else None
            payload = data.get("content") if isinstance(data, Mapping) else message
            message_id = _non_empty_string(record.get("message_id"), "v1 message_id")
            source_ref = {
                "session_id": self.source_session_id,
                "message_sequence": record.get("message_sequence"),
                "message_id": message_id,
            }
            item_status = CanonicalItemStatus.COMPLETED
            legacy_tool_outcome: str | None = None
            if index == 0:
                semantic = SemanticKind.USER_INPUT
                payload_type = (
                    PayloadKind.TEXT
                    if isinstance(payload, str)
                    else PayloadKind.STRUCTURED_CONTENT
                )
                item_turn_id = target_turn_id
                scope = TurnScope.TURN_ROOT
            elif role in {"system", "developer"}:
                # request context 保留在 migration plan/report，不进入 Turn item。
                continue
            elif role == "system_reminder" and self._trusted_system_reminder(record):
                semantic = SemanticKind.RUNTIME_NOTICE
                payload_type = (
                    PayloadKind.TEXT
                    if isinstance(payload, str)
                    else PayloadKind.STRUCTURED_CONTENT
                )
                item_turn_id = None
                scope = TurnScope.PENDING_NEXT_TURN
            elif role == "assistant":
                semantic = SemanticKind.ASSISTANT_OUTPUT
                payload_type = (
                    PayloadKind.TEXT
                    if isinstance(payload, str)
                    else PayloadKind.STRUCTURED_CONTENT
                )
                item_turn_id = target_turn_id
                scope = TurnScope.TURN_MEMBER
            elif role in {"tool", "function"}:
                semantic = SemanticKind.TOOL_RESULT
                payload_type = PayloadKind.TOOL_RESULT
                raw_tool_call_id = (
                    data.get("tool_call_id") if isinstance(data, Mapping) else None
                )
                tool_call_id = _non_empty_string(raw_tool_call_id, "v1 tool_call_id")
                raw_tool_name = data.get("name") if isinstance(data, Mapping) else None
                tool_name = (
                    _non_empty_string(raw_tool_name, "v1 tool name")
                    if raw_tool_name is not None
                    else tool_names[tool_call_id]
                )
                payload = {
                    "tool_call_id": tool_call_id,
                    "tool_invocation_id": f"legacy-invocation:{tool_call_id}",
                    "tool_attempt_id": f"legacy-attempt:{tool_call_id}",
                    "result_id": message_id,
                    "name": tool_name,
                    "content": payload,
                }
                raw_outcome = (
                    data.get("tool_outcome", data.get("outcome", data.get("status")))
                    if isinstance(data, Mapping)
                    else None
                )
                if raw_outcome is None:
                    raw_outcome = record.get(
                        "tool_outcome", record.get("outcome", record.get("status"))
                    )
                outcome_text = (
                    _non_empty_string(raw_outcome, "v1 tool outcome").lower()
                    if raw_outcome is not None
                    else "unknown"
                )
                if outcome_text in {"success", "succeeded", "ok", "completed"}:
                    legacy_tool_outcome = "success"
                    item_status = CanonicalItemStatus.COMPLETED
                elif outcome_text in {"failure", "failed", "error"}:
                    legacy_tool_outcome = "failure"
                    item_status = CanonicalItemStatus.FAILED
                elif outcome_text in {"cancelled", "canceled", "interrupted"}:
                    legacy_tool_outcome = "cancelled"
                    item_status = CanonicalItemStatus.CANCELLED
                else:
                    # v1 没有可靠结果终态时必须保留 uncertainty，不能把
                    # “读取成功”伪装成 tool execution success。
                    legacy_tool_outcome = "unknown"
                    item_status = CanonicalItemStatus.UNKNOWN
                # 非 completed item 只能带 unknown marker；legacy 的已知失败
                # 或取消仍保留在 item status 与 legacy metadata 中，不能被
                # 伪装成可成功 replay 的 completed tool result。
                payload["tool_outcome"] = (
                    legacy_tool_outcome
                    if item_status == CanonicalItemStatus.COMPLETED
                    else "unknown"
                )
                item_turn_id = target_turn_id
                scope = TurnScope.TURN_MEMBER
            else:
                raise ItemSchemaError(f"legacy record role 不可迁移: {role}")
            item_metadata: dict[str, object] = {
                "legacy_source_ref": source_ref,
                "legacy_turn_id": record.get("turn_id"),
                "legacy_source_item_id": f"legacy-item:{seed}:{message_id}",
                "legacy_message_hash": legacy_message_hash,
                "legacy_seed_hash": seed,
                "legacy_provenance": "legacy/unknown_source",
                "legacy_raw_ref": {
                    "message_id": message_id,
                    "jsonl_offset": record.get("jsonl_offset"),
                    "jsonl_length": record.get("jsonl_length"),
                    "payload_hash": record.get("payload_hash", sha256_jcs(message)),
                },
            }
            if legacy_tool_outcome is not None:
                item_metadata["legacy_tool_outcome"] = legacy_tool_outcome
                item_metadata["legacy_outcome_source"] = "legacy_record_or_unknown"
                if legacy_tool_outcome == "success":
                    item_metadata["execution_confirmed"] = True
            producer_kind = (
                "user"
                if index == 0
                else "provider"
                if role == "assistant"
                else "tool"
                if role in {"tool", "function"}
                else "system"
                if role == "system_reminder"
                else "runtime"
            )
            items.append(
                CanonicalItemRecord.create(
                    item_sequence=sequence,
                    item_id=f"legacy-item:{seed}:{message_id}",
                    semantic_kind=semantic,
                    payload_kind=payload_type,
                    status=item_status,
                    producer_ref={
                        # v2 producer_kind 是闭合集合；legacy 来源通过
                        # legacy_provenance/source_ref metadata 保留，不能把
                        # 未注册的 legacy 当成 v2 producer kind 写入。
                        "producer_kind": producer_kind,
                        "producer_id": message_id,
                        "source_hash": sha256_jcs(source_ref),
                    },
                    payload=payload,
                    metadata=item_metadata,
                    turn_id=item_turn_id,
                    turn_scope=scope,
                    message_group_id=f"legacy-message:{message_id}",
                    # v1 function 是旧 tool result 的等价 role；v2
                    # projection 只接受规范化的 tool wire role。
                    wire_role="tool" if role == "function" else role,
                )
            )
            sequence += 1
            if role == "assistant":
                for call in data.get("tool_calls", []):
                    items.append(
                        CanonicalItemRecord.create(
                            item_sequence=sequence,
                            item_id=f"legacy-item:{seed}:{message_id}:call:{call['id']}",
                            semantic_kind=SemanticKind.TOOL_CALL,
                            payload_kind=PayloadKind.TOOL_CALL,
                            status=CanonicalItemStatus.COMPLETED,
                            producer_ref={
                                "producer_kind": "provider",
                                "producer_id": message_id,
                                "source_hash": sha256_jcs(source_ref),
                            },
                            payload={
                                "tool_call_id": call["id"],
                                "tool_invocation_id": f"legacy-invocation:{call['id']}",
                                "tool_attempt_id": f"legacy-attempt:{call['id']}",
                                "name": call["name"],
                                "args": call["args"],
                            },
                            metadata={
                                **item_metadata,
                                "legacy_source_item_id": f"legacy-item:{seed}:{message_id}:call:{call['id']}",
                                "legacy_source_ref": {
                                    **source_ref,
                                    "part_id": f"tool_call:{call['id']}",
                                },
                            },
                            turn_id=target_turn_id,
                            turn_scope=TurnScope.TURN_MEMBER,
                            message_group_id=f"legacy-message:{message_id}",
                            wire_role="assistant",
                        )
                    )
                    sequence += 1
        candidate_context = candidate.get("request_context")
        request_context_by_message_id: dict[str, Mapping[str, object]] = {}
        for record in candidate_context if isinstance(candidate_context, list) else []:
            if not isinstance(record, Mapping):
                raise ItemSchemaError("legacy request_context record 必须是 object")
            validate_envelope(record, expected_format=1)
            context_message_id = _non_empty_string(
                record.get("message_id"), "v1 request_context.message_id"
            )
            request_context_by_message_id[context_message_id] = record
        for record in records[1:]:
            if isinstance(record, Mapping) and self._role(record) in {
                "system",
                "developer",
            }:
                # ``records`` 中保留原始 window 便于 quarantine/report；这里按
                # message_id 去重，避免同一 request context 同时从 candidate
                # metadata 与 window 归属两次注入。
                context_message_id = _non_empty_string(
                    record.get("message_id"), "v1 request_context.message_id"
                )
                request_context_by_message_id[context_message_id] = record
        request_context = sorted(
            request_context_by_message_id.values(),
            key=lambda value: value["message_sequence"],
        )
        return {
            "candidate_key": candidate.get("candidate_key"),
            "turn_id": target_turn_id,
            "identity": identity,
            "items": items,
            "request_context": request_context,
        }


__all__ = ["LegacyRolloutAdapter", "validate_envelope"]
