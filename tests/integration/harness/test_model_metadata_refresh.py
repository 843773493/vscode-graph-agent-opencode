"""真实 HTTP 与冷进程核对公共目录更新；不调用业务模型。"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.http_server import running_http_server

_COLD_PROCESS = r"""
import json, sys
import httpx
sync_requests = []
def reject_sync(url, *args, **kwargs):
    sync_requests.append(str(url))
    raise AssertionError('SDK 首次导入不允许同步下载元数据')
httpx.get = reject_sync
from app.runtime import litellm_runtime
sdk = litellm_runtime.load_litellm()
from app.runtime.network_proxy import ProxyRoutes
litellm_runtime.refresh_model_metadata(owner_key='workspace-under-test', routes=ProxyRoutes(()))
runtime = litellm_runtime._owner_runtimes['workspace-under-test'][1]
assert sync_requests == [], sync_requests
if sys.argv[1] == 'online':
    assert 'metadata-cold-process-test' not in litellm_runtime.local_model_catalog(owner_key='workspace-under-test')
    sdk.register_model({'metadata-local-registration': {
        'litellm_provider': 'openai', 'mode': 'chat', 'supports_vision': True,
    }})
    print('MODEL_METADATA ' + json.dumps({'phase': 'local_ready', 'new_catalog': False}), flush=True)
    assert runtime._finished.wait(15), '后台刷新未完成'
    assert 'metadata-cold-process-test' not in runtime._catalog, '后台不能直接变更在途目录'
    litellm_runtime.prepare_model_request()
    assert sdk.model_cost['metadata-local-registration']['supports_vision'] is True
else:
    assert runtime._worker is None, '新鲜缓存不应再次访问网络'
