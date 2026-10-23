"""进程内资源生命周期的唯一释放合同。

``LifetimeScope`` 只负责持有、排空和释放自己拥有的资源；它不发布业务
状态，也不决定外部资源是否应该停止。外部 owner 收到释放错误后，负责把
错误转换为相应的业务状态或恢复任务。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeAlias

ReleaseCallback: TypeAlias = Callable[[], Awaitable[None] | None]


@dataclass(slots=True)
class _OwnedResource:
    callback: ReleaseCallback
    label: str
    released: bool = False
    release_task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class LifetimeSnapshot:
    """仅用于诊断的 scope 快照，不承载业务状态。"""

    name: str
    state: str
    resource_count: int
    child_count: int
    task_count: int


class LifetimeHandle:
    """登记到 :class:`LifetimeScope` 的可撤销句柄（OpenSpec 3.9）。

    ``revoke()`` 等价于 ``release(handle.resource_id)``：提前释放对应资源，
    关闭时不再调用该回调。重复撤销幂等；scope 成功关闭后资源必然已被释放，
    此时撤销退化为无操作；scope 处于 close_failed/closing 时撤销走既有释放
    路径的显式错误，不虚报撤销成功。
    """

    __slots__ = ("_label", "_resource_id", "_revoked", "_scope")

    def __init__(self, *, scope: LifetimeScope, resource_id: int, label: str) -> None:
        self._scope = scope
        self._resource_id = resource_id
        self._label = label
        self._revoked = False

    @property
    def resource_id(self) -> int:
        return self._resource_id

    @property
    def label(self) -> str:
        return self._label

    @property
    def revoked(self) -> bool:
        return self._revoked

    async def revoke(self) -> None:
        """撤销登记：提前释放该资源；重复撤销幂等。"""
        if self._revoked:
            return
        await self._scope._revoke_handle(self)
        self._revoked = True


class LifetimeScope:
    """可嵌套、可重入关闭且只拥有进程内资源的生命周期 scope。

    资源按注册顺序的逆序释放，子 scope 在父 scope 的资源之前释放。关闭
    期间禁止注册新资源；关闭失败时保留失败资源，下一次 ``close`` 可重试，
    并把全部错误返回给持有 scope 的 owner。
    """

    _OPEN = "open"
    _CLOSING = "closing"
    _CLOSE_FAILED = "close_failed"
    _CLOSED = "closed"

    def __init__(self, name: str, *, parent: LifetimeScope | None = None) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("LifetimeScope.name 必须是非空字符串")
        self._name = name
        self._parent = parent
        self._state = self._OPEN
        self._resources: dict[int, _OwnedResource] = {}
        self._children: dict[int, LifetimeScope] = {}
        self._next_resource_id = 0
        self._state_lock = threading.RLock()
        self._tasks: dict[asyncio.Future[object], str] = {}
        self._close_task: asyncio.Task[None] | None = None
        if parent is not None:
            parent._attach_child(self)

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def is_closed(self) -> bool:
        return self.state == self._CLOSED

    def snapshot(self) -> LifetimeSnapshot:
        with self._state_lock:
            return LifetimeSnapshot(
                name=self._name,
                state=self._state,
                resource_count=sum(
                    not resource.released for resource in self._resources.values()
                ),
                child_count=len(self._children),
                task_count=len(self._tasks),
            )

    def start_task(
        self,
        operation: Callable[[], Awaitable[object]],
        *,
        label: str,
    ) -> asyncio.Future[object]:
        """持有后台操作；关闭前先取消并排空，失败保留给 owner。"""
        with self._state_lock:
            self._require_open_for_registration()
            task = asyncio.ensure_future(operation())
            self._tasks[task] = label
            task.add_done_callback(self._task_finished)
            return task

    def _task_finished(self, task: asyncio.Future[object]) -> None:
        error = None if task.cancelled() else task.exception()
        with self._state_lock:
            if error is None:
                self._tasks.pop(task, None)
                return
        # 终态失败仍由 scope 持有；后续 close 重报，不能把已失败的操作虚报成功。
        task.get_loop().call_exception_handler({
            "message": f"LifetimeScope 后台任务失败: scope={self._name}",
            "exception": error,
            "task": task,
        })

    def register(self, callback: ReleaseCallback, *, label: str = "resource") -> LifetimeHandle:
        """登记一个由当前 scope 独占的释放回调，返回可撤销句柄。

        句柄的 ``revoke()`` 提前释放对应资源（关闭时不再调用该回调）；
        ``release(resource_id)`` 仍然可用（``resource_id`` 取自
        ``handle.resource_id``）。
        """
        if not callable(callback):
            raise TypeError("LifetimeScope.register callback 必须可调用")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("LifetimeScope.register label 必须是非空字符串")
        with self._state_lock:
            self._require_open_for_registration()
            resource_id = self._next_resource_id
            self._next_resource_id += 1
            self._resources[resource_id] = _OwnedResource(callback, label)
            return LifetimeHandle(scope=self, resource_id=resource_id, label=label)

    async def _revoke_handle(self, handle: LifetimeHandle) -> None:
        """句柄撤销的内部路径；成功关闭后的撤销退化为无操作。"""
        with self._state_lock:
            if self._state == self._CLOSED:
                # 成功关闭意味着资源已按逆序释放完毕；再次撤销是无操作。
                return
        await self.release(handle.resource_id)

    async def release(self, resource_id: int) -> None:
        """提前释放一个已登记资源；释放失败原样返回给 owner。"""
        with self._state_lock:
            self._require_open_for_registration()
            resource = self._resources.get(resource_id)
            if resource is None:
                raise KeyError(f"LifetimeScope resource 不存在: {resource_id}")
        await self._release_resource(resource)

    async def _release_resource(self, resource: _OwnedResource) -> None:
        with self._state_lock:
            if resource.released:
                return
            if resource.release_task is None or resource.release_task.done():
                resource.release_task = asyncio.create_task(
                    self._release_owned_resource(resource),
                    name=f"{self._name}:release:{resource.label}",
                )
                resource.release_task.add_done_callback(self._report_cleanup_failure)
            releasing = resource.release_task
        # 提前撤销与 close 共用一次释放；取消等待者不能遗弃释放操作。
        await asyncio.shield(releasing)

    async def _release_owned_resource(self, resource: _OwnedResource) -> None:
        result = resource.callback()
        if inspect.isawaitable(result):
            await result
        with self._state_lock:
            resource.released = True

    def _report_cleanup_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            error: BaseException | None = asyncio.CancelledError(task.get_name())
        else:
            error = task.exception()
        if error is not None:
            task.get_loop().call_exception_handler({
                "message": f"LifetimeScope 清理失败: scope={self._name}",
                "exception": error,
                "task": task,
            })

    def child(self, name: str) -> LifetimeScope:
        """创建一个受当前 scope 所有的子 scope。"""
        return LifetimeScope(name, parent=self)

    async def close(self) -> None:
        """排空所有子 scope 和资源；关闭错误不会被吞掉或转换成成功。"""
        with self._state_lock:
            if self._state == self._CLOSED:
                return
            if self._state != self._CLOSING:
                self._state = self._CLOSING
                # 关闭操作属于 scope；任一等待者被取消都不能遗弃其它资源。
                self._close_task = asyncio.create_task(
                    self._close_owned(), name=f"{self._name}:close",
                )
                self._close_task.add_done_callback(self._report_cleanup_failure)
            closing = self._close_task
        if closing is None:
            raise RuntimeError("LifetimeScope closing 缺少 close task")
        await asyncio.shield(closing)

    async def _close_owned(self) -> None:
        errors: list[Exception] = []
        with self._state_lock:
            tasks = tuple(self._tasks.items())
            children = tuple(reversed(self._children.values()))
        for task, _label in tasks:
            if not task.done():
                task.cancel()
        outcomes = await asyncio.gather(
            *(task for task, _label in tasks),
            *(child.close() for child in children),
            return_exceptions=True,
        )
        for (_task, label), outcome in zip(tasks, outcomes[:len(tasks)], strict=True):
            if isinstance(outcome, Exception):
                error = RuntimeError(
                    f"LifetimeScope 后台任务失败: scope={self._name} task={label}"
                )
                error.__cause__ = outcome
                errors.append(error)
        for child, outcome in zip(children, outcomes[len(tasks):], strict=True):
            if isinstance(outcome, (Exception, asyncio.CancelledError)):
                error = RuntimeError(
                    f"LifetimeScope 子 scope 关闭失败: scope={self._name} child={child.name}"
                )
                error.__cause__ = outcome
                errors.append(error)

        with self._state_lock:
            resources = tuple(self._resources.items())
        for resource_id, resource in reversed(resources):
            try:
                await self._release_resource(resource)
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001
                errors.append(
                    RuntimeError(
                        f"LifetimeScope 释放资源失败: scope={self._name} "
                        f"resource={resource_id}:{resource.label}"
                    )
                )
                errors[-1].__cause__ = error
                continue
        error_group: ExceptionGroup[Exception] | None = None
        with self._state_lock:
            if errors:
                error_group = ExceptionGroup(
                    f"LifetimeScope close 失败: {self._name}", errors
                )
                self._state = self._CLOSE_FAILED
            else:
                self._state = self._CLOSED
        if error_group is not None:
            raise error_group

        if self._parent is not None:
            self._parent._detach_child(self)

    def _attach_child(self, child: LifetimeScope) -> None:
        with self._state_lock:
            if self._state != self._OPEN:
                raise RuntimeError(
                    f"LifetimeScope {self._name} 不允许创建 child: state={self._state}"
                )
            self._children[id(child)] = child

    def _detach_child(self, child: LifetimeScope) -> None:
        with self._state_lock:
            self._children.pop(id(child), None)

    def _require_open_for_registration(self) -> None:
        if self._state != self._OPEN:
            raise RuntimeError(
                f"LifetimeScope {self._name} 不允许注册或提前释放资源: "
                f"state={self._state}"
            )


__all__ = ["LifetimeHandle", "LifetimeScope", "LifetimeSnapshot", "ReleaseCallback"]
