from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from app.core import file_permissions
from app.core.file_permissions import (
    require_private_file_access,
    restrict_private_path_access,
)


@pytest.mark.parametrize("is_directory", [True, False])
def test_windows_acl_policy_uses_native_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    is_directory: bool,
) -> None:
    calls: list[tuple[Path, bool]] = []

    def set_acl(path: Path, *, is_directory: bool) -> None:
        calls.append((path, is_directory))

    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    monkeypatch.setattr(
        file_permissions.windows_fs,
        "private_acl_is_current",
        lambda _path, *, is_directory: False,
    )
    monkeypatch.setattr(file_permissions.windows_fs, "set_private_acl", set_acl)
    target = tmp_path / "private path with spaces"
    if is_directory:
        target.mkdir()
    else:
        target.write_text("secret", encoding="utf-8")

    restrict_private_path_access(target, is_directory=is_directory, descriptor=123)

    assert calls == [(file_permissions.native_io_path(target), is_directory)]


@pytest.mark.parametrize("phase", ["query", "set"])
def test_windows_acl_failure_preserves_native_error_and_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    target = tmp_path / "auth.json"
    target.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    failure = PermissionError(13, "Access is denied", str(target))
    writes: list[Path] = []

    def query(_path: Path, *, is_directory: bool) -> bool:
        if phase == "query":
            raise failure
        return False

    def write(path: Path, *, is_directory: bool) -> None:
        writes.append(path)
        raise failure

    monkeypatch.setattr(file_permissions.windows_fs, "private_acl_is_current", query)
    monkeypatch.setattr(file_permissions.windows_fs, "set_private_acl", write)

    with pytest.raises(PermissionError, match="Access is denied") as error:
        restrict_private_path_access(target)

    assert error.value is failure and error.value.filename == str(target)
    assert writes == ([] if phase == "query" else [file_permissions.native_io_path(target)])


def test_windows_valid_acl_does_not_rewrite_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "auth.json"
    target.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    monkeypatch.setattr(
        file_permissions.windows_fs,
        "private_acl_is_current",
        lambda _path, *, is_directory: True,
    )

    def reject_acl_write(*_arguments: object, **_options: object) -> None:
        raise AssertionError("合规 ACL 不得重新写入")

    monkeypatch.setattr(file_permissions.windows_fs, "set_private_acl", reject_acl_write)

    require_private_file_access(target)


@pytest.mark.skipif(os.name != "posix", reason="仅 POSIX 支持这些 mode 位断言")
def test_posix_private_path_modes_are_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(file_permissions, "_is_windows", lambda: False)
    private_directory = tmp_path / "private"
    private_directory.mkdir()
    private_file = private_directory / "secret.json"
    private_file.write_text("{}\n", encoding="utf-8")

    restrict_private_path_access(private_directory, is_directory=True)
    descriptor = os.open(private_file, os.O_RDONLY)
    try:
        restrict_private_path_access(private_file, descriptor=descriptor)
    finally:
        os.close(descriptor)

    assert private_directory.stat().st_mode & 0o777 == 0o700
    assert private_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="仅 POSIX 支持这些 mode 位断言")
def test_require_private_file_access_rejects_group_or_other_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(file_permissions, "_is_windows", lambda: False)
    private_file = tmp_path / "secret.json"
    private_file.write_text("{}\n", encoding="utf-8")
    private_file.chmod(0o640)

    with (
        private_file.open("rb") as file,
        pytest.raises(PermissionError, match="仅对所有者开放"),
    ):
        require_private_file_access(private_file, descriptor=file.fileno())


