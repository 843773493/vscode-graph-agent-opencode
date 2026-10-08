"""复用 registry 隧道和凭据的唯一长期 channel 生命周期。"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.channel import FederationChannelSession
from app.gateway.federation.dialer import HubDialRequest, dial_spoke_channel
from app.gateway.federation.ports import FederationSpoke
from app.gateway.federation.rpc import FederationRpcService
from app.gateway.registry import GatewayWorkspaceRegistry

logger = logging.getLogger(__name__)


def registered_spokes(registry: GatewayWorkspaceRegistry) -> tuple[FederationSpoke, ...]:
    spokes = tuple(FederationSpoke(item.remote_gateway_id, item.connection_id) for item in registry.remote_gateway_connections())
    if len({item.gateway_id for item in spokes}) != len(spokes):
        raise RuntimeError("同一稳定 Gateway 不能登记多个 spoke connection")
    return spokes


class FederationChannelOwner:
    def __init__(
        self, *, registry: GatewayWorkspaceRegistry, credentials: FederationCredentialStore,
        gateway_root: Path, service: FederationRpcService, reconnect_seconds: float,
    ) -> None:
        if reconnect_seconds <= 0:
            raise ValueError("channel reconnect_seconds 必须为正数")
        self._registry = registry
        self._credentials = credentials
        self._root = gateway_root
        self._service = service
        self._reconnect_seconds = reconnect_seconds
        self._requests: dict[str, HubDialRequest] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sessions: dict[str, FederationChannelSession] = {}
        self._changed = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    def registry_committed(self) -> None:
        self._changed.set()

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("FederationChannelOwner 不允许重复启动")
        self._registry.add_commit_observer(self.registry_committed)
        await self._reconcile()
        self._runner = asyncio.create_task(self._run(), name="federation-channel-owner")
        self._runner.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._failure = error
            logger.error("联邦 channel owner 失败", exc_info=(type(error), error, error.__traceback__))

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=self._reconnect_seconds)
            except TimeoutError:
                pass
            self._changed.clear()
            self.assert_healthy()
            await self._reconcile()

    async def _reconcile(self) -> None:
        desired: dict[str, HubDialRequest] = {}
        for connection in self._registry.remote_gateway_connections():
            try:
                url = self._registry.remote_gateway_url(connection.connection_id)
            except LookupError as error:
                # registry 恢复隧道后会再次提交；未连接状态必须可诊断，不伪造 channel。
                logger.warning("联邦 channel 等待现有 SSH 隧道恢复: %s", error)
                continue
            try:
                credential = self._credentials.get(connection.connection_id)
            except (LookupError, PermissionError) as error:
                logger.warning("联邦 channel 凭据已撤销，关闭原连接: %s", error)
                continue
            if credential.peer_gateway_id != self._service.gateway_id:
                raise PermissionError("outbound federation credential 不属于当前 hub")
            desired[connection.connection_id] = HubDialRequest(url, credential.token, connection.remote_gateway_id, connection.connection_id)
        for connection_id in tuple(self._tasks):
            if not self._tasks[connection_id].done() and self._requests[connection_id] == desired.get(connection_id):
                continue
            task = self._tasks.pop(connection_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._requests.pop(connection_id)
        for connection_id, request in desired.items():
            if connection_id in self._tasks:
                continue
            self._requests[connection_id] = request
            task = asyncio.create_task(self._keep_channel(request), name="federation-spoke-channel")
            task.add_done_callback(self._finished)
            self._tasks[connection_id] = task

    async def _keep_channel(self, request: HubDialRequest) -> None:
        epoch = max((item.channel_epoch for item in self._service.control_store.active_channels()), default=0)
        while True:
            session = None
            try:
                # 连接 locator 与 epoch 不进入任何业务 identity。
                epoch = max(epoch + 1, time.time_ns())
                session = await dial_spoke_channel(
                    request=request, local_gateway_id=self._service.gateway_id, gateway_root=self._root,
                    handler=self._service.request_handler(), channel_epoch=epoch,
                )
                await self._service.adopt_outbound_channel(session)
                self._sessions[request.connection_id] = session
                reason = await session.wait_closed()
                logger.warning("联邦 spoke channel 断开: peer=%s, reason=%s", request.expected_peer_gateway_id, reason)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("联邦 spoke channel 无法连接: peer=%s", request.expected_peer_gateway_id)
            finally:
                if session is not None:
                    self._sessions.pop(request.connection_id, None)
                    active_error = sys.exception()
                    cancellation_error = (
                        active_error
                        if isinstance(active_error, asyncio.CancelledError)
                        else None
                    )
                    cleanup_error: BaseException | None = None
                    try:
                        self._service.unregister_channel(session.channel_instance_id)
                    except BaseException as error:  # noqa: BLE001 - 后续仍须关闭并 join 已取得的 session
                        if isinstance(error, asyncio.CancelledError):
                            cancellation_error = cancellation_error or error
                        else:
                            cleanup_error = error
                    for action, cleanup in (
                        ("关闭联邦 channel", session.close),
                        ("等待联邦 channel 任务结束", session.join),
                    ):
                        try:
                            await cleanup()
                        except BaseException as error:  # noqa: BLE001 - 一项清理失败后仍执行后续清理
                            if isinstance(error, asyncio.CancelledError):
                                cancellation_error = cancellation_error or error
                            elif cleanup_error is None:
                                cleanup_error = error
                            else:
                                cleanup_error.add_note(
                                    f"{action}也失败：{type(error).__name__}: {error}"
                                )
                    if cleanup_error is not None:
                        self._failure = cleanup_error
                        if active_error is not None:
                            active_error.add_note(
                                "清理已取得的联邦 channel 时失败："
                                f"{type(cleanup_error).__name__}: {cleanup_error}"
                            )
                        elif cancellation_error is not None:
                            cancellation_error.add_note(
                                "取消时联邦 channel 清理也失败："
                                f"{type(cleanup_error).__name__}: {cleanup_error}"
                            )
                            raise cancellation_error
                        else:
                            raise cleanup_error
                    elif active_error is None and cancellation_error is not None:
                        raise cancellation_error
            await asyncio.sleep(self._reconnect_seconds)

    def assert_healthy(self) -> None:
        if self._failure is not None:
            raise RuntimeError(f"联邦 channel owner 已失败: {self._failure}") from self._failure

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        if self._runner is not None:
            tasks.append(self._runner)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._requests.clear()
        self._runner = None
        self._registry.remove_commit_observer(self.registry_committed)
        await self._service.close_channels()
        self.assert_healthy()
