from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import commentjson
import httpx
import pytest

from app.gateway.runtime.process import ManagedProcess
from app.services.business.session_context_resource import session_resource_uri
from tests.harness.python.run_context import TestRunContext
from tests.support.communication_federation import (
    CommunicationNode,
    open_communication_triad,
)
from tests.support.e2e_test_control import E2EProcessLease, E2ETestControlHarness
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.model_stream_config import prepare_e2e_model_stream_config
from tests.support.paths import output_root_for_test
from tests.support.ports import e2e_port_block_for_file
from tests.support.process_scope import capture_test_processes, register_test_process
from tests.support.processes import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
    workspace_backend_id_for_root,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(scope="module")
def section8_output_root(request: pytest.FixtureRequest) -> Path:
    return output_root_for_test(Path(request.node.fspath), test_layer="e2e", project_root=Path.cwd())


@pytest.fixture(scope="module", autouse=True)
def section8_process_lease(request: pytest.FixtureRequest, section8_output_root: Path) -> Iterator[E2EProcessLease]:
    block = e2e_port_block_for_file(Path(request.node.fspath))
    offsets = (0, 1, 4, 5, 6, 10, 20, 21, 22, 24, 25, 26, 40, 41, 43, 44, 45, 46, 60, *range(70, 80))
    lease = E2EProcessLease(section8_output_root / "runtime/test-control/process-lease.json", tuple(block.port(offset) for offset in offsets))
    lease.acquire()
    try:
        yield lease
    finally:
        lease.release()


@pytest.fixture(scope="module")
def section8_control(section8_output_root: Path) -> Iterator[E2ETestControlHarness]:
    harness = E2ETestControlHarness(section8_output_root / "runtime/test-control")
    harness.start()
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture(scope="module")
def section8_web_build(section8_output_root: Path, section8_process_lease: E2EProcessLease) -> None:
    """Gateway 静态资源是启动前置；两个原产品入口只构建一次。"""
    del section8_process_lease
    project = Path.cwd().resolve()
    artifacts = section8_output_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    phase_path = artifacts / "basic-chat-tool-loop-phases.log"
    stdout_path = artifacts / "basic-chat-tool-loop-build.stdout.log"
    stderr_path = artifacts / "basic-chat-tool-loop-build.stderr.log"
    with phase_path.open("w") as phase_output, stdout_path.open("w") as build_stdout, stderr_path.open("w") as build_stderr:
        print(f"{datetime.now(UTC).isoformat()} build_begin pid={os.getpid()}", file=phase_output, flush=True)
        # TODO: 定位原构建等待点后移除本次测试侧观察；不改变构建或产品期限。
        with capture_test_processes():
            faulthandler.dump_traceback_later(55, file=phase_output)
            try:
                process = subprocess.Popen(
                    ["bun", "run", "build"], cwd=project / "src" / "clients" / "web",
                    stdout=build_stdout, stderr=build_stderr, text=True, start_new_session=True,
                )
                # 同一 owner 核实原 leader 与全部后代；根 Bun 退出不代表 Vite 已退出。
                build_process = ManagedProcess(process=process, log_file=build_stdout)
                register_test_process(build_process, build_process.close)
                print(f"{datetime.now(UTC).isoformat()} build_process_begin pid={process.pid}", file=phase_output, flush=True)
                returncode = process.wait(timeout=180)
            finally:
                faulthandler.cancel_dump_traceback_later()
        print(f"{datetime.now(UTC).isoformat()} build_end pid={process.pid} returncode={returncode}", file=phase_output, flush=True)
    assert returncode == 0, f"Web 构建失败:\n{stdout_path.read_text()}\n{stderr_path.read_text()}"


@pytest.fixture(scope="module")
def section8_browser_temporary_directory(section8_output_root: Path) -> Iterator[str]:
    """短 fd 引用满足 Chromium socket 预算；实体仍在唯一正式 runtime 目录。"""
    temporary = section8_output_root / "runtime/test-control/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
    path = f"/proc/{os.getpid()}/fd/{descriptor}"
    assert Path(path).resolve() == temporary
    try:
        yield path
    finally:
        os.close(descriptor)


