"""full_rollout_copy remap 的共享状态。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    ProviderProfileItemFrame,
    seal_assembly,
)
from app.domain.itemized.epoch.seal_binding import ContextSealBinding
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.fork.full_copy.plans import (
    CopiedPlanSource,
)
from app.services.infrastructure.rollout_context.fork.validation import required_text
from app.services.infrastructure.rollout_context.runtime.detail_fork import (
    ForkDetailCapability,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
)
from app.services.mapping.itemized.prefix_frames import project_canonical_prefix_frames


class FullCopyRemapState:
    def __init__(
        self,
        *,
        service: object,
        connection: sqlite3.Connection,
        source_session_id: str,
        source_thread_id: str,
        target_session_id: str,
        target_thread_id: str,
        fork_id: str,
        checkpoint_ns: str,
        timestamp: str,
        maps: dict[str, dict[str, str]],
        overlay_epoch_map: dict[int, int],
        item_rows: tuple[tuple[object, ...], ...],
        content_part_rows: tuple[tuple[object, ...], ...],
        part_maps: dict[tuple[str, str], str],
        old_jsonl: bytes,
        old_positions: dict[int, tuple[int, int]],
        new_positions: dict[int, tuple[int, int]],
        new_items: dict[int, CanonicalItemRecord],
        source_items: dict[str, CanonicalItemRecord],
        new_lines: list[bytes],
        new_lines_by_sequence: dict[int, bytes],
        mapped: Callable[[str, object], str | None],
        remap_json: Callable[..., object],
        remap_payload: Callable[[object], object],
    ) -> None:
        self.service = service
        self.connection = connection
        self.source_session_id = source_session_id
        self.source_thread_id = source_thread_id
        self.target_session_id = target_session_id
        self.target_thread_id = target_thread_id
        self.fork_id = fork_id
        self.checkpoint_ns = checkpoint_ns
        self.timestamp = timestamp
        self.maps = maps
        self.overlay_epoch_map = overlay_epoch_map
        self.item_rows = item_rows
        self.content_part_rows = content_part_rows
        self.part_maps = part_maps
        self.old_jsonl = old_jsonl
        self.old_positions = old_positions
        self.new_positions = new_positions
        self.new_items = new_items
        self.source_items = source_items
        self.new_lines = new_lines
        self.new_lines_by_sequence = new_lines_by_sequence
        self.mapped = mapped
        self.remap_json = remap_json
        self.remap_payload = remap_payload
        self.jsonl_path: Path | None = None
        self.temporary: Path | None = None
        self.moved_details: list[tuple[Path, Path]] = []
        self.detail_originals: dict[Path, bytes] = {}
        self.detail_hashes: dict[str, str] = {}
        self.detail_rows: tuple[tuple[object, ...], ...] = ()
        self.session_root: Path | None = None
        self.detail_capability: ForkDetailCapability | None = None
        self.detail_records: dict[str, DetailRecord] = {}
        self.source_assembly_fingerprints: dict[str, tuple[str, str, str]] = {}
        self.plan_sources: tuple[CopiedPlanSource, ...] = ()
        self.plan_failure_rows: tuple[tuple[object, ...], ...] = ()


def localize_full_copy_seal_bindings(
    state: FullCopyRemapState,
    sources: Mapping[str, ContextSealBinding],
) -> dict[str, ContextSealBinding]:
    """从已验证 canonical facts 本地化前缀；只生成值，不提交任何事实。"""
    target_items = {item.item_id: item for item in state.new_items.values()}
    pending = dict(sources)
    localized: dict[str, ContextSealBinding] = {}
    while pending:
        before_count = len(pending)
        for assembly_id, source in tuple(pending.items()):
            previous_id = source.previous_assembly_id
            if previous_id is not None and previous_id not in localized:
                if previous_id not in pending:
                    raise RuntimeError(
                        f"source-mismatch: fork prefix 缺少真实前驱 binding: {previous_id}"
                    )
                continue
            parent = None
            if previous_id is None:
                if source.prefix.state.epoch.ordinal != 1 or source.prefix.parent is not None:
                    raise RuntimeError("source-mismatch: fork 初始 prefix 不得携带前驱或旧 parent")
            else:
                previous = sources[previous_id].prefix
                current_epoch = source.prefix.state.epoch
                previous_epoch = previous.state.epoch
                if current_epoch == previous_epoch:
                    if source.prefix.parent != previous.state.parent:
                        raise RuntimeError("source-mismatch: fork 同 epoch prefix 缺少真实前驱 self parent")
                    parent = localized[previous_id].prefix.state.parent
                elif current_epoch.ordinal == previous_epoch.ordinal + 1:
                    if source.prefix.parent is not None or current_epoch.reason.value not in {
                        "rewind", "compaction", "toolset_changed",
                    }:
                        raise RuntimeError("source-mismatch: fork 新 epoch 必须是真实 rebuild 且无旧 parent")
                else:
                    raise RuntimeError("source-mismatch: fork prefix epoch 与真实前驱不连续")
            epoch = replace(
                source.prefix.state.epoch,
                turn_id=state.mapped("turn", source.prefix.state.epoch.turn_id),
            )
            root = None if source.root is None else replace(
                source.root, epoch=epoch,
                sources=tuple(replace(
                    provenance,
                    source_identity=state.mapped("source_identity", provenance.source_identity),
                ) for provenance in source.root.sources),
            )

            root_ref = source.prefix.state.frames[0].item_ref if root is not None else None
            root_revision = root.frame_hash() if root is not None else None

            def remap_ref(
                ref: AppendedItemRef,
                *,
                root_ref: AppendedItemRef | None = root_ref,
                root_revision: str | None = root_revision,
            ) -> AppendedItemRef:
                if ref.ref_type == "canonical_item":
                    return replace(ref, ref_id=state.maps["item"][ref.ref_id])
                return replace(
                    ref,
                    ref_id=state.mapped("request_ref", ref.ref_id),
                    source_identity=state.mapped("source_identity", ref.source_identity),
                    source_revision=(
                        root_revision if ref == root_ref
                        else ref.source_revision
                    ),
                )

            groups: dict[tuple[str, ...], tuple[
                tuple[ProviderProfileItemFrame, ...], tuple[ProviderProfileItemFrame, ...],
            ]] = {}
            indexes: dict[tuple[str, ...], int] = {}
            frames = []
            for frame in source.prefix.state.frames:
                if frame.item_ref.ref_type != "canonical_item":
                    frames.append(replace(frame, item_ref=remap_ref(frame.item_ref)))
                    continue
                content = frame.content
                if not isinstance(content, Mapping) or set(content) != {"item_ids", "message"}:
                    raise RuntimeError("source-mismatch: fork canonical frame 缺少真实 group manifest")
                raw_ids = content["item_ids"]
                if not isinstance(raw_ids, list) or not raw_ids:
                    raise RuntimeError("source-mismatch: fork canonical frame group 为空或非法")
                ids = tuple(required_text(value, field="prefix.item_ids") for value in raw_ids)
                if ids not in groups:
                    original, _ = project_canonical_prefix_frames(
                        tuple(state.source_items[item_id] for item_id in ids),
                        provider_profile=frame.provider_profile, start_ordinal=0,
                    )
                    target, _ = project_canonical_prefix_frames(
                        tuple(target_items[state.maps["item"][item_id]] for item_id in ids),
                        provider_profile=frame.provider_profile, start_ordinal=0,
                    )
                    if len(original) != len(target):
                        raise RuntimeError("source-mismatch: fork canonical group 投影数量发生变化")
                    groups[ids] = original, target
                    indexes[ids] = 0
                original, target = groups[ids]
                index = indexes[ids]
                if index >= len(original) or (
                    frame.frame_bytes() != replace(original[index], ordinal=frame.ordinal).frame_bytes()
                    or frame.item_ref != original[index].item_ref
                ):
                    raise RuntimeError("source-mismatch: fork canonical frame 与原始事实投影不一致")
                frames.append(replace(target[index], ordinal=frame.ordinal))
                indexes[ids] += 1
            if any(indexes[ids] != len(group[0]) for ids, group in groups.items()):
                raise RuntimeError("source-mismatch: fork canonical group 未完整覆盖原始事实")
            prepared = replace(
                source.prefix.state, epoch=epoch, frames=tuple(frames), parent=parent,
                appended_refs=tuple(remap_ref(ref) for ref in source.prefix.appended_refs),
            )
            transition = source.prefix.consumed_transition
            prefix = replace(
                seal_assembly(prepared, assembly_id=state.maps["assembly"][assembly_id]),
                consumed_transition=None if transition is None else replace(
                    transition,
                    transition_id=state.maps["epoch_transition"][transition.transition_id],
                    turn_id=state.mapped("turn", transition.turn_id),
                ),
            )
            localized[assembly_id] = replace(
                source, prefix=prefix, root=root,
                tool_set_snapshot_id=state.maps["tool_set"][source.tool_set_snapshot_id],
                previous_assembly_id=state.mapped("assembly", previous_id),
            )
            del pending[assembly_id]
        if len(pending) == before_count:
            raise RuntimeError("source-mismatch: fork prefix 前驱链存在循环")
    return localized


__all__ = ["FullCopyRemapState", "localize_full_copy_seal_bindings"]
