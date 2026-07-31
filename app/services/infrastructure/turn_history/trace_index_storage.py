from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from .trace_index_models import (
    PreparedTraceTurnEntry,
    TraceTurnIndexEntry,
    TraceTurnIndexManifest,
)


class TraceIndexStorage:
    """封装 Trace Turn index 的恢复、截断与 manifest 原子 IO。"""

    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.index_path = trace_dir / "turn-events.index.jsonl"
        self.manifest_path = trace_dir / "turn-events.index.json"

    def recover(
        self,
        *,
        validate_committed_entry: Callable[[TraceTurnIndexEntry], None],
        commit_prepared: Callable[[PreparedTraceTurnEntry], None],
    ) -> TraceTurnIndexManifest:
        manifest = self.load_manifest()
        if manifest is None:
            message_size = self.message_size()
            manifest = TraceTurnIndexManifest(
                committed_trace_offset=self.trace_size(),
                committed_message_offset=message_size,
                has_unindexed_prefix=message_size > 0,
            )
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            self.index_path.touch(exist_ok=True)
            self.write_manifest(manifest)
            return manifest
        if not self.index_path.exists():
            raise RuntimeError("Trace Turn index manifest 存在但 index 文件缺失")
        index_size = self.index_path.stat().st_size
        if index_size < manifest.committed_index_offset:
            raise RuntimeError("Trace Turn index committed offset 越过文件末尾")
        if index_size == manifest.committed_index_offset:
            if self.message_size() != manifest.committed_message_offset:
                raise RuntimeError("messages.jsonl 存在未索引语义事件")
            if self.trace_size() < manifest.committed_trace_offset:
                raise RuntimeError("events.jsonl 小于 Trace Turn index 已提交水位")
            return manifest

        with self.index_path.open("rb") as stream:
            stream.seek(manifest.committed_index_offset)
            line = stream.readline()
            pending_end = stream.tell()
            if not line.endswith(b"\n") or stream.read(1):
                raise RuntimeError("Trace Turn index 含多个或不完整的未提交尾记录")
        entry = TraceTurnIndexEntry.model_validate_json(line)
        trace_size = self.trace_size()
        message_size = self.message_size()
        if trace_size >= entry.trace_end and message_size >= entry.message_end:
            validate_committed_entry(entry)
            prepared = PreparedTraceTurnEntry(
                entry=entry,
                index_start=manifest.committed_index_offset,
                index_end=pending_end,
                previous_manifest=manifest,
            )
            commit_prepared(prepared)
            return self.load_manifest_required()
        if trace_size < entry.trace_start:
            raise RuntimeError("events.jsonl 小于 pending Trace 事件起点")
        if message_size < entry.message_start:
            raise RuntimeError("messages.jsonl 小于 pending 语义事件起点")
        with self.index_path.open("r+b") as stream:
            stream.truncate(manifest.committed_index_offset)
        if trace_size > entry.trace_start:
            with (self.trace_dir / "events.jsonl").open("r+b") as stream:
                stream.truncate(entry.trace_start)
        if message_size > entry.message_start:
            with (self.trace_dir / "messages.jsonl").open("r+b") as stream:
                stream.truncate(entry.message_start)
        return manifest

    def repair_uncommitted_trace_tail(
        self,
        manifest: TraceTurnIndexManifest,
    ) -> None:
        trace_path = self.trace_dir / "events.jsonl"
        trace_size = self.trace_size()
        if trace_size == 0:
            return
        committed = manifest.committed_trace_offset
        with trace_path.open("rb") as stream:
            if committed > 0:
                stream.seek(committed - 1)
                if stream.read(1) != b"\n":
                    raise RuntimeError("events.jsonl 未完成尾行侵入已提交语义水位")
            stream.seek(trace_size - 1)
            if stream.read(1) == b"\n":
                return

        block_size = 64 * 1024
        position = trace_size
        truncate_at = committed
        with trace_path.open("rb") as stream:
            while position > committed:
                read_start = max(committed, position - block_size)
                stream.seek(read_start)
                chunk = stream.read(position - read_start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    truncate_at = read_start + newline + 1
                    break
                position = read_start
        with trace_path.open("r+b") as stream:
            stream.truncate(truncate_at)

    def load_manifest(self) -> TraceTurnIndexManifest | None:
        if not self.manifest_path.exists():
            return None
        return self.load_manifest_required()

    def load_manifest_required(self) -> TraceTurnIndexManifest:
        return TraceTurnIndexManifest.model_validate_json(
            self.manifest_path.read_text(encoding="utf-8")
        )

    def write_manifest(self, manifest: TraceTurnIndexManifest) -> None:
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        temp = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with temp.open("wb") as stream:
            stream.write(manifest.model_dump_json().encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.manifest_path)

    def trace_size(self) -> int:
        path = self.trace_dir / "events.jsonl"
        return path.stat().st_size if path.exists() else 0

    def message_size(self) -> int:
        path = self.trace_dir / "messages.jsonl"
        return path.stat().st_size if path.exists() else 0
