from __future__ import annotations

from pathlib import Path

from app.core.path_utils import get_session_path_resolver
from app.schemas.event import Event
from app.services.business.session_turn_history import (
    CURRENT_TURN_PROJECTION_VERSION,
    TurnHistoryProjector,
)
from app.services.infrastructure.trace_event_store import (
    MESSAGE_TRACE_TYPES,
    TraceEventStore,
)
from app.services.infrastructure.turn_history import TurnHistoryStore


def rebuild_turn_projection(
    *,
    store: TurnHistoryStore,
    session_id: str,
    events: list[Event],
    destructive: bool = False,
) -> int:
    """通过真实 projector 重建展示投影。"""

    turn_count = TurnHistoryProjector(store).rebuild_from_events(
        session_id,
        events,
        destructive=destructive,
    )
    semantic_events = [event for event in events if event.type in MESSAGE_TRACE_TYPES]
    if semantic_events:
        store.advance_event_cursor(
            session_id,
            semantic_events[-1].event_id,
            source_offset=sum(
                len(event.model_dump_json().encode("utf-8")) + 1
                for event in semantic_events
            ),
        )
    store.set_projection_status(session_id, "ready")
    store.mark_history_initialized(
        session_id,
        projection_version=CURRENT_TURN_PROJECTION_VERSION,
    )
    return turn_count


def write_trace_fixture(
    *,
    workspace_root: Path,
    session_id: str,
    events: list[Event],
    build_turn_index: bool = True,
) -> Path:
    """一次性写入正式会话 Trace，避免夹具逐事件线程切换。"""

    session_node = get_session_path_resolver(
        workspace_root / ".boxteam" / "sessions"
    ).resolve_session_node(session_id)
    trace_file = session_node / "logs" / "traces" / "events.jsonl"
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    trace_file.write_text(
        "".join(f"{event.model_dump_json()}\n" for event in events),
        encoding="utf-8",
    )
    message_trace_file = trace_file.with_name("messages.jsonl")
    message_trace_file.write_text(
        "".join(
            f"{event.model_dump_json()}\n"
            for event in events
            if event.type in MESSAGE_TRACE_TYPES
        ),
        encoding="utf-8",
    )
    if build_turn_index:
        TraceEventStore(
            workspace_root / ".boxteam" / "sessions"
        ).ensure_turn_index(session_id)
    return trace_file
