"""Backend 到冻结 Debug 插件实例的单一运行控制通道。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.identifier import create_prefixed_id
from app.core.trace_middleware import get_current_request_id
from app.gateway.auth import get_gateway_local_token
from app.schemas.internal_v2.node_debug import NodeDebugBindingSnapshotDTO
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection


class NodeDebugPluginRuntime:
    """持有 Gateway 对精确 plugin instance 的路由与运行期消息复用。"""

    def __init__(
        self,
        socket: ClientConnection,
        binding: NodeDebugBindingSnapshotDTO,
        run_id: str,
        request_id: str,
    ) -> None:
        self._socket = socket
        self.binding = binding
        self.run_id = run_id
        self._request_id = request_id
        self._responses: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._messages: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._events: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._request_lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._read())
        self.exit_code: int | None = None
        self.pid: int | None = None
        self.process_instance_id: str | None = None

    @classmethod
    async def connect(
        cls,
        *,
        binding: NodeDebugBindingSnapshotDTO,
        workspace_id: str,
        connection: GatewayTransportConnection,
        request_id: str,
        run_id: str,
    ) -> NodeDebugPluginRuntime:
        base = connection.resolve_gateway_url()
        scheme = "wss" if base.startswith("https://") else "ws"
        authority = base.removeprefix("https://").removeprefix("http://")
        path = (
            f"/api/gateway/service-plugins/{binding.package_id}/{binding.plugin_id}"
            f"/instances/{binding.instance_id}/debug-runtime"
        )
        query = urlencode({
            "package_version": binding.package_version,
            "workspace_id": workspace_id,
        })
        socket = await connect(
            f"{scheme}://{authority}{path}?{query}",
            additional_headers={
                "X-Local-Token": get_gateway_local_token(),
                "X-BoxTeam-Workspace-Id": workspace_id,
                "X-BoxTeam-Target-Gateway-Id": binding.gateway_id,
                "X-BoxTeam-Gateway-Id": binding.gateway_id,
                "X-Request-ID": request_id,
            },
            compression=None,
            max_queue=1,
            open_timeout=connection.resolve_timeout_seconds(),
            close_timeout=5,
        )
        try:
            ready = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        except (TimeoutError, ValueError) as error:
            await socket.close()
            raise RuntimeError("Debug 插件通道没有返回 ready 身份") from error
        expected = {
            "version": 1,
            "type": "ready",
            "gateway_id": binding.gateway_id,
            "workspace_id": binding.workspace_id,
            "package_id": binding.package_id,
            "plugin_id": binding.plugin_id,
            "package_version": binding.package_version,
            "package_digest": binding.package_digest,
            "instance_id": binding.instance_id,
            "service_id": binding.service_id,
            "contract_version": binding.contract_version,
        }
        if not isinstance(ready, dict) or any(ready.get(key) != value for key, value in expected.items()):
            await socket.close()
            raise RuntimeError("Debug 插件 ready 身份与启动时冻结的绑定不匹配")
        debugger = next((item for item in ready.get("debuggers", [])
                         if isinstance(item, dict) and item.get("type") == binding.type
                         and item.get("service_id") == binding.service_id), None)
        if debugger is None:
            await socket.close()
            raise RuntimeError("Debug 插件 ready 声明中没有冻结的调试器")
        return cls(socket, binding, run_id, request_id)

    async def _read(self) -> None:
        try:
            async for raw in self._socket:
                frame = json.loads(raw)
                if not isinstance(frame, dict) or frame.get("version") != 1:
                    raise TypeError("Debug 插件返回无效控制帧")
                if frame.get("run_id") != self.run_id:
                    raise RuntimeError("Debug 插件帧所属 run_id 不匹配")
                if frame.get("type") == "response":
                    operation_id = frame.get("operation_id")
                    future = self._responses.get(operation_id) if isinstance(operation_id, str) else None
                    if future is not None and not future.done():
                        future.set_result(frame)
                    continue
                if frame.get("type") != "event":
                    raise TypeError("Debug 插件返回未知控制帧类型")
                await self._events.put(frame)
                if frame.get("event") == "inspector.message":
                    payload = frame.get("payload")
                    if not isinstance(payload, dict):
                        raise TypeError("Debug 插件 Inspector 事件必须是对象")
                    await self._messages.put(payload)
                elif frame.get("event") == "process.exited":
                    payload = frame.get("payload")
                    if isinstance(payload, dict):
                        code = payload.get("exit_code")
                        self.exit_code = code if type(code) is int else None
                elif frame.get("event") in {"stdout", "stderr"}:
                    await self._messages.put({"method": "BoxTeam.output", "params": frame})
        except ConnectionClosed:
            pass
        except (TypeError, ValueError, RuntimeError, OSError, WebSocketException) as error:
            for future in tuple(self._responses.values()):
                if not future.done():
                    future.set_exception(error)
        finally:
            for future in tuple(self._responses.values()):
                if not future.done():
                    future.set_exception(ConnectionError("Debug 插件控制通道已断开，操作结果可能未知"))
            await self._messages.put(None)
            await self._events.put(None)

    async def request(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        operation_id = create_prefixed_id("op")
        request_id = get_current_request_id() or self._request_id
        async with self._request_lock:
            future = asyncio.get_running_loop().create_future()
            self._responses[operation_id] = future
            try:
                await self._socket.send(json.dumps({
                    "version": 1,
                    "type": "request",
                    "request_id": request_id,
                    "operation_id": operation_id,
                    "run_id": self.run_id,
                    "operation": operation,
                    "payload": payload,
                }, separators=(",", ":")))
                response = await asyncio.wait_for(future, timeout=30)
            finally:
                self._responses.pop(operation_id, None)
        if response.get("request_id") != request_id or response.get("run_id") != self.run_id:
            raise RuntimeError("Debug 插件响应关联身份不匹配")
        if response.get("ok") is not True:
            raise RuntimeError(str(response.get("error") or "Debug 插件操作失败"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise TypeError("Debug 插件操作结果必须是对象")
        return result

    async def launch(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            result = await self.request("launch", payload)
        except (ConnectionError, TimeoutError) as launch_error:
            try:
                result = await self.status()
            except Exception as status_error:
                raise RuntimeError(
                    "Debug 插件 launch 结果未知，状态查询也失败；没有重放启动操作"
                ) from status_error
            if (result.get("run_id") != self.run_id
                    or result.get("process_instance_id") != payload.get("process_instance_id")
                    or result.get("status") not in {"running", "exited"}):
                raise RuntimeError(
                    "Debug 插件 launch 结果未知，状态未证明原操作已提交；没有重放启动操作"
                ) from launch_error
        pid = result.get("pid")
        process_instance_id = result.get("process_instance_id")
        if type(pid) is not int or pid <= 0 or not isinstance(process_instance_id, str):
            raise TypeError("Debug 插件 launch 回执缺少目标 PID 或 process_instance_id")
        self.pid = pid
        self.process_instance_id = process_instance_id
        return result

    async def stop(self) -> dict[str, object]:
        result = await self.request("stop", {})
        value = result.get("exit_code")
        self.exit_code = value if type(value) is int else None
        return result

    async def status(self) -> dict[str, object]:
        return await self.request("status", {})

    async def send(self, raw: str) -> None:
        command = json.loads(raw)
        if not isinstance(command, dict) or type(command.get("id")) is not int:
            raise ValueError("Node Inspector 请求缺少整数 command id")
        await self.request("inspector.send", {
            "command_id": command["id"],
            "method": command.get("method"),
            "params": command.get("params", {}),
        })

    async def messages(self) -> AsyncIterator[dict[str, object]]:
        while (message := await self._messages.get()) is not None:
            yield message

    async def events(self) -> AsyncIterator[dict[str, object]]:
        while (event := await self._events.get()) is not None:
            yield event

    async def close(self) -> None:
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        await self._socket.close()
