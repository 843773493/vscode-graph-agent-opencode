from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import httpx
import pytest

from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
)
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.custom_tool_test_workspace import (
    custom_tool_workspace_template_copy_paths,
)
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.model_stream.frozen_provider import (
    create_frozen_provider_activation,
    create_frozen_provider_prepare,
    frozen_provider_wire,
)
from tests.support.paths import chromium_temporary_environment, output_root_for_test
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)
from tests.support.rollout.web_boundary_seeding import (
    boundary_item,
    tool_call_payload,
    tool_result_payload,
)
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.support.workspaces import prepare_test_workspace

STATIC_LONG_SESSION_ID = "ses_019be55bdf8d715d8efbb8419fbc7cd7"
LONG_SESSION_TOOL_CALL_ID = "call_chat_reasoning_tool"
LONG_SESSION_TOOL_NAME = "invoke_extension_tool"
LONG_SESSION_FINAL_TEXT = "工具调用完成"
LONG_SESSION_LARGE_JSON_LENGTH = 65536
LARGE_ARGUMENT_TURNS = frozenset({121, 128})


def _call_arguments(turn_number: int) -> dict[str, object]:
    """invoke_extension_tool 调用参数；被断言的轮次序列化后恰好 65536 字符。"""
    marker = f"turn-{turn_number:04d}"
    line = f"LARGE_CALL {marker}|record=000000|status=ok|source=canonical_fixture"

    def arguments_for(query_context: str) -> dict[str, object]:
        return {
            "tool_name": "large_test_output",
            "arguments": {
                "lines": 768,
                "marker": marker,
                "output_bytes": 65536,
                "query_context": query_context,
            },
        }

    if turn_number not in LARGE_ARGUMENT_TURNS:
        return arguments_for(f"LARGE_CALL {marker}_BEGIN\nLARGE_CALL {marker}_END\n")

    def serialized_length(query_context: str) -> int:
        return len(json.dumps(arguments_for(query_context), ensure_ascii=False))

    # 每个中间行在 JSON 字符串里贡献 len(line) 个字符和 1 个转义换行
    # （换行符序列化为 2 个字符）；剩余字符用 END 行补空格凑满精确长度。
    base_context = (
        "\n".join([f"LARGE_CALL {marker}_BEGIN", f"LARGE_CALL {marker}_END"]) + "\n"
    )
    base_length = serialized_length(base_context)
    line_length = len(line) + 2
    remaining = LONG_SESSION_LARGE_JSON_LENGTH - base_length
    line_count = remaining // line_length
    filler_length = remaining - line_count * line_length
    parts = [f"LARGE_CALL {marker}_BEGIN"]
    parts.extend([line] * line_count)
    parts.append(f"LARGE_CALL {marker}_END" + " " * filler_length)
    arguments = arguments_for("\n".join(parts) + "\n")
    actual_length = len(json.dumps(arguments, ensure_ascii=False))
    if actual_length != LONG_SESSION_LARGE_JSON_LENGTH:
        raise RuntimeError(
            "大参数 JSON 长度校准失败: "
            f"expected={LONG_SESSION_LARGE_JSON_LENGTH}, actual={actual_length}"
        )
    return arguments


def _tool_result_content(turn_number: int) -> str:
    marker = f"turn-{turn_number:04d}"
    if turn_number not in LARGE_ARGUMENT_TURNS:
        return f"LARGE_RESULT {marker}_BEGIN\nLARGE_RESULT {marker}_END\n"
    line = f"LARGE_RESULT {marker}|record=000000|status=ok|source=canonical_fixture"
    begin = f"LARGE_RESULT {marker}_BEGIN"
    end = f"LARGE_RESULT {marker}_END"
    # 总长 = 首尾行 + 尾部换行 + 每条中间行自身长度与分隔换行。
    remaining = LONG_SESSION_LARGE_JSON_LENGTH - len(begin) - len(end) - 2
    lines: list[str] = []
    while remaining >= len(line) + 1:
        lines.append(line)
        remaining -= len(line) + 1
    if remaining > 0:
        # 剩余预算由一个 0 填充行吸收：行体占 remaining-1 字符加 1 换行。
        lines.append(f"{marker}"[: remaining - 1].ljust(remaining - 1, "0"))
    content = "\n".join([begin, *lines, end]) + "\n"
    if len(content) != LONG_SESSION_LARGE_JSON_LENGTH:
        raise RuntimeError(
            "大结果长度校准失败: "
            f"expected={LONG_SESSION_LARGE_JSON_LENGTH}, actual={len(content)}"
        )
    return content


