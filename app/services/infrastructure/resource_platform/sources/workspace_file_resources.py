"""工作区文件来源 owner。

底层 watcher 只负责告诉 owner 哪些路径发生变化；本模块负责经
``SourceReconciler``/``StableSourceReader`` 稳定读取并发布内存快照。
业务 middleware 只读取快照，不在 before_model 中直接访问磁盘，也不
直接调用 reader。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.core.phase_observer import observe_phase
from app.core.resource_uri import parse_resource_uri, skill_display_uri
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
    ResourceObservationChannel,
    ResourceObservationSubscription,
)
from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from app.services.infrastructure.resource_platform.sources.observed_source import (
    MAX_STABLE_READ_BYTES,
    STABLE_READ_ATTEMPTS,
    ObservedSourceDescriptor,
    ObservedSourceHandle,
    ObservedSourceRevision,
    SourceReconciler,
)
from app.services.infrastructure.workspace_file_watch_service import (
    FILE_WATCH_QUEUE_SIZE,
    WorkspaceFileChange,
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
    exists: bool = True


@dataclass(frozen=True, slots=True)
class WorkspaceSkillDirectoryEntry:
    name: str
    path: Path
    uri: str
    source_id: str
    entry_identity: str


@dataclass(frozen=True, slots=True)
class WorkspaceSkillDirectorySnapshot:
    directory_id: str
    revision: str
    entries: tuple[WorkspaceSkillDirectoryEntry, ...]
    available: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _SkillDirectoryHandle:
    root: Path
    scope: str
    scope_id: str | None
    declared_entries: tuple[str, ...] | None


@dataclass(slots=True, eq=False)
class FileSourceObservation:
    """一次观察代次；候选只建立订阅，激活后才能更新原 registry 的事实。"""

    policy: SourceUpdatePolicy
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    activated: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    events: asyncio.Queue[WorkspaceFileChangeBatch] = field(default_factory=lambda: asyncio.Queue(maxsize=FILE_WATCH_QUEUE_SIZE))
    subscriptions: dict[tuple[Path, bool, int | None], asyncio.Task[None]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    next_poll_at: float | None = None

    def check_running(self) -> None:
        if self.task is None:
            raise RuntimeError("文件来源观察任务尚未启动")
        if self.task.done():
            self.task.result()
            raise RuntimeError("文件来源观察任务意外退出")


class WorkspaceFileResourceRegistry:
    """共享工作区文件快照 registry。

    registry 只管理来源事实，不决定事实是否进入某个 thread 的上下文。
    稳定读取协议由 ``observed_source.SourceReconciler`` 承载；本类只负责
    登记 handle、发布轻量观察通知与提供权威内存快照。
    """

    def __init__(
        self,
        *,
        workspace_root: Path | None,
        watch_service: WorkspaceFileWatchService,
        project_root: Path | None = None,
        observation_channel: ResourceObservationChannel | None = None,
        gateway_skills_root: Path | None = None,
        update_policy: SourceUpdatePolicy | None = None,
    ) -> None:
        if (workspace_root is None) == (gateway_skills_root is None):
            raise ValueError("文件 owner 必须且只能绑定 Workspace 或 Gateway 配置域")
        self._workspace_root = workspace_root.resolve() if workspace_root is not None else None
        self._project_root = (project_root or Path.cwd()).resolve()
        self._watch_service = watch_service
        self._observation_channel = observation_channel or ResourceObservationChannel()
        self._gateway_skills_root = gateway_skills_root.absolute() if gateway_skills_root is not None else None
        bundled_root = self._project_root / "resources" / "skills"
        self._bundled_skills_root = bundled_root.absolute()
        # 工作区/SKILL 发行资源保持递归监听；Gateway 全局根使用后续的分层浅监听。
        roots = tuple(sorted(
            {
                root for root in (self._workspace_root, self._bundled_skills_root)
                if self._workspace_root is not None and root is not None and root.is_dir()
            },
            key=lambda path: (len(path.parts), str(path)),
        ))
        self._watch_roots = tuple(
            root for root in roots
            if not any(root != parent and root.is_relative_to(parent) for parent in roots)
        )
        self._reconciler = SourceReconciler()
        self._file_handles_by_uri: dict[str, ObservedSourceHandle] = {}
        self._snapshots: dict[str, WorkspaceFileResourceSnapshot] = {}
        self._skill_directories: dict[str, _SkillDirectoryHandle] = {}
        self._skill_directory_snapshots: dict[str, WorkspaceSkillDirectorySnapshot] = {}
        self._skill_entry_watch_roots: dict[str, tuple[Path, ...]] = {}
        self._initial_policy = update_policy or SourceUpdatePolicy()
        self._observation: FileSourceObservation | None = None
        self._observations: set[FileSourceObservation] = set()
        self._watch_filter = self._is_relevant_change
        self._watch_unavailable = False

    def register_file(
        self,
        *,
        uri: str,
        path: Path,
        source_id: str,
        entry_identity: str,
        optional: bool = False,
    ) -> WorkspaceFileResourceSnapshot:
        parse_resource_uri(uri)
        existing_handle = self._file_handles_by_uri.get(uri)
        if existing_handle is not None:
            descriptor = existing_handle.descriptor
            if (descriptor.source_id, descriptor.entry_identity) != (source_id, entry_identity):
                raise ValueError(f"资源 URI identity 绑定冲突: uri={uri}")
            existing_path = self._handle_path(existing_handle)
            if path == existing_path or path.absolute() == existing_path:
                resolved_path = existing_path
                snapshot = self._snapshots.get(uri)
                if snapshot is not None and snapshot.revision is not None:
                    if existing_handle.allow_missing != optional:
                        raise ValueError(f"来源 handle 绑定冲突: source_id={source_id}")
                    return snapshot
            else:
                resolved_path = path.resolve()
                if existing_handle.file_path != str(resolved_path):
                    raise ValueError(f"资源 URI 绑定冲突: uri={uri}")
        else:
            resolved_path = path.resolve()

        if not resolved_path.is_file() and resolved_path.exists():
            raise RuntimeError(f"工作区资源路径不是文件: uri={uri}")
        if any(
            registered_uri != uri
            and handle.descriptor.entry_identity == entry_identity
            for registered_uri, handle in tuple(self._file_handles_by_uri.items())
        ):
            raise ValueError(f"文件来源 entry_identity 冲突: entry_identity={entry_identity}")
        allowed_root = self._allowed_root_for(resolved_path, reference=uri)
        handle = ObservedSourceHandle(
            descriptor=ObservedSourceDescriptor(
                source_id=source_id,
                source_kind="file",
                display_uri=uri,
                entry_identity=entry_identity,
            ),
            file_path=str(resolved_path),
            allowed_root=str(allowed_root),
            allow_missing=optional,
        )
        self._reconciler.bind_handle(handle)
        self._file_handles_by_uri[uri] = handle
        snapshot = self._read_via_reconciler(uri)
        previous = self._snapshots.get(uri)
        self._snapshots[uri] = snapshot
        self._publish_observation(uri, previous, snapshot)
        return snapshot

    def source_descriptor_for_uri(self, uri: str) -> ObservedSourceDescriptor:
        """返回已登记 URI 对应的 owner identity，不从 locator 推导身份。"""
        try:
            return self._file_handles_by_uri[uri].descriptor
        except KeyError as error:
            raise KeyError(f"工作区资源尚未注册: uri={uri}") from error

    def source_id_for_uri(self, uri: str) -> str:
        return self.source_descriptor_for_uri(uri).source_id

    @property
    def workspace_root(self) -> Path:
        if self._workspace_root is None:
            raise RuntimeError("Gateway 文件 owner 没有 Workspace 根目录")
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

    def observed_revision(self, source_id: str) -> ObservedSourceRevision:
        """向语义图提供原 reconciler 的 accepted 值，不暴露读取 handle。"""
        return self._reconciler.observed_revision(source_id)

    def register_skill_directory(
        self, *, root: Path, scope: str, scope_id: str | None,
        declared_entries: tuple[str, ...] | None = None,
    ) -> WorkspaceSkillDirectorySnapshot:
        """登记固定一层边界；后续发现仅由共享 watcher 的 dirty 事件触发。"""
        expected_roots = (
            {"workspace": self._workspace_root / ".boxteam" / "skills", "inline": self._bundled_skills_root}
            if self._workspace_root is not None
            else {"gateway": self._gateway_skills_root}
        )
        expected_root = expected_roots.get(scope)
        if expected_root is None or root.absolute() != expected_root.absolute():
            raise ValueError(f"Skill 目录必须是已知固定根: scope={scope}")
        identity = json.dumps(
            [scope, scope_id, declared_entries], ensure_ascii=False, separators=(",", ":"),
        )
        directory_id = "skill-directory:" + hashlib.sha256(identity.encode()).hexdigest()
        handle = _SkillDirectoryHandle(root.absolute(), scope, scope_id, declared_entries)
        existing = self._skill_directories.get(directory_id)
        if existing is not None:
            if existing != handle:
                raise ValueError(f"Skill 目录绑定冲突: directory_id={directory_id}")
            return self.skill_directory_snapshot(directory_id)
        snapshot = self._reconcile_skill_directory(directory_id, handle)
        # 初次稳定读取完成后才让应用 loop 看见登记边界，避免 worker 编译窗口的半成品。
        self._skill_directories[directory_id] = handle
        for observation in tuple(self._observations):
            observation.changed.set()
        return snapshot

    def skill_directory_snapshot(self, directory_id: str) -> WorkspaceSkillDirectorySnapshot:
        """只读取已接受的目录事实，不触碰文件系统。"""
        return self._skill_directory_snapshots[directory_id]

    def _reconcile_skill_directory(
        self, directory_id: str, handle: _SkillDirectoryHandle,
    ) -> WorkspaceSkillDirectorySnapshot:
        previous = self._skill_directory_snapshots.get(directory_id)
        entries: list[WorkspaceSkillDirectoryEntry] = []
        entry_watch_roots: list[Path] = []
        error = None
        try:
            root = handle.root
            reference = f"scope={handle.scope}, scope_id={handle.scope_id}"
            if root.is_symlink():
                raise RuntimeError(f"Skill 根目录不允许 symlink: {reference}")
            # 枚举前复用文件 owner 的域边界；中间段的内部链接可用，越界链接禁止跟随。
            allowed_root = self._allowed_root_for(root, reference=reference)
            if not root.resolve().is_relative_to(allowed_root):
                raise ValueError(f"Skill 固定根越出已知允许根: {reference}")
            if root.exists() and not root.is_dir():
                raise RuntimeError(f"Skill 根路径不是目录: {reference}")
            if handle.declared_entries is not None:
                if not root.is_dir():
                    raise RuntimeError(f"发行包内置 Skill 根目录不存在: {reference}")
                candidates = tuple(root / name for name in handle.declared_entries)
            else:
                candidates = tuple(sorted(root.iterdir())) if root.is_dir() else ()
            for entry_dir in candidates:
                entry_reference = f"{reference}, skill={entry_dir.name}"
                if entry_dir.is_symlink():
                    raise RuntimeError(f"Skill entry 不允许 symlink: {entry_reference}")
                if not entry_dir.is_dir():
                    if handle.declared_entries is not None:
                        raise RuntimeError(f"发行包内置 Skill 组目录不存在: {entry_reference}")
                    continue
                entry_watch_roots.append(entry_dir.absolute())
                path = entry_dir / "SKILL.md"
                if path.is_symlink():
                    raise RuntimeError(f"SKILL.md 不允许 symlink: {entry_reference}")
                if not path.is_file():
                    if handle.declared_entries is not None:
                        raise RuntimeError(f"发行包内置 Skill 组缺少 SKILL.md: {entry_reference}")
                    continue
                if handle.scope_id is None:
                    raise RuntimeError("gateway scope 的 scope_id 需要真实的 gateway_id，当前来源 owner 尚未绑定")
                uri = skill_display_uri(
                    scope=handle.scope, scope_id=handle.scope_id, skill_name=entry_dir.name,
                )
                source_id = f"skill-source:{handle.scope}:{entry_dir.name}"
                entry_identity = f"skill-entry:{handle.scope}:{entry_dir.name}"
                if uri in self._file_handles_by_uri:
                    descriptor = self.source_descriptor_for_uri(uri)
                    if (descriptor.source_id, descriptor.entry_identity) != (source_id, entry_identity):
                        raise ValueError(f"Skill 文件来源 owner identity 绑定冲突: uri={uri}")
                    self.refresh(uri)
                else:
                    self.register_file(
                        uri=uri,
                        path=path,
                        source_id=source_id,
                        entry_identity=entry_identity,
                    )
                entries.append(WorkspaceSkillDirectoryEntry(
                    entry_dir.name, path, uri, source_id, entry_identity,
                ))
        except OSError as failure:
            error = (
                f"Skill 固定目录读取失败: scope={handle.scope}, scope_id={handle.scope_id}, "
                f"error={type(failure).__name__}, errno={failure.errno}"
            )
            logger.exception("Skill 固定目录读取失败: directory_id=%s", directory_id)
        except (RuntimeError, ValueError) as failure:
            error = str(failure)
            logger.error("Skill 固定目录对账失败: directory_id=%s error=%s", directory_id, error)
        self._skill_entry_watch_roots[directory_id] = tuple(entry_watch_roots)
        if error is None and previous is not None:
            current_uris = {entry.uri for entry in entries}
            for entry in previous.entries:
                if entry.uri not in current_uris:
                    self.refresh(entry.uri)
        revision = "sha256:" + hashlib.sha256(json.dumps(
            [(entry.name, entry.uri) for entry in entries], ensure_ascii=False,
            separators=(",", ":"),
        ).encode()).hexdigest()
        snapshot = WorkspaceSkillDirectorySnapshot(
            directory_id, revision, tuple(entries), error is None, error,
        )
        self._skill_directory_snapshots[directory_id] = snapshot
        if snapshot != previous:
            self._observation_channel.notify(ResourceObservation(
                uri=None, directory_id=directory_id, revision=revision, available=snapshot.available,
            ))
        return snapshot


    def refresh(self, uri: str) -> WorkspaceFileResourceSnapshot:
        if uri not in self._file_handles_by_uri:
            raise KeyError(f"工作区资源尚未注册: uri={uri}")
        snapshot = self._read_via_reconciler(uri)
        previous = self._snapshots.get(uri)
        self._snapshots[uri] = snapshot
        self._publish_observation(uri, previous, snapshot)
        return snapshot

    def _read_via_reconciler(self, uri: str) -> WorkspaceFileResourceSnapshot:
        """消费原来源 owner 的已观察事实；可选缺失与空文件保持独立 revision。"""
        revision = self._reconciler.reconcile(self.source_id_for_uri(uri))
        return WorkspaceFileResourceSnapshot(
            uri=uri,
            revision=revision.revision or None,
            content=revision.content,
            available=revision.available,
            error=revision.error,
            exists=revision.exists,
        )

    def _allowed_root_candidates(self) -> tuple[Path, ...]:
        """当前配置域的允许根候选;按路径深度降序, deepest-match 优先。"""
        if self._workspace_root is not None:
            roots = {self._workspace_root, self._bundled_skills_root.resolve()}
        elif self._gateway_skills_root is not None:
            roots = {self._gateway_skills_root.resolve()}
        else:
            raise RuntimeError("文件来源 owner 未绑定配置域")
        return tuple(
            sorted(
                roots,
                key=lambda root: (len(root.parts), str(root)),
                reverse=True,
            )
        )

    def _allowed_root_for(self, resolved_path: Path, *, reference: str) -> Path:
        """解析来源所属的配置允许根;越出全部已知根的来源拒绝登记。

        允许根由配置域决定,不要求目录在 registry 构造时已存在。
        """
        for root in self._allowed_root_candidates():
            if resolved_path.is_relative_to(root):
                return root
        raise ValueError(
            f"工作区文件来源必须位于已知允许根内: {reference}"
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
        if self._observation is not None:
            raise RuntimeError("工作区文件来源 registry 不允许重复启动")
        observation = await self.prepare_update_policy(self._initial_policy)
        self.activate_update_policy(observation)

    async def prepare_update_policy(self, policy: SourceUpdatePolicy) -> FileSourceObservation:
        """候选订阅 ready 后才交付；失败只回收候选，不改变当前快照与任务。"""
        observation = FileSourceObservation(policy)
        self._observations.add(observation)
        task = asyncio.create_task(self._watch_loop(observation), name="boxteam-workspace-file-resources")
        observation.task = task
        task.add_done_callback(lambda _task: observation.ready.set())
        try:
            await observation.ready.wait()
            observation.check_running()
        except BaseException:
            await self.close_observation(observation)
            raise
        return observation

    def activate_update_policy(self, observation: FileSourceObservation) -> None:
        if observation not in self._observations:
            raise RuntimeError("文件观察候选不属于当前 owner")
        observation.check_running()
        # 来源读取是同步的；与代次切换在同一事件循环步内完成，不接受旧代次迟到事件。
        self.reconcile()
        if self._observation is not None:
            self._observation.activated.clear()
        self._observation = observation
        interval = observation.policy.poll_seconds
        observation.next_poll_at = asyncio.get_running_loop().time() + interval if interval is not None else None
        observation.activated.set()

    def reconcile(self) -> None:
        """监听、周期、初始和手动更新共用同一来源读取及发布链路。"""
        skill_uris = {entry.uri for directory in self._skill_directory_snapshots.values() for entry in directory.entries}
        for uri in tuple(self._file_handles_by_uri):
            if uri not in skill_uris:
                self.refresh(uri)
        for directory_id, handle in tuple(self._skill_directories.items()):
            self._reconcile_skill_directory(directory_id, handle)
        self._watch_unavailable = False

    def check_running(self) -> None:
        if self._observation is None:
            raise RuntimeError("文件来源观察任务尚未启动")
        self._observation.check_running()

    @property
    def observation(self) -> FileSourceObservation:
        self.check_running()
        assert self._observation is not None
        return self._observation

    def is_current_observation(self, observation: FileSourceObservation) -> bool:
        return self._observation is observation

    async def close_observation(self, observation: FileSourceObservation) -> None:
        task = observation.task
        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            self._observations.discard(observation)
            if self._observation is observation:
                self._observation = None

    async def stop(self) -> None:
        results = await asyncio.gather(*(self.close_observation(item) for item in tuple(self._observations)), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("文件观察任务关闭失败", errors)

    async def _watch_loop(self, observation: FileSourceObservation) -> None:
        try:
            await self._sync_watch_subscriptions(observation, publish=False)
            observation.ready.set()
            while True:
                await observation.activated.wait()
                if observation is not self._observation:
                    continue
                await self._sync_watch_subscriptions(observation)
                batch = await self._next_watch_event(observation)
                if observation is not self._observation:
                    continue
                if batch is None:
                    self.reconcile()
                else:
                    if batch.error is not None:
                        logger.error("工作区文件来源监听失败: %s", batch.error)
                        self._watch_unavailable = True
                        for uri, previous in tuple(self._snapshots.items()):
                            current = replace(previous, available=False, error=batch.error)
                            self._snapshots[uri] = current
                            self._publish_observation(uri, previous, current)
                        for directory_id, previous in tuple(self._skill_directory_snapshots.items()):
                            self._skill_directory_snapshots[directory_id] = replace(
                                previous, available=False, error=batch.error,
                            )
                            self._observation_channel.notify(ResourceObservation(
                                uri=None, directory_id=directory_id, revision=previous.revision, available=False,
                            ))
                    else:
                        changed_paths = {Path(change.path) for change in batch.changes}
                        dirty_directories = tuple(
                            directory_id for directory_id, handle in tuple(self._skill_directories.items())
                            if batch.overflow or self._watch_unavailable or any(
                                handle.root.is_relative_to(path)
                                or (path.is_relative_to(handle.root) and (
                                    len(path.relative_to(handle.root).parts) <= 1
                                    or (len(path.relative_to(handle.root).parts) == 2 and path.name == "SKILL.md")
                                ))
                                for path in changed_paths
                            )
                        )
                        skill_uris = {
                            entry.uri for directory_id in dirty_directories
                            for entry in self.skill_directory_snapshot(directory_id).entries
                        }
                        reconcile_all_files = batch.overflow or self._watch_unavailable
                        for uri, handle in tuple(self._file_handles_by_uri.items()):
                            if uri in skill_uris:
                                continue
                            path = self._handle_path(handle)
                            if reconcile_all_files or any(
                                path.is_relative_to(changed) for changed in changed_paths
                            ):
                                self.refresh(uri)
                        for directory_id in dirty_directories:
                            if observation is not self._observation:
                                break
                            directory = self._reconcile_skill_directory(directory_id, self._skill_directories[directory_id])
                            for entry in directory.entries:
                                snapshot = self.snapshot(entry.uri)
                                await observe_phase(entry.uri, "workspace.source.watch.published", details={
                                    "revision": snapshot.revision, "available": snapshot.available,
                                    "registry_object_id": id(self),
                                    "subscriber_ids": self._observation_channel.subscriber_ids,
                                })
                        if observation is self._observation:
                            self._watch_unavailable = False
        finally:
            subscriptions = tuple(observation.subscriptions.values())
            observation.subscriptions.clear()
            for task in subscriptions:
                if not task.done():
                    task.cancel()
            if subscriptions:
                await asyncio.gather(*subscriptions, return_exceptions=True)

    async def _next_watch_event(self, observation: FileSourceObservation) -> WorkspaceFileChangeBatch | None:
        now = asyncio.get_running_loop().time()
        deadline = observation.next_poll_at
        timeout = max(0, deadline - now) if deadline is not None else None
        batch_task = asyncio.create_task(observation.events.get())
        config_task = asyncio.create_task(observation.changed.wait())
        try:
            done, _pending = await asyncio.wait(
                (batch_task, config_task), timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
            )
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                interval = observation.policy.poll_seconds
                if interval is None:
                    raise RuntimeError("文件观察周期与策略不一致")
                observation.next_poll_at = asyncio.get_running_loop().time() + interval
                return None
            if config_task in done:
                observation.changed.clear()
                return batch_task.result() if batch_task in done else None
            return batch_task.result()
        finally:
            pending = tuple(task for task in (batch_task, config_task) if not task.done())
            for task in pending:
                task.cancel()
            await asyncio.gather(batch_task, config_task, return_exceptions=True)

    async def _sync_watch_subscriptions(self, observation: FileSourceObservation, *, publish: bool = True) -> None:
        """按当前安全边界增删监听；新增目录 ready 后立即重新对账。"""
        while True:
            desired = self._desired_watch_roots() if observation.policy.uses_events else set()
            desired_keys = {
                self._subscription_key(root, recursive)
                for root, recursive in desired
            }
            added: list[Path] = []
            for root, recursive in sorted(
                desired, key=lambda item: (len(item[0].parts), str(item[0]), item[1]),
            ):
                key = self._subscription_key(root, recursive)
                task = observation.subscriptions.get(key)
                if task is not None and not task.done():
                    continue
                if task is not None:
                    task.result()
                task = asyncio.create_task(
                    self._pump_watch_root(root, recursive, observation),
                    name=f"boxteam-resource-watch:{root}",
                )
                observation.subscriptions[key] = task
                await self._watch_service.wait_until_ready(
                    (root,), recursive=recursive, change_filter=self._watch_filter,
                )
                if task.done():
                    task.result()
                added.append(root)

            stale_keys = set(observation.subscriptions) - desired_keys
            stale_tasks = [observation.subscriptions.pop(key) for key in stale_keys]
            for task in stale_tasks:
                if not task.done():
                    task.cancel()
            if stale_tasks:
                await asyncio.gather(*stale_tasks, return_exceptions=True)

            if not added:
                return
            affected = tuple(
                (directory_id, handle)
                for directory_id, handle in tuple(self._skill_directories.items())
                if any(
                    root == handle.root
                    or root.is_relative_to(handle.root)
                    or handle.root.is_relative_to(root)
                    for root in added
                )
            )
            if publish and observation is self._observation:
                for directory_id, handle in affected:
                    self._reconcile_skill_directory(directory_id, handle)
            else:
                return

    async def _pump_watch_root(self, root: Path, recursive: bool, observation: FileSourceObservation) -> None:
        iterator: AsyncGenerator[WorkspaceFileChangeBatch, None] = self._watch_service.subscribe_roots(
            (root,),
            include_internal_paths=True,
            recursive=recursive,
            change_filter=self._watch_filter,
        )
        next_batch = asyncio.ensure_future(anext(iterator))
        try:
            await self._watch_service.wait_until_ready(
                (root,), recursive=recursive, change_filter=self._watch_filter,
            )
            while True:
                try:
                    batch = await next_batch
                except StopAsyncIteration:
                    return
                await observation.events.put(batch)
                next_batch = asyncio.ensure_future(anext(iterator))
        finally:
            if not next_batch.done():
                next_batch.cancel()
                try:
                    await next_batch
                except asyncio.CancelledError:
                    pass
            await iterator.aclose()

    def _desired_watch_roots(self) -> set[tuple[Path, bool]]:
        roots = {(root, True) for root in self._watch_roots if root.is_dir()}
        gateway_directories = tuple(
            (directory_id, handle)
            for directory_id, handle in tuple(self._skill_directories.items())
            if handle.scope == "gateway"
        )
        gateway_root = self._gateway_skills_root
        if gateway_directories and gateway_root is not None and gateway_root.parent.is_dir():
            roots.add((gateway_root.parent, False))
        for directory_id, handle in gateway_directories:
            root = handle.root
            if root.is_dir() and not root.is_symlink():
                roots.add((root, False))
                roots.update(
                    (entry_root, False)
                    for entry_root in self._skill_entry_watch_roots.get(directory_id, ())
                    if entry_root.is_dir() and not entry_root.is_symlink()
                )
        return roots

    def _subscription_key(self, root: Path, recursive: bool) -> tuple[Path, bool, int | None]:
        return root.resolve(), recursive, id(self._watch_filter)

    def _is_relevant_change(self, change: WorkspaceFileChange) -> bool:
        """在 watcher 入队/溢出判断前屏蔽与已登记来源无关的文件事件。"""
        changed = Path(change.path)
        # 编译线程可同时登记来源；先冻结 handle 集合，再执行可能跨线程交错的路径读取。
        if any(
            path == changed or path.is_relative_to(changed)
            for path in (self._handle_path(handle) for handle in tuple(self._file_handles_by_uri.values()))
        ):
            return True
        for handle in tuple(self._skill_directories.values()):
            root = handle.root
            if root == changed or root.is_relative_to(changed):
                return True
            if not changed.is_relative_to(root):
                continue
            depth = len(changed.relative_to(root).parts)
            if depth == 1 or (depth == 2 and changed.name == "SKILL.md"):
                return True
        return False

    @staticmethod
    def _handle_path(handle: ObservedSourceHandle) -> Path:
        if handle.file_path is None:
            raise RuntimeError("文件来源 handle 缺少 file_path")
        return Path(handle.file_path)


__all__ = [
    "MAX_WORKSPACE_RESOURCE_BYTES",
    "STABLE_READ_ATTEMPTS",
    "WorkspaceFileResourceRegistry",
    "WorkspaceFileResourceSnapshot",
]
