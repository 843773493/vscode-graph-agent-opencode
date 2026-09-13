from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.bounded_json import bound_json_value
from app.core.identifier import create_prefixed_id
from app.core.session_paths import SessionPathResolver

logger = logging.getLogger(__name__)

TERMINAL_STREAM_STATUSES = frozenset({"completed", "interrupted", "failed"})
MESSAGE_STREAM_EVENT_MAX_PAYLOAD_BYTES = 256 * 1024
MESSAGE_STREAM_MAX_BYTES = 64 * 1024 * 1024
MESSAGE_STREAM_RETAINED_BYTES = 8 * 1024 * 1024
MESSAGE_STREAM_TERMINAL_CACHE_MAX_ENTRIES = 16
MESSAGE_STREAM_TERMINAL_CACHE_MAX_BYTES = 16 * 1024 * 1024
MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS = 256 * 1024
MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS = 64 * 1024
MESSAGE_STREAM_TEXT_TRUNCATION_MARKER = (
    "\n\n…消息流展示已截断；Turn 完成后可从权威历史详情读取持久化内容…\n\n"
)
# 消息事件日志逐事件持久化；状态快照只是恢复加速索引，不需要逐 token 写入。
MESSAGE_STREAM_SNAPSHOT_INTERVAL_EVENTS = 32
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


