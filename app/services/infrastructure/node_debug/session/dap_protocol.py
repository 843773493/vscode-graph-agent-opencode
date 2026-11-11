"""Workbench DAP 请求到现有 NodeDebug owner 的协议映射。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from app.schemas.internal_v2.node_debug import (
    NodeDebugClearBreakpointActionRequest,
    NodeDebugClearBreakpointParams,
    NodeDebugControlActionRequest,
    NodeDebugEvaluateActionRequest,
    NodeDebugEvaluateParams,
    NodeDebugSetBreakpointActionRequest,
    NodeDebugSetBreakpointParams,
)
from app.services.infrastructure.node_debug.runtime_state import publish_dap_event

if TYPE_CHECKING:
    from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
    from app.services.infrastructure.node_debug.service import NodeDebugService


class NodeDebugDAPSession:
    def __init__(self, service: NodeDebugService, runtime: NodeDebugRuntime) -> None:
        self._service = service
        self._runtime = runtime
        self._owner = (runtime.session_id, runtime.thread_id)
        self._sequence = 1
        self._frames: dict[int, tuple[str, int]] = {}
        self._scopes: dict[int, tuple[str, int, int]] = {}
        self._reference = 1
        self._request_pause_generation = runtime.pause_generation

    async def dispatch(self, message: object) -> dict[str, object]:
        if not isinstance(message, dict) or message.get("type") != "request":
            raise ValueError("DAP 输入必须是 request 对象")
        request_seq = message.get("seq")
        command = message.get("command")
        arguments = message.get("arguments", {})
        if type(request_seq) is not int or not isinstance(command, str) or not isinstance(arguments, dict):
            raise ValueError("DAP request 缺少有效 seq、command 或 arguments")
        success = True
        body: dict[str, object] = {}
        error_message: str | None = None
        try:
            async with (
                self._service._admit_mutation(*self._owner),
                self._service._owner_lock(self._owner),
            ):
                if self._service._runtimes.get(self._owner) is not self._runtime:
                    raise RuntimeError("Node Debug run 已更换")
                if self._request_pause_generation != self._runtime.pause_generation:
                    self._frames.clear()
                    self._scopes.clear()
                self._request_pause_generation = self._runtime.pause_generation
                body = await self._dispatch(command, arguments)
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            success = False
            error_message = str(error)
        response: dict[str, object] = {
            "seq": self._next_seq(), "type": "response", "request_seq": request_seq,
            "command": command, "success": success,
        }
        if success:
            response["body"] = body
        else:
            response["message"] = error_message or "DAP 请求失败"
            response["body"] = {"error": {"id": 1, "format": error_message or "DAP 请求失败"}}
        return response

    async def _dispatch(self, command: str, args: dict[str, object]) -> dict[str, object]:
        if command == "initialize":
            return {
                "supportsConfigurationDoneRequest": True,
                "supportsConditionalBreakpoints": True,
                "supportsHitConditionalBreakpoints": True,
                "supportsLogPoints": True,
                "supportsEvaluateForHovers": True,
                "supportsTerminateRequest": True,
            }
        if command == "launch":
            return {}
        if command == "configurationDone":
            # --inspect-brk 的启动暂停不是用户断点；先让原 Inspector owner
            # 结清启动握手，再继续到同一套断点，不能把第1行冒充用户断点。
            await self._service._inspector.command(self._runtime, "Runtime.runIfWaitingForDebugger")
            await self._service._inspector.wait_for_execution_state(self._runtime)
            if (self._runtime.status == "paused"
                    and not self._service._observer.paused_at_breakpoint(self._runtime)):
                await self._service._inspector.debugger_command(
                    self._runtime, "continue", actor="system", tool_name=None, tool_call_id=None,
                )
            self._runtime.configuration_done = True
            if self._runtime.status == "paused":
                publish_dap_event(self._runtime, "stopped", {
                    "reason": "breakpoint", "threadId": 1, "allThreadsStopped": True,
                })
            return {}
        if command == "source":
            raise ValueError("当前调试器不提供 sourceReference；请通过源码资源 URI 读取文件")
        if command == "threads":
            return {"threads": [{"id": 1, "name": "Node.js"}]}
        if command == "stackTrace":
            state = await self._service._get_state_admitted(*self._owner)
            frame_ids = {binding[0]: frame_id for frame_id, binding in self._frames.items()}
            frames = []
            for frame in state.call_stack:
                frame_id = frame_ids.get(frame.call_frame_id)
                if frame_id is None:
                    frame_id = self._next_reference()
                self._frames[frame_id] = (frame.call_frame_id, self._runtime.pause_generation)
                source: dict[str, object] = {"name": frame.function_name, "sourceReference": 0}
                if frame.url.startswith("boxteam://"):
                    source["path"] = frame.url
                frames.append({
                    "id": frame_id, "name": frame.function_name, "line": frame.line,
                    "column": frame.column, "source": source,
                })
            return {"stackFrames": frames, "totalFrames": len(frames)}
        if command == "scopes":
            frame_id = self._int(args, "frameId")
            frame_binding = self._frames.get(frame_id)
            if frame_binding is None or frame_binding[1] != self._runtime.pause_generation:
                raise ValueError("DAP frameId 已失效")
            call_frame_id = frame_binding[0]
            state = await self._service._get_state_admitted(*self._owner)
            frame = next((item for item in state.call_stack if item.call_frame_id == call_frame_id), None)
            if frame is None:
                raise ValueError("DAP frameId 已不属于当前暂停")
            scopes = []
            scope_ids = {binding[0]: scope_id for scope_id, binding in self._scopes.items()
                         if binding[1] == frame_id}
            for name in frame.scope_names:
                scope_id = scope_ids.get(name)
                if scope_id is None:
                    scope_id = self._next_reference()
                self._scopes[scope_id] = (name, frame_id, self._runtime.pause_generation)
                scopes.append({"name": name.title(),
                               "presentationHint": "global" if name == "global" else "local",
                               "variablesReference": scope_id, "expensive": name == "global"})
            return {"scopes": scopes}
        if command == "variables":
            reference = self._int(args, "variablesReference")
            scope = self._scopes.get(reference)
            if (scope is None or scope[1] not in self._frames
                    or scope[2] != self._runtime.pause_generation):
                raise ValueError("DAP variablesReference 已失效")
            frame_binding = self._frames.get(scope[1])
            if frame_binding is None or frame_binding[1] != self._runtime.pause_generation:
                raise ValueError("DAP variablesReference 不属于当前调用帧")
            variables = await self._service._inspector.variables_for_frame(
                self._runtime, frame_binding[0], scope[0],
            )
            return {"variables": [
                {"name": item.name, "value": item.value, "type": item.type or "", "variablesReference": 0}
                for item in variables
            ]}
        if command == "setBreakpoints":
            return await self._set_breakpoints(args)
        if command == "setExceptionBreakpoints":
            filters = args.get("filters", [])
            if not isinstance(filters, list):
                raise TypeError("DAP exception filters 必须是数组")
            if filters:
                raise ValueError("当前 Node Debug 插件不支持异常断点过滤器")
            return {}
        if command in {"continue", "next", "stepIn", "stepOut", "pause"}:
            action_by_command: dict[str, Literal[
                "continue", "pause", "step_over", "step_into", "step_out",
            ]] = {
                "continue": "continue", "next": "step_over", "stepIn": "step_into",
                "stepOut": "step_out", "pause": "pause",
            }
            await self._service._apply_action_admitted(self._owner, command=NodeDebugControlActionRequest(
                session_id=self._owner[0], thread_id=self._owner[1], action=action_by_command[command],
            ), _expected_runtime=self._runtime,
                _expected_pause_generation=(
                    self._request_pause_generation if command != "pause" else None
                ))
            if command in {"continue", "next", "stepIn", "stepOut"}:
                self._frames.clear()
                self._scopes.clear()
            return {"allThreadsContinued": True} if command != "pause" else {}
        if command == "evaluate":
            frame_id = args.get("frameId")
            if frame_id is not None and type(frame_id) is not int:
                raise ValueError("DAP evaluate frameId 必须是整数")
            frame_binding = self._frames.get(frame_id) if type(frame_id) is int else None
            if frame_id is not None and (frame_binding is None
                    or frame_binding[1] != self._runtime.pause_generation):
                raise ValueError("DAP evaluate frameId 已失效")
            call_frame_id = frame_binding[0] if frame_binding is not None else None
            expression = args.get("expression")
            if not isinstance(expression, str) or not expression:
                raise ValueError("DAP evaluate 缺少 expression")
            state = await self._service._apply_action_admitted(self._owner, command=NodeDebugEvaluateActionRequest(
                action="evaluate", session_id=self._owner[0], thread_id=self._owner[1],
                params=NodeDebugEvaluateParams(expression=expression, call_frame_id=call_frame_id),
            ), _expected_runtime=self._runtime,
                _expected_pause_generation=(
                    self._request_pause_generation if frame_id is not None else None
                ))
            result = state.last_evaluation
            if result is None:
                raise RuntimeError("Node Debug owner 没有返回 evaluate 结果")
            if result.error:
                raise RuntimeError(result.error)
            return {"result": result.value or "undefined", "variablesReference": 0,
                    "type": result.type or ""}
        if command in {"disconnect", "terminate"}:
            terminate = command == "terminate" or args.get("terminateDebuggee") is True
            if terminate:
                await self._service._apply_action_admitted(self._owner, command=NodeDebugControlActionRequest(
                    session_id=self._owner[0], thread_id=self._owner[1], action="stop",
                ), _expected_runtime=self._runtime)
            return {}
        raise ValueError(f"当前 Node Debug DAP 不支持请求: {command}")

    async def _set_breakpoints(self, args: dict[str, object]) -> dict[str, object]:
        source = args.get("source")
        raw_path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(raw_path, str) or not raw_path.startswith("boxteam://"):
            raise ValueError("DAP source.path 必须是目标 files 资源 URI")
        target, _ = self._service._file_paths.resolve(raw_path, allow_external=False)
        try:
            path = target.relative_to(self._runtime.workspace_root).as_posix()
        except ValueError as error:
            raise ValueError("DAP 断点当前只支持目标 Workspace 内的源码") from error
        raw_breakpoints = args.get("breakpoints", [])
        if not isinstance(raw_breakpoints, list):
            raise TypeError("DAP breakpoints 必须是数组")
        requested: list[NodeDebugSetBreakpointParams] = []
        for raw in raw_breakpoints:
            if not isinstance(raw, dict) or type(raw.get("line")) is not int:
                raise ValueError("DAP breakpoint 缺少整数 line")
            column = raw.get("column", 1)
            condition = raw.get("condition")
            hit_condition = raw.get("hitCondition")
            log_message = raw.get("logMessage")
            if type(column) is not int:
                raise TypeError("DAP breakpoint column 必须是整数")
            if condition is not None and not isinstance(condition, str):
                raise TypeError("DAP breakpoint condition 必须是字符串")
            if hit_condition is not None:
                if not isinstance(hit_condition, str) or not hit_condition.isdecimal():
                    raise ValueError("当前 Node Debug 仅支持正整数 hitCondition")
                hit_condition = int(hit_condition)
            if log_message is not None and not isinstance(log_message, str):
                raise TypeError("DAP breakpoint logMessage 必须是字符串")
            requested.append(NodeDebugSetBreakpointParams(
                path=path, line=raw["line"], column=column, condition=condition,
                hit_condition=hit_condition, log_message=log_message,
            ))

        state = await self._service._get_state_admitted(*self._owner)
        for breakpoint in state.breakpoints:
            if breakpoint.path == path:
                await self._service._apply_action_admitted(self._owner, command=NodeDebugClearBreakpointActionRequest(
                    action="clear_breakpoint", session_id=self._owner[0], thread_id=self._owner[1],
                    params=NodeDebugClearBreakpointParams(breakpoint_id=breakpoint.breakpoint_id),
                ), _expected_runtime=self._runtime)
        results = []
        for params in requested:
            state = await self._service._apply_action_admitted(self._owner, command=NodeDebugSetBreakpointActionRequest(
                action="set_breakpoint", session_id=self._owner[0], thread_id=self._owner[1],
                params=params,
            ), _expected_runtime=self._runtime)
            breakpoint = next((item for item in state.breakpoints
                               if item.path == path and item.line == params.line), None)
            if breakpoint is None:
                raise RuntimeError("Node Debug owner 未保存请求的源码断点")
            dap_id = self._next_reference()
            results.append({"id": dap_id, "verified": breakpoint.verified, "line": breakpoint.actual_line or params.line})
        return {"breakpoints": results}

    def _next_seq(self) -> int:
        value = self._sequence
        self._sequence += 1
        return value

    def event(self, name: str, body: dict[str, object]) -> dict[str, object]:
        return {"seq": self._next_seq(), "type": "event", "event": name, "body": body}

    def _next_reference(self) -> int:
        value = self._reference
        self._reference += 1
        return value

    @staticmethod
    def _int(value: dict[str, object], name: str) -> int:
        result = value.get(name)
        if type(result) is not int:
            raise ValueError(f"DAP {name} 必须是整数")
        return result
