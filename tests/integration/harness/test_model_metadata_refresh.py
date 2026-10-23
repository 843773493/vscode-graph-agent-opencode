"""真实 HTTP 与冷进程核对公共目录更新；不调用业务模型。"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext

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
assert sync_requests == [], sync_requests
if sys.argv[1] == 'online':
    assert 'metadata-cold-process-test' not in sdk.model_cost
    sdk.register_model({'metadata-local-registration': {
        'litellm_provider': 'openai', 'mode': 'chat', 'supports_vision': True,
    }})
    print('MODEL_METADATA ' + json.dumps({'phase': 'local_ready', 'new_catalog': False}), flush=True)
    assert litellm_runtime._runtime._finished.wait(15), '后台刷新未完成'
    assert 'metadata-cold-process-test' not in sdk.model_cost, '后台不能直接变更在途目录'
    litellm_runtime.prepare_model_request()
    assert sdk.model_cost['metadata-local-registration']['supports_vision'] is True
else:
    assert litellm_runtime._runtime._worker is None, '新鲜缓存不应再次访问网络'
assert sdk.model_cost['metadata-cold-process-test']['supports_vision'] is True
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

    with ExitStack() as lifetime:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        lifetime.callback(server.server_close)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def join() -> None:
            worker.join(timeout=5)
            assert not worker.is_alive()

        lifetime.callback(join)
        lifetime.callback(server.shutdown)
        lifetime.callback(release.set)
        yield server, release, requests


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
