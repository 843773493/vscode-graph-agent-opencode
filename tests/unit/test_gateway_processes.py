import socket

import pytest

from app.gateway.processes import (
    allocate_local_port_in_range,
    ssh_tunnel_port_range_from_env,
)


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
