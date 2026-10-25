"""fork 清单不能把扫描失败当空目录，也不能公开原生文件位置。"""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkSnapshotFile,
    _tree_manifest,
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "private-rollout"
    (root / "nested").mkdir(parents=True)
    (root / "rollout.jsonl").write_bytes(b"canonical-original\n")
    (root / "nested/body").write_bytes(b"frozen-body\n")
    return root


@pytest.mark.parametrize("phase", ["scan_root", "scan_nested", "read", "metadata"])
def test_manifest_native_error_preserves_cause_and_original_tree(
    tree: Path, phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    before = _tree_manifest(tree)
    fault = tree if phase == "scan_root" else tree / ("nested" if phase == "scan_nested" else "nested/body")
    native = PermissionError(errno.EACCES, f"拒绝读取 {fault}", str(fault))
    owner, operation = (os, "scandir") if phase.startswith("scan") else (Path, "open" if phase == "read" else "stat")
    original = getattr(owner, operation)

    def denied(path, *args, **kwargs):
        if not isinstance(path, int) and Path(path) == fault:
            raise native
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner, operation, denied)
        with pytest.raises(PermissionError) as caught:
            _tree_manifest(tree)
    assert caught.value.__cause__ is native and caught.value.errno == errno.EACCES
    assert caught.value.filename is None
    assert "artifact=fork_source stage=manifest" in str(caught.value)
    assert str(tree) not in str(caught.value) and str(fault) in caplog.text
    assert _tree_manifest(tree) == before


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_manifest_root_must_be_an_existing_native_directory(
    tree: Path, kind: str, caplog: pytest.LogCaptureFixture,
) -> None:
    before = _tree_manifest(tree)
    root = tree.with_name(f"invalid-{kind}")
    if kind == "file":
        root.write_bytes(b"keep")
    elif kind == "symlink":
        root.symlink_to(tree, target_is_directory=True)
    with pytest.raises(RuntimeError) as caught:
        _tree_manifest(root)
    assert "清单根不是普通目录" in str(caught.value)
    assert "artifact=fork_source stage=manifest" in str(caught.value)
    assert str(root) not in str(caught.value) and str(root) in caplog.text
    assert _tree_manifest(tree) == before
    if kind == "file":
        assert root.read_bytes() == b"keep"
    elif kind == "symlink":
        assert root.is_symlink()
    else:
        assert not root.exists()


@pytest.mark.parametrize("directory", [False, True])
def test_manifest_rejects_links_without_reading_target_or_publishing_path(
    tree: Path, directory: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    target = tree / "nested" if directory else tree / "nested/body"
    link = tree / "private-link"
    link.symlink_to(target, target_is_directory=directory)
    with pytest.raises(RuntimeError) as caught:
        _tree_manifest(tree)
    assert "包含符号链接" in str(caught.value)
    assert "artifact=fork_source stage=manifest" in str(caught.value)
    assert str(tree) not in str(caught.value) and str(link) in caplog.text
    assert link.is_symlink() and (tree / "nested/body").read_bytes() == b"frozen-body\n"


def test_manifest_keeps_order_hash_private_exclusion_and_real_empty_directory(tree: Path) -> None:
    (tree / "index.sqlite-shm").write_bytes(b"not-a-snapshot-input")
    expected = tuple(ForkSnapshotFile(name, len(body), hashlib.sha256(body).hexdigest()) for name, body in (
        ("nested/body", b"frozen-body\n"), ("rollout.jsonl", b"canonical-original\n"),
    ))
    assert _tree_manifest(tree) == expected
    empty = tree / "empty"
    empty.mkdir()
    assert _tree_manifest(empty) == ()
