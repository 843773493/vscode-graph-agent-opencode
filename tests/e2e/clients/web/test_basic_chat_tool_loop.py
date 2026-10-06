from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from configs.runtime import read_jsonc_object, validate_config
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.model_stream_config import prepare_e2e_model_stream_config
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
    workspace_backend_id_for_root,
)


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(
        Path.cwd()
        / "configs"
        / "tests"
        / "model_stream"
        / "model_stream_web_basic_chat_tool_loop.jsonc"
    )


@pytest.mark.asyncio
async def test_basic_chat_tool_loop_through_real_web_surface(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_backend_process: object,
    e2e_backend_port: int,
) -> None:
    del e2e_backend_process
    project_root = Path.cwd().resolve()
    workspace_root = Path(e2e_workspace_root_path).resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("基础聊天工具循环 Web E2E 需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    port = e2e_port_block_for_file(Path(request.node.fspath)).port(20)
    backend_url = f"http://127.0.0.1:{e2e_backend_port}"
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url=backend_url,
        port=port,
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(project_root / "src" / "clients" / "web" / "dist")
        },
    )
    artifacts = workspace_root.parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "basic-chat-tool-loop-result.json"
    screenshot_path = artifacts / "basic-chat-tool-loop-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
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
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        browser_result = await asyncio.to_thread(
            subprocess.run,
            ["node", "tests/e2e/clients/web/basic-chat-tool-loop.e2e.mjs"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert browser_result.returncode == 0, (
            "基础聊天工具循环 Web E2E 失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
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
        assert result["diagnostics"] == {
            "pageErrors": [],
            "consoleErrors": [],
            "failedRequests": [],
        }
    finally:
        close_gateway_process(gateway)


@pytest.fixture
def persistent_resource_services(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
) -> Iterator[dict[str, str]]:
    """静态辅助前端按本文件端口隔离；领域服务由真实 Gateway runtime 启动。"""
    project = Path.cwd().resolve()
    workspace = Path(e2e_workspace_root_path).resolve()
    node = shutil.which("node")
    if node is None:
        pytest.fail("真实 Terminal/Browser 辅助服务需要生产 launch 使用的 Node.js")
    runtime = workspace.parent / "runtime" / "persistent-resource-lifecycle"
    temporary = runtime / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    ports = e2e_port_block_for_file(Path(request.node.path))
    workspace_id = workspace_backend_id_for_root(workspace)
    urls: dict[str, str] = {}
    with ExitStack() as stack:
        # 此场景验证资源生命周期，明确使用已有真实 Provider，不依赖免费服务的 failover。
        config_path = Path(e2e_workspace_config_path)
        original_config = config_path.read_bytes()
        config = read_jsonc_object(config_path)
        agents = config["agents"]
        assert isinstance(agents, dict)
        model = agents["default"]["model"]
        model["primary_provider"] = "backup_3"
        model["fallback_providers"] = []
        validate_config(config, config_path=config_path, schema_path=project / "configs/workspace_schema.jsonc")
        stack.callback(config_path.write_bytes, original_config)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Chromium 的 Unix socket 有路径长度上限；短 fd 引用仍写入正式 runtime/tmp。
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, descriptor)
        socket_tmp = f"/proc/{os.getpid()}/fd/{descriptor}"
        assert Path(socket_tmp).resolve() == temporary
        environment = os.environ | {
            "TMPDIR": socket_tmp, "TMP": socket_tmp, "TEMP": socket_tmp,
            "WORKSPACE_ROOT": str(workspace), "BOXTEAM_WORKSPACE_ID": workspace_id,
            "BOXTEAM_PROJECT_ROOT": str(project),
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
        model_config = prepare_e2e_model_stream_config(
            project / "configs/tests/model_stream/model_stream_web_basic_chat_tool_loop.jsonc",
            output_root=runtime,
            mode="off",
        )
        yield urls | {name: environment[name] for name in ("TMPDIR", "TMP", "TEMP")} | {
            "BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(model_config),
        }


@pytest.mark.asyncio
async def test_persistent_resources_survive_real_thread_unload_and_session_deletion(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    persistent_resource_services: dict[str, str],
) -> None:
    """实际模型先使 Thread 驻留；Gateway 安全重启后从新浏览器管理同一资源。"""
    project = Path.cwd().resolve()
    workspace = Path(e2e_workspace_root_path).resolve()
    artifacts = workspace.parent / "artifacts" / "persistent-resource-lifecycle"
    artifacts.mkdir(parents=True, exist_ok=True)
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if chromium is None:
        pytest.fail("持久资源 Web E2E 需要真实 Chromium")
    build = await asyncio.to_thread(
        subprocess.run, ["bun", "run", "--cwd", "src/clients/web", "build"],
        cwd=project, capture_output=True, text=True, check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    ports = e2e_port_block_for_file(Path(request.node.path))
    gateway = None
    try:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None,
            port=ports.port(21), extra_env=persistent_resource_services | {
                "BOXTEAM_WEB_ASSETS": str(project / "src/clients/web/dist"),
                "BOXTEAM_DEFAULT_BACKEND_PORT": str(ports.port(10)),
            },
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
