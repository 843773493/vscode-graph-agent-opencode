"""原 Owner 的有界短期 handle；持久占用只写 ExternalResourceLeaseLedger。"""

from __future__ import annotations

import asyncio
from _thread import RLock
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from langchain_core.messages import ToolCall
from langchain_core.tools import BaseTool

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import (
    provider_tool_call_id,
    software_tool_operation_id,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingRef,
    ExtensionTargetBinding,
    ExtensionTargetResolutionError,
    payload_digest,
)
from app.services.infrastructure.mcp.invocation import (
    ExtensionExecutionTarget,
    McpResourceUse,
)
from app.services.infrastructure.mcp.session_runtime import McpServerRuntime

CallRetention = Callable[[Sequence[ToolCall]], None]
ReservationKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class McpExecutionHandle:
    binding: ExtensionTargetBinding
    tool: BaseTool
    runtime: McpServerRuntime


@dataclass(slots=True)
class _Reservation:
    handles: dict[str, McpExecutionHandle]
    lease_ids: tuple[str, ...]
    deadline: datetime
    pending_operations: set[str] | None = None
    timer: asyncio.TimerHandle | None = None


class McpExecutionHandles:
    """只保留已获准的原对象引用，不持久化目录或复制 canonical 调用正文。"""

    def __init__(
        self, *, leases: ExternalResourceLeaseLedger, lock: RLock,
        resolve_active: Callable[[ExtensionTargetBinding], McpExecutionHandle], timeout_seconds: int,
    ) -> None:
        self._leases = leases
        self._lock = lock
        self._resolve_active = resolve_active
        self._timeout_seconds = timeout_seconds
        self._reservations: dict[ReservationKey, _Reservation] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()

    def reconcile_lost_handles(self) -> None:
        """重启失去原 handle，只撤销未来资格；实际外部操作继续独立对账。"""
        with self._lock:
            self.close()
            for resource in self._leases.records_by_kind("mcp_connection"):
                for lease in self._leases.leases_for_resource(resource.resource_id):
                    if lease.lease_id.startswith("mcp-prepared:v1:"):
                        self._leases.release(lease.lease_id)

    @contextmanager
    def reserve_model_call(
        self, binding: ExtensionCatalogBindingRef, *, session_id: str, thread_id: str, model_call_id: str,
    ) -> Iterator[CallRetention]:
        """发请求前保留精确连接；返回时收窄到真实模型输出，包含后续 HITL。"""
        key = (session_id, thread_id, model_call_id)
        if not all(key):
            raise ValueError("MCP 封存资格缺少精确 model-call owner")
        with self._lock:
            if self._loop is None or key in self._reservations:
                raise ExtensionTargetResolutionError("MCP 原 model-call 资格不可重复发放")
            handles = {target.target_id: self._resolve_active(target)
                       for target in binding.targets.values() if target.origin == "mcp"}
            deadline = datetime.now(UTC) + timedelta(seconds=self._timeout_seconds)
            lease_ids: list[str] = []
            reservation = _Reservation(handles, (), deadline)
            self._reservations[key] = reservation
            try:
                connection_refs: set[str] = set()
                for handle in handles.values():
                    connection_ref = handle.binding.connection_ref
                    assert connection_ref is not None
                    connection_refs.add(connection_ref)
                for connection_ref in sorted(connection_refs):
                    lease_id = "mcp-prepared:v1:" + payload_digest(
                        [*key, connection_ref], context="MCP 封存资格", error_type=ValueError,
                    )
                    if self._leases.get_lease(lease_id) is not None:
                        raise ExtensionTargetResolutionError("MCP 原封存资格已结算或失去 handle，不能重新发放")
                    self._leases.acquire_operation(
                        resource_id=connection_ref, turn_stream_id=lease_id, operation_id=model_call_id,
                        lease_id=lease_id, holder_session_id=session_id, holder_thread_id=thread_id,
                        expires_at=deadline.isoformat(),
                    )
                    lease_ids.append(lease_id)
                    reservation.lease_ids = tuple(lease_ids)
            except BaseException:
                self._release(key, reservation)
                raise
            # 空 MCP 目录没有跨响应保留的连接；同步准备可在短期事件循环退出后
            # 继续发送，不应为了不存在的资源向已结束的 loop 登记定时器。
            if handles:
                self._loop.call_soon_threadsafe(self._schedule_expiry, key, reservation)

        def retain(calls: Sequence[ToolCall]) -> None:
            with self._lock:
                if reservation.pending_operations is not None:
                    raise RuntimeError("MCP 模型输出资格只能收窄一次")
                # 生成期间到期仍交付真实响应；ToolNode 随后返回原调用的配对失败。
                if self._reservations.get(key) is not reservation:
                    return
                identity = ItemProjectionIdentity(model_call_id=model_call_id)
                operations: set[str] = set()
                for call in calls:
                    if call["name"] != "invoke_extension_tool":
                        continue
                    call_id = call["id"]
                    if not isinstance(call_id, str) or not call_id:
                        raise ValueError("MCP 模型输出缺少稳定 tool_call_id")
                    target_name = call["args"].get("tool_name")
                    if not isinstance(target_name, str) or target_name.strip() not in binding.targets:
                        continue
                    operations.add(software_tool_operation_id(
                        "mcp_call", session_id, thread_id, model_call_id,
                        provider_tool_call_id(identity, call_id),
                    ))
                reservation.pending_operations = operations
                # HITL 的显式 edit 可改为原封存目录内其它目标；不能只保留模型
                # 初选目标。拒绝／直接应答及本地调用的未用资格至多保留到原期限。
                # TODO(4.4): 从确认结算／canonical 完整结果边界提前释放这些未用资格。
                if not handles or not reservation.pending_operations:
                    self._release(key, reservation)

        try:
            yield retain
        finally:
            with self._lock:
                if reservation.pending_operations is None:
                    self._release(key, reservation)

    @contextmanager
    def execution_target(
        self, binding: ExtensionTargetBinding, use: McpResourceUse | None,
    ) -> Iterator[ExtensionExecutionTarget]:
        with self._lock:
            if use is None or not all((use.holder_id, use.operation_id, use.session_id, use.thread_id, use.model_call_id)):
                raise ExtensionTargetResolutionError("MCP 执行缺少受信 typed resource-use")
            lease_id = "mcp-invocation:v1:" + payload_digest(
                [binding.connection_ref, use.operation_id], context="MCP operation lease", error_type=ValueError,
            )
            if self._leases.get_lease(lease_id) is not None:
                raise ExtensionTargetResolutionError("MCP 原 operation 已执行或待核实，禁止自动重放")
            key = (use.session_id, use.thread_id, use.model_call_id)
            reservation = self._reservations.get(key)
            if reservation is None:
                raise ExtensionTargetResolutionError("MCP 原连接的封存执行资格已失效或不可恢复")
            if datetime.now(UTC) >= reservation.deadline:
                self._release(key, reservation)
                raise ExtensionTargetResolutionError("MCP 原连接的封存执行资格已到期")
            # TODO(4.4/7.x): 正常热替换的退役 runtime 在共同发布屏障内标记后，
            # 可只核原 handle；当前仍核活跃目录，不能放宽既有 schema 撤换行为。
            self._resolve_active(binding)
            handle = reservation.handles.get(binding.target_id)
            if (handle is None or handle.binding != binding or not handle.runtime.running
                    or reservation.pending_operations is None
                    or use.operation_id not in reservation.pending_operations):
                raise ExtensionTargetResolutionError("MCP 原连接的执行 handle 或调用资格不匹配")
            assert binding.connection_ref is not None
            self._leases.acquire_operation(
                resource_id=binding.connection_ref, turn_stream_id=use.holder_id,
                operation_id=use.operation_id, lease_id=lease_id,
                holder_session_id=use.session_id, holder_thread_id=use.thread_id,
            )
        execution = ExtensionExecutionTarget(handle.tool)
        try:
            yield execution
        finally:
            with self._lock:
                if not execution.started or execution.result_confirmed:
                    self._leases.release(lease_id)
                else:
                    record = self._leases.get(binding.connection_ref)
                    assert record is not None
                    self._leases.update_external_state(
                        binding.connection_ref, lifetime_scope="workspace", status=record.status,
                        unresolved_lease_id=lease_id,
                    )
                reservation.pending_operations.discard(use.operation_id)
                if not reservation.pending_operations:
                    self._release(key, reservation)

    def close(self) -> None:
        with self._lock:
            self._loop = None
            for key, reservation in tuple(self._reservations.items()):
                self._release(key, reservation)

    def _schedule_expiry(self, key: ReservationKey, reservation: _Reservation) -> None:
        with self._lock:
            if self._reservations.get(key) is not reservation:
                return
            assert self._loop is not None
            remaining = max(0, (reservation.deadline - datetime.now(UTC)).total_seconds())
            reservation.timer = self._loop.call_later(remaining, self._expire, key, reservation)

    def _expire(self, key: ReservationKey, reservation: _Reservation) -> None:
        with self._lock:
            self._release(key, reservation)

    def _release(self, key: ReservationKey, reservation: _Reservation) -> None:
        if self._reservations.get(key) is not reservation:
            return
        # 只释放未来执行资格，不结清正在执行／结果未知的 operation。
        for lease_id in reservation.lease_ids:
            self._leases.release(lease_id)
        if reservation.timer is not None:
            reservation.timer.cancel()
        del self._reservations[key]