@pytest.mark.skipif(os.name != "posix", reason="仅 POSIX 支持这些 mode 位断言")
def test_require_private_file_access_accepts_owner_only_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(file_permissions, "_is_windows", lambda: False)
    private_file = tmp_path / "secret.json"
    private_file.write_text("{}\n", encoding="utf-8")
    private_file.chmod(0o600)

    with private_file.open("rb") as file:
        require_private_file_access(private_file, descriptor=file.fileno())


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生 ACL")
def test_windows_acl_limits_directory_file_and_inherited_file(tmp_path: Path) -> None:
    directory = tmp_path / "private auth"
    directory.mkdir()
    restrict_private_path_access(directory, is_directory=True)
    inherited_file = directory / "inherited-auth.json"
    inherited_file.write_text("{}\n", encoding="utf-8")
    direct_file = tmp_path / "direct-auth.json"
    direct_file.write_text("{}\n", encoding="utf-8")
    restrict_private_path_access(direct_file)

    def read_acl_entries(path: Path) -> set[tuple[str, str, str]]:
        environment = os.environ.copy()
        environment["BOXTEAM_TEST_ACL_PATH"] = str(path)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-Acl -LiteralPath $env:BOXTEAM_TEST_ACL_PATH).Sddl",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, result.stderr
        entries = [
            entry.split(";")
            for entry in re.findall(r"\(([^()]*)\)", result.stdout.strip())
        ]
        assert all(len(entry) == 6 for entry in entries), result.stdout
        return {(entry[5], entry[1], entry[2]) for entry in entries}

    principals = {"OW", "SY", "BA"}
    assert read_acl_entries(directory) == {
        (principal, "OICI", "FA") for principal in principals
    }
    assert read_acl_entries(inherited_file) == {
        (principal, "ID", "FA") for principal in principals
    }
    assert read_acl_entries(direct_file) == {
        (principal, "", "FA") for principal in principals
    }

    non_inheriting_directory = tmp_path / "private directory without inheritance"
    non_inheriting_directory.mkdir()
    restrict_private_path_access(non_inheriting_directory)
    assert read_acl_entries(non_inheriting_directory) == {
        (principal, "", "FA") for principal in principals
    }
    restrict_private_path_access(non_inheriting_directory, is_directory=True)
    assert read_acl_entries(non_inheriting_directory) == {
        (principal, "OICI", "FA") for principal in principals
    }


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生 DACL 查询")
def test_windows_private_file_access_repairs_once_then_checks_in_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_file = tmp_path / "private-auth.json"
    private_file.write_text("secret", encoding="utf-8")

    require_private_file_access(private_file)

    with monkeypatch.context() as context:
        def reject_acl_write(*_arguments: object, **_options: object) -> None:
            raise AssertionError("合规 ACL 检查不得重新写入")

        context.setattr(file_permissions.windows_fs, "set_private_acl", reject_acl_write)
        require_private_file_access(private_file)


def test_readonly_cleanup_only_changes_the_selected_directory_and_direct_files(tmp_path: Path) -> None:
    directory = tmp_path / "protected"
    nested = directory / "nested"
    nested.mkdir(parents=True)
    direct = directory / "content"
    child = nested / "result"
    direct.write_bytes(b"original")
    child.write_bytes(b"derived")
    direct.chmod(0o400)
    child.chmod(0o400)
    nested.chmod(0o500)
    directory.chmod(0o500)
    nested_mode, child_mode = nested.stat().st_mode, child.stat().st_mode
    try:
        file_permissions.prepare_readonly_directory_removal(directory)
        assert directory.stat().st_mode & 0o200
        assert direct.stat().st_mode & 0o200
        assert nested.stat().st_mode == nested_mode and child.stat().st_mode == child_mode
        assert direct.read_bytes() == b"original" and child.read_bytes() == b"derived"
    finally:
        nested.chmod(0o700)
        child.chmod(0o600)


@pytest.mark.skipif(os.name != "posix", reason="需要无额外权限的符号链接")
def test_readonly_cleanup_rejects_directory_alias_and_keeps_file_alias_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "content"
    target.write_bytes(b"not-owned")
    target.chmod(0o400)
    alias = tmp_path / "directory-alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="不能经过符号链接"):
        file_permissions.prepare_readonly_directory_removal(alias)
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "file-alias").symlink_to(target)
    file_permissions.prepare_readonly_directory_removal(selected)
    assert target.stat().st_mode & 0o777 == 0o400
    assert target.read_bytes() == b"not-owned"
