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

from app.core.lifecycle import LifetimeScope
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
ReservationKey = tuple[str, ...]


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
    owner_scope: LifetimeScope | None = None


class McpExecutionHandles:
    """只保留已获准的原对象引用，不持久化目录或复制 canonical 调用正文。"""

    def __init__(
        self, *, leases: ExternalResourceLeaseLedger, lock: RLock,
        resolve_active: Callable[[ExtensionTargetBinding], McpExecutionHandle], timeout_seconds: int,
        validate_retained: Callable[[ExtensionTargetBinding], None],
    ) -> None:
        self._leases = leases
        self._lock = lock
        self._resolve_active = resolve_active
        self._validate_retained = validate_retained
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
                    if lease.lease_id.startswith(("mcp-prepared:v1:", "mcp-execution:v1:")):
                        self._leases.release(lease.lease_id)

    def _register_qualification(
        self, key: ReservationKey, reservation: _Reservation, *, prefix: str,
        operation_id: str, session_id: str, thread_id: str,
    ) -> None:
        self._reservations[key] = reservation
        lease_ids: list[str] = []
        try:
            connection_refs = {handle.binding.connection_ref for handle in reservation.handles.values()}
            for connection_ref in sorted(ref for ref in connection_refs if ref is not None):
                lease_id = prefix + payload_digest([*key, connection_ref], context="MCP 封存资格", error_type=ValueError)
                if self._leases.get_lease(lease_id) is not None:
                    raise ExtensionTargetResolutionError("MCP 原封存资格已结算或失去 handle，不能重新发放")
                self._leases.acquire_operation(
                    resource_id=connection_ref, turn_stream_id=lease_id, operation_id=operation_id,
                    lease_id=lease_id, holder_session_id=session_id, holder_thread_id=thread_id,
                    expires_at=reservation.deadline.isoformat(),
                )
                lease_ids.append(lease_id)
                reservation.lease_ids = tuple(lease_ids)
        except BaseException:
            self._release(key, reservation)
            raise
        if reservation.handles:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._schedule_expiry, key, reservation)

    def _execution_qualification(
        self, binding: ExtensionCatalogBindingRef, *, session_id: str, thread_id: str,
        execution_id: str, scope: LifetimeScope,
    ) -> _Reservation:
        """turn-bound 目录在原执行内保留；后继模型调用只继承，不延长期限。"""
        key = ("execution", session_id, thread_id, execution_id)
        reservation = self._reservations.get(key)
        targets = {target.target_id: target for target in binding.targets.values() if target.origin == "mcp"}
        if reservation is not None:
            if (reservation.owner_scope is not scope or scope.state != "open"
                    or datetime.now(UTC) >= reservation.deadline
                    or {name: handle.binding for name, handle in reservation.handles.items()} != targets):
                raise ExtensionTargetResolutionError("MCP 原执行的连接资格已到期或冻结目录不一致")
            for target in targets.values():
                self._validate_retained(target)
            return reservation
        if scope.state != "open":
            raise ExtensionTargetResolutionError("MCP 原执行 scope 已关闭")
        reservation = _Reservation(
            {name: self._resolve_active(target) for name, target in targets.items()}, (),
            datetime.now(UTC) + timedelta(seconds=self._timeout_seconds), owner_scope=scope,
        )
        self._register_qualification(key, reservation, prefix="mcp-execution:v1:",
                                     operation_id=execution_id, session_id=session_id, thread_id=thread_id)

        def release() -> None:
            with self._lock:
                self._release(key, reservation)

        scope.register(release, label=f"mcp-execution:{execution_id}")
        return reservation

    @contextmanager
    def reserve_model_call(
        self, binding: ExtensionCatalogBindingRef, *, session_id: str, thread_id: str, model_call_id: str,
        execution_id: str | None = None, execution_scope: LifetimeScope | None = None,
    ) -> Iterator[CallRetention]:
        """发请求前保留精确连接；返回时收窄到真实模型输出，包含后续 HITL。"""
        key = (session_id, thread_id, model_call_id)
        if not all(key):
            raise ValueError("MCP 封存资格缺少精确 model-call owner")
        with self._lock:
            if self._loop is None or key in self._reservations:
                raise ExtensionTargetResolutionError("MCP 原 model-call 资格不可重复发放")
            if (execution_id is None) != (execution_scope is None):
                raise ValueError("MCP 执行资格需要完整 execution owner 和 scope")
            inherited = None
            if execution_id is not None and execution_scope is not None:
                inherited = self._execution_qualification(binding, session_id=session_id, thread_id=thread_id,
                                                          execution_id=execution_id, scope=execution_scope)
            handles = inherited.handles if inherited is not None else {
                target.target_id: self._resolve_active(target)
                for target in binding.targets.values() if target.origin == "mcp"
            }
            deadline = inherited.deadline if inherited is not None else datetime.now(UTC) + timedelta(seconds=self._timeout_seconds)
            reservation = _Reservation(handles, (), deadline)
            self._register_qualification(key, reservation, prefix="mcp-prepared:v1:",
                                         operation_id=model_call_id, session_id=session_id, thread_id=thread_id)

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
            self._validate_retained(binding)
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
