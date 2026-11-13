"""两个真实 Gateway 的模型覆盖、原文件分页及旧代际拒绝。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.types import ASGIApp

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_file_model_coverage import (
    file_search_web_assets as file_search_web_assets,  # noqa: PLC0414
)
from tests.integration.gateway import test_target_gateway_routing as gateway_support
from tests.integration.gateway.test_target_gateway_routing import TargetGatewayNetwork
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_port as target_gateway_port,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import GatewayProcess, start_gateway_process


def create_coverage_gateway_app() -> ASGIApp:
    from app.gateway.search import service
    from app.gateway.search.scheduler import SearchBranchError, SearchJob
    from app.gateway.search.transport import SearchTransport
    from app.schemas.internal_v2.search import WorkspaceSearchResultDTO

    class RetryReadFailure(SearchTransport):
        async def _read(self, job: SearchJob, url: str, headers: dict[str, str]) -> WorkspaceSearchResultDTO:
            # 只在具名查询的本地出站边界失败；远端 B、匹配、调度和 SSE 仍走产品。
            if job.request.query == "retrycoverage" and job.key.owner is not None and job.key.owner.gateway_id == self.gateway_id:
                raise SearchBranchError("owner_http_error", "模型代际验收的具名出站读取失败", retryable=True)
            return await super()._read(job, url, headers)

    service.SearchTransport = RetryReadFailure
    return gateway_support.create_gateway_app()


@pytest.fixture(autouse=True)
def remote_model_environment(
    request: pytest.FixtureRequest, file_search_web_assets: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ModelStreamReplay]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(file_search_web_assets))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")

    def start_observed_gateway(*, asgi_app: str, **arguments: object) -> GatewayProcess:
        del asgi_app
        return start_gateway_process(asgi_app=f"{__name__}:create_coverage_gateway_app", **arguments)

    monkeypatch.setattr(gateway_support, "start_gateway_process", start_observed_gateway)
    with model_stream_replay(context.artifacts_dir, runtime_root=context.runtime_root_for_node(request.node.nodeid),
                            business_assertion="remote-model-coverage-zero-execution") as replay:
        for name, value in replay.environment.items():
            monkeypatch.setenv(name, value)
        yield replay


def test_remote_model_coverage_precedes_quota_and_keeps_query_generation(
    request: pytest.FixtureRequest, target_gateway_network: TargetGatewayNetwork,
    remote_model_environment: ModelStreamReplay, file_search_web_assets: Path,
) -> None:
    network = target_gateway_network
    context = network.context
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "双 Gateway 模型覆盖需要 Chromium"
    originals = {}
    for index, workspace in enumerate(network.workspace_roots):
        files = {
            "a.coverage.txt": "coverageword A-OLD-DISK\n" * 20 if index == 0 else
                "coverageword B-SAME-PATH\nfirstprogress B-REMOTE\nretrycoverage B-SUCCESS\n",
        }
        if index == 0:
            files.update({"b.coverage.txt": "coverageword A-FIRST\ncoverageword A-SECOND\nfirstprogress A-REMOTE\nretrycoverage A-REMOTE\n",
                          "c.coverage.txt": "coverageword A-LAST\nfirstprogress A-TAIL\n"})
        for name, content in files.items():
            path = workspace / name
            path.write_text(content, encoding="utf-8")
            originals[path] = content
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    registered = network.clients[0].post("/api/gateway/remote-gateways", json={
        "name": "模型覆盖远端 B", "host": "127.0.0.1", "port": 22, "username": "model-coverage",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    })
    registered.raise_for_status()
    assert registered.json()["request_id"] == registered.headers["X-Request-ID"]
    result_path = context.artifacts_dir / "remote-model-coverage.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "240", "--",
               "node", "tests/integration/clients/web/quick_access/remote_model_coverage.mjs"]
    fixture = {"baseUrl": str(network.clients[0].base_url).rstrip("/"), "gatewayIds": network.gateway_ids,
               "workspaceId": network.workspace_id, "resultPath": str(result_path),
               "screenshotPath": str(context.artifacts_dir / "remote-model-coverage.png")}
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        process = subprocess.run(command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_MODEL_COVERAGE_FIXTURE": json.dumps(fixture),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium}, capture_output=True, timeout=255, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_bytes(process.stdout)
    (context.artifacts_dir / "browser.stderr.raw").write_bytes(process.stderr)
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": process.returncode, "assets": str(file_search_web_assets),
        "main_sha256": hashlib.sha256((file_search_web_assets / "out/main.js").read_bytes()).hexdigest(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert process.returncode == 0, result_path.read_text(encoding="utf-8")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[key] for key in ("remotePaging", "onlyA", "onlyOpen", "firstProgress", "staleCursor", "staleRetry"))
    assert result["forbiddenWrites"] == []
    observations = {role: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                    for role, path in network.observation_paths.items()}
    for receipt in result["receipts"]:
        request_id = receipt["requestId"]
        for branch in receipt["summary"]["branches"]:
            owner = branch["branch"]["owner"]
            index = network.gateway_ids.index(owner["gateway_id"])
            delivered = [item for item in observations[f"backend-{'ab'[index]}"]
                         if item["request_id"] == request_id and item["path"] == "/api/v1/search"]
            assert owner["workspace_id"] == network.workspace_id
            if branch["state"] == "failed":
                assert receipt["query"] == "retrycoverage" and index == 0 and delivered == []
            else:
                assert delivered and all(item["status"] == 200 and item["response_request_id"] == request_id for item in delivered)
            if index == 1:
                assert any(item["request_id"] == request_id and item["target_gateway_id"] == network.gateway_ids[1]
                           for item in observations["gateway-b"])
        if receipt["phase"] == "only-a":
            assert not any(item["request_id"] == request_id for item in observations["backend-b"])
    with remote_model_environment.control._condition:
        calls = {key: value for key, value in remote_model_environment.control._counters.items() if key.startswith("model_call/")}
    assert sum(calls.values()) == 0, calls
    for path, content in originals.items():
        assert path.read_text(encoding="utf-8") == content
