from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from pathlib import Path

import commentjson
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_migration import migrate_workspace_session_catalog
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
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
from tests.support.web_boundary_seeding import (
    boundary_item,
)
from tests.support.workspaces import prepare_default_test_workspace

STATIC_LONG_SESSION_ID = "ses_9f4e2c7a1b6d4830a5e8f2c1d7b90436"
LONG_SESSION_TOOL_CALL_ID = "call_chat_reasoning_tool"
LONG_SESSION_TOOL_NAME = "invoke_extension_tool"
LONG_SESSION_FINAL_TEXT = "工具调用完成"
LONG_SESSION_LARGE_JSON_LENGTH = 65536
LARGE_ARGUMENT_TURNS = frozenset({121, 128})


def _call_arguments(turn_number: int) -> dict[str, object]:
    """invoke_extension_tool 调用参数；被断言的轮次序列化后恰好 65536 字符。"""
    marker = f"turn-{turn_number:04d}"
    line = f"LARGE_CALL {marker}|record=000000|status=ok|source=handwritten_sse"

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
    line = f"LARGE_RESULT {marker}|record=000000|status=ok|source=handwritten_sse"
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


def _reasoning_items(turn_id: str, *, has_result: bool) -> tuple[object, ...]:
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
    items: list[object] = []
    for suffix, payload_kind, text in blocks:
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
                metadata={"block_id": f"reasoning-{suffix}-{turn_id}"},
            )
        )
    return tuple(items)


