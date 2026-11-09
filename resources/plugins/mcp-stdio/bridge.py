"""用官方 MCP transport 将插件持有的 stdio Server 暴露为 Streamable HTTP。"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from os import PathLike
from typing import Any, TypeAlias, cast
from uuid import uuid4

import anyio
import uvicorn
from anyio.abc import ObjectReceiveStream, ObjectSendStream, Process, TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.text import TextReceiveStream
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp import StdioServerParameters, types
from mcp.client.stdio import get_default_environment
from mcp.os.win32.utilities import get_windows_executable_command
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.shared.message import ServerMessageMetadata, SessionMessage
from mcp.types import CONNECTION_CLOSED, METHOD_NOT_FOUND
from pydantic import ValidationError

from app.runtime.plugin_observation import plugin_observation_router


class OwnedChildProcess:
    """MCP Server 原始进程句柄；关闭时等待该句柄和本机进程组退出。"""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.process: Process | None = None

    async def start(self, command: Sequence[str], *, env: dict[str, str], cwd: str | PathLike[str] | None) -> None:
        self.process = await anyio.open_process(command, env=env, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=None, start_new_session=os.name == "posix")

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            await process.stdin.aclose()
        try:
            with anyio.fail_after(2):
                await process.wait()
        except TimeoutError:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                with anyio.fail_after(2):
                    await process.wait()
            except TimeoutError:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                with anyio.fail_after(2):
                    await process.wait()
        await process.aclose()


@asynccontextmanager
async def mcp_stdio_transport(parameters: StdioServerParameters, process: OwnedChildProcess,
                              on_started: Callable[[], Awaitable[None]]) -> AsyncIterator[
                                  tuple[MemoryObjectReceiveStream[SessionMessage | Exception],
                                        MemoryObjectSendStream[SessionMessage]]]:
    incoming, read = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write, outgoing = anyio.create_memory_object_stream[SessionMessage](0)

    async def receive() -> None:
        assert process.process is not None and process.process.stdout is not None
        async with incoming:
            pending = ""
            async for chunk in TextReceiveStream(process.process.stdout, encoding=parameters.encoding,
                                                  errors=parameters.encoding_error_handler):
                lines = (pending + chunk).split("\n")
                pending = lines.pop()
                for line in lines:
                    try:
                        message = SessionMessage(types.JSONRPCMessage.model_validate_json(line))
                    except ValidationError as error:
                        raise ValueError(f"MCP JSON-RPC 类型校验失败: count={error.error_count()}") from None
                    await incoming.send(message)

    async def send() -> None:
        assert process.process is not None and process.process.stdin is not None
        async with outgoing:
            async for message in outgoing:
                payload = message.message.model_dump_json(by_alias=True, exclude_none=True)
                await process.process.stdin.send((payload + "\n").encode(parameters.encoding,
                    parameters.encoding_error_handler))

    try:
        async with anyio.create_task_group() as group:
            try:
                command = get_windows_executable_command(parameters.command) if sys.platform == "win32" else parameters.command
                environment = {**get_default_environment(), **(parameters.env or {})}
                await process.start([command, *parameters.args], env=environment, cwd=parameters.cwd)
                await on_started()
                group.start_soon(receive)
                group.start_soon(send)
                yield read, write
            finally:
                group.cancel_scope.cancel()
                with anyio.CancelScope(shield=True):
                    await process.close()
    finally:
        read.close()
        write.close()
        incoming.close()
        outgoing.close()


def verify_local_control_request(request: Request) -> None:
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        or "origin" in request.headers or "sec-fetch-site" in request.headers):
        raise HTTPException(403, "运行时控制只接受本机管理通道")


def verify_runtime_control_token(request: Request) -> None:
    expected = os.environ.get("BOXTEAM_RUNTIME_CONTROL_TOKEN")
    actual = request.headers.get("x-local-token")
    if not expected:
        raise HTTPException(503, "此入口未配置本机运行时控制凭据")
    if actual is None or not secrets.compare_digest(actual.encode(), expected.encode()):
        raise HTTPException(401, "invalid runtime control token")


@dataclass(slots=True)
class _RequestRoute:
    client_id: str | int
    generation: int
    method: str


_JSONRPCMessageRoot: TypeAlias = (
    types.JSONRPCRequest
    | types.JSONRPCNotification
    | types.JSONRPCResponse
    | types.JSONRPCError
)


@dataclass(slots=True)
class _HttpSession:
    bridge: _Bridge
    transport: StreamableHTTPServerTransport
    session_id: str
    incoming: ObjectReceiveStream[SessionMessage | Exception] | None = None
    outgoing: ObjectSendStream[SessionMessage] | None = None
    task: asyncio.Task[None] | None = None
    closed: bool = False
    post_requests: set[asyncio.Task[None]] = field(default_factory=set)

    async def run(
        self, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        try:
            async with self.transport.connect() as (incoming, outgoing):
                self.incoming, self.outgoing = incoming, outgoing
                async with anyio.create_task_group() as group:
                    group.start_soon(self.bridge._http_to_stdio, self)
                    task_status.started()
                    await anyio.sleep_forever()
        finally:
            self.closed = True

    async def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.transport.terminate()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class _Bridge:
    def __init__(self) -> None:
        self._stdio_read: (
            ObjectReceiveStream[SessionMessage | Exception] | None
        ) = None
        self._stdio_write: ObjectSendStream[SessionMessage] | None = None
        self._lock = asyncio.Lock()
        self._active: _HttpSession | None = None
        self._generation = 0
        self._next_request_id = 1
        self._routes: dict[str, _RequestRoute] = {}
        self._client_requests: dict[tuple[int, str], str] = {}
        self._progress_routes: dict[str, tuple[_RequestRoute, str | int]] = {}
        self._init_result: dict[str, Any] | None = None
        self._client_capabilities: object | None = None
        self._initialized = False

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        parameters = _stdio_parameters()
        process = OwnedChildProcess(f"mcp-plugin:{uuid4().hex}")
        async with (
            anyio.create_task_group() as task_group,
            mcp_stdio_transport(parameters, process, _started) as streams,
        ):
            self._stdio_read, self._stdio_write = streams
            task_group.start_soon(self._stdio_to_http)
            try:
                yield
            finally:
                active = self._active
                if active is not None:
                    await active.stop()
                task_group.cancel_scope.cancel()
                self._active = None
                self._stdio_read = None
                self._stdio_write = None

    async def route(self, scope, receive, send) -> None:
        request = Request(scope, receive)
        verify_local_control_request(request)
        verify_runtime_control_token(request)
        session_id = request.headers.get("mcp-session-id")
        async with self._lock:
            if request.method == "POST" and session_id is None:
                transport = StreamableHTTPServerTransport(uuid4().hex)
                current = _HttpSession(self, transport, transport.mcp_session_id or "")
                current.task = asyncio.create_task(current.run())
                await _wait_session_started(current)
                previous, self._active = self._active, current
                if previous is not None:
                    await previous.stop()
            else:
                current = self._active
                if (
                    current is None
                    or session_id != current.transport.mcp_session_id
                    or current.closed
                ):
                    raise HTTPException(404, "MCP Streamable HTTP session 已失效")
        request_task = asyncio.current_task()
        if request.method == "POST" and request_task is not None:
            current.post_requests.add(request_task)
        try:
            await current.transport.handle_request(scope, receive, send)
        finally:
            if request_task is not None:
                current.post_requests.discard(request_task)
        if request.method == "DELETE":
            async with self._lock:
                if self._active is current:
                    self._active = None
            await current.stop()

    async def _http_to_stdio(self, session: _HttpSession) -> None:
        assert session.incoming is not None and self._stdio_write is not None
        async for message in session.incoming:
            if isinstance(message, Exception):
                raise message
            root = message.message.root
            if isinstance(root, types.JSONRPCRequest) and root.method == "initialize":
                await self._initialize(session, message)
                continue
            if session is not self._active or session.closed:
                continue
            if isinstance(root, types.JSONRPCNotification):
                if root.method == "notifications/initialized":
                    if self._initialized:
                        continue
                    self._initialized = True
                elif root.method == "notifications/cancelled":
                    params = dict(root.params or {})
                    original_id = params.get("requestId")
                    mapped = self._client_requests.get(
                        (self._generation, str(original_id))
                    )
                    if mapped is None:
                        continue
                    params["requestId"] = int(mapped)
                    message = _replace_message(
                        message, root.model_copy(update={"params": params})
                    )
                elif root.method == "notifications/progress":
                    params = dict(root.params or {})
                    token = str(params.get("progressToken"))
                    route = self._progress_routes.get(token)
                    if route is None or route[0].generation != self._generation:
                        continue
                    params["progressToken"] = route[1]
                    message = _replace_message(
                        message, root.model_copy(update={"params": params})
                    )
            elif isinstance(root, types.JSONRPCRequest):
                internal_id = str(self._next_request_id)
                self._next_request_id += 1
                params = dict(root.params or {})
                self._routes[internal_id] = _RequestRoute(
                    root.id, self._generation, root.method
                )
                self._client_requests[(self._generation, str(root.id))] = internal_id
                metadata = params.get("_meta")
                if isinstance(metadata, dict) and "progressToken" in metadata:
                    token = f"mcp-plugin:{self._generation}:{internal_id}"
                    self._progress_routes[token] = (
                        self._routes[internal_id],
                        metadata["progressToken"],
                    )
                    params["_meta"] = {**metadata, "progressToken": token}
                    root = root.model_copy(update={"id": int(internal_id), "params": params})
                else:
                    root = root.model_copy(update={"id": int(internal_id)})
                message = _replace_message(message, root)
            elif isinstance(root, (types.JSONRPCResponse, types.JSONRPCError)):
                raise TypeError(
                    "MCP bridge 首版不支持上游 Server 发起的 Client Request"
                )
            await self._stdio_write.send(message)

    async def _initialize(self, session: _HttpSession, message: SessionMessage) -> None:
        assert session.outgoing is not None and self._stdio_write is not None
        request = cast(types.JSONRPCRequest, message.message.root)
        params = request.params or {}
        capabilities = params.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            await session.outgoing.send(
                SessionMessage(
                    types.JSONRPCMessage(
                        types.JSONRPCError(
                            jsonrpc="2.0",
                            id=request.id,
                            error=types.ErrorData(
                                code=-32602,
                                message="MCP Client capabilities 必须是对象",
                            ),
                        )
                    )
                )
            )
            return
        if capabilities:
            await session.outgoing.send(
                SessionMessage(
                    types.JSONRPCMessage(
                        types.JSONRPCError(
                            jsonrpc="2.0",
                            id=request.id,
                            error=types.ErrorData(
                                code=-32600,
                                message="MCP bridge 不支持 Client capabilities",
                            ),
                        )
                    )
                )
            )
            return
        self._generation += 1
        self._client_requests.clear()
        if self._init_result is not None:
            if capabilities != self._client_capabilities:
                await session.outgoing.send(
                    SessionMessage(
                        types.JSONRPCMessage(
                            types.JSONRPCError(
                                jsonrpc="2.0",
                                id=request.id,
                                error=types.ErrorData(
                                    code=-32600,
                                    message="重连时 MCP client capabilities 发生变化；需重新配置插件实例",
                                ),
                            )
                        )
                    )
                )
                return
            await session.outgoing.send(
                SessionMessage(
                    types.JSONRPCMessage(
                        types.JSONRPCResponse(
                            jsonrpc="2.0",
                            id=request.id,
                            result=self._init_result,
                        )
                    )
                )
            )
            return
        internal_id = str(self._next_request_id)
        self._next_request_id += 1
        self._routes[internal_id] = _RequestRoute(
            request.id, self._generation, "initialize"
        )
        self._client_requests[(self._generation, str(request.id))] = internal_id
        self._client_capabilities = capabilities
        await self._stdio_write.send(
            _replace_message(
                message, request.model_copy(update={"id": int(internal_id)})
            )
        )

    async def _stdio_to_http(self) -> None:
        assert self._stdio_read is not None
        async for message in self._stdio_read:
            if isinstance(message, Exception):
                raise message
            root = message.message.root
            if isinstance(root, (types.JSONRPCResponse, types.JSONRPCError)):
                route = self._routes.pop(str(root.id), None)
                session = self._active
                if (
                    route is None
                    or session is None
                    or session.outgoing is None
                    or route.generation != self._generation
                ):
                    continue
                self._client_requests.pop((route.generation, str(route.client_id)), None)
                self._progress_routes.pop(
                    f"mcp-plugin:{route.generation}:{root.id}", None
                )
                if route.method == "initialize" and isinstance(
                    root, types.JSONRPCResponse
                ):
                    self._init_result = dict(root.result)
                await session.outgoing.send(
                    _replace_message(
                        message, root.model_copy(update={"id": route.client_id})
                    )
                )
                continue
            session = self._active
            if session is None or session.closed or session.outgoing is None:
                continue
            if isinstance(root, types.JSONRPCRequest):
                assert self._stdio_write is not None
                await self._stdio_write.send(
                    _replace_message(
                        message,
                        types.JSONRPCError(
                            jsonrpc="2.0",
                            id=root.id,
                            error=types.ErrorData(
                                code=METHOD_NOT_FOUND,
                                message="MCP bridge 未声明或实现 Server 发起的 Client Request 能力",
                            ),
                        ),
                    )
                )
                continue
            if (
                isinstance(root, types.JSONRPCNotification)
                and root.method == "notifications/progress"
            ):
                params = dict(root.params or {})
                token = str(params.get("progressToken"))
                route = self._progress_routes.get(token)
                if route is None or route[0].generation != self._generation:
                    continue
                params["progressToken"] = route[1]
                # 进度必须先于结果进入同一 POST 流，不能借独立 GET 流竞争交付。
                message = SessionMessage(
                    types.JSONRPCMessage(root.model_copy(update={"params": params})),
                    metadata=ServerMessageMetadata(related_request_id=route[0].client_id),
                )
            await session.outgoing.send(message)
        active = self._active
        if active is not None:
            assert active.outgoing is not None
            for route in tuple(self._routes.values()):
                if route.generation == self._generation:
                    await active.outgoing.send(
                        SessionMessage(
                            types.JSONRPCMessage(
                                types.JSONRPCError(
                                    jsonrpc="2.0",
                                    id=route.client_id,
                                    error=types.ErrorData(
                                        code=CONNECTION_CLOSED,
                                        message="MCP stdio Server 在响应前关闭了协议流",
                                    ),
                                )
                            )
                        )
                    )
            self._routes.clear()
            self._client_requests.clear()
            self._progress_routes.clear()
            requests = tuple(active.post_requests)
            if requests:
                # TODO：stdio 提前 EOF 时，已接收的 HTTP POST 完成较慢会延长错误响应。
                await asyncio.gather(*requests, return_exceptions=True)
            await active.stop()
        raise RuntimeError("MCP stdio Server 在插件正常停止前关闭了协议流")


def _replace_message(
    message: SessionMessage, root: _JSONRPCMessageRoot
) -> SessionMessage:
    return SessionMessage(types.JSONRPCMessage(root), metadata=message.metadata)


async def _started() -> None:
    return None


async def _wait_session_started(session: _HttpSession) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while session.incoming is None:
        if session.task is None or session.task.done():
            if session.task is not None:
                await session.task
            raise RuntimeError("MCP HTTP session 启动失败")
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("MCP HTTP session 启动超时")
        await asyncio.sleep(0)


def _stdio_parameters() -> StdioServerParameters:
    parameters = json.loads(os.environ.get("BOXTEAM_PLUGIN_PARAMETERS", "{}"))
    command, args = parameters.get("command"), parameters.get("args", [])
    if (
        not isinstance(command, str)
        or not command
        or not isinstance(args, list)
        or any(not isinstance(arg, str) for arg in args)
    ):
        raise ValueError("MCP 插件参数必须包含 command 字符串和 args 字符串数组")
    cwd = parameters.get("cwd")
    env = parameters.get("env", {})
    if cwd is not None and not isinstance(cwd, str):
        raise TypeError("MCP 插件 cwd 必须是字符串")
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise TypeError("MCP 插件 env 必须是字符串字典")
    return StdioServerParameters(command=command, args=args, cwd=cwd, env=env)


def create_app() -> FastAPI:
    bridge = _Bridge()
    server: uvicorn.Server | None = None
    identity = json.loads(os.environ["BOXTEAM_PLUGIN_INSTANCE"])

    def runtime_identity() -> dict[str, object]:
        sink = json.loads(os.environ["BOXTEAM_RUNTIME_LOG_SINK"])
        fields = {"path", "owner_id", "role", "source", "device", "inode"}
        if not isinstance(sink, dict) or set(sink) != fields or sink["role"] != "service-plugin":
            raise RuntimeError("受管进程日志去向非法")
        expected = (sink["device"], sink["inode"])
        descriptor_ids = [os.fstat(1), os.fstat(2)]
        if any((str(value.st_dev), str(value.st_ino)) != expected for value in descriptor_ids):
            raise RuntimeError("受管进程日志描述符与冻结去向不一致")
        path_stat = os.stat(sink["path"])
        if (str(path_stat.st_dev), str(path_stat.st_ino)) != expected:
            raise RuntimeError("受管进程日志文件与冻结去向不一致")
        return {**identity, "status": "ready", "process_id": os.getpid(),
                "capabilities": ["status", "shutdown", "observation"], "log_sink": sink}

    async def shutdown() -> None:
        if app.state.server is not None:
            app.state.server.should_exit = True

    app = FastAPI(lifespan=bridge.lifespan)
    app.state.server = server
    router = APIRouter(dependencies=[Depends(verify_local_control_request), Depends(verify_runtime_control_token)])
    router.include_router(plugin_observation_router(instance_id=identity["instance_id"], service=identity["plugin_id"],
        verify_local=verify_local_control_request, verify_token=verify_runtime_control_token))

    @router.get("/internal/plugin/status")
    async def status() -> dict[str, object]:
        return runtime_identity()

    @router.post("/internal/plugin/shutdown")
    async def stop(background: BackgroundTasks) -> dict[str, object]:
        background.add_task(shutdown)
        return {"accepted": True, "process_id": os.getpid(), "instance_id": identity["instance_id"]}

    app.include_router(router)
    app.mount("/services/mcp", _McpServiceApp(bridge))
    return app


def main() -> None:
    app = create_app()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=int(os.environ["BOXTEAM_PLUGIN_PORT"]),
        lifespan="on",
    )
    server = uvicorn.Server(config)
    app.state.server = server
    server.run()
    if not server.started or server.lifespan.shutdown_failed:
        raise SystemExit(3)


class _McpServiceApp:
    def __init__(self, bridge: _Bridge) -> None:
        self.bridge = bridge

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        try:
            await self.bridge.route(scope, receive, send)
        except HTTPException as error:
            await JSONResponse(
                status_code=error.status_code, content={"detail": error.detail}
            )(scope, receive, send)


if __name__ == "__main__":
    main()
