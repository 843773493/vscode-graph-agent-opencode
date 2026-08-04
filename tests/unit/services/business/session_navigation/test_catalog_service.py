from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.public_v2.session_navigation import SessionFolderUpdateRequest
from app.services.business.session_navigation import SessionCatalogService


T = TypeVar("T")


class _SessionService:
    def __init__(self, sessions_root: Path) -> None:
        self.path_resolver = get_session_path_resolver(sessions_root)
        self.path_resolver.initialize()

    def register_change_listener(self, listener) -> None:
        del listener


class _JobService:
    def __init__(self) -> None:
        self.locked_session_ids: list[str] = []

    async def run_sessions_idle_operation(
        self,
        session_ids: list[str],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        self.locked_session_ids = list(session_ids)
        return await operation()


@pytest.mark.asyncio
async def test_catalog_cache_detects_manual_physical_move(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    source_folder = resolver.create_folder(name="原目录", parent_node_id=None)
    target_folder = resolver.create_folder(name="目标目录", parent_node_id=None)
    session_dir = session_bundle_factory(sessions_root, "ses_manual_catalog")
    resolver.relocate_session(
        session_id="ses_manual_catalog",
        parent_node_id=source_folder.node_id,
        manifest=json.loads((session_dir / "session.json").read_text(encoding="utf-8")),
    )
    catalog = SessionCatalogService(session_service=session_service)
    first = await catalog.export_index()
    first_node = next(
        node for node in first.items if node.node_id == "ses_manual_catalog"
    )
    moved_path = target_folder.path / session_dir.name
    resolver.resolve_session_node("ses_manual_catalog").replace(moved_path)

    assert first_node.parent_node_id == source_folder.node_id
    with pytest.raises(RuntimeError, match="绕过软件修改会话目录结构"):
        await catalog.export_index()


@pytest.mark.asyncio
async def test_moving_folder_uses_idle_guard_for_every_descendant_session(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.create_folder(name="任务目录", parent_node_id=None)
    nested = resolver.create_folder(
        name="日期目录",
        parent_node_id=folder.node_id,
    )
    session_ids = ["ses_guard_alpha", "ses_guard_beta"]
    for session_id in session_ids:
        session_bundle_factory(sessions_root, session_id)
        session_dir = resolver.resolve_session_node(session_id)
        resolver.relocate_session(
            session_id=session_id,
            parent_node_id=nested.node_id,
            manifest=json.loads(
                (session_dir / "session.json").read_text(encoding="utf-8")
            ),
        )
    job_service = _JobService()
    catalog = SessionCatalogService(
        session_service=session_service,
        job_service=job_service,
    )

    await catalog.update_folder(
        folder.node_id,
        SessionFolderUpdateRequest(name="任务目录已改名"),
    )

    assert job_service.locked_session_ids == sorted(session_ids)
    assert resolver.get_node(folder.node_id).name == "任务目录已改名"


@pytest.mark.asyncio
async def test_deep_search_builds_breadcrumbs_only_for_returned_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    parent_id: str | None = None
    for depth in range(100):
        folder = resolver.create_folder(
            name=f"needle-{depth:03d}",
            parent_node_id=parent_id,
        )
        parent_id = folder.node_id
    catalog = SessionCatalogService(session_service=session_service)
    breadcrumb_calls = 0
    original = SessionCatalogService._breadcrumb_items

    def count_breadcrumbs(node, nodes_by_id):
        nonlocal breadcrumb_calls
        breadcrumb_calls += 1
        return original(node, nodes_by_id)

    monkeypatch.setattr(catalog, "_breadcrumb_items", count_breadcrumbs)

    result = await catalog.search(query="needle", limit=2, cursor=None)

    assert result.total == 100
    assert len(result.items) == 2
    assert result.cursor is not None
    assert breadcrumb_calls == 2
