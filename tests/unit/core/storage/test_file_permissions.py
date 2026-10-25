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


@pytest.mark.parametrize(
    ("is_directory", "permission_suffix"),
    [(True, ":(OI)(CI)F"), (False, ":F")],
)
def test_windows_acl_grants_owner_system_and_administrators(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    is_directory: bool,
    permission_suffix: str,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(
        arguments: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    monkeypatch.setattr(
        file_permissions,
        "_windows_acl_is_private",
        lambda _path, *, is_directory: False,
    )
    monkeypatch.setattr(file_permissions.subprocess, "run", run)
    target = tmp_path / "private path with spaces"
    if is_directory:
        target.mkdir()
    else:
        target.write_text("secret", encoding="utf-8")

    restrict_private_path_access(target, is_directory=is_directory, descriptor=123)

    assert [arguments for arguments, _ in calls] == [
        ["icacls", str(target), "/reset"],
        [
            "icacls",
            str(target),
            "/inheritance:r",
            "/grant:r",
            f"*S-1-3-4{permission_suffix}",
            f"*S-1-5-18{permission_suffix}",
            f"*S-1-5-32-544{permission_suffix}",
        ],
    ]
    assert all(
        options
        == {
            "capture_output": True,
            "text": True,
            "timeout": 10,
            "check": False,
        }
        for _, options in calls
    )


def test_windows_acl_failure_reports_target_and_command_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "auth.json"
    target.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    monkeypatch.setattr(
        file_permissions,
        "_windows_acl_is_private",
        lambda _path, *, is_directory: False,
    )
    monkeypatch.setattr(
        file_permissions.subprocess,
        "run",
        lambda arguments, **_: subprocess.CompletedProcess(
            arguments,
            5,
            stdout="processed 0 files",
            stderr="Access is denied",
        ),
    )

    with pytest.raises(PermissionError, match="Access is denied") as error:
        restrict_private_path_access(target)

    assert str(target) in str(error.value)


def test_windows_valid_acl_does_not_start_icacls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "auth.json"
    target.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    monkeypatch.setattr(
        file_permissions,
        "_windows_acl_is_private",
        lambda _path, *, is_directory: True,
    )

    def reject_process_start(*_arguments: object, **_options: object) -> None:
        raise AssertionError("合规 ACL 不得启动 icacls")

    monkeypatch.setattr(file_permissions.subprocess, "run", reject_process_start)

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


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生 icacls 与 ACL")
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
        def reject_process_start(*_arguments: object, **_options: object) -> None:
            raise AssertionError("合规 ACL 检查不得启动 icacls")

        context.setattr(file_permissions.subprocess, "run", reject_process_start)
        require_private_file_access(private_file)
