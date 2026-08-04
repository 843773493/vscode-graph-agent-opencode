from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import TypeVar

from app.abstractions.turn_history import (
    TurnHistoryStoreProtocol,
    TurnProjectionOperation,
)
from app.schemas.event import Event
from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.turn import (
    TurnDetailDTO,
    TurnUserMessageDTO,
)
from app.services.mapping.trace_event_mapper import TraceEventMapper

from .mutation import build_turn_mutation
from .presentation import (
    display_metadata,
    map_turn_item,
    response_fields,
    turn_attachments,
)

_ResultT = TypeVar("_ResultT")

CURRENT_TURN_PROJECTION_VERSION = 2

_PROJECTED_EVENT_TYPES = frozenset(
    {
        "job_created",
        "job_merged",
        "job_started",
        "job_completed",
        "job_cancelled",
        "job_failed",
        "message_created",
        "status_change",
        "text_start",
        "text_end",
        "agent_end",
        "tool_call_start",
        "tool_call_end",
        "error",
        "session_interrupted",
    }
)


class TurnHistoryProjector:
    """把持久化 Job 语义事件增量投影为前端 Turn。"""

    def __init__(
        self,
        store: TurnHistoryStoreProtocol,
        *,
        trace_mapper: TraceEventMapper | None = None,
    ) -> None:
        self._store = store
        self._trace_mapper = trace_mapper or TraceEventMapper()
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)

    def apply_event(
        self,
        session_id: str,
        event: Event,
    ) -> TurnDetailDTO | None:
        with self._locks[session_id]:
            return self._apply_event_unlocked(session_id, event, source_offset=None)

    def _apply_event_unlocked(
        self,
        session_id: str,
        event: Event,
        *,
        source_offset: int | None,
    ) -> TurnDetailDTO | None:
        payload_session_id = getattr(event.payload, "session_id", None)
        if payload_session_id is not None and payload_session_id != session_id:
            raise ValueError(
                "Turn 投影事件跨会话: "
                f"scope={session_id}, payload_session={payload_session_id}, "
                f"event_id={event.event_id}"
            )
        if event.type not in _PROJECTED_EVENT_TYPES:
            return None
        if event.type == "message_created" and event.payload.role != "user":
            return None
        if self._store.is_event_applied(session_id, event.job_id, event.event_id):
            return self._store.get_turn(session_id, event.job_id)
        if event.type == "job_merged":
            return self._apply_merge(
                session_id,
                event,
                source_offset=source_offset,
            )

        current = self._store.get_turn(session_id, event.job_id)
        if current is None and event.type != "job_created":
            raise RuntimeError(
                "Turn 投影缺少可靠 job_created 起点: "
                f"session_id={session_id}, turn_id={event.job_id}, "
                f"event_id={event.event_id}, event_type={event.type}"
            )
        turn = current or self._new_turn(session_id, event)
        update: dict[str, object] = {
            "revision": turn.revision + (1 if current is not None else 0),
            "updated_at": event.timestamp,
        }

        if event.type == "job_created":
            update["status"] = JobStatus.accepted
            message = self._message_from_job_created(event)
            if message is not None:
                update.update(self._merge_user_message(turn, message))
        elif event.type == "message_created":
            message = self._message_from_created_event(event)
            update.update(self._merge_user_message(turn, message))
        elif event.type == "job_started":
            update["status"] = JobStatus.running
        elif event.type == "status_change":
            status = event.payload.status
            if status in {value.value for value in JobStatus}:
                update["status"] = JobStatus(status)
        elif event.type == "text_end":
            if event.payload.kind == "markdown":
                update.update(response_fields(event.payload.text))
        elif event.type == "job_completed":
            update.update(response_fields(event.payload.result))
            update["status"] = JobStatus.completed
            update["completed_at"] = event.timestamp
        elif event.type == "job_failed":
            update["status"] = JobStatus.failed
            update["completed_at"] = event.timestamp
        elif event.type == "job_cancelled" or event.type == "session_interrupted":
            update["status"] = JobStatus.cancelled
            update["completed_at"] = event.timestamp
        elif event.type == "error" and event.payload.phase == "agent_execution":
            update["status"] = JobStatus.failed
            update["completed_at"] = event.timestamp

        item = map_turn_item(self._trace_mapper, session_id, event)
        if item is not None:
            update["items"] = [*turn.items, item]
        updated = TurnDetailDTO.model_validate(
            {**turn.model_dump(mode="python"), **update}
        )
        applied = self._store.apply_operation(
            session_id,
            TurnProjectionOperation(
                event_id=event.event_id,
                source_event_offset=source_offset,
                mutations=[build_turn_mutation(current, updated, item)],
                hidden_turn_ids=self._replayed_turn_ids(session_id, event),
            ),
        )
        if applied:
            return updated
        return self._store.get_turn(session_id, event.job_id)

    def _replayed_turn_ids(self, session_id: str, event: Event) -> list[str]:
        if event.type != "job_created":
            return []
        metadata = event.payload.message_metadata
        if not isinstance(metadata, Mapping):
            return []
        replaced_message_id = metadata.get("replaced_message_id")
        replay_action = metadata.get("replay_action")
        if replay_action not in {"edit_and_continue", "regenerate", "retry_failed"}:
            return []
        if not isinstance(replaced_message_id, str) or not replaced_message_id:
            raise ValueError(
                "Replay Job 缺少 replaced_message_id: "
                f"session_id={session_id}, job_id={event.job_id}"
            )
        truncated_epoch = metadata.get("turn_projection_epoch")
        if (
            isinstance(truncated_epoch, int)
            and not isinstance(truncated_epoch, bool)
            and truncated_epoch == self._store.projection_epoch(session_id)
        ):
            return []
        return self._store.visible_turn_ids_from_message(
            session_id,
            replaced_message_id,
        )

    def record_event(
        self,
        session_id: str,
        event: Event,
        *,
        source_offset: int | None = None,
    ) -> TurnDetailDTO | None:
        with self._locks[session_id]:
            return self._apply_event_unlocked(
                session_id,
                event,
                source_offset=source_offset,
            )

    def rebuild_from_events(
        self,
        session_id: str,
        events: list[Event],
        *,
        destructive: bool = False,
    ) -> int:
        with self._locks[session_id]:
            if destructive:
                publication_base = self._store.publication_watermark(session_id)
                staging = self._store.create_rebuild_staging(session_id)
                staging_projector = TurnHistoryProjector(
                    staging,
                    trace_mapper=self._trace_mapper,
                )
                for event in events:
                    staging_projector._apply_event_unlocked(
                        session_id,
                        event,
                        source_offset=None,
                    )
                if publication_base.event_id is not None:
                    staging.advance_event_cursor(
                        session_id,
                        publication_base.event_id,
                        source_offset=publication_base.source_offset,
                    )
                staging.set_projection_status(session_id, "ready")
                staging.mark_history_initialized(
                    session_id,
                    projection_version=CURRENT_TURN_PROJECTION_VERSION,
                )
                self._store.publish_staging(
                    session_id,
                    staging,
                    publication_base=publication_base,
                )
                return self._store.turn_count(session_id)
            for event in events:
                self._apply_event_unlocked(session_id, event, source_offset=None)
            return self._store.turn_count(session_id)

    def synchronize(
        self,
        session_id: str,
        operation: Callable[[], _ResultT],
    ) -> _ResultT:
        """把 Trace 水位捕获与投影写入纳入实时投影的同一临界区。"""
        with self._locks[session_id]:
            return operation()

    def _apply_merge(
        self,
        session_id: str,
        event: Event,
        *,
        source_offset: int | None,
    ) -> TurnDetailDTO:
        if event.type != "job_merged":
            raise TypeError(f"不是 job_merged 事件: {event.type}")
        execution = self._store.get_turn(session_id, event.job_id)
        if execution is None:
            raise RuntimeError(
                "合并事件缺少实际执行 Turn: "
                f"session_id={session_id}, turn_id={event.job_id}"
            )
        merged_turns: list[TurnDetailDTO] = []
        for merged_job_id in event.payload.merged_job_ids:
            merged = self._store.get_turn(session_id, merged_job_id)
            if merged is None:
                raise RuntimeError(
                    "合并事件缺少来源 Turn: "
                    f"session_id={session_id}, merged_job_id={merged_job_id}"
                )
            merged_turns.append(merged)

        messages_by_id = {
            message.message_id: message
            for turn in [execution, *merged_turns]
            for message in turn.user_messages
        }
        missing_message_ids = [
            message_id
            for message_id in event.payload.source_message_ids
            if message_id not in messages_by_id
        ]
        if missing_message_ids:
            raise RuntimeError(
                "合并事件来源消息尚未投影: "
                f"session_id={session_id}, missing={missing_message_ids}"
            )
        item = map_turn_item(self._trace_mapper, session_id, event)
        updated = TurnDetailDTO.model_validate(
            {
                **execution.model_dump(mode="python"),
                "revision": execution.revision + 1,
                "updated_at": event.timestamp,
                "source_message_ids": list(event.payload.source_message_ids),
                "merged_job_ids": [
                    *execution.merged_job_ids,
                    *(
                        job_id
                        for job_id in event.payload.merged_job_ids
                        if job_id not in execution.merged_job_ids
                    ),
                ],
                "user_messages": [
                    messages_by_id[message_id]
                    for message_id in event.payload.source_message_ids
                ],
                "items": [*execution.items, *([item] if item is not None else [])],
            }
        )
        applied = self._store.apply_operation(
            session_id,
            TurnProjectionOperation(
                event_id=event.event_id,
                source_event_offset=source_offset,
                mutations=[build_turn_mutation(execution, updated, item)],
                hidden_turn_ids=list(event.payload.merged_job_ids),
            ),
        )
        if applied:
            return updated
        current = self._store.get_turn(session_id, event.job_id)
        if current is None:
            raise RuntimeError(
                f"Turn merge 重放后目标不存在: session_id={session_id}, turn_id={event.job_id}"
            )
        return current

    def _new_turn(self, session_id: str, event: Event) -> TurnDetailDTO:
        return TurnDetailDTO(
            turn_id=event.job_id,
            job_id=event.job_id,
            session_id=session_id,
            ordinal=self._store.next_ordinal(session_id),
            revision=1,
            status=JobStatus.accepted,
            created_at=event.timestamp,
            updated_at=event.timestamp,
        )

    def _message_from_job_created(self, event: Event) -> TurnUserMessageDTO | None:
        if event.type != "job_created":
            return None
        message_id = event.payload.message_id or f"legacy:{event.job_id}"
        return TurnUserMessageDTO(
            message_id=message_id,
            content=event.payload.message,
            attachments=turn_attachments(event.payload.attachments),
            metadata=display_metadata(event.payload.message_metadata),
            created_at=event.payload.message_created_at or event.timestamp,
        )

    def _message_from_created_event(self, event: Event) -> TurnUserMessageDTO:
        if event.type != "message_created":
            raise TypeError(f"不是 message_created 事件: {event.type}")
        return TurnUserMessageDTO(
            message_id=event.payload.message_id,
            content=event.payload.content,
            attachments=turn_attachments(event.payload.attachments),
            metadata=display_metadata(event.payload.metadata),
            created_at=event.payload.created_at,
        )

    @staticmethod
    def _merge_user_message(
        turn: TurnDetailDTO,
        message: TurnUserMessageDTO,
    ) -> dict[str, object]:
        legacy_message_id = f"legacy:{turn.job_id}"
        replaced_message_ids = {message.message_id}
        if message.message_id != legacy_message_id:
            replaced_message_ids.add(legacy_message_id)
        messages = [
            existing
            for existing in turn.user_messages
            if existing.message_id not in replaced_message_ids
        ]
        messages.append(message)
        source_ids = [
            source_id
            for source_id in turn.source_message_ids
            if source_id not in replaced_message_ids
        ]
        source_ids.append(message.message_id)
        return {
            "user_messages": messages,
            "source_message_ids": source_ids,
        }
