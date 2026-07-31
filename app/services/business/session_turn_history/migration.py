from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.abstractions.turn_history import (
    TurnHistoryEventSourceProtocol,
    TurnHistoryStoreProtocol,
    TurnLegacyMessageSourceProtocol,
    TurnMigrationSnapshot,
    TurnProjectionPublicationConflict,
    TurnProjectionWatermark,
    TurnProjectorFactoryProtocol,
)
from app.schemas.event import (
    JobCompletedEvent,
    JobCompletedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
)
from app.schemas.public_v2.common import MessageRole
from app.schemas.public_v2.message import MessageDTO


class SessionTurnHistoryMigrator:
    """在独立 staging 中合成旧 checkpoint 与 Trace 的完整 Turn 基线。"""

    def __init__(
        self,
        *,
        store: TurnHistoryStoreProtocol,
        trace_event_store: TurnHistoryEventSourceProtocol,
        legacy_message_source: TurnLegacyMessageSourceProtocol,
        staging_store_factory: Callable[[], TurnHistoryStoreProtocol],
        projector_factory: TurnProjectorFactoryProtocol,
    ) -> None:
        self._store = store
        self._trace_event_store = trace_event_store
        self._legacy_message_source = legacy_message_source
        self._staging_store_factory = staging_store_factory
        self._projector_factory = projector_factory
        self._locks: dict[str, asyncio.Lock] = {}

    def has_checkpoint_history(self, session_id: str) -> bool:
        """供有界 bootstrap 判断是否需要在后台补齐旧 checkpoint。"""
        return self._legacy_message_source.has_checkpoint_history(session_id)

    async def complete(self, session_id: str) -> None:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            status = await asyncio.to_thread(
                self._store.projection_status,
                session_id,
            )
            if status == "ready":
                return
            publication_base = TurnProjectionWatermark(
                event_id=await asyncio.to_thread(
                    self._store.event_cursor,
                    session_id,
                ),
                source_offset=await asyncio.to_thread(
                    self._store.event_offset,
                    session_id,
                ),
            )
            await asyncio.to_thread(
                self._trace_event_store.ensure_turn_index,
                session_id,
            )
            snapshot = await asyncio.to_thread(
                self._trace_event_store.capture_turn_migration_snapshot,
                session_id,
            )
            legacy_messages = await self._load_legacy_messages(session_id)
            staging = self._staging_store_factory()
            try:
                await asyncio.to_thread(
                    self._build_staging_projection,
                    session_id,
                    staging,
                    snapshot,
                    legacy_messages,
                )
                try:
                    await asyncio.to_thread(
                        self._store.publish_staging,
                        session_id,
                        staging,
                        publication_base=publication_base,
                    )
                except TurnProjectionPublicationConflict:
                    await asyncio.to_thread(
                        staging.discard_projection,
                        session_id,
                    )
            except Exception as error:
                await asyncio.to_thread(
                    staging.discard_projection,
                    session_id,
                )
                await asyncio.to_thread(
                    self._store.set_projection_status,
                    session_id,
                    "failed",
                    error=str(error),
                )
                raise

    async def _load_legacy_messages(self, session_id: str) -> list[MessageDTO]:
        has_history = await asyncio.to_thread(self.has_checkpoint_history, session_id)
        if not has_history:
            return []
        return await self._legacy_message_source.list_visible_messages_for_turn_migration(
            session_id
        )

    def _build_staging_projection(
        self,
        session_id: str,
        staging: TurnHistoryStoreProtocol,
        snapshot: TurnMigrationSnapshot,
        legacy_messages: list[MessageDTO],
    ) -> None:
        staging.discard_projection(session_id)
        projector = self._projector_factory(staging)
        legacy_events = self._legacy_events(
            session_id,
            legacy_messages,
            snapshot,
        )
        legacy_index = 0
        last_event_id: str | None = None
        for event in self._trace_event_store.iter_message_events(
            session_id,
            before_offset=snapshot.message_trace_size,
        ):
            while (
                legacy_index < len(legacy_events)
                and legacy_events[legacy_index].timestamp <= event.timestamp
            ):
                projector.record_event(session_id, legacy_events[legacy_index])
                legacy_index += 1
            projector.record_event(session_id, event)
            last_event_id = event.event_id
        for event in legacy_events[legacy_index:]:
            projector.record_event(session_id, event)
        event_cursor = snapshot.event_cursor or last_event_id
        source_offset = snapshot.projected_event_offset or (
            snapshot.message_trace_size if last_event_id is not None else None
        )
        if event_cursor is not None and source_offset is not None:
            staging.advance_event_cursor(
                session_id,
                event_cursor,
                source_offset=source_offset,
            )
        else:
            staging.projection_epoch(session_id)
        staging.set_projection_status(session_id, "ready")
        staging.mark_history_initialized(session_id)

    def _legacy_events(
        self,
        session_id: str,
        messages: list[MessageDTO],
        snapshot: TurnMigrationSnapshot,
    ) -> list[JobCreatedEvent | JobCompletedEvent]:
        trace_job_id_by_message_id: dict[str, str] = {}
        trace_job_ids: set[str] = set()
        trace_execution_job_id: dict[str, str] = {}
        trace_terminal_job_ids: set[str] = set()
        for event in self._trace_event_store.iter_message_events(
            session_id,
            before_offset=snapshot.message_trace_size,
        ):
            if event.type == "job_created":
                trace_job_ids.add(event.job_id)
                if event.payload.message_id:
                    trace_job_id_by_message_id[event.payload.message_id] = event.job_id
            elif event.type == "job_merged":
                for merged_job_id in event.payload.merged_job_ids:
                    trace_execution_job_id[merged_job_id] = event.job_id
                for message_id in event.payload.source_message_ids:
                    trace_job_id_by_message_id[message_id] = event.job_id
            if event.type in {
                "job_completed",
                "job_failed",
                "job_cancelled",
                "session_interrupted",
            } or (
                event.type == "error"
                and event.payload.phase == "agent_execution"
            ):
                trace_terminal_job_ids.add(event.job_id)

        def execution_job_id(job_id: str) -> str:
            seen: set[str] = set()
            current = job_id
            while current in trace_execution_job_id:
                if current in seen:
                    raise RuntimeError(
                        "Trace job_merged 形成环: "
                        f"session_id={session_id}, job_id={job_id}"
                    )
                seen.add(current)
                current = trace_execution_job_id[current]
            return current

        events: list[JobCreatedEvent | JobCompletedEvent] = []
        active_job_id: str | None = None
        for message in messages:
            if message.role == MessageRole.user:
                metadata_job_id = message.metadata.get("job_id")
                candidate_job_id = (
                    metadata_job_id
                    if isinstance(metadata_job_id, str) and metadata_job_id
                    else f"legacy:{message.message_id}"
                )
                traced_job_id = trace_job_id_by_message_id.get(message.message_id)
                if traced_job_id is not None:
                    active_job_id = execution_job_id(traced_job_id)
                    continue
                if candidate_job_id in trace_job_ids:
                    active_job_id = execution_job_id(candidate_job_id)
                    continue
                active_job_id = candidate_job_id
                events.append(
                    JobCreatedEvent(
                        event_id=f"legacy:created:{message.message_id}",
                        job_id=active_job_id,
                        timestamp=message.created_at,
                        payload=JobCreatedPayload(
                            session_id=session_id,
                            message=message.content,
                            agent_id="default",
                            message_id=message.message_id,
                            attachments=[
                                attachment.model_dump(mode="python")
                                for attachment in message.attachments
                            ],
                            message_created_at=message.created_at,
                            message_metadata=message.metadata,
                        ),
                    ),
                )
                continue
            if message.role != MessageRole.assistant or active_job_id is None:
                continue
            if active_job_id in trace_terminal_job_ids:
                active_job_id = None
                continue
            events.append(
                JobCompletedEvent(
                    event_id=f"legacy:completed:{message.message_id}",
                    job_id=active_job_id,
                    timestamp=message.created_at,
                    payload=JobCompletedPayload(result=message.content),
                ),
            )
            active_job_id = None
        return events
