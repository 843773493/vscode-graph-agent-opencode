from __future__ import annotations

import socket
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import pytest

from tests.support.processes import workspace as processes


@dataclass
class PortProbeBoundary:
    platform: str
    listener: MagicMock
    api: SimpleNamespace


@pytest.fixture(params=["linux", "darwin", "win32"])
def port_probe_boundary(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> PortProbeBoundary:
    listener = MagicMock(spec=socket.socket)
    listener.__enter__.return_value = listener
    api = SimpleNamespace(
        AF_INET=socket.AF_INET,
        AF_INET6=socket.AF_INET6,
        SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET,
        IPPROTO_IPV6=socket.IPPROTO_IPV6,
        IPV6_V6ONLY=socket.IPV6_V6ONLY,
        SO_REUSEADDR=socket.SO_REUSEADDR,
        SO_EXCLUSIVEADDRUSE=-5,
        socket=Mock(return_value=listener),
        getaddrinfo=Mock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 12001)),
        ]),
    )
    platform = str(request.param)
    # 显式模拟系统socket边界，不改变宿主平台或监听真实端口。
    monkeypatch.setattr(processes, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(processes, "socket", api)
    return PortProbeBoundary(platform, listener, api)


def test_occupied_port_is_rejected_even_when_windows_listener_allows_reuse(
    port_probe_boundary: PortProbeBoundary,
) -> None:
    boundary = port_probe_boundary
    occupied = OSError("已有监听者持有测试端口")

    def bind(_address: tuple[str, int]) -> None:
        # Windows的REUSEADDR允许重复绑定；其他平台或独占检查拒绝当前监听者。
        if boundary.platform == "win32" and call(
            boundary.api.SOL_SOCKET, boundary.api.SO_REUSEADDR, 1,
        ) in boundary.listener.setsockopt.call_args_list:
            return
        raise occupied

    boundary.listener.bind.side_effect = bind

    with pytest.raises(RuntimeError, match=r"探针服务.*port=12001") as failure:
        processes.ensure_local_port_available(12001, service_name="探针服务")

    assert failure.value.__cause__ is occupied
    boundary.listener.__exit__.assert_called_once()


def test_free_port_probe_releases_its_socket(
    port_probe_boundary: PortProbeBoundary,
) -> None:
    processes.ensure_local_port_available(12001, service_name="探针服务")

    port_probe_boundary.api.socket.assert_called_once_with(
        port_probe_boundary.api.AF_INET, port_probe_boundary.api.SOCK_STREAM,
    )
    port_probe_boundary.listener.bind.assert_called_once_with(("127.0.0.1", 12001))
    port_probe_boundary.listener.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("host,address", [
    ("127.0.0.2", ("127.0.0.2", 12001)),
    ("::1", ("::1", 12001, 0, 0)),
    ("::", ("::", 12001, 0, 0)),
])
def test_explicit_host_conflict_preserves_original_error_and_releases_probe(
    port_probe_boundary: PortProbeBoundary,
    host: str,
    address: tuple[str, int] | tuple[str, int, int, int],
) -> None:
    boundary = port_probe_boundary
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    boundary.api.getaddrinfo.return_value = [
        (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address),
    ]
    occupied = OSError("目标地址已被监听")
    boundary.listener.bind.side_effect = occupied
    with pytest.raises(RuntimeError, match=f"host={host}, port=12001") as failure:
        processes.ensure_local_port_available(12001, service_name="探针服务", host=host)
    assert failure.value.__cause__ is occupied
    boundary.api.getaddrinfo.assert_called_once_with(host, None, type=socket.SOCK_STREAM)
    boundary.api.socket.assert_called_once_with(family, socket.SOCK_STREAM)
    boundary.listener.bind.assert_called_once_with(address)
    boundary.listener.__exit__.assert_called_once()
    if family == socket.AF_INET6:
        boundary.listener.setsockopt.assert_any_call(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)


def test_hostname_checks_each_resolved_address_without_falling_back_on_conflict(
    port_probe_boundary: PortProbeBoundary,
) -> None:
    boundary = port_probe_boundary
    addresses = [("::1", 12001, 0, 0), ("127.0.0.1", 12001)]
    boundary.api.getaddrinfo.return_value = [
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", addresses[0]),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", addresses[1]),
    ]
    occupied = OSError("名称解析的第二个地址已被监听")
    boundary.listener.bind.side_effect = [None, occupied]
    with pytest.raises(RuntimeError, match="host=localhost") as failure:
        processes.ensure_local_port_available(12001, service_name="探针服务", host="localhost")
    assert failure.value.__cause__ is occupied
    assert boundary.listener.bind.call_args_list == [call(address) for address in addresses]
    assert boundary.listener.__exit__.call_count == 2
