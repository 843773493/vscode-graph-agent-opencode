from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.core.path_utils import get_session_path_resolver
from app.schemas.event import (
    Event,
    JobCompletedEvent,
    JobCompletedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    JobMergedEvent,
    JobMergedPayload,
    JobStartedEvent,
    JobStartedPayload,
    MessageCreatedEvent,
    MessageCreatedPayload,
    TextDeltaEvent,
    TextDeltaPayload,
    TextEndEvent,
    TextEndPayload,
    TextStartEvent,
    TextStartPayload,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from app.services.business.session_turn_history import TurnHistoryProjector
from app.services.infrastructure.trace_event_store import (
    MESSAGE_TRACE_TYPES,
    TraceEventStore,
)
from app.services.infrastructure.turn_history import TurnHistoryStore

LONG_SESSION_TURN_COUNT = 48
LONG_SESSION_TRACE_DELTAS_PER_TURN = 24
INLINE_DATA_URL_PAYLOAD_BYTES = 512 * 1024
_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def build_large_markdown(marker: str, *, sections: int = 180) -> str:
    """生成包含常见重型语法且完全确定的 Markdown。"""

    chunks = [f"# {marker}\n"]
    for index in range(sections):
        chunks.append(
            "\n".join(
                (
                    f"## 章节 {index:03d}",
                    "| 字段 | 值 | 说明 |",
                    "| --- | --- | --- |",
                    f"| marker | `{marker}` | 第 {index:03d} 段 |",
                    "| 状态 | complete | 用于渐进 Markdown 渲染验收 |",
                    "",
                    "```python",
                    f"result_{index:03d} = {index} * {index}",
                    "```",
                    "",
                    f"- 列表项 A-{index:03d}",
                    f"- 列表项 B-{index:03d}",
                )
            )
        )
    return "\n".join(chunks)


def build_turn_events(
    *,
    session_id: str,
    turn_index: int,
    large_markdown: bool = False,
    trace_delta_count: int = LONG_SESSION_TRACE_DELTAS_PER_TURN,
    include_text_start: bool = True,
) -> list[Event]:
    """生成一个完整 Job Turn，包含输入、工具、文本和终态。"""

    ordinal = turn_index + 1
    job_id = f"job_turn_e2e_{ordinal:04d}"
    message_id = f"msg_turn_e2e_{ordinal:04d}"
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=ordinal * 10)
    user_marker = f"TURN-E2E-{ordinal:04d}-USER"
    user_content = (
        user_marker + "\n" + "大型用户输入。" * 80_000
        if large_markdown
        else user_marker
    )
    inline_data_url = (
        "data:image/png;base64," + "A" * INLINE_DATA_URL_PAYLOAD_BYTES
        if large_markdown
        else None
    )
    attachments = (
        [
            {
                "file_id": (
                    f"boxteam-session://{session_id}/attachments/turn-{ordinal:04d}.png"
                ),
                "name": f"turn-{ordinal:04d}.png",
                "content_type": "image/png",
                "data_url": inline_data_url,
            }
        ]
        if inline_data_url is not None
        else []
    )
    response_marker = f"TURN-E2E-{ordinal:04d}-FINAL"
    final_response = (
        build_large_markdown(response_marker)
        if large_markdown
        else f"{response_marker}\n\n- 完整 Job Turn\n- ordinal: {ordinal}"
    )
    part_id = f"part_turn_e2e_{ordinal:04d}"
    tool_part_id = f"part_tool_e2e_{ordinal:04d}"
    execution_id = f"exec_turn_e2e_{ordinal:04d}"

    events: list[Event] = [
        JobCreatedEvent(
            event_id=f"evt_job_created_e2e_{ordinal:04d}",
            job_id=job_id,
            timestamp=timestamp,
            payload=JobCreatedPayload(
                session_id=session_id,
                message=user_content,
                agent_id="default",
            ),
        ),
        MessageCreatedEvent(
            event_id=f"evt_message_e2e_{ordinal:04d}",
            job_id=job_id,
            timestamp=timestamp + timedelta(milliseconds=1),
            payload=MessageCreatedPayload(
                message_id=message_id,
                session_id=session_id,
                role="user",
                content=user_content,
                attachments=attachments,
                metadata={
                    "job_id": job_id,
                    "fixture_ordinal": ordinal,
                    "inline_data_url": inline_data_url,
                },
                created_at=timestamp,
            ),
        ),
        JobStartedEvent(
            event_id=f"evt_job_started_e2e_{ordinal:04d}",
            job_id=job_id,
            timestamp=timestamp + timedelta(milliseconds=2),
            payload=JobStartedPayload(),
        ),
        ToolCallStartEvent(
            event_id=f"evt_tool_start_e2e_{ordinal:04d}",
            part_id=tool_part_id,
            job_id=job_id,
            timestamp=timestamp + timedelta(milliseconds=3),
            payload=ToolCallStartPayload(
                execution_id=execution_id,
                tool_name="read",
                args={"path": f"fixture/{ordinal:04d}.md"},
                agent_id="default",
            ),
        ),
        ToolCallEndEvent(
            event_id=f"evt_tool_end_e2e_{ordinal:04d}",
            part_id=tool_part_id,
            job_id=job_id,
            timestamp=timestamp + timedelta(milliseconds=4),
            payload=ToolCallEndPayload(
                execution_id=execution_id,
                tool_call_id=execution_id,
                tool_name="read",
                result=f"fixture result {ordinal:04d}",
                status="success",
                agent_id="default",
            ),
        ),
    ]
    if include_text_start:
        events.append(
            TextStartEvent(
                event_id=f"evt_text_start_e2e_{ordinal:04d}",
                part_id=part_id,
                job_id=job_id,
                timestamp=timestamp + timedelta(milliseconds=5),
                payload=TextStartPayload(kind="markdown"),
            )
        )
    for delta_index in range(trace_delta_count):
        events.append(
            TextDeltaEvent(
                event_id=f"evt_delta_e2e_{ordinal:04d}_{delta_index:04d}",
                part_id=part_id,
                job_id=job_id,
                timestamp=timestamp + timedelta(milliseconds=6 + delta_index),
                payload=TextDeltaPayload(
                    text=f"delta-{ordinal:04d}-{delta_index:04d}",
                    kind="markdown",
                ),
            )
        )
    events.extend(
        [
            TextEndEvent(
                event_id=f"evt_text_end_e2e_{ordinal:04d}",
                part_id=part_id,
                job_id=job_id,
                timestamp=timestamp + timedelta(milliseconds=100),
                payload=TextEndPayload(kind="markdown", text=final_response),
            ),
            JobCompletedEvent(
                event_id=f"evt_job_completed_e2e_{ordinal:04d}",
                job_id=job_id,
                timestamp=timestamp + timedelta(milliseconds=101),
                payload=JobCompletedPayload(result=final_response),
            ),
        ]
    )
    return events


