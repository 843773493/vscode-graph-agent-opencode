from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.identifier import create_prefixed_id
from app.core.session_paths import SessionPathResolver

logger = logging.getLogger(__name__)

TERMINAL_STREAM_STATUSES = frozenset({"completed", "interrupted", "failed"})
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
        event_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._store.commit(
            self.turn_stream_id,
            event_type,
            dict(payload or {}),
            model_call_id=model_call_id,
            block_id=block_id,
            tool_execution_id=tool_execution_id,
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
        subscriber_queue_size: int = 256,
    ) -> None:
        self._path_resolver = path_resolver
        self._subscriber_queue_size = subscriber_queue_size
        self._locks: dict[str, asyncio.Lock] = {}
        self._index_locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._subscriptions: dict[str, set[MessageStreamSubscription]] = {}
        self._event_ids: dict[str, dict[str, dict[str, Any]]] = {}

    def _lock_for(self, turn_stream_id: str) -> asyncio.Lock:
        return self._locks.setdefault(turn_stream_id, asyncio.Lock())

    def _index_lock_for(self, session_id: str) -> asyncio.Lock:
        return self._index_locks.setdefault(session_id, asyncio.Lock())

    def _stream_dir(self, session_id: str) -> Path:
        return self._path_resolver.resolve_session_node(session_id) / "message_streams"

    def _stream_path(self, session_id: str, turn_stream_id: str) -> Path:
        return self._stream_dir(session_id) / f"{turn_stream_id}.jsonl"

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

    def _read_records(self, path: Path) -> list[MessageStreamRecord]:
        if not path.is_file():
            return []
        records: list[MessageStreamRecord] = []
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
                records.append(
                    MessageStreamRecord(
                        event=event,
                        checkpoint=checkpoint,
                    )
                )
                valid_offset = next_offset
        return records

    def _write_index(self, session_id: str, mapping: Mapping[str, str]) -> None:
        index_path = self._index_path(session_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = index_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(dict(mapping), stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(index_path)

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
            return copy.deepcopy(cached)
        return self._load_state_from_disk(session_id, turn_stream_id)

    def _load_state_from_disk(
        self,
        session_id: str,
        turn_stream_id: str,
    ) -> dict[str, Any]:
        path = self._stream_path(session_id, turn_stream_id)
        records = self._read_records(path)
        if not records:
            raise MessageStreamNotFoundError(
                f"消息流不存在: session_id={session_id} turn_stream_id={turn_stream_id}"
            )
        state = copy.deepcopy(records[-1].checkpoint)
        self._backfill_lifecycle_metadata(state, records)
        self._states[turn_stream_id] = state
        self._event_ids[turn_stream_id] = {
            str(record.event["event_id"]): record.event for record in records
        }
        return copy.deepcopy(state)

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

    async def commit(
        self,
        turn_stream_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        model_call_id: str | None = None,
        block_id: str | None = None,
        tool_execution_id: str | None = None,
        job_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if event_type == "stream.snapshot":
            raise MessageStreamError(
                "stream.snapshot 是控制帧，不得作为业务事件提交或消耗 event_seq"
            )
        cached = self._states.get(turn_stream_id)
        if cached is None:
            raise MessageStreamNotFoundError(
                f"消息流不存在: turn_stream_id={turn_stream_id}"
            )
        async with self._lock_for(turn_stream_id):
            state = copy.deepcopy(cached)
            if event_id is not None:
                previous = self._event_ids.get(turn_stream_id, {}).get(event_id)
                if previous is not None:
                    if (
                        previous.get("type") != event_type
                        or previous.get("payload") != dict(payload)
                    ):
                        previous_payload = previous.get("payload")
                        if not (
                            event_type == "interrupt.requested"
                            and previous.get("type") == "interrupt.rejected"
                            and isinstance(previous_payload, dict)
                            and previous_payload.get("interrupt_request_id")
                            == payload.get("interrupt_request_id")
                            and previous_payload.get("reason")
                            in {"already_interrupting", "already_terminal"}
                        ):
                            raise MessageStreamError(
                                "重复 event_id 的消息流事件内容不一致: "
                                f"turn_stream_id={turn_stream_id} event_id={event_id}"
                            )
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
            if model_call_id is not None:
                event["model_call_id"] = model_call_id
            if block_id is not None:
                event["block_id"] = block_id
            if tool_execution_id is not None:
                event["tool_execution_id"] = tool_execution_id
            resolved_job_id = job_id or state.get("job_id")
            if resolved_job_id is not None:
                event["job_id"] = resolved_job_id
            next_state = self._apply_event(state, event)
            if resolved_job_id is not None:
                next_state["job_id"] = resolved_job_id
            next_state["snapshot_seq"] = next_seq
            record = MessageStreamRecord(event=event, checkpoint=next_state)
            path = self._stream_path(str(state["session_id"]), turn_stream_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(
                {"event": event, "checkpoint": next_state},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            try:
                with path.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
                    # 本地工作区没有消息队列，fsync 是 event/checkpoint 的提交边界。
                    os.fsync(stream.fileno())
            except Exception:
                # append/fsync 失败后，磁盘可能已经包含完整记录，也可能只包含
                # 半条记录。重新扫描并截断未完成尾部，避免进程内继续沿用旧
                # checkpoint，下一次提交复用已经写过的 event_seq。
                self._states.pop(turn_stream_id, None)
                self._event_ids.pop(turn_stream_id, None)
                self._load_state_from_disk(str(state["session_id"]), turn_stream_id)
                raise
            self._states[turn_stream_id] = next_state
            self._event_ids.setdefault(turn_stream_id, {})[event["event_id"]] = event
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
            **({"job_id": state["job_id"]} if state.get("job_id") else {}),
        }

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
            tool_call_id = payload.get("tool_call_id")
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
            block["text"] = str(block.get("text") or "") + text
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
        executions = state.setdefault("tool_executions", [])
        for execution in executions:
            if (
                isinstance(execution, dict)
                and execution.get("tool_execution_id") == execution_id
            ):
                execution.update(dict(payload))
                execution["status"] = status
                return execution
        execution = {**dict(payload), "status": status}
        executions.append(execution)
        return execution

    async def get_state(self, turn_stream_id: str) -> dict[str, Any]:
        cached = self._states.get(turn_stream_id)
        if cached is None:
            raise MessageStreamNotFoundError(
                f"消息流不存在: turn_stream_id={turn_stream_id}"
            )
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
                records = self._read_records(path)
                if not records:
                    continue
                state = copy.deepcopy(records[-1].checkpoint)
                turn_stream_id = str(state["turn_stream_id"])
                self._states[turn_stream_id] = state
                self._event_ids[turn_stream_id] = {
                    str(record.event["event_id"]): record.event for record in records
                }
                if state["stream_status"] in TERMINAL_STREAM_STATUSES:
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
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        state = await self.get_state(turn_stream_id)
        path = self._stream_path(session_id, turn_stream_id)
        records = self._read_records(path)
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
