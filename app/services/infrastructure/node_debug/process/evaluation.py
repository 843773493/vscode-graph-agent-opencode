from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from app.schemas.internal_v2.node_debug import (
    NodeDebugEvaluateParams,
    NodeDebugEvaluationDTO,
)
from app.services.infrastructure.node_debug.process.inspector import (
    NodeDebugInspector,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugActionAppender,
    NodeDebugRuntime,
)

#: 求值历史的唯一上限；与动作历史上限解耦（见 NodeDebugService 的动作时间线）。
MAX_NODE_DEBUG_EVALUATIONS = 100


class NodeDebugEvaluation:
    """暂停上下文求值链路：校验暂停帧、命令 Inspector 并落库求值结果。

    只负责单次 ``Debugger.evaluateOnCallFrame`` 的完整行为：暂停/帧准入、
    Inspector 命令、求值 DTO 映射、``last_evaluation``/``evaluations`` 更新与
    动作记录。准入与 owner 临界区仍由 :class:`NodeDebugService` 持有。
    """

    def __init__(
        self,
        *,
        inspector: NodeDebugInspector,
        append_action: NodeDebugActionAppender,
        max_evaluations: int = MAX_NODE_DEBUG_EVALUATIONS,
    ) -> None:
        self._inspector = inspector
        self._append_action = append_action
        self._max_evaluations = max_evaluations

    async def evaluate(
        self,
        runtime: NodeDebugRuntime,
        params: NodeDebugEvaluateParams,
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        expression = params.expression
        async with runtime.state_lock:
            if runtime.status != "paused" or not runtime.call_stack:
                raise RuntimeError("只有暂停在源码断点时才能求值")
            call_frame_id = params.call_frame_id or runtime.call_stack[0].call_frame_id
            if not any(
                frame.call_frame_id == call_frame_id for frame in runtime.call_stack
            ):
                raise ValueError("求值 call_frame_id 不属于当前暂停调用栈")
        result = await self._inspector.command(
            runtime,
            "Debugger.evaluateOnCallFrame",
            {
                "callFrameId": call_frame_id,
                "expression": expression,
                "returnByValue": True,
                "generatePreview": False,
            },
        )
        remote_result = result.get("result")
        exception_details = result.get("exceptionDetails")
        evaluation = NodeDebugEvaluationDTO(
            expression=expression,
            value=self._inspector.remote_value(remote_result),
            type=self._inspector.remote_type(remote_result),
            description=self._inspector.remote_description(remote_result),
            error=(
                self._inspector.exception_message(exception_details)
                if isinstance(exception_details, dict)
                else None
            ),
            evaluated_at=datetime.now(UTC),
        )
        async with runtime.state_lock:
            runtime.last_evaluation = evaluation
            runtime.evaluations.append(evaluation)
            del runtime.evaluations[: -self._max_evaluations]
            runtime.error_message = None
            self._append_action(
                runtime,
                "evaluate",
                f"已求值: {expression}",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
