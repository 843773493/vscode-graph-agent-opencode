from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any

from langchain_core.messages import ToolMessage

from app.core.job_context import set_active_tool_name, set_interruptible_phase
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.agents.tool_identity import CUSTOM_TOOL_INVOKER_NAME
from app.agents.tools.apply_patch import (
    APPLY_PATCH_TOOL_NAME,
    load_apply_patch_journal_from_result,
)
from app.services.mapping.agent_content_mapper import (
    AgentStreamContentPart,
    extract_agent_stream_content_parts,
)
from app.abstractions.session_changes import (
    FileEditSnapshot,
    SessionChangesRecorderProtocol,
    StoredFileEdit,
)
from app.services.orchestration.agent_stream_helpers import (
    extract_tool_result_text,
    is_tracked_chat_model_event,
    normalize_tool_args,
)
from app.services.infrastructure.tool_output_store import (
    ToolOutputStore,
    extract_tool_output_reference,
)
from app.schemas.event import ModelTokenUsagePayload


FILE_EDIT_TOOL_NAMES = {"write_file", "edit_file", APPLY_PATCH_TOOL_NAME}
TEXT_DELTA_FLUSH_CHARS = 128
TEXT_DELTA_FLUSH_SECONDS = 0.04


@dataclass(frozen=True, slots=True)
class AgentEventStreamResult:
    final_text: str
    final_text_part_id: str | None
    latest_model_content_blocks: tuple[dict[str, object], ...]
    last_tool_result_text: str
    completed_custom_tool_names: tuple[str, ...]
    token_usage: ModelTokenUsagePayload = field(
        default_factory=ModelTokenUsagePayload
    )


@dataclass(frozen=True, slots=True)
class ToolEventDisplayContext:
    tool_name: str
    tool_args: dict[str, Any]
    invocation_tool_name: str | None


def _usage_token_count(
    usage: Mapping[str, object],
    key: str,
) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"模型 usage_metadata.{key} 必须是非负整数，实际值: {value!r}")
    return value


def _stream_chunk_token_usage(chunk: object) -> ModelTokenUsagePayload | None:
    message = getattr(chunk, "message", None)
    usage_metadata = getattr(message or chunk, "usage_metadata", None)
    if usage_metadata is None:
        return None
    if not isinstance(usage_metadata, Mapping):
        raise TypeError(
            "模型 chunk usage_metadata 必须是 mapping，"
            f"实际类型: {type(usage_metadata).__name__}"
        )

    raw_input_details = usage_metadata.get("input_token_details")
    cache_read_input_tokens: int | None = None
    if raw_input_details is not None:
        if not isinstance(raw_input_details, Mapping):
            raise TypeError(
                "模型 usage_metadata.input_token_details 必须是 mapping，"
                f"实际类型: {type(raw_input_details).__name__}"
            )
        if "cache_read" in raw_input_details:
            cache_read_input_tokens = _usage_token_count(
                raw_input_details,
                "cache_read",
            )

    return ModelTokenUsagePayload(
        input_tokens=_usage_token_count(usage_metadata, "input_tokens"),
        output_tokens=_usage_token_count(usage_metadata, "output_tokens"),
        total_tokens=_usage_token_count(usage_metadata, "total_tokens"),
        cache_read_input_tokens=cache_read_input_tokens,
        model_calls=1,
        reported_model_calls=1,
    )


