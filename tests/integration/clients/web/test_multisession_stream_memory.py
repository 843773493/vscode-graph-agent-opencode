"""六个真实会话同时生成：两个阅读历史，四个跟随尾部；记录浏览器内存与输入延迟。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import commentjson
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 复用正式完整构建
)
from tests.support.model_stream.config import model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
def streaming_workspace(integration_workspace_root_path, integration_workspace_config_path):
    path = Path(integration_workspace_config_path)
    config = commentjson.loads(path.read_text(encoding="utf-8"))
    config["llm"]["providers"] = {"primary": config["llm"]["providers"]["primary"]}
    # 回放模型的输入容量覆盖多轮大正文；本场景验证前端释放，不消费压缩模型回放。
    config["llm"]["providers"]["primary"]["defaults"]["api_mode"]["model_info"]["max_input_tokens"] = 4_000_000
    for agent in config["agents"].values():
        agent["model"]["primary"] = {"provider_id": "primary", "model_id": "default"}
        agent["model"]["fallbacks"] = []
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return Path(integration_workspace_root_path)


def test_six_generating_sessions_keep_reading_and_following_responsive(
    request, streaming_workspace, web_workbench_assets,
):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    ports = integration_port_block_for_file(Path(request.node.path))
    home = context.boxteam_home_for_node(request.node.nodeid)
    with model_stream_replay(artifacts, runtime_root=runtime,
            business_assertion="six-generating-sessions-two-reading-four-following") as replay:
        backend = start_backend_process(
            workspace_root=str(streaming_workspace), port=ports.port(0), boxteam_home=home,
            artifacts_dir=artifacts, log_name="six-session-backend",
            asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={**replay.environment, "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(20)}",
                # 仅满足 SDK 请求字段构造；所有 HTTP 响应由精确 transport 回放，不进行模型鉴权。
                "OPENCODE_ZEN_API_KEY": "test-key-placeholder"},
        )
        try:
            gateway = start_gateway_process(workspace_root=streaming_workspace,
                default_backend_url=f"http://127.0.0.1:{backend.port}", port=ports.port(20),
                boxteam_home=home, artifacts_dir=artifacts,
                extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
            try:
                supervisor = context.project_root / "app/runtime" / (
                    "windows_job.py" if sys.platform == "win32" else "posix_job.py")
                browser_timeout = 600 + 60 * int(os.getenv("BOXTEAM_MEMORY_REPEAT_ROUNDS", "0"))
                command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                    "--timeout-seconds", str(browser_timeout), "--", "bun",
                    "tests/integration/clients/web/multisession_stream_memory.mjs"]
                environment = os.environ | replay.environment | {
                    "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                    "BOXTEAM_MEMORY_ARTIFACTS": str(artifacts), "BOXTEAM_MEMORY_BASELINE": "0"}
                with chromium_temporary_environment(runtime / "chromium") as temporary:
                    process = subprocess.run(command, cwd=context.project_root, env=environment | temporary,
                        capture_output=True, timeout=browser_timeout + 20, check=False)
                (artifacts / "browser.stdout.log").write_bytes(process.stdout)
                (artifacts / "browser.stderr.log").write_bytes(process.stderr)
                (artifacts / "command.json").write_text(json.dumps({"command": command,
                    "exit_code": process.returncode}), encoding="utf-8")
                assert process.returncode == 0, process.stderr.decode("utf-8", errors="backslashreplace")
                result = json.loads((artifacts / "result.json").read_text(encoding="utf-8"))
                assert result["completed"] is True
                assert len(result["samples"]) == 8
            finally:
                close_gateway_process(gateway)
        finally:
            close_backend_process(backend)
