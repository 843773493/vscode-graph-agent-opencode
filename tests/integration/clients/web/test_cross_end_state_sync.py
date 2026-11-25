"""双 Gateway、独立浏览器观察者与真实消息入口；模型使用统一回放。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import commentjson
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.performance.report import distribution
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.communication.federation import open_communication_triad
from tests.support.model_stream.config import model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


def _prepare_workspace(context: TestRunContext, model_port: int, gateway_port: int) -> Path:
    # 本场景只需要会话身份与模型回放，不复制通信场景的文件和工具配置。
    workspace = prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    )
    config_path = install_test_workspace_config(
        workspace_root=workspace, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
    )
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    workspace_settings = config["settings"]["workspace"]
    workspace_settings.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{gateway_port}"
    provider = workspace_settings["llm"]["providers"]["primary"]
    config["profiles"]["provider.primary"]["settings"]["model_options"].update(
        {"endpoint": f"http://127.0.0.1:{model_port}/v1", "api_key": "${BOXTEAM_TEST_MODEL_API_KEY}"})
    provider["models"]["default"]["model"] = "e2e-stub-model"
    for agent in workspace_settings["agents"].values():
        agent["model"]["primary"] = {"provider_id": "primary", "model_id": "default"}
        agent["model"]["fallbacks"] = []
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return workspace


@pytest.fixture
def state_sync_web_assets(request: pytest.FixtureRequest) -> Path:
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file() and (assets / "out/main.js").is_file(), assets
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with (assets / "out/main.js").open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "prepared": bool(prepared), "main_sha256": digest,
    }, indent=2) + "\n", encoding="utf-8")
    return assets


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["goal_catalog", "messages"])
async def test_cross_end_state_sync_through_two_gateways(
    request: pytest.FixtureRequest, state_sync_web_assets: Path, scope: str,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    assert chromium, "跨端状态同步验收需要真实 Chromium"
    with model_stream_replay(
        context.artifacts_dir, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="two-gateways-workbench-goal-message-catalog-sync",
    ) as replay:
        async with open_communication_triad(
            context, _prepare_workspace, same_backend_routes=True,
            backend_asgi_app="tests.support.e2e_control.backend_app:app",
            backend_environment={**replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true", "BOXTEAM_TEST_MODEL_API_KEY": "e2e-local-model-key"},
            gateway_environment={"BOXTEAM_WEB_ASSETS": str(state_sync_web_assets)},
        ) as nodes:
            source, target = nodes["a"], nodes["b"]
            assert source.gateway is not None and target.gateway is not None
            configuration = commentjson.loads((target.workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
            endpoint = configuration["profiles"]["provider.primary"]["settings"]["model_options"]["endpoint"]
            interactions = build_cassette(
                asset_id="cross-end-state-sync", model="e2e-stub-model", url=endpoint + "/chat/completions",
                frames=chat_completion_frames(
                    response_id="cross-end-sync", model="e2e-stub-model",
                    deltas=[{"role": "assistant", "content": "原身份消息只执行一次。"}], finish_reason="stop",
                ),
            ).raw["interactions"]
            environment = {
                **os.environ, **replay.environment,
                "BOXTEAM_SYNC_MODEL_INTERACTIONS": json.dumps(interactions),
                "BOXTEAM_SYNC_SCOPE": scope,
                "BOXTEAM_SYNC_BASE_URL": f"http://127.0.0.1:{source.gateway.port}",
                "BOXTEAM_SYNC_TARGET_URL": f"http://127.0.0.1:{target.gateway.port}",
                "BOXTEAM_SYNC_TARGET_GATEWAY": target.gateway_id,
                "BOXTEAM_SYNC_WORKSPACE": target.workspace_id,
                "BOXTEAM_SYNC_ARTIFACTS": str(context.artifacts_dir),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
            }
            command = [
                sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
                "--memory-limit-kb", "4194304", "--timeout-seconds", "240", "--",
                "bun", "tests/integration/clients/web/cross_end_state_sync.mjs",
            ]
            log = context.artifacts_dir / "browser.log"
            with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium") as temporary:
                environment.update(temporary)
                with log.open("x", encoding="utf-8") as output:
                    completed = await asyncio.to_thread(
                        subprocess.run, command, cwd=context.project_root, env=environment,
                        stdout=output, stderr=subprocess.STDOUT, check=False,
                    )
            (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
                "command": command, "exit_code": completed.returncode,
                "boundary": "real Workbench, two Gateways, localhost SSH, one Backend, model_stream replay",
            }, indent=2) + "\n", encoding="utf-8")
            assert completed.returncode == 0, log.read_text(encoding="utf-8")
            result = json.loads((context.artifacts_dir / "state-sync.json").read_text(encoding="utf-8"))
            assert result["status"] == "passed", result
            if scope == "messages":
                return
            measurements = {
                endpoint: {
                    "first": samples["first"],
                    "warm": distribution([sample["ms"] for sample in samples["warm"]]),
                    "bytes": [sample["bytes"] for sample in samples["warm"]],
                }
                for endpoint, samples in result["samples"].items()
            }
            assert all(value["warm"]["sample_count"] == 20 for value in measurements.values())
            measurements["navigation"] = {
                "first": result["navigation"][0],
                "warm": distribution([sample["ms"] for sample in result["navigation"][1:]]),
                "samples": result["navigation"],
                "boundary": "关闭编辑器→工作区列表→当前工作区→目录页→真实点击会话→可编辑Composer；localhost SSH联邦完整耗时",
            }
            (context.artifacts_dir / "measurements.json").write_text(
                json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            for endpoint, measured in measurements.items():
                if endpoint == "navigation":
                    assert measured["warm"]["p95_ms"] <= 1000, measured
                    assert measured["warm"]["max_ms"] <= 2000, measured
                else:
                    assert measured["first"]["ms"] <= 3000, measured
                    assert measured["warm"]["p95_ms"] <= 500, measured
                    assert measured["warm"]["max_ms"] <= 1500, measured
