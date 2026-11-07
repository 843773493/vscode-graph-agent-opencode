"""附属于原宿主代际的 Provider 控制连接，不另建宿主、调用租约或安装事实。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from app.schemas.gateway.mcp_definitions import (
    McpProviderCancelDTO,
    McpProviderInvalidateDTO,
    McpProviderResolvedDTO,
    McpProviderResolveDTO,
    McpProviderSnapshotDTO,
)
from app.services.infrastructure.mcp.provider import (
    McpProviderAdmission,
    McpProviderUnavailable,
)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    value: McpProviderSnapshotDTO
    invalidated: asyncio.Event


class McpDefinitionChannel:
    def __init__(self) -> None:
        self.outbound: asyncio.Queue[McpProviderResolveDTO | McpProviderCancelDTO] = asyncio.Queue()
        self.snapshots: dict[str, _Snapshot] = {}
        self._pending: dict[str, tuple[str, asyncio.Future[McpProviderResolvedDTO]]] = {}
        self.closed = False
        self.closed_event = asyncio.Event()

    def invalidate(self, provider_ref: str) -> None:
        snapshot = self.snapshots.pop(provider_ref, None)
        if snapshot is not None:
            snapshot.invalidated.set()
        for request_id, (provider, future) in tuple(self._pending.items()):
            if provider == provider_ref and not future.done():
                future.set_exception(McpProviderUnavailable("Provider 定义已变化或注销"))
                self.outbound.put_nowait(McpProviderCancelDTO(request_id=request_id))

    def receive(self, message: McpProviderSnapshotDTO | McpProviderInvalidateDTO | McpProviderResolvedDTO) -> None:
        if self.closed:
            raise McpProviderUnavailable("Provider 控制通道已关闭")
        if isinstance(message, McpProviderSnapshotDTO):
            self.invalidate(message.provider_ref)
            self.snapshots[message.provider_ref] = _Snapshot(message, asyncio.Event())
        elif isinstance(message, McpProviderInvalidateDTO):
            self.invalidate(message.provider_ref)
        else:
            pending = self._pending.get(message.request_id)
            # 取消后的迟到秘密直接丢弃，不缓存、不复活原候选。
            if pending is not None and not pending[1].done():
                pending[1].set_result(message)

    def definition(self, provider_ref: str, definition_ref: str) -> _Snapshot:
        snapshot = self.snapshots.get(provider_ref)
        if self.closed or snapshot is None or snapshot.invalidated.is_set():
            raise McpProviderUnavailable("原 Provider 已离线或定义已失效")
        if snapshot.value.error_code is not None or not any(item.definition_ref == definition_ref for item in snapshot.value.definitions):
            raise McpProviderUnavailable("原 Provider 没有当前定义，需重新选择来源")
        return snapshot

    async def resolve(self, provider_ref: str, definition_ref: str, check: Callable[[], None]) -> McpProviderAdmission:
        snapshot = self.definition(provider_ref, definition_ref)
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = provider_ref, future
        self.outbound.put_nowait(McpProviderResolveDTO(request_id=request_id, provider_ref=provider_ref, definition_ref=definition_ref))
        try:
            result = await future
            check()
            if result.launch is None:
                raise McpProviderUnavailable(f"Provider 启动前解析未完成：{result.error_code}")
            return McpProviderAdmission(result.launch, snapshot.invalidated, check)
        finally:
            self._pending.pop(request_id, None)
            if not future.done() or future.cancelled():
                self.outbound.put_nowait(McpProviderCancelDTO(request_id=request_id))

    def close(self) -> None:
        self.closed = True
        self.closed_event.set()
        for provider_ref in tuple(self.snapshots):
            self.invalidate(provider_ref)
