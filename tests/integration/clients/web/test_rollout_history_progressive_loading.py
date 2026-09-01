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

from app.core.session_paths import SessionPathResolver
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.orchestration.activity_runtime import (
    ActivityHandlerRegistry,
    ActivityRuntime,
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

STATIC_LONG_SESSION_ID = "ses_9f4e2c7a1b6d4830a5e8f2c1d7b90436"
COMPACTION_STREAM_TURN_ID = "job-0126"
ACTIVITY_STREAM_TURN_ID = "job-0125"


async def seed_compaction_message_stream(workspace_root: Path) -> None:
    """在隔离测试工作区写入可通过 Web snapshot 恢复的重复压缩 Activity。"""

    sessions_root = workspace_root / ".boxteam" / "sessions"
    resolver = SessionPathResolver(sessions_root)
    resolver.initialize()
    store = MessageStreamStore(path_resolver=resolver)
    writer = await store.open(
        session_id=STATIC_LONG_SESSION_ID,
        turn_id=COMPACTION_STREAM_TURN_ID,
    )
    runtime = ActivityRuntime(writer, ActivityHandlerRegistry())
    await runtime.started(
        activity_id="browser_compaction_1",
        kind="context.compaction",
        summary="第一次压缩服务端摘要",
    )
    await runtime.completed(
        activity_id="browser_compaction_1",
        kind="context.compaction",
        summary="第一次压缩已提交",
    )
    await runtime.started(
        activity_id="browser_compaction_2",
        kind="context.compaction",
        summary="第二次压缩服务端摘要",
    )
    await runtime.failed(
        activity_id="browser_compaction_2",
        kind="context.compaction",
        outcome="outcome_unknown",
        summary="第二次压缩结果未知",
    )
    await writer.close_completed()


async def seed_additional_message_stream_display_cases(workspace_root: Path) -> None:
    """写入通用 Activity 和未知工具结果的历史消息流。"""

    sessions_root = workspace_root / ".boxteam" / "sessions"
    resolver = SessionPathResolver(sessions_root)
    resolver.initialize()
    store = MessageStreamStore(path_resolver=resolver)

    activity_writer = await store.open(
        session_id=STATIC_LONG_SESSION_ID,
        turn_id=ACTIVITY_STREAM_TURN_ID,
    )
    activity_runtime = ActivityRuntime(activity_writer, ActivityHandlerRegistry())
    await activity_runtime.started(
        activity_id="browser_approval_wait",
        kind="approval.wait",
        summary="等待浏览器审批",
    )
    await activity_runtime.updated(
        activity_id="browser_approval_wait",
        kind="approval.wait",
        status="waiting",
    )
    await activity_runtime.started(
        activity_id="browser_subagent_done",
        kind="subagent.run",
    )
    await activity_runtime.completed(
        activity_id="browser_subagent_done",
        kind="subagent.run",
    )
    await activity_runtime.started(
        activity_id="browser_resource_unknown",
        kind="resource.operation",
        resource_refs=("resource_browser_1",),
    )
    await activity_runtime.failed(
        activity_id="browser_resource_unknown",
        kind="resource.operation",
        outcome="outcome_unknown",
        summary="资源操作结果无法确认",
    )
    await activity_runtime.failed(
        activity_id="browser_private_unknown",
        kind="provider.private",
        outcome="outcome_unknown",
    )
    await activity_writer.commit(
        "activity.updated",
        {
            "activity_id": "browser_private_unknown",
            "kind": "provider.private",
            "status": "unknown",
            "summary": "Provider 私有 Activity 状态无法确认",
        },
    )
    await activity_writer.commit(
        "tool_call",
        {
            "tool_call_id": "browser_unknown_call",
            "tool_name": "shell",
            "arguments": {"command": "touch side-effect"},
            "status": "completed",
        },
    )
    await activity_writer.commit(
        "tool.started",
        {
            "tool_execution_id": "browser_unknown_execution",
            "tool_call_id": "browser_unknown_call",
            "tool_name": "shell",
        },
    )
    await activity_writer.commit(
        "tool.completed",
        {
            "tool_execution_id": "browser_unknown_execution",
            "tool_call_id": "browser_unknown_call",
            "tool_name": "shell",
            "status": "completed",
            "outcome": "outcome_unknown",
            "completion_reason": "execution_lost",
        },
    )
    # 保留 approval.wait 的 waiting 状态，用于验证前端不会把等待中的
    # Activity 误显示成已完成；该测试工作区在 fixture 生命周期结束时销毁。


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
            "model": "rollout-history-browser-stub",
            "api_key": "e2e-local-model-key",
            "custom_llm_provider": "openai",
        }
    )
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # 该历史 fixture 原本由 handwritten provider 生成，但本测试的 replay
    # 需要真正启动一轮新 Job；复制后的工作区统一切到测试 stub provider，
    # 不修改只读 fixture 源目录。
    session_path = (
        workspace_root
        / ".boxteam"
        / "sessions"
        / STATIC_LONG_SESSION_ID
        / "session.json"
    )
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["current_provider_id"] = "primary"
    session_path.write_text(
        json.dumps(session, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with openai_chat_stub(port_block.port(10)):
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="rollout-history-browser-backend",
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
async def test_rollout_history_around_loading_real_web_chain(
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
        pytest.fail("Rollout 历史浏览器集成需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    session_id = STATIC_LONG_SESSION_ID
    static_rollout_root = (
        Path(integration_workspace_root_path)
        / ".boxteam"
        / "sessions"
        / session_id
        / "rollout"
    )
    rollout_path = static_rollout_root / "rollout.jsonl"
    assert rollout_path.is_file()
    assert not list(static_rollout_root.glob("segment-*.jsonl"))
    large_records = []
    for line in rollout_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        message = record.get("message", {})
        data = message.get("data", {}) if isinstance(message, dict) else {}
        tool_calls = data.get("tool_calls", []) if isinstance(data, dict) else []
        if any(
            isinstance(call, dict)
            and isinstance(call.get("args"), dict)
            and isinstance(call["args"].get("arguments"), dict)
            and isinstance(call["args"]["arguments"].get("query_context"), str)
            for call in tool_calls
        ):
            large_records.append(record)
    assert len(large_records) >= 10
    assert any(
        record.get("message", {}).get("data", {}).get("tool_calls", [{}])[0].get("name")
        == "invoke_custom_tool"
        for record in large_records
        if record.get("message", {}).get("data", {}).get("tool_calls")
    )
    assert all(
        "payload_ref" not in record.get("message", {}) for record in large_records
    )
    session_response = await browser_backend_client.get(
        f"/api/v1/sessions/{session_id}"
    )
    assert session_response.status_code == 200, session_response.text
    await seed_compaction_message_stream(Path(integration_workspace_root_path))
    await seed_additional_message_stream_display_cases(Path(integration_workspace_root_path))

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
    result_path = artifacts / "rollout-history-progressive-result.json"
    screenshot_path = artifacts / "rollout-history-progressive-failure.png"
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
                json={"tracking": {"source": "rollout-history-browser-test"}},
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
                "BOXTEAM_BROWSER_SESSION_ID": session_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        browser_result = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                "tests/integration/clients/web/rollout_history_progressive_loading.mjs",
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        assert browser_result.returncode == 0, (
            "rollout 历史浏览器集成失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["defaultProjectionSafe"] is True
        assert result["canonicalMixedMessageRestored"] is True
        assert result["compactionActivityIds"] == [
            "browser_compaction_1",
            "browser_compaction_2",
        ]
        assert result["compactionCompletedVisible"] is True
        assert result["compactionFailedVisible"] is True
        assert result["activityStatusIds"] == [
            "browser_approval_wait",
            "browser_subagent_done",
            "browser_resource_unknown",
            "browser_private_unknown",
        ]
        assert result["approvalWaitingVisible"] is True
        assert result["subagentCompletedVisible"] is True
        assert result["resourceUnknownVisible"] is True
        assert result["genericActivityUnknownVisible"] is True
        assert result["unknownToolVisible"] is True
        assert result["responseActionsVisible"] is True
        assert result["responseActionLabels"] == [
            "朗读（暂未开放）",
            "复制",
            "有帮助（暂未开放）",
            "没有帮助（暂未开放）",
        ]
        assert result["boundaryResponseActionsVisible"] is True
        assert result["boundaryResponseActionLabels"] == [
            "朗读（暂未开放）",
            "复制（暂无可复制内容）",
            "有帮助（暂未开放）",
            "没有帮助（暂未开放）",
        ]
        assert result["toolDetailsLoaded"] is True
        assert result["largeToolSummarySafe"] is True
        assert result["largeToolDetailsBounded"] is True
        assert result["aroundOrdinals"] == list(range(61, 68))
        assert result["aroundCursorsPresent"] is True
        assert result["aroundBidirectionalSafe"] is True
        assert result["beforeOrdinals"] == [
            [121, 122, 123],
            [118, 119, 120],
            [115, 116, 117],
        ]
        # SQLite reader 本身远低于该值；这里约束完整浏览器/Gateway 链路，
        # 不把前端渲染预算误当成数据库耗时。
        assert result["historyRequestP95Ms"] < 200
        # 首次真实浏览器 prepend 包含 Virtuoso 首次布局和 Gateway 冷路径；
        # 热路径 API p95 仍由上面的 100ms 断言约束。
        assert result["browserPrependMs"] < 500
        assert result["noPageErrors"] is True
    finally:
        close_gateway_process(gateway)
