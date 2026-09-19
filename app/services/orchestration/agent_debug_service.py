from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.schemas.internal_v2.debug import (
    AgentDebugStateDTO,
    DebugActionRecordDTO,
    DebugBreakpointDTO,
    DebugBreakpointKind,
    DebugMode,
    DebugStopSnapshotDTO,
)

DebugActor = Literal["human", "ai", "system"]

_DEBUG_ACTIONS = {
    "debug_continue",
    "debug_step_tool",
    "debug_takeover",
    "debug_handoff_to_ai",
    "debug_inspect_stop",
    "debug_explain_state",
    "debug_set_breakpoint",
    "debug_clear_breakpoint",
    "debug_set_mode",
}
_BREAKPOINT_KINDS: tuple[DebugBreakpointKind, ...] = (
    "tool_before",
    "tool_after",
    "llm_before",
)
_DEBUG_MODES: tuple[DebugMode, ...] = ("ai", "human", "collaborative")
_MAX_ACTIONS = 100


@dataclass(slots=True)
class _DebugRuntimeState:
    job_id: str
    session_id: str
    mode: DebugMode = "collaborative"
    breakpoints: dict[str, DebugBreakpointDTO] = field(default_factory=dict)
    actions: list[DebugActionRecordDTO] = field(default_factory=list)
    active_stop: DebugStopSnapshotDTO | None = None
    last_stop: DebugStopSnapshotDTO | None = None
    stop_count: int = 0
    step_pending: bool = False
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.gate.set()


