from __future__ import annotations

import errno
import json
import socket
import struct
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="仅核验 Linux 的自动临时端口分配与复用语义")


@pytest.fixture
def port_case(request: pytest.FixtureRequest) -> Iterator[tuple[ExitStack, Path]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with ExitStack() as resources:
        yield resources, context.artifacts_dir


def automatic_client(resources: ExitStack, port: int) -> socket.socket:
    client = resources.enter_context(socket.socket())
    client.settimeout(1)
    # Linux 6.3 起支持每个 socket 单独限制自动端口；不改宿主网络配置。
    try:
        client.setsockopt(socket.IPPROTO_IP, 51, struct.pack("I", port | port << 16))
    except OSError as error:
        if error.errno == errno.ENOPROTOOPT:
            pytest.skip("内核不支持 IP_LOCAL_PORT_RANGE，未验证自动分配竞争")
        raise
    return client


def test_lease_excludes_automatic_clients_while_real_service_can_listen(
    port_case: tuple[ExitStack, Path],
) -> None:
    resources, artifacts = port_case
    peer = resources.enter_context(socket.socket())
    peer.bind(("127.0.0.1", 0))
    peer.listen()
    peer.settimeout(1)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    lease = E2EProcessLease(artifacts / "process-lease.json", (port,))
    lease.acquire()
    resources.callback(lease.release)

    with pytest.raises(OSError) as occupied:
        automatic_client(resources, port).connect(peer.getsockname())
    assert occupied.value.errno == errno.EADDRNOTAVAIL
    with socket.socket() as service:
        service.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        service.bind(("127.0.0.1", port))
        service.listen()
        service.settimeout(1)
        with socket.socket() as client:
            client.settimeout(1)
            client.connect(service.getsockname())
            accepted, _ = service.accept()
            with accepted:
                accepted.sendall(b"real-service-on-leased-port")
                assert client.recv(100) == b"real-service-on-leased-port"
                client.close()
                assert accepted.recv(1) == b""
    # 服务退出后、租约释放前，端口仍不进入自动客户端端口池。
    with pytest.raises(OSError) as retained:
        automatic_client(resources, port).connect(peer.getsockname())
    assert retained.value.errno == errno.EADDRNOTAVAIL
    resources.pop_all().close()
    assert not lease.path.exists()
    with ExitStack() as released:
        listener = released.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(1)
        client = automatic_client(released, port)
        client.connect(listener.getsockname())
        assert client.getsockname()[1] == port
        accepted, _ = listener.accept()
        released.enter_context(accepted)
    (artifacts / "port-lease-result.json").write_text(json.dumps({
        "port": port, "automatic_client_rejected": occupied.value.errno,
        "real_service_roundtrip": True, "retained_after_service": retained.value.errno,
        "automatic_client_after_release": True,
    }, indent=2) + "\n", encoding="utf-8")


def test_existing_listener_is_preserved_and_lease_is_not_created(
    port_case: tuple[ExitStack, Path],
) -> None:
    resources, artifacts = port_case
    service = resources.enter_context(socket.socket())
    service.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    service.bind(("127.0.0.1", 0))
    service.listen()
    service.settimeout(1)
    port = service.getsockname()[1]
    lease = E2EProcessLease(artifacts / "process-lease.json", (port,))
    with pytest.raises(RuntimeError, match="端口已被占用"):
        lease.acquire()
    assert not lease.path.exists()
    client = resources.enter_context(socket.socket())
    client.settimeout(1)
    client.connect(service.getsockname())
    accepted, _ = service.accept()
    resources.enter_context(accepted)
    accepted.sendall(b"existing-listener-alive")
    assert client.recv(100) == b"existing-listener-alive"


def test_pool_coordinates_same_file_across_processes_and_releases_after_crash(
    port_case: tuple[ExitStack, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from tests.support import ports
    from tests.support.ports import TestFilePorts

    resources, artifacts = port_case
    # 父进程与子进程使用同一测试文件及候选起点；必须由租约互斥，而非随机运气隔离。
    plan = TestFilePorts(Path(__file__).resolve(), artifacts / "shared-port-pool")
    resources.callback(plan.close)
    first = plan.port("gateway")
    probe = TestFilePorts(Path(__file__).resolve(), plan.lease_root)
    second = probe.port("gateway")
    probe.close()
    script = '''
import os, sys
from pathlib import Path
from tests.support import ports
ports.secrets.randbelow = lambda _size: 0
plan = ports.TestFilePorts(Path(sys.argv[1]), Path(sys.argv[2]), tuple((int(p), int(p)) for p in sys.argv[3:]))
selected = plan.port("gateway")
print(selected, flush=True)
os._exit(0)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script, str(plan.test_file), str(plan.lease_root), str(first), str(second)],
        capture_output=True, text=True, timeout=15, check=True, cwd=Path.cwd(),
    )
    (artifacts / "concurrent-allocation.json").write_text(json.dumps({
        "parent": first, "child": completed.stdout.strip(), "stderr": completed.stderr,
    }) + "\n", encoding="utf-8")
    assert int(completed.stdout.strip()) == second
    # os._exit 不执行 Python 清理；OS 释放锁/socket，PID 创建时间核验允许下一次取得旧记录。
    monkeypatch.setattr(ports.secrets, "randbelow", lambda _size: 0)
    after_crash = TestFilePorts(plan.test_file, plan.lease_root, ((second, second),))
    resources.callback(after_crash.close)
    assert after_crash.port("gateway") == second


def test_pool_skips_busy_port_and_hands_reservation_to_real_service(
    port_case: tuple[ExitStack, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from tests.support import ports
    from tests.support.ports import TestFilePorts

    resources, artifacts = port_case
    occupied = resources.enter_context(socket.socket())
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    busy = occupied.getsockname()[1]
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        selected = available.getsockname()[1]
    monkeypatch.setattr(ports.secrets, "randbelow", lambda _size: 0)
    plan = TestFilePorts(Path(__file__).resolve(), artifacts / "handoff-pool", ((busy, busy), (selected, selected)))
    resources.callback(plan.close)
    assert plan.port("gateway") == selected
    assert plan.port("gateway") == selected
    with pytest.raises(RuntimeError, match="端口池已耗尽.*attempts=2"):
        plan.port("other-service")
    with pytest.raises(OSError) as retained:
        automatic_client(resources, selected).connect(occupied.getsockname())
    assert retained.value.errno == errno.EADDRNOTAVAIL
    completed = subprocess.run(
        [sys.executable, "-c", '''
import socket, sys
with socket.socket() as service:
    service.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    service.bind(("127.0.0.1", int(sys.argv[1])))
    service.listen()
    print(service.getsockname()[1])
''', str(selected)],
        capture_output=True, text=True, timeout=10, check=True,
    )
    assert int(completed.stdout) == selected
    # 子服务关闭也不提前归还端口，支持原地址重启。
    with pytest.raises(OSError) as after_stop:
        automatic_client(resources, selected).connect(occupied.getsockname())
    assert after_stop.value.errno == errno.EADDRNOTAVAIL
    plan.close()
    with socket.socket() as reuse:
        reuse.bind(("127.0.0.1", selected))
