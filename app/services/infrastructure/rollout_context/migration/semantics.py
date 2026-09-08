"""v1 source identity、终态证据和无法无损转换字段的审计规则。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord


def legacy_hashes(session_id: str, record: Mapping[str, object]) -> tuple[str, str]:
    coordinate = {
        "source_session_id": session_id,
        "message_sequence": record["message_sequence"],
        "message_id": record["message_id"],
    }
    message_hash = sha256_jcs(
        {**coordinate, "role": record["role"], "message": record["message"]}
    )
    return message_hash, sha256_jcs({**coordinate, "legacy_message_hash": message_hash})


def final_marker(record: Mapping[str, object]) -> bool:
    metadata = record.get("metadata")
    return isinstance(metadata, Mapping) and (
        any(
            metadata.get(name) is True
            for name in ("final", "is_final", "final_response")
        )
        or metadata.get("phase") == "final_answer"
        or record.get("legacy_manifest_final") is True
    )


def terminal_evidence(
    records: Sequence[Mapping[str, object]], items: Sequence[CanonicalItemRecord]
) -> tuple[str, str | None, str]:
    markers = [record for record in records if final_marker(record)]
    outcomes: set[str] = set()
    for record in records:
        metadata = record.get("metadata")
        if isinstance(metadata, Mapping):
            outcome = metadata.get(
                "turn_status", metadata.get("outcome", metadata.get("status"))
            )
            if outcome in {"failed", "interrupted", "cancelled"}:
                outcomes.add(outcome)
    if len(outcomes) == 1 and not markers:
        return next(iter(outcomes)), None, "explicit_legacy_control_marker"
    if outcomes or len(markers) > 1:
        return "unknown", None, "ambiguous_legacy_finalization"
    if len(markers) != 1:
        return "unknown", None, "legacy_finalization_unavailable"
    marker = markers[0]
    selected = [
        item
        for item in items
        if item.semantic_kind == "assistant_output"
        and item.status == "completed"
        and item.metadata["legacy_source_ref"]["message_id"] == marker["message_id"]
    ]
    metadata = marker["metadata"]
    if (
        marker["role"] != "assistant"
        or len(selected) != 1
        or metadata.get("status", "completed") != "completed"
        or any(
            marker["message"]["data"].get(field)
            not in (None, "completed", "success", "succeeded")
            for field in ("status", "outcome")
        )
        or marker["message"]["data"].get("tool_calls")
    ):
        return "unknown", None, "legacy_final_marker_not_completed_assistant"
    return "completed", selected[0].item_id, "explicit_legacy_final_marker"


def payload_supported(record: Mapping[str, object]) -> bool:
    message = record.get("message")
    data = message.get("data") if isinstance(message, Mapping) else None
    if not isinstance(data, Mapping) or not isinstance(
        data.get("content"), (str, list)
    ):
        return False
    if (
        record.get("contribution_kind") == "tool_set"
        or record.get("metadata", {}).get("contribution_kind") == "tool_set"
    ):
        return False
    content = data["content"]
    if isinstance(content, list) and any(
        not isinstance(block, str)
        and not (
            isinstance(block, Mapping)
            and block.get("type") in {"text", "input_text", "output_text"}
            and isinstance(block.get("text"), str)
        )
        for block in content
    ):
        # reasoning/attachment/provider 扩展尚无可靠映射时整体隔离，避免受保护值进入普通正文。
        return False
    calls = data.get("tool_calls", [])
    if not isinstance(calls, list) or any(
        not isinstance(call, Mapping)
        or not isinstance(call.get("id"), str)
        or not call["id"]
        or not isinstance(call.get("name"), str)
        or not call["name"]
        or "args" not in call
        for call in calls
    ):
        return False
    role = record.get("role")
    if calls and role != "assistant":
        return False
    expected = {
        "user": {"human"},
        "assistant": {"ai"},
        "tool": {"tool"},
        "function": {"function"},
        "system": {"system"},
        "developer": {"system", "chat"},
        "system_reminder": {"human", "system"},
    }
    if not isinstance(message.get("type"), str) or message["type"] not in expected.get(
        role, set()
    ):
        return False
    if data.get("type", message["type"]) != message["type"]:
        return False
    if data.get("id") is not None and data["id"] != record["message_id"]:
        return False
    # 没有显式 call identity 不能靠 name/邻接伪装成 tool result。
    return role not in {"tool", "function"} or (
        isinstance(data.get("tool_call_id"), str) and bool(data["tool_call_id"])
    )


def record_loss(record: Mapping[str, object]) -> list[str]:
    """未消费字段始终保留完整 raw reference，同时显式标记 projection loss。"""
    message = record["message"]
    data = message["data"]
    known = {
        "content",
        "type",
        "name",
        "id",
        "additional_kwargs",
        "response_metadata",
        "tool_call_id",
        "tool_calls",
        "tool_outcome",
        "status",
        "outcome",
    }
    loss = [
        f"message.data.{key}"
        for key in data
        if key not in known and data[key] not in (None, [], {})
    ]
    for key in ("additional_kwargs", "response_metadata"):
        if data.get(key):
            loss.append(f"message.data.{key}")
    consumed_metadata = {
        "final",
        "is_final",
        "final_response",
        "phase",
        "status",
        "outcome",
        "turn_status",
    }
    if record["role"] == "system_reminder":
        consumed_metadata.update({"internal", "checkpoint", "source"})
    loss.extend(
        f"metadata.{key}" for key in record["metadata"] if key not in consumed_metadata
    )
    if record["role"] not in {"tool", "function"}:
        loss.extend(
            f"message.data.{key}"
            for key in ("name", "tool_call_id", "tool_outcome", "status", "outcome")
            if data.get(key) is not None
        )
    known_envelope = {
        "format_version",
        "record_type",
        "message_sequence",
        "message_id",
        "turn_id",
        "role",
        "message",
        "metadata",
        "payload_hash",
        "raw_line_sha256",
        "jsonl_offset",
        "jsonl_length",
        "legacy_manifest_final",
    }
    loss.extend(f"envelope.{key}" for key in record if key not in known_envelope)
    loss.extend(f"message.{key}" for key in message if key not in {"type", "data"})
    for index, call in enumerate(data.get("tool_calls", [])):
        loss.extend(
            f"tool_calls[{index}].{key}"
            for key in call
            if key not in {"id", "name", "args", "type"}
        )
    return loss


def tool_identity_conflict(records: Sequence[Mapping[str, object]]) -> bool:
    calls: set[str] = set()
    results: set[str] = set()
    for record in records:
        data = record["message"]["data"]
        for call in data.get("tool_calls", []):
            if call["id"] in calls:
                return True
            calls.add(call["id"])
        if record["role"] in {"tool", "function"}:
            call_id = data["tool_call_id"]
            if call_id not in calls or call_id in results:
                return True
            results.add(call_id)
    return False


def candidate_audit(candidate: Mapping[str, object], raw_ref: str) -> dict[str, object]:
    """普通 v2 report 只存坐标；原始 prompt/provider 正文留在私有快照。"""
    status = candidate["candidate_status"]
    records = []
    for record in candidate["records"]:
        disposition = (
            status
            if status not in {"accepted", "legacy_missing_turn_id"}
            else "legacy_request_context"
            if record["role"] in {"system", "developer"}
            else "runtime_notice"
            if record["role"] == "system_reminder"
            else "canonical_item"
        )
        records.append(
            {
                **{
                    key: record.get(key)
                    for key in (
                        "message_sequence",
                        "message_id",
                        "turn_id",
                        "role",
                        "jsonl_offset",
                        "jsonl_length",
                        "payload_hash",
                        "raw_line_sha256",
                    )
                },
                "disposition": disposition,
                "raw_ref": raw_ref + "/rollout.jsonl",
                "protection": "protected",
            }
        )
    return {
        "candidate_key": candidate["candidate_key"],
        "candidate_status": status,
        "turn_id": candidate.get("turn_id"),
        "records": records,
    }
