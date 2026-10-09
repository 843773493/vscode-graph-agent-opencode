"""full_rollout_copy 的 identity map 与 JSONL 重建准备阶段。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.activation.schema import (
    require_resource_activation_schema,
)
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
from app.services.infrastructure.rollout_context.fork.full_copy.source_catalog import (
    read_full_copy_item_rows,
    read_full_copy_overlay_epochs,
)
from app.services.infrastructure.rollout_context.fork.identity import (
    remap_canonical_item,
    remap_tool_payload,
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
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    validate_catalog_body,
    validate_catalog_projection,
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
    source_thread_id = self._storage._resolve_thread_identity(source_session_id, None)
    target_thread_id = self._storage._resolve_thread_identity(target_session_id, None)
    fork_id = required_text(fork_id, field="fork_id")
    if source_session_id == target_session_id:
        raise ValueError("full_rollout_copy source/target session 不能相同")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("full_rollout_copy checkpoint_ns 必须是字符串")
    if not isinstance(timestamp, str) or not timestamp:
        raise RuntimeError("full_rollout_copy timestamp 必须是非空字符串")
    if self._storage._rollout_format(connection) != storage_version.ROLLOUT_FORMAT_VERSION:
        return
    overlay_owner = connection.execute(
        "SELECT session_id, thread_id FROM database_meta WHERE singleton_id = 1"
    ).fetchone()
    if overlay_owner != (target_session_id, target_thread_id):
        raise RuntimeError(
            "full_rollout_copy target database owner 不匹配: "
            f"expected={(target_session_id, target_thread_id)!r}, actual={overlay_owner!r}"
        )

    maps: dict[str, dict[str, str]] = {}
    # acceptance 与 execution admission identity 已在 full-copy 的早期
    # 事务中按 target SessionThread 重写；把 mapping 表作为唯一的
    # source->target 来源，避免用 target key 再猜 source。
    for entity_type in (
        "accepted_ingress",
        "acceptance_idempotency_key",
        "admission_idempotency_key",
    ):
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
                message_id = (
                    maps.get("message", {}).get(source_id)
                    if entity_type == "tool_result"
                    else None
                )
                mapping[source_id] = message_id or self._full_copy_identity(
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

    item_rows = read_full_copy_item_rows(connection)
    # full copy 的目标 rollout 是从 source 副本建立的独立根；overlay epoch
    # 也必须在目标坐标系重新编号，不能把 source 的全局进度当成 target 的
    # 本地历史。保留排序后的相对顺序，保证同一 source 的多 overlay 在
    # target 中仍可按 epoch 重放。
    source_overlay_epochs = read_full_copy_overlay_epochs(
        connection, target_session_id, target_thread_id,
    )
    overlay_epoch_map = {
        source_epoch: target_epoch
        for target_epoch, source_epoch in enumerate(source_overlay_epochs)
    }
    collect("item", (row[1] for row in item_rows))
    collect("turn", rows("turn_id", "turn_records"))
    collect("turn", rows("turn_id", "turns"))
    collect("execution", rows("execution_id", "executions"))
    source_execution_by_item = dict(
        connection.execute("SELECT item_id, execution_id FROM item_catalog").fetchall()
    )
    collect("model_call", rows("model_call_id", "model_calls"))
    collect("assembly", rows("assembly_id", "context_assemblies"))
    collect("view", rows("view_id", "context_views"))
    collect("branch", rows("branch_id", "branches"))
    collect("checkpoint", rows("checkpoint_id", "checkpoints"))
    collect("epoch_transition", rows("transition_id", "context_epoch_transitions"))
    collect("anchor", rows("anchor_id", "operation_anchors"))
    collect(
        "relation",
        tuple(
            row[0]
            for row in connection.execute(
                "SELECT relation_id FROM item_relations WHERE canonical_item_id IS NULL ORDER BY relation_id"
            ).fetchall()
        ),
    )
    collect("context_contribution", rows("contribution_id", "context_contributions"))
    collect("tool_set", rows("tool_set_snapshot_id", "tool_set_snapshots"))
    collect(
        "source_overlay",
        (
            row[0]
            for row in connection.execute(
                "SELECT overlay_id FROM source_overlays "
                "WHERE session_id = ? AND thread_id = ? ORDER BY overlay_id",
                (target_session_id, target_thread_id),
            ).fetchall()
        ),
    )
    collect("assembly", rows("assembly_id", "context_plan_details"))
    # activation root 的 execution identity 与 lineage detail 都必须 target-local。
    require_resource_activation_schema(connection)
    collect(
        "activation_snapshot",
        rows("activation_snapshot_id", "resource_activation_snapshots"),
    )
    detail_assembly_map = {
        **maps["assembly"],
        **maps["activation_snapshot"],
    }
    maps["detail"] = collect_detail_mappings(
        connection,
        source_session_id=source_session_id,
        source_thread_id=source_thread_id,
        target_session_id=target_session_id,
        target_thread_id=target_thread_id,
        assembly_map=detail_assembly_map,
        allocate=lambda key: self._full_copy_identity(
            fork_id, target_session_id, "detail", key
        ),
    )
    collect("message", rows("message_id", "messages"))
    collect("tool_call", rows("tool_call_id", "tool_calls"))
    collect("control", rows("control_id", "control_events"))
    collect("plan", rows("plan_id", "context_assemblies"))
    collect_plan_identities(
        connection,
        source_session_id=source_session_id,
        source_thread_id=source_thread_id,
        collect=collect,
    )
    # 逻辑资源键按字段语义保留；不能依赖 request-ref 收集的先后顺序。
    context_source_ids = frozenset(source_id for (source_id,) in connection.execute(
        "SELECT source_id FROM context_source_control_states "
        "UNION SELECT source_id FROM checkpoint_context_source_states"
    ).fetchall())
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
                "SELECT base_ref, delta_ref FROM source_overlays "
                "WHERE session_id = ? AND thread_id = ?",
                (target_session_id, target_thread_id),
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

    # 普通 request-only ref 可以直接以 contribution_id 作为 ref_id 和
    # source_ref；这是一条 provenance identity，不能分别分配两个目标 ID。
    for source_id, target_id in maps.get("context_contribution", {}).items():
        if source_id in maps.get("request_ref", {}):
            maps["request_ref"][source_id] = target_id

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
        if entity_type == "source_identity":
            # selection 与 prefix/root 必须共用同一来源映射；有控制状态的
            # 逻辑资源键保留，普通 alias 跟随其 target-local request identity。
            return text if text in context_source_ids else maps.get("request_ref", {}).get(text, text)
        if entity_type == "detail":
            return detail_ref_key(
                mapped_detail_ref(
                    detail_ref_from_key(text),
                    source_session_id=source_session_id,
                    source_thread_id=source_thread_id,
                    target_session_id=target_session_id,
                    target_thread_id=target_thread_id,
                    detail_map=maps["detail"],
                )
            )
        return maps.get(entity_type, {}).get(text, text)

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
                if key_text in {
                    "extensions",
                    "source",
                    "tools",
                    "tool_snapshot",
                    "tool_policy",
                    "body",
                    "admission_preimage",
                }:
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
                elif key_text == "thread_id" and child == source_thread_id:
                    child = target_thread_id
                elif key_text == "plan_id":
                    child = mapped("plan", child)
                elif key_text == "ref_id":
                    if resolved_ref_type == "canonical_item":
                        child = mapped("item", child)
                    elif resolved_ref_type == "tool_set":
                        child = mapped("tool_set", child)
                    else:
                        child = mapped("request_ref", child)
                elif key_text in {
                    "item_id",
                    "root_input_item_id",
                    "final_item_id",
                    "notice_item_id",
                }:
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
                elif key_text in {"assembly_id", "previous_assembly_id"}:
                    child = mapped("assembly", child)
                elif key_text == "transition_id":
                    child = mapped("epoch_transition", child)
                elif key_text == "tool_set_snapshot_id":
                    child = mapped("tool_set", child)
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
                elif key_text == "admission_idempotency_key":
                    child = mapped("admission_idempotency_key", child)
                elif key_text in {"detail_ref", "target_detail_ref"} or (
                    key_text == "source_ref" and isinstance(child, Mapping)
                ):
                    result[key_text] = (
                        mapped_detail_ref(
                            DetailRef.from_dict(child),
                            source_session_id=source_session_id,
                            source_thread_id=source_thread_id,
                            target_session_id=target_session_id,
                            target_thread_id=target_thread_id,
                            detail_map=maps["detail"],
                        ).to_dict()
                        if child is not None
                        else None
                    )
                    continue
                elif key_text in {"contribution_id"}:
                    child = mapped("context_contribution", child)
                elif key_text == "source_ref":
                    child = mapped("source_identity", child)
                elif key_text in {
                    "overlay_id",
                    "supersedes_overlay_id",
                    "materializes_overlay_id",
                }:
                    child = mapped("source_overlay", child)
                elif key_text in {
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
        return remap_tool_payload(value, mapped)

    jsonl_path = self._storage.jsonl_path(
        target_session_id, checkpoint_ns, thread_id=target_thread_id
    )
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
            row[5], field=f"item_catalog.projection_manifest_json:{item_id}"
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
        item = CanonicalItemRecord.from_dict(envelope)
        catalog = connection.execute(
            "SELECT content_hash, payload_length, source_revision, tool_call_id "
            "FROM item_catalog WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if catalog is None:
            raise RuntimeError(
                f"full_rollout_copy 缺少 canonical item catalog: {item_id}"
            )
        validate_catalog_body(
            raw,
            sequence=sequence,
            item_id=item_id,
            catalog_hash=required_text(catalog[0], field="item_catalog.content_hash"),
            payload_length=non_negative_int(
                catalog[1], field="item_catalog.payload_length"
            ),
            source_revision=required_text(
                catalog[2], field="item_catalog.source_revision"
            ),
        )
        relation_rows = connection.execute(
            "SELECT canonical_relation_ordinal, relation, source_ref, target_ref "
            "FROM item_relations WHERE canonical_item_id = ? ORDER BY canonical_relation_ordinal",
            (item_id,),
        ).fetchall()
        validate_catalog_projection(
            item,
            projection_manifest_json=metadata_text,
            tool_call_id=optional_text(catalog[3], field="item_catalog.tool_call_id"),
            relation_rows=relation_rows,
        )
        return raw, envelope

    # v2 tool call/result identity 通常只存在 item payload，而不一定有
    # 独立 tool_calls 行。先从已校验的 source JSONL 收集这些 identity，
    # 使 full copy 的 payload remap 不会把 source tool_call_id 留在 target。
    source_items: dict[str, CanonicalItemRecord] = {}
    for (
        sequence,
        old_item_id,
        _old_turn_id,
        old_offset,
        old_length,
        _projection_manifest_json,
    ) in item_rows:
        _raw, envelope = load_item_line(
            (
                sequence,
                old_item_id,
                _old_turn_id,
                old_offset,
                old_length,
                _projection_manifest_json,
            )
        )
        source_item = CanonicalItemRecord.from_dict(envelope)
        source_items[source_item.item_id] = source_item
        if source_item.projection_identity is not None:
            collect("model_call", (source_item.projection_identity.model_call_id,))
            collect("message", (source_item.projection_identity.projection_message_id,))
        if source_item.tool_result_evidence is not None:
            collect("tool_call", (source_item.tool_result_evidence.tool_call_id,))
            collect("tool_result", (source_item.tool_result_evidence.result_id,))
        identity = source_item.projection_identity
        if identity is not None:
            collect("content_part", (ref.id for ref in identity.content_part_refs))
            if identity.block_id is not None:
                prefix = f"{identity.model_call_id}:block:"
                if identity.block_id.startswith(prefix):
                    collect("content_part", (identity.block_id.removeprefix(prefix),))
        call_ids = (
            [source_item.tool_call_id] if source_item.tool_call_id is not None else []
        )
        if isinstance(source_item.payload, Mapping):
            call_ids.extend(
                call["id"] for call in source_item.payload.get("tool_calls", ())
            )
        for call_id in call_ids:
            collect("tool_call", (call_id,))
            if identity is not None and identity.model_call_id is not None:
                prefix = f"{identity.model_call_id}:tool-call:"
                if call_id.startswith(prefix):
                    raw_call_id = call_id.removeprefix(prefix)
                    collect("tool_call", (raw_call_id,))
                    maps["tool_call"][call_id] = (
                        f"{mapped('model_call', identity.model_call_id)}:tool-call:{mapped('tool_call', raw_call_id)}"
                    )
        payload = source_item.payload
        if isinstance(payload, Mapping):
            if source_item.semantic_kind == SemanticKind.TOOL_RESULT:
                collect("tool_result", (payload.get("result_id"),))
            collect("tool_invocation", (payload.get("tool_invocation_id"),))
            collect("tool_attempt", (payload.get("tool_attempt_id"),))
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
        _projection_manifest_json,
    ) in item_rows:
        sequence_value = non_negative_int(sequence, field="item_catalog.item_sequence")
        old_position = (
            non_negative_int(old_offset, field="item_catalog.jsonl_offset"),
            non_negative_int(old_length, field="item_catalog.jsonl_length"),
        )
        old_positions[sequence_value] = old_position
        _raw, envelope = load_item_line(
            (
                sequence,
                old_item_id,
                old_turn_id,
                old_offset,
                old_length,
                _projection_manifest_json,
            )
        )
        item = CanonicalItemRecord.from_dict(envelope)
        source_execution_id = required_text(
            source_execution_by_item[old_item_id],
            field=f"source.item.execution_id:{old_item_id}",
        )
        if source_execution_id not in maps["execution"]:
            raise RuntimeError("full copy canonical item 引用了缺失的 source execution")
        producer = dict(item.producer_ref)
        producer_kind = required_text(
            producer.get("producer_kind"), field="item.producer_ref.producer_kind"
        )
        if producer.get("producer_id") in maps.get("accepted_ingress", {}):
            producer["producer_id"] = mapped(
                "accepted_ingress", producer["producer_id"]
            )
        elif producer.get("producer_id") in maps.get("message", {}):
            producer["producer_id"] = mapped("message", producer["producer_id"])
        elif producer_kind == "provider":
            producer["producer_id"] = mapped("model_call", producer.get("producer_id"))
        elif producer_kind == "tool":
            producer["producer_id"] = mapped("tool_call", producer.get("producer_id"))
        if producer.get("invocation_id") in maps.get("execution", {}):
            producer["invocation_id"] = mapped("execution", producer["invocation_id"])
        target_item_id = mapped("item", item.item_id) or item.item_id
        remapped_payload = remap_payload(item.payload)
        new_item = remap_canonical_item(
            item,
            mapped=mapped,
            source_session_id=source_session_id,
            source_thread_id=source_thread_id,
            target_session_id=target_session_id,
            target_thread_id=target_thread_id,
            item_sequence=sequence_value,
            item_id=target_item_id,
            turn_id=mapped("turn", item.turn_id),
            message_group_id=mapped("group", item.message_group_id),
            producer_ref=producer,
            payload=remapped_payload,
        )
        if (
            item.projection_identity is not None
            and item.projection_identity.block_id is not None
        ):
            part_maps[(item.item_id, item.projection_identity.block_id)] = (
                new_item.projection_identity.block_id
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
        source_thread_id=source_thread_id,
        target_session_id=target_session_id,
        target_thread_id=target_thread_id,
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
        source_items=source_items,
        new_lines=new_lines,
        new_lines_by_sequence=new_lines_by_sequence,
        mapped=mapped,
        remap_json=remap_json,
        remap_payload=remap_payload,
    )