def combine_model_token_usage(
    usages: Sequence[ModelTokenUsagePayload],
) -> ModelTokenUsagePayload:
    model_calls = sum(usage.model_calls for usage in usages)
    reported_model_calls = sum(usage.reported_model_calls for usage in usages)
    cache_is_complete = (
        model_calls > 0
        and model_calls == reported_model_calls
        and all(
            usage.cache_read_input_tokens is not None
            for usage in usages
            if usage.model_calls > 0
        )
    )
    return ModelTokenUsagePayload(
        input_tokens=sum(usage.input_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        total_tokens=sum(usage.total_tokens for usage in usages),
        cache_read_input_tokens=(
            sum(usage.cache_read_input_tokens or 0 for usage in usages)
            if cache_is_complete
            else None
        ),
        model_calls=model_calls,
        reported_model_calls=reported_model_calls,
    )


def _event_run_id(event: dict[str, Any]) -> str:
    run_id = event.get("run_id")
    return run_id if isinstance(run_id, str) else ""


def _tool_output_succeeded(output: Any) -> bool:
    status = getattr(output, "status", None)
    if status == "error":
        return False
    if status == "success":
        return True
    text = extract_tool_result_text(output).strip()
    return bool(text) and not text.startswith("Error:")


def _file_paths_from_tool_args(tool_name: str, tool_args: dict[str, Any]) -> list[str]:
    if tool_name not in FILE_EDIT_TOOL_NAMES:
        return []
    if tool_name == APPLY_PATCH_TOOL_NAME:
        return []
    value = tool_args.get("file_path")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    raise RuntimeError(f"{tool_name} 工具缺少 file_path，无法记录文件变更")


def _stored_edit_payload(record: StoredFileEdit) -> dict[str, object]:
    return {
        "edit_id": record.edit_id,
        "file_path": record.file_path,
        "kind": record.kind,
        "additions": record.additions,
        "deletions": record.deletions,
        "diff_file": record.diff_file,
        "before_file": record.before_file,
        "after_file": record.after_file,
    }


def _apply_patch_snapshots_from_result(
    *,
    result_text: str,
    session_changes_service: SessionChangesRecorderProtocol,
    workspace_root: Path,
) -> list[FileEditSnapshot]:
    snapshots: list[FileEditSnapshot] = []
    for raw_snapshot in load_apply_patch_journal_from_result(
        result_text,
        workspace_root=workspace_root,
    ):
        file_path = raw_snapshot.get("file_path")
        before_exists = raw_snapshot.get("before_exists")
        before_content = raw_snapshot.get("before_content")
        if not isinstance(file_path, str):
            raise RuntimeError("apply_patch journal 快照缺少 file_path")
        if not isinstance(before_exists, bool):
            raise RuntimeError(f"apply_patch journal 快照 {file_path} 缺少 before_exists")
        if not isinstance(before_content, str):
            raise RuntimeError(f"apply_patch journal 快照 {file_path} 缺少 before_content")
        snapshots.append(
            session_changes_service.build_snapshot(
                file_path=file_path,
                existed=before_exists,
                content=before_content,
            )
        )
    return snapshots


def _build_tool_display_context(
    *,
    raw_tool_name: str,
    raw_tool_args: dict[str, Any],
) -> ToolEventDisplayContext:
    if raw_tool_name == CUSTOM_TOOL_INVOKER_NAME:
        target_tool_name = raw_tool_args.get("tool_name")
        if isinstance(target_tool_name, str) and target_tool_name.strip():
            return ToolEventDisplayContext(
                tool_name=target_tool_name.strip(),
                tool_args=normalize_tool_args(raw_tool_args.get("arguments")),
                invocation_tool_name=raw_tool_name,
            )

    return ToolEventDisplayContext(
        tool_name=raw_tool_name,
        tool_args=raw_tool_args,
        invocation_tool_name=None,
    )


async def process_agent_event_stream(
    *,
    agent: Any,
    input_payload: dict[str, Any],
    config: dict[str, Any],
    session_id: str,
    turn_id: str,
    agent_id: str,
    custom_tool_skill_sources: dict[str, list[str]],
    publish: Callable[[str, dict[str, Any]], Awaitable[None]],
    session_changes_service: SessionChangesRecorderProtocol,
    workspace_root: Path,
) -> AgentEventStreamResult:
    """消费 DeepAgent 事件流，并发布前端可观察的 trace 事件。"""
    collected_text_parts: list[str] = []
    latest_model_part_order: list[str] = []
    latest_model_parts: dict[str, dict[str, object]] = {}
    current_text_part_id: str | None = None
    current_text_part_kind: str | None = None
    current_text_part_chunks: list[str] = []
    pending_text_delta_chunks: list[str] = []
    last_text_delta_flush_at = time.monotonic()
    tool_contexts_by_run_id: dict[str, ToolEventDisplayContext] = {}
    file_edit_snapshots_by_run_id: dict[str, list[FileEditSnapshot]] = {}
    last_tool_result_text = ""
    completed_custom_tool_names: list[str] = []
    tracked_model_run_ids: set[str] = set()
    model_usage_by_run_id: dict[str, ModelTokenUsagePayload] = {}
    tool_output_store = ToolOutputStore(workspace_root=workspace_root)

    async def flush_text_delta() -> None:
        nonlocal pending_text_delta_chunks, last_text_delta_flush_at
        if current_text_part_id is None or current_text_part_kind is None:
            return
        text = "".join(pending_text_delta_chunks)
        if not text:
            return
        await publish(
            EventType.TEXT_DELTA,
            {
                "part_id": current_text_part_id,
                "kind": current_text_part_kind,
                "text": text,
            },
        )
        pending_text_delta_chunks = []
        last_text_delta_flush_at = time.monotonic()

    async def close_current_text_part() -> None:
        nonlocal current_text_part_id, current_text_part_kind, current_text_part_chunks
        nonlocal pending_text_delta_chunks
        if current_text_part_id is None or current_text_part_kind is None:
            return
        await flush_text_delta()
        await publish(
            EventType.TEXT_END,
            {
                "part_id": current_text_part_id,
                "kind": current_text_part_kind,
                "text": "".join(current_text_part_chunks),
            },
        )
        current_text_part_id = None
        current_text_part_kind = None
        current_text_part_chunks = []
        pending_text_delta_chunks = []

    def record_latest_model_part(part: AgentStreamContentPart) -> None:
        existing = latest_model_parts.get(part.part_id)
        text_key = "reasoning" if part.block_type == "reasoning" else (
            "refusal" if part.block_type == "refusal" else "text"
        )
        if existing is None:
            latest_model_part_order.append(part.part_id)
            latest_model_parts[part.part_id] = {
                "type": part.block_type,
                text_key: part.text,
                "id": part.part_id,
                "index": part.index,
            }
            return
        if existing.get("type") != part.block_type or existing.get("index") != part.index:
            raise RuntimeError(
                f"模型流 part 身份冲突: part_id={part.part_id} "
                f"existing={existing!r} incoming={part!r}"
            )
        current_text = existing.get(text_key)
        if not isinstance(current_text, str):
            raise RuntimeError(f"模型流 part 缺少 {text_key}: part_id={part.part_id}")
        existing[text_key] = current_text + part.text

    async def publish_text_delta(part: AgentStreamContentPart) -> None:
        nonlocal current_text_part_id, current_text_part_kind, last_text_delta_flush_at
        if current_text_part_id != part.part_id:
            await close_current_text_part()
        if current_text_part_id is None:
            current_text_part_id = part.part_id
            current_text_part_kind = part.kind
            interrupt_state = SessionInterruptState.get(session_id)
            if not interrupt_state.active_tools_by_run_id:
                SessionInterruptState.set(session_id, phase="text", tool_name=None)
                set_interruptible_phase("text")
            await publish(
                EventType.TEXT_START,
                {"part_id": current_text_part_id, "kind": part.kind},
            )
        elif current_text_part_kind != part.kind:
            raise RuntimeError(
                f"模型流 part kind 发生变化: part_id={part.part_id} "
                f"{current_text_part_kind} -> {part.kind}"
            )
        record_latest_model_part(part)
        current_text_part_chunks.append(part.text)
        pending_text_delta_chunks.append(part.text)
        pending_length = sum(len(chunk) for chunk in pending_text_delta_chunks)
        if (
            pending_length >= TEXT_DELTA_FLUSH_CHARS
            or time.monotonic() - last_text_delta_flush_at >= TEXT_DELTA_FLUSH_SECONDS
        ):
            await flush_text_delta()

    async for event in agent.astream_events(input_payload, config=config, version="v2"):
        event_type = event.get("event")
        name = event.get("name", "")
        data = event.get("data", {})
        metadata = event.get("metadata", {})

        if event_type == "on_chat_model_start" and is_tracked_chat_model_event(name):
            await close_current_text_part()
            latest_model_part_order.clear()
            latest_model_parts.clear()
            model_run_id = _event_run_id(event)
            if model_run_id:
                tracked_model_run_ids.add(model_run_id)
            model_name = metadata.get("ls_model_name") or "unknown_model"
            await publish(
                EventType.LLM_REQUEST,
                {
                    "model": model_name,
                    "timestamp": int(time.time() * 1000),
                },
            )
            continue

        if event_type == "on_chat_model_stream" and is_tracked_chat_model_event(name):
            chunk = data.get("chunk")
            if chunk is None:
                continue
            chunk_token_usage = _stream_chunk_token_usage(chunk)
            if chunk_token_usage is not None:
                model_run_id = _event_run_id(event)
                if not model_run_id:
                    raise RuntimeError("带 usage_metadata 的模型流事件缺少 run_id")
                tracked_model_run_ids.add(model_run_id)
                model_usage_by_run_id[model_run_id] = chunk_token_usage
            chunk_message = getattr(chunk, "message", None)
            if chunk_message is not None:
                content = getattr(chunk_message, "content", None) or ""
                tool_calls = getattr(chunk_message, "tool_calls", None) or []
            else:
                content = getattr(chunk, "content", None) or ""
                tool_calls = getattr(chunk, "tool_calls", None) or []

            for part in extract_agent_stream_content_parts(content):
                if part.kind == "reasoning":
                    if not part.text.strip():
                        continue
                    SessionInterruptState.set(
                        session_id,
                        current_text="".join(collected_text_parts),
                    )
                    await publish_text_delta(part)
                    continue
                if part.text and (part.text.strip() or collected_text_parts):
                    collected_text_parts.append(part.text)
                    SessionInterruptState.set(
                        session_id,
                        current_text="".join(collected_text_parts),
                    )
                    await publish_text_delta(part)

            if tool_calls and (collected_text_parts or current_text_part_id is not None):
                await close_current_text_part()
                collected_text_parts.clear()
                SessionInterruptState.set(session_id, current_text="")
            continue

        if event_type == "on_tool_start":
            await close_current_text_part()
            if not name:
                raise RuntimeError("工具开始事件缺少 name")
            raw_tool_name = name
            raw_tool_args = normalize_tool_args(data.get("input"))
            display_context = _build_tool_display_context(
                raw_tool_name=raw_tool_name,
                raw_tool_args=raw_tool_args,
            )
            run_id = _event_run_id(event)
            if not run_id:
                raise RuntimeError(f"{display_context.tool_name} 工具开始事件缺少 run_id")
            if run_id in tool_contexts_by_run_id:
                raise RuntimeError(
                    f"工具开始事件使用了重复的 run_id: run_id={run_id} "
                    f"tool={display_context.tool_name}"
                )
            tool_contexts_by_run_id[run_id] = display_context
            file_paths = _file_paths_from_tool_args(
                display_context.tool_name,
                display_context.tool_args,
            )
            if file_paths:
                file_edit_snapshots_by_run_id[run_id] = [
                    session_changes_service.capture_before(file_path)
                    for file_path in file_paths
                ]
            skill_names = custom_tool_skill_sources.get(display_context.tool_name, [])
            interrupt_state = SessionInterruptState.start_tool(
                session_id,
                run_id=run_id,
                tool_name=display_context.tool_name,
            )
            set_interruptible_phase("tool")
            set_active_tool_name(interrupt_state.tool_name)
            payload: dict[str, object] = {
                "part_id": run_id,
                "execution_id": run_id,
                "tool_name": display_context.tool_name,
                "args": display_context.tool_args,
                "agent_id": agent_id,
            }
            if display_context.invocation_tool_name:
                payload["invocation_tool_name"] = display_context.invocation_tool_name
            if skill_names:
                payload["skill_names"] = skill_names
            await publish(
                EventType.TOOL_CALL_START,
                payload,
            )
            continue

        if event_type == "on_tool_end":
            run_id = _event_run_id(event)
            if not run_id:
                raise RuntimeError(f"{name or 'unknown_tool'} 工具结束事件缺少 run_id")
            display_context = tool_contexts_by_run_id.pop(run_id, None)
            if display_context is None:
                raise RuntimeError(
                    f"工具结束事件找不到对应的开始事件: run_id={run_id} "
                    f"tool={name or 'unknown_tool'}"
                )
            raw_output = data.get("output")
            if not isinstance(raw_output, ToolMessage):
                raise TypeError(
                    "工具结束事件必须返回带 tool_call_id 的 ToolMessage: "
                    f"execution_id={run_id} tool={display_context.tool_name} "
                    f"output_type={type(raw_output).__name__}"
                )
            tool_call_id = raw_output.tool_call_id
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise RuntimeError(
                    "工具结束事件的 ToolMessage 缺少 tool_call_id: "
                    f"execution_id={run_id} tool={display_context.tool_name}"
                )
            raw_result_text = extract_tool_result_text(raw_output)
            output = raw_output
            if isinstance(raw_output, ToolMessage) and raw_output.status != "error":
                output = await tool_output_store.abound(
                    session_id=session_id,
                    tool_name=display_context.tool_name,
                    tool_call_id=raw_output.tool_call_id,
                    message=raw_output,
                )
            result_text = extract_tool_result_text(output)
            last_tool_result_text = result_text
            skill_names = custom_tool_skill_sources.get(display_context.tool_name, [])
            if display_context.invocation_tool_name == CUSTOM_TOOL_INVOKER_NAME:
                completed_custom_tool_names.append(display_context.tool_name)
            stored_edits: list[StoredFileEdit] = []
            if run_id:
                snapshots = file_edit_snapshots_by_run_id.pop(run_id, None)
                if (
                    snapshots is None
                    and display_context.tool_name == APPLY_PATCH_TOOL_NAME
                    and _tool_output_succeeded(raw_output)
                ):
                    snapshots = _apply_patch_snapshots_from_result(
                        result_text=raw_result_text,
                        session_changes_service=session_changes_service,
                        workspace_root=workspace_root,
                    )
                if snapshots is not None and _tool_output_succeeded(raw_output):
                    for snapshot in snapshots:
                        stored_edit = await session_changes_service.record_tool_file_edit(
                            session_id=session_id,
                            turn_id=turn_id,
                            tool_call_id=tool_call_id,
                            execution_id=run_id,
                            tool_name=display_context.tool_name,
                            before=snapshot,
                        )
                        if stored_edit is not None:
                            stored_edits.append(stored_edit)
            interrupt_state = SessionInterruptState.end_tool(session_id, run_id=run_id)
            if interrupt_state.active_tools_by_run_id:
                set_interruptible_phase("tool")
                set_active_tool_name(interrupt_state.tool_name)
            else:
                set_interruptible_phase("text")
                set_active_tool_name(None)
            payload = {
                "part_id": run_id,
                "execution_id": run_id,
                "tool_call_id": tool_call_id,
                "tool_name": display_context.tool_name,
                "result": result_text,
                "agent_id": agent_id,
            }
            tool_output_reference = extract_tool_output_reference(output)
            if tool_output_reference is not None:
                payload["tool_output"] = tool_output_reference
            if display_context.invocation_tool_name:
                payload["invocation_tool_name"] = display_context.invocation_tool_name
            if skill_names:
                payload["skill_names"] = skill_names
            if stored_edits:
                payload["file_edits"] = [
                    _stored_edit_payload(stored_edit)
                    for stored_edit in stored_edits
                ]
                if len(stored_edits) == 1:
                    payload["file_edit"] = _stored_edit_payload(stored_edits[0])
            await publish(
                EventType.TOOL_CALL_END,
                payload,
            )

    final_text_part_id = (
        current_text_part_id if current_text_part_kind == "markdown" else None
    )
    if current_text_part_kind == "reasoning":
        await close_current_text_part()
    else:
        await flush_text_delta()

    usage_parts = list(model_usage_by_run_id.values())
    missing_usage_calls = len(tracked_model_run_ids) - len(model_usage_by_run_id)
    if missing_usage_calls > 0:
        usage_parts.append(ModelTokenUsagePayload(model_calls=missing_usage_calls))

    return AgentEventStreamResult(
        final_text="".join(collected_text_parts).strip(),
        final_text_part_id=final_text_part_id,
        latest_model_content_blocks=tuple(
            latest_model_parts[part_id] for part_id in latest_model_part_order
        ),
        last_tool_result_text=last_tool_result_text,
        completed_custom_tool_names=tuple(completed_custom_tool_names),
        token_usage=combine_model_token_usage(usage_parts),
    )
