from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.abstractions.session_context import (
    SessionContextMessageSourceProtocol,
    SessionLookupProtocol,
)
from app.schemas.public_v2.session_context import (
    SessionContextGrepResultDTO,
    SessionContextLineDTO,
    SessionContextMatchDTO,
    SessionContextReadResultDTO,
    SessionContextSnapshotMetadataDTO,
    SessionRecentAssistantTextMessageDTO,
    SessionRecentTextMessagesDTO,
    SessionRecentTextMessageDTO,
    SessionRecentUserTextMessageDTO,
)


@dataclass(frozen=True, slots=True)
class _SessionContextSnapshot:
    session_id: str
    snapshot_id: str
    content_sha256: str
    generated_at: str
    records: list[dict[str, object]]
    lines: list[str]
    byte_count: int
    raw_message_count: int
    compacted: bool
    compaction_cutoff: int | None
    history_file_path: str | None


class SessionContextQueryService:
    """查询会话当前有效模型上下文，并提供稳定的快照一致性语义。"""

    def __init__(
        self,
        *,
        message_source: SessionContextMessageSourceProtocol,
        session_lookup: SessionLookupProtocol,
    ) -> None:
        self._message_source = message_source
        self._session_lookup = session_lookup

    async def recent_text(
        self,
        session_id: str,
        *,
        rounds: int = 5,
    ) -> SessionRecentTextMessagesDTO:
        if rounds < 1 or rounds > 50:
            raise ValueError("rounds 必须在 1-50 之间")
        snapshot = await self._load_snapshot(session_id)
        messages = self._select_recent_user_rounds_with_assistant_text(
            snapshot.records,
            rounds,
        )
        return SessionRecentTextMessagesDTO(
            session_id=snapshot.session_id,
            rounds=rounds,
            user_message_count=sum(
                1 for message in messages if message.role == "user"
            ),
            context_snapshot=self._snapshot_metadata(snapshot),
            messages=messages,
        )

    async def grep(
        self,
        session_id: str,
        *,
        pattern: str,
        case_sensitive: bool = False,
        max_matches: int = 20,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextGrepResultDTO:
        if not pattern:
            raise ValueError("pattern 不能为空")
        if max_matches < 1 or max_matches > 200:
            raise ValueError("max_matches 必须在 1-200 之间")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            expression = re.compile(pattern, flags)
        except re.error as error:
            raise ValueError(f"pattern 不是有效正则表达式: {error}") from error

        snapshot = await self._load_snapshot(session_id)
        matches: list[SessionContextMatchDTO] = []
        total_matching_lines = 0
        for index, line in enumerate(snapshot.lines, start=1):
            match = expression.search(line)
            if match is None:
                continue
            total_matching_lines += 1
            if len(matches) >= max_matches:
                continue
            preview_start = max(0, match.start() - 180)
            preview_end = min(len(line), match.end() + 180)
            matches.append(
                SessionContextMatchDTO(
                    line_number=index,
                    match_start=match.start() + 1,
                    match_end=match.end(),
                    preview=line[preview_start:preview_end],
                    preview_truncated_left=preview_start > 0,
                    preview_truncated_right=preview_end < len(line),
                    line_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
                )
            )

        return SessionContextGrepResultDTO(
            session_id=snapshot.session_id,
            pattern=pattern,
            case_sensitive=case_sensitive,
            context_snapshot=self._snapshot_metadata(
                snapshot,
                expected_snapshot_id,
            ),
            total_matching_lines=total_matching_lines,
            returned_match_count=len(matches),
            matches_truncated=total_matching_lines > len(matches),
            matches=matches,
        )

    async def read_lines(
        self,
        session_id: str,
        *,
        line_start: int = 1,
        line_count: int = 20,
        max_chars_per_line: int = 4000,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextReadResultDTO:
        if line_start < 1:
            raise ValueError("line_start 必须大于等于 1")
        if line_count < 1 or line_count > 200:
            raise ValueError("line_count 必须在 1-200 之间")
        if max_chars_per_line < 200 or max_chars_per_line > 20_000:
            raise ValueError("max_chars_per_line 必须在 200-20000 之间")

        snapshot = await self._load_snapshot(session_id)
        start_index = min(line_start - 1, len(snapshot.lines))
        selected = snapshot.lines[start_index:start_index + line_count]
        lines: list[SessionContextLineDTO] = []
        for offset, line in enumerate(selected):
            clipped = line[:max_chars_per_line]
            lines.append(
                SessionContextLineDTO(
                    line_number=start_index + offset + 1,
                    text=clipped,
                    original_chars=len(line),
                    truncated=len(clipped) < len(line),
                    line_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
                )
            )
        line_end = start_index + len(selected)
        return SessionContextReadResultDTO(
            session_id=snapshot.session_id,
            context_snapshot=self._snapshot_metadata(
                snapshot,
                expected_snapshot_id,
            ),
            line_start=line_start,
            line_end=line_end,
            has_more=line_end < len(snapshot.lines),
            next_line_start=(
                line_end + 1 if line_end < len(snapshot.lines) else None
            ),
            lines=lines,
        )

    async def _load_snapshot(self, session_id: str) -> _SessionContextSnapshot:
        target_session_id = session_id.strip()
        if not target_session_id:
            raise ValueError("session_id 不能为空")

        await self._session_lookup.get(target_session_id)
        state = await self._message_source.get_agent_context_state(target_session_id)
        lines = [
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in state["records"]
        ]
        encoded = "\n".join(lines).encode("utf-8")
        content_sha256 = hashlib.sha256(encoded).hexdigest()
        checkpoint_id = state["checkpoint_id"].strip()
        return _SessionContextSnapshot(
            session_id=target_session_id,
            snapshot_id=checkpoint_id or f"content:{content_sha256}",
            content_sha256=content_sha256,
            generated_at=datetime.now(UTC).isoformat(),
            records=state["records"],
            lines=lines,
            byte_count=len(encoded),
            raw_message_count=state["raw_message_count"],
            compacted=state["compacted"],
            compaction_cutoff=state["compaction_cutoff"],
            history_file_path=state["history_file_path"],
        )

    @staticmethod
    def _snapshot_metadata(
        snapshot: _SessionContextSnapshot,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextSnapshotMetadataDTO:
        if expected_snapshot_id is None:
            consistency = "not_checked"
            warning = None
        elif expected_snapshot_id == snapshot.snapshot_id:
            consistency = "matched"
            warning = None
        else:
            consistency = "changed"
            warning = (
                "目标 session 上下文已变化；当前结果来自新快照，"
                "不要与旧 grep/read 结果按行号拼接。"
            )
        return SessionContextSnapshotMetadataDTO(
            snapshot_id=snapshot.snapshot_id,
            content_sha256=snapshot.content_sha256,
            generated_at=snapshot.generated_at,
            line_count=len(snapshot.lines),
            raw_message_count=snapshot.raw_message_count,
            byte_count=snapshot.byte_count,
            compacted=snapshot.compacted,
            compaction_cutoff=snapshot.compaction_cutoff,
            history_file_path=snapshot.history_file_path,
            expected_snapshot_id=expected_snapshot_id,
            consistency=consistency,
            warning=warning,
        )

    @staticmethod
    def _content_text(
        content: object,
        *,
        assistant_text_blocks_only: bool,
    ) -> str:
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if not assistant_text_blocks_only:
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if assistant_text_blocks_only and item_type != "text":
                continue
            if not assistant_text_blocks_only and item_type not in {
                None,
                "text",
                "input_text",
            }:
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()

    @classmethod
    def _is_user_record(cls, record: dict[str, object]) -> bool:
        role = record.get("role")
        message_type = record.get("type")
        text = cls._content_text(
            record.get("content"),
            assistant_text_blocks_only=False,
        )
        return (
            (role == "user" or message_type == "human")
            and bool(text)
            and not text.strip().startswith("<system_reminder>")
        )

    @staticmethod
    def _is_assistant_record(record: dict[str, object]) -> bool:
        return record.get("role") == "assistant" or record.get("type") == "ai"

    @classmethod
    def _select_recent_user_rounds_with_assistant_text(
        cls,
        records: list[dict[str, object]],
        rounds: int,
    ) -> list[SessionRecentTextMessageDTO]:
        user_indexes = [
            index
            for index, record in enumerate(records)
            if cls._is_user_record(record)
        ]
        if not user_indexes:
            return []

        start_index = (
            user_indexes[-rounds]
            if len(user_indexes) > rounds
            else user_indexes[0]
        )
        selected: list[SessionRecentTextMessageDTO] = []
        for record in records[start_index:]:
            if cls._is_user_record(record):
                selected.append(
                    SessionRecentUserTextMessageDTO(
                        text=cls._content_text(
                            record.get("content"),
                            assistant_text_blocks_only=False,
                        ),
                    )
                )
                continue
            if not cls._is_assistant_record(record) or record.get("tool_calls"):
                continue
            text = cls._content_text(
                record.get("content"),
                assistant_text_blocks_only=True,
            )
            if text:
                selected.append(
                    SessionRecentAssistantTextMessageDTO(
                        text=text,
                    )
                )
        return selected