def seed_long_rollout_history(workspace_root: Path) -> None:
    """以模板 v1 文案为基线，用当前 Saver 重写 128 轮长会话数据。

    模板 rollout 是 v1 dispatch 旧格式，runtime 与 legacy 导入器都无法读取；
    复制出的工作区副本先读取 v1 的轮次结构与用户文案，再按真实运行时的
    提交顺序（accept_turn → converge/put → put）重写为 schema-4 数据：
    结果轮的 canonical item 由 LangChainMessageCodec 从消息生成，保证与
    checkpoint message projection 的内容哈希一致；无结果轮由 checkpoint
    直接派生 tool_call item，并以 completed_empty 收敛。
    """
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    session_dir = get_session_path_resolver(sessions_dir).resolve_session_node(
        STATIC_LONG_SESSION_ID
    )
    rollout_path = session_dir / "rollout" / "rollout.jsonl"
    if not rollout_path.is_file():
        raise FileNotFoundError(f"模板长会话 rollout 缺失: {rollout_path}")
    records = [
        json.loads(line)
        for line in rollout_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    turn_order: list[str] = []
    user_texts: dict[str, str] = {}
    result_turns: set[str] = set()
    for record in records:
        turn_id = record["turn_id"]
        if turn_id not in user_texts and turn_id not in result_turns:
            turn_order.append(turn_id)
        if record.get("role") == "user":
            user_texts[turn_id] = record["message"]["data"]["content"]
        elif record.get("role") == "tool":
            result_turns.add(turn_id)
    if len(turn_order) != 128 or len(user_texts) != 128 or len(result_turns) != 16:
        raise RuntimeError(
            "模板长会话结构与基线不符: "
            f"turns={len(turn_order)}, users={len(user_texts)}, results={len(result_turns)}"
        )
    shutil.rmtree(session_dir / "rollout")
    saver = RolloutCheckpointSaver(sessions_dir)
    codec = LangChainMessageCodec()
    timestamp = datetime.now(UTC).isoformat()
    for index, turn_id in enumerate(turn_order, start=1):
        has_result = turn_id in result_turns
        model_call_id = f"model-call-{turn_id}"
        message_metadata = {
            "message_metadata": {"turn_id": turn_id},
            "model_call_id": model_call_id,
        }
        messages: list[object] = [
            HumanMessage(
                content=user_texts[turn_id],
                id=f"rollout-user-{turn_id}",
                response_metadata={
                    "message_metadata": {"turn_id": turn_id}
                },
            ),
            AIMessage(
                content="",
                id=f"rollout-call-{turn_id}",
                tool_calls=[
                    {
                        "name": LONG_SESSION_TOOL_NAME,
                        "args": _call_arguments(index),
                        "id": LONG_SESSION_TOOL_CALL_ID,
                        "type": "tool_call",
                    }
                ],
                response_metadata=message_metadata,
            ),
        ]
        if has_result:
            messages.extend(
                [
                    ToolMessage(
                        content=_tool_result_content(index),
                        tool_call_id=LONG_SESSION_TOOL_CALL_ID,
                        name=LONG_SESSION_TOOL_NAME,
                        id=f"rollout-result-{turn_id}",
                        status="success",
                        response_metadata=message_metadata,
                    ),
                    AIMessage(
                        content=LONG_SESSION_FINAL_TEXT,
                        id=f"rollout-final-{turn_id}",
                        response_metadata=message_metadata,
                    ),
                ]
            )
        accepted = saver.accept_turn(
            STATIC_LONG_SESSION_ID,
            accepted_ingress_id=f"ingress-rollout-{turn_id}",
            acceptance_idempotency_key=f"acceptance-rollout-{turn_id}",
            payload=user_texts[turn_id],
            payload_kind=PayloadKind.TEXT,
            turn_id=turn_id,
            root_item_id=f"item-rollout-user-{turn_id}",
            initial_execution_id=f"execution-rollout-{turn_id}",
        )

        def put_checkpoint(turn_messages: list[object], checkpoint_turn_id: str) -> None:
            checkpoint = empty_checkpoint()
            checkpoint["id"] = f"checkpoint-rollout-{checkpoint_turn_id}"
            checkpoint["channel_values"] = {"messages": turn_messages}
            checkpoint["channel_versions"] = {"messages": "1"}
            checkpoint["updated_channels"] = ["messages"]
            saver.put(
                build_checkpoint_config(STATIC_LONG_SESSION_ID),
                checkpoint,
                {"source": f"rollout-history-fixture-{checkpoint_turn_id}", "step": 1},
                {"messages": "1"},
            )

        if has_result:
            converge_items: list[object] = []
            for message in messages[1:]:
                converge_items.extend(
                    codec.items_for_message(
                        message,
                        item_sequence=1,
                        message_id=message.id,
                        turn_id=turn_id,
                        timestamp=timestamp,
                        model_call_id=model_call_id,
                    )
                )
            saver.converge_execution(
                STATIC_LONG_SESSION_ID,
                turn_id=turn_id,
                execution_id=str(accepted["initial_execution_id"]),
                outcome="completed",
                turn_status="completed",
                items=tuple(converge_items),
                final_item_id=f"item-rollout-final-{turn_id}",
            )
            put_checkpoint(messages, turn_id)
        else:
            put_checkpoint(messages, turn_id)
            saver.converge_execution(
                STATIC_LONG_SESSION_ID,
                turn_id=turn_id,
                execution_id=str(accepted["initial_execution_id"]),
                outcome="completed_empty",
                turn_status="completed_empty",
            )
        saver.append_items(STATIC_LONG_SESSION_ID, list(_reasoning_items(turn_id, has_result=has_result)))


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
    # 模板 rollout 是 v1 dispatch 旧格式；先完成一次性 catalog 迁移，
    # 然后统一在副本上以当前 Saver 重写 schema-4 数据。
    asyncio.run(migrate_workspace_session_catalog(workspace_root=workspace_root))
    seed_long_rollout_history(workspace_root)
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
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    session_path = (
        get_session_path_resolver(sessions_dir).resolve_session_node(
            STATIC_LONG_SESSION_ID
        )
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
    sessions_dir = Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    static_rollout_root = (
        get_session_path_resolver(sessions_dir).resolve_session_node(session_id)
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
        (call,) = payload["tool_calls"]
        assert call["name"] == LONG_SESSION_TOOL_NAME
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
