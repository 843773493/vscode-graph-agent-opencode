"""固定来源 bf0f2e1d 的 Trace v1 提交合同转为现行 SQLite schema 3。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, closing
from pathlib import Path

from app.core.sqlite_state import SQLiteStateDatabase
from app.schemas.event import BaseEvent, Event

from ..trace_database import _SCHEMA_VERSION, _migrations, job_threads
from ..trace_page import parse_trace_record

LEGACY_FILES = ("events.jsonl", "messages.jsonl", "turn-events.index.json", "turn-events.index.jsonl")
_MESSAGE_TYPES = frozenset({
    "job_created", "job_started", "job_completed", "job_cancelled", "job_failed", "message_created",
    "status_change", "text_start", "text_end", "tool_call_start", "tool_call_end", "error", "session_interrupted",
})


def iter_v1_trace(directory: Path, *, session_id: str, main_thread_id: str) -> Iterator[tuple[str, Event | None]]:
    """只读核验已停止且提交完整的旧 writer 输出；不修复或丢弃未提交尾部。"""
    manifest = json.loads((directory / "turn-events.index.json").read_bytes())
    if manifest.get("schema_version") != 1 or manifest.get("has_unindexed_prefix") is not False:
        raise ValueError(f"Trace JSONL 只支持完整 v1 提交清单，保留原件: {directory}")
    trace = directory / "events.jsonl"
    messages = directory / "messages.jsonl"
    index = directory / "turn-events.index.jsonl"
    if index.stat().st_size != manifest["committed_index_offset"]:
        raise ValueError(f"Trace v1 index 有未提交尾部或已截断，保留原件: {index}")
    message_size = messages.stat().st_size if messages.exists() else 0
    if message_size != manifest["committed_message_offset"]:
        raise ValueError(f"Trace v1 messages 提交边界不一致，保留原件: {messages}")
    message_offset = committed_trace = 0
    owners: dict[str, str] = {}
    jobs: set[str] = set()
    with ExitStack() as stack:
        events = stack.enter_context(trace.open("rb"))
        entries = stack.enter_context(index.open("rb"))
        semantic = stack.enter_context(messages.open("rb")) if messages.exists() else None
        while raw := events.readline():
            end = events.tell()
            if not raw.endswith(b"\n"):
                raise ValueError(f"Trace v1 存在不完整事件，保留原件: {trace}, offset={end - len(raw)}")
            value = json.loads(raw)
            event_type = value["type"]
            payload = value["payload"]
            base = BaseEvent.model_validate(value)
            # 用户明确允许丢弃无法补齐事实的实验数据；不能伪造历史队列水位或 Goal revision。
            discard = event_type in {"job_started", "goal_cleared"} and set(payload) == {"session_id", "thread_id"}
            event = None if discard else parse_trace_record(session_id, end - len(raw), end, raw).event
            if event_type in {"goal_updated", "goal_cleared"} and (
                payload["session_id"] != session_id or payload["thread_id"] != main_thread_id
            ):
                raise ValueError("Trace v1 Goal 不属于原 Session main Thread")
            if event_type not in {"goal_updated", "goal_cleared"} and not discard:
                jobs.add(base.job_id)
            if event_type in {"job_created", "message_created"} and (
                payload["session_id"] != session_id or owners.setdefault(base.job_id, payload["thread_id"]) != payload["thread_id"]
            ):
                raise ValueError("Trace v1 Job 归属不一致，保留原件")
            if event_type in _MESSAGE_TYPES:
                entry = json.loads(entries.readline())
                if semantic is None or semantic.readline() != raw or any((
                    entry.get("schema_version") != 1,
                    entry.get("event_id") != base.event_id,
                    entry.get("event_type") != event_type,
                    entry.get("job_id") != base.job_id,
                    entry.get("trace_start") != end - len(raw),
                    entry.get("trace_end") != end,
                    entry.get("message_start") != message_offset,
                    entry.get("message_end") != message_offset + len(raw),
                )):
                    raise ValueError(f"Trace v1 派生记录与权威事件不一致: event_id={base.event_id}")
                message_offset += len(raw)
                committed_trace = end
            yield event_type, event
        if (entries.read(1) or semantic is not None and semantic.read(1)
                or message_offset != message_size or committed_trace != manifest["committed_trace_offset"]):
            raise ValueError(f"Trace v1 提交清单与正文不一致，保留原件: {directory}")
        if jobs - owners.keys():
            raise ValueError("Trace v1 缺少可保留的 Job owner，保留原件")


def convert_trace_v1(directory: Path, destination: Path, *, session_id: str, main_thread_id: str) -> dict[str, int]:
    """可转换事件写入现行 envelope，丢弃无法补齐事实的旧事件并明确返回计数。"""
    if destination.exists():
        raise FileExistsError(destination)
    discarded: dict[str, int] = {}
    try:
        with (SQLiteStateDatabase(path=destination, schema_version=_SCHEMA_VERSION, migrations=_migrations()) as database,
              closing(database.connection()) as connection):
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                offset = 0
                for event_type, event in iter_v1_trace(directory, session_id=session_id, main_thread_id=main_thread_id):
                    if event is None:
                        discarded[event_type] = discarded.get(event_type, 0) + 1
                        continue
                    raw = event.model_dump_json().encode("utf-8") + b"\n"
                    connection.execute("INSERT INTO trace_events(start,raw) VALUES (?,?)", (offset, raw.decode("utf-8")))
                    offset += len(raw)
                if connection.execute("SELECT 1 FROM trace_events GROUP BY event_id HAVING count(*)>1 LIMIT 1").fetchone():
                    raise ValueError("Trace v1 event_id 重复，拒绝转换")
                # 当前恢复与查询沿同一正文派生的 Job owner 索引校验，不从导航关系猜归属。
                for job, in connection.execute("SELECT DISTINCT job_id FROM trace_events WHERE event_type NOT IN ('goal_updated','goal_cleared')"):
                    job_threads(connection, session_id, {job})
                if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                    raise RuntimeError("Trace 转换后 SQLite 完整性校验失败")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return discarded
