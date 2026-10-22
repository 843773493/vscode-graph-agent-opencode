from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from app.services.infrastructure.trace_event_store import TraceEventStore


@pytest.fixture
def trace_event_store_factory(
    tmp_path: Path,
    session_bundle_factory: Callable[[Path, str], Path],
) -> Callable[[str], tuple[TraceEventStore, Path]]:
    """在当前测试目录创建合法会话 bundle 和事件存储。"""

    def create(session_id: str) -> tuple[TraceEventStore, Path]:
        session_dir = session_bundle_factory(tmp_path, session_id)
        return TraceEventStore(sessions_dir=tmp_path), session_dir

    return create
