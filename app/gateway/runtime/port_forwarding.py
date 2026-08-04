from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from app.gateway.control.storage import atomic_write_json, read_json_object
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.process import (
    ManagedProcess,
    SshLocalForwardSpec,
    allocate_ssh_tunnel_port,
    is_local_port_available,
    start_workspace_ssh_port_forward_process,
)
from app.gateway.schemas import (
    CreatePortForwardRequest,
    PortForwardDTO,
    PortForwardProtocol,
    PortForwardStatus,
)

_SCHEMA_VERSION = 1
_LOCAL_HOST = "127.0.0.1"
_REMOTE_HOST = "127.0.0.1"
_READY_TIMEOUT_SECONDS = 10.0
_LOG_TAIL_LIMIT = 4000

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PortForwardDefinition:
    forward_id: str
    workspace_id: str
    connection_id: str
    remote_host: str
    remote_port: int
    local_host: str
    local_port: int
    protocol: PortForwardProtocol
    label: str | None
    desired_running: bool


@dataclass(slots=True)
class _PortForwardRuntime:
    process: ManagedProcess | None = None
    log_path: Path | None = None
    status: PortForwardStatus = "stopped"
    error: str | None = None


PortForwardProcessStarter = Callable[..., tuple[ManagedProcess, Path]]


