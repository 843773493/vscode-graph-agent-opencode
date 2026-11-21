from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.gateway.control.storage import atomic_write_json, read_json_object
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.consumer_protocol import GatewayRuntimeHealthProof
from app.gateway.runtime.ssh_process import start_ssh_port_forward_process
from app.schemas.gateway.port_forwarding import (
    CreatePortForwardRequest,
    PortForwardDTO,
    PortForwardStatus,
)

from .hosts import resolve_host
from .tunnels import ProcessStarter, Tunnel, start_tunnel

logger = logging.getLogger(__name__)
_SCHEMA_VERSION = 3


def port_forward_failure(error: Exception, *, stage: str) -> str:
    details = [f"stage={stage}", f"error_type={type(error).__name__}"]
    for name in ("errno", "winerror"):
        value = getattr(error, name, None)
        if isinstance(value, int):
            details.append(f"{name}={value}")
    return f"端口转发失败: {', '.join(details)}；请查看 Gateway 原始诊断日志"


class Definition(CreatePortForwardRequest):
    forward_id: str


@dataclass(slots=True)
class Runtime:
    tunnel: Tunnel
    status: PortForwardStatus = "stopped"
    error: str | None = None


class PortForwardManager:
    def __init__(self, *, registry: GatewayWorkspaceRegistry, storage_path: Path,
                 log_dir: Path, process_starter: ProcessStarter = start_ssh_port_forward_process) -> None:
        self._registry = registry
        self._storage_path = storage_path
        self._log_dir = log_dir
        self._process_starter = process_starter
        self._definitions: dict[str, Definition] = {}
        self._runtimes: dict[str, Runtime] = {}
        self._runtime_generation: str | None = None
        self._lock = asyncio.Lock()
        payload = read_json_object(storage_path, default={"schema_version": _SCHEMA_VERSION, "items": []})
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("端口转发规则版本不匹配，请显式转换旧工作区规则；原文件未修改")
        for item in payload["items"]:
            definition = Definition.model_validate(item)
            if definition.forward_id in self._definitions:
                raise ValueError(f"转发规则 ID 重复: {definition.forward_id}")
            self._assert_listener_available(definition)
            self._definitions[definition.forward_id] = definition

    async def restore(self) -> None:
        async with self._lock:
            semaphore = asyncio.Semaphore(8)
            async def restore_one(definition: Definition) -> None:
                async with semaphore:
                    await self._start(definition)
            await asyncio.gather(*(restore_one(item) for item in self._definitions.values() if item.enabled))

    def assert_healthy(self) -> None:
        """确认恢复已结算；单条规则失败由面板展示，不阻断 Gateway 管理入口。"""

        errors: list[str] = []
        for definition in self._definitions.values():
            if not definition.enabled:
                continue
            runtime = self._runtimes.get(definition.forward_id)
            if runtime is None:
                errors.append(f"{definition.forward_id}: 运行时未恢复")
                continue
            if runtime.status == "starting":
                errors.append(
                    f"{definition.forward_id}: {runtime.error or runtime.status}"
                )
        if errors:
            raise RuntimeError("Gateway SSH 端口转发健康检查失败: " + "; ".join(errors))

    def runtime_health_proof(
        self,
        *,
        generation: str,
        fencing_token_digest: str | None = None,
    ) -> GatewayRuntimeHealthProof:
        """为 SSH tunnel/proxy 消费者生成不含连接信息的健康证明。"""

        self.assert_healthy()
        if self._runtime_generation not in {None, generation}:
            raise RuntimeError(
                "SSH tunnel/proxy runtime generation 不匹配: "
                f"expected={generation}, actual={self._runtime_generation}"
            )
        active_count = sum(
            1
            for runtime in self._runtimes.values()
            if runtime.status == "active"
        )
        return GatewayRuntimeHealthProof(
            consumer_id="ssh-tunnel-proxy",
            generation=generation,
            state="healthy",
            details={
                "defined_forward_count": len(self._definitions),
                "active_forward_count": active_count,
                "runtime_generation": self._runtime_generation,
            },
            fencing_token_digest=fencing_token_digest,
        )

    def prepare_runtime_generation(self, generation: str) -> str | None:
        """校验 SSH tunnel/proxy 可转移到新的 Gateway generation。"""

        if not generation.strip():
            raise ValueError("SSH tunnel/proxy generation 不能为空")
        self.assert_healthy()
        return self._runtime_generation

    def apply_runtime_generation(self, generation: str) -> str | None:
        previous_generation = self.prepare_runtime_generation(generation)
        self._runtime_generation = generation
        return previous_generation

    def promote_runtime_generation(self, generation: str) -> None:
        if self._runtime_generation != generation:
            raise RuntimeError(
                "SSH tunnel/proxy generation promotion 不匹配: "
                f"expected={generation}, actual={self._runtime_generation}"
            )

    def rollback_runtime_generation(
        self,
        generation: str,
        previous_generation: str | None,
    ) -> None:
        if self._runtime_generation == previous_generation:
            return
        if self._runtime_generation != generation:
            raise RuntimeError(
                "SSH tunnel/proxy generation 回退发现当前 lease 已被替换: "
                f"expected={generation}, actual={self._runtime_generation}"
            )
        self._runtime_generation = previous_generation

    async def list(self) -> list[PortForwardDTO]:
        # 快照不等待 SSH 握手或其它规则启停；状态由同一事件循环写入。
        return [self._dto(item) for item in self._definitions.values()]

    async def create(self, payload: CreatePortForwardRequest) -> PortForwardDTO:
        async with self._lock:
            await self._validate_hosts(payload)
            self._assert_listener_available(payload)
            definition = Definition(**payload.model_dump(), forward_id=create_prefixed_id("pf"))
            self._definitions[definition.forward_id] = definition
            self._save()
            if definition.enabled:
                await self._start(definition)
            return self._dto(definition)

    async def update(self, forward_id: str, payload: CreatePortForwardRequest) -> PortForwardDTO:
        async with self._lock:
            previous = self._owned(forward_id)
            await self._validate_hosts(payload)
            self._assert_listener_available(payload, excluding=forward_id)
            await self._stop(forward_id)
            definition = Definition(**payload.model_dump(), forward_id=previous.forward_id)
            self._definitions[forward_id] = definition
            self._save()
            if definition.enabled:
                await self._start(definition)
            return self._dto(definition)

    async def set_running(self, forward_id: str, enabled: bool) -> PortForwardDTO:
        async with self._lock:
            definition = self._owned(forward_id)
            await self._stop(forward_id)
            definition = definition.model_copy(update={"enabled": enabled})
            self._definitions[forward_id] = definition
            self._save()
            if enabled:
                await self._start(definition)
            return self._dto(definition)

    async def delete(self, forward_id: str) -> None:
        async with self._lock:
            self._owned(forward_id)
            await self._stop(forward_id)
            del self._definitions[forward_id]
            self._runtimes.pop(forward_id, None)
            self._save()

    async def disconnect_host(self, host_id: str) -> None:
        """删除连接只回收使用它的隧道；保留规则供用户编辑。"""
        async with self._lock:
            for definition in self._definitions.values():
                if host_id in {definition.listener.host_id, definition.destination.host_id}:
                    await self._stop(definition.forward_id)
                    self._runtimes[definition.forward_id].status = "error"
                    self._runtimes[definition.forward_id].error = "Host 连接已移除，请编辑规则"

    async def close(self) -> None:
        async with self._lock:
            for forward_id in list(self._runtimes):
                await self._stop(forward_id)

    async def _validate_hosts(self, payload: CreatePortForwardRequest) -> None:
        for host_id in {payload.listener.host_id, payload.destination.host_id}:
            await asyncio.to_thread(resolve_host, self._registry, host_id)

    def _assert_listener_available(self, payload: CreatePortForwardRequest, *, excluding: str | None = None) -> None:
        for item in self._definitions.values():
            listener, other = payload.listener, item.listener
            if (item.forward_id != excluding and listener.host_id == other.host_id
                and listener.port == other.port
                and (listener.address == other.address or listener.address in {"0.0.0.0", "::"}
                     or other.address in {"0.0.0.0", "::"})):
                raise ValueError(f"监听端口已被规则 {item.forward_id} 使用")

    async def _start(self, definition: Definition) -> None:
        runtime = Runtime(Tunnel(), status="starting")
        self._runtimes[definition.forward_id] = runtime
        try:
            listener = await asyncio.to_thread(resolve_host, self._registry, definition.listener.host_id)
            destination = await asyncio.to_thread(resolve_host, self._registry, definition.destination.host_id)
            await start_tunnel(runtime.tunnel, definition, listener_host=listener, destination_host=destination,
                               log_dir=self._log_dir, forward_id=definition.forward_id,
                               process_starter=self._process_starter)
        except BaseException as error:
            await runtime.tunnel.close()
            if not isinstance(error, Exception):
                raise
            logger.exception("端口转发启动失败: forward_id=%s", definition.forward_id)
            runtime.status = "error"
            runtime.error = port_forward_failure(error, stage="start")
        else:
            runtime.status = "active"
            if runtime.tunnel.processes:
                runtime.tunnel.monitor = asyncio.create_task(runtime.tunnel.watch(
                    lambda error: self._process_exited(definition.forward_id, runtime, error),
                ))

    async def _process_exited(self, forward_id: str, runtime: Runtime, error: Exception) -> None:
        # watcher 不等待管理锁，防止显式停止正在持锁等待 watcher 时互相等待。
        runtime.status = "error"
        runtime.error = port_forward_failure(error, stage="process_exit")
        try:
            await runtime.tunnel.close()
        except Exception:
            logger.exception("回收已退出转发的其它隧道失败: forward_id=%s", forward_id)
            raise

    async def _stop(self, forward_id: str) -> None:
        runtime = self._runtimes.setdefault(forward_id, Runtime(Tunnel()))
        try:
            await runtime.tunnel.close()
        except Exception as error:
            runtime.status = "error"
            runtime.error = port_forward_failure(error, stage="stop")
            raise
        runtime.status = "stopped"
        runtime.error = None

    def _owned(self, forward_id: str) -> Definition:
        if forward_id not in self._definitions:
            raise LookupError(f"端口转发规则不存在: {forward_id}")
        return self._definitions[forward_id]

    def _dto(self, definition: Definition) -> PortForwardDTO:
        runtime = self._runtimes.get(definition.forward_id)
        return PortForwardDTO(**definition.model_dump(), gateway_id=self._registry.gateway_id,
                              status=runtime.status if runtime else "stopped",
                              error=runtime.error if runtime else None)

    def _save(self) -> None:
        atomic_write_json(self._storage_path, {
            "schema_version": _SCHEMA_VERSION,
            "items": [item.model_dump(mode="json") for item in self._definitions.values()],
        })

