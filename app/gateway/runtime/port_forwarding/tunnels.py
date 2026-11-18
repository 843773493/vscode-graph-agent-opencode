from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.shielded_cleanup import run_sync_shielded
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.gateway.runtime.process_ports import allocate_ssh_tunnel_port
from app.gateway.runtime.ssh_process import (
    SshPortForwardSpec,
    start_ssh_port_forward_process,
)
from app.schemas.gateway.port_forwarding import CreatePortForwardRequest

from .hosts import SshHost

logger = logging.getLogger(__name__)
ProcessStarter = Callable[..., tuple[ManagedProcess, Path, int]]


def log_tail(path: Path, *, start: int = 0) -> str:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(start, stream.tell() - 16384))
        return stream.read(16384).decode("utf-8", errors="replace")


@dataclass(slots=True)
class Tunnel:
    processes: list[tuple[ManagedProcess, Path]] = field(default_factory=list)
    server: asyncio.Server | None = None
    clients: set[asyncio.Task[None]] = field(default_factory=set)
    monitor: asyncio.Task[None] | None = None

    async def close(self) -> None:
        if self.monitor is not None and self.monitor is not asyncio.current_task():
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in self.clients:
            task.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
        # 保留关闭失败的句柄，让用户可以重试，不能假装已停止。
        errors: list[Exception] = []
        for process, path in list(reversed(self.processes)):
            try:
                await run_sync_shielded(process.close)
            except Exception as error:  # noqa: BLE001 - 全部句柄都要回收，结算后合并抛出原始错误。
                errors.append(error)
            else:
                self.processes.remove((process, path))
        if errors:
            raise ExceptionGroup("端口转发进程回收失败", errors)

    async def watch(self, on_exit: Callable[[Exception], Awaitable[None]]) -> None:
        while True:
            for process, path in self.processes:
                code = process.process.poll()
                if code is not None:
                    logger.error("端口转发 SSH 退出: code=%s log=%s tail=%s", code, path, log_tail(path))
                    await on_exit(RuntimeError(f"SSH 进程退出，returncode={code}"))
                    return
            await asyncio.sleep(1)


async def start_tunnel(
    tunnel: Tunnel, rule: CreatePortForwardRequest, *,
    listener_host: SshHost | None, destination_host: SshHost | None,
    log_dir: Path, forward_id: str,
    process_starter: ProcessStarter = start_ssh_port_forward_process,
) -> None:
    async def ssh(host: SshHost, spec: SshPortForwardSpec, suffix: str) -> None:
        log_start = 0
        def spawn_owned() -> None:
            nonlocal log_start
            process, path, log_start = process_starter(
                host=host.host, port=host.port, username=host.username,
                private_key_path=host.private_key_path, ssh_config_host=host.ssh_config_host,
                forward=spec, log_dir=log_dir, forward_id=f"{forward_id}-{suffix}",
            )
            # 线程结算前登记句柄，取消随后沿同一个 Tunnel 回收，避免遗留孤儿进程。
            tunnel.processes.append((process, path))

        await run_sync_shielded(spawn_owned)
        process, path = tunnel.processes[-1]
        # -R 必须等服务端的监听确认；不能用 Gateway 本地连通性冒充远端就绪。
        marker = "remote forward success for:" if spec.direction == "R" else "Local forwarding listening on"
        async with asyncio.timeout(10):
            while True:
                code = process.process.poll()
                if code is not None:
                    raise RuntimeError(f"SSH 启动失败，returncode={code}，日志: {log_tail(path)[-4000:]}")
                if marker in log_tail(path, start=log_start):
                    return
                await asyncio.sleep(0.05)

    listener, destination = rule.listener, rule.destination
    if listener_host is None and destination_host is None:
        async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            assert task is not None
            tunnel.clients.add(task)
            remote_writer: asyncio.StreamWriter | None = None
            try:
                async with asyncio.timeout(10):
                    remote_reader, remote_writer = await asyncio.open_connection(destination.address, destination.port)

                async def pump(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
                    while data := await source.read(65536):
                        target.write(data)
                        await target.drain()
                    if target.can_write_eof():
                        target.write_eof()

                async with asyncio.TaskGroup() as group:
                    group.create_task(pump(reader, remote_writer))
                    group.create_task(pump(remote_reader, writer))
            except (OSError, TimeoutError, ExceptionGroup):
                logger.exception("端口转发连接失败: forward_id=%s", forward_id)
            finally:
                writer.close()
                if remote_writer is not None:
                    remote_writer.close()
                tunnel.clients.discard(task)

        tunnel.server = await asyncio.start_server(relay, listener.address, listener.port)
    elif listener_host is None:
        assert destination_host is not None
        await ssh(destination_host, SshPortForwardSpec(
            "L", listener.address, listener.port, destination.address, destination.port,
        ), "listen")
    else:
        destination_address, destination_port = destination.address, destination.port
        if destination_host is not None:
            # 两个 SSH Host 通过 Gateway 上仅监听 loopback 的中继相连。
            destination_port = allocate_ssh_tunnel_port()
            await ssh(destination_host, SshPortForwardSpec(
                "L", "127.0.0.1", destination_port, destination.address, destination.port,
            ), "destination")
            destination_address = "127.0.0.1"
        await ssh(listener_host, SshPortForwardSpec(
            "R", listener.address, listener.port, destination_address, destination_port,
        ), "listen")
