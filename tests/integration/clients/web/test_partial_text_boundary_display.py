from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import commentjson
import httpx
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_migration import migrate_workspace_session_catalog
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.stubs.http_stubs import openai_chat_stub
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.paths import output_root_for_test
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import close_backend_process, start_backend_process
from tests.support.workspaces import prepare_default_test_workspace

PARTIAL_TEXT_SESSION_ID = "ses_b1a2c3d4e5f6478899aabbccddeeff04"
PARTIAL_TEXT_TURN_ID = "boundary-turn-0004"
PARTIAL_TEXT_ITEM_ID = "item-boundary-assistant-partial-text-0004"
PARTIAL_TEXT_MESSAGE_ID = "boundary-assistant-partial-text-0004"
PARTIAL_TEXT_CONTENT = "我已经开始分析这个问题，但回答在这里被用户中断……"
PARTIAL_TEXT_USER_CONTENT = "请分析这个问题；如果我中断，请保留已经生成的正文。"


def _seed_v2_partial_text_turn(workspace_root: Path) -> None:
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    session_dir = get_session_path_resolver(sessions_dir).resolve_session_node(
        PARTIAL_TEXT_SESSION_ID
    )
    shutil.rmtree(session_dir / "rollout")

    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = saver.accept_turn(
        PARTIAL_TEXT_SESSION_ID,
        accepted_ingress_id="ingress-boundary-partial-text-0004",
        acceptance_idempotency_key="acceptance-boundary-partial-text-0004",
        payload=PARTIAL_TEXT_USER_CONTENT,
        payload_kind=PayloadKind.TEXT,
        turn_id=PARTIAL_TEXT_TURN_ID,
        root_item_id="item-boundary-user-partial-text-0004",
        initial_execution_id="execution-boundary-partial-text-0004",
    )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "checkpoint-boundary-partial-text-0004"
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(
                content=PARTIAL_TEXT_USER_CONTENT,
                id="boundary-user-partial-text-0004",
                response_metadata={
                    "message_metadata": {"turn_id": PARTIAL_TEXT_TURN_ID}
                },
            )
        ]
    }
    checkpoint["channel_versions"] = {"messages": "1"}
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        build_checkpoint_config(PARTIAL_TEXT_SESSION_ID),
        checkpoint,
        {"source": "partial-text-boundary-v2-fixture", "step": 1},
        {"messages": "1"},
    )
    partial_item = CanonicalItemRecord.create(
        item_sequence=1,
        item_id=PARTIAL_TEXT_ITEM_ID,
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.PARTIAL,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "model-call-boundary-partial-text-0004",
            "invocation_id": str(accepted["initial_execution_id"]),
        },
        payload=PARTIAL_TEXT_CONTENT,
        metadata={
            "projection_message_id": PARTIAL_TEXT_MESSAGE_ID,
            "completion_reason": "user_interrupt",
            "phase": "assistant_text",
        },
        turn_id=PARTIAL_TEXT_TURN_ID,
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id="message-boundary-partial-text-0004",
        wire_role="assistant",
    )
    saver.converge_execution(
        PARTIAL_TEXT_SESSION_ID,
        turn_id=PARTIAL_TEXT_TURN_ID,
        execution_id=str(accepted["initial_execution_id"]),
        outcome="cancelled",
        turn_status="cancelled",
        items=(partial_item,),
    )


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="integration",
        project_root=project_root,
    )
    workspace_root = prepare_default_test_workspace(
        workspace_root=output_root / "workspace",
        template_root=project_root / "tests" / "fixtures" / "workspaces" / "custom_tool_test_workspace",
        shared_skill_root=project_root / "resources" / "skills",
    )
    # 模板是旧 JSON 权威索引形态；先建立 SQLite catalog authority，
    # 否则后端启动会按唯一 resolver 契约 fail-closed。
    asyncio.run(migrate_workspace_session_catalog(workspace_root=workspace_root))
    _seed_v2_partial_text_turn(workspace_root)
    return str(workspace_root)


