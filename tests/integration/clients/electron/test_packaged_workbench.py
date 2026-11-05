from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.model_stream.config import model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import TestFilePortBlock, integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)

PROMPT = "ELECTRON_PACKAGED_USER_MESSAGE"
ANSWER = "ELECTRON_PACKAGED_AGENT_RESPONSE"


def _run_process(
    command: list[str], *, cwd: Path, log: Path, timeout: int,
    environment: dict[str, str] | None = None,
) -> int:
    """打包和原生驱动使用同一进程组 owner，超时也回收子进程并留下回执。"""
    process: subprocess.Popen[str] | None = None
    try:
        with log.open("x", encoding="utf-8") as output, capture_test_processes():
            process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=output,
                stderr=subprocess.STDOUT, text=True, start_new_session=True)
            managed = ManagedProcess(process=process, log_file=output)
            register_test_process(managed, managed.close)
            return process.wait(timeout=timeout)
    finally:
        log.with_name(f"{log.stem}-process.json").write_text(json.dumps({
            "command": command, "cwd": str(cwd), "timeout_seconds": timeout,
            "pid": process.pid if process is not None else None,
            "exit_code": process.returncode if process is not None else None,
        }) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def electron_service_ports(request: pytest.FixtureRequest) -> Iterator[TestFilePortBlock]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    # 打包前保留整组绑定，避免客户端临时源端口抢占尚未启动的 Gateway 或代理。
    lease = E2EProcessLease(
        context.runtime_root_for_node(request.node.nodeid) / "service-ports.json",
        tuple(ports.port(offset) for offset in (0, 20, 21)),
    )
    lease.acquire()
    try:
        yield ports
    finally:
        lease.release()


@pytest.fixture(scope="module")
def packaged_electron(request: pytest.FixtureRequest, electron_service_ports: TestFilePortBlock) -> Path:
    if sys.platform != "linux":
        pytest.fail("便携 Electron 原生回归当前仅验收 Linux；Windows/macOS 尚待各自平台验收")
    if shutil.which("xvfb-run") is None:
        pytest.fail("便携 Electron 原生回归需要 xvfb-run")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    output = runtime / "package"
    temporary = runtime / "tmp"
    temporary.mkdir(parents=True)
    environment = os.environ | {name: str(temporary) for name in ("TMPDIR", "TMP", "TEMP")}
    download_cache = context.project_root / "out/packaging/electron/downloads"
    if download_cache.is_dir():
        # 只复用官方运行时下载缓存，不复用其它运行的应用或 renderer 产物。
        command = ["bun", "tests/integration/clients/electron/seed_electron_cache.mjs",
                   str(download_cache), str(output / "downloads")]
        assert _run_process(command, cwd=context.project_root,
            log=context.artifacts_dir / "seed-cache.log", timeout=30, environment=environment) == 0
    command = ["bun", "run", "package:electron", "--output-root", str(output)]
    log_path = context.artifacts_dir / "package.log"
    exit_code = _run_process(command, cwd=context.project_root, log=log_path, timeout=360,
                             environment=environment)
    receipt: dict[str, object] = {"command": command, "exit_code": exit_code,
                                  "log": str(log_path), "output": str(output)}
    assert exit_code == 0, f"Electron 打包失败：{log_path}"
    release = json.loads((output / "release.json").read_text(encoding="utf-8"))
    assert release["platform"] == "linux"
    assert len(release["archives"]) == 1
    archive = Path(release["archives"][0]["archive"])
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    assert digest == release["archives"][0]["sha256"]
    assert archive.with_name(archive.name + ".sha256").read_text().strip() == f"{digest}  {archive.name}"
    relocated = runtime / "relocated"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(relocated, filter="data")
    application = relocated / Path(release["archives"][0]["bundle"]).name
    assert (application / "resources/app.asar").is_file()
    executable = application / "boxteam"
    assert os.access(executable, os.X_OK)
    receipt.update({"release": release, "sha256": digest, "executable": str(executable)})
    (context.artifacts_dir / "package-verification.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8",
    )
    return executable


@pytest.mark.asyncio
async def test_packaged_electron_chat_files_and_restart_cache(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    electron_service_ports: TestFilePortBlock,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    assert Path(integration_workspace_config_path).is_file()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    workspace = Path(integration_workspace_root_path)
    (workspace / "AGENTS.md").write_text("NATIVE_READONLY_AGENT_SPEC\n", encoding="utf-8")
    (workspace / "native-cache.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC",
    ))
    ports = electron_service_ports
    temporary = runtime / "tmp"
    node = shutil.which("node")
    assert node is not None, "Playwright 原生 Electron inspector 驱动需要 Node"
    with model_stream_replay(
        artifacts / "model-replay", runtime_root=runtime,
        business_assertion="packaged-native-electron-chat-vfs-and-restart-cache",
    ) as replay, ExitStack() as services:
        browser_temporary = services.enter_context(chromium_temporary_environment(temporary))
        backend = start_backend_process(
            workspace_root=str(workspace), port=ports.port(0), log_name="electron-backend",
            boxteam_home=runtime / "server-home",
            asgi_app="tests.support.e2e_control.backend_app:app", artifacts_dir=artifacts,
            env_overrides={**replay.environment, "OPENCODE_ZEN_API_KEY": "integration-replay-key",
                           "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(20)}"},
        )
        services.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=ports.port(20), boxteam_home=runtime / "server-home", artifacts_dir=artifacts,
        )
        services.callback(close_gateway_process, gateway)
        base_url = f"http://127.0.0.1:{gateway.port}"
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60) as client:
            navigation = await client.get("/api/gateway/workspaces")
            assert navigation.status_code == 200, navigation.text
            active_owner = navigation.json()["data"]["active_workspace"]
            assert active_owner["workspace_id"] == backend.workspace_id
        history_content = "".join(f"NATIVE_HISTORY_LINE_{index:02d}\n" for index in range(1, 21))
        replies = [
            ({"tool_calls": [{"index": 0, "id": "native-write-history", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"file_path": "native-history.txt", "content": history_content,
                    "expected_version": "absent"}),
            }}]}, "tool_calls"),
            ({"content": ANSWER}, "stop"),
        ]
        interactions = [cast(list[dict[str, object]], build_cassette(
            asset_id=f"packaged-electron-chat-{index}", model="big-pickle",
            url="https://opencode.ai/zen/v1/chat/completions",
            frames=chat_completion_frames(response_id=f"chatcmpl-packaged-electron-{index}", model="big-pickle",
                deltas=({"role": "assistant"}, delta), finish_reason=reason),
        ).raw["interactions"])[0] for index, (delta, reason) in enumerate(replies)]
        result_path = artifacts / "native-result.json"
        environment = os.environ | replay.environment | {
            **browser_temporary,
            "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
            "BOXTEAM_ELECTRON_GATEWAY": base_url,
            "BOXTEAM_ELECTRON_OWNER": json.dumps(active_owner),
            "BOXTEAM_ELECTRON_PROXY_PORT": str(ports.port(21)),
            "BOXTEAM_ELECTRON_RUNTIME": str(runtime),
            "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
            "BOXTEAM_ELECTRON_INTERACTIONS": json.dumps(interactions),
        }
        # TODO：Bun 1.3.11 的 Playwright inspector WebSocket 握手不完成，修复后再统一驱动。
        command = ["xvfb-run", "-a", node, "tests/integration/clients/electron/packaged_workbench.mjs"]
        driver_exit = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            environment=environment, log=artifacts / "driver.log", timeout=300)
        assert driver_exit == 0, f"Electron 原生验收失败：{artifacts}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["classification"] == "Integration"
        assert result["pageErrors"] == []
        assert result["storageErrors"] == []
        assert result["proxyErrors"] == []
        for name in ("packagedAsar", "noRendererNode", "nativeClose", "stableOrigin",
                     "cacheBeforeSync", "cacheReadOnly", "liveAfterSync", "profileIsolation", "fileSaved",
                     "imageRevalidated", "imageCacheSurvivesRestart", "imageProfileIsolation",
                     "readonlyDiffShared", "readonlyDocumentsRestored", "readonlyProfileIsolation"):
            assert result["checks"][name] is True, name
        owner = result["owner"]
        assert owner["workspace_id"] == backend.workspace_id
        assert len(result["modelCallIds"]) == 2
        recordings = {path.stem: json.loads(path.read_text()) for path in replay.artifacts.glob("*.json")}
        assert set(recordings) == set(result["modelCallIds"])
        for call_id in result["modelCallIds"]:
            recorded = recordings[call_id]
            assert recorded["identity"] == {"session_id": owner["session_id"], "thread_id": owner["thread_id"], "model_call_id": call_id}
            assert recorded["request"]["stream"] is True
            assert PROMPT in json.dumps(recorded["request"]["messages"])
        final_messages = recordings[result["modelCallIds"][1]]["request"]["messages"]
        assert any(message["role"] == "tool" and message["tool_call_id"] == "native-write-history" for message in final_messages)
        assert (workspace / "native-history.txt").read_text() == history_content
        assert result["file"]["sha256"] == hashlib.sha256((workspace / "README.md").read_bytes()).hexdigest()
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS | {
            "X-BoxTeam-Target-Gateway-Id": active_owner["gateway_id"],
            "X-BoxTeam-Workspace-Id": active_owner["workspace_id"],
        }, timeout=60) as client:
            await acquire_gateway_guest(client)
            history = await client.post(f"/api/v1/sessions/{owner['session_id']}/threads/{owner['thread_id']}/history", json={})
            assert history.status_code == 200, history.text
            assert history.headers["X-Request-ID"] == history.json()["request_id"]
            value = history.json()["data"]
            assert value["thread_id"] == owner["thread_id"]
            assert len(value["items"]) == 1
            assert value["items"][0]["status"] == "completed"
            assert PROMPT in json.dumps(value["items"])
            assert ANSWER in json.dumps(value["items"])
            (artifacts / "authoritative-history.json").write_text(history.text, encoding="utf-8")
    # 后端释放附件绑定仍需访问回放时钟；正常关闭应发生在控制服务退出之前。
    shutdown_log = (artifacts / "electron-backend.stderr.log").read_text(encoding="utf-8")
    assert "Application shutdown complete." in shutdown_log, artifacts
    assert "Application shutdown failed." not in shutdown_log, artifacts