class AgentDebugService:
    """Agent 调试第一期运行时服务：断点、暂停门、控制权和审计轨迹。"""

    def __init__(self, *, job_event_bus: JobEventBusProtocol) -> None:
        self._bus = job_event_bus
        self._states: dict[str, _DebugRuntimeState] = {}
        self._states_lock = asyncio.Lock()

    async def _get_state(
        self,
        job_id: str,
        session_id: str,
    ) -> _DebugRuntimeState:
        async with self._states_lock:
            state = self._states.get(job_id)
            if state is None:
                state = _DebugRuntimeState(job_id=job_id, session_id=session_id)
                self._states[job_id] = state
            elif state.session_id != session_id:
                raise RuntimeError(
                    "调试状态的 session_id 与 Job 不一致: "
                    f"job_id={job_id} expected={state.session_id} actual={session_id}"
                )
            return state

    async def get_state(self, job_id: str, session_id: str) -> AgentDebugStateDTO:
        state = await self._get_state(job_id, session_id)
        async with state.lock:
            return self._snapshot(state)

    async def apply_action(
        self,
        *,
        job_id: str,
        session_id: str,
        action: str,
        params: dict[str, object] | None = None,
    ) -> AgentDebugStateDTO:
        if action not in _DEBUG_ACTIONS:
            raise ValueError(f"不支持的 Agent 调试动作: {action}")
        state = await self._get_state(job_id, session_id)
        raw_params = params or {}
        actor = self._actor(raw_params.get("actor"), default="human")
        release_gate = False
        action_message = ""
        breakpoint_id: str | None = None
        breakpoint_kind: DebugBreakpointKind | None = None
        tool_name: str | None = None

        async with state.lock:
            if action == "debug_set_breakpoint":
                breakpoint_kind = self._breakpoint_kind(raw_params.get("kind"))
                tool_name = self._optional_tool_name(raw_params.get("tool_name"))
                breakpoint_id = create_prefixed_id("bp")
                breakpoint = DebugBreakpointDTO(
                    breakpoint_id=breakpoint_id,
                    kind=breakpoint_kind,
                    tool_name=tool_name,
                    created_at=datetime.now(UTC),
                )
                state.breakpoints[breakpoint_id] = breakpoint
                action_message = self._message(
                    raw_params.get("message"),
                    f"已设置 {breakpoint_kind} 断点",
                )
            elif action == "debug_clear_breakpoint":
                breakpoint_id = self._optional_non_empty_string(
                    raw_params.get("breakpoint_id")
                )
                if breakpoint_id is None:
                    removed = len(state.breakpoints)
                    state.breakpoints.clear()
                    action_message = f"已清除全部断点（{removed} 个）"
                else:
                    if breakpoint_id not in state.breakpoints:
                        raise ValueError(f"调试断点不存在: {breakpoint_id}")
                    state.breakpoints.pop(breakpoint_id)
                    action_message = f"已清除断点 {breakpoint_id}"
            elif action == "debug_set_mode":
                state.mode = self._mode(raw_params.get("mode"))
                action_message = f"调试控制权已切换为 {self._mode_label(state.mode)}"
            elif action == "debug_step_tool":
                state.step_pending = True
                release_gate = True
                action_message = "已允许执行一个工具步骤，并在工具返回后再次暂停"
            elif action == "debug_continue":
                state.step_pending = False
                release_gate = True
                action_message = "已继续 Agent 执行"
            elif action == "debug_takeover":
                state.mode = "human"
                action_message = "人类已接管调试控制权"
            elif action == "debug_handoff_to_ai":
                state.mode = "ai"
                state.step_pending = False
                release_gate = True
                action_message = "已将调试控制权交给 AI"
            elif action == "debug_inspect_stop":
                action_message = "已查看当前停止快照"
            elif action == "debug_explain_state":
                action_message = self._explanation_message(state)

            if release_gate:
                state.active_stop = None
                state.gate.set()

            record = self._append_action(
                state,
                action=action,
                actor=actor,
                message=action_message,
            )
            snapshot = self._snapshot(state)

        await self._publish_action(
            state,
            record,
            breakpoint_id=breakpoint_id,
            breakpoint_kind=breakpoint_kind,
            tool_name=tool_name,
        )
        return snapshot

    async def record_job_control(
        self,
        *,
        job_id: str,
        session_id: str,
        action: str,
    ) -> None:
        """把旧有 Job 控制也纳入调试审计，并解除调试等待门。"""
        state = await self._get_state(job_id, session_id)
        async with state.lock:
            if action in {"pause", "cancel", "resume"}:
                state.active_stop = None
                state.step_pending = False
                state.gate.set()
            message = {
                "pause": "任务暂停控制已记录",
                "resume": "任务恢复控制已记录",
                "cancel": "任务取消控制已记录",
            }.get(action, f"Job 控制动作已记录: {action}")
            record = self._append_action(
                state,
                action=action,
                actor="human",
                message=message,
            )
        await self._publish_action(state, record)

    async def maybe_stop(
        self,
        *,
        job_id: str,
        session_id: str,
        point: DebugBreakpointKind,
        tool_name: str | None,
        args: dict[str, object] | None = None,
        result: str | None = None,
    ) -> None:
        state = await self._get_state(job_id, session_id)
        async with state.lock:
            matched = next(
                (
                    breakpoint
                    for breakpoint in state.breakpoints.values()
                    if breakpoint.enabled
                    and breakpoint.kind == point
                    and (
                        breakpoint.tool_name is None
                        or breakpoint.tool_name == tool_name
                    )
                ),
                None,
            )
            single_step = state.step_pending and point == "tool_after"
            if matched is None and not single_step:
                return

            state.step_pending = False
            state.stop_count += 1
            stop_id = create_prefixed_id("stop")
            explanation = self._build_explanation(
                point=point,
                tool_name=tool_name,
                args=args or {},
                result=result,
                single_step=single_step,
            )
            snapshot = DebugStopSnapshotDTO(
                stop_id=stop_id,
                job_id=job_id,
                session_id=session_id,
                point=point,
                reason="single_step" if single_step else "breakpoint",
                tool_name=tool_name,
                args=args or {},
                result=result,
                breakpoint_id=matched.breakpoint_id if matched else None,
                mode=state.mode,
                explanation=explanation,
                stopped_at=datetime.now(UTC),
                sequence=state.stop_count,
            )
            state.active_stop = snapshot
            state.last_stop = snapshot
            state.gate.clear()

        await self._bus.publish(
            job_id=job_id,
            event_type=EventType.DEBUG_STOP,
            payload=snapshot.model_dump(mode="json"),
            agent_id="agent_debugger",
        )
        try:
            await state.gate.wait()
        finally:
            async with state.lock:
                state.active_stop = None
                state.gate.set()

    async def _publish_action(
        self,
        state: _DebugRuntimeState,
        record: DebugActionRecordDTO,
        *,
        breakpoint_id: str | None = None,
        breakpoint_kind: DebugBreakpointKind | None = None,
        tool_name: str | None = None,
    ) -> None:
        await self._bus.publish(
            job_id=state.job_id,
            event_type=EventType.DEBUG_ACTION,
            payload={
                "session_id": state.session_id,
                "action_id": record.action_id,
                "action": record.action,
                "actor": record.actor,
                "mode": record.mode,
                "message": record.message,
                "breakpoint_id": breakpoint_id,
                "breakpoint_kind": breakpoint_kind,
                "tool_name": tool_name,
                "created_at": record.created_at,
            },
            agent_id="agent_debugger",
        )

    @staticmethod
    def _append_action(
        state: _DebugRuntimeState,
        *,
        action: str,
        actor: DebugActor,
        message: str,
    ) -> DebugActionRecordDTO:
        record = DebugActionRecordDTO(
            action_id=create_prefixed_id("debug-action"),
            job_id=state.job_id,
            session_id=state.session_id,
            action=action,
            actor=actor,
            mode=state.mode,
            message=message,
            created_at=datetime.now(UTC),
        )
        state.actions.append(record)
        del state.actions[:-_MAX_ACTIONS]
        return record

    @staticmethod
    def _snapshot(state: _DebugRuntimeState) -> AgentDebugStateDTO:
        return AgentDebugStateDTO(
            job_id=state.job_id,
            session_id=state.session_id,
            enabled=bool(state.breakpoints),
            mode=state.mode,
            paused=state.active_stop is not None,
            active_stop=state.active_stop.model_copy(deep=True)
            if state.active_stop
            else None,
            last_stop=state.last_stop.model_copy(deep=True)
            if state.last_stop
            else None,
            breakpoints=[
                item.model_copy(deep=True) for item in state.breakpoints.values()
            ],
            actions=[item.model_copy(deep=True) for item in state.actions],
            stop_count=state.stop_count,
        )

    @staticmethod
    def _optional_non_empty_string(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"调试参数必须是非空字符串: {value!r}")
        return value.strip()

    @classmethod
    def _optional_tool_name(cls, value: object) -> str | None:
        return cls._optional_non_empty_string(value)

    @classmethod
    def _breakpoint_kind(cls, value: object) -> DebugBreakpointKind:
        if not isinstance(value, str) or value not in _BREAKPOINT_KINDS:
            raise ValueError(
                "断点 kind 必须是 tool_before、tool_after 或 llm_before，"
                f"实际值: {value!r}"
            )
        return value  # type: ignore[return-value]

    @classmethod
    def _mode(cls, value: object) -> DebugMode:
        if not isinstance(value, str) or value not in _DEBUG_MODES:
            raise ValueError(
                f"调试 mode 必须是 ai、human 或 collaborative，实际值: {value!r}"
            )
        return value  # type: ignore[return-value]

    @staticmethod
    def _actor(value: object, *, default: DebugActor) -> DebugActor:
        if value is None:
            return default
        if value not in {"human", "ai", "system"}:
            raise ValueError(f"调试 actor 无效: {value!r}")
        return value  # type: ignore[return-value]

    @staticmethod
    def _message(value: object, default: str) -> str:
        if value is None:
            return default
        if not isinstance(value, str):
            raise TypeError(f"调试 message 必须是字符串: {value!r}")
        return value.strip() or default

    @staticmethod
    def _mode_label(mode: DebugMode) -> str:
        return {"ai": "AI 驱动", "human": "人类驱动", "collaborative": "协同模式"}[mode]

    @classmethod
    def _explanation_message(cls, state: _DebugRuntimeState) -> str:
        if state.active_stop is None:
            return "当前没有停止中的 Agent；可以先设置断点或等待下一次工具/模型边界。"
        return state.active_stop.explanation

    @staticmethod
    def _build_explanation(
        *,
        point: DebugBreakpointKind,
        tool_name: str | None,
        args: dict[str, object],
        result: str | None,
        single_step: bool,
    ) -> str:
        if point == "tool_before":
            prefix = "单步工具已执行到返回前" if single_step else "即将调用工具"
            return f"{prefix} {tool_name or '未知工具'}，可以检查输入参数后继续。"
        if point == "tool_after":
            result_hint = (
                "工具返回了错误"
                if result and result.startswith("Error:")
                else "工具已经返回结果"
            )
            return f"{tool_name or '未知工具'}：{result_hint}，可以检查结果后决定是否继续请求模型。"
        model = args.get("model") or "当前模型"
        return f"即将发起 {model} 的下一次模型请求，可以先检查当前 Agent 执行上下文。"
