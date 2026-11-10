from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hashing import sha256_jcs
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.services.business.session_context_resource import session_resource_uri
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式工作台构建装具
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    _publish as publish_mcp_plugin,
)
from tests.support.communication.federation import (
    CommunicationNode,
    open_communication_triad,
)
from tests.support.e2e_control.harness import E2EProcessLease, E2ETestControlHarness
from tests.support.model_stream.config import prepare_e2e_model_stream_config
from tests.support.plugin_package import install_plugin_async
from tests.support.ports import TestFilePortBlock, integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    reset_gateway_persistent_state,
)
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.sealed_request_audit import audit_sealed_requests
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)

SECTION8_NODE_PORT_OFFSETS = (0, 10, 40)
MCP_PLUGIN_VERSION = "0.1.0"


def _section8_mcp_plugin_package(path: Path, server_id: str, prepared_package: Path) -> str:
    with zipfile.ZipFile(prepared_package) as prepared, zipfile.ZipFile(path, "w") as archive:
        for entry in prepared.infolist():
            content = prepared.read(entry.filename)
            if entry.filename == "plugin.jsonc":
                manifest = commentjson.loads(content)
                manifest["package_id"] = f"boxteam.mcp-e2e-{server_id.replace('_', '-')}"
                manifest["name"] = f"BoxTeam MCP {server_id} test bridge"
                content = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
            archive.writestr(entry, content)
    return manifest["package_id"]


async def _wait_section8_mcp_plugins(node: CommunicationNode, package_ids: set[str]) -> None:
    async with asyncio.timeout(60):
        while True:
            response = await node.client.post("/api/gateway/service-plugins/status", json={
                "gateway_id": node.gateway_id,
            })
            response.raise_for_status()
            plugins = response.json()["data"]["plugins"]
            states = {plugin["binding"]["package_id"]: plugin["state"] for plugin in plugins
                      if plugin["binding"].get("workspace_id") == node.workspace_id}
            failed = {package_id: states[package_id] for package_id in package_ids
                      if states.get(package_id) == "failed"}
            assert not failed, f"MCP 测试插件启动失败: {failed}"
            if all(states.get(package_id) == "ready" for package_id in package_ids):
                return
            await asyncio.sleep(0.1)


@pytest.fixture(scope="module")
def section8_context(request: pytest.FixtureRequest) -> TestRunContext:
    context = TestRunContext.from_test_file(Path(request.node.path))
    context.boxteam_home = context.boxteam_home_for_node(request.node.nodeid)
    return context.prepare()


@pytest.fixture(scope="module")
def section8_output_root(section8_context: TestRunContext) -> Path:
    return section8_context.output_root


@pytest.fixture(scope="module")
def section8_runtime(request: pytest.FixtureRequest, section8_context: TestRunContext) -> Path:
    return section8_context.runtime_root_for_node(request.node.nodeid)


