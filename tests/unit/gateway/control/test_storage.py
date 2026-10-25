from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.core import file_permissions
from app.gateway.control import storage


def test_read_json_object_skips_existence_probe_for_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "gateway.json"
    target.write_text('{"name": "工作区"}', encoding="utf-8")

    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "exists",
            lambda _path: pytest.fail("JSON 读取不应先执行 exists 查询"),
        )
        payload = storage.read_json_object(target, default={})

    assert payload == {"name": "工作区"}


def test_read_json_object_returns_a_copy_of_default_for_missing_file(
    tmp_path: Path,
) -> None:
    default = {"schema_version": 1}
    target = tmp_path / "missing.json"

    first = storage.read_json_object(target, default=default)
    second = storage.read_json_object(target, default=default)

    assert first == second == default
    assert first is not default
    assert second is not default
    assert first is not second


def test_atomic_write_json_sets_private_windows_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    monkeypatch.setattr(
        file_permissions,
        "_windows_acl_is_private",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(file_permissions.subprocess, "run", run)
    target = tmp_path / "gateway.json"

    storage.atomic_write_json(target, {"value": "serializable"})

    assert target.is_file()
    assert len(calls) == 2
    assert all(Path(arguments[1]).parent == tmp_path for arguments in calls)
    assert all(Path(arguments[1]) != target for arguments in calls)


def test_write_error_remains_primary_when_temporary_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "gateway.json"
    primary_error = ValueError("injected serialization failure")
    cleanup_error = PermissionError("injected temporary-file cleanup failure")
    cleanup_paths: list[Path] = []
    original_unlink = Path.unlink

    def fail_dump(*_args: object, **_kwargs: object) -> None:
        raise primary_error

    def fail_temporary_unlink(
        path: Path,
        *,
        missing_ok: bool = False,
    ) -> None:
        if path.parent == tmp_path and path.name.startswith(f".{target.name}."):
            cleanup_paths.append(path)
            raise cleanup_error
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(storage.json, "dump", fail_dump)
        patch.setattr(Path, "unlink", fail_temporary_unlink)
        with pytest.raises(ValueError) as caught:
            storage.atomic_write_json(target, {"value": "serializable"})

    assert caught.value is primary_error
    assert len(cleanup_paths) == 1
    assert primary_error.__notes__ == [
        f"临时文件清理失败: {cleanup_paths[0]}: {cleanup_error!r}"
    ]
    assert cleanup_paths[0].exists()
    cleanup_paths[0].unlink()


def test_cleanup_error_after_replace_remains_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "gateway.json"
    payload = {"name": "工作区"}
    cleanup_error = PermissionError("injected temporary-file cleanup failure")
    cleanup_paths: list[Path] = []
    original_unlink = Path.unlink

    def fail_temporary_unlink(
        path: Path,
        *,
        missing_ok: bool = False,
    ) -> None:
        if path.parent == tmp_path and path.name.startswith(f".{target.name}."):
            cleanup_paths.append(path)
            raise cleanup_error
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_temporary_unlink)
        with pytest.raises(PermissionError) as caught:
            storage.atomic_write_json(target, payload)

    assert caught.value is cleanup_error
    assert len(cleanup_paths) == 1
    assert not cleanup_paths[0].exists()
    assert target.read_text(encoding="utf-8") == json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ) + "\n"