def write_latest_turn_attachment(
    *,
    workspace_root: Path,
    session_id: str,
    turn_count: int,
) -> Path:
    session_node = get_session_path_resolver(
        workspace_root / ".boxteam" / "sessions"
    ).resolve_session_node(session_id)
    attachment = session_node / "attachments" / f"turn-{turn_count:04d}.png"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_bytes(_ONE_PIXEL_PNG)
    return attachment


def build_long_session_events(
    *,
    session_id: str,
    turn_count: int = LONG_SESSION_TURN_COUNT,
    text_end_only_turn_indexes: set[int] | None = None,
) -> list[Event]:
    events: list[Event] = []
    text_end_only = text_end_only_turn_indexes or set()
    for turn_index in range(turn_count):
        events.extend(
            build_turn_events(
                session_id=session_id,
                turn_index=turn_index,
                large_markdown=turn_index == turn_count - 1,
                include_text_start=turn_index not in text_end_only,
            )
        )
    return events


def build_steering_merge_events(*, session_id: str) -> list[Event]:
    """生成三个 steering Job 被合并为一个实际执行 Turn 的事件序列。"""

    source_events = [
        build_turn_events(
            session_id=session_id,
            turn_index=index,
            trace_delta_count=0,
        )
        for index in range(3)
    ]
    execution_job_id = "job_turn_e2e_0001"
    merged_job_ids = ["job_turn_e2e_0002", "job_turn_e2e_0003"]
    source_message_ids = [
        "msg_turn_e2e_0001",
        "msg_turn_e2e_0002",
        "msg_turn_e2e_0003",
    ]
    merge_timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=35)
    return [
        *(event for events in source_events for event in events[:2]),
        JobMergedEvent(
            event_id="evt_job_merged_e2e_0001",
            job_id=execution_job_id,
            timestamp=merge_timestamp,
            payload=JobMergedPayload(
                session_id=session_id,
                merged_job_ids=merged_job_ids,
                source_message_ids=source_message_ids,
            ),
        ),
        *source_events[0][2:],
    ]


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
    store.mark_history_initialized(session_id)
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


