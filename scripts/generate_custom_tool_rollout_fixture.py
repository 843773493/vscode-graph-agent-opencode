"""生成 custom_tool_test_workspace 使用的静态 rollout 历史 fixture。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path.cwd().resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver

FIXTURE_VERSION: Final = 3
STATIC_LONG_SESSION_ID: Final = "ses_a1b2c3d4e5f6478899aabbccddeeff00"
REAL_MODEL_SESSION_ID: Final = "ses_8128d7f0a4b64aa0b3f1c9e7d2a65018"
LARGE_LONG_SESSION_ID: Final = "ses_9f4e2c7a1b6d4830a5e8f2c1d7b90436"
COMPACTION_SESSION_ID: Final = "ses_4c0a1d6e7f8b49a2b5c6d7e8f9012345"
TOOL_SESSION_ID: Final = "ses_7e5d3c1b9a8f4762b4d6e8f0a1c23579"
FORK_SESSION_ID: Final = "ses_6b2d4f8a0c1e4937b5d9f1a3c7e24680"
MOCK_SESSION_IDS: Final = (
    STATIC_LONG_SESSION_ID,
    LARGE_LONG_SESSION_ID,
    COMPACTION_SESSION_ID,
    TOOL_SESSION_ID,
    FORK_SESSION_ID,
)
BASE_TIME: Final = datetime(2026, 8, 1, tzinfo=UTC)
# 刻意生成超过 64 KiB 的单条 JSONL record，验证大 canonical message 仍以内联
# 形式保存；详情接口仍按当前 64 KiB 单项预算返回有界结果并标记 detail_truncated。
LARGE_TOOL_PAYLOAD_BYTES: Final = 64 * 1024

MANUAL_PROMPTS: Final = (
    "先帮我梳理这个工作区里自定义工具的调用入口，暂时不要修改文件。",
    "我想确认大输出工具的结果会不会把聊天历史撑得很大，请给出一个小规模验证方案。",
    "请检查浏览器控制工具的说明，告诉我怎样区分页面资源和普通文件。",
    "把最近一次工具调用的结果整理成适合团队评审的短摘要。",
    "我需要一个最小的 TypeScript 改动方案，先说明风险，再决定是否执行。",
    "请搜索工作区中和 session history 相关的配置，列出值得保留的字段。",
    "这个测试偶尔会慢，先帮我区分是 Gateway、SQLite 还是浏览器渲染造成的。",
    "请设计一个能覆盖 tool_call、tool_result 和最终响应的回归用例。",
    "我刚刚改了工具参数，帮我核对调用参数和返回值是否仍然配对。",
    "把这轮排查结论写成三条可以直接贴到 issue 里的 bullet。",
    "检查一下是否存在旧 checkpoint 目录，说明它和 rollout 的区别。",
    "最后请给我一个不依赖真实模型、但能验证前端历史投影的测试入口。",
)

TOPICS: Final = (
    "会话历史加载",
    "自定义工具说明",
    "SQLite 索引",
    "浏览器资源管理",
    "TypeScript 状态更新",
    "Gateway 路由",
    "测试隔离工作区",
    "上下文压缩",
)


def _checkpoint(
    checkpoint_id: str,
    messages: list[BaseMessage],
    *,
    channel_version: int,
    event: object = None,
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    channel_values: dict[str, object] = {"messages": messages}
    if event is not None:
        channel_values["_summarization_event"] = event
    checkpoint["channel_values"] = channel_values
    version = f"{channel_version:032d}.fixture"
    checkpoint["channel_versions"] = {"messages": version}
    checkpoint["updated_channels"] = ["messages"]
    if event is not None:
        checkpoint["channel_versions"]["_summarization_event"] = version
        checkpoint["updated_channels"].append("_summarization_event")
    return checkpoint


def _stamp(turn_index: int) -> str:
    return (BASE_TIME + timedelta(minutes=turn_index)).isoformat()


def _metadata(
    turn_id: str, stamp: str, *, phase: str | None = None
) -> dict[str, object]:
    result: dict[str, object] = {
        "created_at": stamp,
        "updated_at": stamp,
        "message_metadata": {"turn_id": turn_id, "job_id": turn_id},
    }
    if phase is not None:
        result["phase"] = phase
    return result


def _large_payload(marker: str, size: int = LARGE_TOOL_PAYLOAD_BYTES) -> str:
    header = f"{marker}_BEGIN\n"
    footer = f"\n{marker}_END"
    if size <= len(header) + len(footer):
        raise ValueError("大工具 fixture payload 尺寸过小")
    line = f"{marker}|record=000000|status=ok|source=custom_tool_test_workspace\n"
    body_size = size - len(header) - len(footer)
    body = (line * ((body_size // len(line)) + 1))[:body_size]
    return f"{header}{body}{footer}"


def _large_tool_arguments(turn_index: int) -> dict[str, object]:
    marker = f"LARGE_CALL turn-{turn_index:04d}"
    return {
        "tool_name": "large_test_output",
        "arguments": {
            "lines": 768,
            "marker": f"turn-{turn_index:04d}",
            "output_bytes": LARGE_TOOL_PAYLOAD_BYTES,
            "query_context": _large_payload(marker),
        },
    }


def _large_tool_result(turn_index: int) -> str:
    return _large_payload(f"LARGE_RESULT turn-{turn_index:04d}")


def _tool_specs(turn_index: int) -> list[tuple[str, dict[str, object], str]]:
    fixture_path = f"fixture/{turn_index:04d}.json"
    mode = turn_index % 11
    if mode == 0:
        return [
            (
                "invoke_custom_tool",
                _large_tool_arguments(turn_index),
                _large_tool_result(turn_index),
            )
        ]
    if mode == 1:
        return [
            (
                "read_file",
                {"path": fixture_path, "line_start": 1, "line_end": 24},
                f"读取 {fixture_path}：包含 fixture turn {turn_index} 的示例内容。",
            ),
            (
                "search_workspace",
                {"query": "session history", "path": "src"},
                "在 src 中找到 3 处历史加载相关引用。",
            ),
        ]
    if mode == 2:
        return [
            (
                "apply_patch",
                {
                    "path": "src/history.ts",
                    "patch": "保留游标并在响应后恢复滚动锚点",
                },
                "补丁预览通过，尚未写入工作区。",
            )
        ]
    if mode == 3:
        return [
            (
                "invoke_custom_tool",
                {
                    "tool_name": "test_tool_2",
                    "arguments": {"value": f"history-check-{turn_index:04d}"},
                },
                "test_tool_2 返回结构化校验结果：ok=true。",
            )
        ]
    if mode == 4:
        return [
            (
                "run_tests",
                {"command": "bun test src/clients/web/src", "scope": "history"},
                "测试计划已生成：前端状态测试 271 项，其中历史分页相关 9 项。",
            )
        ]
    return [
        (
            "read_fixture",
            {"path": fixture_path},
            json.dumps(
                {"turn": turn_index, "result": f"fixture result {turn_index}"},
                ensure_ascii=False,
            ),
        )
    ]


def _turn_messages(turn_index: int, *, long_fixture: bool = False) -> list[BaseMessage]:
    turn_id = f"job-{turn_index:04d}"
    stamp = _stamp(turn_index)
    topic = TOPICS[(turn_index - 1) % len(TOPICS)]
    user_text = (
        MANUAL_PROMPTS[turn_index - 1]
        if long_fixture and turn_index <= len(MANUAL_PROMPTS)
        else f"继续处理第 {turn_index} 轮的{topic}问题，并保留可复查的工具证据。"
    )
    messages: list[BaseMessage] = [
        HumanMessage(
            id=f"user-{turn_index:04d}",
            content=user_text,
            response_metadata=_metadata(turn_id, stamp),
        )
    ]
    if turn_index % 17 == 0:
        messages.append(
            AIMessage(
                id=f"internal-progress-{turn_index:04d}",
                content=f"内部进度：第 {turn_index} 轮已完成上下文路由和索引检查。",
                response_metadata={
                    **_metadata(turn_id, stamp, phase="internal"),
                    "internal": True,
                    "message_metadata": {
                        "turn_id": turn_id,
                        "job_id": turn_id,
                        "internal": True,
                    },
                },
            )
        )
    tool_specs = [] if turn_index % 13 == 0 else _tool_specs(turn_index)
    if tool_specs:
        tool_calls = [
            {
                "name": tool_name,
                "args": args,
                "id": f"call-{turn_index:04d}-{call_index:02d}",
            }
            for call_index, (tool_name, args, _result) in enumerate(tool_specs, start=1)
        ]
        messages.append(
            AIMessage(
                id=f"assistant-tool-{turn_index:04d}",
                content=[
                    {
                        "type": "text",
                        "text": f"我先检查第 {turn_index} 轮的 {topic}，再给出结论。",
                    }
                ],
                tool_calls=tool_calls,
                response_metadata=_metadata(turn_id, stamp, phase="tool_call"),
            )
        )
        for call_index, (tool_name, args, tool_result) in enumerate(
            tool_specs, start=1
        ):
            messages.append(
                ToolMessage(
                    id=f"tool-result-{turn_index:04d}-{call_index:02d}",
                    content=tool_result,
                    name=tool_name,
                    tool_call_id=f"call-{turn_index:04d}-{call_index:02d}",
                    response_metadata=_metadata(turn_id, stamp, phase="tool_result"),
                )
            )

    final_text = f"模型最终响应 {turn_index}：已完成{topic}的检查，保留了用户目标、工具证据和可执行的下一步。"
    if long_fixture and turn_index == 128:
        final_text = "模型最终响应 128"
    final_blocks: list[dict[str, object]] = []
    if turn_index % 2 == 0:
        final_blocks.append(
            {
                "type": "reasoning",
                "reasoning": f"普通模型思考摘要 {turn_index}",
            }
        )
    if turn_index % 3 == 0:
        final_blocks.append(
            {
                "type": "reasoning",
                "summary": [
                    {
                        "type": "summary_text",
                        "text": f"Provider 摘要 {turn_index}",
                    }
                ],
            }
        )
    if turn_index % 4 == 0:
        encrypted_block = final_blocks[0] if final_blocks else {"type": "reasoning"}
        encrypted_block.setdefault("extras", {})
        extras = encrypted_block["extras"]
        if isinstance(extras, dict):
            extras["response_item"] = {
                "type": "reasoning",
                "encrypted_content": f"encrypted-reasoning-{turn_index:04d}",
            }
        if not final_blocks:
            final_blocks.append(encrypted_block)
    if long_fixture and turn_index == 128:
        final_blocks = [
            {
                "type": "reasoning",
                "reasoning": "普通模型思考摘要 128",
                "extras": {
                    "response_item": {
                        "type": "reasoning",
                        "encrypted_content": "encrypted-reasoning-0128",
                    }
                },
            },
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Provider 摘要 128"}],
            },
        ]
    final_content: object = (
        [*final_blocks, {"type": "text", "text": final_text}]
        if final_blocks
        else final_text
    )
    messages.append(
        AIMessage(
            id=f"assistant-final-{turn_index:04d}",
            content=final_content,
            response_metadata=_metadata(turn_id, stamp, phase="final_answer"),
        )
    )
    return messages


def _write_session_manifest(
    session_dir: Path,
    session_id: str,
    title: str,
    *,
    created_at: str,
    context_source_session_id: str | None = None,
) -> None:
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "created_at": created_at,
                "updated_at": created_at,
                "session_id": session_id,
                "workspace_id": "ws_custom_tool_fixture",
                "title": title,
                "title_source": "user",
                "current_agent_id": "default",
                "current_provider_id": "primary",
                "parent_session_id": None,
                "context_source_session_id": context_source_session_id,
                "kind": "normal",
                "delegation": None,
                "generation_origin": None,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _create_session(
    *,
    sessions_dir: Path,
    session_id: str,
    title: str,
    turn_count: int,
    long_fixture: bool = False,
    compaction_points: tuple[int, ...] = (),
    fork_source: tuple[str, str] | None = None,
) -> None:
    resolver = get_session_path_resolver(sessions_dir)
    session_dir = resolver.allocate_session_dir(session_id=session_id, title=title)
    created_at = _stamp(0)
    _write_session_manifest(
        session_dir,
        session_id,
        title,
        created_at=created_at,
        context_source_session_id=fork_source[0] if fork_source else None,
    )
    resolver.register_session(session_id, session_dir)
    saver = RolloutCheckpointSaver(sessions_dir)
    if fork_source is not None:
        source_session_id, source_checkpoint_id = fork_source
        saver.record_fork_origin(
            target_thread_id=session_id,
            source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=None,
            fork_mode="reference",
            relationship="detached",
        )
    config = build_checkpoint_config(session_id)
    messages: list[BaseMessage] = []
    start = 1
    for end in (*compaction_points, turn_count):
        if end < start or end > turn_count:
            raise ValueError(f"fixture checkpoint 边界非法: start={start}, end={end}")
        cutoff_index = len(messages)
        for turn_index in range(start, end + 1):
            messages.extend(_turn_messages(turn_index, long_fixture=long_fixture))
        event = None
        if start > 1:
            event = {
                "event_id": f"compaction-{start:04d}",
                "strategy": "cache_preserving",
                "cutoff_index": cutoff_index,
                "cache_prefix_messages": [],
                "summary_message": HumanMessage(
                    id=f"summary-{start:04d}",
                    content=(
                        f"第 {start} 轮前完成上下文压缩，保留主题和工具结论。"
                    ),
                    additional_kwargs={"lc_source": "summarization"},
                ),
                "file_path": f".boxteam/context-history/compaction-{start:04d}.json",
            }
        config = saver.put(
            config,
            _checkpoint(
                f"checkpoint-{end:04d}",
                messages,
                channel_version=end,
                event=event,
            ),
            {
                "source": "custom-tool-rollout-fixture",
                "checkpoint_end_turn": end,
                "semantic_boundary": "compaction" if event else "initial",
                "contains_internal_messages": any(
                    turn_index % 17 == 0 for turn_index in range(start, end + 1)
                ),
                "contains_large_tool_payload": any(
                    turn_index % 11 == 0 for turn_index in range(start, end + 1)
                ),
            },
            {"messages": str(end)},
        )
        for turn_index in range(start, end + 1):
            saver.finalize_turn(
                session_id=session_id,
                turn_id=f"job-{turn_index:04d}",
                final_message_id=f"assistant-final-{turn_index:04d}",
            )
        start = end + 1


def _write_fixture(workspace_root: Path, *, clean: bool) -> None:
    if clean:
        for relative_path in (
            ".boxteam/checkpoints",
            ".boxteam/logs",
            ".boxteam/migrations",
            ".boxteam/state",
            ".boxteam/terminal-manager",
        ):
            target = workspace_root / relative_path
            if target.exists():
                shutil.rmtree(target)
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir = workspace_root / "fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    for turn_index in range(1, 129):
        (fixture_dir / f"{turn_index:04d}.json").write_text(
            json.dumps(
                {
                    "fixture_turn": turn_index,
                    "topic": TOPICS[(turn_index - 1) % len(TOPICS)],
                    "purpose": "供历史工具调用回放读取的确定性工作区样本",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    resolver = get_session_path_resolver(sessions_dir)
    resolver.initialize()
    if clean:
        # 只删除本生成器拥有的确定性 mock；真实模型快照必须作为长期测试资源保留。
        for session_id in MOCK_SESSION_IDS:
            if any(node.node_id == session_id for node in resolver.list_nodes()):
                resolver.delete_session_subtree(session_id)
        resolver.initialize()
    _create_session(
        sessions_dir=sessions_dir,
        session_id=STATIC_LONG_SESSION_ID,
        title="自定义工具工作区：128 Turn 历史压测",
        turn_count=128,
        long_fixture=True,
        compaction_points=(32, 64, 96),
    )
    _create_session(
        sessions_dir=sessions_dir,
        session_id=LARGE_LONG_SESSION_ID,
        title="大型工具调用：128 Turn 历史投影压测",
        turn_count=128,
        long_fixture=True,
        compaction_points=(32, 64, 96),
    )
    _create_session(
        sessions_dir=sessions_dir,
        session_id=COMPACTION_SESSION_ID,
        title="上下文压缩与摘要示例",
        turn_count=24,
        compaction_points=(8, 16),
    )
    _create_session(
        sessions_dir=sessions_dir,
        session_id=TOOL_SESSION_ID,
        title="多工具调用和大输出示例",
        turn_count=12,
        compaction_points=(6,),
    )
    _create_session(
        sessions_dir=sessions_dir,
        session_id=FORK_SESSION_ID,
        title="独立历史分支示例",
        turn_count=7,
        fork_source=(STATIC_LONG_SESSION_ID, "checkpoint-0064"),
    )
    migrations_dir = workspace_root / ".boxteam" / "migrations"
    if migrations_dir.exists():
        shutil.rmtree(migrations_dir)
    fixture_sessions: list[dict[str, object]] = [
        {
            "session_id": STATIC_LONG_SESSION_ID,
            "turn_count": 128,
            "title": "自定义工具工作区：128 Turn 历史压测（mock）",
            "kind": "deterministic_mock",
        },
        {
            "session_id": LARGE_LONG_SESSION_ID,
            "turn_count": 128,
            "title": "大型工具调用：128 Turn 历史投影压测",
            "kind": "deterministic_mock",
        },
        {
            "session_id": COMPACTION_SESSION_ID,
            "turn_count": 24,
            "title": "上下文压缩与摘要示例",
            "kind": "deterministic_mock",
        },
        {
            "session_id": TOOL_SESSION_ID,
            "turn_count": 12,
            "title": "多工具调用和大输出示例",
            "kind": "deterministic_mock",
        },
        {
            "session_id": FORK_SESSION_ID,
            "turn_count": 7,
            "title": "独立历史分支示例",
            "kind": "deterministic_mock",
        },
    ]
    if (sessions_dir / REAL_MODEL_SESSION_ID).is_dir():
        fixture_sessions.insert(
            1,
            {
                "session_id": REAL_MODEL_SESSION_ID,
                "turn_count": 128,
                "title": "真实模型 128 Turn checkpoint 压测",
                "kind": "real_model_snapshot",
            },
        )
    (workspace_root / "rollout-fixture.json").write_text(
        json.dumps(
            {
                "fixture_version": FIXTURE_VERSION,
                "description": "custom_tool_test_workspace 的静态 rollout 历史 fixture",
                "history_semantics": [
                    "user_message",
                    "internal_message",
                    "reasoning",
                    "summary",
                    "encrypted_content",
                    "tool_call",
                    "tool_result",
                    "final_response",
                    "compaction_boundary",
                    "fork_origin",
                ],
                "large_tool_payload": {
                    "tool_name": "large_test_output",
                    "argument_bytes": LARGE_TOOL_PAYLOAD_BYTES,
                    "result_bytes": LARGE_TOOL_PAYLOAD_BYTES,
                    "stored_as": "inline JSONL record",
                    "detail_behavior": "bounded detail with detail_truncated",
                },
                "sessions": fixture_sessions,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_static_long_session(workspace_root: Path) -> None:
    """只重建确定性 mock 长会话，不触碰真实模型会话。"""
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_dir)
    resolver.initialize()
    if any(node.node_id == STATIC_LONG_SESSION_ID for node in resolver.list_nodes()):
        resolver.delete_session_subtree(STATIC_LONG_SESSION_ID)
    _create_session(
        sessions_dir=sessions_dir,
        session_id=STATIC_LONG_SESSION_ID,
        title="自定义工具工作区：128 Turn 历史压测（mock）",
        turn_count=128,
        long_fixture=True,
        compaction_points=(32, 64, 96),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd() / "asset" / "custom_tool_test_workspace",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="删除该资产目录中明确列出的旧运行时数据后重新生成 fixture",
    )
    parser.add_argument(
        "--only-large-session",
        action="store_true",
        help="只补生成独立的大型工具 128 Turn 会话，不覆盖现有资产",
    )
    parser.add_argument(
        "--only-static-long-session",
        action="store_true",
        help="只重建确定性 mock 128 Turn 会话，不触碰真实模型会话",
    )
    args = parser.parse_args()
    workspace_root = args.workspace.resolve()
    if not workspace_root.is_dir():
        raise FileNotFoundError(f"fixture 工作区不存在: {workspace_root}")
    if args.only_static_long_session:
        _write_static_long_session(workspace_root)
    elif args.only_large_session:
        sessions_dir = workspace_root / ".boxteam" / "sessions"
        resolver = get_session_path_resolver(sessions_dir)
        resolver.initialize()
        if any(node.node_id == LARGE_LONG_SESSION_ID for node in resolver.list_nodes()):
            resolver.delete_session_subtree(LARGE_LONG_SESSION_ID)
        _create_session(
            sessions_dir=sessions_dir,
            session_id=LARGE_LONG_SESSION_ID,
            title="大型工具调用：128 Turn 历史投影压测",
            turn_count=128,
            long_fixture=True,
            compaction_points=(32, 64, 96),
        )
        manifest_path = workspace_root / "rollout-fixture.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sessions = manifest.get("sessions")
        if isinstance(sessions, list) and not any(
            isinstance(item, dict) and item.get("session_id") == LARGE_LONG_SESSION_ID
            for item in sessions
        ):
            sessions.append(
                {
                    "session_id": LARGE_LONG_SESSION_ID,
                    "turn_count": 128,
                    "title": "大型工具调用：128 Turn 历史投影压测",
                }
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    else:
        _write_fixture(workspace_root, clean=args.clean)


if __name__ == "__main__":
    main()
