from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
import zlib
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.abstractions.turn_terminal_status import (
    ToolResultCompletion,
    TurnTerminalStatusWriter,
)
from app.core.atomic_fs import atomic_write_bytes, native_io_path
from app.core.bounded_json import bound_json_value
from app.core.identifier import create_prefixed_id, validate_thread_id
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.shielded_cleanup import run_cleanup_shielded
from app.domain.message_stream.errors import MessageStreamError
from app.domain.message_stream.projection import (
    apply_event,
    backfill_lifecycle_metadata,
    validate_event_payload,
)

logger = logging.getLogger(__name__)

TERMINAL_STREAM_STATUSES = frozenset({"completed", "interrupted", "failed"})
MESSAGE_STREAM_EVENT_MAX_PAYLOAD_BYTES = 256 * 1024
MESSAGE_STREAM_MAX_BYTES = 64 * 1024 * 1024
MESSAGE_STREAM_RETAINED_BYTES = 8 * 1024 * 1024
MESSAGE_STREAM_TERMINAL_CACHE_MAX_ENTRIES = 16
MESSAGE_STREAM_TERMINAL_CACHE_MAX_BYTES = 16 * 1024 * 1024
# 会话级索引锁按 session_id 哈希分片到固定数量的锁对象：锁数量有界（不随历史
# 会话总数增长），且同一 session_id 恒映射到同一把锁，互斥语义不变。
MESSAGE_STREAM_INDEX_LOCK_SHARDS = 64
# 消息事件日志逐事件持久化；状态快照只是恢复加速索引，不需要逐 token 写入。
MESSAGE_STREAM_SNAPSHOT_INTERVAL_EVENTS = 32
RECOVERED_TOOL_RESULT_UNKNOWN = "工作区后端重启，工具执行结果未知；未确认执行完成，不会自动重放。"
INTERRUPTING_ALLOWED_EVENT_TYPES = frozenset(
    {
        "block.completed",
        "model.completed",
        "model.failed",
        "tool_call.completed",
        "tool.completed",
        "stream.interrupted",
        "stream.failed",
        "stream.snapshot",
    }
)



class MessageStreamNotFoundError(MessageStreamError):
    """请求的消息流不存在。"""


class MessageStreamCursorGoneError(MessageStreamError):
    def __init__(self, *, turn_stream_id: str, after_seq: int, first_seq: int) -> None:
        self.turn_stream_id = turn_stream_id
        self.after_seq = after_seq
        self.first_seq = first_seq
        super().__init__(
            "消息流游标早于可恢复事件范围: "
            "turn_stream_id="
            f"{turn_stream_id} after_seq={after_seq} first_seq={first_seq}"
        )


class MessageStreamCursorAheadError(MessageStreamError):
    """游标越过服务端已持久化的最高事件序号，属于不可能的客户端输入。

    与「游标已被保留窗口裁掉」的 ``CursorGone`` 是相反方向的失效：``CursorGone``
    说明客户端落后于保留窗口，客户端可用快照重新对齐；这里则说明客户端持有的序号
    从未被服务端产生过（坏游标或越界代理），不存在可对齐的续播点。越过最高水位曾
    在续播链路上静默返回空页，让 SSE 永久阻塞在订阅队列上。
    """

    def __init__(
        self, *, turn_stream_id: str, after_seq: int, high_water_seq: int
    ) -> None:
        self.turn_stream_id = turn_stream_id
        self.after_seq = after_seq
        self.high_water_seq = high_water_seq
        super().__init__(
            "消息流游标越过已持久化的最高事件序号: "
            "turn_stream_id="
            f"{turn_stream_id} after_seq={after_seq} high_water_seq={high_water_seq}"
        )


class MessageStreamTerminalError(MessageStreamError):
    """终态消息流拒绝新的业务事件。"""


@dataclass(frozen=True, slots=True)
class MessageStreamRecord:
    event: dict[str, Any]
    checkpoint: dict[str, Any]


class MessageStreamSubscription:
    def __init__(self, *, turn_stream_id: str, maxsize: int = 256) -> None:
        self.turn_stream_id = turn_stream_id
        self.queue: asyncio.Queue[MessageStreamRecord] = asyncio.Queue(maxsize=maxsize)
        self.closed = False

    def offer(self, record: MessageStreamRecord) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.closed = True
            return False
        return True

    async def get(self) -> MessageStreamRecord:
        if self.closed and self.queue.empty():
            raise MessageStreamError(
                f"消息流订阅已关闭: turn_stream_id={self.turn_stream_id}"
            )
        return await self.queue.get()


