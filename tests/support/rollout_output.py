"""静态历史 fixture 通过正式模型终态 owner 提交输出，不发送 Provider 请求。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage

from app.core.execution_admission_context import require_current_execution_admission
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter


def commit_fixture_output(
    saver: RolloutCheckpointSaver, *, messages: Sequence[object],
) -> None:
    """先提交已完成的工具前缀，再原子提交最终 item 与执行终态。

    调用方须处于真实 admission scope，随后由原 checkpoint 写入验证同一消息组；
    不能把已经独立落盘的 assistant item 再补成成功终态。
    """
    final = messages[-1]
    if not isinstance(final, AIMessage) or final.tool_calls:
        raise ValueError("静态完成 fixture 必须以无 tool-call 的 AIMessage 结束")
    admission = require_current_execution_admission()
    saver.ensure_request_items(
        admission.session_id, admission.thread_id,
        turn_id=admission.turn_id, execution_id=admission.execution_id,
        messages=messages[:-1],
    )
    StepModelCallAdapter(
        checkpointer=saver, session_id=admission.session_id,
        thread_id=admission.thread_id, job_id=admission.job_id, checkpoint_ns="",
    ).converge_final_message(final)
