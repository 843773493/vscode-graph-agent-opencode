"""full_rollout_copy remap 的共享状态。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.fork.full_copy.plans import (
    CopiedPlanSource,
)
from app.services.infrastructure.rollout_context.runtime.detail_fork import (
    ForkDetailCapability,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
)


class FullCopyRemapState:
    def __init__(
        self,
        *,
        service: object,
        connection: sqlite3.Connection,
        source_session_id: str,
        target_session_id: str,
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
        new_lines: list[bytes],
        new_lines_by_sequence: dict[int, bytes],
        mapped: Callable[[str, object], str | None],
        remap_json: Callable[..., object],
        remap_payload: Callable[[object], object],
    ) -> None:
        self.service = service
        self.connection = connection
        self.source_session_id = source_session_id
        self.target_session_id = target_session_id
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
        self.target_session_key: bytes | None = None
        self.detail_records: dict[str, DetailRecord] = {}
        self.detail_digests: dict[str, str] = {}
        self.source_assembly_fingerprints: dict[str, tuple[str, str, str]] = {}
        self.plan_sources: tuple[CopiedPlanSource, ...] = ()
        self.plan_failure_rows: tuple[tuple[object, ...], ...] = ()


__all__ = ["FullCopyRemapState"]
