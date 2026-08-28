from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class ScopeCancelledError(RuntimeError):
    """运行时操作收到结构化取消请求。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"运行时 scope 已取消: reason={reason}")


class ScopeDeadlineExceededError(ScopeCancelledError):
    """局部操作超时；不会自动升级为 Turn 级用户中断。"""


_CURRENT_TURN_SCOPE: ContextVar[TurnExecutionScope | None] = ContextVar(
    "boxteam_current_turn_execution_scope",
    default=None,
)


def set_current_turn_execution_scope(scope: TurnExecutionScope) -> Token[TurnExecutionScope | None]:
    return _CURRENT_TURN_SCOPE.set(scope)


def reset_current_turn_execution_scope(token: Token[TurnExecutionScope | None]) -> None:
    _CURRENT_TURN_SCOPE.reset(token)


def get_current_turn_execution_scope() -> TurnExecutionScope | None:
    return _CURRENT_TURN_SCOPE.get()


CancellationHook = Callable[[str], Awaitable[None] | None]
CleanupHook = Callable[[], Awaitable[None] | None]


class CancellationSignal:
    """只负责取消通知、原因和 hook 的窄职责信号。"""

    def __init__(self, parent: CancellationSignal | None = None) -> None:
        self._cancelled = False
        self._reason: str | None = None
        self._event = asyncio.Event()
        self._hooks: dict[int, CancellationHook] = {}
        self._next_hook_id = 0
        if parent is not None:
            parent.add_hook(self._cascade_from_parent)
            if parent.is_cancelled:
                self._cancelled = True
                self._reason = parent.reason or "parent_cancelled"
                self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def add_hook(self, hook: CancellationHook) -> int:
        hook_id = self._next_hook_id
        self._next_hook_id += 1
        self._hooks[hook_id] = hook
        if self._cancelled:
            result = hook(self._reason or "cancelled")
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        return hook_id

    def remove_hook(self, hook_id: int) -> None:
        self._hooks.pop(hook_id, None)

    async def cancel(self, reason: str) -> bool:
        if self._cancelled:
            return False
        if not reason:
            raise ValueError("CancellationSignal.cancel 缺少取消原因")
        self._cancelled = True
        self._reason = reason
        self._event.set()
        errors: list[Exception] = []
        for hook in tuple(self._hooks.values()):
            try:
                result = hook(reason)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        if errors:
            raise ExceptionGroup("取消 hook 执行失败", errors)
        return True

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise ScopeCancelledError(self._reason or "cancelled")

    async def wait(self) -> str:
        """等待取消通知；这里只暴露取消广播，不承载其它控制语义。"""
        await self._event.wait()
        return self._reason or "cancelled"

    async def _cascade_from_parent(self, reason: str) -> None:
        await self.cancel(reason)


@dataclass(slots=True, eq=False)
class TurnExecutionScope:
    """单个 Turn 的进程内执行边界，不是持久化恢复权威。"""

    turn_stream_id: str
    deadline: float | None = None
    parent: TurnExecutionScope | None = None
    cancellation_signal: CancellationSignal = field(init=False)
    _children: set[TurnExecutionScope] = field(default_factory=set, init=False)
    _cleanup_hooks: list[CleanupHook] = field(default_factory=list, init=False)
    _abort_hooks: dict[int, CancellationHook] = field(default_factory=dict, init=False)
    _next_abort_hook_id: int = field(default=0, init=False)
    _lease_ids: set[str] = field(default_factory=set, init=False)
    _active_operation: TurnExecutionScope | None = field(default=None, init=False)
    _deadline_handle: asyncio.TimerHandle | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        parent_signal = self.parent.cancellation_signal if self.parent else None
        self.cancellation_signal = CancellationSignal(parent_signal)
        self.cancellation_signal.add_hook(self._run_abort_hooks)
        if self.deadline is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._deadline_handle = loop.call_later(
                    max(0.0, self.deadline - time.monotonic()),
                    self._schedule_deadline_cancel,
                )
        if self.parent is not None:
            self.parent._children.add(self)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def lease_ids(self) -> frozenset[str]:
        return frozenset(self._lease_ids)

    def child(
        self,
        name: str,
        *,
        deadline: float | None = None,
        timeout_seconds: float | None = None,
    ) -> TurnExecutionScope:
        if not name:
            raise ValueError("TurnExecutionScope.child 缺少 name")
        if deadline is not None and timeout_seconds is not None:
            raise ValueError("child 不能同时指定 deadline 和 timeout_seconds")
        if timeout_seconds is not None:
            if timeout_seconds <= 0:
                raise ValueError("child timeout_seconds 必须大于 0")
            deadline = time.monotonic() + timeout_seconds
        if self._closed:
            raise RuntimeError("已关闭的 TurnExecutionScope 不能创建 child scope")
        child_scope = TurnExecutionScope(
            turn_stream_id=f"{self.turn_stream_id}:{name}",
            deadline=deadline,
            parent=self,
        )
        self._children.add(child_scope)
        return child_scope

    @property
    def effective_scope(self) -> TurnExecutionScope:
        return self._active_operation or self

    @property
    def effective_cancellation_signal(self) -> CancellationSignal:
        return self.effective_scope.cancellation_signal

    def set_active_operation(self, scope: TurnExecutionScope) -> None:
        if scope.parent is not self and scope is not self:
            raise RuntimeError("active operation 必须属于当前 TurnExecutionScope")
        self._active_operation = scope

    def clear_active_operation(self, scope: TurnExecutionScope | None = None) -> None:
        if scope is None or self._active_operation is scope:
            self._active_operation = None

    def register_abort(self, hook: CancellationHook) -> int:
        if self._closed:
            raise RuntimeError("已关闭的 TurnExecutionScope 不能注册 abort hook")
        hook_id = self._next_abort_hook_id
        self._next_abort_hook_id += 1
        self._abort_hooks[hook_id] = hook
        if self.cancellation_signal.is_cancelled:
            result = hook(self.cancellation_signal.reason or "cancelled")
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        return hook_id

    def remove_abort(self, hook_id: int) -> None:
        self._abort_hooks.pop(hook_id, None)

    def register_cleanup(self, hook: CleanupHook) -> int:
        if self._closed:
            raise RuntimeError("已关闭的 TurnExecutionScope 不能注册 cleanup")
        self._cleanup_hooks.append(hook)
        return len(self._cleanup_hooks) - 1

    def add_lease(self, lease_id: str) -> None:
        if not lease_id:
            raise ValueError("TurnExecutionScope.add_lease 缺少 lease_id")
        if self._closed:
            raise RuntimeError("已关闭的 TurnExecutionScope 不能持有 lease")
        self._lease_ids.add(lease_id)

    def remove_lease(self, lease_id: str) -> None:
        self._lease_ids.discard(lease_id)

    async def cancel(self, reason: str) -> bool:
        return await self.cancellation_signal.cancel(reason)

    @property
    def deadline_expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    async def enforce_deadline(self) -> bool:
        """只取消当前局部 scope，不把局部超时升级为整个 Turn 中断。"""
        if not self.deadline_expired:
            return False
        await self.cancel("scope_deadline_exceeded")
        return True

    def raise_if_cancelled(self) -> None:
        self.effective_cancellation_signal.raise_if_cancelled()
        if self.effective_scope.deadline_expired:
            raise ScopeDeadlineExceededError("scope_deadline_exceeded")

    async def _run_abort_hooks(self, reason: str) -> None:
        errors: list[Exception] = []
        for hook in tuple(self._abort_hooks.values()):
            try:
                result = hook(reason)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        if errors:
            raise ExceptionGroup("scope abort hook 执行失败", errors)

    def _schedule_deadline_cancel(self) -> None:
        if not self._closed and not self.cancellation_signal.is_cancelled:
            asyncio.create_task(self.cancel("scope_deadline_exceeded"))

    async def close(self) -> None:
        if self._closed:
            return
        errors: list[Exception] = []
        for child in tuple(self._children):
            try:
                await child.close()
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        for hook in reversed(self._cleanup_hooks):
            try:
                result = hook()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        self._closed = True
        if self.parent is not None:
            self.parent._children.discard(self)
        if self._deadline_handle is not None:
            self._deadline_handle.cancel()
            self._deadline_handle = None
        self._abort_hooks.clear()
        self._active_operation = None
        if errors:
            raise ExceptionGroup("TurnExecutionScope cleanup 失败", errors)


class TurnExecutionScopeRegistry:
    """按 turn_stream_id 隔离活动 Turn 的内存注册表。"""

    def __init__(self) -> None:
        self._scopes: dict[str, TurnExecutionScope] = {}
        self._inboxes: dict[str, AgentControlInbox] = {}

    def create(self, turn_stream_id: str) -> TurnExecutionScope:
        if turn_stream_id in self._scopes:
            raise RuntimeError(f"Turn 已存在执行 scope: turn_stream_id={turn_stream_id}")
        scope = TurnExecutionScope(turn_stream_id=turn_stream_id)
        self._scopes[turn_stream_id] = scope
        return scope

    def get(self, turn_stream_id: str) -> TurnExecutionScope | None:
        return self._scopes.get(turn_stream_id)

    def register_inbox(self, turn_stream_id: str, inbox: AgentControlInbox) -> None:
        if turn_stream_id in self._inboxes:
            raise RuntimeError(f"Turn 已存在 AgentControlInbox: turn_stream_id={turn_stream_id}")
        self._inboxes[turn_stream_id] = inbox

    def get_inbox(self, turn_stream_id: str) -> AgentControlInbox | None:
        return self._inboxes.get(turn_stream_id)

    async def cancel(self, turn_stream_id: str, reason: str) -> bool:
        scope = self._scopes.get(turn_stream_id)
        if scope is None:
            return False
        return await scope.cancel(reason)

    async def close(self, turn_stream_id: str) -> None:
        scope = self._scopes.pop(turn_stream_id, None)
        self._inboxes.pop(turn_stream_id, None)
        if scope is not None:
            await scope.close()

    def active_ids(self) -> tuple[str, ...]:
        return tuple(self._scopes)


@dataclass(frozen=True, slots=True)
class AgentControlCommand:
    command_id: str
    kind: str
    turn_stream_id: str
    idempotency_key: str
    payload: dict[str, object]
    control_seq: int
    status: str = "accepted"


class AgentControlInbox:
    """Turn 内带序号、幂等和结果状态的控制命令队列。"""

    _KINDS = frozenset(
        {
            "interrupt",
            "steer",
            "approval.result",
            "resume",
            "resource.operation.result",
        }
    )

    def __init__(self, turn_stream_id: str, *, state_path: Path | None = None) -> None:
        self.turn_stream_id = turn_stream_id
        self._state_path = state_path
        self._next_seq = 0
        self._commands: dict[str, AgentControlCommand] = {}
        self._pending: asyncio.Queue[AgentControlCommand] = asyncio.Queue()
        self._load()

    def accept(
        self,
        *,
        command_id: str,
        kind: str,
        idempotency_key: str,
        payload: dict[str, object] | None = None,
    ) -> AgentControlCommand:
        if not command_id or not idempotency_key:
            raise ValueError("控制命令必须包含 command_id 和 idempotency_key")
        if kind not in self._KINDS:
            raise ValueError(f"未知 Agent 控制命令: kind={kind}")
        existing = next(
            (
                command
                for command in self._commands.values()
                if command.idempotency_key == idempotency_key
            ),
            None,
        )
        if existing is not None:
            return existing
        self._next_seq += 1
        command = AgentControlCommand(
            command_id=command_id,
            kind=kind,
            turn_stream_id=self.turn_stream_id,
            idempotency_key=idempotency_key,
            payload=dict(payload or {}),
            control_seq=self._next_seq,
        )
        self._commands[command_id] = command
        self._persist()
        self._pending.put_nowait(command)
        return command

    async def next(self) -> AgentControlCommand:
        return await self._pending.get()

    def mark(self, command_id: str, status: str) -> AgentControlCommand:
        if status not in {"accepted", "consumed", "rejected"}:
            raise ValueError(f"未知控制命令状态: status={status}")
        command = self._commands.get(command_id)
        if command is None:
            raise KeyError(f"控制命令不存在: command_id={command_id}")
        updated = AgentControlCommand(
            command_id=command.command_id,
            kind=command.kind,
            turn_stream_id=command.turn_stream_id,
            idempotency_key=command.idempotency_key,
            payload=dict(command.payload),
            control_seq=command.control_seq,
            status=status,
        )
        self._commands[command_id] = updated
        self._persist()
        return updated

    def get(self, command_id: str) -> AgentControlCommand | None:
        return self._commands.get(command_id)

    def snapshot(self) -> list[AgentControlCommand]:
        return sorted(self._commands.values(), key=lambda command: command.control_seq)

    def recoverable(self) -> list[AgentControlCommand]:
        """返回崩溃前已接受但未消费的命令，不自动重放副作用。"""
        return [
            command
            for command in self.snapshot()
            if command.status == "accepted"
        ]

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError(f"AgentControlInbox 状态文件必须是对象: path={self._state_path}")
        for value in raw.get("commands", []):
            if not isinstance(value, dict):
                raise TypeError("AgentControlInbox 状态文件包含无效命令")
            command = AgentControlCommand(**value)
            if command.turn_stream_id != self.turn_stream_id:
                raise RuntimeError("AgentControlInbox 状态文件的 turn_stream_id 不匹配")
            self._commands[command.command_id] = command
            self._next_seq = max(self._next_seq, command.control_seq)

    def _persist(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._state_path.with_suffix(".tmp")
        payload = {
            "turn_stream_id": self.turn_stream_id,
            "commands": [
                {
                    "command_id": command.command_id,
                    "kind": command.kind,
                    "turn_stream_id": command.turn_stream_id,
                    "idempotency_key": command.idempotency_key,
                    "payload": command.payload,
                    "control_seq": command.control_seq,
                    "status": command.status,
                }
                for command in self.snapshot()
            ],
        }
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(self._state_path)


class MessageStreamCommitter(Protocol):
    async def commit(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        event_id: str | None = None,
    ) -> dict[str, object]: ...


@dataclass(slots=True)
class TurnControlCoordinator:
    """将 interrupt 的持久事实和 CancellationSignal 连接成唯一路径。"""

    scope: TurnExecutionScope
    inbox: AgentControlInbox
    writer: MessageStreamCommitter

    async def submit_interrupt(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reason: str = "user_requested",
    ) -> dict[str, object]:
        command = self.inbox.accept(
            command_id=command_id,
            kind="interrupt",
            idempotency_key=idempotency_key,
            payload={"reason": reason},
        )
        if command.status != "accepted":
            return {"status": command.status, "command_id": command.command_id}
        event = await self.writer.commit(
            "interrupt.requested",
            {
                "interrupt_request_id": command.command_id,
                "reason": reason,
            },
            event_id=command.command_id,
        )
        if event["type"] == "interrupt.rejected":
            self.inbox.mark(command.command_id, "rejected")
            return {**event, "command_id": command.command_id}
        await self.scope.cancel(reason)
        self.inbox.mark(command.command_id, "consumed")
        return {**event, "command_id": command.command_id}


@dataclass(slots=True)
class AgentLoopControlCoordinator:
    """AgentLoop 唯一控制消费点；迟到控制只能被拒绝，不能复活 Turn。"""

    scope: TurnExecutionScope
    inbox: AgentControlInbox
    writer: MessageStreamCommitter

    async def process(self, command: AgentControlCommand) -> dict[str, object]:
        if command.status != "accepted":
            return {"status": command.status, "command_id": command.command_id}
        if self.scope.is_closed or self.scope.cancellation_signal.is_cancelled:
            self.inbox.mark(command.command_id, "rejected")
            return {
                "status": "rejected",
                "command_id": command.command_id,
                "reason": "turn_not_active",
            }
        if command.kind == "interrupt":
            return await TurnControlCoordinator(
                scope=self.scope,
                inbox=self.inbox,
                writer=self.writer,
            ).submit_interrupt(
                command_id=command.command_id,
                idempotency_key=command.idempotency_key,
                reason=str(command.payload.get("reason") or "user_requested"),
            )
        # 这些命令必须由明确的业务 handler 消费；没有 handler 时宁可拒绝，
        # 不能假设审批/资源结果已经安全生效。
        self.inbox.mark(command.command_id, "rejected")
        return {
            "status": "rejected",
            "command_id": command.command_id,
            "reason": "no_agent_loop_handler",
        }

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            next_command = asyncio.create_task(self.inbox.next())
            stop_waiter = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {next_command, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop_waiter in done:
                return
            command = next_command.result()
            await self.process(command)