@pytest.fixture(scope="module")
def browser_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Generator[tuple[str, Path], None, None]:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    provider = next(
        item for item in config["llm"]["providers"] if item["id"] == "primary"
    )
    provider.update(
        {
            "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
            "model": "partial-text-boundary-browser-stub",
            "api_key": "${BOXTEAM_TEST_MODEL_API_KEY}",
            "custom_llm_provider": "openai",
        }
    )
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with openai_chat_stub(port_block.port(10)):
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="partial-text-boundary-browser-backend",
            env_overrides={
                "BOXTEAM_TEST_MODEL_API_KEY": "partial-text-boundary-local-key"
            },
        )
        try:
            yield f"http://127.0.0.1:{backend.port}", workspace_root
        finally:
            close_backend_process(backend)


@pytest.fixture
async def browser_backend_client(
    browser_backend: tuple[str, Path],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=browser_backend[0],
        headers={"X-Local-Token": "local-dev-token"},
        timeout=60,
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_partial_text_cancelled_turn_is_explicit_in_real_web_chain(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path],
    browser_backend_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("partial text 浏览器集成需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    sessions_dir = Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    rollout_path = (
        get_session_path_resolver(sessions_dir)
        .resolve_session_node(PARTIAL_TEXT_SESSION_ID)
        / "rollout"
        / "rollout.jsonl"
    )
    boundary_record = next(
        record
        for record in map(json.loads, rollout_path.read_text(encoding="utf-8").splitlines())
        if record.get("turn_id") == PARTIAL_TEXT_TURN_ID
        and record.get("item_id") == PARTIAL_TEXT_ITEM_ID
    )
    assert boundary_record["format_version"] == 2
    assert boundary_record["record_type"] == "item"
    assert boundary_record["semantic_kind"] == "assistant_output"
    assert boundary_record["payload_kind"] == "text"
    assert boundary_record["status"] == "partial"
    assert boundary_record["wire_role"] == "assistant"
    assert boundary_record["payload"] == PARTIAL_TEXT_CONTENT
    assert boundary_record["metadata"]["completion_reason"] == "user_interrupt"
    assert "role" not in boundary_record
    assert "message" not in boundary_record

    history_response = await browser_backend_client.post(
        f"/api/v1/sessions/{PARTIAL_TEXT_SESSION_ID}/history",
        json={
            "turn_ids": [PARTIAL_TEXT_TURN_ID],
            "include": ["user", "text", "assistant_text", "final_response"],
        },
    )
    assert history_response.status_code == 200, history_response.text
    history_item = history_response.json()["data"]["items"][0]
    assert history_item["status"] == "cancelled"
    assert history_item["final_response"] == ""
    assert history_item["assistant_text"] == [PARTIAL_TEXT_CONTENT]
    partial_response_part = next(
        part
        for part in history_item["response_parts"]
        if part.get("text") == PARTIAL_TEXT_CONTENT
    )
    assert partial_response_part["kind"] == "text"
    assert partial_response_part["final"] is False

    port_block = integration_port_block_for_file(Path(request.node.fspath))
    gateway = start_gateway_process(
        workspace_root=Path(browser_backend[1]),
        default_backend_url=browser_backend[0],
        port=port_block.port(20),
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(
                project_root / "src" / "clients" / "web" / "dist"
            ),
        },
    )
    artifacts = Path(browser_backend[1]).parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "partial-text-boundary-display-result.json"
    screenshot_path = artifacts / "partial-text-boundary-display-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            guest = await client.post(
                "/api/gateway/users/guest",
                json={"tracking": {"source": "partial-text-boundary-browser-test"}},
            )
            assert guest.status_code == 200, guest.text
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            workspace_id = workspaces.json()["data"]["active_workspace_id"]
            assert isinstance(workspace_id, str) and workspace_id

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_SESSION_ID": PARTIAL_TEXT_SESSION_ID,
                "BOXTEAM_BROWSER_TURN_ID": PARTIAL_TEXT_TURN_ID,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        browser_result = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                "tests/integration/clients/web/partial_text_boundary_display.mjs",
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert browser_result.returncode == 0, (
            "partial text 浏览器集成失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["apiCancelled"] is True
        assert result["apiNonFinalText"] is True
        assert result["partialTextVisible"] is True
        assert result["cancelledStatusVisible"] is True
        assert result["independentRetryVisible"] is False
        assert result["failedRetryLabelVisible"] is False
        assert result["renderErrorVisible"] is False
        assert result["noPageErrors"] is True
    finally:
        close_gateway_process(gateway)
