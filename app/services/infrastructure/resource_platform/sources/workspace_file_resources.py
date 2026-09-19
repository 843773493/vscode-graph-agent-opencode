"""工作区文件来源 owner。

底层 watcher 只负责告诉 owner 哪些路径发生变化；本模块负责经
``SourceReconciler``/``StableSourceReader`` 稳定读取并发布内存快照。
业务 middleware 只读取快照，不在 before_model 中直接访问磁盘，也不
直接调用 reader。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from app.core.path_utils import get_boxteam_home
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
    ResourceObservationChannel,
    ResourceObservationSubscription,
)
from app.services.infrastructure.resource_platform.sources.observed_source import (
    MAX_STABLE_READ_BYTES,
    STABLE_READ_ATTEMPTS,
    ObservedSourceDescriptor,
    ObservedSourceHandle,
    SourceReconciler,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)

logger = logging.getLogger(__name__)

MAX_WORKSPACE_RESOURCE_BYTES = MAX_STABLE_READ_BYTES


@dataclass(frozen=True, slots=True)
class WorkspaceFileResourceSnapshot:
    """来源 owner 发布的安全快照，不暴露给模型的物理 locator。"""

    uri: str
    revision: str | None
    content: str
    available: bool
    error: str | None = None


class WorkspaceFileResourceRegistry:
    """共享工作区文件快照 registry。

    registry 只管理来源事实，不决定事实是否进入某个 thread 的上下文。
    稳定读取协议由 ``observed_source.SourceReconciler`` 承载；本类只负责
    登记 handle、发布轻量观察通知与提供权威内存快照。
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        watch_service: WorkspaceFileWatchService,
        project_root: Path | None = None,
        observation_channel: ResourceObservationChannel | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._project_root = (project_root or Path.cwd()).resolve()
        self._watch_service = watch_service
        self._observation_channel = observation_channel or ResourceObservationChannel()
        bundled_root = self._project_root / "resources" / "skills"
        gateway_root = get_boxteam_home() / "skills"
        roots = sorted(
            {
                root for root in (self._workspace_root, bundled_root, gateway_root)
                if root.is_dir()
            },
            key=lambda path: (len(path.parts), str(path)),
        )
        self._watch_roots = tuple(
            root
            for root in roots
            if not any(
                root.is_relative_to(parent)
                for parent in roots
                if parent != root
            )
        )
        self._reconciler = SourceReconciler()
        self._paths_by_uri: dict[str, Path] = {}
        self._snapshots: dict[str, WorkspaceFileResourceSnapshot] = {}
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    def register_file(self, *, uri: str, path: Path) -> WorkspaceFileResourceSnapshot:
        if not uri or not uri.startswith("boxteam://"):
            raise ValueError("工作区文件来源必须使用 boxteam:// 虚拟 URI")
        resolved_path = path.resolve()
        if not resolved_path.is_file() and resolved_path.exists():
            raise RuntimeError(f"工作区资源路径不是文件: {resolved_path}")
        existing_path = self._paths_by_uri.get(uri)
        if existing_path is not None and existing_path != resolved_path:
            raise ValueError(f"资源 URI 绑定冲突: uri={uri}")
        allowed_root = self._allowed_root_for(resolved_path)
        self._reconciler.bind_handle(
            ObservedSourceHandle(
                descriptor=ObservedSourceDescriptor(
                    source_id=uri,
                    source_kind="file",
                    display_uri=uri,
                    entry_identity=uri,
                ),
                file_path=str(resolved_path),
                allowed_root=str(allowed_root),
            )
        )
        self._paths_by_uri[uri] = resolved_path
        snapshot = self._read_via_reconciler(uri)
        previous = self._snapshots.get(uri)
        self._snapshots[uri] = snapshot
        self._publish_observation(uri, previous, snapshot)
        return snapshot

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def observation_channel(self) -> ResourceObservationChannel:
        """暴露唯一的轻量观察通道；registry 仍是快照事实的唯一发布者。"""
        return self._observation_channel

    def subscribe_observation(
        self,
        *,
        label: str,
    ) -> ResourceObservationSubscription:
        """订阅「已登记来源有新 revision」的轻量通知。

        通知只携带虚拟 URI、revision 和可用性，不携带正文；consumer 必须
        回到本 registry 的内存快照读取权威内容。
        """
        return self._observation_channel.subscribe(label=label)

    def unsubscribe_observation(
        self,
        subscription: ResourceObservationSubscription,
    ) -> bool:
        return self._observation_channel.unsubscribe(subscription)

    def snapshot(self, uri: str) -> WorkspaceFileResourceSnapshot:
        try:
            return self._snapshots[uri]
        except KeyError as error:
            raise KeyError(f"工作区资源尚未注册: uri={uri}") from error


    def refresh(self, uri: str) -> WorkspaceFileResourceSnapshot:
        if uri not in self._paths_by_uri:
            raise KeyError(f"工作区资源尚未注册: uri={uri}")
        snapshot = self._read_via_reconciler(uri)
        previous = self._snapshots.get(uri)
        self._snapshots[uri] = snapshot
        self._publish_observation(uri, previous, snapshot)
        return snapshot

    def _read_via_reconciler(self, uri: str) -> WorkspaceFileResourceSnapshot:
        """经 SourceReconciler 稳定读取并转换为本 registry 的快照类型。

        来源文件尚未创建时保持历史语义:空内容、无 revision、available=True。
        其余读取失败由 reconciler 保留上一份 valid revision 并标记 unavailable;
        本层不二次加工，也不调用 reader 以外的读取路径。
        """
        path = self._paths_by_uri[uri]
        if not path.exists():
            return WorkspaceFileResourceSnapshot(
                uri=uri,
                revision=None,
                content="",
                available=True,
            )
        revision = self._reconciler.reconcile(uri)
        return WorkspaceFileResourceSnapshot(
            uri=uri,
            revision=revision.revision or None,
            content=revision.content,
            available=revision.available,
            error=revision.error,
        )

    def _allowed_root_candidates(self) -> tuple[Path, ...]:
        """当前配置域的允许根候选;按路径深度降序, deepest-match 优先。"""
        bundled_root = (self._project_root / "resources" / "skills").resolve()
        gateway_root = (get_boxteam_home() / "skills").resolve()
        return tuple(
            sorted(
                {self._workspace_root, bundled_root, gateway_root},
                key=lambda root: (len(root.parts), str(root)),
                reverse=True,
            )
        )

    def _allowed_root_for(self, resolved_path: Path) -> Path:
        """解析来源所属的配置允许根;越出全部已知根的来源拒绝登记。

        允许根由配置域决定,不要求目录在 registry 构造时已存在。
        """
        for root in self._allowed_root_candidates():
            if resolved_path.is_relative_to(root):
                return root
        raise ValueError(
            f"工作区文件来源必须位于已知允许根内: {resolved_path}"
        )

    def _publish_observation(
        self,
        uri: str,
        previous: WorkspaceFileResourceSnapshot | None,
        current: WorkspaceFileResourceSnapshot,
    ) -> None:
        """向观察通道发布轻量 change 通知，不携带正文。

        revision 或可用性发生变化才算一次新的事实；不可用时沿用上一个有效
        revision，保证 consumer 能识别「同一 revision 变为不可用」。
        """
        if previous is not None and (
            previous.revision,
            previous.available,
        ) == (current.revision, current.available):
            return
        self._observation_channel.notify(
            ResourceObservation(
                uri=uri,
                revision=current.revision,
                available=current.available,
            )
        )

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("工作区文件来源 registry 不允许重复启动")
        self._ready.clear()
        self._task = asyncio.create_task(
            self._watch_loop(),
            name="boxteam-workspace-file-resources",
        )
        self._task.add_done_callback(lambda _task: self._ready.set())
        await self._ready.wait()
        if self._task.done():
            task = self._task
            self._task = None
            task.result()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _watch_loop(self) -> None:
        iterator: AsyncIterator[WorkspaceFileChangeBatch] = (
            self._watch_service.subscribe_roots(
                self._watch_roots,
                include_internal_paths=True,
            )
        )
        next_batch = asyncio.create_task(anext(iterator))
        try:
            await self._watch_service.wait_until_ready(self._watch_roots)
            self._ready.set()
            while True:
                try:
                    batch = await next_batch
                except StopAsyncIteration:
                    return
                if batch.error is not None:
                    logger.error("工作区文件来源监听失败: %s", batch.error)
                else:
                    if batch.overflow:
                        # overflow 只表示监视事件丢失。已登记来源逐个定点刷新，
                        # 不扫描未登记文件，也不吸收未知目录结构。
                        paths = tuple(self._paths_by_uri.values())
                    else:
                        changed_paths = {
                            Path(change.path).resolve() for change in batch.changes
                        }
                        paths = tuple(
                            path
                            for path in self._paths_by_uri.values()
                            if path in changed_paths
                        )
                    for uri, path in tuple(self._paths_by_uri.items()):
                        if path in paths:
                            self.refresh(uri)
                next_batch = asyncio.create_task(anext(iterator))
        finally:
            if not next_batch.done():
                next_batch.cancel()
                try:
                    await next_batch
                except asyncio.CancelledError:
                    pass
            await iterator.aclose()


__all__ = [
    "MAX_WORKSPACE_RESOURCE_BYTES",
    "STABLE_READ_ATTEMPTS",
    "WorkspaceFileResourceRegistry",
    "WorkspaceFileResourceSnapshot",
]