def _reasoning_items(turn_id: str, *, has_result: bool) -> tuple[CanonicalItemRecord, ...]:
    """按模板 v1 文案生成推理与摘要 item；block_id 保证逻辑 key 唯一。"""
    blocks: list[tuple[str, str, object]] = [
        ("a", PayloadKind.TEXT, "先读取 "),
        ("a-summary", PayloadKind.SUMMARY, None),
        ("b", PayloadKind.TEXT, "README，再根据工具结果作答。"),
        ("b-summary", PayloadKind.SUMMARY, None),
    ]
    if has_result:
        blocks.extend(
            [
                ("c", PayloadKind.TEXT, "已读取 README，"),
                ("c-summary", PayloadKind.SUMMARY, None),
                ("d", PayloadKind.TEXT, "整理最终答复。"),
                ("d-summary", PayloadKind.SUMMARY, None),
            ]
        )
    items: list[CanonicalItemRecord] = []
    for block_index, (suffix, payload_kind, text) in enumerate(blocks):
        item_id = f"item-rollout-reasoning-{suffix}-{turn_id}"
        if payload_kind is PayloadKind.SUMMARY:
            payload: object = {
                "summary_id": item_id,
                "view_revision": "0",
                "content": [
                    {
                        "id": f"reasoning-item:{suffix}-{turn_id}",
                        "type": "reasoning",
                        "summary": [
                            {"type": "summary_text", "text": "summary-large"}
                        ],
                    }
                ],
            }
        else:
            payload = text
        items.append(
            boundary_item(
                item_id=item_id,
                semantic_kind=SemanticKind.REASONING,
                payload_kind=payload_kind,
                status=CanonicalItemStatus.COMPLETED,
                payload=payload,
                turn_id=turn_id,
                projection_identity=ItemProjectionIdentity(
                    model_call_id=f"model-call-{turn_id}",
                    block_id=f"reasoning-{suffix}-{turn_id}", block_index=block_index,
                ),
            )
        )
    return tuple(items)