class MessageStreamError(RuntimeError):
    """消息流状态或持久化边界错误。"""


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
        turn_id: str,
        turn_stream_id: str,
        job_id: str | None = None,
    ) -> None:
        self._store = store
        self.session_id = session_id
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
        path_resolver: SessionPathResolver,
        workspace_id: str | None = None,
        subscriber_queue_size: int = 256,
    ) -> None:
        self._path_resolver = path_resolver
        self._workspace_id = workspace_id
        self._subscriber_queue_size = subscriber_queue_size
        self._locks: dict[str, asyncio.Lock] = {}
        self._index_locks: dict[str, asyncio.Lock] = {}
        self._snapshot_locks: dict[str, asyncio.Lock] = {}
        self._snapshot_tasks: dict[str, asyncio.Task[None]] = {}
        self._snapshot_file_lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}
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
        self._event_ids.pop(turn_stream_id, None)
        self._event_ids_loaded.discard(turn_stream_id)

    def _touch_cached_state(
        self,
        turn_stream_id: str,
        state: dict[str, Any],
    ) -> None:
        # dict 的插入顺序就是进程内 LRU。业务事实始终在 JSONL/state snapshot，
        # 这里的终态状态只用于加速最近访问，不能随着历史会话无限增长。
        self._states.pop(turn_stream_id, None)
        self._states[turn_stream_id] = state
        self._prune_terminal_cache(protected_stream_id=turn_stream_id)

    def _prune_terminal_cache(self, *, protected_stream_id: str | None) -> None:
        terminal_entries = [
            (stream_id, state, self._cached_state_size_bytes(state))
            for stream_id, state in self._states.items()
            if str(state.get("stream_status")) in TERMINAL_STREAM_STATUSES
        ]
        total_bytes = sum(size for _, _, size in terminal_entries)
        while (
            len(terminal_entries) > MESSAGE_STREAM_TERMINAL_CACHE_MAX_ENTRIES
            or total_bytes > MESSAGE_STREAM_TERMINAL_CACHE_MAX_BYTES
        ):
            candidate_index = next(
                (
                    index
                    for index, (stream_id, _, _) in enumerate(terminal_entries)
                    if stream_id != protected_stream_id
                    and not self._subscriptions.get(stream_id)
                ),
                None,
            )
            if candidate_index is None:
                # 当前请求使用中的单个超大终态允许暂留；下一条流进入时会把它淘汰。
                break
            stream_id, _, size = terminal_entries.pop(candidate_index)
            total_bytes -= size
            self._evict_stream_cache(stream_id)

    @staticmethod
    def _bounded_stream_text(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        marker = MESSAGE_STREAM_TEXT_TRUNCATION_MARKER
        retained_chars = max_chars - len(marker)
        head_chars = retained_chars // 2
        tail_chars = retained_chars - head_chars
        return f"{value[:head_chars]}{marker}{value[-tail_chars:]}"

    def _lock_for(self, turn_stream_id: str) -> asyncio.Lock:
        return self._locks.setdefault(turn_stream_id, asyncio.Lock())

    def _index_lock_for(self, session_id: str) -> asyncio.Lock:
        return self._index_locks.setdefault(session_id, asyncio.Lock())

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

    def _empty_state(
        self,
        *,
        session_id: str,
        turn_id: str,
        turn_stream_id: str,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
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
        event_type: str,
        payload: Mapping[str, Any],
        identity_fields: Mapping[str, Any],
        *,
        path: Path | None = None,
    ) -> None:
        """校验信封身份与 payload 身份一致，禁止同一事件出现两套归属。"""
        location = f" path={path}" if path is not None else ""
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
                        f"type={event_type} field={field_name} source={source}{location}"
                    )
            if (
                envelope_value is not None
                and payload_value is not None
                and envelope_value != payload_value
            ):
                raise MessageStreamError(
                    "消息流信封与 payload 身份不一致: "
                    f"type={event_type} field={field_name} "
                    f"envelope={envelope_value} payload={payload_value}{location}"
                )

    @staticmethod
    def _validate_event_record(
        event: Mapping[str, Any],
        *,
        path: Path,
        expected_session_id: str | None = None,
        expected_turn_id: str | None = None,
        expected_turn_stream_id: str | None = None,
    ) -> tuple[str, str, str, int, str]:
        event_id = event.get("event_id")
        session_id = event.get("session_id")
        turn_id = event.get("turn_id")
        turn_stream_id = event.get("turn_stream_id")
        event_seq = event.get("event_seq")
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(event_id, str) or not event_id:
            raise MessageStreamError(f"消息流事件缺少 event_id: path={path}")
        if not isinstance(session_id, str) or not session_id:
            raise MessageStreamError(f"消息流事件缺少 session_id: path={path}")
        if not isinstance(turn_id, str) or not turn_id:
            raise MessageStreamError(f"消息流事件缺少 turn_id: path={path}")
        if not isinstance(turn_stream_id, str) or not turn_stream_id:
            raise MessageStreamError(f"消息流事件缺少 turn_stream_id: path={path}")
        if (
            isinstance(event_seq, bool)
            or not isinstance(event_seq, int)
            or event_seq <= 0
        ):
            raise MessageStreamError(f"消息流事件序号非法: path={path}")
        if not isinstance(event_type, str) or not event_type:
            raise MessageStreamError(f"消息流事件缺少 type: path={path}")
        if not isinstance(payload, dict):
            raise MessageStreamError(
                f"消息流事件 payload 必须是对象: path={path} type={event_type}"
            )
        MessageStreamStore._validate_event_identity_fields(
            event_type,
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
            path=path,
        )
        expected_values = (
            ("session_id", session_id, expected_session_id),
            ("turn_id", turn_id, expected_turn_id),
            ("turn_stream_id", turn_stream_id, expected_turn_stream_id),
        )
        for field_name, actual, expected in expected_values:
            if expected is not None and actual != expected:
                raise MessageStreamError(
                    "消息流事件关联键不匹配: "
                    f"path={path} field={field_name} expected={expected} actual={actual}"
                )
        return event_id, session_id, turn_id, event_seq, turn_stream_id

    def _read_records(
        self,
        path: Path,
        *,
        expected_session_id: str | None = None,
        expected_turn_stream_id: str | None = None,
    ) -> list[MessageStreamRecord]:
        if not path.is_file():
            return []
        records: list[MessageStreamRecord] = []
        seen_event_ids: set[str] = set()
        expected_turn_id: str | None = None
        previous_event_seq: int | None = None
        valid_offset = 0
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
                            f"消息流事件日志中间记录损坏: path={path}"
                        ) from error
                    with path.open("r+b") as repair_stream:
                        repair_stream.truncate(valid_offset)
                    logger.warning("已丢弃消息流日志末尾未提交记录: path=%s", path)
                    break
                if not isinstance(raw, dict):
                    raise MessageStreamError(f"消息流记录必须是对象: path={path}")
                event = raw.get("event")
                checkpoint = raw.get("checkpoint")
                if not isinstance(event, dict) or not isinstance(checkpoint, dict):
                    raise MessageStreamError(
                        f"消息流记录缺少 event/checkpoint: path={path}"
                    )
                event_id, _, event_turn_id, event_seq, _ = self._validate_event_record(
                    event,
                    path=path,
                    expected_session_id=expected_session_id,
                    expected_turn_id=expected_turn_id,
                    expected_turn_stream_id=expected_turn_stream_id,
                )
                if expected_turn_id is None:
                    expected_turn_id = event_turn_id
                if previous_event_seq is not None and event_seq != previous_event_seq + 1:
                    raise MessageStreamError(
                        "消息流事件序号不连续: "
                        f"path={path} previous={previous_event_seq} current={event_seq}"
                    )
                if event_id in seen_event_ids:
                    raise MessageStreamError(
                        f"消息流日志包含重复 event_id: path={path} event_id={event_id}"
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
        return records

    def _read_state_snapshot(
        self,
        session_id: str,
        turn_stream_id: str,
    ) -> dict[str, Any] | None:
        path = self._state_path(session_id, turn_stream_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MessageStreamError(f"消息流状态快照损坏: path={path}") from error
        if not isinstance(raw, dict):
            raise MessageStreamError(f"消息流状态快照必须是对象: path={path}")
        return raw

    @staticmethod
    def _validate_state_snapshot(
        state: Mapping[str, Any],
        *,
        path: Path,
        session_id: str,
        turn_id: str,
        turn_stream_id: str,
        last_event_seq: int,
    ) -> int:
        for field_name, expected in (
            ("session_id", session_id),
            ("turn_id", turn_id),
            ("turn_stream_id", turn_stream_id),
        ):
            if state.get(field_name) != expected:
                raise MessageStreamError(
                    "消息流状态快照关联键不匹配: "
                    f"path={path} field={field_name} expected={expected} "
                    f"actual={state.get(field_name)}"
                )
        snapshot_seq = state.get("snapshot_seq")
        if (
            isinstance(snapshot_seq, bool)
            or not isinstance(snapshot_seq, int)
            or snapshot_seq < 0
            or snapshot_seq > last_event_seq
        ):
            raise MessageStreamError(
                "消息流状态快照序号非法: "
                f"path={path} snapshot_seq={snapshot_seq} last_event_seq={last_event_seq}"
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
            if path.is_file():
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    raise MessageStreamError(
                        f"消息流状态快照必须是对象: path={path}"
                    )
                if int(existing.get("snapshot_seq", 0)) > snapshot_seq:
                    return
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(".state.tmp")
            with temp_path.open("w", encoding="utf-8") as stream:
                json.dump(dict(state), stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(path)

    def _write_index(self, session_id: str, mapping: Mapping[str, str]) -> None:
        index_path = self._index_path(session_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = index_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(dict(mapping), stream, ensure_ascii=False, separators=(",", ":"))
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

    def _compact_stream_log(self, path: Path, turn_stream_id: str) -> None:
        """保留最近事件，使用最新 state snapshot 继续完整恢复。"""
        records = self._read_records(path)
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
        self._event_ids[turn_stream_id] = {
            str(record.event["event_id"]): record.event for record in retained
        }
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
    ) -> None:
        """在线程中完成事件追加，确保 fsync 不阻塞消息流事件循环。"""
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
        if needs_compaction:
            # 先让当前事件成为恢复事实，再写入同序号快照，最后才能裁剪旧
            # 事件；否则后台快照尚未执行时，裁剪可能移除 stream.opened。
            self._write_state_snapshot(session_id, turn_stream_id, state)
            self._compact_stream_log(path, turn_stream_id)

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

    def _read_index(self, session_id: str) -> dict[str, str]:
        path = self._index_path(session_id)
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise MessageStreamError(f"消息流索引必须是对象: path={path}")
        return {
            str(turn_id): str(turn_stream_id)
            for turn_id, turn_stream_id in raw.items()
        }

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
            # TODO: 迁移完成后删除旧版内嵌 checkpoint 的读取分支；当前仅用于读取既有消息流。
            # 兼容旧版每条记录都内嵌完整 checkpoint 的 JSONL。新格式的
            # checkpoint 字段为空，需要从已 fsync 的事件日志重放恢复。
            legacy_checkpoint = records[-1].checkpoint
            if legacy_checkpoint:
                state = copy.deepcopy(legacy_checkpoint)
            else:
                first_event = records[0].event
                if first_event.get("type") != "stream.opened":
                    raise MessageStreamError(
                        "消息流状态快照缺失且事件日志不是完整流: "
                        f"session_id={session_id} turn_stream_id={turn_stream_id}"
                    )
                state = self._empty_state(
                    session_id=session_id,
                    turn_id=str(first_event["turn_id"]),
                    turn_stream_id=turn_stream_id,
                    job_id=(
                        str(first_event["job_id"])
                        if first_event.get("job_id") is not None
                        else None
                    ),
                )
                for record in records:
                    state = self._apply_event(state, record.event)
                    state["snapshot_seq"] = int(record.event["event_seq"])
        else:
            snapshot_seq = self._validate_state_snapshot(
                state,
                path=self._state_path(session_id, turn_stream_id),
                session_id=session_id,
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
                    state = self._apply_event(state, record.event)
                    state["snapshot_seq"] = event_seq
        stored_workspace_id = state.get("workspace_id")
        if (
            self._workspace_id is not None
            and stored_workspace_id is not None
            and stored_workspace_id != self._workspace_id
        ):
            raise MessageStreamError(
                "消息流状态快照 workspace_id 不匹配: "
                f"expected={self._workspace_id} actual={stored_workspace_id}"
            )
        if self._workspace_id is not None:
            state["workspace_id"] = self._workspace_id
        self._backfill_lifecycle_metadata(state, records)
        self._touch_cached_state(turn_stream_id, state)
        self._event_ids[turn_stream_id] = {
            str(record.event["event_id"]): record.event for record in records
        }
        self._event_ids_loaded.add(turn_stream_id)
        return copy.deepcopy(state)

    def _load_event_ids_from_disk(self, session_id: str, turn_stream_id: str) -> None:
        if turn_stream_id in self._event_ids_loaded:
            return
        records = self._read_records(
            self._stream_path(session_id, turn_stream_id),
            expected_session_id=session_id,
            expected_turn_stream_id=turn_stream_id,
        )
        self._event_ids[turn_stream_id] = {
            str(record.event["event_id"]): record.event for record in records
        }
        self._event_ids_loaded.add(turn_stream_id)

    async def open(
        self,
        *,
        session_id: str,
        turn_id: str,
        turn_stream_id: str | None = None,
        job_id: str | None = None,
    ) -> MessageStreamWriter:
        async with self._index_lock_for(session_id):
            index = self._read_index(session_id)
            resolved_stream_id = turn_stream_id or index.get(turn_id)
            if resolved_stream_id is None:
                resolved_stream_id = create_prefixed_id("strm")
                state = self._empty_state(
                    session_id=session_id,
                    turn_id=turn_id,
                    turn_stream_id=resolved_stream_id,
                    job_id=job_id,
                )
                self._states[resolved_stream_id] = state
                index[turn_id] = resolved_stream_id
                self._write_index(session_id, index)
                writer = MessageStreamWriter(
                    self,
                    session_id=session_id,
                    turn_id=turn_id,
                    turn_stream_id=resolved_stream_id,
                    job_id=job_id,
                )
                await writer.commit("stream.opened", {"status": "open"})
                return writer
        state = self._load_state(session_id, resolved_stream_id)
        if state["turn_id"] != turn_id or state["session_id"] != session_id:
            raise MessageStreamError(
                "消息流关联键不匹配: "
                "turn_stream_id="
                f"{resolved_stream_id} session_id={session_id} turn_id={turn_id}"
            )
        return MessageStreamWriter(
            self,
            session_id=session_id,
            turn_id=turn_id,
            turn_stream_id=resolved_stream_id,
            job_id=job_id or state.get("job_id"),
        )

    async def open_existing(
        self,
        *,
        session_id: str,
        turn_id: str,
        turn_stream_id: str | None = None,
    ) -> MessageStreamWriter:
        """只读取已持久化的消息流，不为历史查询创建空流。"""
        async with self._index_lock_for(session_id):
            index = self._read_index(session_id)
            resolved_stream_id = turn_stream_id or index.get(turn_id)
        if resolved_stream_id is None:
            raise MessageStreamNotFoundError(
                f"消息流不存在: session_id={session_id} turn_id={turn_id}"
            )
        state = self._load_state(session_id, resolved_stream_id)
        if state["turn_id"] != turn_id or state["session_id"] != session_id:
            raise MessageStreamError(
                "消息流关联键不匹配: "
                "turn_stream_id="
                f"{resolved_stream_id} session_id={session_id} turn_id={turn_id}"
            )
        return MessageStreamWriter(
            self,
            session_id=session_id,
            turn_id=turn_id,
            turn_stream_id=resolved_stream_id,
            job_id=state.get("job_id"),
        )

    async def existing_stream_ids(
        self,
        *,
        session_id: str,
        turn_ids: list[str],
    ) -> dict[str, str]:
        """返回已持久化的 TurnStream，不为缺少 message.v1 的历史创建空流。"""
        async with self._index_lock_for(session_id):
            index = self._read_index(session_id)
        result: dict[str, str] = {}
        for turn_id in turn_ids:
            turn_stream_id = index.get(turn_id)
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
        self._validate_event_identity_fields(event_type, payload, identity_fields)
        for field_name, field_value in identity_fields.items():
            if field_value is not None and (
                field_name != "workspace_id" or event_type == "stream.snapshot"
            ):
                payload.setdefault(field_name, field_value)
        async with self._lock_for(turn_stream_id):
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
                running_block_ids = [
                    str(item.get("block_id"))
                    for item in state.get("blocks", [])
                    if isinstance(item, Mapping)
                    and item.get("status") == "running"
                    and isinstance(item.get("block_id"), str)
                ]
                if running_block_ids:
                    # provider delta hook 与 LangChain callback event 可能在
                    # model.completed 后仍有一个调度窗口。stream.completed
                    # 是整条消息流的原子终态边界，允许并记录只针对 block 的
                    # 最终闭合；model/tool/activity 仍由统一校验严格拒绝。
                    payload = {
                        **dict(payload),
                        "auto_closed_blocks": running_block_ids,
                    }
            next_seq = int(state["snapshot_seq"]) + 1
            event: dict[str, Any] = {
                "event_id": event_id or create_prefixed_id("evt"),
                "session_id": state["session_id"],
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
            next_state = self._apply_event(state, event)
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
                await asyncio.to_thread(
                    self._append_durable_event,
                    session_id,
                    path,
                    encoded,
                    turn_stream_id,
                    next_state,
                )
            except Exception:
                # append/fsync 失败后，磁盘可能已经包含完整记录，也可能只包含
                # 半条记录。重新扫描并截断未完成尾部，避免进程内继续沿用旧
                # checkpoint，下一次提交复用已经写过的 event_seq。
                self._states.pop(turn_stream_id, None)
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

    async def snapshot_event(self, turn_stream_id: str) -> dict[str, Any]:
        """构造与 checkpoint 高水位一致的控制帧，不追加事件日志。"""
        state = await self.get_state(turn_stream_id)
        snapshot_seq = int(state["snapshot_seq"])
        return {
            "event_id": create_prefixed_id("snapshot"),
            "session_id": state["session_id"],
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

    def _apply_event(
        self,
        state: dict[str, Any],
        event: Mapping[str, Any],
    ) -> dict[str, Any]:
        next_state = copy.deepcopy(state)
        event_type = str(event["type"])
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise MessageStreamError(
                f"消息流事件 payload 必须是对象: type={event_type}"
            )
        if event_type == "stream.opened":
            next_state["stream_status"] = "open"
            next_state["active_state"] = None
        elif event_type == "model.started":
            next_state["current_model_call_id"] = payload.get("model_call_id")
            next_state["current_attempt"] = int(payload.get("attempt") or 0)
            next_state["agent_loop_status"] = "model_running"
            self._upsert_model_call(next_state, payload, status="running")
            self._set_active_state(
                next_state,
                {
                    "kind": "model_output",
                    "phase": "reasoning",
                    "entity_id": str(payload.get("model_call_id") or ""),
                    "status": "running",
                },
            )
        elif event_type in {"model.completed", "model.failed"}:
            next_state["agent_loop_status"] = "validating"
            self._upsert_model_call(
                next_state,
                payload,
                status="completed" if event_type == "model.completed" else "failed",
            )
            if event_type == "model.failed":
                next_state["failure"] = dict(payload)
        elif event_type == "model.retrying":
            next_state["agent_loop_status"] = "retrying"
            current_model_call_id = next_state.get("current_model_call_id")
            for block in next_state.get("blocks", []):
                if (
                    isinstance(block, dict)
                    and block.get("model_call_id") == current_model_call_id
                ):
                    block["projection"] = "intermediate"
        elif event_type == "block.started":
            self._upsert_block(
                next_state,
                payload,
                status="running",
                model_call_id=event.get("model_call_id"),
            )
            carrier_type = str(payload.get("carrier_type") or "text")
            phase = "reasoning" if carrier_type in {
                "reasoning",
                "reasoning_content",
                "reasoning_items",
                "thinking",
                "redacted_thinking",
            } else "text"
            self._set_active_state(
                next_state,
                {
                    "kind": "model_output",
                    "phase": phase,
                    "entity_id": str(payload.get("block_id") or ""),
                    "block_id": payload.get("block_id"),
                    "carrier_type": carrier_type,
                    "status": "running",
                },
            )
        elif event_type == "block.delta":
            self._apply_block_delta(
                next_state,
                payload,
                model_call_id=event.get("model_call_id"),
            )
        elif event_type == "block.completed":
            block = self._find_block(next_state, payload.get("block_id"))
            if block is not None:
                block["status"] = str(payload.get("status") or "completed")
                block["completion_reason"] = str(
                    payload.get("completion_reason") or "upstream_completed"
                )
                block["partial"] = bool(payload.get("partial", False))
        elif event_type in {"tool_call", "tool_call.delta"}:
            self._upsert_tool_call(next_state, payload)
            self._set_active_state(
                next_state,
                {
                    "kind": "tool_call",
                    "phase": "accumulating",
                    "entity_id": str(payload.get("tool_call_id") or ""),
                    "tool_call_id": payload.get("tool_call_id"),
                    "tool_invocation_id": payload.get("tool_invocation_id"),
                    "tool_attempt_id": payload.get("tool_attempt_id"),
                    "status": str(payload.get("status") or "running"),
                },
            )
        elif event_type == "tool_call.completed":
            self._upsert_tool_call(next_state, payload)
            call = self._find_tool_call(next_state, payload.get("tool_call_id"))
            if call is not None:
                call["status"] = str(payload.get("status") or "incomplete")
                call["completion_reason"] = str(
                    payload.get("completion_reason") or "execution_lost"
                )
            self._set_active_state(
                next_state,
                {
                    "kind": "tool_call",
                    "phase": "stopping",
                    "entity_id": str(payload.get("tool_call_id") or ""),
                    "tool_call_id": payload.get("tool_call_id"),
                    "tool_invocation_id": payload.get("tool_invocation_id"),
                    "tool_attempt_id": payload.get("tool_attempt_id"),
                    "status": str(payload.get("status") or "incomplete"),
                },
            )
        elif event_type == "tool.started":
            self._upsert_tool(next_state, payload, status="running")
            next_state["agent_loop_status"] = "tool_running"
            self._set_active_state(
                next_state,
                {
                    "kind": "tool_execution",
                    "phase": "running",
                    "entity_id": str(payload.get("tool_execution_id") or ""),
                    "tool_execution_id": payload.get("tool_execution_id"),
                    "tool_call_id": payload.get("tool_call_id"),
                    "tool_invocation_id": payload.get("tool_invocation_id"),
                    "tool_attempt_id": payload.get("tool_attempt_id"),
                    "status": "running",
                },
            )
        elif event_type == "tool.completed":
            tool_status = str(payload.get("status") or "completed")
            if tool_status == "outcome_unknown":
                tool_status = "completed"
            self._upsert_tool(
                next_state,
                payload,
                status=tool_status,
            )
            self._set_active_state(
                next_state,
                {
                    "kind": "tool_execution",
                    "phase": "stopping",
                    "entity_id": str(payload.get("tool_execution_id") or ""),
                    "tool_execution_id": payload.get("tool_execution_id"),
                    "tool_call_id": payload.get("tool_call_id"),
                    "tool_invocation_id": payload.get("tool_invocation_id"),
                    "tool_attempt_id": payload.get("tool_attempt_id"),
                    "status": tool_status,
                },
            )
        elif event_type.startswith("activity."):
            self._apply_activity_event(next_state, event_type, payload)
        elif event_type == "interrupt.requested":
            next_state["interrupt_state"] = {
                "request_id": payload.get("interrupt_request_id"),
                "status": "requested",
                "reason": payload.get("reason"),
            }
            next_state["stream_status"] = "interrupting"
            previous = next_state.get("active_state")
            next_state["active_state"] = {
                "kind": "interrupting",
                "phase": "stopping",
                "entity_id": str(payload.get("interrupt_request_id") or ""),
                "status": "stopping",
                "last_kind": previous.get("kind") if isinstance(previous, Mapping) else None,
                "last_phase": previous.get("phase") if isinstance(previous, Mapping) else None,
                "reason": payload.get("reason"),
            }
        elif event_type == "interrupt.rejected":
            if next_state.get("stream_status") != "interrupting":
                next_state["interrupt_state"] = {
                    "request_id": payload.get("interrupt_request_id"),
                    "status": "rejected",
                    "reason": payload.get("reason"),
                }
        elif event_type == "stream.completed":
            auto_closed_blocks = payload.get("auto_closed_blocks", ())
            if auto_closed_blocks in (None, ()):
                auto_closed_blocks = []
            if not isinstance(auto_closed_blocks, list):
                raise MessageStreamError(
                    "stream.completed.auto_closed_blocks 必须是 list"
                )
            auto_closed_ids = {
                str(block_id)
                for block_id in auto_closed_blocks
                if isinstance(block_id, str) and block_id
            }
            for block in next_state.get("blocks", []):
                if (
                    isinstance(block, dict)
                    and block.get("block_id") in auto_closed_ids
                    and block.get("status") == "running"
                ):
                    block["status"] = "completed"
                    block["completion_reason"] = "stream_completed"
                    block["partial"] = False
            next_state["stream_status"] = "completed"
            next_state["agent_loop_status"] = "completed"
            next_state["resumable"] = False
            self._set_terminal_active_state(next_state, "completed", payload)
        elif event_type == "stream.interrupted":
            next_state["stream_status"] = "interrupted"
            next_state["agent_loop_status"] = "interrupted"
            next_state["resumable"] = False
            self._finish_running_blocks(next_state, status="interrupted")
            self._finish_running_tool_calls(next_state, reason="user_interrupt")
            self._mark_running_tools_unknown(next_state)
            self._finish_running_activities(
                next_state,
                terminal_status="completed",
                outcome="user_interrupt",
                completion_reason="user_interrupt",
            )
            self._set_terminal_active_state(next_state, "interrupted", payload)
            if next_state.get("interrupt_state") is None:
                next_state["interrupt_state"] = {
                    "request_id": payload.get("interrupt_request_id"),
                    "status": "confirmed",
                }
            else:
                next_state["interrupt_state"]["status"] = "confirmed"
        elif event_type == "stream.failed":
            next_state["stream_status"] = "failed"
            next_state["agent_loop_status"] = "failed"
            next_state["failure"] = copy.deepcopy(dict(payload))
            next_state["resumable"] = bool(payload.get("resumable", False))
            self._finish_running_blocks(next_state, status="failed")
            self._finish_running_tool_calls(next_state, reason="execution_lost")
            self._mark_running_tools_unknown(next_state)
            self._finish_running_activities(
                next_state,
                terminal_status="failed",
                outcome="execution_lost",
                completion_reason="execution_lost",
            )
            next_state["recovery"] = {
                "status": "execution_lost" if payload.get("code") == "execution_lost" else "failed",
                "code": payload.get("code"),
                "message": payload.get("message"),
                "resumable": bool(payload.get("resumable", False)),
            }
            self._set_terminal_active_state(next_state, "failed", payload)
        elif event_type == "stream.snapshot":
            snapshot = payload.get("snapshot", payload)
            if isinstance(snapshot, Mapping):
                next_state = copy.deepcopy(dict(snapshot))
        self._mark_event_lifecycle(next_state, event)
        return next_state

    @staticmethod
    def _upsert_tool_call(
        state: dict[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        tool_call_id = payload.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise MessageStreamError("tool_call 事件缺少 tool_call_id")
        tool_calls = state.setdefault("tool_calls", [])
        for tool_call in tool_calls:
            if (
                isinstance(tool_call, dict)
                and tool_call.get("tool_call_id") == tool_call_id
            ):
                incoming = dict(payload)
                existing_name = tool_call.get("tool_name")
                if (
                    isinstance(existing_name, str)
                    and existing_name
                    and not incoming.get("tool_name")
                ):
                    incoming.pop("tool_name", None)
                existing_arguments = tool_call.get("arguments")
                if (
                    isinstance(existing_arguments, dict)
                    and existing_arguments
                    and incoming.get("arguments") in ({}, "", None)
                ):
                    incoming.pop("arguments", None)
                tool_call.update(incoming)
                return
        tool_calls.append(copy.deepcopy(dict(payload)))

    @staticmethod
    def _find_tool_call(
        state: dict[str, Any],
        tool_call_id: object,
    ) -> dict[str, Any] | None:
        if not isinstance(tool_call_id, str):
            return None
        for tool_call in state.get("tool_calls", []):
            if isinstance(tool_call, dict) and tool_call.get("tool_call_id") == tool_call_id:
                return tool_call
        return None

    @staticmethod
    def _upsert_model_call(
        state: dict[str, Any],
        payload: Mapping[str, Any],
        *,
        status: str,
    ) -> None:
        model_call_id = payload.get("model_call_id")
        if not isinstance(model_call_id, str) or not model_call_id:
            return
        calls = state.setdefault("model_calls", [])
        for call in calls:
            if isinstance(call, dict) and call.get("model_call_id") == model_call_id:
                call.update(dict(payload))
                call["status"] = status
                return
        calls.append({**dict(payload), "status": status})

    @staticmethod
    def _set_active_state(state: dict[str, Any], active_state: Mapping[str, Any]) -> None:
        state["active_state"] = {
            key: value for key, value in dict(active_state).items() if value is not None
        }

    @staticmethod
    def _event_timestamp(event: Mapping[str, Any]) -> str | None:
        emitted_at = event.get("emitted_at")
        if isinstance(emitted_at, str) and emitted_at:
            return emitted_at
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            updated_at = payload.get("updated_at")
            if isinstance(updated_at, str) and updated_at:
                return updated_at
        return None

    @classmethod
    def _mark_entity_lifecycle(
        cls,
        entity: dict[str, Any],
        event: Mapping[str, Any],
        *,
        completed: bool = False,
    ) -> None:
        event_seq = event.get("event_seq")
        if isinstance(event_seq, bool) or not isinstance(event_seq, int):
            raise MessageStreamError("消息流实体生命周期缺少整数 event_seq")
        event_timestamp = cls._event_timestamp(event)
        started_seq = entity.get("started_seq")
        if isinstance(started_seq, bool) or not isinstance(started_seq, int) or started_seq <= 0:
            entity["started_seq"] = event_seq
            if event_timestamp is not None:
                entity["started_at"] = event_timestamp
        last_event_seq = entity.get("last_event_seq")
        if (
            isinstance(last_event_seq, bool)
            or not isinstance(last_event_seq, int)
            or event_seq >= last_event_seq
        ):
            entity["last_event_seq"] = event_seq
            if event_timestamp is not None:
                entity["updated_at"] = event_timestamp
        if not completed:
            return
        completed_seq = entity.get("completed_seq")
        if (
            isinstance(completed_seq, bool)
            or not isinstance(completed_seq, int)
            or event_seq >= completed_seq
        ):
            entity["completed_seq"] = event_seq
            if event_timestamp is not None:
                entity["completed_at"] = event_timestamp

    @staticmethod
    def _find_model_call(
        state: dict[str, Any],
        model_call_id: object,
    ) -> dict[str, Any] | None:
        if not isinstance(model_call_id, str) or not model_call_id:
            return None
        for model_call in state.get("model_calls", []):
            if (
                isinstance(model_call, dict)
                and model_call.get("model_call_id") == model_call_id
            ):
                return model_call
        return None

    @classmethod
    def _mark_event_lifecycle(
        cls,
        state: dict[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        event_type = str(event.get("type") or "")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return
        completed = event_type in {
            "model.completed",
            "model.failed",
            "block.completed",
            "tool_call.completed",
            "tool.completed",
            "activity.completed",
            "activity.failed",
        }
        if event_type in {"model.started", "model.completed", "model.failed"}:
            model_call = cls._find_model_call(
                state,
                payload.get("model_call_id") or event.get("model_call_id"),
            )
            if model_call is not None:
                cls._mark_entity_lifecycle(model_call, event, completed=completed)
        elif event_type in {"block.started", "block.delta", "block.completed"}:
            block = cls._find_block(state, payload.get("block_id") or event.get("block_id"))
            if block is not None:
                cls._mark_entity_lifecycle(block, event, completed=completed)
        elif event_type in {"tool_call", "tool_call.delta", "tool_call.completed"}:
            tool_call_id = payload.get("tool_call_id") or event.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                for tool_call in state.get("tool_calls", []):
                    if (
                        isinstance(tool_call, dict)
                        and tool_call.get("tool_call_id") == tool_call_id
                    ):
                        cls._mark_entity_lifecycle(tool_call, event, completed=completed)
                        break
        elif event_type in {"tool.started", "tool.completed"}:
            tool_execution_id = payload.get("tool_execution_id") or event.get(
                "tool_execution_id"
            )
            if isinstance(tool_execution_id, str) and tool_execution_id:
                for execution in state.get("tool_executions", []):
                    if (
                        isinstance(execution, dict)
                        and execution.get("tool_execution_id") == tool_execution_id
                    ):
                        cls._mark_entity_lifecycle(execution, event, completed=completed)
                        break
        elif event_type.startswith("activity."):
            activity_id = payload.get("activity_id")
            if isinstance(activity_id, str) and activity_id:
                for activity in state.get("activities", []):
                    if (
                        isinstance(activity, dict)
                        and activity.get("activity_id") == activity_id
                    ):
                        cls._mark_entity_lifecycle(activity, event, completed=completed)
                        break
        elif event_type in {"stream.interrupted", "stream.failed"}:
            for block in state.get("blocks", []):
                if isinstance(block, dict) and "completed_seq" not in block:
                    cls._mark_entity_lifecycle(block, event, completed=True)
            for tool_call in state.get("tool_calls", []):
                if isinstance(tool_call, dict) and "completed_seq" not in tool_call:
                    cls._mark_entity_lifecycle(tool_call, event, completed=True)
            for execution in state.get("tool_executions", []):
                if isinstance(execution, dict) and "completed_seq" not in execution:
                    cls._mark_entity_lifecycle(execution, event, completed=True)
            for activity in state.get("activities", []):
                if isinstance(activity, dict) and "completed_seq" not in activity:
                    cls._mark_entity_lifecycle(activity, event, completed=True)

    @classmethod
    def _backfill_lifecycle_metadata(
        cls,
        state: dict[str, Any],
        records: list[MessageStreamRecord],
    ) -> None:
        """从已有事件日志为旧 checkpoint 补齐实体生命周期序号。"""
        for record in records:
            cls._mark_event_lifecycle(state, record.event)

    @classmethod
    def _set_terminal_active_state(
        cls,
        state: dict[str, Any],
        status: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = state.get("active_state")
        cls._set_active_state(
            state,
            {
                "kind": "terminal",
                "phase": status,
                "entity_id": str(state.get("turn_stream_id") or ""),
                "status": status,
                "last_kind": previous.get("kind") if isinstance(previous, Mapping) else None,
                "last_phase": previous.get("phase") if isinstance(previous, Mapping) else None,
                "reason": payload.get("completion_reason") or payload.get("code") or status,
            },
        )

    @classmethod
    def _apply_activity_event(
        cls,
        state: dict[str, Any],
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        activity_id = payload.get("activity_id")
        if not isinstance(activity_id, str) or not activity_id:
            raise MessageStreamError(f"Activity 事件缺少 activity_id: type={event_type}")
        activities = state.setdefault("activities", [])
        activity = next(
            (
                item
                for item in activities
                if isinstance(item, dict) and item.get("activity_id") == activity_id
            ),
            None,
        )
        if activity is None:
            activity = {"activity_id": activity_id}
            activities.append(activity)
        activity.update(dict(payload))
        if event_type == "activity.started":
            activity["status"] = "running"
        elif event_type == "activity.updated":
            activity["status"] = str(payload.get("status") or activity.get("status") or "running")
        elif event_type == "activity.completed":
            activity["status"] = "completed"
        elif event_type == "activity.failed":
            activity["status"] = str(payload.get("status") or "failed")
        cls._set_active_state(
            state,
            {
                "kind": "activity",
                "phase": activity["status"],
                "entity_id": activity_id,
                "activity_id": activity_id,
                "activity_kind": activity.get("kind"),
                "status": activity["status"],
                "detail_ref": activity.get("detail_ref"),
            },
        )

    @staticmethod
    def _mark_running_tools_unknown(state: dict[str, Any]) -> None:
        for execution in state.get("tool_executions", []):
            if (
                isinstance(execution, dict)
                and execution.get("status") == "running"
            ):
                execution["status"] = "completed"
                execution["outcome"] = "outcome_unknown"
                execution["completion_reason"] = "execution_lost"

    @staticmethod
    def _finish_running_activities(
        state: dict[str, Any],
        *,
        terminal_status: str,
        outcome: str,
        completion_reason: str,
    ) -> None:
        """终态收敛 Activity，避免 snapshot 留下不可解释的运行态。"""
        for activity in state.get("activities", []):
            if not isinstance(activity, dict):
                continue
            if activity.get("status") not in {"running", "waiting", "stopping"}:
                continue
            resolved_status = terminal_status
            resolved_outcome = outcome
            if (
                outcome == "user_interrupt"
                and activity.get("side_effect_policy")
                not in {"none", "read_only"}
            ):
                resolved_status = "failed"
                resolved_outcome = "outcome_unknown"
            activity["status"] = resolved_status
            activity["outcome"] = resolved_outcome
            activity["completion_reason"] = completion_reason
            if resolved_status == "failed":
                activity["resumable"] = False

    @staticmethod
    def _finish_running_blocks(state: dict[str, Any], *, status: str) -> None:
        for block in state.get("blocks", []):
            if isinstance(block, dict) and block.get("status") == "running":
                block["status"] = status
                block["completion_reason"] = (
                    "user_interrupt" if status == "interrupted" else "execution_lost"
                )
                block["partial"] = True

    @staticmethod
    def _finish_running_tool_calls(state: dict[str, Any], *, reason: str) -> None:
        for tool_call in state.get("tool_calls", []):
            if not isinstance(tool_call, dict):
                continue
            if tool_call.get("status") not in {"accumulating", "streaming", "running"}:
                continue
            arguments_complete = tool_call.get("arguments_complete") is True
            tool_call["status"] = (
                "cancelled"
                if reason == "user_interrupt" and arguments_complete
                else "incomplete"
            )
            tool_call["completion_reason"] = reason

    @staticmethod
    def _find_block(
        state: dict[str, Any],
        block_id: object,
    ) -> dict[str, Any] | None:
        if not isinstance(block_id, str):
            return None
        for block in state.get("blocks", []):
            if isinstance(block, dict) and block.get("block_id") == block_id:
                return block
        return None

    def _upsert_block(
        self,
        state: dict[str, Any],
        payload: Mapping[str, Any],
        *,
        status: str,
        model_call_id: object = None,
    ) -> dict[str, Any]:
        block_id = payload.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            raise MessageStreamError("block 事件缺少 block_id")
        block = self._find_block(state, block_id)
        if block is None:
            block = {
                "block_id": block_id,
                "block_index": int(payload.get("block_index") or 0),
                "carrier_type": str(payload.get("carrier_type") or "text"),
                "status": status,
                "text": "",
                "items": [],
                "redacted": bool(payload.get("redacted", False)),
                "projection": str(payload.get("projection") or "streaming"),
            }
            if isinstance(model_call_id, str) and model_call_id:
                block["model_call_id"] = model_call_id
            state.setdefault("blocks", []).append(block)
        else:
            block["status"] = status
        return block

    def _apply_block_delta(
        self,
        state: dict[str, Any],
        payload: Mapping[str, Any],
        *,
        model_call_id: object = None,
    ) -> None:
        block = self._upsert_block(
            state,
            payload,
            status="running",
            model_call_id=model_call_id,
        )
        operation = str(payload.get("operation") or "append")
        text = payload.get("text")
        if operation == "append" and isinstance(text, str):
            block["text"] = self._bounded_stream_text(
                str(block.get("text") or "") + text,
                MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS,
            )
        if operation in {"item_upsert", "item_patch"}:
            item = payload.get("item")
            if not isinstance(item, Mapping):
                raise MessageStreamError("结构化 block.delta 缺少 item")
            items = block.setdefault("items", [])
            item_id = item.get("id")
            found = False
            if isinstance(item_id, str):
                for index, existing in enumerate(items):
                    if isinstance(existing, dict) and existing.get("id") == item_id:
                        if operation == "item_patch":
                            items[index] = {**existing, **dict(item)}
                        else:
                            items[index] = copy.deepcopy(dict(item))
                        found = True
                        break
            if not found:
                items.append(copy.deepcopy(dict(item)))
        if bool(payload.get("redacted", False)):
            block["redacted"] = True

    @staticmethod
    def _upsert_tool(
        state: dict[str, Any],
        payload: Mapping[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        execution_id = payload.get("tool_execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise MessageStreamError("工具事件缺少 tool_execution_id")
        bounded_payload = dict(payload)
        for field_name in ("result", "error"):
            field_value = bounded_payload.get(field_name)
            if isinstance(field_value, str):
                bounded_payload[field_name] = MessageStreamStore._bounded_stream_text(
                    field_value,
                    MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS,
                )
        executions = state.setdefault("tool_executions", [])
        for execution in executions:
            if (
                isinstance(execution, dict)
                and execution.get("tool_execution_id") == execution_id
            ):
                execution.update(bounded_payload)
                execution["status"] = status
                return execution
        execution = {**bounded_payload, "status": status}
        executions.append(execution)
        return execution

    async def get_state(self, turn_stream_id: str) -> dict[str, Any]:
        cached = self._states.get(turn_stream_id)
        if cached is None:
            raise MessageStreamNotFoundError(
                f"消息流不存在: turn_stream_id={turn_stream_id}"
            )
        self._touch_cached_state(turn_stream_id, cached)
        return copy.deepcopy(cached)

    async def reconcile_unfinished_streams(self) -> int:
        """在后端重启时把没有终态的消息流标记为 execution_lost。"""
        reconciled = 0
        for node in self._path_resolver.list_nodes():
            if node.kind != "session":
                continue
            stream_dir = node.path / "message_streams"
            if not stream_dir.is_dir():
                continue
            for path in sorted(stream_dir.glob("*.jsonl")):
                records = self._read_records(
                    path,
                    expected_session_id=str(node.node_id),
                    expected_turn_stream_id=path.stem,
                )
                if not records:
                    continue
                record = records[-1]
                turn_stream_id = str(record.event["turn_stream_id"])
                state_snapshot = self._read_state_snapshot(
                    str(node.node_id),
                    turn_stream_id,
                )
                snapshot_seq = (
                    int(state_snapshot.get("snapshot_seq", 0))
                    if state_snapshot is not None
                    else 0
                )
                last_seq = int(record.event["event_seq"])
                if state_snapshot is not None and snapshot_seq >= last_seq:
                    state = copy.deepcopy(state_snapshot)
                else:
                    # 启动恢复必须重放快照之后已经 fsync 的事件尾部，不能只看
                    # JSONL 最后一条记录中为空的 checkpoint 字段。
                    state = self._load_state_from_disk(
                        str(node.node_id),
                        turn_stream_id,
                    )
                turn_stream_id = str(state["turn_stream_id"])
                if state["stream_status"] in TERMINAL_STREAM_STATUSES:
                    # 启动扫描只需要识别未完成执行；终态历史已经在磁盘上，不能
                    # 因一次全仓恢复扫描永久占用内存。
                    self._evict_stream_cache(turn_stream_id)
                    continue
                self._touch_cached_state(turn_stream_id, state)
                self._event_ids[turn_stream_id] = {
                    str(record.event["event_id"]): record.event
                }
                self._event_ids_loaded.discard(turn_stream_id)
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
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        state = await self.get_state(turn_stream_id)
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
        return events[:limit]

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
                snapshot_event = await self.snapshot_event(turn_stream_id)
                last_seq = int(snapshot_event["event_seq"])
                yield snapshot_event
                if self._is_terminal_snapshot(snapshot_event["payload"]):
                    return
                initial_events = []
            for event in initial_events:
                last_seq = int(event["event_seq"])
                yield event
                if self._is_terminal_event(event):
                    return
            while True:
                record = await subscription.get()
                event_seq = int(record.event["event_seq"])
                if event_seq <= last_seq:
                    continue
                if event_seq != last_seq + 1:
                    snapshot_event = await self.snapshot_event(turn_stream_id)
                    last_seq = int(snapshot_event["event_seq"])
                    yield snapshot_event
                    if self._is_terminal_snapshot(snapshot_event["payload"]):
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

    @staticmethod
    def _is_terminal_snapshot(snapshot: Mapping[str, Any]) -> bool:
        return str(snapshot.get("stream_status")) in TERMINAL_STREAM_STATUSES