class MessageStreamWriter:
    """一个 TurnStream 的串行提交入口。"""

    def __init__(
        self,
        store: MessageStreamStore,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        turn_stream_id: str,
        job_id: str | None = None,
    ) -> None:
        self._store = store
        self.session_id = session_id
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.turn_stream_id = turn_stream_id
        self.job_id = job_id

    async def commit(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        model_call_id: str | None = None,
        block_id: str | None = None,
        tool_execution_id: str | None = None,
        tool_call_id: str | None = None,
        tool_invocation_id: str | None = None,
        tool_attempt_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._store.commit(
            self.turn_stream_id,
            event_type,
            dict(payload or {}),
            model_call_id=model_call_id,
            block_id=block_id,
            tool_execution_id=tool_execution_id,
            tool_call_id=tool_call_id,
            tool_invocation_id=tool_invocation_id,
            tool_attempt_id=tool_attempt_id,
            job_id=self.job_id,
            event_id=event_id,
        )

    async def snapshot(self) -> dict[str, Any]:
        """返回不消耗 event_seq 的 snapshot 控制帧。"""
        return await self._store.snapshot_event(self.turn_stream_id)

    async def close_completed(self) -> dict[str, Any]:
        return await self.commit("stream.completed", {"status": "completed"})

    async def close_interrupted(self, interrupt_request_id: str) -> dict[str, Any]:
        return await self.commit(
            "stream.interrupted",
            {"interrupt_request_id": interrupt_request_id, "status": "interrupted"},
        )

    async def close_failed(
        self,
        *,
        code: str,
        message: str,
        after_interrupt_requested: bool = False,
        resumable: bool = False,
    ) -> dict[str, Any]:
        return await self.commit(
            "stream.failed",
            {
                "code": code,
                "message": message,
                "after_interrupt_requested": after_interrupt_requested,
                "resumable": resumable,
            },
        )


class MessageStreamStore:
    """工作区内的消息流事件日志、checkpoint 和临时订阅。"""

    def __init__(
        self,
        *,
        path_resolver: SessionCatalogPathResolver,
        workspace_id: str | None = None,
        subscriber_queue_size: int = 256,
    ) -> None:
        self._path_resolver = path_resolver
        self._workspace_id = workspace_id
        self._subscriber_queue_size = subscriber_queue_size
        self._locks: dict[str, asyncio.Lock] = {}
        self._index_locks: tuple[asyncio.Lock, ...] = tuple(
            asyncio.Lock() for _ in range(MESSAGE_STREAM_INDEX_LOCK_SHARDS)
        )
        self._snapshot_locks: dict[str, asyncio.Lock] = {}
        self._snapshot_tasks: dict[str, asyncio.Task[None]] = {}
        self._snapshot_file_lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}
        # 缓存状态按对象整体替换；记录对象身份后，终态大小只需在状态替换时序列化一次。
        self._terminal_state_sizes: dict[str, tuple[dict[str, Any], int]] = {}
        self._subscriptions: dict[str, set[MessageStreamSubscription]] = {}
        self._event_ids: dict[str, dict[str, dict[str, Any]]] = {}
        self._event_ids_loaded: set[str] = set()

    @staticmethod
    def _cached_state_size_bytes(state: Mapping[str, Any]) -> int:
        return len(
            json.dumps(
                state,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def _evict_stream_cache(self, turn_stream_id: str) -> None:
        self._states.pop(turn_stream_id, None)
        self._terminal_state_sizes.pop(turn_stream_id, None)
        self._event_ids.pop(turn_stream_id, None)
        self._event_ids_loaded.discard(turn_stream_id)
        # 被淘汰的流不再需要串行锁；锁字典必须随缓存一起收敛，否则每处理一个
        # 历史 Turn 都会永久留下两个 asyncio.Lock，进程内存随会话总量无界增长。
        # 锁被持有时说明仍有协程在提交或等待同一 Turn，此时绝不能移除，否则
        # 等待者与新锁会并行进入同一临界区，重新破坏 event_seq 串行性。
        for locks in (self._locks, self._snapshot_locks):
            lock = locks.get(turn_stream_id)
            if lock is not None and not lock.locked():
                locks.pop(turn_stream_id, None)

    def _touch_cached_state(
        self,
        turn_stream_id: str,
        state: dict[str, Any],
    ) -> None:
        # dict 的插入顺序就是进程内 LRU。业务事实始终在 JSONL/state snapshot，
        # 这里的终态状态只用于加速最近访问，不能随着历史会话无限增长。
        self._states.pop(turn_stream_id, None)
        self._states[turn_stream_id] = state
        if str(state.get("stream_status")) in TERMINAL_STREAM_STATUSES:
            cached_size = self._terminal_state_sizes.get(turn_stream_id)
            if cached_size is None or cached_size[0] is not state:
                self._terminal_state_sizes[turn_stream_id] = (
                    state,
                    self._cached_state_size_bytes(state),
                )
        else:
            self._terminal_state_sizes.pop(turn_stream_id, None)
        self._prune_terminal_cache(protected_stream_id=turn_stream_id)

    def _prune_terminal_cache(self, *, protected_stream_id: str | None) -> None:
        terminal_entries: list[tuple[str, int]] = []
        for stream_id, state in self._states.items():
            if str(state.get("stream_status")) not in TERMINAL_STREAM_STATUSES:
                continue
            cached_size = self._terminal_state_sizes.get(stream_id)
            if cached_size is None or cached_size[0] is not state:
                raise RuntimeError(
                    "消息流终态缓存大小与状态对象不一致: "
                    f"turn_stream_id={stream_id}"
                )
            terminal_entries.append((stream_id, cached_size[1]))
        total_bytes = sum(size for _, size in terminal_entries)
        while (
            len(terminal_entries) > MESSAGE_STREAM_TERMINAL_CACHE_MAX_ENTRIES
            or total_bytes > MESSAGE_STREAM_TERMINAL_CACHE_MAX_BYTES
        ):
            candidate_index = next(
                (
                    index
                    for index, (stream_id, _) in enumerate(terminal_entries)
                    if stream_id != protected_stream_id
                    and not self._subscriptions.get(stream_id)
                ),
                None,
            )
            if candidate_index is None:
                # 当前请求使用中的单个超大终态允许暂留；下一条流进入时会把它淘汰。
                break
            stream_id, size = terminal_entries.pop(candidate_index)
            total_bytes -= size
            self._evict_stream_cache(stream_id)


    def _lock_for(self, turn_stream_id: str) -> asyncio.Lock:
        return self._locks.setdefault(turn_stream_id, asyncio.Lock())

    def _index_lock_for(self, session_id: str) -> asyncio.Lock:
        # 分片锁池：同一 session_id 恒得同一把锁（保住互斥），数量恒为分片数
        # （有界）。用 crc32 而非 hash() 是为了跨进程确定，不引入 hash 随机化。
        shard = zlib.crc32(session_id.encode("utf-8")) % len(self._index_locks)
        return self._index_locks[shard]

    def _snapshot_lock_for(self, turn_stream_id: str) -> asyncio.Lock:
        return self._snapshot_locks.setdefault(turn_stream_id, asyncio.Lock())

    async def _persist_state_snapshot(
        self,
        session_id: str,
        turn_stream_id: str,
        state: Mapping[str, Any],
    ) -> None:
        # 同一 Turn 的快照必须按提交顺序写入，避免较旧快照覆盖较新快照。
        async with self._snapshot_lock_for(turn_stream_id):
            await asyncio.to_thread(
                self._write_state_snapshot,
                session_id,
                turn_stream_id,
                state,
            )

    def _on_snapshot_task_done(
        self,
        turn_stream_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._snapshot_tasks.get(turn_stream_id) is task:
            self._snapshot_tasks.pop(turn_stream_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            # 事件日志已经是恢复事实；快照失败不能静默吞掉，后续恢复会重放
            # 快照尾部事件，同时把具体异常保留在后端日志中。
            logger.exception(
                "消息流状态快照异步写入失败: turn_stream_id=%s",
                turn_stream_id,
            )

    def _schedule_state_snapshot(
        self,
        session_id: str,
        turn_stream_id: str,
        state: Mapping[str, Any],
    ) -> None:
        task = asyncio.create_task(
            self._persist_state_snapshot(
                session_id,
                turn_stream_id,
                # next_state 后续只会被整体替换，不会原地修改，可以避免在事件
                # 循环中再次复制完整的 blocks/tool_executions。
                state,
            )
        )
        self._snapshot_tasks[turn_stream_id] = task
        task.add_done_callback(
            lambda finished: self._on_snapshot_task_done(
                turn_stream_id,
                finished,
            )
        )

    def _stream_dir(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node_for_runtime(session_id)
            / "message_streams"
        )

    def _stream_path(self, session_id: str, turn_stream_id: str) -> Path:
        return self._stream_dir(session_id) / f"{turn_stream_id}.jsonl"

    def _state_path(self, session_id: str, turn_stream_id: str) -> Path:
        return self._stream_dir(session_id) / f"{turn_stream_id}.state.json"

    def _index_path(self, session_id: str) -> Path:
        return self._stream_dir(session_id) / "index.json"

    def _applied_event_ids_path(self, session_id: str, turn_stream_id: str) -> Path:
        return self._stream_dir(session_id) / f"{turn_stream_id}.applied-event-ids.json"

    def _read_applied_event_ids_file(
        self,
        session_id: str,
        turn_stream_id: str,
        *,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, dict[str, Any]]:
        path = native_io_path(self._applied_event_ids_path(session_id, turn_stream_id))
        context = f"session_id={session_id} turn_stream_id={turn_stream_id} artifact=applied_event_ids"
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            logger.exception("消息流已应用event_id读取失败: path=%s", path)
            raise MessageStreamError(
                f"消息流已应用 event_id 记录损坏: {context}"
            ) from error
        if not isinstance(raw, dict):
            logger.error("消息流已应用event_id记录非法: path=%s record=%r", path, raw)
            raise MessageStreamError(
                f"消息流已应用 event_id 记录必须是对象: {context}"
            )
        entries: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            try:
                if not isinstance(value, dict):
                    raise MessageStreamError(f"消息流已应用 event_id 记录项非法: {context}")
                event_id, *_ = self._validate_event_record(
                    value,
                    context=context,
                    expected_session_id=session_id,
                    expected_thread_id=thread_id,
                    expected_turn_id=turn_id,
                    expected_turn_stream_id=turn_stream_id,
                )
                if key != event_id:
                    raise MessageStreamError(f"消息流已应用 event_id 与索引键不一致: {context}")
            except MessageStreamError:
                logger.exception("消息流已应用event_id记录校验失败: path=%s event_id=%r record=%r", path, key, value)
                raise
            entries[key] = value
        return entries

    def _write_applied_event_ids_file(
        self,
        session_id: str,
        turn_stream_id: str,
        entries: Mapping[str, Mapping[str, Any]],
    ) -> None:
        path = self._applied_event_ids_path(session_id, turn_stream_id)
        with self._snapshot_file_lock:
            atomic_write_bytes(path, json.dumps(
                dict(entries), ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))

    def _event_ids_from_records(
        self,
        session_id: str,
        turn_stream_id: str,
        records: Sequence[MessageStreamRecord],
        *,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, dict[str, Any]]:
        """构成唯一的 event_id 幂等查找表。

        保留窗口裁剪会物理移除旧事件记录；显式 event_id 的已应用事实由
        ``*.applied-event-ids.json`` 单独持久化，必须与保留日志合并，否则旧
        id 会被遗忘并静默重复应用副作用。保留记录是权威正文，优先采用。
        """
        event_ids = {
            str(record.event["event_id"]): record.event for record in records
        }
        for event_id, event in self._read_applied_event_ids_file(
            session_id,
            turn_stream_id,
            thread_id=thread_id,
            turn_id=turn_id,
        ).items():
            event_ids.setdefault(event_id, event)
        return event_ids

    def _empty_state(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        turn_stream_id: str,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "turn_stream_id": turn_stream_id,
            "job_id": job_id,
            "workspace_id": self._workspace_id,
            "snapshot_seq": 0,
            "stream_status": "open",
            "agent_loop_status": "running",
            "current_model_call_id": None,
            "current_attempt": 0,
            "blocks": [],
            "tool_calls": [],
            "tool_executions": [],
            "model_calls": [],
            "activities": [],
            "resource_refs": [],
            "active_state": None,
            "interrupt_state": None,
            "failure": None,
            "recovery": None,
            "resumable": True,
        }


    @staticmethod
    def _validate_event_identity_fields(
        payload: Mapping[str, Any],
        identity_fields: Mapping[str, Any],
        *,
        context: str = "",
    ) -> None:
        """校验信封身份与 payload 身份一致，禁止同一事件出现两套归属。"""
        location = f" {context}" if context else ""
        for field_name, envelope_value in identity_fields.items():
            payload_value = payload.get(field_name)
            for value, source in (
                (envelope_value, "信封"),
                (payload_value, "payload"),
            ):
                if value is not None and (
                    not isinstance(value, str) or not value
                ):
                    raise MessageStreamError(
                        "消息流身份字段必须是非空字符串: "
                        f"field={field_name} source={source}{location}"
                    )
            if (
                envelope_value is not None
                and payload_value is not None
                and envelope_value != payload_value
            ):
                raise MessageStreamError(
                    "消息流信封与 payload 身份不一致: "
                    f"field={field_name}{location}"
                )

    @staticmethod
    def _validate_event_record(
        event: Mapping[str, Any],
        *,
        context: str,
        expected_session_id: str | None = None,
        expected_turn_id: str | None = None,
        expected_thread_id: str | None = None,
        expected_turn_stream_id: str | None = None,
    ) -> tuple[str, str, str, int, str]:
        event_id = event.get("event_id")
        session_id = event.get("session_id")
        thread_id = event.get("thread_id")
        turn_id = event.get("turn_id")
        turn_stream_id = event.get("turn_stream_id")
        event_seq = event.get("event_seq")
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(event_id, str) or not event_id:
            raise MessageStreamError(f"消息流事件缺少 event_id: {context}")
        if not isinstance(session_id, str) or not session_id:
            raise MessageStreamError(f"消息流事件缺少 session_id: {context}")
        if not isinstance(thread_id, str) or not thread_id:
            raise MessageStreamError(f"消息流事件缺少 thread_id: {context}")
        if not isinstance(turn_id, str) or not turn_id:
            raise MessageStreamError(f"消息流事件缺少 turn_id: {context}")
        if not isinstance(turn_stream_id, str) or not turn_stream_id:
            raise MessageStreamError(f"消息流事件缺少 turn_stream_id: {context}")
        if (
            isinstance(event_seq, bool)
            or not isinstance(event_seq, int)
            or event_seq <= 0
        ):
            raise MessageStreamError(f"消息流事件序号非法: {context}")
        if not isinstance(event_type, str) or not event_type:
            raise MessageStreamError(f"消息流事件缺少 type: {context}")
        if not isinstance(payload, dict):
            raise MessageStreamError(
                f"消息流事件 payload 必须是对象: {context}"
            )
        MessageStreamStore._validate_event_identity_fields(
            payload,
            {
                field_name: event.get(field_name)
                for field_name in (
                    "model_call_id",
                    "block_id",
                    "tool_call_id",
                    "tool_invocation_id",
                    "tool_attempt_id",
                    "tool_execution_id",
                    "workspace_id",
                )
            },
            context=context,
        )
        expected_values = (
            ("session_id", session_id, expected_session_id),
            ("thread_id", thread_id, expected_thread_id),
            ("turn_id", turn_id, expected_turn_id),
            ("turn_stream_id", turn_stream_id, expected_turn_stream_id),
        )
        for field_name, actual, expected in expected_values:
            if expected is not None and actual != expected:
                raise MessageStreamError(
                    "消息流事件关联键不匹配: "
                    f"{context} field={field_name}"
                )
        return event_id, session_id, turn_id, event_seq, turn_stream_id

    def _read_records(
        self,
        path: Path,
        *,
        expected_session_id: str,
        expected_turn_stream_id: str,
    ) -> list[MessageStreamRecord]:
        context = f"session_id={expected_session_id} turn_stream_id={expected_turn_stream_id} artifact=events"
        if not path.is_file():
            return []
        records: list[MessageStreamRecord] = []
        seen_event_ids: set[str] = set()
        expected_turn_id: str | None = None
        expected_thread_id: str | None = None
        previous_event_seq: int | None = None
        valid_offset = 0
        raw = None
        try:
            with path.open("rb") as stream:
                for line in stream:
                    next_offset = valid_offset + len(line)
                    if not line.strip():
                        valid_offset = next_offset
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError as error:
                        if stream.tell() != path.stat().st_size:
                            raise MessageStreamError(
                                f"消息流事件日志中间记录损坏: {context}"
                            ) from error
                        with path.open("r+b") as repair_stream:
                            repair_stream.truncate(valid_offset)
                        logger.warning("已丢弃消息流日志末尾未提交记录: path=%s", path)
                        break
                    if not isinstance(raw, dict):
                        raise MessageStreamError(f"消息流记录必须是对象: {context}")
                    event = raw.get("event")
                    checkpoint = raw.get("checkpoint")
                    if not isinstance(event, dict) or not isinstance(checkpoint, dict):
                        raise MessageStreamError(
                            f"消息流记录缺少 event/checkpoint: {context}"
                        )
                    event_id, _, event_turn_id, event_seq, _ = self._validate_event_record(
                        event,
                        context=context,
                        expected_session_id=expected_session_id,
                        expected_turn_id=expected_turn_id,
                        expected_thread_id=expected_thread_id,
                        expected_turn_stream_id=expected_turn_stream_id,
                    )
                    if expected_thread_id is None:
                        expected_thread_id = event["thread_id"]
                    if expected_turn_id is None:
                        expected_turn_id = event_turn_id
                    if previous_event_seq is not None and event_seq != previous_event_seq + 1:
                        raise MessageStreamError(
                            "消息流事件序号不连续: "
                            f"{context} previous={previous_event_seq} current={event_seq}"
                        )
                    if event_id in seen_event_ids:
                        raise MessageStreamError(
                            f"消息流日志包含重复 event_id: {context}"
                        )
                    records.append(
                        MessageStreamRecord(
                            event=event,
                            checkpoint=checkpoint,
                        )
                    )
                    seen_event_ids.add(event_id)
                    previous_event_seq = event_seq
                    valid_offset = next_offset
        except MessageStreamError:
            logger.exception("消息流事件日志校验失败: path=%s record=%r", path, raw)
            raise
        except (OSError, UnicodeDecodeError) as error:
            logger.exception("消息流事件日志读取失败: path=%s", path)
            raise MessageStreamError(
                f"消息流事件日志读取失败: {context} error_type={type(error).__name__} errno={getattr(error, 'errno', None)}"
            ) from error
        return records

    def _read_state_snapshot(
        self,
        session_id: str,
        turn_stream_id: str,
    ) -> dict[str, Any] | None:
        path = self._state_path(session_id, turn_stream_id)
        context = f"session_id={session_id} turn_stream_id={turn_stream_id} artifact=state"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            logger.exception("消息流状态快照读取失败: path=%s", path)
            raise MessageStreamError(f"消息流状态快照损坏: {context} error_type={type(error).__name__}") from error
        if not isinstance(raw, dict):
            logger.error("消息流状态快照必须是对象: path=%s record=%r", path, raw)
            raise MessageStreamError(f"消息流状态快照必须是对象: {context}")
        return raw

    @staticmethod
    def _validate_state_snapshot(
        state: Mapping[str, Any],
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        turn_stream_id: str,
        last_event_seq: int,
    ) -> int:
        context = f"session_id={session_id} turn_stream_id={turn_stream_id} artifact=state"
        for field_name, expected in (
            ("session_id", session_id),
            ("thread_id", thread_id),
            ("turn_id", turn_id),
            ("turn_stream_id", turn_stream_id),
        ):
            if state.get(field_name) != expected:
                logger.error("消息流快照关联键不匹配: %s field=%s expected=%r actual=%r", context, field_name, expected, state.get(field_name))
                raise MessageStreamError(
                    "消息流状态快照关联键不匹配: "
                    f"{context} field={field_name}"
                )
        snapshot_seq = state.get("snapshot_seq")
        if (
            isinstance(snapshot_seq, bool)
            or not isinstance(snapshot_seq, int)
            or snapshot_seq < 0
            or snapshot_seq > last_event_seq
        ):
            logger.error("消息流快照序号非法: %s snapshot_seq=%r last_event_seq=%s", context, snapshot_seq, last_event_seq)
            raise MessageStreamError(
                "消息流状态快照序号非法: "
                f"{context} field=snapshot_seq last_event_seq={last_event_seq}"
            )
        return snapshot_seq

    def _write_state_snapshot(
        self,
        session_id: str,
        turn_stream_id: str,
        state: Mapping[str, Any],
    ) -> None:
        with self._snapshot_file_lock:
            path = self._state_path(session_id, turn_stream_id)
            snapshot_seq = int(state.get("snapshot_seq", 0))
            existing = self._read_state_snapshot(session_id, turn_stream_id)
            if existing is not None and int(existing.get("snapshot_seq", 0)) > snapshot_seq:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(".state.tmp")
            with temp_path.open("w", encoding="utf-8") as stream:
                json.dump(dict(state), stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(path)

    def _write_index(self, session_id: str, mapping: Mapping[str, dict[str, str]]) -> None:
        index_path = self._index_path(session_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = index_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump({"format_version": 2, "threads": dict(mapping)}, stream,
                      ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(index_path)

    @staticmethod
    def _encode_event(event: Mapping[str, Any]) -> bytes:
        bounded_event = {
            **dict(event),
            "payload": bound_json_value(
                event.get("payload", {}),
                max_bytes=MESSAGE_STREAM_EVENT_MAX_PAYLOAD_BYTES,
            ),
        }
        return json.dumps(
            {"event": bounded_event, "checkpoint": {}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"

    def _compact_stream_log(
        self,
        session_id: str,
        path: Path,
        turn_stream_id: str,
        *,
        thread_id: str,
        turn_id: str,
    ) -> None:
        """保留最近事件，使用最新 state snapshot 继续完整恢复。"""
        records = self._read_records(
            path, expected_session_id=session_id, expected_turn_stream_id=turn_stream_id,
        )
        retained: list[MessageStreamRecord] = []
        retained_bytes = 0
        for record in reversed(records):
            encoded = self._encode_event(record.event)
            if retained and retained_bytes + len(encoded) > MESSAGE_STREAM_RETAINED_BYTES:
                break
            retained.append(record)
            retained_bytes += len(encoded)
        retained.reverse()
        temp_path = path.with_name(f".{path.name}.compact.tmp")
        with temp_path.open("wb") as stream:
            for record in retained:
                stream.write(self._encode_event(record.event))
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
        self._event_ids[turn_stream_id] = self._event_ids_from_records(
            session_id,
            turn_stream_id,
            retained,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        self._event_ids_loaded.add(turn_stream_id)
        logger.warning(
            "消息流超过保留上限，已保留尾部事件并依赖 snapshot 恢复: "
            "turn_stream_id=%s records_before=%d records_after=%d bytes_after=%d",
            turn_stream_id,
            len(records),
            len(retained),
            retained_bytes,
        )

    def _append_durable_event(
        self,
        session_id: str,
        path: Path,
        encoded: bytes,
        turn_stream_id: str,
        state: Mapping[str, Any],
        event: Mapping[str, Any] | None = None,
    ) -> None:
        """在线程中完成事件追加，确保 fsync 不阻塞消息流事件循环。"""
        # 先验证裁剪后仍需保留的幂等事实，失败不能先追加一个新事件。
        entries = self._read_applied_event_ids_file(
            session_id,
            turn_stream_id,
            thread_id=str(state["thread_id"]),
            turn_id=str(state["turn_id"]),
        ) if event is not None else None
        path.parent.mkdir(parents=True, exist_ok=True)
        needs_compaction = (
            path.is_file()
            and path.stat().st_size + len(encoded) > MESSAGE_STREAM_MAX_BYTES
        )
        with path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            # 事件日志是消息流的崩溃恢复边界；fanout 必须在这里之后。
            os.fsync(stream.fileno())
        if event is not None and entries is not None:
            # 调用方显式 event_id 的已应用事实必须独立于保留窗口持久化，否则
            # 日志被裁剪后无法区分「已应用」与「从未出现」，会静默重放副作用。
            entries[str(event["event_id"])] = dict(event)
            self._write_applied_event_ids_file(
                session_id,
                turn_stream_id,
                entries,
            )
        if needs_compaction:
            # 先让当前事件成为恢复事实，再写入同序号快照，最后才能裁剪旧
            # 事件；否则后台快照尚未执行时，裁剪可能移除 stream.opened。
            self._write_state_snapshot(session_id, turn_stream_id, state)
            self._compact_stream_log(
                session_id, path, turn_stream_id,
                thread_id=str(state["thread_id"]), turn_id=str(state["turn_id"]),
            )

    @staticmethod
    def _should_write_state_snapshot(event_type: str, event_seq: int) -> bool:
        return (
            event_type == "stream.opened"
            or event_type in {
                "stream.completed",
                "stream.interrupted",
                "stream.failed",
            }
            or event_seq % MESSAGE_STREAM_SNAPSHOT_INTERVAL_EVENTS == 0
        )

    def _read_index(self, session_id: str) -> dict[str, dict[str, str]]:
        path = self._index_path(session_id)
        context = f"session_id={session_id} artifact=index"
        if not path.is_file():
            return {}
        raw = None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("format_version") != 2:
                raise MessageStreamError(f"消息流索引格式不兼容，需要显式 thread 索引: {context}")
            threads = raw.get("threads")
            if not isinstance(threads, dict):
                raise MessageStreamError(f"消息流索引 threads 必须是对象: {context}")
            for thread_id, turns in threads.items():
                validate_thread_id(thread_id)
                if not isinstance(turns, dict) or any(
                    not isinstance(turn_id, str) or not turn_id
                    or not isinstance(stream_id, str) or not stream_id
                    for turn_id, stream_id in turns.items()
                ):
                    raise MessageStreamError(f"消息流 thread 索引损坏: {context}")
        except MessageStreamError:
            logger.exception("消息流索引校验失败: path=%s record=%r", path, raw)
            raise
        except (OSError, UnicodeDecodeError, ValueError) as error:
            logger.exception("消息流索引读取失败: path=%s record=%r", path, raw)
            raise MessageStreamError(
                f"消息流索引读取失败: {context} error_type={type(error).__name__}"
            ) from error
        return threads

    def _load_state(self, session_id: str, turn_stream_id: str) -> dict[str, Any]:
        cached = self._states.get(turn_stream_id)
        if cached is not None:
            self._touch_cached_state(turn_stream_id, cached)
            return copy.deepcopy(cached)
        return self._load_state_from_disk(session_id, turn_stream_id)

    def _load_state_from_disk(
        self,
        session_id: str,
        turn_stream_id: str,
    ) -> dict[str, Any]:
        path = self._stream_path(session_id, turn_stream_id)
        records = self._read_records(
            path,
            expected_session_id=session_id,
            expected_turn_stream_id=turn_stream_id,
        )
        if not records:
            raise MessageStreamNotFoundError(
                f"消息流不存在: session_id={session_id} turn_stream_id={turn_stream_id}"
            )
        state = self._read_state_snapshot(session_id, turn_stream_id)
        if state is None:
            if int(records[0].event["event_seq"]) != 1:
                raise MessageStreamError(
                    "消息流状态快照缺失且事件日志已经被裁剪: "
                    f"session_id={session_id} turn_stream_id={turn_stream_id}"
                )
            first_event = records[0].event
            if first_event.get("type") != "stream.opened":
                raise MessageStreamError(
                    "消息流状态快照缺失且事件日志不是完整流: "
                    f"session_id={session_id} turn_stream_id={turn_stream_id}"
                )
            state = self._empty_state(
                session_id=session_id,
                thread_id=str(first_event["thread_id"]),
                turn_id=str(first_event["turn_id"]),
                turn_stream_id=turn_stream_id,
                job_id=(
                    str(first_event["job_id"])
                    if first_event.get("job_id") is not None
                    else None
                ),
            )
            for record in records:
                state = apply_event(state, record.event)
                state["snapshot_seq"] = int(record.event["event_seq"])
        else:
            snapshot_seq = self._validate_state_snapshot(
                state,
                session_id=session_id,
                thread_id=str(records[0].event["thread_id"]),
                turn_id=str(records[0].event["turn_id"]),
                turn_stream_id=turn_stream_id,
                last_event_seq=int(records[-1].event["event_seq"]),
            )
            if (
                int(records[-1].event["event_seq"]) > snapshot_seq
                and int(records[0].event["event_seq"]) > snapshot_seq + 1
            ):
                raise MessageStreamError(
                    "消息流状态快照与事件日志之间存在不可恢复间隙: "
                    f"session_id={session_id} turn_stream_id={turn_stream_id} "
                    f"snapshot_seq={snapshot_seq} first_event_seq={records[0].event['event_seq']}"
                )
            for record in records:
                event_seq = int(record.event.get("event_seq", 0))
                if event_seq > snapshot_seq:
                    state = apply_event(state, record.event)
                    state["snapshot_seq"] = event_seq
        stored_workspace_id = state.get("workspace_id")
        if (
            self._workspace_id is not None
            and stored_workspace_id is not None
            and stored_workspace_id != self._workspace_id
        ):
            logger.error("消息流快照工作区不匹配: session_id=%s turn_stream_id=%s expected=%s actual=%r", session_id, turn_stream_id, self._workspace_id, stored_workspace_id)
            raise MessageStreamError(
                "消息流状态快照 workspace_id 不匹配: "
                f"session_id={session_id} turn_stream_id={turn_stream_id} expected={self._workspace_id}"
            )
        if self._workspace_id is not None:
            state["workspace_id"] = self._workspace_id
        backfill_lifecycle_metadata(state, [record.event for record in records])
        event_ids = self._event_ids_from_records(
            session_id,
            turn_stream_id,
            records,
            thread_id=str(state["thread_id"]),
            turn_id=str(state["turn_id"]),
        )
        self._touch_cached_state(turn_stream_id, state)
        self._event_ids[turn_stream_id] = event_ids
        self._event_ids_loaded.add(turn_stream_id)
        return copy.deepcopy(state)

    def _load_event_ids_from_disk(
        self, session_id: str, turn_stream_id: str, *, thread_id: str, turn_id: str,
    ) -> None:
        if turn_stream_id in self._event_ids_loaded:
            return
        records = self._read_records(
            self._stream_path(session_id, turn_stream_id),
            expected_session_id=session_id,
            expected_turn_stream_id=turn_stream_id,
        )
        self._event_ids[turn_stream_id] = self._event_ids_from_records(
            session_id,
            turn_stream_id,
            records,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        self._event_ids_loaded.add(turn_stream_id)

    async def open(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        turn_stream_id: str | None = None,
        job_id: str | None = None,
    ) -> MessageStreamWriter:
        validate_thread_id(thread_id)
        self._path_resolver.resolve_thread_node(session_id, thread_id)
        async with self._index_lock_for(session_id):
            index = self._read_index(session_id)
            resolved_stream_id = turn_stream_id or index.get(thread_id, {}).get(turn_id)
            if resolved_stream_id is None:
                resolved_stream_id = create_prefixed_id("strm")
                state = self._empty_state(
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    turn_stream_id=resolved_stream_id,
                    job_id=job_id,
                )
                self._states[resolved_stream_id] = state
                index.setdefault(thread_id, {})[turn_id] = resolved_stream_id
                self._write_index(session_id, index)
                writer = MessageStreamWriter(
                    self,
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    turn_stream_id=resolved_stream_id,
                    job_id=job_id,
                )
                await writer.commit("stream.opened", {"status": "open"})
                return writer
        state = self._load_state(session_id, resolved_stream_id)
        if (state["session_id"], state["thread_id"], state["turn_id"]) != (session_id, thread_id, turn_id):
            raise MessageStreamError(
                "消息流关联键不匹配: "
                "turn_stream_id="
                f"{resolved_stream_id} session_id={session_id} turn_id={turn_id}"
            )
        return MessageStreamWriter(
            self,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            turn_stream_id=resolved_stream_id,
            job_id=job_id or state.get("job_id"),
        )

    async def open_existing(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        turn_stream_id: str | None = None,
    ) -> MessageStreamWriter:
        """只读取已持久化的消息流，不为历史查询创建空流。"""
        validate_thread_id(thread_id)
        self._path_resolver.resolve_thread_node(session_id, thread_id)
        async with self._index_lock_for(session_id):
            index = self._read_index(session_id)
            resolved_stream_id = turn_stream_id or index.get(thread_id, {}).get(turn_id)
        if resolved_stream_id is None:
            raise MessageStreamNotFoundError(
                f"消息流不存在: session_id={session_id} turn_id={turn_id}"
            )
        state = self._load_state(session_id, resolved_stream_id)
        if (state["session_id"], state["thread_id"], state["turn_id"]) != (session_id, thread_id, turn_id):
            raise MessageStreamError(
                "消息流关联键不匹配: "
                "turn_stream_id="
                f"{resolved_stream_id} session_id={session_id} turn_id={turn_id}"
            )
        return MessageStreamWriter(
            self,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            turn_stream_id=resolved_stream_id,
            job_id=state.get("job_id"),
        )

    async def existing_stream_ids(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_ids: list[str],
    ) -> dict[str, str]:
        """返回已持久化的 TurnStream，不为缺少 message.v1 的历史创建空流。"""
        validate_thread_id(thread_id)
        self._path_resolver.resolve_thread_node(session_id, thread_id)
        async with self._index_lock_for(session_id):
            index = self._read_index(session_id)
        result: dict[str, str] = {}
        for turn_id in turn_ids:
            turn_stream_id = index.get(thread_id, {}).get(turn_id)
            if turn_stream_id is None:
                continue
            # availability 只回答“索引指向的持久化流是否存在”，不能为了四个
            # 布尔式结果把完整 snapshot、事件 ID 和正文加载进进程缓存。
            if self._stream_path(session_id, turn_stream_id).is_file():
                result[turn_id] = turn_stream_id
        return result

    async def commit(
        self,
        turn_stream_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        model_call_id: str | None = None,
        block_id: str | None = None,
        tool_execution_id: str | None = None,
        tool_call_id: str | None = None,
        tool_invocation_id: str | None = None,
        tool_attempt_id: str | None = None,
        job_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock_for(turn_stream_id):
            return await self._commit_locked(
                turn_stream_id,
                event_type,
                payload,
                model_call_id=model_call_id,
                block_id=block_id,
                tool_execution_id=tool_execution_id,
                tool_call_id=tool_call_id,
                tool_invocation_id=tool_invocation_id,
                tool_attempt_id=tool_attempt_id,
                job_id=job_id,
                event_id=event_id,
            )

    async def _commit_locked(
        self,
        turn_stream_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        model_call_id: str | None = None,
        block_id: str | None = None,
        tool_execution_id: str | None = None,
        tool_call_id: str | None = None,
        tool_invocation_id: str | None = None,
        tool_attempt_id: str | None = None,
        job_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """在已持有 Turn 锁的前提下提交单个业务事件。"""
        if event_type == "stream.snapshot":
            raise MessageStreamError(
                "stream.snapshot 是控制帧，不得作为业务事件提交或消耗 event_seq"
            )
        payload = dict(payload)
        identity_fields = {
            "model_call_id": model_call_id,
            "block_id": block_id,
            "tool_call_id": tool_call_id,
            "tool_invocation_id": tool_invocation_id,
            "tool_attempt_id": tool_attempt_id,
            "tool_execution_id": tool_execution_id,
            "workspace_id": self._workspace_id,
        }
        for field_name, field_value in tuple(identity_fields.items()):
            if field_value is None:
                identity_fields[field_name] = payload.get(field_name)
        validate_event_payload(event_type, payload)
        self._validate_event_identity_fields(payload, identity_fields)
        for field_name, field_value in identity_fields.items():
            if field_value is not None and (
                field_name != "workspace_id" or event_type == "stream.snapshot"
            ):
                payload.setdefault(field_name, field_value)
        # 必须在 Turn 锁内读取缓存；模型生命周期事件和 provider delta
        # 可能并发提交，锁外捕获的旧 state 会让两个提交复用同一个 event_seq。
        cached = self._states.get(turn_stream_id)
        if cached is None:
            raise MessageStreamNotFoundError(
                f"消息流不存在: turn_stream_id={turn_stream_id}"
            )
        state = copy.deepcopy(cached)
        incoming_workspace_id = identity_fields["workspace_id"]
        stored_workspace_id = state.get("workspace_id")
        if (
            incoming_workspace_id is not None
            and stored_workspace_id is not None
            and incoming_workspace_id != stored_workspace_id
        ):
            raise MessageStreamError(
                "消息流事件 workspace_id 与当前流不匹配: "
                f"turn_stream_id={turn_stream_id} "
                f"expected={stored_workspace_id} actual={incoming_workspace_id}"
            )
        if stored_workspace_id is None and incoming_workspace_id is not None:
            state["workspace_id"] = incoming_workspace_id
        if event_id is not None:
            self._load_event_ids_from_disk(
                str(state["session_id"]),
                turn_stream_id,
                thread_id=str(state["thread_id"]),
                turn_id=str(state["turn_id"]),
            )
            previous = self._event_ids.get(turn_stream_id, {}).get(event_id)
            previous_payload = previous.get("payload") if previous is not None else None
            allowed_interrupt_duplicate = (
                event_type == "interrupt.requested"
                and previous is not None
                and previous.get("type") == "interrupt.rejected"
                and isinstance(previous_payload, dict)
                and previous_payload.get("interrupt_request_id")
                == payload.get("interrupt_request_id")
                and previous_payload.get("reason")
                in {"already_interrupting", "already_terminal"}
            )
            identity_conflict = (
                previous is not None
                and (
                    previous.get("type") != event_type
                    or previous.get("payload") != payload
                    or any(
                        field_value is not None
                        and (
                            previous.get(field_name)
                            or (
                                previous_payload.get(field_name)
                                if isinstance(previous_payload, Mapping)
                                else None
                            )
                        ) != field_value
                        for field_name, field_value in identity_fields.items()
                    )
                )
            )
            if identity_conflict and not allowed_interrupt_duplicate:
                raise MessageStreamError(
                    "重复 event_id 的消息流事件内容不一致: "
                    f"turn_stream_id={turn_stream_id} event_id={event_id}"
                )
            if previous is not None:
                return copy.deepcopy(previous)
        current_status = state["stream_status"]
        if current_status == "interrupting":
            if event_type == "interrupt.requested":
                event_type = "interrupt.rejected"
                payload = {
                    **dict(payload),
                    "reason": "already_interrupting",
                }
            elif event_type not in INTERRUPTING_ALLOWED_EVENT_TYPES:
                raise MessageStreamTerminalError(
                    "中断闸门拒绝新的消息流事件: "
                    "turn_stream_id="
                    f"{turn_stream_id} type={event_type}"
                )
        if current_status in TERMINAL_STREAM_STATUSES and event_type not in {
            "interrupt.rejected",
            "stream.snapshot",
        }:
            if event_type == "interrupt.requested":
                event_type = "interrupt.rejected"
                payload = {
                    **dict(payload),
                    "reason": "already_terminal",
                }
            else:
                raise MessageStreamTerminalError(
                    "终态消息流拒绝新事件: "
                    "turn_stream_id="
                    f"{turn_stream_id} status={current_status} type={event_type}"
                )
        if event_type == "stream.completed":
            # stream.completed 是整条消息流的原子终态边界。provider delta
            # hook 与 LangChain callback event 可能在 model.completed 后仍
            # 有一个调度窗口，此时 state 中仍残留 running block。它们必须在
            # 同一把 Turn 锁内以真实的 block.completed 公共事件闭合，事件路径
            # 与快照路径才会看到同一份终态事实；不得依赖内部标记绕过协议。
            await self._close_running_blocks_locked(
                turn_stream_id,
                job_id=job_id,
            )
            state = copy.deepcopy(self._states[turn_stream_id])
        next_seq = int(state["snapshot_seq"]) + 1
        event: dict[str, Any] = {
            "event_id": event_id or create_prefixed_id("evt"),
            "session_id": state["session_id"],
            "thread_id": state["thread_id"],
            "turn_id": state["turn_id"],
            "turn_stream_id": turn_stream_id,
            "event_seq": next_seq,
            "emitted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "type": event_type,
            "payload": copy.deepcopy(dict(payload)),
        }
        for field_name, field_value in identity_fields.items():
            if field_value is not None:
                event[field_name] = field_value
        if state.get("workspace_id") is not None:
            event["workspace_id"] = state["workspace_id"]
        resolved_job_id = job_id or state.get("job_id")
        if resolved_job_id is not None:
            event["job_id"] = resolved_job_id
        next_state = apply_event(state, event)
        if event_type == "stream.completed":
            self._validate_completed_state(next_state)
        if resolved_job_id is not None:
            next_state["job_id"] = resolved_job_id
        next_state["snapshot_seq"] = next_seq
        # checkpoint 只供进程内订阅者使用；磁盘只追加 event，并把最新状态
        # 原子写入单独快照，避免每个事件重复复制整个 blocks/tool_executions。
        record = MessageStreamRecord(event=event, checkpoint=next_state)
        session_id = str(state["session_id"])
        path = self._stream_path(session_id, turn_stream_id)
        encoded = self._encode_event(event)
        try:
            append_task = asyncio.create_task(asyncio.to_thread(
                self._append_durable_event,
                session_id,
                path,
                encoded,
                turn_stream_id,
                next_state,
                # 只有调用方显式指定的 event_id 才需要跨保留窗口记忆；自动生成
                # 的 evt_* 不会被重放，写入侧车只会让文件无界增长。
                event if event_id is not None else None,
            ))
            try:
                await asyncio.shield(append_task)
            finally:
                # 取消不能停止线程中的 append；持有 Turn 锁直到真实 worker
                # 结算，重复取消也不能让下一提交复用旧 checkpoint 的序号。
                await run_cleanup_shielded(lambda: append_task)
        except (Exception, asyncio.CancelledError):
            # append/fsync 失败或取消后，磁盘可能已经包含完整记录，也可能只包含
            # 半条记录。重新扫描并截断未完成尾部，避免进程内继续沿用旧
            # checkpoint，下一次提交复用已经写过的 event_seq。
            self._states.pop(turn_stream_id, None)
            self._terminal_state_sizes.pop(turn_stream_id, None)
            self._event_ids.pop(turn_stream_id, None)
            self._event_ids_loaded.discard(turn_stream_id)
            self._load_state_from_disk(session_id, turn_stream_id)
            raise
        self._touch_cached_state(turn_stream_id, next_state)
        self._event_ids.setdefault(turn_stream_id, {})[event["event_id"]] = event
        self._event_ids_loaded.add(turn_stream_id)
        subscribers = self._subscriptions.get(turn_stream_id, set())
        overflowed: list[MessageStreamSubscription] = []
        for subscription in tuple(subscribers):
            try:
                offered = subscription.offer(record)
            except Exception:
                subscription.closed = True
                logger.exception(
                    "消息流订阅 fanout 失败并关闭: turn_stream_id=%s",
                    turn_stream_id,
                )
                offered = False
            if not offered:
                overflowed.append(subscription)
        for subscription in overflowed:
            subscribers.discard(subscription)
            logger.error(
                "消息流订阅队列溢出并关闭: turn_stream_id=%s",
                turn_stream_id,
            )
        if self._should_write_state_snapshot(event_type, next_seq):
            self._schedule_state_snapshot(
                session_id,
                turn_stream_id,
                next_state,
            )
        return copy.deepcopy(event)

    async def _close_running_blocks_locked(
        self,
        turn_stream_id: str,
        *,
        job_id: str | None,
    ) -> None:
        """用规范的 block.completed 事件闭合终态边界上仍在 running 的 block。"""
        pending = [
            item
            for item in self._states[turn_stream_id].get("blocks", [])
            if isinstance(item, Mapping)
            and item.get("status") == "running"
            and isinstance(item.get("block_id"), str)
        ]
        for item in pending:
            await self._commit_locked(
                turn_stream_id,
                "block.completed",
                {
                    "block_id": str(item["block_id"]),
                    "block_index": int(item.get("block_index") or 0),
                    "carrier_type": str(item.get("carrier_type") or "text"),
                    "status": "completed",
                    "completion_reason": "stream_completed",
                    "partial": False,
                },
                model_call_id=(
                    str(item["model_call_id"])
                    if isinstance(item.get("model_call_id"), str)
                    else None
                ),
                block_id=str(item["block_id"]),
                job_id=job_id,
            )

    async def snapshot_event(self, turn_stream_id: str) -> dict[str, Any]:
        """构造与 checkpoint 高水位一致的控制帧，不追加事件日志。"""
        state = await self.get_state(turn_stream_id)
        snapshot_seq = int(state["snapshot_seq"])
        return {
            "event_id": create_prefixed_id("snapshot"),
            "session_id": state["session_id"],
            "thread_id": state["thread_id"],
            "turn_id": state["turn_id"],
            "turn_stream_id": turn_stream_id,
            "event_seq": snapshot_seq,
            "emitted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "type": "stream.snapshot",
            "payload": copy.deepcopy(state),
            **(
                {"workspace_id": state["workspace_id"]}
                if state.get("workspace_id")
                else {}
            ),
            **({"job_id": state["job_id"]} if state.get("job_id") else {}),
        }

    @staticmethod
    def _validate_completed_state(state: Mapping[str, Any]) -> None:
        active_entities = {
            "model_calls": {
                str(item.get("model_call_id"))
                for item in state.get("model_calls", [])
                if isinstance(item, Mapping) and item.get("status") == "running"
            },
            "blocks": {
                str(item.get("block_id"))
                for item in state.get("blocks", [])
                if isinstance(item, Mapping) and item.get("status") == "running"
            },
            "tool_calls": {
                str(item.get("tool_call_id"))
                for item in state.get("tool_calls", [])
                if isinstance(item, Mapping)
                and item.get("status") in {"accumulating", "streaming", "running"}
            },
            "tool_executions": {
                str(item.get("tool_execution_id"))
                for item in state.get("tool_executions", [])
                if isinstance(item, Mapping)
                and item.get("status") in {"running", "waiting", "stopping"}
            },
            "activities": {
                str(item.get("activity_id"))
                for item in state.get("activities", [])
                if isinstance(item, Mapping)
                and item.get("status") in {"running", "waiting", "stopping"}
            },
        }
        unfinished = {
            entity_type: sorted(entity_ids)
            for entity_type, entity_ids in active_entities.items()
            if entity_ids
        }
        if unfinished:
            raise MessageStreamError(
                "stream.completed 前仍存在未闭合消息流实体: "
                f"{unfinished}"
            )






















    async def get_state(self, turn_stream_id: str) -> dict[str, Any]:
        cached = self._states.get(turn_stream_id)
        if cached is None:
            raise MessageStreamNotFoundError(
                f"消息流不存在: turn_stream_id={turn_stream_id}"
            )
        self._touch_cached_state(turn_stream_id, cached)
        return copy.deepcopy(cached)

    async def reconcile_job_tools(
        self, *, session_id: str, thread_id: str, job_id: str,
        terminal_status_writer: TurnTerminalStatusWriter,
    ) -> int:
        """先持久化原 attempt 的真实终态，再闭合它的消息流镜像。"""
        validate_thread_id(thread_id)
        reconciled = 0
        for turn_id, stream_id in self._read_index(session_id).get(thread_id, {}).items():
            state = self._load_state(session_id, stream_id)
            if state.get("job_id") != job_id:
                continue
            if (state.get("session_id"), state.get("thread_id"), state.get("turn_id")) != (session_id, thread_id, turn_id):
                raise MessageStreamError("工具恢复的持久消息流与原 Job owner 不一致")
            if state["stream_status"] in TERMINAL_STREAM_STATUSES:
                continue
            for attempt in state["tool_executions"]:
                if attempt["status"] != "running":
                    continue
                completion = ToolResultCompletion(
                    model_call_id=attempt.get("model_call_id"),
                    tool_call_id=attempt.get("tool_call_id"),
                    tool_invocation_id=attempt.get("tool_invocation_id"),
                    tool_attempt_id=attempt.get("tool_attempt_id"),
                    tool_name=attempt.get("tool_name"), content=RECOVERED_TOOL_RESULT_UNKNOWN,
                    tool_outcome="unknown",
                )
                if completion.tool_attempt_id != attempt.get("tool_execution_id"):
                    raise MessageStreamError("工具恢复的 execution/attempt 身份不一致")
                item = await asyncio.to_thread(
                    terminal_status_writer.commit_tool_result,
                    session_id=session_id, thread_id=thread_id, job_id=job_id,
                    completion=completion, recovery=True,
                )
                payload = item.payload
                outcome = payload["tool_outcome"]
                stream_outcome = (
                    "outcome_unknown" if outcome == "unknown"
                    else "success" if outcome == "success"
                    else "provider_error"
                )
                await self.commit(
                    stream_id, "tool.completed", {
                        "tool_execution_id": completion.tool_attempt_id,
                        "tool_call_id": completion.tool_call_id,
                        "tool_invocation_id": completion.tool_invocation_id,
                        "tool_attempt_id": completion.tool_attempt_id,
                        "tool_name": attempt["tool_name"],
                        "status": "completed" if outcome in {"success", "unknown"} else "failed",
                        "outcome": stream_outcome,
                        "completion_reason": (
                            "execution_lost" if outcome == "unknown"
                            else "user_interrupt" if outcome == "cancelled"
                            else "tool_completed"
                        ),
                        "result": payload["content"],
                    }, model_call_id=completion.model_call_id,
                    tool_execution_id=completion.tool_attempt_id,
                    tool_call_id=completion.tool_call_id,
                    tool_invocation_id=completion.tool_invocation_id,
                    tool_attempt_id=completion.tool_attempt_id, job_id=job_id,
                    event_id=f"tool-terminal:{item.item_id}",
                )
                reconciled += 1
        return reconciled

    async def reconcile_unfinished_streams(self, *, resumed_job_ids: frozenset[str] | set[str] = frozenset()) -> int:
        """在后端重启时把没有终态的消息流标记为 execution_lost。"""
        reconciled = 0
        for node in self._path_resolver.list_nodes():
            if node.kind != "session":
                continue
            stream_dir = (
                self._path_resolver.resolve_session_node(node.node_id)
                / "message_streams"
            )
            if not stream_dir.is_dir():
                continue
            for path in sorted(stream_dir.glob("*.jsonl")):
                turn_stream_id = path.stem
                try:
                    state = self._load_state_from_disk(str(node.node_id), turn_stream_id)
                except MessageStreamNotFoundError:
                    # 创建中断留下的空日志没有已提交事件，保持原有跳过行为。
                    logger.info("启动恢复跳过空消息流: session_id=%s turn_stream_id=%s", node.node_id, turn_stream_id)
                    continue
                if state["stream_status"] in TERMINAL_STREAM_STATUSES:
                    # 启动扫描只需要识别未完成执行；终态历史已经在磁盘上，不能
                    # 因一次全仓恢复扫描永久占用内存。
                    self._evict_stream_cache(turn_stream_id)
                    continue
                if state.get("job_id") in resumed_job_ids:
                    continue
                interrupt_state = state.get("interrupt_state")
                after_interrupt_requested = (
                    isinstance(interrupt_state, Mapping)
                    and interrupt_state.get("status") == "requested"
                )
                await self.commit(
                    turn_stream_id,
                    "stream.failed",
                    {
                        "code": "execution_lost",
                        "message": "工作区后端重启，无法安全续接原 AgentLoop 执行",
                        "after_interrupt_requested": after_interrupt_requested,
                        "resumable": False,
                    },
                )
                reconciled += 1
        return reconciled

    async def list_events(
        self,
        *,
        session_id: str,
        turn_stream_id: str,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """返回游标之后的事件；``limit`` 为空表示不截断，供 SSE 续播使用。"""
        state = await self.get_state(turn_stream_id)
        self._raise_if_cursor_ahead(
            state,
            turn_stream_id=turn_stream_id,
            after_seq=after_seq,
        )
        path = self._stream_path(session_id, turn_stream_id)
        records = self._read_records(
            path,
            expected_session_id=session_id,
            expected_turn_stream_id=turn_stream_id,
        )
        events = [
            copy.deepcopy(record.event)
            for record in records
            if int(record.event.get("event_seq", 0)) > after_seq
        ]
        if events and int(events[0]["event_seq"]) > after_seq + 1:
            raise MessageStreamCursorGoneError(
                turn_stream_id=turn_stream_id,
                after_seq=after_seq,
                first_seq=int(events[0]["event_seq"]),
            )
        if not events and after_seq < int(state["snapshot_seq"]):
            raise MessageStreamCursorGoneError(
                turn_stream_id=turn_stream_id,
                after_seq=after_seq,
                first_seq=int(state["snapshot_seq"]),
            )
        return events if limit is None else events[:limit]

    @staticmethod
    def _raise_if_cursor_ahead(
        state: Mapping[str, Any],
        *,
        turn_stream_id: str,
        after_seq: int,
    ) -> None:
        """游标越过最高水位时 fail-closed；``list_events`` 与 SSE 入口共用同一判定。

        越过最高水位不是「已追平」：服务端从未产生过这些序号，重放窗口与后续事件都
        无法补齐。``list_events`` 曾在此静默返回空页，使 ``stream_records`` 既不重放
        也不收敛、永久阻塞在订阅队列上。
        """
        high_water_seq = int(state["snapshot_seq"])
        if after_seq > high_water_seq:
            raise MessageStreamCursorAheadError(
                turn_stream_id=turn_stream_id,
                after_seq=after_seq,
                high_water_seq=high_water_seq,
            )

    async def require_cursor_reachable(
        self, *, turn_stream_id: str, after_seq: int
    ) -> None:
        """SSE 建流前校验游标：越过最高水位必须 fail-closed，不能静默挂起连接。"""
        self._raise_if_cursor_ahead(
            await self.get_state(turn_stream_id),
            turn_stream_id=turn_stream_id,
            after_seq=after_seq,
        )

    async def subscribe(self, turn_stream_id: str) -> MessageStreamSubscription:
        subscription = MessageStreamSubscription(
            turn_stream_id=turn_stream_id,
            maxsize=self._subscriber_queue_size,
        )
        self._subscriptions.setdefault(turn_stream_id, set()).add(subscription)
        return subscription

    async def unsubscribe(self, subscription: MessageStreamSubscription) -> None:
        subscription.closed = True
        subscribers = self._subscriptions.get(subscription.turn_stream_id)
        if subscribers is not None:
            subscribers.discard(subscription)
            if not subscribers:
                del self._subscriptions[subscription.turn_stream_id]

    async def stream_records(
        self,
        *,
        session_id: str,
        turn_stream_id: str,
        after_seq: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        subscription = await self.subscribe(turn_stream_id)
        last_seq = after_seq
        try:
            try:
                initial_events = await self.list_events(
                    session_id=session_id,
                    turn_stream_id=turn_stream_id,
                    after_seq=after_seq,
                )
            except MessageStreamCursorGoneError:
                snapshot_event, terminal = await self._recovery_snapshot(turn_stream_id)
                last_seq = int(snapshot_event["event_seq"])
                yield snapshot_event
                if terminal:
                    return
                initial_events = []
            for event in initial_events:
                last_seq = int(event["event_seq"])
                yield event
                if self._is_terminal_event(event):
                    return
            # 重放窗口里没有终态事件，但流本身可能已经终态：客户端用 Last-Event-ID
            # 指到它已经收到的终态帧上，或终态之后只追加了 interrupt.rejected 这类
            # 非终态事件。此时必须立刻用控制帧收敛并关闭；否则生成器会永久阻塞在
            # 订阅队列上，SSE 连接既收不到帧也不结束。
            if self._is_terminal_snapshot(await self.get_state(turn_stream_id)):
                snapshot_event, _ = await self._recovery_snapshot(turn_stream_id)
                yield snapshot_event
                return
            while True:
                record = await subscription.get()
                event_seq = int(record.event["event_seq"])
                if event_seq <= last_seq:
                    continue
                if event_seq != last_seq + 1:
                    snapshot_event, terminal = await self._recovery_snapshot(
                        turn_stream_id
                    )
                    last_seq = int(snapshot_event["event_seq"])
                    yield snapshot_event
                    if terminal:
                        return
                    continue
                last_seq = event_seq
                yield copy.deepcopy(record.event)
                if self._is_terminal_event(record.event):
                    return
        finally:
            await self.unsubscribe(subscription)

    @staticmethod
    def _is_terminal_event(event: Mapping[str, Any]) -> bool:
        return str(event.get("type")) in {
            "stream.completed",
            "stream.interrupted",
            "stream.failed",
        }

    async def _recovery_snapshot(
        self,
        turn_stream_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """构造续播恢复用的控制帧，并同时返回该快照是否已终态。

        SSE 的三条恢复路径（游标失效、重放窗口无终态、事件序号跳变）都必须先
        发同一个控制帧、再据终态决定是否收敛，收敛为唯一实现以免三处各自漂移。
        """
        snapshot_event = await self.snapshot_event(turn_stream_id)
        return snapshot_event, self._is_terminal_snapshot(snapshot_event["payload"])

    @staticmethod
    def _is_terminal_snapshot(snapshot: Mapping[str, Any]) -> bool:
        return str(snapshot.get("stream_status")) in TERMINAL_STREAM_STATUSES
