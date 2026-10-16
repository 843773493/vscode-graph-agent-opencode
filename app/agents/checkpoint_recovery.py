"""仅续接 checkpoint 已冻结的幂等通信工具节点。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.runtime import ModelCallRecord
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.event_stream.contracts import AgentEventSource


class CheckpointRecoveryRejectedError(RuntimeError):
    """原 execution 缺少完整恢复证据，只能明确收敛为 execution_lost。"""


@dataclass(frozen=True, slots=True)
class PendingToolCheckpoint:
    checkpoint_id: str
    assistant: AIMessage
    model_call: ModelCallRecord
    assembly_id: str


def pending_tool_assistant(messages: object) -> AIMessage | None:
    """只接受整个冻结 batch 都可沿同一调用幂等恢复的 completed carrier。"""
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], AIMessage):
        return None
    assistant = messages[-1]
    if not assistant.tool_calls or assistant.invalid_tool_calls:
        return None
    for call in assistant.tool_calls:
        name = call.get("name")
        arguments = call.get("args")
        if name == "invoke_extension_tool" and isinstance(arguments, dict):
            name = arguments.get("tool_name")
        if name not in {"send_message_to_session", "read_context", "wait_for_session"}:
            return None
    return assistant


async def persisted_pending_tool_candidate(
    checkpointer: RolloutCheckpointSaver, admission: ExecutionAdmission,
) -> PendingToolCheckpoint | None:
    """编译前只读固定 checkpoint；执行 owner 再以公开 aget_state 判定 scheduler。"""
    latest = await checkpointer.aget_tuple(build_checkpoint_config(admission.thread_id))
    if latest is None:
        return None
    # 最新读取可以包含排队的 accepted root；固定 checkpoint 读取严格排除它们。
    frozen = await checkpointer.aget_tuple(latest.config)
    if frozen is None:
        raise RuntimeError("恢复预检的固定 checkpoint 已消失")
    assistant = pending_tool_assistant(frozen.checkpoint.get("channel_values", {}).get("messages"))
    if assistant is None:
        return None
    return await _registered_candidate(checkpointer, admission, assistant, frozen.config)


async def _registered_candidate(
    checkpointer: RolloutCheckpointSaver, admission: ExecutionAdmission,
    assistant: AIMessage, config: RunnableConfig,
) -> PendingToolCheckpoint | None:
    model_call_id = LangChainMessageCodec().model_call_id(assistant)
    if model_call_id is None:
        raise CheckpointRecoveryRejectedError("pending tools checkpoint 缺少 emitting ModelCall identity")
    if not isinstance(assistant.id, str) or not assistant.id:
        raise CheckpointRecoveryRejectedError("pending tools checkpoint 缺少稳定 carrier message_id")
    # codec 复用 typed projection_group 的完整成员验证；不按正文或最后一个 item 推测完成。
    try:
        carrier = tuple(LangChainMessageCodec().items_for_message(
            assistant, item_sequence=1, message_id=assistant.id,
            turn_id=admission.turn_id, execution_id=admission.execution_id,
            timestamp="1970-01-01T00:00:00+00:00", model_call_id=model_call_id,
        ))
        registered = await asyncio.to_thread(
            checkpointer.read_resumable_model_call, admission, model_call_id,
            carrier_items=carrier,
        )
    except (ItemSchemaError, RuntimeError) as error:
        raise CheckpointRecoveryRejectedError(f"原 pending tools 缺少完整 owner/carrier 证明: {error}") from error
    if registered is None:
        return None
    checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise CheckpointRecoveryRejectedError("pending tools scheduler snapshot 缺少 checkpoint_id")
    model_call, assembly_id = registered
    return PendingToolCheckpoint(checkpoint_id, assistant, model_call, assembly_id)


async def pending_tool_checkpoint(
    agent: AgentEventSource,
    checkpointer: RolloutCheckpointSaver,
    admission: ExecutionAdmission,
    *, checkpoint_id: str | None = None,
) -> PendingToolCheckpoint | None:
    """沿原图的公开 scheduler snapshot 判定，绝不从历史推测可重跑节点。"""
    snapshot = await agent.aget_state(build_checkpoint_config(admission.thread_id, checkpoint_id=checkpoint_id))
    if snapshot.next != ("tools",) or any(task.interrupts or task.error for task in snapshot.tasks):
        return None
    assistant = pending_tool_assistant(snapshot.values.get("messages"))
    if assistant is None:
        return None
    return await _registered_candidate(checkpointer, admission, assistant, snapshot.config)
