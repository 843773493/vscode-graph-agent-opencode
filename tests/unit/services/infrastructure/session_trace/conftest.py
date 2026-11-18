from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from app.services.infrastructure.session_trace.store import TraceEventStore


@pytest.fixture
async def trace_event_store_factory(
    tmp_path: Path,
    session_bundle_factory: Callable[[Path, str], Path],
) -> AsyncIterator[Callable[[str], tuple[TraceEventStore, Path]]]:
    """在当前测试目录创建合法会话 bundle 和事件存储。"""

    stores: list[TraceEventStore] = []

    def create(session_id: str) -> tuple[TraceEventStore, Path]:
        session_dir = session_bundle_factory(tmp_path, session_id)
        store = TraceEventStore(sessions_dir=tmp_path)
        stores.append(store)
        return store, session_dir

    yield create
    for store in stores:
        await store.close()
