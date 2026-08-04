from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.turn_history import TurnHistoryStore
from tests.e2e.gateway.processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.http_stubs import openai_chat_stub
from tests.support.ports import e2e_port_block_for_file
from tests.support.turn_history.checkpoints import seed_compactable_checkpoint
from tests.support.turn_history.event_builders import (
    build_long_session_events,
    write_latest_turn_attachment,
)
from tests.support.turn_history.projection import (
    rebuild_turn_projection,
    write_trace_fixture,
)

if TYPE_CHECKING:
    from app.schemas.event import Event


BROWSER_TURN_COUNT = 240


def _configure_stub_provider(config_path: Path, endpoint: str) -> None:
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    primary = next(
        provider
        for provider in config["llm"]["providers"]
        if provider["id"] == "primary"
    )
    primary.update(
        {
            "endpoint": endpoint,
            "model": "e2e-stub-model",
            "api_key": "e2e-local-model-key",
            "custom_llm_provider": "openai",
            "api_mode": "chat_completions",
        }
    )
    primary.pop("capabilities", None)
    primary.pop("request_options", None)
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _create_session(client: httpx.Client, title: str) -> str:
    response = client.post("/api/v1/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["session_id"])


def _seed_turn_session(
    *,
    store: TurnHistoryStore,
    workspace_root: Path,
    session_id: str,
    turn_count: int,
    text_end_only_turn_indexes: set[int] | None = None,
) -> list[Event]:
    events = build_long_session_events(
        session_id=session_id,
        turn_count=turn_count,
        text_end_only_turn_indexes=text_end_only_turn_indexes,
    )
    assert rebuild_turn_projection(
        store=store,
        session_id=session_id,
        events=events,
    ) == turn_count
    write_trace_fixture(
        workspace_root=workspace_root,
        session_id=session_id,
        events=events,
    )
    seed_compactable_checkpoint(
        workspace_root=workspace_root,
        session_id=session_id,
    )
    write_latest_turn_attachment(
        workspace_root=workspace_root,
        session_id=session_id,
        turn_count=turn_count,
    )
    return events


def test_long_turn_session_keeps_composer_responsive_and_history_incremental(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    workspace_root = Path(e2e_workspace_root_path).resolve()
    output_root = workspace_root.parent
    artifacts = output_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    metrics_path = artifacts / "turn-history-performance-metrics.json"
    screenshot_path = artifacts / "turn-history-performance-failure.png"
    metrics_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail(
            "Turn 历史性能 E2E 需要 Chromium；设置 PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"
        )

    build = subprocess.run(
        ["bun", "run", "build"],
        cwd=project_root / "src" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    gateway_port = port_block.port(20)
    model_port = port_block.port(30)
    _configure_stub_provider(
        Path(e2e_workspace_config_path),
        f"http://127.0.0.1:{model_port}/v1",
    )
    with openai_chat_stub(model_port):
        gateway = start_gateway_process(
            workspace_root=workspace_root,
            default_backend_url="http://127.0.0.1:9",
            port=gateway_port,
            extra_env={
                "BOXTEAM_WEB_ASSETS": str(project_root / "src" / "web" / "dist"),
                "BOXTEAM_GATEWAY_PRESERVE_BROWSER_MANAGERS_ON_SHUTDOWN": "false",
            },
        )
        try:
            gateway_url = f"http://127.0.0.1:{gateway_port}"
            with httpx.Client(
                base_url=gateway_url,
                headers=LOCAL_TOKEN_HEADERS,
                timeout=60,
            ) as client:
                target_session_id = _create_session(
                    client,
                    "Turn 性能长会话",
                )
                store = TurnHistoryStore(workspace_root / ".boxteam" / "sessions")
                target_events = _seed_turn_session(
                    store=store,
                    workspace_root=workspace_root,
                    session_id=target_session_id,
                    turn_count=BROWSER_TURN_COUNT,
                )
                corrupted_session_id = _create_session(
                    client,
                    "Turn 投影损坏会话",
                )
                _seed_turn_session(
                    store=store,
                    workspace_root=workspace_root,
                    session_id=corrupted_session_id,
                    turn_count=1,
                )
                corrupted_projection_root = (
                    get_session_path_resolver(
                        workspace_root / ".boxteam" / "sessions"
                    ).resolve_session_node(corrupted_session_id)
                    / "turn_history"
                )
                (corrupted_projection_root / "manifest.json").write_text(
                    "{broken browser projection",
                    encoding="utf-8",
                )
                control_session_id = _create_session(
                    client,
                    "Turn 性能控制会话",
                )
                _seed_turn_session(
                    store=store,
                    workspace_root=workspace_root,
                    session_id=control_session_id,
                    turn_count=1,
                    text_end_only_turn_indexes={0},
                )

            fixture = {
                "target": {
                    "sessionId": target_session_id,
                    "title": "Turn 性能长会话",
                    "latestUserMarker": (f"TURN-E2E-{BROWSER_TURN_COUNT:04d}-USER"),
                    "latestFinalMarker": (f"TURN-E2E-{BROWSER_TURN_COUNT:04d}-FINAL"),
                },
                "control": {
                    "sessionId": control_session_id,
                    "title": "Turn 性能控制会话",
                    "latestFinalMarker": "TURN-E2E-0001-FINAL",
                    "textEndOnly": True,
                },
                "corrupted": {
                    "sessionId": corrupted_session_id,
                    "title": "Turn 投影损坏会话",
                    "expectedErrorCode": "turn_projection_corrupted",
                },
                "turnCount": BROWSER_TURN_COUNT,
                "traceEventCount": len(target_events),
                "historyPagesToLoad": 3,
                "racePrompt": "RACE-TURN-E2E-USER",
                "postPaginationPrompt": "POST-PAGINATION-TURN-E2E-USER",
                "liveResponse": "E2E 生成器替身回复",
                "thresholds": {
                    "composerReadyMs": 500,
                    "latestSummaryMs": 1500,
                    "latestDetailMs": 5000,
                    "maxBootstrapBytes": 32 * 1024,
                    "maxRenderedTurns": 60,
                    "maxDomNodes": 6000,
                    "maxAnchorDeltaPx": 8,
                    "maxDetailBatchSize": 4,
                    "maxFullTraceRequests": 0,
                },
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "BOXTEAM_E2E_BASE_URL": gateway_url,
                    "BOXTEAM_E2E_METRICS_PATH": str(metrics_path),
                    "BOXTEAM_E2E_SCREENSHOT_PATH": str(screenshot_path),
                    "BOXTEAM_E2E_LOCAL_TOKEN": LOCAL_TOKEN_HEADERS[
                        "X-Local-Token"
                    ],
                    "BOXTEAM_E2E_FIXTURE": json.dumps(
                        fixture,
                        ensure_ascii=False,
                    ),
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
                }
            )
            try:
                result = subprocess.run(
                    ["node", "tests/e2e/clients/web/turn_history_performance.mjs"],
                    cwd=project_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                pytest.fail(
                    "Turn 历史性能浏览器驱动在 180 秒内未结束；"
                    f"stdout={error.stdout!r} stderr={error.stderr!r}"
                )
            assert metrics_path.is_file(), (
                "浏览器测试没有写出 metrics JSON:\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            assert result.returncode == 0, (
                "Turn 历史性能指标失败:\n"
                + "\n".join(metrics.get("failures", []))
                + f"\nmetrics={metrics_path}\n"
                + f"stdout={result.stdout}\nstderr={result.stderr}"
            )
            assert metrics["passed"] is True
        finally:
            close_gateway_process(gateway)
