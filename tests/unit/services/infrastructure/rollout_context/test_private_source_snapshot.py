from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.infrastructure.rollout_context.fork import source_snapshot


def test_fork_snapshot_restricts_empty_capture_directory_before_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_rollout = tmp_path / "source-rollout"
    source_rollout.mkdir()
    captured_root = tmp_path / "capture"
    storage = SimpleNamespace(
        storage=SimpleNamespace(root=lambda *_args, **_kwargs: source_rollout)
    )
    restricted_paths: list[Path] = []
    restrict = source_snapshot.restrict_private_path_access

    def restrict_and_record(
        path: Path,
        *,
        is_directory: bool = False,
        descriptor: int | None = None,
    ) -> None:
        assert path == captured_root
        assert path.is_dir()
        assert list(path.iterdir()) == []
        assert is_directory
        assert descriptor is None
        restrict(path, is_directory=is_directory, descriptor=descriptor)
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o700
        restricted_paths.append(path)

    monkeypatch.setattr(
        source_snapshot,
        "restrict_private_path_access",
        restrict_and_record,
    )

    with pytest.raises(RuntimeError, match="缺少既有普通写锁文件"):
        source_snapshot.capture_fork_rollout_snapshot(
            storage,
            source_session_id="source-session",
            source_thread_id="source-thread",
            captured_root=captured_root,
            snapshot_id="snapshot-test",
            rollout_lock_held=True,
        )

    assert restricted_paths == [captured_root]
    assert not captured_root.exists()


def test_fork_snapshot_cleans_capture_directory_when_acl_restriction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_rollout = tmp_path / "source-rollout"
    source_rollout.mkdir()
    captured_root = tmp_path / "capture"
    storage = SimpleNamespace(
        storage=SimpleNamespace(root=lambda *_args, **_kwargs: source_rollout)
    )

    def reject_restriction(
        path: Path,
        *,
        is_directory: bool = False,
        descriptor: int | None = None,
    ) -> None:
        assert path == captured_root
        assert path.is_dir()
        assert list(path.iterdir()) == []
        assert is_directory
        assert descriptor is None
        raise PermissionError("capture ACL denied")

    monkeypatch.setattr(
        source_snapshot,
        "restrict_private_path_access",
        reject_restriction,
    )

    with pytest.raises(PermissionError, match="stage=preparation") as caught:
        source_snapshot.capture_fork_rollout_snapshot(
            storage,
            source_session_id="source-session",
            source_thread_id="source-thread",
            captured_root=captured_root,
            snapshot_id="snapshot-test",
            rollout_lock_held=True,
        )

    assert isinstance(caught.value.__cause__, PermissionError)
    assert str(caught.value.__cause__) == "capture ACL denied"
    assert not captured_root.exists()
