from __future__ import annotations

import os
from pathlib import Path

from app.abstractions.turn_history import (
    TurnBootstrapBatch,
    TurnIndexedEvent,
    TurnRecoveryBatch,
)
from app.schemas.event import Event

from .trace_index_compaction import (
    MAX_COMPACT_LINE_BYTES,
    _AnyEvent,
    compact_event_json,
)
from .trace_index_models import (
    PreparedTraceTurnEntry,
    TraceTurnIndexEntry,
    TraceTurnIndexManifest,
)
from .trace_index_storage import TraceIndexStorage

THREAD_OWNER_EVENT_TYPES = frozenset({"job_created", "job_started", "message_created"})


class TraceTurnIndex:
    """Trace 的轻量 Turn 起点、语义水位和有界回放索引。"""

    def __init__(self, trace_dir: Path) -> None:
        self._trace_dir = trace_dir
        self._index_path = trace_dir / "turn-events.index.jsonl"
        self._storage = TraceIndexStorage(trace_dir)

    def prepare(
        self,
        event: Event,
        *,
        trace_start: int,
        serialized_size: int,
        projects_turn: bool,
    ) -> PreparedTraceTurnEntry:
        manifest = self._recover()
        actual_trace_start = self._trace_size()
        if actual_trace_start != trace_start:
            raise RuntimeError(
                "Trace 文件在 append 临界区内发生变化: "
                f"expected={trace_start}, actual={actual_trace_start}"
            )
        if self._message_size() != manifest.committed_message_offset:
            raise RuntimeError(
                "Trace Turn index 与 messages.jsonl 水位不一致: "
                f"indexed={manifest.committed_message_offset}, "
                f"actual={self._message_size()}"
            )
        trace_end = trace_start + serialized_size
        message_start = self._message_size()
        message_end = message_start + serialized_size
        full_payload = event.model_dump(mode="json")
        compact_payload = compact_event_json(full_payload)
        entry = TraceTurnIndexEntry(
            event_id=event.event_id,
            event_type=event.type,
            job_id=event.job_id,
            projects_turn=projects_turn,
            trace_start=trace_start,
            trace_end=trace_end,
            message_start=message_start,
            message_end=message_end,
            source_compacted=compact_payload != full_payload,
            compact_event=compact_payload,
        )
        line = entry.model_dump_json().encode("utf-8") + b"\n"
        if len(line) > MAX_COMPACT_LINE_BYTES:
            raise RuntimeError(
                "Trace Turn index 事件超过固定上限: "
                f"event_id={event.event_id}, bytes={len(line)}"
            )
        self._trace_dir.mkdir(parents=True, exist_ok=True)
        index_start = (
            self._index_path.stat().st_size if self._index_path.exists() else 0
        )
        with self._index_path.open("ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        return PreparedTraceTurnEntry(
            entry=entry,
            index_start=index_start,
            index_end=index_start + len(line),
            previous_manifest=manifest,
        )

    def recover_before_append(self) -> int:
        """在统一 append 分支前恢复 pending 事务并返回稳定 Trace 末端。"""
        manifest = self._recover()
        self._storage.repair_uncommitted_trace_tail(manifest)
        return self._trace_size()

    def commit(self, prepared: PreparedTraceTurnEntry) -> None:
        entry = prepared.entry
        if (
            self._trace_size() < entry.trace_end
            or self._message_size() < entry.message_end
        ):
            raise RuntimeError(
                f"Trace Turn index 不能提交未落盘事件: event_id={entry.event_id}"
            )
        manifest = prepared.previous_manifest.model_copy(deep=True)
        manifest.committed_index_offset = prepared.index_end
        manifest.committed_trace_offset = entry.trace_end
        manifest.committed_message_offset = entry.message_end
        if entry.projects_turn:
            manifest.event_cursor = entry.event_id
            manifest.projected_message_offset = entry.message_end
            manifest.projected_trace_offset = entry.trace_end
        if entry.event_type == "job_created":
            manifest.latest_job_index_offset = prepared.index_start
            manifest.latest_job_id = entry.job_id
        self._storage.write_manifest(manifest)

    def bootstrap_batch(
        self,
        *,
        max_events: int,
        max_bytes: int,
    ) -> TurnBootstrapBatch:
        manifest = self._load_manifest()
        if manifest is None:
            raise RuntimeError(
                "Trace Turn bootstrap 阶段缺少索引 manifest: "
                f"path={self._trace_dir / 'turn-events.index.json'}"
            )
        manifest = self._recover()
        if manifest.has_unindexed_prefix:
            raise RuntimeError(
                "Trace Turn bootstrap 阶段存在未索引语义事件: "
                f"path={self._trace_dir}, "
                "请先显式重建 turn-events index"
            )
        if manifest.latest_job_index_offset is None:
            return TurnBootstrapBatch(
                event_cursor=manifest.event_cursor,
                event_offset=(
                    manifest.projected_message_offset
                    if manifest.event_cursor is not None
                    else None
                ),
                has_older_events=manifest.has_unindexed_prefix,
            )
        first = self._read_entry_at(manifest.latest_job_index_offset)
        tail, omitted = self._read_tail_entries(
            start=manifest.latest_job_index_offset,
            end=manifest.committed_index_offset,
            max_events=max(1, max_events - 1),
            max_bytes=max_bytes,
        )
        entries = [
            first,
            *(entry for entry in tail if entry.event_id != first.event_id),
        ]
        if len(entries) > max_events:
            entries = [first, *entries[-(max_events - 1) :]]
            omitted = True
        return TurnBootstrapBatch(
            events=[self._indexed_event(entry, compact=True) for entry in entries],
            event_cursor=manifest.event_cursor,
            event_offset=manifest.projected_message_offset,
            has_older_events=(
                manifest.has_unindexed_prefix
                or manifest.latest_job_index_offset > 0
                or omitted
                or any(entry.source_compacted for entry in entries)
            ),
        )

    def recovery_batch(
        self,
        *,
        after_event_id: str | None,
        max_events: int,
        max_bytes: int,
    ) -> TurnRecoveryBatch:
        manifest = self._load_manifest()
        if manifest is None:
            return TurnRecoveryBatch(event_cursor=None, complete=False)
        manifest = self._recover()
        if (
            after_event_id is not None
            and after_event_id == manifest.event_cursor
            and manifest.projected_message_offset > 0
        ):
            return TurnRecoveryBatch(
                event_cursor=manifest.event_cursor,
                event_offset=manifest.projected_message_offset,
            )
        if (
            after_event_id is None
            and manifest.event_cursor is None
            and not manifest.has_unindexed_prefix
        ):
            return TurnRecoveryBatch(complete=True)
        entries, _ = self._read_tail_entries(
            start=0,
            end=manifest.committed_index_offset,
            max_events=max_events + 1,
            max_bytes=max_bytes,
        )
        if after_event_id is None:
            return TurnRecoveryBatch(
                event_cursor=manifest.event_cursor,
                event_offset=(
                    manifest.projected_message_offset
                    if manifest.event_cursor is not None
                    and manifest.projected_message_offset > 0
                    else None
                ),
                complete=False,
            )
        cursor_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if entry.event_id == after_event_id
            ),
            None,
        )
        if cursor_index is None:
            return TurnRecoveryBatch(
                event_cursor=manifest.event_cursor,
                event_offset=manifest.projected_message_offset,
                complete=False,
            )
        pending = entries[cursor_index + 1 :]
        source_bytes = sum(entry.trace_end - entry.trace_start for entry in pending)
        if len(pending) > max_events or source_bytes > max_bytes:
            return TurnRecoveryBatch(
                event_cursor=manifest.event_cursor,
                event_offset=manifest.projected_message_offset,
                complete=False,
            )
        return TurnRecoveryBatch(
            events=[self._indexed_event(entry, compact=False) for entry in pending],
            event_cursor=manifest.event_cursor,
            event_offset=manifest.projected_message_offset,
            complete=True,
            bytes_read=source_bytes,
        )

    def snapshot(self) -> TraceTurnIndexManifest | None:
        return self._recover() if self._load_manifest() is not None else None

    def job_threads(self, session_id: str, job_ids: set[str]) -> dict[str, str]:
        """只从已提交的 Job 创建/启动事实取得归属，不读取历史请求正文。"""
        if not job_ids:
            return {}
        manifest = self.snapshot()
        if manifest is None or manifest.has_unindexed_prefix:
            raise RuntimeError("Trace 缺少完整的归属索引，请显式重建 turn-events index")
        thread_by_job_id: dict[str, str] = {}
        # TODO: 10 万条合成索引归属查询中位约 1.46s（8.6 测量收据）；
        # 后续复用现有索引增加按 Job 的定位，不得另存可变归属。调用端已移出事件循环。
        with self._index_path.open("rb") as stream:
            while stream.tell() < manifest.committed_index_offset:
                line = stream.readline(MAX_COMPACT_LINE_BYTES + 1)
                if (
                    not line.endswith(b"\n")
                    or stream.tell() > manifest.committed_index_offset
                ):
                    raise RuntimeError("Trace 归属索引越过已提交边界")
                entry = TraceTurnIndexEntry.model_validate_json(line)
                if (
                    entry.job_id not in job_ids
                    or entry.event_type not in THREAD_OWNER_EVENT_TYPES
                ):
                    continue
                owner_event = _AnyEvent.model_validate(entry.compact_event).root
                owner_payload = owner_event.payload
                owner_session_id = getattr(owner_payload, "session_id", None)
                owner_thread_id = getattr(owner_payload, "thread_id", None)
                if (
                    owner_session_id != session_id
                    or not isinstance(owner_thread_id, str)
                    or not owner_thread_id
                ):
                    raise RuntimeError(
                        f"Trace Job 归属与 Session 不一致: job_id={entry.job_id}"
                    )
                previous_thread_id = thread_by_job_id.setdefault(
                    entry.job_id, owner_thread_id
                )
                if previous_thread_id != owner_thread_id:
                    raise RuntimeError(
                        f"Trace Job 的 Thread 归属漂移: job_id={entry.job_id}"
                    )
        missing_job_ids = job_ids - thread_by_job_id.keys()
        if missing_job_ids:
            raise RuntimeError(
                "Trace Job 缺少持久 Thread 归属: "
                f"session_id={session_id}, job_ids={sorted(missing_job_ids)}"
            )
        return thread_by_job_id

    def trace_offset_after_event(
        self,
        event_id: str,
        *,
        max_index_bytes: int = 256 * 1024,
    ) -> int | None:
        """O(1) 命中最新语义 cursor，旧 cursor 只读取有界 index 尾部。"""
        manifest = self._load_manifest()
        if manifest is None:
            return None
        manifest = self._recover()
        if event_id == manifest.event_cursor:
            return manifest.projected_trace_offset
        entries, _ = self._read_tail_entries(
            start=0,
            end=manifest.committed_index_offset,
            max_events=1024,
            max_bytes=max_index_bytes,
        )
        matched = next((entry for entry in entries if entry.event_id == event_id), None)
        return matched.trace_end if matched is not None else None

    def _recover(self) -> TraceTurnIndexManifest:
        return self._storage.recover(
            validate_committed_entry=self._validate_committed_entry,
            commit_prepared=self.commit,
        )

    def _validate_committed_entry(self, entry: TraceTurnIndexEntry) -> None:
        source_event = self._read_source_event(entry)
        message_event = self._read_message_event(entry)
        if (
            source_event.event_id != entry.event_id
            or message_event.event_id != entry.event_id
        ):
            raise RuntimeError(
                "Trace Turn index pending 记录身份与数据文件不一致: "
                f"event_id={entry.event_id}"
            )

    def _read_message_event(self, entry: TraceTurnIndexEntry) -> Event:
        size = entry.message_end - entry.message_start
        with (self._trace_dir / "messages.jsonl").open("rb") as stream:
            stream.seek(entry.message_start)
            line = stream.read(size)
        if len(line) != size or not line.endswith(b"\n"):
            raise RuntimeError(
                f"Trace index 指向的 message 行损坏: event_id={entry.event_id}"
            )
        return _AnyEvent.model_validate_json(line).root

    def _read_tail_entries(
        self,
        *,
        start: int,
        end: int,
        max_events: int,
        max_bytes: int,
    ) -> tuple[list[TraceTurnIndexEntry], bool]:
        read_start = max(start, end - max_bytes)
        with self._index_path.open("rb") as stream:
            stream.seek(read_start)
            if read_start > start:
                stream.readline()
            lines = stream.read(end - stream.tell()).splitlines()
        omitted = read_start > start or len(lines) > max_events
        selected = lines[-max_events:]
        return (
            [TraceTurnIndexEntry.model_validate_json(line) for line in selected],
            omitted,
        )

    def _read_entry_at(self, offset: int) -> TraceTurnIndexEntry:
        with self._index_path.open("rb") as stream:
            stream.seek(offset)
            line = stream.readline(MAX_COMPACT_LINE_BYTES + 1)
        if not line.endswith(b"\n") or len(line) > MAX_COMPACT_LINE_BYTES:
            raise RuntimeError(f"Trace Turn index 起点记录损坏: offset={offset}")
        return TraceTurnIndexEntry.model_validate_json(line)

    def _indexed_event(
        self,
        entry: TraceTurnIndexEntry,
        *,
        compact: bool,
    ) -> TurnIndexedEvent:
        event = (
            _AnyEvent.model_validate(entry.compact_event).root
            if compact
            else self._read_source_event(entry)
        )
        return TurnIndexedEvent(event=event, source_offset=entry.message_end)

    def _read_source_event(self, entry: TraceTurnIndexEntry) -> Event:
        size = entry.trace_end - entry.trace_start
        with (self._trace_dir / "events.jsonl").open("rb") as stream:
            stream.seek(entry.trace_start)
            line = stream.read(size)
        if len(line) != size or not line.endswith(b"\n"):
            raise RuntimeError(
                f"Trace index 指向的事件行损坏: event_id={entry.event_id}"
            )
        event = _AnyEvent.model_validate_json(line).root
        if event.event_id != entry.event_id:
            raise RuntimeError(f"Trace index 指向错误事件: event_id={entry.event_id}")
        return event

    def _load_manifest(self) -> TraceTurnIndexManifest | None:
        return self._storage.load_manifest()

    def _trace_size(self) -> int:
        return self._storage.trace_size()

    def _message_size(self) -> int:
        return self._storage.message_size()