@pytest.fixture(scope="module")
def e2e_workspace_root_path(section8_output_root: Path) -> str:
    workspace = section8_output_root / "workspace/primary/workspace"
    reset_gateway_persistent_state(workspace_root=workspace)
    return str(prepare_default_test_workspace(workspace_root=workspace, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", shared_skill_root=Path.cwd() / "resources/skills"))


@pytest.fixture(scope="module")
def e2e_backend_composition(section8_control: E2ETestControlHarness) -> tuple[str, dict[str, str]]:
    return "tests.support.section8_backend:app", section8_control.environment


@pytest.fixture(scope="module")
def e2e_model_stream_runtime_config_path(
    e2e_model_stream_config_path: str, section8_output_root: Path,
    section8_control: E2ETestControlHarness, e2e_model_stream_mode: str | None,
) -> str:
    fixture_root = section8_output_root / "runtime/model-stream-fixture"
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
    return str(prepare_e2e_model_stream_config(e2e_model_stream_config_path, output_root=section8_output_root, mode=e2e_model_stream_mode, exact_fixture_root=fixture_root))


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(
        Path.cwd()
        / "configs"
        / "tests"
        / "model_stream"
        / "model_stream_web_basic_chat_tool_loop.jsonc"
    )


@pytest.fixture
def section8_mcp_catalogs(section8_output_root: Path) -> dict[str, dict[str, Path]]:
    catalogs: dict[str, dict[str, Path]] = {}
    for node in ("a", "c"):
        directory = section8_output_root / "runtime/mcp" / node
        directory.mkdir(parents=True, exist_ok=True)
        catalogs[node] = {}
        for server in ("notify", "silent_a", "silent_b"):
            path = directory / f"{server}.json"
            path.write_text(json.dumps({"revision": 0, "tools": []}) + "\n")
            catalogs[node][server] = path
    return catalogs


@pytest.fixture
async def section8_product_nodes(
    request: pytest.FixtureRequest, section8_output_root: Path,
    section8_control: E2ETestControlHarness, e2e_workspace_root_path: str,
    e2e_workspace_config_path: str, e2e_model_stream_runtime_config_path: str,
    section8_web_build: None,
    section8_mcp_catalogs: dict[str, dict[str, Path]],
) -> AsyncIterator[dict[str, CommunicationNode]]:
    """唯一产品 owner 的三工作区；正常配置与真实 SSH/channel，不改 Provider。"""
    del section8_web_build
    project = Path.cwd().resolve()
    context = TestRunContext.from_test_file(Path(request.node.path), project_root=project)
    context.workspace_root = section8_output_root / "workspace"
    context.artifacts_dir = section8_output_root / "artifacts/section8-product"
    context.artifacts_dir.mkdir(parents=True, exist_ok=True)
    phase_path = section8_output_root / "artifacts/basic-chat-tool-loop-phases.log"
    with phase_path.open("a") as phase_output:
        print(f"{datetime.now(UTC).isoformat()} fixture_begin pid={os.getpid()}", file=phase_output, flush=True)

    def prepare(node_context: TestRunContext, _model_port: int, gateway_port: int) -> Path:
        if node_context.workspace_root == context.workspace_root / "a/workspace":
            workspace = Path(e2e_workspace_root_path)
            config_path = Path(e2e_workspace_config_path)
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
        config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{gateway_port}"
        if node_context.workspace_root.name == "workspace" and node_context.workspace_root.parent.name in {"a", "c"}:
            config["runtime"].setdefault("agent", {}).setdefault("run", {})["mode"] = "team"
        if node_context.workspace_root == context.workspace_root / "c/workspace":
            config.setdefault("context", {}).setdefault("resource_activation", {}).setdefault("overrides", {}).update({
                "team_state": "model_call", "mcp_tool_catalog": "model_call",
            })
        node = node_context.workspace_root.parent.name
        if node in section8_mcp_catalogs:
            config["mcp"] = {"servers": {
                server: {"enabled": True, "transport": "stdio", "command": sys.executable,
                         "args": ["-m", "tests.support.section8_mcp_server", "--catalog", str(path),
                                  *(["--notifications"] if server == "notify" else [])],
                         "cwd": str(project), "env": section8_control.environment}
                for server, path in section8_mcp_catalogs[node].items()
            }}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        return workspace

    async with open_communication_triad(
        context, prepare, ports=e2e_port_block_for_file(Path(request.node.path)),
        resource_service_nodes=("a", "b", "c"),
        backend_asgi_app="tests.support.section8_backend:app",
        backend_environment=section8_control.environment | {
            "BOXTEAM_TEST_MODEL_STREAM_CONFIG": e2e_model_stream_runtime_config_path,
            "PYTHONPROFILEIMPORTTIME": "1",
            # 本场景验证模型 transport，不验证远端价格表更新；使用 LiteLLM 包内表。
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        },
        gateway_environment={"BOXTEAM_WEB_ASSETS": str(project / "src/clients/web/dist")},
    ) as nodes:
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
async def test_basic_chat_tool_loop_through_real_web_surface(
    request: pytest.FixtureRequest,
    section8_output_root: Path,
    section8_control: E2ETestControlHarness,
    section8_product_nodes: dict[str, CommunicationNode],
    section8_browser_temporary_directory: str,
    section8_mcp_catalogs: dict[str, dict[str, Path]],
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
        pytest.fail("基础聊天工具循环 Web E2E 需要 Chromium")

    phase_path = section8_output_root / "artifacts/basic-chat-tool-loop-phases.log"
    node = section8_product_nodes["a"]
    artifacts = section8_output_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
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
            workspace_id = workspaces.json()["data"]["active_workspace_id"]
            assert isinstance(workspace_id, str) and workspace_id

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": str(node.client.base_url).rstrip("/"),
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
                "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(artifacts),
                "TMPDIR": section8_browser_temporary_directory,
                "TMP": section8_browser_temporary_directory,
                "TEMP": section8_browser_temporary_directory,
                "BOXTEAM_BROWSER_NODES": json.dumps({
                    name: {"base_url": str(peer.client.base_url).rstrip("/"), "workspace_identity": peer.workspace_id,
                           "gateway_id": peer.gateway_id, "workspace": str(peer.workspace),
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
                ["bun", "tests/e2e/clients/web/basic-chat-tool-loop.e2e.mjs"],
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
            returncode = await asyncio.to_thread(process.wait, timeout=1200)
            print(f"{datetime.now(UTC).isoformat()} browser_driver_end returncode={returncode}", file=phase_output, flush=True)
        assert returncode == 0, (
            "基础聊天工具循环 Web E2E 失败:\n"
            f"stdout:\n{browser_stdout_path.read_text()}\n"
            f"stderr:\n{browser_stderr_path.read_text()}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        diagnostics = result["diagnostics"]
        assert diagnostics["pageErrors"] == []
        assert diagnostics["failedRequests"] == []
        assert diagnostics["unhandledConsoleErrors"] == []
        handled = (
            diagnostics["handledHistoryConflicts"]
            + diagnostics["handledLifecycleErrors"]
            + diagnostics["handledTurnNavigationErrors"]
        )
        assert sorted(fact["consoleIndex"] for fact in handled) == list(
            range(len(diagnostics["consoleErrors"])),
        )
        focus = [name.strip() for name in os.getenv("BOXTEAM_E2E_SECTION8_FOCUS", "").split(",") if name.strip()]
        if focus:
            assert result["focusedScenarios"] == focus
            assert set(result["section8"]) == set(focus)
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
        assert result["persisted"]["llmRequestCount"] == 4
    except subprocess.TimeoutExpired as error:
        error.add_note(f"浏览器实时输出: {browser_stdout_path}; {browser_stderr_path}; 阶段: {phase_path}")
        raise


@pytest.fixture
def persistent_resource_services(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    section8_output_root: Path,
    section8_control: E2ETestControlHarness,
    e2e_model_stream_runtime_config_path: str,
) -> Iterator[dict[str, str]]:
    """静态辅助前端按本文件端口隔离；领域服务由真实 Gateway runtime 启动。"""
    project = Path.cwd().resolve()
    workspace = Path(e2e_workspace_root_path).resolve()
    node = shutil.which("node")
    if node is None:
        pytest.fail("真实 Terminal/Browser 辅助服务需要生产 launch 使用的 Node.js")
    runtime = section8_output_root / "runtime" / "persistent-resource-lifecycle"
    temporary = runtime / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    ports = e2e_port_block_for_file(Path(request.node.path))
    workspace_id = workspace_backend_id_for_root(workspace)
    urls: dict[str, str] = {}
    with ExitStack() as stack:
        assert Path(e2e_workspace_config_path).is_file()
        # Chromium 的 Unix socket 有路径长度上限；短 fd 引用仍写入正式 runtime/tmp。
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, descriptor)
        socket_tmp = f"/proc/{os.getpid()}/fd/{descriptor}"
        assert Path(socket_tmp).resolve() == temporary
        environment = os.environ | {
            "TMPDIR": socket_tmp, "TMP": socket_tmp, "TEMP": socket_tmp,
            "WORKSPACE_ROOT": str(workspace), "BOXTEAM_WORKSPACE_ID": workspace_id,
            "BOXTEAM_PROJECT_ROOT": str(project),
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        }
        for kind, offset in (("terminal", 41), ("browser", 43)):
            port = ports.port(offset)
            ensure_local_port_available(port, service_name=f"{kind} client")
            log = stack.enter_context((runtime / f"{kind}-client.log").open("w"))
            process = subprocess.Popen(
                [node, "server.js", "--host", "127.0.0.1", "--port", str(port),
                 "--workspace-root", str(workspace), "--workspace-id", workspace_id,
                 "--asset-root", str(project)],
                cwd=project / "src/workspace-services" / kind / "client",
                env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
            stack.callback(terminate_process, process)
            wait_for_http_ok(f"http://127.0.0.1:{port}/health", process)
            urls[f"BOXTEAM_{kind.upper()}_FRONTEND_URL"] = f"http://127.0.0.1:{port}"
        yield urls | {name: environment[name] for name in ("TMPDIR", "TMP", "TEMP")} | {
            "BOXTEAM_TEST_MODEL_STREAM_CONFIG": e2e_model_stream_runtime_config_path,
        } | section8_control.environment


@pytest.mark.asyncio
async def test_persistent_resources_survive_real_thread_unload_and_session_deletion(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    persistent_resource_services: dict[str, str],
    section8_output_root: Path,
    section8_web_build: None,
) -> None:
    """真实 parser 回放先使 Thread 驻留；安全重启后从新浏览器管理同一资源。"""
    del section8_web_build
    project = Path.cwd().resolve()
    workspace = Path(e2e_workspace_root_path).resolve()
    artifacts = section8_output_root / "artifacts" / "persistent-resource-lifecycle"
    artifacts.mkdir(parents=True, exist_ok=True)
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if chromium is None:
        pytest.fail("持久资源 Web E2E 需要真实 Chromium")
    ports = e2e_port_block_for_file(Path(request.node.path))
    gateway = None
    try:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None,
            port=ports.port(21), extra_env=persistent_resource_services | {
                "BOXTEAM_WEB_ASSETS": str(project / "src/clients/web/dist"),
                "BOXTEAM_DEFAULT_BACKEND_PORT": str(ports.port(10)),
            },
            asgi_app="tests.support.section8_gateway:app",
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            reply = await client.get("/api/gateway/workspaces")
            assert reply.status_code == 200, reply.text
            workspace_id = reply.json()["data"]["active_workspace_id"]
        environment = os.environ | persistent_resource_services | {
            "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
            "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
            "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(artifacts),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
        }
        for phase in ("before-unload", "after-unload"):
            phase_result = artifacts / f"{phase}.json"
            phase_result.unlink(missing_ok=True)
            (artifacts / f"{phase}-failure.png").unlink(missing_ok=True)
            result = await asyncio.to_thread(
                subprocess.run,
                ["bun", "tests/e2e/clients/web/persistent-resource-lifecycle.e2e.mjs", phase],
                cwd=project, env=environment, capture_output=True, text=True, timeout=240, check=False,
            )
            (artifacts / f"{phase}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            details = phase_result.read_text() if phase_result.is_file() else "阶段报告未生成"
            assert result.returncode == 0, f"{phase}: {result.stdout}\n{result.stderr}\n{details}"
            if phase == "before-unload":
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{gateway.port}",
                    headers=LOCAL_TOKEN_HEADERS, timeout=120,
                ) as client:
                    await acquire_gateway_guest(client)
                    health_before = await client.get("/api/v1/health")
                    assert health_before.status_code == 200, health_before.text
                    restarted = await client.post(f"/api/gateway/workspaces/{workspace_id}/runtime/restart-safe")
                    assert restarted.status_code == 200, restarted.text
                    assert restarted.json()["data"]["status"] == "restarted", restarted.text
                    assert restarted.json()["data"]["blockers"] == [], restarted.text
                    health_after = await client.get("/api/v1/health")
                    assert health_after.status_code == 200, health_after.text
                    assert health_before.json()["process_id"] != health_after.json()["process_id"]
                    (artifacts / "backend-restart.json").write_text(
                        json.dumps({"before": health_before.json(), "after": health_after.json(),
                                    "restart": restarted.json()}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        facts = json.loads((artifacts / "after-unload.json").read_text())
        assert facts["passed"] is True
        assert facts["kinds"] == ["terminal", "browser"]
        assert facts["real_model_job_completed"] is True
        assert facts["unload_associations_removed"] is True
        assert facts["other_thread_attach_detach"] is True
        assert facts["session_deleted_resources_accessible"] is True
        assert facts["conflict_shown_and_owner_refetched"] is True
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