assert litellm_runtime.local_model_catalog(owner_key='workspace-under-test')['metadata-cold-process-test']['supports_vision'] is True
assert 'metadata-cold-process-test' not in sdk.model_cost
assert 'metadata-cold-process-test' not in litellm_runtime.local_model_catalog()
assert sync_requests == []
print('MODEL_METADATA ' + json.dumps({'phase': 'adopted', 'new_catalog': True, 'mode': sys.argv[1]}), flush=True)
"""


@pytest.fixture
def metadata_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[ThreadingHTTPServer, threading.Event, list[str]]]:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    from app.runtime.litellm_runtime import load_litellm

    catalog = copy.deepcopy(load_litellm().model_cost)
    catalog["metadata-cold-process-test"] = {"litellm_provider": "openai", "mode": "chat", "supports_vision": True}
    payload = json.dumps(catalog).encode()
    release = threading.Event()
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append(self.path)
            assert release.wait(20), "主流程未先取得旧目录"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as server:
        try:
            yield server, release, requests
        finally:
            release.set()


def test_late_http_catalog_is_adopted_next_request_and_restored_offline(
    metadata_server: tuple[ThreadingHTTPServer, threading.Event, list[str]],
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    server, release, requests = metadata_server
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    environment = dict(os.environ)
    environment.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    environment["BOXTEAM_HOME"] = str(tmp_path / "boxteam-home")
    environment["LITELLM_MODEL_COST_MAP_URL"] = f"http://127.0.0.1:{server.server_port}/catalog.json"
    command = [sys.executable, "-c", _COLD_PROCESS]
    with subprocess.Popen(
        [*command, "online"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment, cwd=Path.cwd(),
    ) as process:
        initial = remaining = errors = ""
        try:
            assert process.stdout is not None
            while line := process.stdout.readline():
                initial += line
                if line.startswith("MODEL_METADATA "):
                    assert json.loads(line.removeprefix("MODEL_METADATA ")) == {"phase": "local_ready", "new_catalog": False}
                    break
            else:
                raise AssertionError("冷进程未返回本地目录就绪回执")
            release.set()
            remaining, errors = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                remaining, errors = process.communicate(timeout=5)
            (context.artifacts_dir / "online.stdout.raw").write_text(initial + remaining)
            (context.artifacts_dir / "online.stderr.raw").write_text(errors)
        assert process.returncode == 0, errors
        reports = [json.loads(line.removeprefix("MODEL_METADATA "))
                   for line in remaining.splitlines() if line.startswith("MODEL_METADATA ")]
        assert reports == [{"phase": "adopted", "new_catalog": True, "mode": "online"}]
    assert requests == ["/catalog.json"]
    server.shutdown()
    server.server_close()
    offline = subprocess.run(
        [*command, "offline"], capture_output=True, text=True, timeout=30,
        env=environment, cwd=Path.cwd(), check=False,
    )
    (context.artifacts_dir / "offline.stdout.raw").write_text(offline.stdout)
    (context.artifacts_dir / "offline.stderr.raw").write_text(offline.stderr)
    assert offline.returncode == 0, offline.stderr
    reports = [json.loads(line.removeprefix("MODEL_METADATA "))
               for line in offline.stdout.splitlines() if line.startswith("MODEL_METADATA ")]
    assert reports == [{"phase": "adopted", "new_catalog": True, "mode": "offline"}]
    assert requests == ["/catalog.json"]


@pytest.fixture
def metadata_proxies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from contextlib import ExitStack

    from app.runtime import litellm_runtime
    from app.runtime.network_proxy import ProxyRoutes
    from configs.network import ExplicitProxy

    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    sdk = litellm_runtime.load_litellm()
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP")
    monkeypatch.setattr(sdk, "model_cost_map_url", "http://metadata.invalid/catalog.json")
    monkeypatch.setenv("LITELLM_MODEL_COST_MAP_URL", sdk.model_cost_map_url)
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(litellm_runtime, "_runtime", None)
    monkeypatch.setattr(litellm_runtime, "_owner_runtimes", {})
    monkeypatch.setattr(litellm_runtime, "_retired_runtimes", {})
    release = threading.Event()
    proxies = []
    def handler(requests, received, index):
        catalog = dict(sdk.model_cost)
        catalog["metadata-proxy-test"] = {
            "litellm_provider": "openai", "mode": "chat", "supports_vision": index == 0,
            "max_input_tokens": 1000 + index,
        }
        catalog["metadata-owner-" + str(index)] = {"litellm_provider": "openai", "mode": "chat"}
        payload = json.dumps(catalog).encode()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                pass
            def do_GET(self):
                requests.append((self.path, self.headers.get("Proxy-Authorization")))
                received.set()
                assert release.wait(10), "未释放元数据下载"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        return Handler
    with ExitStack() as cleanup:
        for index in range(2):
            requests, received = [], threading.Event()
            server = cleanup.enter_context(running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), handler(requests, received, index))))
            routes = ProxyRoutes.from_configuration(ExplicitProxy.model_validate({
                "mode": "explicit", "url": f"http://127.0.0.1:{server.server_port}",
                "auth": {"username": "workspace-" + str(index), "password": "secret:sec_" + "1" * 32},
            }), password="proxy-secret")
            proxies.append((routes, requests, received))
        try:
            yield proxies, release
        finally:
            release.set()
            import asyncio
            for owner in list(litellm_runtime._owner_runtimes):
                asyncio.run(litellm_runtime.release_model_metadata(owner))


def test_workspace_metadata_downloads_use_own_proxy_and_do_not_block_local_reads(metadata_proxies):
    from app.runtime import litellm_runtime
    proxies, release = metadata_proxies
    for index, (routes, _requests, received) in enumerate(proxies):
        litellm_runtime.refresh_model_metadata(owner_key="workspace-" + str(index), routes=routes)
        assert received.wait(5)
        litellm_runtime.refresh_model_metadata(owner_key="workspace-" + str(index), routes=routes)
    assert litellm_runtime.local_model_catalog()
    assert all(len(requests) == 1 for _routes, requests, _received in proxies)
    release.set()
    for _revision, runtime in litellm_runtime._owner_runtimes.values():
        assert runtime._finished.wait(5)
    from app.runtime.model_metadata import model_metadata
    for index in range(2):
        catalog = litellm_runtime.local_model_catalog(owner_key="workspace-" + str(index))
        assert "metadata-owner-" + str(index) in catalog
        assert "metadata-owner-" + str(1 - index) not in catalog
        assert model_metadata("openai", "metadata-proxy-test", owner_key="workspace-" + str(index)) == {
            "supports_vision": index == 0, "max_input_tokens": 1000 + index,
        }
    assert "metadata-proxy-test" not in litellm_runtime.load_litellm().model_cost
    assert "metadata-proxy-test" not in litellm_runtime.local_model_catalog()
    assert proxies[0][1] == [("http://metadata.invalid/catalog.json", "Basic d29ya3NwYWNlLTA6cHJveHktc2VjcmV0")]
    assert proxies[1][1] == [("http://metadata.invalid/catalog.json", "Basic d29ya3NwYWNlLTE6cHJveHktc2VjcmV0")]
    assert len({runtime._cache_path for _revision, runtime in litellm_runtime._owner_runtimes.values()}) == 2
