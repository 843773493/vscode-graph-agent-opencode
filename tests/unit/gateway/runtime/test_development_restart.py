from pathlib import Path

import pytest

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_DEVELOPMENT_RESTART_RUNNER", "/tmp/bun")
    monkeypatch.delenv("BOXTEAM_DEVELOPMENT_RESTART_SCRIPT", raising=False)
    monkeypatch.delenv("BOXTEAM_DEVELOPMENT_RESTART_CWD", raising=False)

    with pytest.raises(RuntimeError, match="重启配置不完整"):
        resolve_development_restart_command()


def test_restart_process_uses_validated_command_and_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr(
        "app.gateway.runtime.development_restart.subprocess.Popen",
        fake_popen,
    )
    log_path = tmp_path / "logs" / "development-restart.log"

    assert start_development_restart(command, log_path=log_path) == 24680
    assert captured["argv"] == [
        str(runner),
        str(script),
        "--only-launch",
        f"--restart-delay-ms={RESTART_DELAY_MS}",
    ]
    assert log_path.is_file()
