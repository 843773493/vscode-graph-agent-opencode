from __future__ import annotations

import errno

from tests.e2e.system.gateway import gateway_docker


def test_docker_target_setup_uses_windows_lock_before_provisioning(
    monkeypatch, tmp_path
):
    class WindowsLockApi:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self, events):
            self.events = events
            self.errors = [PermissionError(errno.EACCES, "锁区已占用")]
            self.calls = []

        def locking(self, file_descriptor, mode, byte_count):
            self.calls.append((file_descriptor, mode, byte_count))
            self.events.append(("lock", mode))
            if self.errors:
                raise self.errors.pop(0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gateway_docker, "_IS_WINDOWS", True)
    events = []
    lock_api = WindowsLockApi(events)
    monkeypatch.setattr(gateway_docker, "msvcrt", lock_api, raising=False)
    monkeypatch.setattr(gateway_docker, "sleep", lambda _: None)

    configured_known_hosts = (
        tmp_path
        / "out"
        / "cross-platform-dev-targets"
        / gateway_docker.TARGET_ID
        / "ssh"
        / "known_hosts"
    )

    def run_target_command(project_root, command, *arguments):
        events.append(("command", command, arguments))
        if command == "bootstrap":
            configured_known_hosts.parent.mkdir(parents=True, exist_ok=True)
            configured_known_hosts.write_text("docker target\n", encoding="utf-8")

    monkeypatch.setattr(gateway_docker, "_run_target_command", run_target_command)
    monkeypatch.setattr(gateway_docker, "wait_for_ssh_ready", lambda _: None)
    known_hosts_path = tmp_path / "artifacts" / "known_hosts"

    target = gateway_docker.ensure_gateway_ssh_container(
        known_hosts_path=known_hosts_path,
    )

    lock_path = (
        tmp_path
        / "out"
        / "cross-platform-dev-targets"
        / gateway_docker.TARGET_ID
        / ".e2e-provision.lock"
    )
    assert lock_path.read_bytes() == b"\0"
    assert events == [
        ("lock", WindowsLockApi.LK_NBLCK),
        ("lock", WindowsLockApi.LK_NBLCK),
        ("command", "provision", ()),
        ("command", "sync", ("--activate",)),
        ("command", "bootstrap", ()),
        ("lock", WindowsLockApi.LK_UNLCK),
    ]
    assert [mode for _, mode, _ in lock_api.calls] == [
        WindowsLockApi.LK_NBLCK,
        WindowsLockApi.LK_NBLCK,
        WindowsLockApi.LK_UNLCK,
    ]
    assert known_hosts_path.read_text(encoding="utf-8") == "docker target\n"
    assert target.platform == "linux"
    assert target.repository_path == gateway_docker.TARGET_REPOSITORY
