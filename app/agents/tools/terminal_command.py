from __future__ import annotations

import base64
import os
import shlex


def _build_posix_shell_input(
    *,
    command_text: str,
    working_directory: str | None,
    shell_executable: str | None,
    login: bool,
) -> str:
    executable = (
        shlex.quote(shell_executable)
        if shell_executable
        else '"${SHELL:-/bin/bash}"'
    )
    shell_flag = "-lc" if login else "-c"
    invocation = f"{executable} {shell_flag} {shlex.quote(command_text)}"
    if working_directory is None:
        return invocation
    return f"cd -- {shlex.quote(working_directory)} && {invocation}"


def _windows_shell_executable_name(shell_executable: str | None) -> str:
    configured = shell_executable or os.environ.get("COMSPEC") or "cmd.exe"
    return configured.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _windows_cmd_quote(path: str) -> str:
    if '"' in path:
        raise ValueError("Windows 命令路径不能包含双引号")
    return f'"{path}"'


def _windows_powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _build_windows_powershell_input(
    *,
    command_text: str,
    working_directory: str | None,
    shell_executable: str,
    start_marker: str,
    done_marker: str,
) -> str:
    command_payload = base64.b64encode(command_text.encode("utf-8")).decode("ascii")
    script_lines = [
        f"Write-Output {_windows_powershell_quote(start_marker)}",
        "$boxteamExitCode = 0",
        "try {",
    ]
    if working_directory is not None:
        script_lines.append(
            "  Set-Location -LiteralPath "
            f"{_windows_powershell_quote(working_directory)}"
        )
    script_lines.extend(
        [
            "  $global:LASTEXITCODE = 0",
            (
                "  $boxteamCommand = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
                f"{_windows_powershell_quote(command_payload)}))"
            ),
            "  Invoke-Expression -Command $boxteamCommand",
            "  $boxteamSucceeded = $?",
            "  $boxteamExitCode = [int]$LASTEXITCODE",
            "  if (-not $boxteamSucceeded -and $boxteamExitCode -eq 0) {",
            "    $boxteamExitCode = 1",
            "  }",
            "} catch {",
            "  Write-Error $_",
            "  $boxteamExitCode = 1",
            "}",
            (
                "Write-Output ("
                f"{_windows_powershell_quote(done_marker + ':')}"
                " + $boxteamExitCode)"
            ),
        ]
    )
    encoded_script = base64.b64encode(
        "\n".join(script_lines).encode("utf-16le")
    ).decode("ascii")
    return (
        f"{_windows_cmd_quote(shell_executable)} -NoLogo -NoProfile -NonInteractive "
        f"-ExecutionPolicy Bypass -EncodedCommand {encoded_script}\r"
    )


def _build_windows_shell_input(
    *,
    command_text: str,
    working_directory: str | None,
    shell_executable: str | None,
    start_marker: str,
    done_marker: str,
) -> str:
    shell_name = _windows_shell_executable_name(shell_executable)
    executable = shell_executable or os.environ.get("COMSPEC") or "cmd.exe"
    if shell_name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return _build_windows_powershell_input(
            command_text=command_text,
            working_directory=working_directory,
            shell_executable=executable,
            start_marker=start_marker,
            done_marker=done_marker,
        )
    if shell_name not in {"cmd", "cmd.exe"}:
        raise ValueError(
            "Windows 终端暂不支持该 shell；请使用 cmd.exe、powershell.exe 或 pwsh.exe: "
            f"{executable}"
        )

    lines = [f"echo {start_marker}"]
    if working_directory is not None:
        lines.append(f"cd /d {_windows_cmd_quote(working_directory)}")
    lines.extend(
        [
            command_text,
            'set "__BOXTEAM_RC=%ERRORLEVEL%"',
            f"echo {done_marker}:%__BOXTEAM_RC%",
        ]
    )
    return "\r\n".join(lines) + "\r"


def _build_terminal_input(
    *,
    command_text: str,
    working_directory: str | None,
    shell_executable: str | None,
    login: bool,
    start_marker: str,
    done_marker: str,
) -> str:
    if os.name == "nt":
        return _build_windows_shell_input(
            command_text=command_text,
            working_directory=working_directory,
            shell_executable=shell_executable,
            start_marker=start_marker,
            done_marker=done_marker,
        )
    return _build_posix_shell_input(
        command_text=command_text,
        working_directory=working_directory,
        shell_executable=shell_executable,
        login=login,
    )
