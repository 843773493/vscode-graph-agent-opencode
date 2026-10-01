"""Node Debug 调试动作分发链路。

承载 apply_action 这一条产品入口的动作分发：owner 准入后按动作类型委托既有
breakpoint/evaluation/inspector/lifecycle 协作者，并与 start 共用 per-owner
临界区处理 stop。由 NodeDebugService 继承（宿主必须提供 _runtimes、_session_state、
_configuration_registry、_breakpoint_mutations、_evaluation、_inspector、_lifecycle、
_owner_key、_admit_mutation、_reconcile_session_sources、_assert_no_unsettled_claim、
_append_action 与公开入口 get_state），不反向依赖顶层 service.py。
"""

from __future__ import annotations

from typing import Literal

from app.schemas.internal_v2.node_debug import (
    NodeDebugActionRequest,
    NodeDebugClearBreakpointActionRequest,
    NodeDebugControlActionRequest,
    NodeDebugEvaluateActionRequest,
    NodeDebugSetBreakpointActionRequest,
    NodeDebugStateDTO,
    NodeDebugUpdateBreakpointActionRequest,
)


class NodeDebugActionDispatchMixin:
    """调试动作分发链路的方法族（由 NodeDebugService 继承）。"""

    async def apply_action(
        self,
        *,
        command: NodeDebugActionRequest,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = command.session_id, command.thread_id
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        owner = self._owner_key(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        await self._reconcile_session_sources(session_id, thread_id, runtime)
        if runtime is None and not isinstance(
            command,
            (
                NodeDebugSetBreakpointActionRequest,
                NodeDebugUpdateBreakpointActionRequest,
                NodeDebugClearBreakpointActionRequest,
            ),
        ):
            self._assert_no_unsettled_claim(
                owner,
                operation=f"调试动作 {command.action}",
            )
            raise RuntimeError(
                f"Node 调试会话不存在: session_id={session_id}, thread_id={thread_id}"
            )
        if isinstance(command, NodeDebugSetBreakpointActionRequest):
            self._configuration_registry.ensure_configuration_for_breakpoint(
                session_id,
                thread_id,
                path=command.params.path,
            )
            await self._breakpoint_mutations.set_breakpoint(
                owner,
                runtime,
                command.params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif isinstance(command, NodeDebugUpdateBreakpointActionRequest):
            await self._breakpoint_mutations.update_breakpoint(
                owner,
                runtime,
                command.params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif isinstance(command, NodeDebugClearBreakpointActionRequest):
            await self._breakpoint_mutations.clear_breakpoint(
                owner,
                runtime,
                command.params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif isinstance(command, NodeDebugEvaluateActionRequest):
            await self._evaluation.evaluate(
                runtime,
                command.params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif isinstance(command, NodeDebugControlActionRequest):
            if command.action != "stop":
                await self._inspector.debugger_command(
                    runtime,
                    command.action,
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
                self._session_state.persist_runtime_state(
                    session_id, thread_id, runtime
                )
                return await self.get_state(session_id, thread_id)
            # stop 与 start 共用 per-owner 临界区（R3b 复核残留项）：否则 stop 落在
            # "runtime 已登记、进程尚未 spawn"的窗口会以 process is None 判定
            # "已停止/可证明不存在"，随后启动序列仍会 spawn，形成"exited + 进程
            # 存活"的假终态。临界区内的 await 全部有界（见 _owner_lock 文档）。
            async with self._owner_lock(owner):
                outcome = await self._lifecycle.stop_runtime(runtime)
                if outcome != "reconcile_required":
                    async with runtime.state_lock:
                        runtime.status = "exited"
                        runtime.error_message = None
                        self._append_action(
                            runtime,
                            "stop",
                            "已停止 Node Inspector",
                            actor=actor,
                            tool_name=tool_name,
                            tool_call_id=tool_call_id,
                        )
            if outcome == "reconcile_required":
                # 无法核实终态：保持 reconcile_required，不报告 stopped。
                self._session_state.persist_runtime_state(
                    session_id, thread_id, runtime
                )
                return await self.get_state(session_id, thread_id)
        self._session_state.persist_runtime_state(
            session_id, thread_id, runtime
        )
        return await self.get_state(session_id, thread_id)
