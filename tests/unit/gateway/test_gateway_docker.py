from __future__ import annotations

import portalocker

from tests.e2e.system.gateway import gateway_docker


def test_docker_target_setup_holds_portable_lock_during_provisioning(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    events = []
    lock_attempts = 0

    def acquire(_lock_file, flags):
        nonlocal lock_attempts
        assert flags == portalocker.LOCK_EX | portalocker.LOCK_NB
        lock_attempts += 1
        events.append(("lock", flags))
        if lock_attempts == 1:
            raise portalocker.AlreadyLocked("test lock is held")

    def release(lock_file):
        events.append(("unlock", lock_file.name))

    monkeypatch.setattr(gateway_docker.portalocker, "lock", acquire)
    monkeypatch.setattr(gateway_docker.portalocker, "unlock", release)
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
        ("lock", portalocker.LOCK_EX | portalocker.LOCK_NB),
        ("lock", portalocker.LOCK_EX | portalocker.LOCK_NB),
        ("command", "provision", ()),
        ("command", "sync", ("--activate",)),
        ("command", "bootstrap", ()),
        ("unlock", str(lock_path)),
    ]
    assert known_hosts_path.read_text(encoding="utf-8") == "docker target\n"
    assert target.platform == "linux"
    assert target.repository_path == gateway_docker.TARGET_REPOSITORY
