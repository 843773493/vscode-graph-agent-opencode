"""Backend关闭等待有界；原owner继续排空并保留未完成的依赖步骤。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from asyncio import timeout
from collections import deque
from contextlib import ExitStack
from typing import TYPE_CHECKING

from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    ATTACHMENT_ROOT_DIRECTORY_NAME,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.workspace_lifecycle import CloseAction

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.container import WorkspaceContainerRegistry
    from app.runtime.model_stream import ModelStreamTransportController
    from app.workspace_lifecycle import WorkspaceLifespan

logger = logging.getLogger(__name__)


class BackendLifespan:
    """全部挂载独立排空后，按原依赖顺序关闭catalog、维护锁和模型传输。"""

    def __init__(self, model_stream: ModelStreamTransportController | None, application: FastAPI) -> None:
        self._application = application
        self.registry: WorkspaceContainerRegistry | None = None
        self.workspaces: list[WorkspaceLifespan] = []
        self._access = ExitStack()
        self._model_stream = model_stream
        self._close_owner: asyncio.Task[None] | None = None
        self._steps: deque[tuple[str, CloseAction]] = deque([
            ("workspaces", self._close_workspaces),
            ("catalog", self._close_registry),
            ("maintenance", self._access.close),
            ("model_stream", self._close_model_stream),
        ])

    @property
    def closed(self) -> bool:
        return not self._steps

    def acquire_access(self) -> None:
        if self.registry is None:
            raise RuntimeError("Backend未装配registry，不能取得维护锁")
        for mount in self.registry.mounted_workspaces.mounts:
            self._access.enter_context(AttachmentMaintenanceLock(
                mount.workspace_root / ".boxteam" / ATTACHMENT_ROOT_DIRECTORY_NAME, exclusive=False,
            ))

    async def close(self) -> None:
        if self.closed:
            return
        if self._close_owner is None or self._close_owner.done():
            self._close_owner = asyncio.create_task(self._close(), name="backend-lifespan:close")
            self._close_owner.add_done_callback(self._report_close_failure)
        close_owner = self._close_owner
        # 只限制本次观察；取消或到期不能取消仍在写入/核实的原关闭owner。
        deadline = timeout(30)
        try:
            async with deadline:
                await asyncio.shield(close_owner)
        except TimeoutError as error:
            if not deadline.expired():
                raise
            if close_owner.done():
                await close_owner
                return
            pending = ",".join(
                f"{workspace.container.workspace_id}:{workspace.pending_close_stage}"
                for workspace in self.workspaces if not workspace.closed
            )
            raise TimeoutError(
                f"Backend关闭等待超过30秒: stage={self._steps[0][0]} "
                f"pending_workspaces=[{pending}]；原关闭owner与未释放依赖继续保留"
            ) from error

    def _report_close_failure(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException as error:
            # 等待者可能已经到期；迟到失败仍必须明确记录并由同入口重试。
            logger.error("Backend关闭owner失败，保留原步骤及依赖",
                         exc_info=(type(error), error, error.__traceback__))

    async def _close(self) -> None:
        while self._steps:
            _stage, action = self._steps[0]
            result = action()
            if inspect.isawaitable(result):
                await result
            self._steps.popleft()
        self._application.state.workspace_container_registry = None
        self._application.state.workspace_lifespan = None

    async def _close_workspaces(self) -> None:
        # 按挂载逆序发起，各自的依赖链仍由创建它的原Workspace任务串行关闭。
        results = await asyncio.gather(
            *(workspace.close() for workspace in reversed(self.workspaces)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("Backend关闭未完成，保留catalog与维护锁", errors)

    def _close_registry(self) -> None:
        # TODO(5.4)：同步关闭阻塞、线程池及生产进程终止协调仍须单独核实。
        if self.registry is not None:
            self.registry.close()

    async def _close_model_stream(self) -> None:
        if self._model_stream is not None:
            await self._model_stream.aclose()
