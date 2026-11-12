"""通过真实分页列表入口验证普通成员与链接的私有边界。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.exceptions import ForbiddenError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_service import WorkspaceService


@pytest.fixture
def config_service() -> Mock:
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    return config


@pytest.fixture
def workspace_service(tmp_path: Path, config_service: Mock):
    service = WorkspaceService(config_service=config_service, workspace_root=tmp_path)
    yield service
    service.close()


async def test_workspace_listing_filters_private_links_across_pages(
    workspace_service: WorkspaceService, tmp_path: Path,
) -> None:
    for index in range(256):
        (tmp_path / f"file-{index:03}.txt").write_text("scan", encoding="utf-8")
    (tmp_path / "private-alias").symlink_to(tmp_path / ".boxteam", target_is_directory=True)
    (tmp_path / "public-alias").symlink_to(tmp_path / "file-000.txt")
    (tmp_path / "broken-alias").symlink_to(tmp_path / "missing.txt")
    first = await workspace_service.list_files(limit=17)
    assert first.next_cursor is not None
    second = await workspace_service.list_files(limit=500, cursor=first.next_cursor)
    assert [item.name for item in (*first.items, *second.items)] == sorted(
        [f"file-{index:03}.txt" for index in range(256)] + ["public-alias", "broken-alias"],
    )
    assert {item.name: item.kind for item in (*first.items, *second.items)
            if item.name.endswith("alias")} == {"public-alias": "symlink", "broken-alias": "symlink"}
    assert first.directory_revision == second.directory_revision


async def test_listing_rejects_private_root_retargeted_during_scan(
    tmp_path: Path, config_service: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_store, new_store = tmp_path / "store-a", tmp_path / "store-b"
    old_store.mkdir()
    new_store.mkdir()
    private = tmp_path / ".boxteam"
    private.symlink_to(old_store, target_is_directory=True)
    service = WorkspaceService(config_service=config_service, workspace_root=tmp_path)
    (tmp_path / "public.txt").write_text("scan", encoding="utf-8")
    original_node = service._entry_to_file_node
    changed = False

    def retarget(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            private.unlink()
            private.symlink_to(new_store, target_is_directory=True)
        return original_node(*args, **kwargs)

    monkeypatch.setattr(service, "_entry_to_file_node", retarget)
    try:
        with pytest.raises(ForbiddenError, match="私有.*边界.*改变"):
            await service.list_files()
        assert changed
    finally:
        service.close()
