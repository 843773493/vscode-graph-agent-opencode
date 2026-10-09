"""把原文件 watcher 与同 owner 的持久 terminal outbox 合并为一条 SSE。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from pathlib import Path

from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryRevisionDTO,
    WorkspaceFileListDTO,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_files.operations import (
    WorkspaceFileOperations,
)


async def file_events(owner: WorkspaceFileOperations, watcher: WorkspaceFileWatchService,
                      roots: tuple[Path, ...], after_event_id: int | None) -> AsyncIterator[WorkspaceFileChangeBatch]:
    latest = owner.store.latest_event_id()
    cursor = latest if after_event_id is None else after_event_id
    if cursor > latest:
        raise RuntimeError(f"原文件事件序号超过当前 owner 的持久 high-water: after={cursor}, latest={latest}")
    async with aclosing(watcher.subscribe_roots(roots)) as source:
        next_batch = asyncio.create_task(anext(source))
        try:
            while True:
                for event in owner.store.events(cursor):
                    cursor = event["event_id"]
                    directories = []
                    for value in event["directories"]:
                        revision = WorkspaceDirectoryRevisionDTO.model_validate(value)
                        key = revision.directory_key
                        path = Path(key.root) / key.path if key.scope == "workspace" else Path(key.path)
                        if any(path == root or path.is_relative_to(root) for root in roots):
                            directories.append(revision)
                    if directories:
                        keys = {value.directory_key for value in directories}
                        snapshots = tuple(WorkspaceFileListDTO.model_validate(value) for value in event["snapshots"]
                                          if WorkspaceFileListDTO.model_validate(value).directory_key in keys)
                        yield WorkspaceFileChangeBatch(directories=tuple(directories), event_id=cursor, snapshots=snapshots)
                done, _pending = await asyncio.wait({next_batch}, timeout=0.2)
                if not done:
                    continue
                try:
                    batch = next_batch.result()
                except StopAsyncIteration:
                    return
                next_batch = asyncio.create_task(anext(source))
                if batch.error is not None:
                    yield batch
                    return
                observed = []
                if not batch.overflow:
                    parents = {Path(change.path).parent for change in batch.changes}
                    removed = {Path(change.path) for change in batch.changes if change.kind == "delete"}
                    for key, real in owner.store.observed_directories(parents, removed=removed):
                        path = Path(real)
                        if path in parents or any(path == value or path.is_relative_to(value) for value in removed):
                            observed.append((key, path))
                directories = await asyncio.to_thread(owner.observe_directories, observed)
                yield WorkspaceFileChangeBatch(changes=batch.changes, overflow=batch.overflow,
                                                directories=tuple(directories), event_id=cursor,
                                                invalidated_roots=tuple(str(root) for root in roots) if batch.overflow else ())
        finally:
            next_batch.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await next_batch
