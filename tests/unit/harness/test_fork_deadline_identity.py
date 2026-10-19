from __future__ import annotations

import os
import shutil
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support import fork_deadline


def test_c3_identity_uses_saved_launch_arguments_after_proc_cmdline_rewrite(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sshd = shutil.which("sshd")
    if sshd is None:
        pytest.skip("C3 SSHD identity requires the OpenSSH server executable")

    context = TestRunContext.from_test_file(Path(request.node.path))
    ssh_root = context.artifacts_dir / "c3-ssh"
    ssh_root.mkdir(parents=True, exist_ok=True)
    config_file = ssh_root / "sshd_config"
    config_file.write_text("Port 2222\n")
    pid = 42_001
    process_root = Path(f"/proc/{pid}")
    stat_file = process_root / "stat"
    cmdline_file = process_root / "cmdline"
    executable_file = process_root / "exe"
    executable = Path(sshd).resolve()
    start_time = "394396094"
    stat_fields = ["S", "1", str(pid), str(pid), *(["0"] * 15), start_time]
    rewritten_cmdline = (
        f"sshd: {executable} -D -e -f {config_file} [listener] 0 of 10-100 startups\0"
    ).encode()
    launch_arguments = [str(sshd), "-D", "-e", "-f", str(config_file)]
    process = SimpleNamespace(pid=pid, args=launch_arguments)
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_stat = Path.stat
    original_resolve = Path.resolve

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == stat_file:
            return f"{pid} (sshd) {' '.join(stat_fields)}"
        return original_read_text(path, *args, **kwargs)

    def read_bytes(path: Path, *args: object, **kwargs: object) -> bytes:
        if path == cmdline_file:
            return rewritten_cmdline
        return original_read_bytes(path, *args, **kwargs)

    def stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == process_root:
            return SimpleNamespace(st_uid=os.geteuid())  # type: ignore[return-value]
        return original_stat(path, *args, **kwargs)

    def resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == executable_file:
            return executable
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "resolve", resolve)
    identity = fork_deadline._read_c3_sshd_identity(context.artifacts_dir, process, config_file)

    assert identity["launch_arguments"] == launch_arguments
    terminated: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        fork_deadline.os,
        "killpg",
        lambda pgid, sig: terminated.append((pgid, sig)),
    )
    fork_deadline._kill_owned_c3_sshd(identity)

    assert terminated == [(pid, signal.SIGKILL)]
