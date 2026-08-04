from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
