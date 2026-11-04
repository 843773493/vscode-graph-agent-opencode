"""通过真实列表入口约束私有根解析成本，并保留逐节点边界检查。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.exceptions import ForbiddenError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_files import paths as paths_module
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


async def test_workspace_listing_resolves_private_root_per_scan(
    workspace_service: WorkspaceService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(256):
        (tmp_path / f"file-{index:03}.txt").write_text("scan", encoding="utf-8")
    (tmp_path / "private-alias").symlink_to(tmp_path / ".boxteam", target_is_directory=True)
    original_realpath = paths_module.os.path.realpath
    roots = 0
    checked_children: set[Path] = set()

    def observed_realpath(path, *args, **kwargs):
        nonlocal roots
        candidate = Path(path)
        if candidate == tmp_path / ".boxteam":
            roots += 1
        if candidate.parent == tmp_path:
            checked_children.add(candidate)
        return original_realpath(path, *args, **kwargs)

    monkeypatch.setattr(paths_module.os.path, "realpath", observed_realpath)
    first = await workspace_service.list_files(limit=17)
    first_roots = roots
    assert first.next_cursor is not None
    second = await workspace_service.list_files(limit=500, cursor=first.next_cursor)
    assert len(first.items) + len(second.items) == 256
    assert all(item.name != "private-alias" for item in (*first.items, *second.items))
    assert {tmp_path / f"file-{index:03}.txt" for index in range(256)} <= checked_children
    assert first_roots <= 16, f"单页不应随 256 个 child 重复解析私有根: {first_roots}"
    assert roots - first_roots <= 16, f"续页必须重新核根，但不应逐 child 重复: {roots - first_roots}"
    print(f"workspace private-root resolutions: first={first_roots}, second={roots - first_roots}")


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