class SshPortForwardManager:
    def __init__(
        self,
        *,
        registry: GatewayWorkspaceRegistry,
        storage_path: Path,
        log_dir: Path,
        process_starter: PortForwardProcessStarter = (
            start_workspace_ssh_port_forward_process
        ),
        port_allocator: Callable[[], int] = allocate_ssh_tunnel_port,
    ) -> None:
        self._registry = registry
        self._storage_path = storage_path
        self._log_dir = log_dir
        self._process_starter = process_starter
        self._port_allocator = port_allocator
        self._definitions: dict[str, _PortForwardDefinition] = {}
        self._runtimes: dict[str, _PortForwardRuntime] = {}
        self._lock = asyncio.Lock()
        self._load()

    async def restore(self) -> None:
        async with self._lock:
            for definition in self._definitions.values():
                if not definition.desired_running:
                    continue
                try:
                    connection = self._connection_for_definition(definition)
                    await self._start_definition(definition, connection)
                except Exception as error:  # noqa: BLE001
                    self._runtimes[definition.forward_id] = _PortForwardRuntime(
                        status="error",
                        error=(
                            "Gateway 启动时恢复 SSH 端口转发失败: "
                            f"{type(error).__name__}: {error}"
                        ),
                    )
                    logger.exception(
                        "恢复 SSH 端口转发失败: forward_id=%s workspace_id=%s",
                        definition.forward_id,
                        definition.workspace_id,
                    )

    async def list(self, workspace_id: str) -> list[PortForwardDTO]:
        async with self._lock:
            self._resolve_remote_workspace(workspace_id)
            return [
                self._dto(definition)
                for definition in self._definitions.values()
                if definition.workspace_id == workspace_id
            ]

    async def create(
        self,
        workspace_id: str,
        payload: CreatePortForwardRequest,
    ) -> PortForwardDTO:
        async with self._lock:
            connection = self._resolve_remote_workspace(workspace_id)
            self._assert_remote_port_available(
                connection_id=connection.connection_id,
                remote_port=payload.remote_port,
            )
            local_port = payload.local_port
            if local_port is None:
                local_port = self._allocate_unclaimed_local_port()
            self._assert_local_port_available(local_port)
            forward_id = f"pf_{uuid4().hex}"
            definition = _PortForwardDefinition(
                forward_id=forward_id,
                workspace_id=workspace_id,
                connection_id=connection.connection_id,
                remote_host=_REMOTE_HOST,
                remote_port=payload.remote_port,
                local_host=_LOCAL_HOST,
                local_port=local_port,
                protocol=payload.protocol,
                label=payload.label or None,
                desired_running=True,
            )
            self._definitions[forward_id] = definition
            self._save()
            try:
                await self._start_definition(definition, connection)
            except Exception as error:
                self._runtimes[forward_id] = _PortForwardRuntime(
                    status="error",
                    error=f"{type(error).__name__}: {error}",
                )
                logger.exception(
                    "创建 SSH 端口转发失败: forward_id=%s workspace_id=%s",
                    forward_id,
                    workspace_id,
                )
            return self._dto(definition)

    async def delete(self, workspace_id: str, forward_id: str) -> None:
        async with self._lock:
            self._resolve_remote_workspace(workspace_id)
            definition = self._owned_definition(workspace_id, forward_id)
            await self._stop_runtime(definition.forward_id)
            del self._definitions[definition.forward_id]
            self._runtimes.pop(definition.forward_id, None)
            self._save()

    async def reconnect(self, workspace_id: str, forward_id: str) -> PortForwardDTO:
        async with self._lock:
            connection = self._resolve_remote_workspace(workspace_id)
            definition = self._owned_definition(workspace_id, forward_id)
            await self._stop_runtime(definition.forward_id)
            try:
                await self._start_definition(definition, connection)
            except Exception as error:
                self._runtimes[forward_id] = _PortForwardRuntime(
                    status="error",
                    error=f"{type(error).__name__}: {error}",
                )
                logger.exception(
                    "重连 SSH 端口转发失败: forward_id=%s workspace_id=%s",
                    forward_id,
                    workspace_id,
                )
            return self._dto(definition)

    async def remove_workspace(self, workspace_id: str) -> None:
        async with self._lock:
            definitions = [
                definition
                for definition in self._definitions.values()
                if definition.workspace_id == workspace_id
            ]
            for definition in definitions:
                await self._stop_runtime(definition.forward_id)
                del self._definitions[definition.forward_id]
                self._runtimes.pop(definition.forward_id, None)
            if definitions:
                self._save()

    async def reconcile_workspaces(self) -> None:
        """清理注册表中已不存在工作区所拥有的转发。"""
        async with self._lock:
            stale_definitions = [
                definition
                for definition in self._definitions.values()
                if not self._registry.has_target(definition.workspace_id)
            ]
            errors: list[str] = []
            removed_any = False
            for definition in stale_definitions:
                try:
                    await self._stop_runtime(definition.forward_id)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{definition.forward_id}: {error}")
                    continue
                del self._definitions[definition.forward_id]
                self._runtimes.pop(definition.forward_id, None)
                removed_any = True
            if removed_any:
                self._save()
            if errors:
                raise RuntimeError(
                    "清理已移除工作区的 SSH 端口转发失败: " + "; ".join(errors)
                )

    async def close(self) -> None:
        async with self._lock:
            errors: list[str] = []
            for forward_id in tuple(self._runtimes):
                try:
                    await self._stop_runtime(forward_id)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{forward_id}: {error}")
            if errors:
                raise RuntimeError(
                    "关闭 SSH 端口转发进程失败: " + "; ".join(errors)
                )

    def _resolve_remote_workspace(
        self,
        workspace_id: str,
    ) -> RemoteGatewayConnection:
        target = self._registry.resolve(workspace_id)
        if target.connection_kind != "remote_gateway":
            raise ValueError(
                "SSH 端口转发只支持远程 Gateway 投影工作区: "
                f"workspace_id={workspace_id}"
            )
        if target.remote_gateway_connection_id is None:
            raise RuntimeError(
                f"远程投影工作区缺少 Gateway 连接 ID: {workspace_id}"
            )
        return self._registry.remote_gateway_connection(
            target.remote_gateway_connection_id
        )

    def _connection_for_definition(
        self,
        definition: _PortForwardDefinition,
    ) -> RemoteGatewayConnection:
        connection = self._resolve_remote_workspace(definition.workspace_id)
        if connection.connection_id != definition.connection_id:
            raise RuntimeError(
                "端口转发绑定的远程 Gateway 已变化: "
                f"stored={definition.connection_id}, "
                f"current={connection.connection_id}"
            )
        return connection

    def _owned_definition(
        self,
        workspace_id: str,
        forward_id: str,
    ) -> _PortForwardDefinition:
        definition = self._definitions.get(forward_id)
        if definition is None or definition.workspace_id != workspace_id:
            raise LookupError(
                "工作区端口转发不存在: "
                f"workspace_id={workspace_id}, forward_id={forward_id}"
            )
        return definition

    def _assert_remote_port_available(
        self,
        *,
        connection_id: str,
        remote_port: int,
    ) -> None:
        existing = next(
            (
                definition
                for definition in self._definitions.values()
                if definition.connection_id == connection_id
                and definition.remote_host == _REMOTE_HOST
                and definition.remote_port == remote_port
            ),
            None,
        )
        if existing is not None:
            raise ValueError(
                f"远端端口 {_REMOTE_HOST}:{remote_port} 已归属于工作区 "
                f"{existing.workspace_id}: forward_id={existing.forward_id}"
            )

    def _assert_local_port_available(self, local_port: int) -> None:
        existing = next(
            (
                definition
                for definition in self._definitions.values()
                if definition.local_port == local_port
            ),
            None,
        )
        if existing is not None:
            raise ValueError(
                f"本地端口 {_LOCAL_HOST}:{local_port} 已被转发 "
                f"{existing.forward_id} 占用"
            )
        if not is_local_port_available(local_port):
            raise ValueError(f"本地端口 {_LOCAL_HOST}:{local_port} 已被其他进程占用")

    def _allocate_unclaimed_local_port(self) -> int:
        claimed_ports = {
            definition.local_port for definition in self._definitions.values()
        }
        for _ in range(1000):
            port = self._port_allocator()
            if port not in claimed_ports and is_local_port_available(port):
                return port
        raise RuntimeError("无法分配未被端口转发占用的本地 SSH 隧道端口")

    async def _start_definition(
        self,
        definition: _PortForwardDefinition,
        connection: RemoteGatewayConnection,
    ) -> None:
        runtime = _PortForwardRuntime(status="starting")
        self._runtimes[definition.forward_id] = runtime
        process, log_path = self._process_starter(
            host=connection.host,
            port=connection.port,
            username=connection.username,
            private_key_path=(
                Path(connection.private_key_path)
                if connection.private_key_path is not None
                else None
            ),
            ssh_config_host=connection.ssh_config_host,
            forward=SshLocalForwardSpec(
                local_port=definition.local_port,
                remote_host=definition.remote_host,
                remote_port=definition.remote_port,
            ),
            log_dir=self._log_dir,
            forward_id=definition.forward_id,
        )
        runtime.process = process
        runtime.log_path = log_path
        try:
            await self._wait_until_listening(process, definition.local_port)
        except Exception as error:
            await asyncio.to_thread(process.close)
            log_detail = self._read_log_tail(log_path)
            raise RuntimeError(
                f"{error}" + (f"；SSH 日志: {log_detail}" if log_detail else "")
            ) from error
        runtime.status = "active"
        runtime.error = None

    async def _wait_until_listening(
        self,
        process: ManagedProcess,
        local_port: int,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + _READY_TIMEOUT_SECONDS
        last_error: OSError | None = None
        while asyncio.get_running_loop().time() < deadline:
            return_code = process.process.poll()
            if return_code is not None:
                raise RuntimeError(
                    "SSH 端口转发进程在监听建立前退出: "
                    f"returncode={return_code}"
                )
            try:
                reader, writer = await asyncio.open_connection(_LOCAL_HOST, local_port)
                del reader
                writer.close()
                await writer.wait_closed()
                return
            except OSError as error:
                last_error = error
            await asyncio.sleep(0.05)
        detail = f"，最后错误: {last_error}" if last_error is not None else ""
        raise TimeoutError(
            f"SSH 端口转发在 {_READY_TIMEOUT_SECONDS:g} 秒内未监听 "
            f"{_LOCAL_HOST}:{local_port}{detail}"
        )

    async def _stop_runtime(self, forward_id: str) -> None:
        runtime = self._runtimes.get(forward_id)
        if runtime is None or runtime.process is None:
            return
        process = runtime.process
        runtime.process = None
        try:
            await asyncio.to_thread(process.close)
        except Exception:
            runtime.process = process
            raise
        runtime.status = "stopped"
        runtime.error = None

    def _dto(self, definition: _PortForwardDefinition) -> PortForwardDTO:
        runtime = self._runtimes.get(definition.forward_id)
        if runtime is None:
            status: PortForwardStatus = "stopped"
            error = None
        else:
            self._refresh_runtime(definition.forward_id, runtime)
            status = runtime.status
            error = runtime.error
        local_url = (
            f"{definition.protocol}://{definition.local_host}:{definition.local_port}"
            if definition.protocol in {"http", "https"}
            else None
        )
        return PortForwardDTO(
            forward_id=definition.forward_id,
            workspace_id=definition.workspace_id,
            connection_id=definition.connection_id,
            remote_host=_REMOTE_HOST,
            remote_port=definition.remote_port,
            local_host=_LOCAL_HOST,
            local_port=definition.local_port,
            protocol=definition.protocol,
            label=definition.label,
            status=status,
            error=error,
            local_url=local_url,
        )

    def _refresh_runtime(
        self,
        forward_id: str,
        runtime: _PortForwardRuntime,
    ) -> None:
        if runtime.process is None or runtime.status not in {"starting", "active"}:
            return
        return_code = runtime.process.process.poll()
        if return_code is None:
            return
        runtime.process.close(timeout_seconds=0.1)
        runtime.process = None
        runtime.status = "error"
        log_detail = self._read_log_tail(runtime.log_path)
        runtime.error = (
            f"SSH 端口转发进程已退出: forward_id={forward_id}, "
            f"returncode={return_code}"
            + (f"；日志: {log_detail}" if log_detail else "")
        )

    @staticmethod
    def _read_log_tail(log_path: Path | None) -> str:
        if log_path is None or not log_path.exists():
            return ""
        content = log_path.read_text(encoding="utf-8", errors="replace")
        return content[-_LOG_TAIL_LIMIT:].strip()

    def _load(self) -> None:
        payload = read_json_object(
            self._storage_path,
            default={"schema_version": _SCHEMA_VERSION, "items": []},
        )
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(
                "Gateway 端口转发持久化版本不受支持: "
                f"{payload.get('schema_version')!r}"
            )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise TypeError("Gateway 端口转发持久化 items 必须是数组")
        claimed_local_ports: set[int] = set()
        claimed_remote_ports: set[tuple[str, str, int]] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise TypeError("Gateway 端口转发持久化条目必须是对象")
            definition = _PortForwardDefinition(
                forward_id=str(raw_item["forward_id"]),
                workspace_id=str(raw_item["workspace_id"]),
                connection_id=str(raw_item["connection_id"]),
                remote_host=str(raw_item["remote_host"]),
                remote_port=int(raw_item["remote_port"]),
                local_host=str(raw_item["local_host"]),
                local_port=int(raw_item["local_port"]),
                protocol=self._parse_protocol(raw_item["protocol"]),
                label=(
                    str(raw_item["label"])
                    if raw_item.get("label") is not None
                    else None
                ),
                desired_running=self._parse_desired_running(
                    raw_item.get("desired_running")
                ),
            )
            self._validate_definition(definition)
            if definition.forward_id in self._definitions:
                raise ValueError(
                    f"Gateway 端口转发 ID 重复: {definition.forward_id}"
                )
            if definition.local_port in claimed_local_ports:
                raise ValueError(
                    f"Gateway 端口转发本地端口重复: {definition.local_port}"
                )
            remote_key = (
                definition.connection_id,
                definition.remote_host,
                definition.remote_port,
            )
            if remote_key in claimed_remote_ports:
                raise ValueError(
                    "Gateway 端口转发远端端口重复: "
                    f"connection_id={definition.connection_id}, "
                    f"remote_port={definition.remote_port}"
                )
            self._definitions[definition.forward_id] = definition
            claimed_local_ports.add(definition.local_port)
            claimed_remote_ports.add(remote_key)

    @staticmethod
    def _parse_protocol(value: object) -> PortForwardProtocol:
        if value not in {"http", "https", "tcp"}:
            raise ValueError(f"Gateway 端口转发 protocol 非法: {value!r}")
        return value

    @staticmethod
    def _parse_desired_running(value: object) -> bool:
        if not isinstance(value, bool):
            raise TypeError("Gateway 端口转发 desired_running 必须是布尔值")
        return value

    @staticmethod
    def _validate_definition(definition: _PortForwardDefinition) -> None:
        if definition.remote_host != _REMOTE_HOST:
            raise ValueError(
                f"Gateway 端口转发远端地址必须是 {_REMOTE_HOST}"
            )
        if definition.local_host != _LOCAL_HOST:
            raise ValueError(
                f"Gateway 端口转发本地地址必须是 {_LOCAL_HOST}"
            )
        for name, port in (
            ("remote_port", definition.remote_port),
            ("local_port", definition.local_port),
        ):
            if port < 1 or port > 65535:
                raise ValueError(f"Gateway 端口转发 {name} 非法: {port}")

    def _save(self) -> None:
        atomic_write_json(
            self._storage_path,
            {
                "schema_version": _SCHEMA_VERSION,
                "items": [
                    asdict(definition)
                    for definition in self._definitions.values()
                ],
            },
        )
