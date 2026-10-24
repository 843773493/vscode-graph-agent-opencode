"""导航存储拒绝损坏数据，不向普通错误复制磁盘位置或损坏值。"""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path

import pytest

from app.gateway.control import navigation
from app.schemas.gateway_control import WorkspaceFolderCreateRequest


@pytest.fixture
def navigation_store(tmp_path: Path) -> tuple[navigation.WorkspaceNavigationStore, Path]:
    path = tmp_path / "private-navigation-root" / "workspace-tree.json"
    store = navigation.WorkspaceNavigationStore(storage_path=path)
    store.create_folder(WorkspaceFolderCreateRequest(name="保留的目录"), ())
    return store, path


@pytest.mark.parametrize("content, category", [
    (b"[]", RuntimeError),
    (b'{"nodes":"/private/navigation-secret"}', RuntimeError),
    (b'{"nodes":[{"kind":"/private/navigation-secret"}]}', ValueError),
    (b'{"nodes":[{"node_id":"x","kind":"workspace_folder","name":"x","position":"/private/navigation-secret"}]}', ValueError),
    (b'{"nodes":[{"node_id":"x","kind":"workspace_folder","name":"x","parent_node_id":"/private/navigation-secret"}]}', ValueError),
    (b'{"nodes":"/private/navigation-secret",}', ValueError),
    (b'\xff/private/navigation-secret', ValueError),
])
def test_corrupt_navigation_is_rejected_without_rewrite_or_input_echo(
    navigation_store, content: bytes, category: type[Exception],
) -> None:
    store, path = navigation_store
    original = path.read_bytes()
    path.write_bytes(content)
    for operation in (lambda: store.list_tree(()), lambda: store.create_folder(WorkspaceFolderCreateRequest(name="不能写入"), ())):
        with pytest.raises(category) as caught:
            operation()
        assert str(path) not in str(caught.value)
        assert "navigation-secret" not in str(caught.value)
        assert path.read_bytes() == content
    path.write_bytes(original)
    assert [item.name for item in store.list_tree(()).nodes] == ["保留的目录"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("native_path", ["/private/navigation-secret", r"C:\private\navigation-secret", r"\\host\private\navigation-secret"])
def test_native_failure_preserves_category_errno_cause_and_diagnostics(
    navigation_store, operation: str, native_path: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    store, path = navigation_store
    original = path.read_bytes()
    error = PermissionError(errno.EACCES, f"拒绝访问 {native_path}", native_path)
    original_read = Path.read_text

    def read(target: Path, *args: object, **kwargs: object) -> str:
        if target == path:
            raise error
        return original_read(target, *args, **kwargs)

    def write(*_args: object, **_kwargs: object) -> None:
        raise error

    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR, logger=navigation.__name__):
        if operation == "read":
            patch.setattr(Path, "read_text", read)
        else:
            patch.setattr(navigation, "atomic_write_json", write)
        with pytest.raises(PermissionError) as caught:
            store.create_folder(WorkspaceFolderCreateRequest(name="新目录"), ())
    assert caught.value.__cause__ is error
    assert caught.value.errno == errno.EACCES
    assert f"operation={operation}" in str(caught.value)
    assert "PermissionError" in str(caught.value)
    assert native_path not in str(caught.value)
    assert str(path) not in str(caught.value)
    assert native_path in caplog.text and str(path) in caplog.text
    assert path.read_bytes() == original


def test_write_failure_after_publication_does_not_claim_rollback(
    navigation_store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, path = navigation_store
    write = navigation.atomic_write_json
    error = OSError(errno.EIO, "发布后清理失败", "/private/navigation-secret")

    def publish_then_fail(target: Path, payload: dict[str, object]) -> None:
        write(target, payload)
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(navigation, "atomic_write_json", publish_then_fail)
        with pytest.raises(OSError) as caught:
            store.create_folder(WorkspaceFolderCreateRequest(name="已发布目录"), ())
    assert caught.value.__cause__ is error
    assert caught.value.errno == errno.EIO
    assert "navigation-secret" not in str(caught.value)
    assert [item["name"] for item in json.loads(path.read_bytes())["nodes"]] == ["保留的目录", "已发布目录"]
    reopened = navigation.WorkspaceNavigationStore(storage_path=path)
    assert [item.name for item in reopened.list_tree(()).nodes] == ["保留的目录", "已发布目录"]
