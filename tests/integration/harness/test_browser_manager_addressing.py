"""Browser装具通过真实辅助服务验证监听目标、访问地址与端口预检一致。"""

from __future__ import annotations

import errno
import json
import socket
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock
from urllib.request import urlopen

import pytest

from tests.harness.python import browser_manager
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.support.processes.workspace import ensure_local_port_available, wait_for_http_ok


@pytest.fixture(params=[
    ("127.0.0.1", "127.0.0.1"),
    ("127.0.0.2", "127.0.0.2"),
    ("0.0.0.0", "127.0.0.1"),
    ("::1", "[::1]"),
    ("::", "[::1]"),
    ("localhost", "localhost"),
])
def backend_address(request: pytest.FixtureRequest) -> tuple[str, str]:
    host, url_host = request.param
    # 部分系统未配置额外回环地址或IPv6；只跳过明确的不支持，不换地址冒充通过。
    try:
        _free_port(host)
    except OSError as error:
        if host in {"127.0.0.2", "::1", "::"} and error.errno in {
            errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT,
        }:
            pytest.skip(f"当前宿主不支持测试监听地址{host}: {error}")
        raise
    return host, url_host


@pytest.fixture
def startup_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # 保留真实HTTP就绪入口，只缩短故障探针的观察预算，避免旧错误等待一分钟。
    def short_readiness(url: str, process: subprocess.Popen[str]) -> None:
        wait_for_http_ok(url, process, timeout_seconds=3)

    monkeypatch.setattr(browser_manager, "wait_for_http_ok", short_readiness)


def _free_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def test_browser_fixture_uses_its_backend_address_end_to_end(
    backend_address: tuple[str, str],
    startup_budget: None,
    integration_workspace_root_path: str,
) -> None:
    host, url_host = backend_address
    workspace = Path(integration_workspace_root_path)
    backend_port = _free_port(host)
    frontend_port = _free_port("127.0.0.1")
    while frontend_port == backend_port:
        frontend_port = _free_port("127.0.0.1")
    backend_url = f"http://{url_host}:{backend_port}"
    frontend_url = f"http://127.0.0.1:{frontend_port}"
    with ExitStack() as lifetime:
        handle = start_browser_manager_processes(
            workspace_root=workspace,
            artifacts_dir=workspace.parent / "artifacts",
            backend_port=backend_port,
            frontend_port=frontend_port,
            backend_host=host,
        )
        lifetime.callback(close_browser_manager_processes, handle)
        with urlopen(f"{backend_url}/health", timeout=1) as response:
            backend_health = json.load(response)
        assert backend_health["process_id"] == handle.backend_process.pid
        assert backend_health["browser_count"] == 0
        with urlopen(f"{frontend_url}/health", timeout=1) as response:
            frontend_health = json.load(response)
        assert frontend_health["backend_url"] == backend_url
        assert frontend_health["workspace_id"] == backend_health["workspace_id"]
        with urlopen(f"{frontend_url}/config.js", timeout=1) as response:
            config = response.read().decode("utf-8")
        assert f"window.BOXTEAM_BROWSER_BACKEND_URL = {json.dumps(backend_url)};" in config
        assert handle.backend_process.poll() is None
        assert handle.frontend_process.poll() is None
    assert handle.backend_process.poll() is not None
    assert handle.frontend_process.poll() is not None


def test_backend_port_check_preserves_listener_on_requested_address(
    backend_address: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, _ = backend_address
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    # localhost在这里保留IPv4监听者；真实启动场景另验系统的名称解析。
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        spawn = Mock(side_effect=AssertionError("占用端口必须在启动子进程前拒绝"))
        monkeypatch.setattr(browser_manager, "spawn_logged_process", spawn)
        with pytest.raises(RuntimeError, match="测试端口已被占用"):
            start_browser_manager_processes(
                workspace_root=tmp_path,
                artifacts_dir=tmp_path / "artifacts",
                backend_port=port,
                frontend_port=_free_port("127.0.0.1"),
                backend_host=host,
            )
        spawn.assert_not_called()
        connect_host = "::1" if host == "::" else "127.0.0.1" if host == "0.0.0.0" else host
        with socket.create_connection((connect_host, port), timeout=1):
            pass


@pytest.mark.parametrize("port", [-1, 65536, 70000])
def test_invalid_port_is_rejected_instead_of_probing_a_truncated_port(port: int) -> None:
    with pytest.raises(OverflowError, match="port must be 0-65535"):
        ensure_local_port_available(port, service_name="越界端口探针")