def seed_long_rollout_history(workspace_root: Path) -> None:
    """只为展示准备128轮；本测试不据此证明执行、fork或恢复。"""
    sessions_dir = workspace_root / ".boxteam/sessions"
    manifest = json.loads((workspace_root / "rollout-fixture.json").read_text())
    session = next(item for item in manifest["sessions"] if item["session_id"] == STATIC_LONG_SESSION_ID)
    assert session["turn_count"] == 128
    seed_catalog_session_bundle(sessions_dir, STATIC_LONG_SESSION_ID, title=session["title"], current_provider_id="primary")
    saver = RolloutCheckpointSaver(sessions_dir)
    owner = saver.forks.source.resolve_main_thread_owner(STATIC_LONG_SESSION_ID)
    pending = []
    for index in range(1, 129):
        turn_id = f"job-{index:04d}"
        has_result = index % 8 == 0
        model_call_id = f"model-call-{turn_id}"
        call = tool_call_payload(LONG_SESSION_TOOL_CALL_ID, LONG_SESSION_TOOL_NAME, _call_arguments(index), model_call_id=model_call_id)
        reasoning = _reasoning_items(turn_id, has_result=has_result)
        items = [*reasoning[:4], boundary_item(
            item_id=f"item-rollout-call-{turn_id}", semantic_kind=SemanticKind.TOOL_CALL,
            payload_kind=PayloadKind.TOOL_CALL, status=CanonicalItemStatus.COMPLETED,
            payload=call, turn_id=turn_id, projection_identity=ItemProjectionIdentity(
                model_call_id=model_call_id, block_id=call["tool_call_id"], block_index=0),
        )]
        # 有调用但无结果的展示轮次属于interrupted，不能伪装成零输出completed_empty。
        final_id = None
        if has_result:
            final_id = f"item-rollout-final-{turn_id}"
            items.extend((boundary_item(
                item_id=f"item-rollout-result-{turn_id}", semantic_kind=SemanticKind.TOOL_RESULT,
                payload_kind=PayloadKind.TOOL_RESULT, status=CanonicalItemStatus.COMPLETED,
                payload=tool_result_payload(LONG_SESSION_TOOL_CALL_ID, LONG_SESSION_TOOL_NAME, _tool_result_content(index), model_call_id=model_call_id),
                turn_id=turn_id, wire_role="tool"), *reasoning[4:], boundary_item(
                    item_id=final_id, semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                    payload_kind=PayloadKind.TEXT, status=CanonicalItemStatus.COMPLETED,
                    payload=LONG_SESSION_FINAL_TEXT, turn_id=turn_id),
            ))
        # 展示夹具通过owner写入acceptance；不启动Agent或执行工具。
        accepted = saver.accept_turn(
            STATIC_LONG_SESSION_ID, thread_id=owner.thread_id,
            accepted_ingress_id=f"ingress-{turn_id}", acceptance_idempotency_key=f"acceptance-{turn_id}",
            job_id=f"job-{turn_id}", admission_idempotency_key=f"execution-admission-{turn_id}",
            payload=f"继续处理第 {index} 轮的大工具历史查询，保留可复查的工具证据。",
            payload_kind=PayloadKind.TEXT, turn_id=turn_id,
            root_item_id=f"item-boundary-user-{turn_id}", initial_execution_id=f"execution-{turn_id}",
        )
        pending.append((turn_id, str(accepted["initial_execution_id"]), tuple(items), has_result, final_id))
    # 先准备历史模型记录，再安装显示输出；未闭合调用不会被当成后继模型的可执行前缀。
    prepare = create_frozen_provider_prepare(frozen_provider_wire, create_frozen_provider_activation())
    assemblies = {}
    for turn_id, execution_id, *_ in pending:
        admission = saver.execution_admission(STATIC_LONG_SESSION_ID, thread_id=owner.thread_id, execution_id=execution_id)
        with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
              execution_admission_scope(admission, langgraph_thread_id=owner.thread_id)):
            assemblies[turn_id] = prepare(saver, key=f"progressive-display:{turn_id}").assembly_id
    for turn_id, execution_id, items, has_result, final_id in pending:
        saver.converge_execution(
            STATIC_LONG_SESSION_ID, thread_id=owner.thread_id, turn_id=turn_id,
            execution_id=execution_id, items=items, assembly_id=assemblies[turn_id],
            outcome="completed" if has_result else "interrupted",
            turn_status="completed" if has_result else "interrupted", final_item_id=final_id,
        )


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="integration",
        project_root=project_root,
    )
    reset_gateway_persistent_state(workspace_root=output_root / "workspace")
    template = project_root / "tests/fixtures/workspaces/custom_tool_test_workspace"
    workspace_root = prepare_test_workspace(
        workspace_root=output_root / "workspace",
        template_root=template,
        template_items=custom_tool_workspace_template_copy_paths(template),
        shared_skill_root=project_root / "resources" / "skills",
    )
    # 为长历史浏览器场景在副本上生成逐轮工具调用数据。
    with fixture_catalog_scope():
        seed_long_rollout_history(workspace_root)
    return str(workspace_root)


