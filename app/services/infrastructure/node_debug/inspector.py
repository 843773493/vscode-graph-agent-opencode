from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import unquote, urlparse

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from app.schemas.internal_v2.node_debug import (
    NodeDebugAction,
    NodeDebugBreakpointDTO,
    NodeDebugEvaluationDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStatus,
    NodeDebugVariableDTO,
)


@dataclass(slots=True)
class NodeDebugInspectorState:
    """单个调试运行时的 Inspector 连接、命令和暂停上下文。"""

    socket: ClientConnection | None = None
    inspector_url: str | None = None
    script_urls: dict[str, str] = field(default_factory=dict)
    scope_object_ids: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    next_command_id: int = 1
    pending_commands: dict[int, asyncio.Future[dict[str, object]]] = field(
        default_factory=dict
    )
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inspector_ready: asyncio.Event = field(default_factory=asyncio.Event)
    receiver_task: asyncio.Task[None] | None = None
    variable_hydration_task: asyncio.Task[None] | None = None


class NodeDebugInspectorRuntime(Protocol):
    """Inspector 链路需要的运行时字段契约。"""

    inspector: NodeDebugInspectorState
    process: asyncio.subprocess.Process | None
    closing: bool
    status: NodeDebugStatus
    command_timeout_seconds: float
    error_message: str | None
    logpoint_error_message: str | None
    paused_reason: str | None
    paused_breakpoint_ids: set[str]
    call_stack: list[NodeDebugStackFrameDTO]
    last_stopped_frame: NodeDebugStackFrameDTO | None
    breakpoints: dict[str, NodeDebugBreakpointDTO]
    inspector_breakpoint_ids: dict[str, str]
    last_evaluation: NodeDebugEvaluationDTO | None
    state_lock: asyncio.Lock