def seed_compactable_checkpoint(
    *,
    workspace_root: Path,
    session_id: str,
    pair_count: int = 32,
) -> FileSystemCheckpointSaver:
    """写入明显超过普通测试长度、可被压缩的模型上下文。"""

    saver = FileSystemCheckpointSaver(
        sessions_dir=workspace_root / ".boxteam" / "sessions"
    )
    messages = []
    for index in range(pair_count):
        ordinal = index + 1
        created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
            seconds=ordinal * 10
        )
        messages.extend(
            (
                HumanMessage(
                    id=f"msg_compact_user_{index:04d}",
                    content=(
                        f"压缩夹具用户消息 {index:04d}。"
                        + "需要保留的长上下文资料。" * 160
                    ),
                    response_metadata={
                        "message_id": f"msg_turn_e2e_{ordinal:04d}",
                        "created_at": created_at.isoformat(),
                        "updated_at": created_at.isoformat(),
                        "message_metadata": {
                            "job_id": f"job_turn_e2e_{ordinal:04d}"
                        },
                    },
                ),
                AIMessage(
                    id=f"msg_compact_ai_{index:04d}",
                    content=(
                        f"压缩夹具助手消息 {index:04d}。"
                        + "这是长上下文的处理结果。" * 160
                    ),
                    response_metadata={
                        "message_id": f"msg_compact_ai_{index:04d}",
                        "created_at": (created_at + timedelta(seconds=1)).isoformat(),
                        "updated_at": (created_at + timedelta(seconds=1)).isoformat(),
                        "message_metadata": {},
                    },
                ),
            )
        )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = str(uuid4())
    version = saver.get_next_version(None, None)
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": version}
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        build_checkpoint_config(session_id),
        checkpoint,
        metadata={"source": "turn_history_e2e", "step": -1, "writes": {}},
        new_versions={"messages": version},
    )
    return saver


def replace_with_compacted_checkpoint(
    *,
    saver: FileSystemCheckpointSaver,
    session_id: str,
) -> str:
    """模拟压缩完成后的 checkpoint 变更，并返回新的 checkpoint ID。"""

    checkpoint_tuple = saver.get_tuple(build_checkpoint_config(session_id))
    if checkpoint_tuple is None:
        raise RuntimeError(f"压缩夹具 checkpoint 不存在: session_id={session_id}")
    checkpoint = checkpoint_tuple.checkpoint.copy()
    checkpoint_id = str(uuid4())
    checkpoint["id"] = checkpoint_id
    channel_values = dict(checkpoint.get("channel_values", {}))
    original_messages = list(channel_values.get("messages", []))
    channel_values["messages"] = [
        original_messages[0],
        SystemMessage(
            id="msg_compaction_summary",
            content="压缩摘要：保留首轮稳定前缀，并汇总中间长上下文。",
            additional_kwargs={"lc_source": "summarization"},
        ),
        original_messages[-1],
    ]
    channel_values["_summarization_event"] = {
        "strategy": "cache_preserving",
        "cutoff_index": max(1, len(original_messages) - 1),
    }
    checkpoint["channel_values"] = channel_values
    versions = dict(checkpoint.get("channel_versions", {}))
    version = saver.get_next_version(versions.get("messages"), None)
    versions["messages"] = version
    checkpoint["channel_versions"] = versions
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        checkpoint_tuple.config,
        checkpoint,
        metadata={"source": "turn_history_e2e_compacted", "step": -1, "writes": {}},
        new_versions={"messages": version},
    )
    return checkpoint_id
