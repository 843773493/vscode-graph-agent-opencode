"""跨 session fork 的 target-local identity 辅助。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace

from app.domain.itemized.hashing import content_hash as item_content_hash
from app.domain.itemized.identity.item_projection import ItemProvenanceRelation
from app.domain.itemized.records import CanonicalItemRecord


class RolloutIdentityMixin:
    """生成 rollout 的稳定本地标识。"""

    def _rollout_id(
        self, session_id: str, *, thread_id: str, checkpoint_ns: str
    ) -> str:
        return (
            "rollout-"
            + hashlib.sha256(
                str(self.root(session_id, checkpoint_ns, thread_id=thread_id)).encode()
            ).hexdigest()[:24]
        )


def target_local_acceptance_identity(
    *,
    target_session_id: str,
    fork_id: str,
    source_accepted_ingress_id: str,
    source_acceptance_key: str,
) -> tuple[str, str]:
    """生成不复用 source key 的 target ingress/key，并保留可审计映射输入。"""
    seed = (
        f"{target_session_id}|{fork_id}|{source_accepted_ingress_id}|"
        f"{source_acceptance_key}"
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return (
        f"fork-ingress:{target_session_id}:{digest}",
        f"fork-acceptance:{target_session_id}:{digest}",
    )


def remap_tool_payload(
    payload: object,
    mapped: Callable[[str, str | None], str | None],
) -> object:
    """只平移工具协议的身份位置；args/content 与未知业务字段保持原样。"""
    if not isinstance(payload, Mapping):
        return payload
    result = dict(payload)
    for key, entity in (
        ("tool_call_id", "tool_call"), ("call_id", "tool_call"),
        ("tool_invocation_id", "tool_invocation"), ("tool_attempt_id", "tool_attempt"),
        ("result_id", "tool_result"),
    ):
        if key in result:
            result[key] = mapped(entity, result[key])
    if "id" in result and "name" in result and "args" in result:
        result["id"] = mapped("tool_call", result["id"])
    if "tool_calls" in result:
        result["tool_calls"] = [remap_tool_payload(call, mapped) for call in result["tool_calls"]]
    return result


def remap_canonical_item(
    item: CanonicalItemRecord,
    *,
    mapped: Callable[[str, str | None], str | None],
    source_session_id: str,
    source_thread_id: str,
    target_session_id: str,
    target_thread_id: str,
    item_sequence: int,
    item_id: str,
    turn_id: str | None,
    message_group_id: str | None,
    producer_ref: Mapping[str, object],
    payload: object,
) -> CanonicalItemRecord:
    """fork 只改 typed 本地身份；未知 extensions 原样保留。"""
    identity = item.projection_identity
    if identity is not None:
        target_model = mapped("model_call", identity.model_call_id)
        block_id = identity.block_id
        if block_id is not None:
            content_prefix = f"{identity.model_call_id}:block:"
            tool_prefix = f"{identity.model_call_id}:tool-call:"
            if block_id.startswith(content_prefix):
                block_id = f"{target_model}:block:{mapped('content_part', block_id.removeprefix(content_prefix))}"
            elif block_id.startswith(tool_prefix):
                block_id = f"{target_model}:tool-call:{mapped('tool_call', block_id.removeprefix(tool_prefix))}"
            else:
                raise ValueError("fork stream block 与 typed model-call scope 不一致")
        identity = replace(
            identity,
            projection_message_id=mapped("message", identity.projection_message_id),
            model_call_id=target_model,
            block_id=block_id,
            content_part_refs=tuple(
                replace(ref, id=mapped("content_part", ref.id))
                for ref in identity.content_part_refs
            ),
        )
    evidence = item.tool_result_evidence
    if evidence is not None:
        evidence = replace(
            evidence,
            tool_call_id=mapped("tool_call", evidence.tool_call_id),
            result_id=mapped("tool_result", evidence.result_id),
        )
    relations = []
    for relation in item.provenance_relations:
        if relation.target_kind == "projection_message":
            relation = replace(
                relation, target_id=mapped("message", relation.target_id),
                target_model_call_id=mapped("model_call", relation.target_model_call_id),
            )
        elif relation.target_kind == "canonical_item" and (
            relation.target_session_id, relation.target_thread_id
        ) == (source_session_id, source_thread_id):
            relation = replace(
                relation, target_id=mapped("item", relation.target_id),
                target_session_id=target_session_id, target_thread_id=target_thread_id,
                target_turn_id=mapped("turn", relation.target_turn_id),
                target_model_call_id=mapped("model_call", relation.target_model_call_id),
            )
        relations.append(relation)
    relations.append(ItemProvenanceRelation(
        "derived_from", "canonical_item", item.item_id,
        target_session_id=source_session_id, target_thread_id=source_thread_id,
        target_turn_id=item.turn_id,
        target_model_call_id=item.projection_identity.model_call_id if item.projection_identity else None,
    ))
    digest = item_content_hash(item.payload_kind, payload)
    source_manifest = item.source_manifest
    if source_manifest is not None and source_manifest.source_revision == f"canonical:{item.item_id}:{item.content_hash}":
        source_manifest = replace(source_manifest, source_revision=f"canonical:{item_id}:{digest}")
    result = replace(
        item, item_sequence=item_sequence, item_id=item_id, turn_id=turn_id,
        message_group_id=message_group_id, producer_ref=dict(producer_ref),
        payload=payload, content_hash=digest, projection_identity=identity,
        tool_result_evidence=evidence, source_manifest=source_manifest,
        provenance_relations=tuple(relations),
    )
    result.validate()
    return result


__all__ = ["RolloutIdentityMixin", "remap_canonical_item", "remap_tool_payload", "target_local_acceptance_identity"]