class NodeDebugInspector:
    """集中管理 Node Inspector 命令、事件和暂停变量读取。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        append_action: Callable[..., None],
        clear_stop_snapshot: Callable[[NodeDebugInspectorRuntime], None],
    ) -> None:
        self._workspace_root = workspace_root
        self._append_action = append_action
        self._clear_stop_snapshot = clear_stop_snapshot

    async def connect(self, runtime: NodeDebugInspectorRuntime) -> None:
        inspector_url = runtime.inspector.inspector_url
        if inspector_url is None:
            raise RuntimeError("Node Inspector 已报告就绪，但缺少 WebSocket 地址")
        import websockets

        # Node Inspector 不兼容 websockets 默认的 20 秒 keepalive ping；
        # 该 ping 会导致连接关闭，进而让暂停中的脚本继续执行。
        runtime.inspector.socket = await websockets.connect(
            inspector_url,
            ping_interval=None,
        )
        runtime.inspector.receiver_task = asyncio.create_task(
            self.receive_messages(runtime)
        )

    async def debugger_command(
        self,
        runtime: NodeDebugInspectorRuntime,
        action: NodeDebugAction,
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        if runtime.inspector.socket is None or runtime.status not in {"running", "paused"}:
            raise RuntimeError("Node 调试进程当前不可控制")
        method_by_action = {
            "continue": "Debugger.resume",
            "pause": "Debugger.pause",
            "step_over": "Debugger.stepOver",
            "step_into": "Debugger.stepInto",
            "step_out": "Debugger.stepOut",
        }
        method = method_by_action.get(action)
        if method is None:
            raise ValueError(f"不支持的 Node 调试动作: {action}")
        if action in {"continue", "step_over", "step_into", "step_out"}:
            async with runtime.state_lock:
                runtime.status = "running"
                self.clear_paused_snapshot(runtime)
        await self.command(runtime, method)
        await self.wait_for_execution_state(runtime)
        await self.wait_for_frame_variables(runtime)
        message = {
            "continue": "已继续执行 JavaScript",
            "pause": "已请求暂停 JavaScript",
            "step_over": "已执行一步单步跳过",
            "step_into": "已执行一步单步进入",
            "step_out": "已执行一步单步跳出",
        }[action]
        async with runtime.state_lock:
            self._append_action(
                runtime,
                action,
                message,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )

    async def command(
        self,
        runtime: NodeDebugInspectorRuntime,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        socket = runtime.inspector.socket
        if socket is None:
            raise RuntimeError("Node Inspector WebSocket 尚未连接")
        async with runtime.inspector.command_lock:
            command_id = runtime.inspector.next_command_id
            runtime.inspector.next_command_id += 1
            future: asyncio.Future[dict[str, object]] = (
                asyncio.get_running_loop().create_future()
            )
            runtime.inspector.pending_commands[command_id] = future
            await socket.send(
                json.dumps(
                    {"id": command_id, "method": method, "params": params or {}},
                    ensure_ascii=False,
                )
            )
            try:
                response = await asyncio.wait_for(
                    future,
                    timeout=runtime.command_timeout_seconds,
                )
            finally:
                runtime.inspector.pending_commands.pop(command_id, None)
        error = response.get("error")
        if isinstance(error, dict):
            message = error.get("message") or "Node Inspector 命令失败"
            raise RuntimeError(str(message))  # noqa: TRY004 - 这是远端协议错误
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise TypeError(f"Node Inspector 响应 result 不是对象: {result!r}")
        return cast(dict[str, object], result)

    async def receive_messages(self, runtime: NodeDebugInspectorRuntime) -> None:
        socket = runtime.inspector.socket
        if socket is None:
            return
        try:
            async for raw_message in socket:
                payload = json.loads(raw_message)
                if not isinstance(payload, dict):
                    raise TypeError(f"Node Inspector 消息不是对象: {payload!r}")
                command_id = payload.get("id")
                if isinstance(command_id, int):
                    future = runtime.inspector.pending_commands.get(command_id)
                    if future is not None and not future.done():
                        future.set_result(cast(dict[str, object], payload))
                    continue
                method = payload.get("method")
                params = payload.get("params")
                if isinstance(method, str) and isinstance(params, dict):
                    await self._handle_event(runtime, method, params)
        except ConnectionClosed:
            if not runtime.closing:
                async with runtime.state_lock:
                    if runtime.process is not None and runtime.process.returncode == 0:
                        runtime.status = "exited"
                        runtime.error_message = runtime.logpoint_error_message
                    else:
                        runtime.status = "failed"
                        runtime.error_message = (
                            runtime.logpoint_error_message
                            or "Node Inspector WebSocket 已断开"
                        )
                    self._clear_stop_snapshot(runtime)
        except Exception as error:  # noqa: BLE001 - 接收循环必须将适配器故障写入状态
            if not runtime.closing:
                async with runtime.state_lock:
                    runtime.status = "failed"
                    runtime.error_message = (
                        runtime.logpoint_error_message
                        or f"读取 Node Inspector 事件失败: {error}"
                    )
                    self._clear_stop_snapshot(runtime)
        finally:
            error = RuntimeError("Node Inspector WebSocket 已关闭")
            for future in tuple(runtime.inspector.pending_commands.values()):
                if not future.done():
                    future.set_exception(error)

    async def _handle_event(
        self,
        runtime: NodeDebugInspectorRuntime,
        method: str,
        params: dict[str, object],
    ) -> None:
        if method == "Debugger.scriptParsed":
            script_id = params.get("scriptId")
            url = params.get("url")
            if isinstance(script_id, str) and isinstance(url, str) and url:
                runtime.inspector.script_urls[script_id] = url
        elif method == "Debugger.paused":
            call_frames = params.get("callFrames")
            frames = self._parse_call_frames(runtime, call_frames)
            hit_breakpoints = params.get("hitBreakpoints")
            async with runtime.state_lock:
                runtime.status = "paused"
                runtime.paused_reason = self._string_or_none(params.get("reason"))
                runtime.paused_breakpoint_ids = (
                    {
                        breakpoint_id
                        for breakpoint_id in hit_breakpoints
                        if isinstance(breakpoint_id, str)
                    }
                    if isinstance(hit_breakpoints, list)
                    else set()
                )
                runtime.error_message = runtime.logpoint_error_message
                runtime.inspector.scope_object_ids = self._scope_object_ids(call_frames)
                runtime.call_stack = frames
                if frames:
                    frame = frames[0]
                    runtime.last_stopped_frame = frame.model_copy(deep=True)
                    for breakpoint_id, breakpoint in runtime.breakpoints.items():
                        if (
                            breakpoint.path != frame.path
                            or breakpoint.line != frame.line
                        ):
                            continue
                        runtime.breakpoints[breakpoint_id] = breakpoint.model_copy(
                            update={
                                "verified": True,
                                "actual_line": frame.line,
                                "inspector_id": runtime.inspector_breakpoint_ids.get(
                                    breakpoint_id
                                ),
                            }
                        )
            if frames:
                runtime.inspector.variable_hydration_task = asyncio.create_task(
                    self._hydrate_frame_variables_safe(runtime, frames[0]),
                )
        elif method == "Debugger.resumed":
            async with runtime.state_lock:
                runtime.status = (
                    "exited"
                    if runtime.process is not None
                    and runtime.process.returncode is not None
                    else "running"
                )
                runtime.error_message = runtime.logpoint_error_message
                self.clear_paused_snapshot(runtime)
        elif method == "NodeRuntime.waitingForDisconnect":
            socket = runtime.inspector.socket
            if socket is not None:
                await socket.close()
                runtime.inspector.socket = None

    async def _hydrate_frame_variables_safe(
        self,
        runtime: NodeDebugInspectorRuntime,
        frame: NodeDebugStackFrameDTO,
    ) -> None:
        try:
            await self._hydrate_frame_variables(runtime, frame)
        except Exception as error:  # noqa: BLE001 - 变量读取故障必须暴露在调试状态
            if "Cannot find context with specified id" in str(error):
                return
            async with runtime.state_lock:
                if (
                    runtime.status == "paused"
                    and runtime.call_stack
                    and runtime.call_stack[0].call_frame_id == frame.call_frame_id
                ):
                    runtime.error_message = f"读取局部变量失败: {error}"

    async def _hydrate_frame_variables(
        self,
        runtime: NodeDebugInspectorRuntime,
        frame: NodeDebugStackFrameDTO,
    ) -> None:
        object_ids = runtime.inspector.scope_object_ids.get(frame.call_frame_id, {})
        variables: list[NodeDebugVariableDTO] = []
        expired_object_count = 0
        for scope, scope_object_ids in object_ids.items():
            for object_id in scope_object_ids[:3]:
                try:
                    result = await self.command(
                        runtime,
                        "Runtime.getProperties",
                        {
                            "objectId": object_id,
                            "ownProperties": True,
                            "accessorPropertiesOnly": False,
                        },
                    )
                except RuntimeError as error:
                    if "Could not find object with given id" not in str(error):
                        raise
                    expired_object_count += 1
                    continue
                properties = result.get("result")
                if not isinstance(properties, list):
                    continue
                for property_value in properties:
                    if not isinstance(property_value, dict):
                        continue
                    name = property_value.get("name")
                    if not isinstance(name, str):
                        continue
                    remote_value = property_value.get("value")
                    variables.append(
                        NodeDebugVariableDTO(
                            name=name,
                            value=self.remote_value(remote_value) or "undefined",
                            type=self.remote_type(remote_value),
                            object_id=self.remote_object_id(remote_value),
                            scope=scope,
                        )
                    )
        async with runtime.state_lock:
            if runtime.status != "paused" or not runtime.call_stack:
                return
            if runtime.call_stack[0].call_frame_id != frame.call_frame_id:
                return
            runtime.call_stack[0] = frame.model_copy(update={"variables": variables})
            if variables:
                runtime.error_message = runtime.logpoint_error_message
                if expired_object_count:
                    self._append_action(
                        runtime,
                        "variable_scope_skipped",
                        f"已跳过 {expired_object_count} 个失效的 Inspector 变量对象；其余变量已返回",
                        actor="system",
                        result="error",
                    )
            elif expired_object_count:
                runtime.error_message = (
                    "读取局部变量失败：暂停期间 Inspector 变量对象已经失效"
                )

    async def wait_for_execution_state(self, runtime: NodeDebugInspectorRuntime) -> None:
        for _ in range(200):
            async with runtime.state_lock:
                if runtime.status in {"paused", "exited", "failed"}:
                    return
            await asyncio.sleep(0.01)

    async def wait_for_frame_variables(self, runtime: NodeDebugInspectorRuntime) -> None:
        for _ in range(100):
            task = runtime.inspector.variable_hydration_task
            if task is not None:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=runtime.command_timeout_seconds,
                )
                return
            async with runtime.state_lock:
                if runtime.status != "paused":
                    return
            await asyncio.sleep(0.01)

    def clear_paused_snapshot(self, runtime: NodeDebugInspectorRuntime) -> None:
        runtime.paused_reason = None
        runtime.paused_breakpoint_ids.clear()
        runtime.call_stack.clear()
        runtime.inspector.scope_object_ids.clear()
        # 求值结果跟随当前暂停上下文，继续执行后不再属于当前快照。
        runtime.last_evaluation = None

    def _parse_call_frames(
        self,
        runtime: NodeDebugInspectorRuntime,
        value: object,
    ) -> list[NodeDebugStackFrameDTO]:
        if not isinstance(value, list):
            return []
        frames: list[NodeDebugStackFrameDTO] = []
        for raw_frame in value:
            if not isinstance(raw_frame, dict):
                continue
            location = raw_frame.get("location")
            if not isinstance(location, dict):
                continue
            raw_url = raw_frame.get("url")
            url = raw_url if isinstance(raw_url, str) else ""
            line = location.get("lineNumber")
            column = location.get("columnNumber")
            call_frame_id = raw_frame.get("callFrameId")
            if not isinstance(call_frame_id, str) or not isinstance(line, int):
                continue
            if not url:
                script_id = location.get("scriptId")
                if isinstance(script_id, str):
                    url = runtime.inspector.script_urls.get(script_id, "")
            frames.append(
                NodeDebugStackFrameDTO(
                    call_frame_id=call_frame_id,
                    function_name=str(raw_frame.get("functionName") or "<anonymous>"),
                    url=url,
                    path=self._url_to_workspace_path(url),
                    line=line + 1,
                    column=(column if isinstance(column, int) else 0) + 1,
                    scope_names=self._scope_names(raw_frame.get("scopeChain")),
                )
            )
        return frames

    @staticmethod
    def _scope_object_ids(value: object) -> dict[str, dict[str, list[str]]]:
        if not isinstance(value, list):
            return {}
        result: dict[str, dict[str, list[str]]] = {}
        for raw_frame in value:
            if not isinstance(raw_frame, dict):
                continue
            call_frame_id = raw_frame.get("callFrameId")
            scope_chain = raw_frame.get("scopeChain")
            if not isinstance(call_frame_id, str) or not isinstance(scope_chain, list):
                continue
            object_ids: dict[str, list[str]] = {
                "local": [],
                "global": [],
            }
            for scope in scope_chain:
                if not isinstance(scope, dict):
                    continue
                scope_object = scope.get("object")
                if not isinstance(scope_object, dict):
                    continue
                object_id = scope_object.get("objectId")
                if isinstance(object_id, str):
                    scope_name = "global" if scope.get("type") == "global" else "local"
                    object_ids[scope_name].append(object_id)
            result[call_frame_id] = object_ids
        return result

    @staticmethod
    def _scope_names(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            str(scope.get("name"))
            for scope in value
            if isinstance(scope, dict) and isinstance(scope.get("name"), str)
        ]

    @staticmethod
    def _string_or_none(value: object) -> str | None:
        return value if isinstance(value, str) else None

    def _url_to_workspace_path(self, url: str) -> str | None:
        if not url.startswith("file:"):
            return None
        path = Path(unquote(urlparse(url).path)).resolve()
        try:
            return path.relative_to(self._workspace_root).as_posix()
        except ValueError:
            return None

    @staticmethod
    def remote_value(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        if "value" in value:
            return json.dumps(value["value"], ensure_ascii=False)
        for key in ("unserializableValue", "description"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        return None

    @staticmethod
    def remote_type(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        candidate = value.get("type")
        return candidate if isinstance(candidate, str) else None

    @staticmethod
    def remote_description(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        candidate = value.get("description")
        return candidate if isinstance(candidate, str) else None

    @staticmethod
    def remote_object_id(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        candidate = value.get("objectId")
        return candidate if isinstance(candidate, str) else None

    @classmethod
    def exception_message(cls, value: dict[str, object]) -> str:
        details = value.get("exception")
        return cls.remote_description(details) or "表达式求值失败"
