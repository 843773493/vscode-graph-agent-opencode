import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.gateway.runtime import process_lifecycle
from app.gateway.runtime.development_restart import (
    RESTART_DELAY_MS,
    resolve_development_restart_command,
    start_development_restart,
)


def test_restart_command_is_unavailable_without_development_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "BOXTEAM_DEVELOPMENT_RESTART_RUNNER",
        "BOXTEAM_DEVELOPMENT_RESTART_SCRIPT",
        "BOXTEAM_DEVELOPMENT_RESTART_CWD",
    ):
        monkeypatch.delenv(name, raising=False)

    assert resolve_development_restart_command() is None


def test_restart_command_requires_complete_development_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "BOXTEAM_DEVELOPMENT_RESTART_RUNNER", str(tmp_path / "bun")
    )
    monkeypatch.delenv("BOXTEAM_DEVELOPMENT_RESTART_SCRIPT", raising=False)
    monkeypatch.delenv("BOXTEAM_DEVELOPMENT_RESTART_CWD", raising=False)

    with pytest.raises(RuntimeError, match="重启配置不完整"):
        resolve_development_restart_command()


@pytest.mark.parametrize("platform_name", ["posix", "nt"], ids=["posix", "windows"])
def test_restart_process_uses_validated_command_and_platform_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
) -> None:
    runner = tmp_path / "bun"
    script = tmp_path / "dev.mjs"
    runner.touch()
    script.touch()
    monkeypatch.setenv("BOXTEAM_DEVELOPMENT_RESTART_RUNNER", str(runner))
    monkeypatch.setenv("BOXTEAM_DEVELOPMENT_RESTART_SCRIPT", str(script))
    monkeypatch.setenv("BOXTEAM_DEVELOPMENT_RESTART_CWD", str(tmp_path))
    command = resolve_development_restart_command()
    assert command is not None

    captured: dict[str, object] = {}

    class Process:
        pid = 24680

    def fake_popen(argv: list[str], **options: object) -> Process:
        captured["argv"] = argv
        captured["options"] = options
        return Process()

    log_dir = tmp_path / "logs"
    log_path = log_dir / "launcher/development-restart.log"

    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(process_lifecycle, "os", SimpleNamespace(name=platform_name, environ=process_lifecycle.os.environ))
        platform_patch.setattr("app.gateway.runtime.process_logs.ProcessLogOwnership.bind_process", lambda *_: None)
        if platform_name == "nt":
            process_group_flag = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
            )
            detached_flag = getattr(subprocess, "DETACHED_PROCESS", 0x8)
            platform_patch.setattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                process_group_flag,
                raising=False,
            )
            platform_patch.setattr(
                subprocess, "DETACHED_PROCESS", detached_flag, raising=False
            )
        platform_patch.setattr(subprocess, "Popen", fake_popen)

        assert start_development_restart(command, log_dir=log_dir) == 24680

    assert captured["argv"] == [
        str(runner),
        str(script),
        "--only-launch",
        f"--restart-delay-ms={RESTART_DELAY_MS}",
    ]
    assert log_path.is_file()
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["cwd"] == command.working_directory
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stderr"] is options["stdout"]
    assert options["stdout"].closed
    assert options["env"]["BOXTEAM_RUNTIME_LOG_SINK"]
    if platform_name == "nt":
        expected_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | getattr(
            subprocess, "DETACHED_PROCESS", 0x8
        )
        assert options["creationflags"] == expected_flags
        assert options["start_new_session"] is False
    else:
        assert options["start_new_session"] is True
        assert options["creationflags"] == 0
