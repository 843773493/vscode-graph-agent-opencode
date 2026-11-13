"""真实远端 URI 经原 Workbench 打开、复制、恢复及登记路由重解。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.quick_access.resources.test_thread_references import (
    _configure_gateway_url,
    _interaction,
)
from tests.integration.gateway import (
    test_target_gateway_routing as target_gateway_support,
)
from tests.integration.gateway.test_target_gateway_routing import (
    ObservedHttp,
    TargetGatewayNetwork,
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_port as target_gateway_port,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    BackendProcess,
    start_backend_process,
    terminate_process,
)
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)


@pytest.fixture(autouse=True)
def remote_uri_backend_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    # 网络装具仍观察原 socket；本组真实模型执行沿既有受控 ID composition 精确回放。
    def start_controlled_backend(*, asgi_app: str, **arguments: object) -> BackendProcess:
        del asgi_app
        return start_backend_process(asgi_app=f"{__name__}:create_remote_uri_backend_app", **arguments)

    monkeypatch.setattr(target_gateway_support, "start_backend_process", start_controlled_backend)


def create_remote_uri_backend_app() -> ObservedHttp:
    from tests.support.e2e_control.backend_app import app

    return ObservedHttp(app)


@pytest.fixture(autouse=True)
def remote_uri_environment(
    request: pytest.FixtureRequest,
    resource_web_assets: Path,
    integration_workspace_config_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ModelStreamReplay]:
    """在双 Gateway 装具启动之前绑定唯一精确 model_stream。"""
    del resource_web_assets
    configuration_path = Path(integration_workspace_config_path)
    config = commentjson.loads(configuration_path.read_text(encoding="utf-8"))
    primary = next(item for item in config["llm"]["providers"] if item["id"] == "primary")
    primary.update({"model": "e2e-stub-model", "api_key": "remote-uri-fixture-key"})
    config["agents"]["default"]["model"] = {"primary_provider": "primary", "fallback_providers": []}
    configuration_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with model_stream_replay(
        context.artifacts_dir,
        runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="real-remote-gateway-resource-uri",
    ) as replay:
        for name, value in replay.environment.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", LOCAL_TOKEN_HEADERS["X-Local-Token"])
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
        yield replay


async def _prepare_history_runtime(client: httpx.Client, gateway_id: str, workspace_id: str) -> None:
    async with httpx.AsyncClient(base_url=str(client.base_url), headers=LOCAL_TOKEN_HEADERS, timeout=45) as asynchronous:
        await _configure_gateway_url(asynchronous, gateway_id=gateway_id, workspace_id=workspace_id,
                                     gateway_url=str(client.base_url).rstrip("/"))
        await enable_developer_tools_plugins(asynchronous, gateway_id=gateway_id, workspace_id=workspace_id,
                                            plugin_ids=("terminal", "browser"))


@pytest.mark.parametrize("scenario", ("connection-capacity", "resources"))
def test_remote_gateway_uri_keeps_owner_across_open_copy_restore_and_route_reuse(
    request: pytest.FixtureRequest,
    remote_uri_environment: ModelStreamReplay,
    target_gateway_network: TargetGatewayNetwork,
    prepared_developer_tools_package: Path,
    scenario: str,
) -> None:
    network = target_gateway_network
    context = network.context
    client_a, client_b = network.clients
    gateway_a, gateway_b = network.gateway_ids
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "真实远端 URI 页面需要 Chromium"
    for marker, workspace in zip(("A", "B"), network.workspace_roots, strict=True):
        (workspace / "same-uri.txt").write_text(f"REMOTE-TEXT-{marker} 😀\n", encoding="utf-8")
        (workspace / "AGENTS.md").write_text(f"# 相同名称\nREMOTE-AGENTS-{marker} 😀\n", encoding="utf-8")
    copy_prepared_developer_tools_package(prepared_developer_tools_package, network.gateway_homes[1])
    asyncio.run(_prepare_history_runtime(client_b, gateway_b, network.workspace_id))

    def register_remote(port: int) -> dict[str, object]:
        response = client_a.post("/api/gateway/remote-gateways", json={
            "name": "URI 远端 B", "host": "127.0.0.1", "port": 22, "username": "uri-integration",
            "private_key_path": topology["private_key"], "remote_gateway_port": port,
        })
        response.raise_for_status()
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"]
        target, = [item for item in payload["data"]["items"] if item["gateway_id"] == gateway_b]
        assert target["workspace_id"] == network.workspace_id
        return target

    original_port, changed_port = topology["ports"][3:5]
    registered = register_remote(original_port)
    connection_id = registered["remote_gateways"][0]["gateway_connection_id"]
    result_path = context.artifacts_dir / "remote-uri.json"
    screenshot_path = context.artifacts_dir / "remote-uri.png"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "330", "--",
               "node", "tests/integration/clients/web/quick_access/resources/remote_gateway_uri.mjs"]
    fixture = {
        "baseUrl": str(client_a.base_url).rstrip("/"), "gatewayA": gateway_a, "gatewayB": gateway_b,
        "workspaceId": network.workspace_id, "interaction": _interaction(network.workspace_roots[1]),
        "resultPath": str(result_path), "screenshotPath": str(screenshot_path), "scenario": scenario,
    }
    host_actions = []
    with ExitStack() as cleanup, chromium_temporary_environment(
        context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp",
    ) as temporary:
        stderr = cleanup.enter_context((context.artifacts_dir / "browser.stderr.raw").open("x", encoding="utf-8"))
        stdout = cleanup.enter_context((context.artifacts_dir / "browser.stdout.raw").open("x", encoding="utf-8"))
        process = subprocess.Popen(command, cwd=context.project_root, env={**os.environ, **temporary,
            "BOXTEAM_REMOTE_URI_FIXTURE": json.dumps(fixture, ensure_ascii=False),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1)

        def save_process_receipt() -> None:
            (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
                "command": command, "exit_code": process.returncode, "host_actions": host_actions,
                "workbench_assets": os.environ["BOXTEAM_WEB_ASSETS"],
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        cleanup.callback(save_process_receipt)
        cleanup.callback(terminate_process, process)
        assert process.stdout is not None and process.stdin is not None
        for line in process.stdout:
            stdout.write(line)
            stdout.flush()
            if not line.startswith("BOXTEAM_REMOTE_ACTION "):
                continue
            action = line.removeprefix("BOXTEAM_REMOTE_ACTION ").strip()
            if action == "stop":
                network.stop_gateway(1)
                receipt = {"action": action, "port": original_port, "stopped": True}
            elif action == "reuse":
                client_b = network.restart_gateway(1, changed_port, {})
                # 真实新 Gateway 占据旧 B 端口，后端是 A；旧登记必须拒绝它，不能信任端口。
                reused_log = context.artifacts_dir / "reused-gateway"
                reused_log.mkdir()
                replacement = start_gateway_process(
                    workspace_root=network.workspace_roots[0],
                    default_backend_url=f"http://127.0.0.1:{topology['ports'][0]}", port=original_port,
                    boxteam_home=context.runtime_root_for_node(request.node.nodeid) / "reused-gateway-home",
                    artifacts_dir=reused_log,
                    asgi_app="tests.integration.gateway.test_target_gateway_routing:create_gateway_app",
                    extra_env={"BOXTEAM_TEST_TARGET_HTTP_LOG": str(reused_log / "http.jsonl")},
                )
                cleanup.callback(close_gateway_process, replacement)
                with httpx.Client(base_url=f"http://127.0.0.1:{original_port}", headers=LOCAL_TOKEN_HEADERS,
                                  timeout=25, trust_env=False) as replaced:
                    replacement_id = replaced.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
                assert replacement_id not in network.gateway_ids
                rejected = client_a.post(f"/api/gateway/remote-gateways/{connection_id}/reconnect")
                assert rejected.status_code == 502, rejected.text
                replacement_http = [json.loads(item) for item in (reused_log / "http.jsonl").read_text(encoding="utf-8").splitlines()]
                discovery = [item for item in replacement_http if item["path"].startswith("/api/gateway/federation/")]
                assert {item["path"] for item in discovery} == {
                    "/api/gateway/federation/manifest", "/api/gateway/federation/workspaces",
                }
                assert all(item["status"] == 401 and item["request_id"] == item["response_request_id"]
                           for item in discovery), discovery
                receipt = {"action": action, "old_port": original_port, "new_port": changed_port,
                           "replacement_gateway_id": replacement_id, "status": rejected.status_code,
                           "header": rejected.headers["X-Request-ID"], "response": rejected.json(),
                           "replacement_http": replacement_http}
                assert receipt["header"] == receipt["response"]["request_id"]
            elif action == "rebind":
                deleted = client_a.delete(f"/api/gateway/remote-gateways/{connection_id}")
                deleted.raise_for_status()
                target = register_remote(changed_port)
                new_connection = target["remote_gateways"][0]["gateway_connection_id"]
                assert new_connection != connection_id
                receipt = {"action": action, "target": target, "old_connection": connection_id,
                           "new_connection": new_connection}
            else:
                raise AssertionError(f"未知浏览器进程协调动作：{action}")
            host_actions.append(receipt)
            process.stdin.write(json.dumps(receipt, ensure_ascii=False) + "\n")
            process.stdin.flush()
        process.wait(timeout=15)
    assert process.returncode == 0, result_path.read_text(encoding="utf-8")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == []
    if scenario == "connection-capacity":
        assert result["connectionCapacity"]
    else:
        assert all(result[name] for name in ("remoteText", "remoteReadonlySource", "lateOpenKeptOwnerAndGroup",
            "exactMainChild", "copyWithoutBody", "normalRestore", "offlineRejected", "reusedRouteRejected", "newRouteReopened"))
    assert result["forbiddenWrites"] == []
    # 浏览器请求与真实下游 socket 逐一对应，不能只凭编辑器标题判断归属。
    observations = {role: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                    for role, path in network.observation_paths.items()}
    for receipt in result["remoteBodyReceipts"]:
        request_id = receipt["requestId"]
        assert receipt["gatewayId"] == gateway_b and receipt["workspaceId"] == network.workspace_id
        assert receipt["status"] == 200 and request_id
        assert any(item["request_id"] == request_id and item["response_request_id"] == request_id
                   for item in observations["backend-b"])
        assert not any(item["request_id"] == request_id for item in observations["backend-a"])
    if scenario == "resources":
        assert [item["phase"] for item in result["rejectedReads"]] == ["remote-stopped", "old-route-reused"]
        for rejected in result["rejectedReads"]:
            request_id = rejected["receipt"]["requestId"]
            assert rejected["gatewayId"] == gateway_b and rejected["workspaceId"] == network.workspace_id
            assert rejected["receipt"]["status"] == 409
            assert any(item["response_request_id"] == request_id and item["status"] == 409
                       and item["target_gateway_id"] == gateway_b for item in observations["gateway-a"])
            assert not any(item["request_id"] == request_id
                           for role in ("backend-a", "backend-b") for item in observations[role])
        replacement_http = [json.loads(line) for line in (reused_log / "http.jsonl").read_text(encoding="utf-8").splitlines()]
        for item in replacement_http:
            if not item["path"].startswith("/api/v1/"):
                continue
            # 原后台目录同步仍可尝试旧登记；替代 Gateway 必须认证拒绝，不能交付业务 Backend。
            assert (item["method"], item["path"], item["status"]) == ("GET", "/api/v1/session-catalog/export", 401), item
            assert item["sender_gateway_id"] == gateway_a and item["target_gateway_id"] == gateway_b
            assert item["workspace_id"] == network.workspace_id
            request_id = item["request_id"]
            assert request_id and item["response_request_id"] == request_id
            assert not any(observed["request_id"] == request_id
                           for role in ("backend-a", "backend-b") for observed in observations[role])
    with remote_uri_environment.control._condition:
        calls = {key: count for key, count in remote_uri_environment.control._counters.items()
                 if key.startswith("model_call/")}
    assert sum(calls.values()) == (0 if scenario == "connection-capacity" else 1), calls
    for marker, workspace in zip(("A", "B"), network.workspace_roots, strict=True):
        assert (workspace / "same-uri.txt").read_text(encoding="utf-8") == f"REMOTE-TEXT-{marker} 😀\n"
        assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == f"# 相同名称\nREMOTE-AGENTS-{marker} 😀\n"
