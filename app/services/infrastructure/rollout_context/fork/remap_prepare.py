"""full_rollout_copy 的 identity map 与 JSONL 重建准备阶段。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import content_hash as item_content_hash
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.fork.detail_mapping import (
    collect_detail_mappings,
    mapped_detail_ref,
)
from app.services.infrastructure.rollout_context.fork.full_copy.plans import (
    collect_plan_identities,
)
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line as _v2_json_line,
)


def prepare_full_copy_remap(
    service: object,
    connection: sqlite3.Connection,
    *,
    source_session_id: str,
    target_session_id: str,
    fork_id: str,
    checkpoint_ns: str,
    timestamp: str,
) -> FullCopyRemapState | None:
    """把 full copy 的 v2 副本变成真正 target-local 的 rollout。

    ``clone_rollout`` 先复制 source 文件，以便 fork journal 能在目标节点
    建立。提交事务内再一次性重写所有 v2 identity；这样 target 永远不会
    在正常提交后以 source 的裸 ID、JSONL offset 或 detail path 运行。源
    坐标只进入 ``fork_identity_mappings``，不参与 target lookup。
    """
    self = service
    source_session_id = required_text(source_session_id, field="source_session_id")
    target_session_id = required_text(target_session_id, field="target_session_id")
    fork_id = required_text(fork_id, field="fork_id")
    if source_session_id == target_session_id:
        raise ValueError("full_rollout_copy source/target session 不能相同")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("full_rollout_copy checkpoint_ns 必须是字符串")
    if not isinstance(timestamp, str) or not timestamp:
        raise RuntimeError("full_rollout_copy timestamp 必须是非空字符串")
    if self._rollout_format(connection) != storage_version.ROLLOUT_FORMAT_VERSION:
        return

    maps: dict[str, dict[str, str]] = {}
    # acceptance identity 已在 full-copy 的早期事务中按 target session
    # 重写；snapshot/commit metadata 仍可能保存 source key。把 mapping
    # 表作为唯一的 source->target 来源读回，避免用 target key 再猜 source
    # 或把跨会话 copy 当成同一 acceptance。
    for entity_type in ("accepted_ingress", "acceptance_idempotency_key"):
        for source_id, target_id in connection.execute(
            """
            SELECT source_local_id, target_local_id
            FROM fork_identity_mappings
            WHERE fork_id = ? AND entity_type = ?
            """,
            (fork_id, entity_type),
        ).fetchall():
            source_id = required_text(
                source_id,
                field=f"fork_identity_mappings.{entity_type}.source_local_id",
            )
            target_id = required_text(
                target_id,
                field=f"fork_identity_mappings.{entity_type}.target_local_id",
            )
            mapping = maps.setdefault(entity_type, {})
            previous = mapping.get(source_id)
            if previous is not None and previous != target_id:
                raise RuntimeError(
                    f"full_rollout_copy identity mapping 冲突: {entity_type}={source_id}"
                )
            mapping[source_id] = target_id

    def collect(entity_type: str, values: Iterable[object]) -> None:
        mapping = maps.setdefault(entity_type, {})
        for value in values:
            if value is None:
                continue
            source_id = required_text(
                value, field=f"full_rollout_copy.{entity_type}.source_id"
            )
            if source_id not in mapping:
                mapping[source_id] = self._full_copy_identity(
                    fork_id,
                    target_session_id,
                    entity_type,
                    source_id,
                )

    def rows(column: str, table: str) -> tuple[object, ...]:
        return tuple(
            row[0]
            for row in connection.execute(
                f"SELECT {column} FROM {table} ORDER BY {column}"
            ).fetchall()
        )

    item_rows = tuple(
        connection.execute(
            "SELECT item_sequence, item_id, turn_id, jsonl_offset, jsonl_length, metadata_json FROM item_catalog ORDER BY item_sequence"
        ).fetchall()
    )
    normalized_item_rows: list[tuple[object, ...]] = []
    seen_item_sequences: set[int] = set()
    seen_item_ids: set[str] = set()
    for row in item_rows:
        if len(row) != 6:
            raise RuntimeError("full_rollout_copy item catalog 字段数量不一致")
        sequence = non_negative_int(row[0], field="item_catalog.item_sequence")
        if sequence == 0 or sequence in seen_item_sequences:
            raise RuntimeError(
                f"full_rollout_copy item sequence 非法或重复: {sequence}"
            )
        item_id = required_text(row[1], field="item_catalog.item_id")
        if item_id in seen_item_ids:
            raise RuntimeError(f"full_rollout_copy item identity 重复: {item_id}")
        turn_id = optional_text(row[2], field="item_catalog.turn_id")
        offset = non_negative_int(row[3], field="item_catalog.jsonl_offset")
        length = non_negative_int(row[4], field="item_catalog.jsonl_length")
        if length == 0:
            raise RuntimeError(f"full_rollout_copy item JSONL length 为 0: {item_id}")
        if not isinstance(row[5], str) or not row[5]:
            raise RuntimeError(
                f"full_rollout_copy item metadata_json 非空字符串: {item_id}"
            )
        normalized_item_rows.append(
            (sequence, item_id, turn_id, offset, length, row[5])
        )
        seen_item_sequences.add(sequence)
        seen_item_ids.add(item_id)
    item_rows = tuple(normalized_item_rows)
    # full copy 的目标 rollout 是从 source 副本建立的独立根；overlay epoch
    # 也必须在目标坐标系重新编号，不能把 source 的全局进度当成 target 的
    # 本地历史。保留排序后的相对顺序，保证同一 source 的多 overlay 在
    # target 中仍可按 epoch 重放。
    source_overlay_epochs = sorted(
        {
            non_negative_int(row[0], field="source_overlays.source_overlay_epoch")
            for row in connection.execute(
                "SELECT source_overlay_epoch FROM source_overlays"
            ).fetchall()
        }
    )
    overlay_epoch_map = {
        source_epoch: target_epoch
        for target_epoch, source_epoch in enumerate(source_overlay_epochs)
    }
    collect("item", (row[1] for row in item_rows))
    collect("turn", rows("turn_id", "turn_records"))
    collect("turn", rows("turn_id", "turns"))
    collect("execution", rows("execution_id", "executions"))
    collect("model_call", rows("model_call_id", "model_calls"))
    collect("assembly", rows("assembly_id", "context_assemblies"))
    collect("view", rows("view_id", "context_views"))
    collect("branch", rows("branch_id", "branches"))
    collect("checkpoint", rows("checkpoint_id", "checkpoints"))
    collect("anchor", rows("anchor_id", "operation_anchors"))
    collect("relation", rows("relation_id", "item_relations"))
    collect("context_contribution", rows("contribution_id", "context_contributions"))
    collect("tool_set", rows("tool_set_snapshot_id", "tool_set_snapshots"))
    collect("source_overlay", rows("overlay_id", "source_overlays"))
    collect("assembly", rows("assembly_id", "context_plan_details"))
    maps["detail"] = collect_detail_mappings(
        connection,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
        assembly_map=maps["assembly"],
        allocate=lambda key: self._full_copy_identity(
            fork_id, target_session_id, "detail", key
        ),
    )
    collect("message", rows("message_id", "messages"))
    collect("tool_call", rows("tool_call_id", "tool_calls"))
    collect("control", rows("control_id", "control_events"))
    collect("plan", rows("plan_id", "context_assemblies"))
    collect_plan_identities(connection, collect)
    collect(
        "tool_set",
        (
            row[0]
            for row in connection.execute(
                "SELECT ref_id FROM context_assembly_selections WHERE ref_type = 'tool_set'"
            ).fetchall()
        ),
    )
    collect(
        "request_ref",
        (
            row[0]
            for row in connection.execute(
                "SELECT ref_id FROM assembly_item_refs WHERE ref_type = 'request_only'"
            ).fetchall()
        ),
    )
    collect(
        "request_ref",
        (
            value
            for row in connection.execute(
                "SELECT base_ref, delta_ref FROM source_overlays"
            ).fetchall()
            for value in row
            if value
        ),
    )
    for raw_row in connection.execute(
        "SELECT message_group_id FROM item_catalog WHERE message_group_id IS NOT NULL"
    ).fetchall():
        collect("group", (raw_row[0],))

    # overlay contribution 的 identity 不是普通随机 contribution：它是
    # overlay registry 的 role-backed manifest。若只给 contribution 分配独立
    # UUID，target overlay 已经 target-local 后，composition 无法再找到同一
    # base/delta binding。让这类 contribution 跟随 overlay 一起定位，普通
    # prompt/notice contribution 仍保留独立映射。
    for source_overlay_id, target_overlay_id in maps.get("source_overlay", {}).items():
        for role in ("base", "delta"):
            source_contribution_id = f"overlay:{source_overlay_id}:{role}"
            target_contribution_id = f"overlay:{target_overlay_id}:{role}"
            if source_contribution_id in maps.get("context_contribution", {}):
                maps["context_contribution"][source_contribution_id] = (
                    target_contribution_id
                )

    content_part_rows = tuple(
        connection.execute(
            "SELECT item_id, part_id FROM item_parts ORDER BY item_id, part_ordinal"
        ).fetchall()
    )
    part_maps: dict[tuple[str, str], str] = {}
    for old_item_id, old_part_id in content_part_rows:
        old_key = (
            required_text(old_item_id, field="item_parts.item_id"),
            required_text(old_part_id, field="item_parts.part_id"),
        )
        part_maps[old_key] = self._full_copy_identity(
            fork_id,
            target_session_id,
            "content_part",
            f"{old_key[0]}:{old_key[1]}",
        )

    def mapped(entity_type: str, value: object) -> str | None:
        if value is None:
            return None
        text = required_text(value, field=f"full_rollout_copy.{entity_type}.id")
        if entity_type == "detail":
            return detail_ref_key(
                mapped_detail_ref(
                    detail_ref_from_key(text),
                    source_session_id=source_session_id,
                    target_session_id=target_session_id,
                    detail_map=maps["detail"],
                )
            )
        return maps.get(entity_type, {}).get(text, text)

    def replace_text(value: object, *, include_acceptance: bool = False) -> object:
        if not isinstance(value, str):
            return value
        result = value
        all_maps = (
            maps.values()
            if include_acceptance
            else (
                mapping
                for entity_type, mapping in maps.items()
                if entity_type not in {"message", "control"}
            )
        )
        pairs = [
            (source_id, target_id)
            for mapping in all_maps
            for source_id, target_id in mapping.items()
            if source_id != target_id
        ]
        for source_id, target_id in sorted(pairs, key=lambda pair: -len(pair[0])):
            result = result.replace(source_id, target_id)
        return result

    def remap_json(
        value: object,
        *,
        parent_ref_type: str | None = None,
        preserve_source_coordinates: bool = False,
    ) -> object:
        if isinstance(value, Mapping):
            ref_type = value.get("ref_type")
            if ref_type in {"canonical_item", "request_only"} and "detail_ref" in value:
                raise RuntimeError(
                    "plan-order-integrity: raw ContextRef 不得携带最终 detail_ref"
                )
            resolved_ref_type = (
                required_text(ref_type, field="context.ref_type")
                if ref_type is not None
                else parent_ref_type
            )
            result: dict[str, object] = {}
            source_keys = {
                "source_session_id",
                "source_turn_id",
                "source_item_id",
                "source_execution_id",
                "source_model_call_id",
                "source_assembly_id",
                "source_view_id",
                "source_branch_id",
                "source_checkpoint_id",
                "source_overlay_id",
                "source_detail_ref",
                "source_accepted_ingress_id",
                "source_acceptance_idempotency_key",
                "legacy_source_item_id",
                "legacy_source_turn_id",
            }
            for key, child in value.items():
                key_text = str(key)
                if key_text in {"source", "tools", "tool_snapshot", "tool_policy", "body"}:
                    # 工具 schema/policy 与正文不是 ContextRef；其中恰好同名的
                    # JSON property 不能被当作 operational identity 改写。
                    result[key_text] = child
                    continue
                if key_text == "target":
                    result[key_text] = remap_json(
                        child,
                        parent_ref_type=resolved_ref_type,
                    )
                    continue
                if key_text in source_keys or key_text in {
                    "legacy_source_ref",
                    "fork_source_ref",
                }:
                    result[key_text] = child
                    continue
                if key_text == "session_id" and child == source_session_id:
                    child = target_session_id
                elif key_text == "plan_id":
                    child = mapped("plan", child)
                elif key_text == "ref_id":
                    if resolved_ref_type == "canonical_item":
                        child = mapped("item", child)
                    elif resolved_ref_type == "tool_set":
                        child = mapped("tool_set", child)
                    else:
                        child = mapped("request_ref", child)
                elif key_text in {"item_id", "root_input_item_id", "final_item_id"}:
                    child = mapped("item", child)
                elif key_text in {"turn_id", "replay_of_turn_id"}:
                    child = mapped("turn", child)
                elif key_text in {
                    "execution_id",
                    "resumed_from_execution_id",
                    "replay_of_execution_id",
                }:
                    child = mapped("execution", child)
                elif key_text in {"model_call_id", "retry_of_model_call_id"}:
                    child = mapped("model_call", child)
                elif key_text in {"assembly_id"}:
                    child = mapped("assembly", child)
                elif key_text in {
                    "view_id",
                    "active_view_id",
                    "parent_view_id",
                    "ancestor_view_id",
                    "source_view_id",
                }:
                    child = mapped("view", child)
                elif key_text in {
                    "branch_id",
                    "parent_branch_id",
                    "source_branch_id",
                }:
                    child = mapped("branch", child)
                elif key_text in {"checkpoint_id", "parent_checkpoint_id"}:
                    child = mapped("checkpoint", child)
                elif key_text == "accepted_ingress_id":
                    child = mapped("accepted_ingress", child)
                elif key_text == "acceptance_idempotency_key":
                    child = mapped("acceptance_idempotency_key", child)
                elif key_text in {"detail_ref", "target_detail_ref"} or (
                    key_text == "source_ref" and isinstance(child, Mapping)
                ):
                    result[key_text] = (
                        mapped_detail_ref(
                            DetailRef.from_dict(child),
                            source_session_id=source_session_id,
                            target_session_id=target_session_id,
                            detail_map=maps["detail"],
                        ).to_dict()
                        if child is not None
                        else None
                    )
                    continue
                elif key_text in {"contribution_id"}:
                    child = mapped("context_contribution", child)
                elif key_text in {
                    "overlay_id",
                    "supersedes_overlay_id",
                    "materializes_overlay_id",
                }:
                    child = mapped("source_overlay", child)
                elif key_text in {
                    "source_ref",
                    "overlay_ref",
                    "base_ref",
                    "delta_ref",
                    "target_base_ref",
                    "target_delta_ref",
                }:
                    child = mapped("request_ref", child)
                elif key_text in {"tool_call_id"}:
                    child = mapped("tool_call", child)
                elif (
                    key_text == "source_overlay_epoch"
                    and not preserve_source_coordinates
                    and child is not None
                ):
                    epoch = non_negative_int(
                        child, field="metadata.source_overlay_epoch"
                    )
                    child = overlay_epoch_map.get(epoch, epoch)
                result[key_text] = remap_json(
                    child,
                    parent_ref_type=resolved_ref_type,
                    preserve_source_coordinates=preserve_source_coordinates,
                )
            return result
        if isinstance(value, list):
            return [
                remap_json(
                    child,
                    parent_ref_type=parent_ref_type,
                    preserve_source_coordinates=preserve_source_coordinates,
                )
                for child in value
            ]
        if isinstance(value, tuple):
            return [
                remap_json(
                    child,
                    parent_ref_type=parent_ref_type,
                    preserve_source_coordinates=preserve_source_coordinates,
                )
                for child in value
            ]
        return value

    def remap_payload(value: object) -> object:
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, child in value.items():
                key_text = str(key)
                if key_text in {"tool_call_id", "call_id"}:
                    child = mapped("tool_call", child)
                elif key_text in {"tool_invocation_id", "tool_attempt_id"}:
                    child = mapped(key_text.removesuffix("_id"), child)
                elif key_text == "result_id":
                    child = mapped("tool_result", child)
                elif (
                    key_text == "id"
                    and isinstance(child, str)
                    and child in maps.get("tool_call", {})
                ):
                    child = mapped("tool_call", child)
                result[key_text] = remap_payload(child)
            return result
        if isinstance(value, list):
            return [remap_payload(child) for child in value]
        if isinstance(value, tuple):
            return [remap_payload(child) for child in value]
        return value

    jsonl_path = self.jsonl_path(target_session_id, checkpoint_ns)
    if not jsonl_path.is_file() or jsonl_path.is_symlink():
        raise RuntimeError(
            f"full_rollout_copy target JSONL 不是安全普通文件: {jsonl_path}"
        )
    old_jsonl = jsonl_path.read_bytes()

    def load_item_line(row: tuple[object, ...]) -> tuple[bytes, Mapping[str, object]]:
        sequence = non_negative_int(row[0], field="item_catalog.item_sequence")
        item_id = required_text(row[1], field="item_catalog.item_id")
        turn_id = optional_text(row[2], field="item_catalog.turn_id")
        offset = non_negative_int(row[3], field="item_catalog.jsonl_offset")
        length = non_negative_int(row[4], field="item_catalog.jsonl_length")
        if length == 0:
            raise RuntimeError(f"full_rollout_copy item JSONL length 为 0: {item_id}")
        metadata_text = required_text(
            row[5], field=f"item_catalog.metadata_json:{item_id}"
        )
        end = offset + length
        if end > len(old_jsonl):
            raise RuntimeError(
                "full_rollout_copy item JSONL locator 越界: "
                f"item_id={item_id}, offset={offset}, length={length}, file={len(old_jsonl)}"
            )
        raw = old_jsonl[offset:end]
        if not raw.endswith(b"\n"):
            raise RuntimeError(
                f"full_rollout_copy item JSONL record 缺少换行: {item_id}"
            )
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"full_rollout_copy 的 v2 item JSONL 无法解码: {item_id}"
            ) from error
        if not isinstance(envelope, Mapping):
            raise TypeError(f"full_rollout_copy 的 v2 item envelope 非法: {item_id}")
        if raw != _v2_json_line(envelope):
            raise RuntimeError(
                f"full_rollout_copy item JSONL 不是 canonical line: {item_id}"
            )
        envelope_sequence = non_negative_int(
            envelope.get("item_sequence"),
            field=f"item envelope.item_sequence:{item_id}",
        )
        envelope_item_id = required_text(
            envelope.get("item_id"), field=f"item envelope.item_id:{item_id}"
        )
        envelope_turn_id = optional_text(
            envelope.get("turn_id"), field=f"item envelope.turn_id:{item_id}"
        )
        if envelope_sequence != sequence or envelope_item_id != item_id:
            raise RuntimeError(
                f"full_rollout_copy catalog 与 JSONL item identity 不一致: {item_id}"
            )
        if envelope_turn_id != turn_id:
            raise RuntimeError(
                f"full_rollout_copy catalog 与 JSONL Turn identity 不一致: {item_id}"
            )
        try:
            catalog_metadata = json.loads(metadata_text)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"full_rollout_copy item catalog metadata_json 无法解析: {item_id}"
            ) from error
        if not isinstance(catalog_metadata, Mapping):
            raise TypeError(
                f"full_rollout_copy item catalog metadata_json 必须是 object: {item_id}"
            )
        if _v2_json_line(catalog_metadata) != (
            metadata_text.encode("utf-8") + b"\n"
        ) or catalog_metadata != envelope.get("metadata"):
            raise RuntimeError(
                f"full_rollout_copy item catalog metadata 与 JSONL 不一致: {item_id}"
            )
        return raw, envelope

    # v2 tool call/result identity 通常只存在 item payload，而不一定有
    # 独立 tool_calls 行。先从已校验的 source JSONL 收集这些 identity，
    # 使 full copy 的 payload remap 不会把 source tool_call_id 留在 target。
    for (
        sequence,
        old_item_id,
        _old_turn_id,
        old_offset,
        old_length,
        _metadata_json,
    ) in item_rows:
        _raw, envelope = load_item_line(
            (
                sequence,
                old_item_id,
                _old_turn_id,
                old_offset,
                old_length,
                _metadata_json,
            )
        )
        payload = envelope.get("payload")
        stack: list[object] = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, Mapping):
                for key, child in current.items():
                    key_text = str(key)
                    if (
                        key_text in {"tool_call_id", "call_id"}
                        and isinstance(child, str)
                        and child
                    ):
                        collect("tool_call", (child,))
                    elif key_text == "result_id" and isinstance(child, str) and child:
                        collect("tool_result", (child,))
                    elif (
                        key_text in {"tool_invocation_id", "tool_attempt_id"}
                        and isinstance(child, str)
                        and child
                    ):
                        collect(key_text.removesuffix("_id"), (child,))
                    elif isinstance(child, (Mapping, list, tuple)):
                        stack.append(child)
            elif isinstance(current, (list, tuple)):
                stack.extend(current)
    old_positions: dict[int, tuple[int, int]] = {}
    new_positions: dict[int, tuple[int, int]] = {}
    new_items: dict[int, CanonicalItemRecord] = {}
    new_lines: list[bytes] = []
    new_lines_by_sequence: dict[int, bytes] = {}
    for (
        sequence,
        old_item_id,
        old_turn_id,
        old_offset,
        old_length,
        _metadata_json,
    ) in item_rows:
        sequence_value = non_negative_int(sequence, field="item_catalog.item_sequence")
        old_position = (
            non_negative_int(old_offset, field="item_catalog.jsonl_offset"),
            non_negative_int(old_length, field="item_catalog.jsonl_length"),
        )
        old_positions[sequence_value] = old_position
        _raw, envelope = load_item_line(
            (sequence, old_item_id, old_turn_id, old_offset, old_length, _metadata_json)
        )
        item = CanonicalItemRecord.from_dict(envelope)
        remapped_metadata = remap_json(dict(item.metadata))
        if not isinstance(remapped_metadata, Mapping):
            raise TypeError(
                f"full_rollout_copy item metadata remap 非 object: {old_item_id}"
            )
        metadata = dict(remapped_metadata)
        for key, entity_type in (
            ("projection_message_id", "message"),
            ("message_group_id", "group"),
        ):
            if key in metadata:
                metadata[key] = mapped(entity_type, metadata[key])
        block_id = metadata.get("block_id")
        if isinstance(block_id, str):
            metadata["block_id"] = part_maps.get(
                (item.item_id, block_id),
                mapped("content_part", block_id),
            )
        metadata["fork_target_session_id"] = target_session_id
        producer = dict(item.producer_ref)
        producer_kind = required_text(
            producer.get("producer_kind"), field="item.producer_ref.producer_kind"
        )
        if producer_kind == "user":
            producer["producer_id"] = mapped("message", producer.get("producer_id"))
        elif producer_kind == "provider":
            producer["producer_id"] = mapped("model_call", producer.get("producer_id"))
        elif producer_kind == "tool":
            producer["producer_id"] = mapped("tool_call", producer.get("producer_id"))
        if producer.get("invocation_id") in maps.get("execution", {}):
            producer["invocation_id"] = mapped("execution", producer["invocation_id"])
        target_item_id = mapped("item", item.item_id) or item.item_id
        remapped_payload = remap_payload(item.payload)
        # source revision 的默认值包含 item identity。full copy 重写
        # target-local item id 或 tool identity 后，不能继续让 reader
        # 根据旧 source id/hash 猜 revision；显式写入新的 target manifest。
        revision = metadata.get("source_revision")
        if (
            revision is None
            or revision == ""
            or revision == f"canonical:{item.item_id}:{item.content_hash}"
        ):
            metadata["source_revision"] = (
                f"canonical:{target_item_id}:"
                f"{item_content_hash(item.payload_kind, remapped_payload)}"
            )
        new_item = CanonicalItemRecord.create(
            item_sequence=sequence_value,
            item_id=target_item_id,
            semantic_kind=item.semantic_kind,
            payload_kind=item.payload_kind,
            status=item.status,
            producer_ref=producer,
            payload=remapped_payload,
            created_at=item.created_at,
            metadata=metadata,
            turn_id=mapped("turn", item.turn_id),
            turn_scope=item.turn_scope,
            message_group_id=mapped("group", item.message_group_id),
            wire_role=item.wire_role,
        )
        new_items[sequence_value] = new_item
        # full_rollout_copy 重建的是新的 canonical item line；LangChain
        # message 仍由 target 的 projection reader 从 item 生成，不能把
        # source 的兼容 message 再复制成第二份事实。
        encoded = _v2_json_line(new_item.to_dict())
        new_positions[sequence_value] = (
            sum(len(line) for line in new_lines),
            len(encoded),
        )
        new_lines.append(encoded)
        new_lines_by_sequence[sequence_value] = encoded

    return FullCopyRemapState(
        service=service,
        connection=connection,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
        fork_id=fork_id,
        checkpoint_ns=checkpoint_ns,
        timestamp=timestamp,
        maps=maps,
        overlay_epoch_map=overlay_epoch_map,
        item_rows=item_rows,
        content_part_rows=content_part_rows,
        part_maps=part_maps,
        old_jsonl=old_jsonl,
        old_positions=old_positions,
        new_positions=new_positions,
        new_items=new_items,
        new_lines=new_lines,
        new_lines_by_sequence=new_lines_by_sequence,
        mapped=mapped,
        remap_json=remap_json,
        remap_payload=remap_payload,
    )
