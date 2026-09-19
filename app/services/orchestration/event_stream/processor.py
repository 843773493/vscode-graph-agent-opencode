from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.tools import BaseTool

from app.abstractions.session_changes import (
    FileEditSnapshot,
    SessionChangesRecorderProtocol,
    StoredFileEdit,
)
from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.model_capability_routing import MODEL_FAILED_CUSTOM_EVENT
from app.agents.model_tool_schema import normalize_model_tool_arguments
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tools.apply_patch import APPLY_PATCH_TOOL_NAME
from app.core.job_context import set_active_tool_name, set_interruptible_phase
from app.core.job_event_bus import EventType
from app.core.model_delta_context import ModelRunIdentityCallbackHandler
from app.core.session_interrupt_state import SessionInterruptState
from app.core.turn_execution_scope import (
    CancellationSignal,
    ScopeCancelledError,
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.schemas.event import ModelTokenUsagePayload
from app.services.infrastructure.tool_output_store import (
    ToolOutputStore,
    extract_tool_output_reference,
)
from app.services.mapping.agent_content_mapper import (
    AgentStreamContentPart,
    extract_agent_stream_content_parts,
)
from app.services.orchestration.agent_stream_helpers import (
    extract_tool_result_text,
    is_tracked_chat_model_event,
    normalize_tool_args,
)
from app.services.orchestration.event_stream.contracts import (
    AgentEventSource,
    AgentEventStreamResult,
    AgentEventStreamTimeoutError,
    SuccessfulToolCall,
    ToolEventDisplayContext,
)
from app.services.orchestration.event_stream.identity import (
    build_isolated_stream_config,
    event_run_id,
    validate_stream_event_identity,
)
from app.services.orchestration.event_stream.model_events import (
    merge_model_content_part,
    stream_chunk_token_usage,
)
from app.services.orchestration.event_stream.reader import (
    iter_agent_events,
    resolve_event_timeouts,
)
from app.services.orchestration.event_stream.tool_events import (
    SUBAGENT_TOOL_NAMES,
    activity_result_detail,
    apply_patch_snapshots_from_result,
    build_tool_display_context,
    file_paths_from_tool_args,
    resource_activity_binding_from_metadata,
    stored_edit_payload,
    tool_message_from_output,
    tool_output_status,
    tool_output_succeeded,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


def _field_value(value: object, field_name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(field_name)
    return getattr(value, field_name, None)


def _model_output_content(output: object) -> object | None:
    """从 LangChain 的 model.end 输出中提取消息正文。

    流式 ChatModel 的 end 事件在不同 LangChain 版本和 provider 实现中可能
    携带 AIMessage、AIMessageChunk，或 ChatResult/字典形式的 generations。
    这些都是同一次模型调用的同一个事实来源，不能因为外层类型不同而丢弃。
    """
    content = _field_value(output, "content")
    if content is not None:
        return content
    generations = _field_value(output, "generations")
    if not isinstance(generations, (list, tuple)):
        return None
    for generation in generations:
        message = _field_value(generation, "message")
        content = _field_value(message, "content")
        if content is not None:
            return content
    return None


async def process_agent_event_stream(
    *,
    agent: AgentEventSource,
    input_payload: dict[str, Any],
    config: dict[str, Any],
    session_id: str,
    turn_id: str,
    agent_id: str,
    custom_tool_skill_sources: dict[str, list[str]],
    publish: Callable[[str, dict[str, Any]], Awaitable[None]],
    session_changes_service: SessionChangesRecorderProtocol,
    workspace_root: Path,
    message_stream_runtime: MessageStreamRuntime | None = None,
    cancellation_signal: CancellationSignal | None = None,
    execution_scope: TurnExecutionScope | None = None,
    model_timeout_seconds: float | None = None,
    tool_dispatch_timeout_seconds: float | None = None,
    progress_reporter: Callable[[str], None] | None = None,
) -> AgentEventStreamResult:
    """消费 DeepAgent 事件流，并发布前端可观察的 trace 事件。"""
    (
        idle_model_timeout_seconds,
        initial_event_timeout_seconds,
        effective_tool_dispatch_timeout_seconds,
    ) = resolve_event_timeouts(
        model_timeout_seconds,
        tool_dispatch_timeout_seconds,
    )

    collected_text_parts: list[str] = []
    latest_model_part_order: list[str] = []
    latest_model_parts: dict[str, dict[str, object]] = {}
    latest_model_visible_text_seen = False
    latest_model_reasoning_seen = False
    tool_contexts_by_run_id: dict[str, ToolEventDisplayContext] = {}
    activity_bindings_by_run_id: dict[str, tuple[tuple[str, str, str | None], ...]] = {}
    tool_scopes_by_run_id: dict[str, TurnExecutionScope] = {}
    message_stream_tool_call_ids_by_run_id: dict[str, str] = {}
    file_edit_snapshots_by_run_id: dict[str, list[FileEditSnapshot]] = {}
    last_tool_result_text = ""
    successful_tool_calls: list[SuccessfulToolCall] = []
    completed_custom_tool_names: list[str] = []
    tracked_model_run_ids: set[str] = set()
    tracked_model_run_order: list[str] = []
    model_scopes_by_run_id: dict[str, TurnExecutionScope] = {}
    model_scope_tokens_by_run_id: dict[str, object] = {}
    model_usage_by_run_id: dict[str, ModelTokenUsagePayload] = {}
    tool_output_store = ToolOutputStore(workspace_root=workspace_root)
    agent_has_tool_registry = callable(getattr(agent, "get_graph", None))
    model_tools_by_name = (
        extract_agent_tools_by_name(agent) if agent_has_tool_registry else {}
    )
    stream_config = build_isolated_stream_config(
        config,
        session_id=session_id,
        job_id=turn_id,
    )
    stream_callbacks = stream_config.get("callbacks")
    if stream_callbacks is None:
        stream_config["callbacks"] = [
            ModelRunIdentityCallbackHandler(message_stream_runtime)
        ]
    elif isinstance(stream_callbacks, list):
        stream_config["callbacks"] = [
            *stream_callbacks,
            ModelRunIdentityCallbackHandler(message_stream_runtime),
        ]
    else:
        raise TypeError("Agent 事件流 config.callbacks 必须是 list")

    def track_model_run(run_id: str) -> None:
        if run_id in tracked_model_run_ids:
            return
        tracked_model_run_ids.add(run_id)
        tracked_model_run_order.append(run_id)

    def record_latest_model_part(part: AgentStreamContentPart) -> None:
        merge_model_content_part(
            part,
            part_order=latest_model_part_order,
            parts=latest_model_parts,
        )

    def record_model_content_parts(parts: list[AgentStreamContentPart]) -> None:
        nonlocal latest_model_reasoning_seen, latest_model_visible_text_seen
        for part in parts:
            if part.kind == "reasoning":
                latest_model_reasoning_seen = True
                record_latest_model_part(part)
                continue
            if part.text and (part.text.strip() or collected_text_parts):
                latest_model_visible_text_seen = True
                record_latest_model_part(part)
                collected_text_parts.append(part.text)
                SessionInterruptState.set(
                    session_id,
                    current_text="".join(collected_text_parts),
                )

    def normalize_end_output_content(
        content: object,
        *,
        model_call_id: str | None,
    ) -> list[dict[str, object]]:
        """为缺少 stream 正文的 model.end 输出补齐 block 身份。"""
        fallback_prefix = model_call_id or "unbound-model-call"
        if isinstance(content, str):
            return [
                {
                    "type": "text",
                    "text": content,
                    "id": f"{fallback_prefix}:end-output:text:0",
                    "index": 0,
                }
            ] if content else []
        if not isinstance(content, (list, tuple)):
            return []
        normalized: list[dict[str, object]] = []
        for index, raw_block in enumerate(content):
            if isinstance(raw_block, str):
                if raw_block:
                    normalized.append(
                        {
                            "type": "text",
                            "text": raw_block,
                            "id": f"{fallback_prefix}:end-output:text:{index}",
                            "index": index,
                        }
                    )
                continue
            if not isinstance(raw_block, Mapping):
                continue
            block_type = raw_block.get("type")
            if block_type not in {
                "reasoning",
                "reasoning_content",
                "reasoning_items",
                "thinking",
                "redacted_thinking",
                "text",
                "output_text",
                "refusal",
            }:
                continue
            block = dict(raw_block)
            block.setdefault("id", f"{fallback_prefix}:end-output:{index}")
            block.setdefault("index", index)
            normalized.append(block)
        return normalized

    async for event in iter_agent_events(
        agent=agent,
        input_payload=input_payload,
        stream_config=stream_config,
        session_id=session_id,
        turn_id=turn_id,
        message_stream_runtime=message_stream_runtime,
        cancellation_signal=cancellation_signal,
        idle_model_timeout_seconds=idle_model_timeout_seconds,
        initial_event_timeout_seconds=initial_event_timeout_seconds,
        effective_tool_dispatch_timeout_seconds=effective_tool_dispatch_timeout_seconds,
    ):
        if cancellation_signal is not None:
            try:
                cancellation_signal.raise_if_cancelled()
            except ScopeCancelledError as error:
                raise asyncio.CancelledError(str(error)) from error
        event_type = event.get("event")
        name = event.get("name", "")
        data = event.get("data", {})
        metadata = event.get("metadata", {})
        is_model_event = (
            isinstance(event_type, str)
            and event_type.startswith("on_chat_model_")
            and is_tracked_chat_model_event(name)
        )
        is_model_failed_event = (
            event_type == "on_custom_event" and name == MODEL_FAILED_CUSTOM_EVENT
        )
        if (
            is_model_event
            or is_model_failed_event
            or event_type in {"on_tool_start", "on_tool_end"}
        ):
            validate_stream_event_identity(
                metadata,
                session_id=session_id,
                job_id=turn_id,
                event_type=event_type,
                name=name,
            )

        if progress_reporter is not None:
            if is_model_event:
                progress_reporter("model")
            elif is_model_failed_event:
                progress_reporter("model_failed")
            elif event_type in {"on_tool_start", "on_tool_end"}:
                progress_reporter(f"tool:{name or 'unknown'}")

        if is_model_failed_event:
            if not isinstance(data, dict):
                raise TypeError("模型失败自定义事件 data 必须是 dict")
            await publish(EventType.MODEL_FAILED, dict(data))
            if message_stream_runtime is not None:
                error_type = data.get("error_type")
                error_message = data.get("error")
                await message_stream_runtime.fail_model(
                    code=(
                        str(error_type)
                        if isinstance(error_type, str) and error_type
                        else "provider_failed"
                    ),
                    message=(
                        str(error_message)
                        if isinstance(error_message, str) and error_message
                        else "模型 provider 请求失败"
                    ),
                    outcome="upstream_error",
                    retryable=True,
                )
            continue

        if event_type == "on_chat_model_start" and is_tracked_chat_model_event(name):
            latest_model_part_order.clear()
            latest_model_parts.clear()
            latest_model_visible_text_seen = False
            latest_model_reasoning_seen = False
            model_run_id = event_run_id(event)
            if model_run_id:
                track_model_run(model_run_id)
            model_name = metadata.get("ls_model_name") or "unknown_model"
            if execution_scope is not None:
                previous_model_scope = model_scopes_by_run_id.get(model_run_id)
                if previous_model_scope is not None:
                    execution_scope.clear_active_operation(previous_model_scope)
                    await previous_model_scope.close()
                # 模型事件的 timeout 只用于下面的 idle watchdog。这里不能再给
                # child scope 设置固定 deadline，否则持续输出超过该时长时会
                # 取消底层流，路由层再误当作 provider 失败切到 backup_4。
                model_scope = execution_scope.child(
                    f"model-{model_run_id or len(model_scopes_by_run_id)}",
                )
                model_scopes_by_run_id[model_run_id] = model_scope
                model_scope_tokens_by_run_id[model_run_id] = (
                    set_current_turn_execution_scope(model_scope)
                )
                execution_scope.set_active_operation(model_scope)
            if message_stream_runtime is not None:
                await message_stream_runtime.start_model(
                    model_run_id or f"model_{int(time.time() * 1000)}",
                    str(model_name),
                )
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
            chunk_token_usage = stream_chunk_token_usage(chunk)
            if chunk_token_usage is not None:
                model_run_id = event_run_id(event)
                if not model_run_id:
                    raise RuntimeError("带 usage_metadata 的模型流事件缺少 run_id")
                track_model_run(model_run_id)
                model_usage_by_run_id[model_run_id] = chunk_token_usage
            chunk_message = _field_value(chunk, "message")
            if chunk_message is not None:
                content = _field_value(chunk_message, "content") or ""
            else:
                content = _field_value(chunk, "content") or ""

            record_model_content_parts(extract_agent_stream_content_parts(content))

            continue

        if event_type == "on_chat_model_end" and is_tracked_chat_model_event(name):
            model_run_id = event_run_id(event)
            if not latest_model_visible_text_seen:
                canonical_visible_text = (
                    message_stream_runtime.visible_text_for_model_call(model_run_id)
                    if message_stream_runtime is not None
                    else ""
                )
                if canonical_visible_text:
                    # provider hook 已经提交了权威正文。这里仅把同一份已提交
                    # 内容补进 AgentLoop 的本地结果，不再次写入消息流。
                    record_model_content_parts(
                        extract_agent_stream_content_parts(
                            [
                                {
                                    "type": "text",
                                    "text": canonical_visible_text,
                                    "id": f"{model_run_id or 'model'}:canonical:text",
                                    "index": 0,
                                }
                            ]
                        )
                    )
                else:
                    model_output = data.get("output")
                    output_content = _model_output_content(model_output)
                    end_output_blocks = normalize_end_output_content(
                        output_content,
                        model_call_id=model_run_id,
                    )
                    canonical_has_reasoning = (
                        message_stream_runtime.model_call_has_carrier(
                            model_run_id,
                            {
                                "reasoning",
                                "reasoning_content",
                                "reasoning_items",
                                "thinking",
                                "redacted_thinking",
                            },
                        )
                        if message_stream_runtime is not None
                        else False
                    )
                    if latest_model_reasoning_seen or canonical_has_reasoning:
                        end_output_blocks = [
                            block
                            for block in end_output_blocks
                            if block.get("type")
                            in {"text", "output_text", "refusal"}
                        ]
                    if end_output_blocks:
                        if message_stream_runtime is not None:
                            await message_stream_runtime.accept_message_chunk(
                                AIMessageChunk(content=end_output_blocks),
                                model_call_id=model_run_id,
                            )
                        record_model_content_parts(
                            extract_agent_stream_content_parts(end_output_blocks)
                        )
            if message_stream_runtime is not None:
                await message_stream_runtime.finish_model()
            model_scope = model_scopes_by_run_id.pop(event_run_id(event), None)
            if execution_scope is not None and model_scope is not None:
                execution_scope.clear_active_operation(model_scope)
                scope_token = model_scope_tokens_by_run_id.pop(
                    event_run_id(event),
                    None,
                )
                if scope_token is not None:
                    reset_current_turn_execution_scope(scope_token)
                await model_scope.close()
            continue

        if event_type == "on_tool_start":
            if collected_text_parts:
                collected_text_parts.clear()
                SessionInterruptState.set(session_id, current_text="")
            if not name:
                raise RuntimeError("工具开始事件缺少 name")
            raw_tool_name = name
            raw_tool_args = normalize_tool_args(data.get("input"))
            if agent_has_tool_registry:
                model_tool = model_tools_by_name.get(raw_tool_name)
                if model_tool is None:
                    raise RuntimeError(
                        "工具开始事件中的工具不在 Agent 工具注册表中: "
                        f"tool_name={raw_tool_name}"
                    )
                if not isinstance(model_tool, BaseTool):
                    raise TypeError(
                        "Agent 工具注册表包含无效工具对象: "
                        f"tool_name={raw_tool_name} "
                        f"actual_type={type(model_tool).__name__}"
                    )
                raw_tool_args = normalize_model_tool_arguments(
                    model_tool,
                    raw_tool_args,
                )
            display_context = build_tool_display_context(
                raw_tool_name=raw_tool_name,
                raw_tool_args=raw_tool_args,
            )
            run_id = event_run_id(event)
            if not run_id:
                raise RuntimeError(
                    f"{display_context.tool_name} 工具开始事件缺少 run_id"
                )
            if run_id in tool_contexts_by_run_id:
                raise RuntimeError(
                    f"工具开始事件使用了重复的 run_id: run_id={run_id} "
                    f"tool={display_context.tool_name}"
                )
            if execution_scope is not None:
                tool_scopes_by_run_id[run_id] = execution_scope.child(f"tool-{run_id}")
            tool_contexts_by_run_id[run_id] = display_context
            file_paths = file_paths_from_tool_args(
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
            if message_stream_runtime is not None:
                provider_tool_name = (
                    display_context.invocation_tool_name or display_context.tool_name
                )
                message_stream_tool_call_id = message_stream_runtime.claim_tool_call_id(
                    provider_tool_name,
                    display_context.tool_args,
                    target_tool_name=display_context.tool_name,
                )
                if message_stream_tool_call_id is None:
                    raise RuntimeError(
                        "工具开始事件无法关联已提交的 provider tool_call: "
                        f"tool={display_context.tool_name} run_id={run_id}"
                    )
                message_stream_tool_call_ids_by_run_id[run_id] = (
                    message_stream_tool_call_id
                )
                await message_stream_runtime.start_tool(
                    tool_execution_id=run_id,
                    tool_call_id=message_stream_tool_call_id,
                    tool_name=display_context.tool_name,
                )
                activity_bindings: list[tuple[str, str, str | None]] = []
                if display_context.tool_name in SUBAGENT_TOOL_NAMES:
                    activity_id = (
                        f"{message_stream_runtime.writer.turn_stream_id}:"
                        f"subagent:{run_id}"
                    )
                    await message_stream_runtime.activities.started(
                        activity_id=activity_id,
                        kind="subagent.run",
                        summary="子 Agent 启动中",
                        cancellable=False,
                        resumable=True,
                        side_effect_policy="external",
                        detail={
                            "phase": "starting",
                            "agent_id": agent_id,
                        },
                    )
                    activity_bindings.append((activity_id, "subagent.run", None))
                resource_binding = resource_activity_binding_from_metadata(
                    event.get("metadata")
                )
                if resource_binding is not None:
                    activity_id = (
                        f"{message_stream_runtime.writer.turn_stream_id}:"
                        f"resource:{run_id}"
                    )
                    await message_stream_runtime.activities.started(
                        activity_id=activity_id,
                        kind="resource.operation",
                        summary="资源操作执行中",
                        cancellable=True,
                        resumable=False,
                        side_effect_policy="external",
                        resource_refs=(resource_binding.resource_id,),
                        detail={
                            "resource_id": resource_binding.resource_id,
                            "operation": display_context.tool_name,
                            "phase": "starting",
                            "agent_id": agent_id,
                        },
                    )
                    activity_bindings.append(
                        (activity_id, "resource.operation", resource_binding.resource_id)
                    )
                if activity_bindings:
                    activity_bindings_by_run_id[run_id] = tuple(activity_bindings)
            continue

        if event_type == "on_tool_end":
            run_id = event_run_id(event)
            if not run_id:
                raise RuntimeError(f"{name or 'unknown_tool'} 工具结束事件缺少 run_id")
            display_context = tool_contexts_by_run_id.pop(run_id, None)
            if display_context is None:
                raise RuntimeError(
                    f"工具结束事件找不到对应的开始事件: run_id={run_id} "
                    f"tool={name or 'unknown_tool'}"
                )
            raw_output = data.get("output")
            raw_output = tool_message_from_output(
                raw_output,
                execution_id=run_id,
                tool_name=display_context.tool_name,
            )
            tool_call_id = raw_output.tool_call_id
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise RuntimeError(
                    "工具结束事件的 ToolMessage 缺少 tool_call_id: "
                    f"execution_id={run_id} tool={display_context.tool_name}"
                )
            raw_result_text = extract_tool_result_text(raw_output)
            effective_tool_status = tool_output_status(raw_output)
            output = raw_output
            if (
                isinstance(raw_output, ToolMessage)
                and effective_tool_status == "success"
            ):
                output = await tool_output_store.abound(
                    session_id=session_id,
                    tool_name=display_context.tool_name,
                    tool_call_id=raw_output.tool_call_id,
                    message=raw_output,
                )
            result_text = extract_tool_result_text(output)
            last_tool_result_text = result_text
            skill_names = custom_tool_skill_sources.get(display_context.tool_name, [])
            if effective_tool_status == "success":
                successful_tool_calls.append(
                    SuccessfulToolCall(
                        tool_name=display_context.tool_name,
                        tool_args=dict(display_context.tool_args),
                    )
                )
            if display_context.invocation_tool_name == EXTENSION_TOOL_INVOKER_NAME:
                completed_custom_tool_names.append(display_context.tool_name)
            stored_edits: list[StoredFileEdit] = []
            if run_id:
                snapshots = file_edit_snapshots_by_run_id.pop(run_id, None)
                if (
                    snapshots is None
                    and display_context.tool_name == APPLY_PATCH_TOOL_NAME
                    and tool_output_succeeded(raw_output)
                ):
                    snapshots = apply_patch_snapshots_from_result(
                        result_text=raw_result_text,
                        session_changes_service=session_changes_service,
                        workspace_root=workspace_root,
                    )
                if snapshots is not None and tool_output_succeeded(raw_output):
                    for snapshot in snapshots:
                        stored_edit = (
                            await session_changes_service.record_tool_file_edit(
                                session_id=session_id,
                                turn_id=turn_id,
                                tool_call_id=tool_call_id,
                                execution_id=run_id,
                                tool_name=display_context.tool_name,
                                before=snapshot,
                            )
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
                "status": effective_tool_status,
                "failed": effective_tool_status == "error",
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
                    stored_edit_payload(stored_edit) for stored_edit in stored_edits
                ]
                if len(stored_edits) == 1:
                    payload["file_edit"] = stored_edit_payload(stored_edits[0])
            await publish(
                EventType.TOOL_CALL_END,
                payload,
            )
            if message_stream_runtime is not None:
                message_stream_tool_call_id = (
                    message_stream_tool_call_ids_by_run_id.pop(
                        run_id,
                        None,
                    )
                )
                if message_stream_tool_call_id is None:
                    raise RuntimeError(
                        "工具结束事件缺少已关联的 provider tool_call: "
                        f"tool={display_context.tool_name} run_id={run_id}"
                    )
                await message_stream_runtime.complete_tool(
                    tool_execution_id=run_id,
                    tool_call_id=message_stream_tool_call_id,
                    tool_name=display_context.tool_name,
                    status=(
                        "failed" if effective_tool_status == "error" else "succeeded"
                    ),
                    result=result_text,
                    error=result_text if effective_tool_status == "error" else None,
                )
                activity_bindings = activity_bindings_by_run_id.pop(run_id, ())
                for activity_id, activity_kind, resource_id in activity_bindings:
                    detail = activity_result_detail(
                        result_text,
                        tool_name=display_context.tool_name,
                        agent_id=agent_id,
                    )
                    if activity_kind == "resource.operation":
                        detail["operation"] = display_context.tool_name
                        if resource_id is not None:
                            detail["resource_id"] = resource_id
                    await message_stream_runtime.activities.updated(
                        activity_id=activity_id,
                        kind=activity_kind,
                        status="stopping",
                        detail=detail,
                    )
                    if effective_tool_status == "error":
                        await message_stream_runtime.activities.failed(
                            activity_id=activity_id,
                            kind=activity_kind,
                            outcome="outcome_unknown",
                            summary=result_text,
                            detail=detail,
                        )
                    else:
                        await message_stream_runtime.activities.completed(
                            activity_id=activity_id,
                            kind=activity_kind,
                            summary=(
                                "子 Agent 已接受并独立运行"
                                if activity_kind == "subagent.run"
                                else f"资源操作已完成：{display_context.tool_name}"
                            ),
                        )
            tool_scope = tool_scopes_by_run_id.pop(run_id, None)
            if tool_scope is not None:
                await tool_scope.close()

    if message_stream_runtime is not None:
        pending_tool_calls = message_stream_runtime.pending_tool_calls()
        if pending_tool_calls:
            await message_stream_runtime.fail_pending_tool_calls(
                completion_reason="tool_dispatch_timeout",
                error=(
                    "模型工具调用参数已完整，但事件流结束前没有收到工具执行分派事件: "
                    f"tool_calls={[item[0] for item in pending_tool_calls]}"
                ),
            )
            raise AgentEventStreamTimeoutError(
                "Agent 事件流结束时仍存在未分派的工具调用: "
                f"tool_calls={[item[0] for item in pending_tool_calls]}",
                code="tool_dispatch_timeout",
            )

    if tracked_model_run_order:
        last_model_run_id = tracked_model_run_order[-1]
        token_usage = model_usage_by_run_id.get(
            last_model_run_id,
            ModelTokenUsagePayload(model_calls=1),
        )
    else:
        token_usage = ModelTokenUsagePayload()

    return AgentEventStreamResult(
        final_text="".join(collected_text_parts).strip(),
        latest_model_content_blocks=tuple(
            part
            for part_id in latest_model_part_order
            if (part := latest_model_parts.get(part_id)) is not None
        ),
        last_tool_result_text=last_tool_result_text,
        successful_tool_calls=tuple(successful_tool_calls),
        completed_custom_tool_names=tuple(completed_custom_tool_names),
        token_usage=token_usage,
    )


__all__ = ["process_agent_event_stream"]