@pytest.fixture(scope="module", autouse=True)
def section8_ports(request: pytest.FixtureRequest, section8_context: TestRunContext) -> Iterator[TestFilePortBlock]:
    block = integration_port_block_for_file(Path(request.node.fspath))
    # 只检查实际服务和 SSH 隧道池；模型回放不再启动独立 HTTP 服务。
    offsets = (*(
        offset + step for offset in SECTION8_NODE_PORT_OFFSETS for step in (0, 1, 4, 5, 6)
    ), 60, *range(70, 80))
    probes: list[dict[str, object]] = []
    allocation = section8_context.artifacts_dir / "port-allocation.json"
    lease_path = section8_context.runtime_root_for_node(request.node.nodeid) / "test-control/process-lease.json"
    with ExitStack() as reservations:
        for start in range(block.start, block.end - max(offsets) + 1):
            selected = replace(block, start=start)
            lease = E2EProcessLease(lease_path, tuple(selected.port(offset) for offset in offsets))
            try:
                lease.acquire()
            except (RuntimeError, OSError) as error:
                cause = error if isinstance(error, OSError) else error.__cause__
                if not isinstance(cause, OSError) or (
                    cause.errno != errno.EADDRINUSE and getattr(cause, "winerror", None) != 10048
                ):
                    raise
                probes.append({"start": start, "available": False, "error": str(error)})
            else:
                reservations.callback(lease.release)
                probes.append({"start": start, "available": True, "ports": lease.ports})
            allocation.write_text(json.dumps({
                "test_file": str(block.test_file), "block_index": block.index,
                "block": {"start": block.start, "end": block.end}, "probes": probes,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if probes[-1]["available"]:
                yield selected
                return
        raise RuntimeError(f"会话生命周期测试端口块无可用服务布局: block={block}, allocation={allocation}")


@pytest.fixture(scope="module")
def section8_control(section8_runtime: Path) -> Iterator[E2ETestControlHarness]:
    harness = E2ETestControlHarness(section8_runtime / "test-control")
    harness.start()
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture(scope="module")
def section8_browser_temporary_directory(section8_runtime: Path) -> Iterator[str]:
    """短 fd 引用满足 Chromium socket 预算；实体仍在唯一正式 runtime 目录。"""
    temporary = section8_runtime / "test-control/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
    path = f"/proc/{os.getpid()}/fd/{descriptor}"
    assert Path(path).resolve() == temporary
    try:
        yield path
    finally:
        os.close(descriptor)


@pytest.fixture(scope="module")
def integration_workspace_root_path(section8_output_root: Path) -> str:
    workspace = section8_output_root / "workspace/a/workspace"
    reset_gateway_persistent_state(workspace_root=workspace)
    return str(prepare_default_test_workspace(workspace_root=workspace, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", shared_skill_root=Path.cwd() / "resources/skills"))


@pytest.fixture(scope="module")
def section8_model_stream_runtime_config_path(
    section8_model_stream_config_path: str, section8_runtime: Path,
    section8_context: TestRunContext,
    section8_control: E2ETestControlHarness,
) -> str:
    fixture_root = section8_runtime / "model-stream-fixture"
    (fixture_root / "scenarios").mkdir(parents=True, exist_ok=True)
    cassette = json.loads((Path.cwd() / "tests/fixtures/model_stream/handwritten/openai_chat/basic-chat-tool-loop.json").read_text())
    # 启动 manifest 的 sentinel 不对应任何可执行 owner；每次真实 API 创建后由
    # 外置 harness 发布后续精确 identity，不按全局 request 顺序分发。
    cassette["interactions"] = [dict(cassette["interactions"][0], identity={
        "session_id": "ses_00000000400070008000000000000000",
        "thread_id": "thr_00000000400070008000000000000000",
        "model_call_id": "00000000-4000-7000-8000-000000000000",
    })]
    cassette_path = fixture_root / "calls.json"
    cassette_path.write_text(json.dumps(cassette, ensure_ascii=False, indent=2) + "\n")
    (fixture_root / "scenarios/basic-chat-tool-loop.jsonc").write_text(json.dumps({"scenario_id": "basic-chat-tool-loop", "asset": "calls.json", "business_assertion": "section8-product"}) + "\n")
    section8_control.bind_cassette(cassette_path)
    return str(prepare_e2e_model_stream_config(
        section8_model_stream_config_path,
        output_root=section8_context.artifacts_dir.parent,
        mode="replay", exact_fixture_root=fixture_root,
    ))


@pytest.fixture(scope="module")
def section8_model_stream_config_path() -> str:
    return str(
        Path.cwd()
        / "configs"
        / "tests"
        / "model_stream"
        / "model_stream_web_basic_chat_tool_loop.jsonc"
    )


@pytest.fixture
def section8_mcp_catalogs(section8_runtime: Path) -> dict[str, dict[str, Path]]:
    catalogs: dict[str, dict[str, Path]] = {}
    for node in ("a", "c"):
        directory = section8_runtime / "mcp" / node
        directory.mkdir(parents=True, exist_ok=True)
        catalogs[node] = {}
        for server in ("notify", "silent_a", "silent_b"):
            path = directory / f"{server}.json"
            path.write_text(json.dumps({"revision": 0, "tools": []}) + "\n")
            catalogs[node][server] = path
    return catalogs


@pytest.fixture
async def section8_product_nodes(
    request: pytest.FixtureRequest, section8_context: TestRunContext, section8_runtime: Path,
    section8_control: E2ETestControlHarness, integration_workspace_root_path: str,
    integration_workspace_config_path: str, section8_model_stream_runtime_config_path: str,
    web_workbench_assets: Path,
    section8_mcp_catalogs: dict[str, dict[str, Path]],
    section8_ports: TestFilePortBlock,
    prepared_developer_tools_package: Path,
    prepared_mcp_stdio_package: Path,
) -> AsyncIterator[dict[str, CommunicationNode]]:
    """唯一产品 owner 的三工作区；真实 SSH/channel，模型沿显式回放 transport。"""
    project = Path.cwd().resolve()
    context = section8_context
    context.artifacts_dir.mkdir(parents=True, exist_ok=True)
    phase_path = section8_context.artifacts_dir / "basic-chat-tool-loop-phases.log"
    with phase_path.open("a") as phase_output:
        print(f"{datetime.now(UTC).isoformat()} fixture_begin pid={os.getpid()}", file=phase_output, flush=True)

    def prepare(node_context: TestRunContext, _model_port: int, gateway_port: int) -> Path:
        if node_context.workspace_root == context.workspace_root / "a/workspace":
            workspace = Path(integration_workspace_root_path)
            config_path = Path(integration_workspace_config_path)
        else:
            workspace = prepare_default_test_workspace(
                workspace_root=node_context.workspace_root,
                template_root=project / "tests/fixtures/workspaces/default_test_workspace",
                shared_skill_root=project / "resources/skills",
            )
            config_path = install_test_workspace_config(
                workspace_root=workspace, config_path=project / "configs/tests/workspace/default.jsonc",
                schema_path=project / "configs/workspace_schema.jsonc",
            )
        config = commentjson.loads(config_path.read_text())
        # 保留协议资产的 Provider URL；精确回放 transport 隔离网络与个人凭据。
        for provider in config["llm"]["providers"]:
            provider["api_key"] = "model-stream-replay-unused-key"
            provider.pop("auth", None)
        config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{gateway_port}"
        if node_context.workspace_root.name == "workspace" and node_context.workspace_root.parent.name in {"a", "c"}:
            config["runtime"].setdefault("agent", {}).setdefault("run", {})["mode"] = "team"
        if node_context.workspace_root == context.workspace_root / "c/workspace":
            config.setdefault("context", {}).setdefault("resource_activation", {}).setdefault("overrides", {}).update({
                "team_state": "model_call", "mcp_tool_catalog": "model_call",
            })
        config["mcp"] = {"servers": {}}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        return workspace

    async with open_communication_triad(
        context, prepare, ports=section8_ports, node_port_offsets=SECTION8_NODE_PORT_OFFSETS,
        browser_plugin_nodes=("a", "b", "c"),
        prepared_developer_tools_package=prepared_developer_tools_package,
        gateway_runtime_root=section8_runtime / "gateways",
        backend_asgi_app="tests.support.e2e_control.backend_app:app",
        backend_environment=section8_control.environment | {
            "BOXTEAM_TEST_MODEL_STREAM_CONFIG": section8_model_stream_runtime_config_path,
            "PYTHONPROFILEIMPORTTIME": "1",
            # 本场景验证模型 transport，不验证远端价格表更新；使用 LiteLLM 包内表。
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        },
        gateway_environment={
            "BOXTEAM_WEB_ASSETS": str(web_workbench_assets),
        },
    ) as nodes:
        for name in ("a", "c"):
            node = nodes[name]
            node.client.headers.update({
                "X-BoxTeam-Target-Gateway-Id": node.gateway_id,
                "X-BoxTeam-Gateway-Id": node.gateway_id,
                "X-BoxTeam-Workspace-Id": node.workspace_id,
            })
            packages: dict[str, str] = {}
            runtime_entries = []
            plugin_values = {}
            mcp_servers = {}
            for server, catalog in section8_mcp_catalogs[name].items():
                package_id = _section8_mcp_plugin_package(
                    section8_runtime / f"mcp-{name}-{server}.zip",
                    f"{name}-{server}",
                    prepared_mcp_stdio_package,
                )
                packages[package_id] = server
                runtime_entries.append({"package_id": package_id, "package_version": MCP_PLUGIN_VERSION,
                                        "plugin_id": "stdio", "enabled": True})
                plugin_values[package_id] = {"stdio": {
                    "command": sys.executable,
                    "args": [str(project / "tests/support/e2e_control/mcp_stdio_server.py"),
                             "--catalog", str(catalog), *(["--notifications"] if server == "notify" else [])],
                    "cwd": str(project), "env": section8_control.environment,
                }}
                package = section8_runtime / f"mcp-{name}-{server}.zip"
                await install_plugin_async(lambda path, body, current=node: current.client.post(path, json=body),
                    gateway_id=node.gateway_id, path=str(package),
                    expected_digest=hashlib.sha256(package.read_bytes()).hexdigest())
                mcp_servers[server] = {"enabled": True, "transport": "plugin", "service": {
                    "gateway_id": node.gateway_id, "workspace_id": node.workspace_id,
                    "package_id": package_id, "package_version": MCP_PLUGIN_VERSION,
                    "plugin_id": "stdio", "service_id": "mcp",
                }}
            await publish_mcp_plugin(node, {"plugins": {
                "runtime": {"entries": runtime_entries},
                "configuration": {"values": plugin_values},
            }, "mcp": {"servers": mcp_servers}}, f"section8-mcp-plugin-{name}")
            await _wait_section8_mcp_plugins(node, set(packages))
        for name, node in nodes.items():
            section8_control.bind_process_control(name, node.restart_backend)
            section8_control.bind_trace_root(name, node.workspace)
        try:
            with phase_path.open("a") as phase_output:
                print(f"{datetime.now(UTC).isoformat()} fixture_yield pid={os.getpid()}", file=phase_output, flush=True)
            yield nodes
        finally:
            for name in nodes:
                section8_control.unbind_process_control(name)
                section8_control.unbind_trace_root(name)


@pytest.mark.asyncio
async def test_conversation_lifecycle_through_real_workbench(
    request: pytest.FixtureRequest,
    section8_context: TestRunContext,
    section8_control: E2ETestControlHarness,
    section8_product_nodes: dict[str, CommunicationNode],
    section8_browser_temporary_directory: str,
    section8_mcp_catalogs: dict[str, dict[str, Path]],
    section8_selection_contract: dict[str, object],
) -> None:
    del request
    project_root = Path.cwd().resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("完整会话 Workbench Integration 需要 Chromium")

    phase_path = section8_context.artifacts_dir / "basic-chat-tool-loop-phases.log"
    node = section8_product_nodes["a"]
    artifacts = section8_context.artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    sources = (
        "tests/integration/clients/web/conversation_lifecycle/test_conversation_lifecycle.py",
        "tests/integration/clients/web/conversation_lifecycle/conversation_lifecycle.mjs",
        "tests/integration/clients/web/conversation_lifecycle/generated_internal_history_oracle.mjs",
        "tests/integration/clients/web/conversation_lifecycle/fork_history_oracle.mjs",
        "tests/integration/clients/web/conversation_lifecycle/native_shutdown.mjs",
        "tests/integration/clients/web/conversation_lifecycle/diagnostic_response_bodies.mjs",
        "tests/support/communication/federation.py",
        "tests/support/processes/workspace.py",
    )
    (artifacts / "conversation-sources.json").write_text(json.dumps({
        "selection": section8_selection_contract,
        "sha256": {source: hashlib.sha256((project_root / source).read_bytes()).hexdigest() for source in sources},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result_path = artifacts / "basic-chat-tool-loop-result.json"
    screenshot_path = artifacts / "basic-chat-tool-loop-failure.png"
    browser_stdout_path = artifacts / "basic-chat-tool-loop-browser.log"
    browser_stderr_path = artifacts / "basic-chat-tool-loop-browser.stderr.log"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    try:
        async with httpx.AsyncClient(
                base_url=str(node.client.base_url),
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            active_workspace = workspaces.json()["data"]["active_workspace"]
            assert active_workspace == {"gateway_id": node.gateway_id, "workspace_id": node.workspace_id}
            assert len([
                item for item in workspaces.json()["data"]["items"]
                if (item["gateway_id"], item["workspace_id"]) == (node.gateway_id, node.workspace_id)
            ]) == 1, "活动 owner 必须属于唯一真实登记的 Gateway／工作区"
            workspace_id = active_workspace["workspace_id"]
            gateway_id = active_workspace["gateway_id"]

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": str(node.client.base_url).rstrip("/"),
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_GATEWAY_ID": gateway_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
                "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(artifacts),
                "TMPDIR": section8_browser_temporary_directory,
                "TMP": section8_browser_temporary_directory,
                "TEMP": section8_browser_temporary_directory,
                "BOXTEAM_BROWSER_NODES": json.dumps({
                    name: {"base_url": str(peer.client.base_url).rstrip("/"), "workspace_id": peer.workspace_id,
                           "gateway_id": peer.gateway_id, "workspace": str(peer.workspace),
                           "backend_stderr": str(peer.backend.stderr_file.name),
                           "uri_template": session_resource_uri(peer.workspace_id, "__SESSION__", gateway_authority=peer.gateway_id),
                           "mcp_catalogs": {server: str(path) for server, path in section8_mcp_catalogs.get(name, {}).items()}}
                    for name, peer in section8_product_nodes.items()
                }),
            }
        )
        environment.update(section8_control.environment)
        with phase_path.open("a") as phase_output, browser_stdout_path.open("w") as browser_stdout, browser_stderr_path.open("w") as browser_stderr, capture_test_processes():
            print(f"{datetime.now(UTC).isoformat()} browser_driver_begin pid={os.getpid()}", file=phase_output, flush=True)
            process = await asyncio.to_thread(
                subprocess.Popen,
                # TODO：Bun/JSC 预留映射会触达4GiB数据段保护；驱动统一用Node，不提高上限。
                ["node", "--expose-gc", "tests/integration/clients/web/conversation_lifecycle/conversation_lifecycle.mjs"],
                cwd=project_root,
                env=environment,
                stdout=browser_stdout,
                stderr=browser_stderr,
                text=True,
                start_new_session=True,
            )
            browser_process = ManagedProcess(process=process, log_file=browser_stdout)
            register_test_process(browser_process, browser_process.close)
            print(f"{datetime.now(UTC).isoformat()} browser_driver_process_begin pid={process.pid}", file=phase_output, flush=True)
            # default 实测运行一小时仍在推进 fork 矩阵；整套预算须覆盖后续场景。
            # 单项业务 deadline 仍由各 owner 约束，focus 保留原一小时上限。
            driver_seconds = 10800 if section8_selection_contract["mode"] == "default" else 3600
            returncode = await asyncio.to_thread(process.wait, timeout=driver_seconds)
            print(f"{datetime.now(UTC).isoformat()} browser_driver_end returncode={returncode}", file=phase_output, flush=True)
        if returncode != 0:
            # 先留存本 fixture 持有的进程；只对本测试 composition 的存活后端采集线程栈。
            owned_processes = []
            for name, peer in section8_product_nodes.items():
                for role, owner in (("backend", peer.backend), ("gateway", peer.gateway)):
                    if owner is None:
                        continue
                    observed_returncode = owner.process.poll()
                    if role == "backend" and observed_returncode is None and hasattr(signal, "SIGUSR1"):
                        owner.process.send_signal(signal.SIGUSR1)
                    owned_processes.append({
                        "node": name, "role": role, "pid": owner.process.pid,
                        "returncode": observed_returncode, "port": owner.port,
                        "workspace_id": peer.workspace_id, "gateway_id": peer.gateway_id,
                        "logs": {
                            label: {"path": str(log.name), "bytes": Path(log.name).stat().st_size,
                                    "handle_closed": log.closed}
                            for label, log in (("stdout", owner.stdout_file), ("stderr", owner.stderr_file))
                        },
                    })
            (artifacts / "basic-chat-tool-loop-process-failure.json").write_text(json.dumps({
                "observed_at_utc": datetime.now(UTC).isoformat(),
                "stage": "browser_exited_before_product_teardown", "browser_returncode": returncode,
                "owned_processes": owned_processes,
            }, ensure_ascii=False, indent=2) + "\n")
        assert returncode == 0, (
            "完整会话 Workbench Integration 失败:\n"
            f"stdout:\n{browser_stdout_path.read_text()}\n"
            f"stderr:\n{browser_stderr_path.read_text()}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        diagnostics = result["diagnostics"]
        assert diagnostics["pageErrors"] == []
        assert diagnostics["unhandledFailedRequests"] == []
        assert diagnostics["unhandledConsoleErrors"] == []
        handled = (
            diagnostics["handledHistoryConflicts"]
            + diagnostics["handledLifecycleErrors"]
            + diagnostics["handledMessageStreamErrors"]
            + diagnostics["handledIncompleteMessageStreamErrors"]
            + diagnostics["handledForkAttachmentDeletionConflicts"]
            + diagnostics["handledControlledRestartErrors"]
            + diagnostics["handledNativeShutdownLogs"]
            + diagnostics["handledNavigationFailures"]
        )
        assert sorted(fact["consoleIndex"] for fact in handled) == list(
            range(len(diagnostics["consoleErrors"])),
        )
        message_stream_responses = result["messageStreamResponses"]
        assert all(
            isinstance(response["pageId"], int)
            and isinstance(response["documentId"], int)
            and isinstance(response["requestedAt"], int)
            and isinstance(response["receivedAt"], int)
            and response["receivedAt"] >= response["requestedAt"]
            for response in message_stream_responses
        )
        assert all(
            "body" not in response
            and isinstance(response["messageStreamId"], str)
            and response["messageStreamId"]
            for response in message_stream_responses
            if response["status"] == 200
        ), "SSE 成功响应只记录 headers 与 target，不读取流 body"
        assert all(
            response.get("bodyBytes", 0) <= 4096
            for response in message_stream_responses
            if response["status"] == 404
        ), "404 diagnostic body 必须有界"
        requested = section8_selection_contract["requested"]
        selected = section8_selection_contract["selected"]
        assert isinstance(requested, list)
        assert isinstance(selected, list)
        assert result["focusedScenarios"] == requested
        assert set(result["section8"]) == set(selected)
        if "navigation_optimism" in selected:
            navigation = result["section8"]["navigation_optimism"]
            isolation = navigation["isolation"]
            baseline = isolation["before"]
            assert baseline["sources"] and baseline["toolSets"] and baseline["sealed"]
            assert baseline["runtime"]["built"] and baseline["runtime"]["acquired"]
            for stage in ("pending", "afterReload", "afterRestart"):
                assert isolation[stage] == baseline, f"导航 {stage} 不能修改上下文或物化运行时"
            for stage in ("pendingDelete", "rejectedDelete"):
                assert isolation[stage] == isolation["beforeRejectedDelete"]
            recovery = navigation["pendingRecovery"]
            assert recovery["killed"]["before_pid"] != recovery["restarted"]["after_pid"]
            assert all(receipt["state"] == "committed" for receipt in recovery["terminals"])
            assert navigation["deletion"]["rejectedReceipt"]["error_code"] == "source_retained_by_fork"
            repair = navigation["repair"]["repairReceipt"]
            assert repair["state"] == "committed" and repair["settlement"] == "repair_required"
            assert repair["error_code"] == "delete_settlement_repair_required"
        if "lifecycle_admission_deletion" in selected:
            cold = result["section8"]["lifecycle_admission_deletion"]["cold"]
            assert cold["guarded"]["phase"] == "history.read.guarded"
            assert cold["deleting"]["phase"] == "deletion.catalog_deleting"
            assert cold["deleting"]["at"] >= cold["guarded"]["at"]
            assert cold["metadata"][0]["state"] == "deleting"
            assert cold["fenceBeforeReadCompleted"] == [{"state": "active", "generation": 1}]
            assert cold["history"]["thread_id"] == cold["guarded"]["details"]["thread_id"]
            assert cold["journal"]["state"] == "completed"
        if "lifecycle_execution_deletion" in selected:
            lifecycle = result["section8"]["lifecycle_execution_deletion"]
            assert {(item["actor"], item["operationFirst"]) for item in lifecycle} == {
                (actor, first) for actor in ("execution", "rehydrate") for first in (True, False)
            }
            for item in lifecycle:
                assert [reply["target"] for reply in item["denied"]] == ["", f"/threads/{item['threadId']}"]
                for reply in item["denied"]:
                    assert reply["status"] == 409
                    assert reply["body"]["request_id"] == reply["requestId"]
                assert item["terminal"]["state"] == "committed"
                assert item["terminal"]["settlement"] == "settled"
                if not item["operationFirst"]:
                    assert item["after"] == item["before"]
                else:
                    assert item["deleting"]["at"] >= item["admitted"]["at"]
                    assert all(row["phase"] == "terminal" for row in item["after"]["queue"])
                if item["actor"] == "rehydrate":
                    assert len(item["cold"]["graph"]["closed"]) == 1
        captures = {}
        for path in sorted((artifacts / "model-stream").glob("*.json")):
            capture = json.loads(path.read_text(encoding="utf-8"))
            call = capture["identity"]["model_call_id"]
            assert call not in captures, f"transport产物包含重复ModelCall: {call}"
            captures[call] = capture
        audits = {}
        covered_calls = set()
        audit_targets = []
        for scenario, node_name in (
            ("basic_chat", "a"), ("child", "a"), ("team_turn", "a"),
            ("team_model_call", "c"), ("mcp_turn", "a"), ("mcp_model_call", "c"),
            ("background_mailbox", "a"), ("compaction", "a"),
        ):
            if scenario not in selected:
                continue
            facts = result if scenario == "basic_chat" else result["section8"][scenario]
            audit_targets.append((scenario, node_name, facts["sessionId"]))
        if "fork_successor_rewind" in selected:
            audit_targets.extend(
                ("fork_successor_rewind", owner["name"], owner["sessionId"])
                for owner in result["section8"]["fork_successor_rewind"]["auditOwners"]
            )
        for scenario, node_name, session in audit_targets:
            owner = section8_product_nodes[node_name]
            resolver = get_session_path_resolver(owner.workspace / ".boxteam/sessions")
            threads = {capture["identity"]["thread_id"] for capture in captures.values()
                       if capture["identity"]["session_id"] == session}
            assert threads, f"场景缺少真实transport请求: {scenario}/{session}"
            scenario_audits = audits.setdefault(scenario, {})
            for thread in sorted(threads):
                report = audit_sealed_requests(
                    resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite",
                    (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures,
                )
                assert thread not in scenario_audits, f"场景重复审计thread: {scenario}/{session}/{thread}"
                scenario_audits[thread] = report
                covered_calls.update(proof["model_call_id"] for proof in report["requests"])
            assert covered_calls.issuperset(
                call for call, capture in captures.items() if capture["identity"]["session_id"] == session
            ), f"场景存在未审计的thread请求: {scenario}/{session}"
        (artifacts / "basic-chat-tool-loop-provenance.json").write_text(
            json.dumps({"scenarios": audits, "covered_model_calls": sorted(covered_calls),
                        "outside_scope_model_calls": sorted(captures.keys() - covered_calls)},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        if "runtime_shared_resources" in selected:
            scope = result["section8"]["runtime_shared_resources"]
            assert scope["threadId"] != scope["parentThreadId"]
            assert scope["historyBefore"]["items"] == scope["coldHistory"]["items"]
            assert scope["historyBefore"]["internal_executions"] == scope["coldHistory"]["internal_executions"]
            assert scope["prefixBefore"] == scope["prefixAfter"]
            closed, = scope["closed"]["closed"]
            assert closed["details"]["scope_state"] == "closed"
            assert closed["details"]["session_id"] == scope["sessionId"]
            assert closed["details"]["thread_id"] == scope["threadId"]
            assert len(scope["closed"]["compiled"]) == 1
            assert scope["coldBeforeResume"]["residency"] == "cold"
            assert len(scope["rebuilt"]["compiled"]) == 2
            assert scope["consumerBefore"]["compiled"] == scope["consumerAfter"]["compiled"]
            assert scope["consumerAfter"]["closed"] == []
            watch = scope["watchPublished"]
            assert watch["at"] > closed["at"] and watch["pid"] == closed["pid"]
            assert watch["details"]["revision"] == scope["changedRevision"]
            assert watch["details"]["available"] is True and watch["details"]["subscriber_ids"]
            assert scope["changedMarker"] in json.dumps(scope["observedRequest"]["request"]["messages"], ensure_ascii=False)
            assert len(scope["resources"]) == 4
            assert {(resource["kind"], resource["retention_scope"]) for resource in scope["resources"]} == {
                (kind, retention) for kind in ("terminal", "browser") for retention in ("thread", "workspace")
            }
            for before, after in zip(scope["resources"], scope["externalAfter"], strict=True):
                assert before["resource_id"] == after["resource_id"]
                assert after["owner_revision"] > before["owner_revision"]
                assert after["retention_scope"] == before["retention_scope"]
                assert after["reconciliation_status"] == "verified"
                assert after["thread_associations"] == []
                assert after["status"] == ("deleted" if before["retention_scope"] == "thread" else "running")
                if before["retention_scope"] == "workspace":
                    fields = ("os_pid", "process_start_time", "pty_worker_pid") if before["kind"] == "terminal" else ("runtime_generation", "active_page_id")
                    for field in fields:
                        assert after["metadata"].get(field) == before["metadata"].get(field)
        if "child" in selected:
            child = result["section8"]["child"]
            assert child["startupHistory"]["thread_id"] == child["mainThreadId"]
            assert child["mainThreadId"] != child["threadId"]
            for name in ("initialHistory", "pending", "terminalHistory", "coldHistory", "coldDetail"):
                assert child[name]["thread_id"] == child["threadId"]
            trace = child["traceIdentity"]
            assert trace["aggregate"]["events"]
            for name, owner in (("child", child["threadId"]), ("main", child["mainThreadId"])):
                actual = trace[name]["events"]
                assert actual and {event["thread_id"] for event in actual} == {owner}
                assert {event["event_id"] for event in actual} == {
                    event["event_id"] for event in trace["aggregate"]["events"] if event["thread_id"] == owner
                }
            for name in ("wrongThread", "wrongScope", "wrongPurpose"):
                assert trace[name]["code"] == "trace_cursor_gone"
            assert child["startupHistory"]["items"] == []
            assert child["initialHistory"]["items"] == []
            (initial,) = child["initialHistory"]["internal_executions"]
            assert initial["session_id"] == child["sessionId"] and initial["thread_id"] == child["threadId"]
            panel = child["panelEvidence"]
            provenance = panel["reportNotice"]["provenance"]
            assert provenance["source_session_id"] == child["sessionId"]
            assert provenance["source_thread_id"] == child["threadId"]
            assert provenance["source_execution_id"] == initial["execution_id"]
            assert panel["sourceReport"]["thread_id"] == child["threadId"]
            assert [execution["execution_id"] for execution in panel["sourceReport"]["internal_executions"]] == [initial["execution_id"]]
            assert panel["sourceReport"]["items"] == []
            assert panel["selectedSessionId"] == child["sessionId"]
            assert panel["childDraft"] != panel["siblingDraft"]
            assert panel["mainTurnIds"] == [child[name]["sent"]["turnId"] for name in ("creation", "siblingCreation")]
            first, second = child["first"], child["second"]
            pending, terminal = child["pending"]["items"], child["terminalHistory"]["items"]
            assert len(pending) == len(terminal) == len(child["interleaved"]["turns"]) == 2
            assert pending[1]["status"] == "accepted"
            assert pending[0]["root_logical_item_ordinal"] < pending[1]["root_logical_item_ordinal"]
            for sent, queued, turn, displayed in zip((first, second), pending, terminal, child["interleaved"]["turns"], strict=True):
                assert sent["accepted"]["thread_id"] == child["threadId"]
                for actual in (queued, turn):
                    assert actual["session_id"] == child["sessionId"]
                    assert actual["turn_id"] == sent["turnId"] and actual["job_id"] == sent["jobId"]
                    assert actual["source_message_ids"] == [sent["accepted"]["message_id"]]
                    assert [item["message_id"] for item in actual["user_messages"]] == actual["source_message_ids"]
                    assert [item["content"] for item in actual["user_messages"]] == [sent["prompt"]]
                assert turn["status"] == "completed" and turn["activity_stats"]["item_count"] == 4
                assert displayed["turnId"] == turn["turn_id"] and displayed["rootOrdinal"] == turn["root_logical_item_ordinal"]
                assert displayed["activity"] == turn["activity_stats"]
                assert displayed["userMessages"] == [{"messageId": sent["accepted"]["message_id"], "content": sent["prompt"]}]
                coordinates = [{"part_id": part["part_id"], "kind": part["kind"], **part["source"]} for part in turn["response_parts"]]
                assert displayed["parts"] == [{key: part[key] for key in ("part_id", "kind", "item_id", "item_sequence", "logical_item_ordinal", "part_ordinal", "created_at", "elapsed_ms")} for part in coordinates]
                ordering = [(part["logical_item_ordinal"], part["part_ordinal"]) for part in displayed["parts"]]
                assert ordering == sorted(ordering)
            assert child["queuedDom"] == [{
                "stage": stage, "turnId": second["turnId"], "rootOrdinal": str(pending[1]["root_logical_item_ordinal"]),
                "messageId": second["accepted"]["message_id"],
                "content": second["prompt"], "placement": "composer-queue",
            } for stage in ("queued live", "queued refresh")]
            parts = child["interleaved"]["turns"][0]["parts"]
            assert any(part["item_sequence"] < child["secondRoot"] for part in parts)
            assert any(part["item_sequence"] > child["secondRoot"] for part in parts)
            assert all(part["item_sequence"] != child["secondRoot"] for part in parts)
            requests = child["requests"]
            assert len(requests["successors"]) == 2
            for request in [requests["firstContinuation"], *requests["successors"]]:
                assert request["identity"]["session_id"] == child["sessionId"] and request["identity"]["thread_id"] == child["threadId"]
            old_messages = json.dumps(requests["firstContinuation"]["request"]["messages"], ensure_ascii=False)
            assert first["prompt"] in old_messages and second["prompt"] not in old_messages
            assert all(second["prompt"] in json.dumps(request["request"]["messages"], ensure_ascii=False) for request in requests["successors"])
            assert child["coldHistory"]["items"] == terminal and child["coldDetail"]["items"] == terminal[:1]
            assert child["bindingBefore"] == child["bindingAfter"]
            runtime, graph = child["runtime"], child["graph"]
            assert runtime["withBlocker"]["execution_state"] == "active" and runtime["withBlocker"]["cold_eligible"] is False
            assert all(runtime[key]["residency"] == "resident" for key in ("withBlocker", "stillActive", "beforeDeadline"))
            assert runtime["coldRuntime"]["residency"] == "cold"
            assert [len(graph[key]["compiled"]) for key in ("initiallyResident", "reused", "released", "rebuilt")] == [1, 1, 1, 2]
            (closed,) = graph["released"]["closed"]
            generation = graph["initiallyResident"]["acquired"][0]["details"]["generation"]
            assert closed["details"]["generation"] == generation
            for sent in (first, second):
                (acquired,) = [event for event in graph["reused"]["acquired"] if event["details"]["job_id"] == sent["jobId"]]
                assert acquired["details"]["generation"] == generation
            (rebuilt,) = [event for event in graph["rebuilt"]["acquired"] if event["details"]["job_id"] == child["rehydrated"]["sent"]["jobId"]]
            assert rebuilt["details"]["generation"] > generation
            own, sibling = graph["initiallyResident"]["compiled"][0], graph["siblingGraph"]["compiled"][0]
            assert own["pid"] == sibling["pid"] and own["details"]["compiled_graph_object_id"] != sibling["details"]["compiled_graph_object_id"]
        if "compaction" in selected:
            compaction = result["section8"]["compaction"]
            recovery = compaction["activationRecovery"]
            refs = json.loads(recovery["control"]["payload_json"])["resource_activation_refs"]
            assert refs == recovery["refs"] and refs
            assert len({ref["assembly_id"] for ref in refs}) == len(refs)
            frozen = {row["assembly_id"]: row for row in recovery["frozenAtRestart"]}
            for ref in refs:
                assert ref == {key: value for key, value in frozen[ref["assembly_id"]].items() if key != "snapshot_json"}
            assert len(recovery["frozenBefore"]) >= len(compaction["prepared"]) == 3
            for row in recovery["frozenBefore"]:
                assert frozen[row["assembly_id"]] == row
                assert row["assembly_id"] in {ref["assembly_id"] for ref in refs}
            assert recovery["frozenAfter"] == recovery["frozenAtRestart"]
            assert recovery["killed"]["before_pid"] != recovery["restarted"]["after_pid"]
            assert len(recovery["evidence"]["turns"]) == 4
            assert compaction["summary"]["kind"] == "compaction_summary"
            assert "read_compaction_history(line_offset=1, limit=200)" in compaction["summary"]["text"]
        if "initial_admission_recovery" in selected:
            recovery = result["section8"]["initial_admission_recovery"]
            intent, bound, admission = recovery["intent"], recovery["bound"], recovery["admission"]
            assert intent["state"] == "pending" and bound["state"] == "bound"
            frozen_fields = ("admission_idempotency_key", "session_id", "thread_id", "creation_idempotency_key",
                             "initial_state", "execution_binding_id", "job_id", "binding_preimage_hash")
            assert {key: intent[key] for key in frozen_fields} == {key: bound[key] for key in frozen_fields}
            assert recovery["admissionKey"] == admission["admission_idempotency_key"] == intent["admission_idempotency_key"]
            assert intent["binding_preimage_hash"] == sha256_jcs(recovery["bindingPreimage"]).rsplit(":", 1)[1]
            assert admission["admission_payload_hash"] == sha256_jcs(recovery["requestPreimage"])
            assert admission["commit_status"] == "committed" and admission["execution_origin"] == "internal"
            assert admission["execution_id"] == intent["execution_binding_id"] and admission["job_id"] == intent["job_id"]
            assert admission["turn_id"] is None and admission["accepted_ingress_id"] is None
            assert recovery["recovered"] == [{
                "execution_id": intent["execution_binding_id"], "job_id": intent["job_id"], "turn_id": None,
                "accepted_ingress_id": None, "admission_idempotency_key": recovery["admissionKey"],
                "admission_payload_hash": admission["admission_payload_hash"], "outcome": "completed",
            }], "恢复只能完成原 admission，不能创建第二个 execution 或用户 Turn"
            assert recovery["commits"] == [{"metadata_json": admission["metadata_json"]}]
            assert recovery["killed"]["before_pid"] != recovery["restarted"]["after_pid"]
            assert recovery["displayed"]["turns"] == []
            (internal,) = recovery["displayed"]["internal"]
            for key in ("session_id", "thread_id", "execution_id", "job_id"):
                assert internal[key] == admission[key]
            assert internal["status"] == "completed"
            assert [part["text"] for part in internal["response_parts"] if part["kind"] in {"text", "final_text"}] == ["初始化报告的私有正文。"]
            (parent,) = recovery["parentTurn"]
            assert parent["thread_id"] != intent["thread_id"] and parent["session_id"] == intent["session_id"]
            terminal = recovery["parentTerminal"]
            assert terminal["execution_id"] == parent["execution_id"] and terminal["turn_id"] == parent["turn_id"]
            assert terminal["turn_status"] == "unknown" and terminal["outcome"] == "execution_lost"
        if "generated_internal_history" in selected:
            generated = result["section8"]["generated_internal_history"]
            admission, notice = generated["admission"], generated["canonicalNotice"]
            assert admission["session_id"] == generated["target"] and admission["execution_origin"] == "internal"
            assert admission["turn_id"] is None and admission["accepted_ingress_id"] is None
            assert admission["commit_status"] == "committed" and admission["commit_mode"] == "item_bearing"
            assert admission["jsonl_record_count"] == 1 and generated["roots"] == [{"turns": 1, "roots": 1}]
            assert admission["admission_payload_hash"] == sha256_jcs(generated["admissionPreimage"])
            assert json.loads(admission["metadata_json"]) == generated["admissionMetadata"]
            assert generated["admissionMetadata"]["request_input"] == {"kind": "runtime_notice", "payload": notice["payload"]}
            assert notice["content_hash"] == sha256_jcs({"payload_kind": notice["payload_kind"], "payload": notice["payload"]})
            assert len(generated["notices"]) == 1 and generated["notices"][0]["item_id"] == notice["item_id"]
            assert notice.get("turn_id") is None and notice["turn_scope"] == "pending_next_turn"
            internal = generated["internal"]
            assert internal["execution_id"] == admission["execution_id"] and internal["job_id"] == admission["job_id"]
            assert internal["session_id"] == admission["session_id"] and internal["thread_id"] == admission["thread_id"]
            (display,) = internal["display_parts"]
            assert display["item_id"] == notice["item_id"] and display["text"] == notice["payload"]["display_part"]["text"]
            assert display["kind"] == "generated_session_result" and display["provenance"]["source_session_id"] == generated["generated"]
            assert len(generated["membership"]) > 256 and len(generated["pages"]) > 1
            paged_parts = []
            for page in generated["pages"]:
                assert page["items"] == []
                (entry,) = page["internal_executions"]
                assert entry["execution_id"] == admission["execution_id"] and entry["status"] == "completed"
                records = {part["item_id"] for part in entry["display_parts"]}
                records.update(part["source"]["item_id"] for part in entry["response_parts"])
                assert len(records) <= 256
                paged_parts.extend(entry["response_parts"])
            assert len({part["part_id"] for part in paged_parts}) == len(paged_parts)
            assert sorted(paged_parts, key=lambda part: part["part_id"]) == sorted(internal["response_parts"], key=lambda part: part["part_id"])
            assert generated["snapshot"]["stale"]["status"] == 409
            assert generated["snapshot"]["stale"]["body"]["detail"]["code"] == "stale_turn_cursor"
            assert generated["crossOwner"]["status"] == 400
            assert generated["head"]["zero"]["items"] == []
            assert len(generated["head"]["zero"]["internal_executions"]) == 1
            assert generated["head"]["history"]["internal_executions"] == generated["head"]["zero"]["internal_executions"]
        if "background_mailbox" in selected:
            mailbox = result["section8"]["background_mailbox"]
            assert mailbox["mainThreadId"] != mailbox["threadId"]
            before, after = mailbox["beforeTask"], mailbox["afterTask"]
            assert before["task_id"] == after["task_id"] == mailbox["taskId"]
            assert before["thread_id"] == after["thread_id"] == mailbox["threadId"]
            assert before["lease_id"] == after["lease_id"]
            assert before["descriptor_json"] == after["descriptor_json"]
            assert before["status"] == "running" and after["status"] == "completed"
            assert after["fencing_token"] > before["fencing_token"]
            assert json.loads(before["checkpoint_json"])["next_index"] == 1
            assert json.loads(after["checkpoint_json"])["next_index"] == 2
            assert mailbox["activeLease"]["state"] == "active"
            assert mailbox["settledLease"]["state"] == "completed"
            assert mailbox["killed"]["before_pid"] != mailbox["restarted"]["after_pid"]
            expected_ids = [f"{mailbox['taskId']}:message:{index}" for index in range(2)]
            assert [item["message_id"] for item in mailbox["firstMailbox"]] == expected_ids[:1]
            assert [item["message_id"] for item in mailbox["mailbox"]] == expected_ids
            assert all(item["thread_id"] == mailbox["threadId"] and item["session_id"] == mailbox["sessionId"]
                       for item in mailbox["mailbox"])
            assert mailbox["blockedRuntime"]["residency"] == "resident"
            assert any(blocker["kind"] == "background_task" for blocker in mailbox["blockedRuntime"]["blockers"])
            blocker = mailbox["blockerEvidence"]
            assert blocker["reasons"] == [entry["reason"] for entry in mailbox["blockedRuntime"]["blockers"]]
            assert blocker["reasons"] and all(reason in blocker["text"] for reason in blocker["reasons"])
            assert mailbox["beforeTask"]["task_id"] not in blocker["text"]
            assert mailbox["coldRuntime"]["residency"] == "cold"
            assert mailbox["bindingBefore"] == mailbox["bindingAfter"]
            counts = mailbox["graphCounts"]
            assert counts["cold"] == counts["before"] and counts["collected"] == counts["before"] + 1
            assert mailbox["consumptionBefore"]["acknowledged"] == 0
            assert mailbox["consumptionAfter"]["acknowledged"] == 1
            assert mailbox["consumptionBefore"]["batch_json"] == mailbox["consumptionAfter"]["batch_json"]
            assert mailbox["canonicalBefore"] == []
            canonical = mailbox["canonicalAfter"]
            carrier_kinds = [bool(json.loads(item["projection_manifest_json"])["projection_identity"].get("projection_message_id"))
                             for item in canonical]
            assert len(set(carrier_kinds)) == len(canonical)
            assert sum(item["item_id"] == mailbox["projectedResultItemId"] for item in canonical) == 1
            assert all(item["status"] == "completed" for item in canonical)
            assert mailbox["batch"] == mailbox["collectedBatch"]
            assert [item["message_id"] for item in mailbox["batch"]["messages"]] == expected_ids
            assert mailbox["batch"]["thread_id"] == mailbox["threadId"]
        if "skill_untrack" in selected:
            skill = result["section8"]["skill_untrack"]
            assert set(skill["skillLoadSummaries"]) == {"agent", "events", "requests"}
            for text in skill["skillLoadSummaries"].values():
                assert "已接受的 Skill 加载" in text and "gateway-context" in text
            assert skill["tracked"][0]["tracking_status"] == "tracked"
            assert skill["untracked"][0]["tracking_status"] == "untracked"
            assert [reply["data"]["status"] for reply in skill["receipts"]] == ["loaded", "not_tracked"]
            assert all(reply["data"]["session_id"] == skill["sessionId"]
                       and reply["data"]["thread_id"] == skill["threadId"] for reply in skill["receipts"])
        if "basic_chat" not in selected:
            return
        assert result["firstJob"]["status"] in {"completed", "succeeded"}
        assert result["secondJob"]["status"] in {"completed", "succeeded"}
        assert result["firstTurn"]["userCount"] == 1
        assert result["firstTurn"]["durationMs"] > 0
        assert result["firstTurn"]["itemCount"] == 4
        assert result["firstTurn"]["expanded"] is True
        assert result["firstTurn"]["activityPreview"].endswith(" · Item 4 项")
        assert len(result["firstTurn"]["activityOrder"]) == 3
        assert result["firstTurn"]["finalText"] == "首轮工具调用已完成。"
        assert "已运行 read_file" in result["firstTurn"]["toolText"]
        assert result["restoredHistory"]["durationMs"] == result["firstTurn"]["durationMs"]
        assert result["restoredHistory"]["itemCount"] == 4
        assert result["restoredHistory"]["activityPreview"] == result["firstTurn"]["activityPreview"]
        assert result["restoredHistory"]["expanded"] is True
        assert result["restoredHistory"]["finalText"] == "首轮工具调用已完成。"
        assert result["secondTurn"]["userCount"] == 1
        assert result["secondTurn"]["durationMs"] > 0
        assert result["secondTurn"]["itemCount"] == 4
        assert result["secondTurn"]["expanded"] is True
        assert result["secondTurn"]["activityPreview"].endswith(" · Item 4 项")
        assert len(result["secondTurn"]["activityOrder"]) == 3
        assert result["secondTurn"]["finalText"] == "第二轮工具调用也已完成。"
        assert "已运行 read_file" in result["secondTurn"]["toolText"]
        assert result["streams"] == {"trace": 2, "message": 2}
        trace_connections = result["basicStreamConnections"]["trace"]
        assert len({connection["documentId"] for connection in trace_connections}) == 2
        for connection in trace_connections:
            assert connection["status"] == 200
            assert connection["requestId"] and connection["cdpRequestId"]
            assert connection["workspaceHeader"] == workspace_id
            assert connection["targetGatewayHeader"] == gateway_id
            subscription, = (
                item for item in connection["subscriptions"]
                if item["subscription_id"] == connection["subscriptionId"]
            )
            assert subscription["kind"] == "session_trace"
            assert subscription["session_id"] == result["sessionId"]
            assert subscription["subscription_id"] in connection["connectedSubscriptions"]
        for connection in result["basicStreamConnections"]["message"]:
            assert connection["sessionId"] == result["sessionId"]
            assert connection["threadId"] == result["threadId"]
            assert connection["workspaceHeader"] == workspace_id
            assert connection["targetGatewayHeader"] == gateway_id
        assert result["persisted"]["llmRequestCount"] == 4
    except subprocess.TimeoutExpired as error:
        error.add_note(f"浏览器实时输出: {browser_stdout_path}; {browser_stderr_path}; 阶段: {phase_path}")
        raise
