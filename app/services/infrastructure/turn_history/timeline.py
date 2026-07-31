from __future__ import annotations

from dataclasses import dataclass

from .files import TurnHistoryFiles
from .models import TimelineEntry, TurnIndex

_MAX_TIMELINE_PAGE_SCAN_ENTRIES = 128
_MAX_TIMELINE_PAGE_SCAN_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class TimelineScanPage:
    entries: list[TimelineEntry]
    next_anchor_turn_id: str | None
    has_more: bool


class TurnTimeline:
    """维护 append-only Turn 时间线和可见 Turn 索引。"""

    def __init__(self, files: TurnHistoryFiles) -> None:
        self._files = files

    def rebuild_index(
        self,
        session_id: str,
        *,
        projection_epoch: int,
    ) -> TurnIndex:
        path = self._files.timeline_path(session_id)
        visible: list[TimelineEntry] = []
        seen: set[str] = set()
        latest_ordinal = 0
        with path.open("rb") as stream:
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    raise RuntimeError(
                        "Turn timeline 尾行不完整: "
                        f"session_id={session_id}, offset={stream.tell()}"
                    )
                entry = TimelineEntry.model_validate_json(line)
                if entry.turn_id in seen:
                    raise RuntimeError(
                        "Turn timeline 包含重复身份: "
                        f"session_id={session_id}, turn_id={entry.turn_id}"
                    )
                seen.add(entry.turn_id)
                if entry.ordinal != latest_ordinal + 1:
                    raise RuntimeError(
                        "Turn timeline ordinal 必须严格连续递增: "
                        f"session_id={session_id}, previous={latest_ordinal}, "
                        f"current={entry.ordinal}, turn_id={entry.turn_id}"
                    )
                record = self._files.read_turn_header(
                    session_id,
                    entry.turn_id,
                    required=True,
                )
                if record.summary.ordinal != entry.ordinal:
                    raise RuntimeError(
                        "Turn timeline ordinal 与记录不一致: "
                        f"turn_id={entry.turn_id}, timeline={entry.ordinal}, "
                        f"record={record.summary.ordinal}"
                    )
                latest_ordinal = entry.ordinal
                if record.visible:
                    visible.append(entry)
        return TurnIndex(
            projection_epoch=projection_epoch,
            turn_count=len(visible),
            latest_ordinal=latest_ordinal,
            latest_turn_id=visible[-1].turn_id if visible else None,
            timeline_size=path.stat().st_size,
        )

    def validate_index_anchor(self, session_id: str, index: TurnIndex) -> None:
        """常规读取只定点校验尾锚；完整 rebuild 负责全量唯一性。"""
        path = self._files.timeline_path(session_id)
        if index.timeline_size != path.stat().st_size:
            raise RuntimeError(
                "Turn timeline 与 index committed size 不一致: "
                f"session_id={session_id}, indexed={index.timeline_size}, "
                f"actual={path.stat().st_size}"
            )
        if index.latest_turn_id is None:
            if index.turn_count != 0 or index.latest_ordinal != 0:
                raise RuntimeError("Turn 空索引包含非零计数或 ordinal")
            return
        record = self._files.read_turn_header(
            session_id,
            index.latest_turn_id,
            required=True,
        )
        self.validate_record_anchor(
            session_id,
            record.summary.turn_id,
            timeline_start=record.timeline_start,
            timeline_end=record.timeline_end,
            expected_ordinal=record.summary.ordinal,
            committed_size=index.timeline_size,
        )

    def validate_record_anchor(
        self,
        session_id: str,
        turn_id: str,
        *,
        timeline_start: int,
        timeline_end: int,
        expected_ordinal: int,
        committed_size: int,
    ) -> None:
        path = self._files.timeline_path(session_id)
        if not 0 <= timeline_start < timeline_end <= committed_size:
            raise RuntimeError(
                "Turn record timeline offset 越界: "
                f"turn_id={turn_id}, start={timeline_start}, "
                f"end={timeline_end}, size={committed_size}"
            )
        with path.open("rb") as stream:
            stream.seek(timeline_start)
            line = stream.read(timeline_end - timeline_start)
        if not line.endswith(b"\n"):
            raise RuntimeError(f"Turn record timeline anchor 不完整: {turn_id}")
        entry = TimelineEntry.model_validate_json(line)
        if entry.turn_id != turn_id or entry.ordinal != expected_ordinal:
            raise RuntimeError(
                "Turn record timeline anchor 身份不一致: "
                f"turn_id={turn_id}, entry={entry.model_dump()}"
            )

    def find_latest_visible_turn_id(
        self,
        session_id: str,
        index: TurnIndex,
    ) -> str | None:
        end_offset = index.timeline_size
        while end_offset > 0:
            page = self.read_visible_entries_before(
                session_id,
                end_offset=end_offset,
                limit=1,
            )
            if page.entries:
                return page.entries[0].turn_id
            if page.next_anchor_turn_id is None:
                return None
            anchor = self._files.read_turn_header(
                session_id,
                page.next_anchor_turn_id,
                required=True,
            )
            end_offset = anchor.timeline_start
        return None

    def read_visible_entries_before(
        self,
        session_id: str,
        *,
        end_offset: int,
        limit: int,
    ) -> TimelineScanPage:
        path = self._files.timeline_path(session_id)
        if end_offset < 0 or end_offset > path.stat().st_size:
            raise ValueError(f"Turn timeline offset 无效: {end_offset}")
        read_start = max(0, end_offset - _MAX_TIMELINE_PAGE_SCAN_BYTES)
        with path.open("rb") as stream:
            stream.seek(read_start)
            payload = stream.read(end_offset - read_start)
        if read_start > 0:
            first_newline = payload.find(b"\n")
            if first_newline < 0:
                raise RuntimeError("Turn timeline 单行超过分页扫描字节上限")
            read_start += first_newline + 1
            payload = payload[first_newline + 1 :]
        positioned_lines: list[tuple[int, bytes]] = []
        position = read_start
        for line in payload.splitlines(keepends=True):
            if line.strip():
                positioned_lines.append((position, line))
            position += len(line)

        entries: list[TimelineEntry] = []
        last_scanned: TimelineEntry | None = None
        last_scanned_start = end_offset
        for scanned_count, (line_start, line) in enumerate(
            reversed(positioned_lines),
            start=1,
        ):
            entry = TimelineEntry.model_validate_json(line)
            record = self._files.read_turn_header(
                session_id,
                entry.turn_id,
                required=True,
            )
            self.validate_record_anchor(
                session_id,
                record.summary.turn_id,
                timeline_start=record.timeline_start,
                timeline_end=record.timeline_end,
                expected_ordinal=record.summary.ordinal,
                committed_size=end_offset,
            )
            last_scanned = entry
            last_scanned_start = line_start
            if record.visible:
                entries.append(entry)
            if len(entries) >= limit or scanned_count >= _MAX_TIMELINE_PAGE_SCAN_ENTRIES:
                break
        has_more = last_scanned is not None and last_scanned_start > 0
        return TimelineScanPage(
            entries=entries,
            next_anchor_turn_id=(last_scanned.turn_id if has_more else None),
            has_more=has_more,
        )

    def find_or_append_entry(
        self,
        session_id: str,
        *,
        turn_id: str,
        ordinal: int,
        indexed_size: int,
    ) -> tuple[int, int]:
        path = self._files.timeline_path(session_id)
        matches: list[tuple[int, int]] = []
        with path.open("rb") as stream:
            stream.seek(indexed_size)
            while line := stream.readline():
                start = stream.tell() - len(line)
                if not line.endswith(b"\n"):
                    raise RuntimeError(
                        "Turn timeline 未索引尾行不完整: "
                        f"session_id={session_id}, offset={start}"
                    )
                entry = TimelineEntry.model_validate_json(line)
                if entry.turn_id == turn_id:
                    if entry.ordinal != ordinal:
                        raise RuntimeError(
                            "Turn 未索引 timeline ordinal 冲突: "
                            f"turn_id={turn_id}, timeline={entry.ordinal}, "
                            f"requested={ordinal}"
                        )
                    matches.append((start, stream.tell()))
        if len(matches) > 1:
            raise RuntimeError(
                "Turn 未索引 timeline 包含重复身份: "
                f"session_id={session_id}, turn_id={turn_id}"
            )
        if matches:
            return matches[0]
        return self._append_entry(
            session_id,
            TimelineEntry(turn_id=turn_id, ordinal=ordinal),
        )

    def _append_entry(
        self,
        session_id: str,
        entry: TimelineEntry,
    ) -> tuple[int, int]:
        path = self._files.timeline_path(session_id)
        line = entry.model_dump_json().encode("utf-8") + b"\n"
        with path.open("ab") as stream:
            start = stream.tell()
            stream.write(line)
            self._files.flush_stream(stream)
            return start, stream.tell()
