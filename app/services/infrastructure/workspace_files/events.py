"""把原文件 watcher 与同 owner 的持久 terminal outbox 合并为一条 SSE。"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakKeyDictionary

from app.core.resource_uri import file_uri_child, parse_resource_uri
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryKeyDTO,
    WorkspaceDirectoryRevisionDTO,
    WorkspaceFileListDTO,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChange,
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_files.operations import (
    WorkspaceFileOperations,
)


@dataclass
class _ObservationBatchCache:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    results: OrderedDict[
        int,
        tuple[WorkspaceFileChangeBatch, tuple[WorkspaceDirectoryRevisionDTO, ...]],
    ] = field(default_factory=OrderedDict)


_observation_batch_caches: WeakKeyDictionary[
    WorkspaceFileOperations, _ObservationBatchCache
] = WeakKeyDictionary()


async def _observe_batch_once(
    owner: WorkspaceFileOperations,
    batch: WorkspaceFileChangeBatch,
    observed: list[tuple[WorkspaceDirectoryKeyDTO, Path]],
) -> tuple[WorkspaceDirectoryRevisionDTO, ...]:
    cache = _observation_batch_caches.get(owner)
    if cache is None:
        cache = _ObservationBatchCache()
        _observation_batch_caches[owner] = cache
    batch_identity = id(batch)
    async with cache.lock:
        cached = cache.results.get(batch_identity)
        if cached is not None and cached[0] is batch:
            cache.results.move_to_end(batch_identity)
            return cached[1]
        directories = tuple(await asyncio.to_thread(owner.observe_directories, observed))
        cache.results[batch_identity] = (batch, directories)
        while len(cache.results) > 64:
            cache.results.popitem(last=False)
        return directories


async def file_events(owner: WorkspaceFileOperations, watcher: WorkspaceFileWatchService,
                      roots: tuple[Path, ...], after_event_id: int | None,
                      references: dict[Path, str]) -> AsyncGenerator[WorkspaceFileChangeBatch, None]:
    def subscription_root(path: Path) -> Path | None:
        matches = [root for root in references if path == root or path.is_relative_to(root)]
        if not matches:
            return None
        root = max(matches, key=lambda value: len(value.parts))
        return None if owner.paths.is_private(path, root_uri=references[root]) else root

    def reference(path: Path) -> str:
        root = subscription_root(path)
        if root is None:
            raise ValueError("文件观察事件不属于公开订阅根")
        return file_uri_child(references[root], *path.relative_to(root).parts)
    latest = owner.store.latest_event_id()
    cursor = latest if after_event_id is None else after_event_id
    if cursor > latest:
        raise RuntimeError(f"原文件事件序号超过当前 owner 的持久 high-water: after={cursor}, latest={latest}")
    recent_staging_roots: dict[Path, float] = {}

    def remember_operation_staging_roots(operation_id: str) -> None:
        record = owner.store.get(operation_id, "local")
        expires_at = time.monotonic() + 30
        for index in range(len(record.steps)):
            recent_staging_roots[owner._stage_root(record, index)] = expires_at

    def private_staging_roots() -> set[Path]:
        now = time.monotonic()
        for root, expires_at in tuple(recent_staging_roots.items()):
            if expires_at <= now:
                del recent_staging_roots[root]
        active_roots = owner.staging_roots()
        for root in active_roots:
            recent_staging_roots[root] = now + 30
        return active_roots | set(recent_staging_roots)

    def is_private_staging_change(path: Path, roots: set[Path]) -> bool:
        return path in roots or any(parent in roots for parent in path.parents)

    include_attachments = any(parse_resource_uri(uri).kind == "attachments" for uri in references.values())
    # 底层仍复用原工作区 watcher；只开放显式附件根，不能公布其它私有存储。
    async with aclosing(watcher.subscribe_roots(roots, include_internal_paths=include_attachments)) as source:
        next_batch = asyncio.create_task(anext(source))
        pending_batch: WorkspaceFileChangeBatch | None = None
        try:
            while True:
                for event in owner.store.events(cursor):
                    cursor = event["event_id"]
                    operation_id = event.get("operation_id")
                    if isinstance(operation_id, str):
                        remember_operation_staging_roots(operation_id)
                    directories = []
                    for value in event["directories"]:
                        revision = WorkspaceDirectoryRevisionDTO.model_validate(value)
                        key = revision.directory_key
                        path = await asyncio.to_thread(owner.paths.native, key.path, materialize=False)
                        if subscription_root(path) is not None:
                            directories.append(revision)
                    if directories:
                        keys = {value.directory_key for value in directories}
                        snapshots = tuple(WorkspaceFileListDTO.model_validate(value) for value in event["snapshots"]
                                          if WorkspaceFileListDTO.model_validate(value).directory_key in keys)
                        yield WorkspaceFileChangeBatch(directories=tuple(directories), event_id=cursor, snapshots=snapshots)
                if pending_batch is None:
                    done, _pending = await asyncio.wait({next_batch}, timeout=0.2)
                    if not done:
                        continue
                    try:
                        pending_batch = next_batch.result()
                    except StopAsyncIteration:
                        return
                    next_batch = asyncio.create_task(anext(source))
                    continue
                batch, pending_batch = pending_batch, None
                if batch.error is not None:
                    yield batch
                    return
                changes = batch.changes
                if not batch.overflow:
                    private_roots = private_staging_roots()
                    changes = tuple(
                        change for change in changes
                        if (not is_private_staging_change(Path(change.path), private_roots)
                            and subscription_root(Path(change.path)) is not None)
                    )
                    if not changes:
                        continue
                observed = []
                if not batch.overflow:
                    parents = {Path(change.path).parent for change in changes}
                    removed = {Path(change.path) for change in changes if change.kind == "delete"}
                    for key, real in owner.observed_directories(parents, removed=removed):
                        path = real
                        if path in parents or any(path == value or path.is_relative_to(value) for value in removed):
                            observed.append((key, path))
                directories = await _observe_batch_once(owner, batch, observed)
                changes = tuple(WorkspaceFileChange(kind=change.kind, path=reference(Path(change.path))) for change in changes)
                yield WorkspaceFileChangeBatch(changes=changes, overflow=batch.overflow,
                                                directories=tuple(directories), event_id=cursor,
                                                invalidated_roots=tuple(dict.fromkeys(references.values())) if batch.overflow else ())
        finally:
            next_batch.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await next_batch
