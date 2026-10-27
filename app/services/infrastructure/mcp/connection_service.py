"""持久连接意图及私有授权等待；网络与目录仍由唯一 MCP Owner 持有。"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import parse_qs, urlparse

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.mcp import (
    McpAuthorizationDTO,
    McpConnectionOperationDTO,
    McpConnectionRequestDTO,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.oauth import McpOAuthProvider
from app.services.infrastructure.mcp.operation_store import McpConnectionOperationStore
from app.services.infrastructure.mcp.session_runtime import (
    production_mcp_session_factory,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

_OPERATION_TIMEOUT_SECONDS = 300


class McpConnectionSummary(TypedDict):
    connection_id: str
    server_id: str
    transport: Literal["stdio", "streamable_http"]
    enabled: bool
    status: Literal["disabled", "ready", "failed", "authorization_required"]


@dataclass
class _Authorization:
    url: str = field(repr=False)
    state: str = field(repr=False)
    callback: asyncio.Future[tuple[str, str | None]] = field(repr=False)


class McpConnectionService:
    def __init__(self, *, owner: McpCatalogOwner, store: WorkspaceStateStore, workspace_id: str) -> None:
        self._owner = owner
        self._store = McpConnectionOperationStore(store)
        self._workspace_root: Path = store.workspace_root
        self._workspace_id = workspace_id
        self._scope = LifetimeScope("mcp-connection-operations")
        self._tasks: dict[str, asyncio.Future[object]] = {}
        self._authorizations: dict[str, _Authorization] = {}
        self._changed = asyncio.Event()
        self._started = False

    def _configuration_ref(self, server_id: str) -> str:
        return sha256_jcs(asdict(self._owner.server_configuration(server_id)))

    def _connection_id(self, server_id: str) -> str:
        return f"mcp-server:v1:{self._workspace_id}:{server_id}"

    def list_connections(self, server_id: str | None = None) -> list[McpConnectionSummary]:
        servers = self._owner.list_servers()
        if server_id is not None:
            self._owner.server_configuration(server_id)
            servers = [server for server in servers if server.server_id == server_id]
        return [McpConnectionSummary(
            connection_id=self._connection_id(server.server_id), server_id=server.server_id,
            transport=server.transport, enabled=server.enabled, status=server.status,
        ) for server in servers]

    def operation_path(self, operation: McpConnectionOperationDTO) -> str:
        """真实受保护状态入口；不构造假授权链接或返回 SDK 的敏感地址。"""
        return f"/api/v1/workspaces/{self._workspace_id}/mcp/connection-operations/{operation.operation_id}"

    def start(self) -> None:
        """只对账遗留操作；不恢复旧 socket，不自动重放授权交换。"""
        if self._started or self._scope.is_closed:
            raise RuntimeError("MCP 连接操作服务不允许重复启动")
        ready = {server.server_id for server in self._owner.list_servers() if server.status == "ready"}
        for operation in self._store.active():
            if operation.deadline <= time.time():
                status, code = "expired", "deadline_exceeded"
            else:
                try:
                    verified = (operation.action == "connect" and operation.server_id in ready
                                and self._store.configuration_ref(operation.operation_id) == self._configuration_ref(operation.server_id))
                except LookupError:
                    verified = False
                status, code = ("succeeded", None) if verified else ("failed", "process_restarted_unconfirmed")
            self._advance(operation.operation_id, status=status, stage="reconcile", error_code=code)
        self._started = True

    async def close(self) -> None:
        self._started = False
        await self._scope.close()

    async def submit(self, request: McpConnectionRequestDTO) -> McpConnectionOperationDTO:
        if not self._started:
            raise RuntimeError("MCP 连接操作服务尚未启动或正在关闭")
        # 原意图重放先返回历史事实，不能因当前配置变化而另建操作。
        try:
            existing = self._store.get(request.operation_id)
        except LookupError:
            existing = None
        connection_id = self._connection_id(request.server_id)
        if existing is not None:
            if (existing.connection_id, existing.action) != (connection_id, request.action):
                raise ValueError("MCP operation_id 已被不同连接意图使用")
            return existing
        configuration_ref = self._configuration_ref(request.server_id)
        if request.action == "disconnect":
            for active in self._store.active():
                if active.connection_id == connection_id and active.action == "connect":
                    await self.cancel(active.operation_id)
        operation, created = self._store.begin(
            operation_id=request.operation_id, connection_id=connection_id, server_id=request.server_id,
            action=request.action, configuration_ref=configuration_ref, deadline=time.time() + _OPERATION_TIMEOUT_SECONDS,
        )
        if created:
            task = self._scope.start_task(
                partial(self._run, operation), label=operation.operation_id,
            )
            self._tasks[operation.operation_id] = task
            task.add_done_callback(lambda _task: self._tasks.pop(operation.operation_id, None))
        return operation

    def _advance(self, operation_id: str, *, status: str, stage: str, error_code: str | None = None) -> McpConnectionOperationDTO:
        operation = self._store.advance(operation_id, status=status, stage=stage, error_code=error_code)
        self._changed.set()
        self._changed = asyncio.Event()
        return operation

    def _admit(self, operation_id: str) -> None:
        operation = self._store.get(operation_id)
        if operation.terminal or operation.deadline <= time.time():
            raise PermissionError("MCP 连接操作已结束或过期")
        if self._configuration_ref(operation.server_id) != self._store.configuration_ref(operation_id):
            raise PermissionError("MCP 连接操作的配置已改变")

    async def _redirect(self, operation_id: str, url: str) -> None:
        self._admit(operation_id)
        values = parse_qs(urlparse(url).query)
        states = values.get("state", [])
        if len(states) != 1 or not states[0]:
            raise ValueError("MCP SDK 未提供唯一 OAuth state")
        self._owner.mark_authorization_required(self._store.get(operation_id).server_id)
        self._authorizations[operation_id] = _Authorization(url, states[0], asyncio.get_running_loop().create_future())
        self._advance(operation_id, status="waiting_user", stage="authorization")

    async def _callback(self, operation_id: str) -> tuple[str, str | None]:
        authorization = self._authorizations[operation_id]
        result = await authorization.callback
        self._admit(operation_id)
        self._advance(operation_id, status="verifying", stage="initialize")
        return result

    async def _run(self, operation: McpConnectionOperationDTO) -> None:
        identity = operation.operation_id
        remaining = max(0, operation.deadline - time.time())
        # 期限先撤销发布资格；SDK 清理可能仍在进行，不能继续显示有效授权等待。
        expiry = asyncio.get_running_loop().call_later(remaining, self._expire, identity)
        try:
            async with asyncio.timeout(remaining):
                self._admit(identity)
                if operation.action == "connect":
                    server = self._owner.server_configuration(operation.server_id)
                    provider = None if server.oauth is None else McpOAuthProvider(
                        server=server, workspace_root=self._workspace_root,
                        redirect_handler=partial(self._redirect, identity), callback_handler=partial(self._callback, identity),
                    )
                    factory = partial(production_mcp_session_factory, workspace_root=self._workspace_root, oauth_provider=provider)
                    self._advance(identity, status="verifying", stage="initialize")
                    def catalog_ready() -> None:
                        self._advance(identity, status="verifying", stage="catalog")
                    await self._owner.connect_server(
                        operation.server_id, admit=partial(self._admit, identity), session_factory=factory,
                        on_initialized=catalog_ready,
                    )
                else:
                    self._advance(identity, status="verifying", stage="disconnect")
                    await self._owner.disconnect_server(operation.server_id, admit=partial(self._admit, identity))
                self._advance(identity, status="succeeded", stage="complete")
        except TimeoutError:
            current = self._store.get(identity)
            self._advance(identity, status="expired", stage=current.stage, error_code="deadline_exceeded")
        except asyncio.CancelledError:
            current = self._store.get(identity)
            self._advance(identity, status="cancelled", stage=current.stage, error_code="operation_cancelled")
            raise
        except Exception as error:  # noqa: BLE001 - 操作边界只保存安全类型与阶段，不复制 OAuth 或 Server 自由正文。
            current = self._store.get(identity)
            if current.status in {"cancelled", "expired"}:
                self._store.record_cleanup_failure(identity, type(error).__name__)
                raise RuntimeError("MCP 连接操作清理未完成，请检查该操作状态") from None
            self._advance(identity, status="failed", stage=current.stage, error_code=type(error).__name__)
        finally:
            expiry.cancel()
            self._authorizations.pop(identity, None)

    def _expire(self, operation_id: str) -> None:
        current = self._store.get(operation_id)
        self._advance(operation_id, status="expired", stage=current.stage, error_code="deadline_exceeded")

    async def status(self, operation_id: str, *, wait_timeout_ms: int = 0) -> McpConnectionOperationDTO:
        if not 0 <= wait_timeout_ms <= 5000:
            raise ValueError("MCP status 短等待必须在 0–5000ms 内")
        changed = self._changed
        operation = self._store.get(operation_id)
        if wait_timeout_ms and not operation.terminal:
            try:
                async with asyncio.timeout(wait_timeout_ms / 1000):
                    await changed.wait()
            except TimeoutError:
                pass  # 状态查询到期只结束观察，不取消共享连接操作。
            operation = self._store.get(operation_id)
        return operation

    async def wait_terminal(self, operation_id: str, *, deadline: float) -> McpConnectionOperationDTO:
        """只观察权威操作；先捕获通知再读状态，不轮询或延长原期限。"""
        while True:
            changed = self._changed
            operation = self._store.get(operation_id)
            remaining = min(deadline, operation.deadline) - time.time()
            if operation.terminal or remaining <= 0:
                return operation
            try:
                async with asyncio.timeout(remaining):
                    await changed.wait()
            except TimeoutError:
                return self._store.get(operation_id)

    def authorization(self, operation_id: str) -> McpAuthorizationDTO:
        operation = self._store.get(operation_id)
        self._admit(operation.operation_id)
        authorization = self._authorizations.get(operation.operation_id)
        if authorization is None or operation.status != "waiting_user":
            raise ValueError("MCP 连接操作当前不接受用户授权")
        return McpAuthorizationDTO(authorization_url=authorization.url)

    def complete_authorization(self, operation_id: str, *, code: str, state: str) -> McpConnectionOperationDTO:
        operation = self._store.get(operation_id)
        self._admit(operation.operation_id)
        authorization = self._authorizations.get(operation.operation_id)
        if (authorization is None or operation.status != "waiting_user"
                or not secrets.compare_digest(authorization.state.encode(), state.encode())):
            raise ValueError("MCP 授权回调与当前操作不匹配")
        if authorization.callback.done():
            raise ValueError("MCP 授权回调已接收")
        authorization.callback.set_result((code, state))
        return operation

    async def cancel(self, operation_id: str) -> McpConnectionOperationDTO:
        operation = self._store.get(operation_id)
        if not operation.terminal:
            operation = self._advance(operation.operation_id, status="cancelled", stage=operation.stage, error_code="operation_cancelled")
        task = self._tasks.get(operation.operation_id)
        if task is not None:
            task.cancel()
            outcomes = await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            if any(isinstance(outcome, Exception) for outcome in outcomes):
                raise RuntimeError("MCP 连接操作取消后的清理未完成")
        return self._store.get(operation.operation_id)
