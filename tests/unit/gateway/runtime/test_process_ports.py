import os
import socket
from types import SimpleNamespace

import pytest

from app.gateway.runtime import process_ports
from app.gateway.runtime.process_ports import (
    allocate_local_port_in_range,
    is_local_port_available,
    ssh_tunnel_port_range_from_env,
)


def test_allocate_local_port_in_range_scans_from_random_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed_ports: list[int] = []
    random_bounds: list[int] = []

    class FixedRandomStart:
        def randrange(self, stop: int) -> int:
            random_bounds.append(stop)
            return 2

    def is_available(port: int) -> bool:
        probed_ports.append(port)
        return port == 43000

    monkeypatch.setattr(process_ports.random, "SystemRandom", FixedRandomStart)
    monkeypatch.setattr(process_ports, "is_local_port_available", is_available)

    assert allocate_local_port_in_range(43000, 43003) == 43000
    assert random_bounds == [4]
    assert probed_ports == [43002, 43003, 43000]


def test_ssh_tunnel_port_range_reads_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN", "43000")
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX", "43010")

    assert ssh_tunnel_port_range_from_env() == (43000, 43010)


def test_ssh_tunnel_port_range_rejects_invalid_order(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN", "43010")
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX", "43000")

    with pytest.raises(ValueError, match="不能大于"):
        ssh_tunnel_port_range_from_env()


def test_allocate_local_port_in_range_skips_occupied_port():
    first_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        first_socket.bind(("127.0.0.1", 0))
        occupied_port = int(first_socket.getsockname()[1])
        free_port = None
        for candidate in (occupied_port - 1, occupied_port + 1):
            if candidate < 1 or candidate > 65535:
                continue
            probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe_socket.bind(("127.0.0.1", candidate))
                free_port = candidate
                break
            except OSError:
                continue
            finally:
                probe_socket.close()
        if free_port is None:
            pytest.skip("没有找到相邻可用端口用于范围分配测试")

        assert (
            allocate_local_port_in_range(
                min(occupied_port, free_port),
                max(occupied_port, free_port),
            )
            == free_port
        )
    finally:
        first_socket.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 端口复用语义")
def test_port_availability_probe_allows_reuse_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []

    class _ProbeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def setsockopt(self, level: int, option: int, value: int) -> None:
            calls.append((level, option, value))

        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 8010)

    monkeypatch.setattr(socket, "socket", lambda *_: _ProbeSocket())

    assert is_local_port_available(8010) is True
    assert calls == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


def test_port_availability_probe_requires_exclusive_binding_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []

    class _ProbeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def setsockopt(self, level: int, option: int, value: int) -> None:
            calls.append((level, option, value))

        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 8010)

    socket_api = SimpleNamespace(
        AF_INET=socket.AF_INET,
        SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET,
        SO_EXCLUSIVEADDRUSE=-5,
        socket=lambda *_: _ProbeSocket(),
    )
    monkeypatch.setattr(process_ports, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process_ports, "socket", socket_api)

    assert is_local_port_available(8010) is True
    assert calls == [(socket.SOL_SOCKET, -5, 1)]