@pytest.fixture(scope="module")
def browser_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Generator[tuple[str, Path, ModelStreamReplay], None, None]:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    assert Path(integration_workspace_config_path).is_file()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with model_stream_replay(
        context.output_root, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="read-only-128-turn-progressive-history",
    ) as replay:
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="rollout-history-browser-backend",
            asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={**replay.environment, "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{port_block.port(20)}"},
        )
        try:
            yield f"http://127.0.0.1:{backend.port}", workspace_root, replay
        finally:
            close_backend_process(backend)
        assert list(replay.artifacts.glob("*.json")) == [], "只读长历史不得分配模型调用"
        assert not any(event["phase"] == "agent.graph.compiled" for event in replay.control.events), "只读长历史不得物化graph"


@pytest.fixture
async def browser_backend_client(
    browser_backend: tuple[str, Path, ModelStreamReplay],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=browser_backend[0],
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": read_workspace_id(browser_backend[1])},
        timeout=60,
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_rollout_history_around_loading_real_web_chain(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
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
    sessions_dir = Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_dir)
    static_rollout_root = (
        resolver.resolve_thread_node(session_id, resolver.main_thread_id(session_id))
        / "rollout"
    )
    rollout_path = static_rollout_root / "rollout.jsonl"
    assert rollout_path.is_file()
    assert not list(static_rollout_root.glob("segment-*.jsonl"))
    item_records = [
        json.loads(line)
        for line in rollout_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tool_call_records = [
        record
        for record in item_records
        if record.get("record_type") == "item"
        and record.get("semantic_kind") == "tool_call"
    ]
    tool_result_records = [
        record
        for record in item_records
        if record.get("record_type") == "item"
        and record.get("semantic_kind") == "tool_result"
    ]
    final_text_records = [
        record
        for record in item_records
        if record.get("record_type") == "item"
        and record.get("semantic_kind") == "assistant_output"
        and record.get("payload_kind") == "text"
        and record.get("status") == "completed"
    ]
    assert len(tool_call_records) == 128
    assert len(tool_result_records) == 16
    assert len(final_text_records) == 16
    for record in tool_call_records:
        assert "payload_ref" not in record
        payload = record["payload"]
        call = payload
        assert call["name"] == LONG_SESSION_TOOL_NAME
        assert call["tool_call_id"] == f"model-call-{record['turn_id']}:tool-call:{LONG_SESSION_TOOL_CALL_ID}"
        arguments = call["args"]
        arguments_json = json.dumps(arguments, ensure_ascii=False)
        marker = arguments["arguments"]["marker"]
        assert marker == f"turn-{int(record['turn_id'].removeprefix('job-')):04d}"
        assert arguments["arguments"]["query_context"].startswith(
            f"LARGE_CALL {marker}_BEGIN"
        )
        # 大参数只放在被断言精确长度的轮次；其余轮次保持结构一致的短参数。
        if int(record["turn_id"].removeprefix("job-")) in LARGE_ARGUMENT_TURNS:
            assert len(arguments_json) == LONG_SESSION_LARGE_JSON_LENGTH
        else:
            assert len(arguments_json) < LONG_SESSION_LARGE_JSON_LENGTH
    for record in tool_result_records:
        marker = f"turn-{int(record['turn_id'].removeprefix('job-')):04d}"
        content = record["payload"]["content"]
        assert content.startswith(f"LARGE_RESULT {marker}_BEGIN")
        if int(record["turn_id"].removeprefix("job-")) in LARGE_ARGUMENT_TURNS:
            assert len(content) == LONG_SESSION_LARGE_JSON_LENGTH
    for record in final_text_records:
        assert record["payload"] == LONG_SESSION_FINAL_TEXT
    session_response = await browser_backend_client.get(
        f"/api/v1/sessions/{session_id}"
    )
    assert session_response.status_code == 200, session_response.text
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
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary_environment:
            environment.update(temporary_environment)
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
        (artifacts / "browser.stdout.log").write_text(browser_result.stdout)
        (artifacts / "browser.stderr.log").write_text(browser_result.stderr)
        assert browser_result.returncode == 0, (
            "rollout 历史浏览器集成失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["defaultProjectionSafe"] is True
        assert result["detailProjectionSafe"] is True
        assert result["canonicalMixedMessageRestored"] is True
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
