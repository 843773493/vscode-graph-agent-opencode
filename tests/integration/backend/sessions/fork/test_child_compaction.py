"""child 通过模型工具摘要及读回归档，main 存储与请求保持隔离。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from itertools import pairwise
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.schemas.internal_v2.message import MessageRunAccepted
from app.schemas.internal_v2.turn import ToolExecutionSummaryDTO, TurnHistoryPageDTO
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.attachments.test_thread_isolation import (
    history,
    state,
)
from tests.integration.backend.sessions.fork.test_thread_rewind import (
    complete,
    fork_child,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _prepare_workspace,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 - 复用真实进程与唯一模型协议装具
)
from tests.support.api_helpers import create_session, with_message_run_operation_id
from tests.support.sealed_request_audit import audit_sealed_requests


def _prepare_child_workspace(context: TestRunContext, model_port: int, gateway_port: int) -> Path:
    workspace = _prepare_workspace(context, model_port, gateway_port)
    path = workspace / ".boxteam/workspace.jsonc"
    config = json.loads(path.read_text(encoding="utf-8"))
    settings = config["settings"]["workspace"]
    settings.setdefault("ui", {})["auto_summarize"] = False
    profile = config["profiles"]["provider.primary"]
    profile["settings"]["model_options"]["api_mode"]["model_info"]["max_input_tokens"] = 1_000_000
    settings["llm"]["providers"]["backup_1"]["config_ref"] = "#/profiles/provider.primary"
    settings["llm"]["providers"]["backup_1"]["models"]["default"]["model"] = "e2e-stub-model"
    settings["agents"]["default"]["model"]["fallbacks"] = [{"provider_id": "backup_1", "model_id": "default"}]
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return workspace


def _history_ranges(root: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect((root / "index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT checkpoint_ns,item_id,source_revision,ref_json,render_version,line_start,line_count,adopted_control_sequence "
            "FROM compaction_history_ranges ORDER BY line_start",
        ).fetchall()


def _archive_messages(content: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for index, line in enumerate(content.splitlines(), 1):
        number, field = line.split(": ", 1)
        assert number == str(index)
        if field.startswith("["):
            messages.append({})
            continue
        path, encoded = field.split(": ", 1)
        value = json.loads(encoded)
        if isinstance(value, str):
            # 长字符串按行分片；只拼回同一 Item 的同一路径，保留完整原文与顺序。
            messages[-1][path] = messages[-1].get(path, "") + value
    return messages


@pytest.mark.parametrize("communication_case", [{"resources": True, "prepare_workspace": _prepare_child_workspace}], indirect=True)
async def test_child_compaction_tool_preserves_main_and_restores_own_archive(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    # 通过正常 catalog/selection 启用工具，不替测试预置 graph、view 或摘要。
    catalog = await case.client.get("/api/v1/tools", params={"agent_id": "default"})
    assert catalog.status_code == 200, catalog.text
    tools = {tool["name"]: tool for tool in catalog.json()["data"]}
    selected = await case.client.patch("/api/v1/tools/selection", json={
        "agent_id": "default", "changes": [
            {"tool_id": tools[name]["tool_id"], "execution_enabled": True, "model_visible": True}
            for name in ("compact_conversation", "read_compaction_history")
        ],
    })
    assert selected.status_code == 200, selected.text
    session = await create_session(case.client, "child 摘要工具与 main 隔离")
    selected_model = {"provider_id": "backup_1", "model_id": "default"}
    selected = await case.client.patch(f"/api/v1/sessions/{session}", json={"model": selected_model})
    assert selected.status_code == 200, selected.text
    assert selected.json()["data"]["current_model"] == selected_model
    main_response = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main_response.status_code == 200, main_response.text
    main = main_response.json()["data"]["thread_id"]
    shared = [(f"child-compact-shared-question-{index}", f"child-compact-shared-answer-{index}:" + "已完成的详细记录。" * 120)
              for index in range(6)]
    initial = [await complete(case, session, main, question, answer) for question, answer in shared]
    child = (await fork_child(case, session, "child-compact-isolation")).thread_id
    assert child != main
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    roots = {thread: resolver.resolve_thread_node(session, thread) / "rollout" for thread in (main, child)}
    assert all(not _history_ranges(root) for root in roots.values())
    child_before = state(roots[child])
    main_private = await complete(case, session, main, "main-private-question", "main-private-answer")
    assert state(roots[child]) == child_before
    main_before = state(roots[main])
    main_history = await history(case, session, main)

    summary = "child-only-summary: 已完成共享历史的中段，继续保留近期记录。"
    compact_call, summary_call = await case.plan(session, [
        _tool("compact_conversation", {}, "child-compact"),
        {"content": summary},
    ], thread_id=child)
    compact_tool = compact_call + ":tool-call:child-compact"
    control = case.replay.control
    phase = "test.tool_result.committed"
    control.arm(compact_tool, phase)
    sent = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/messages", json=with_message_run_operation_id({
        "message": {"content": "child-only-compaction-question"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    }))
    assert sent.status_code == 200, sent.text
    compacted = MessageRunAccepted.model_validate(sent.json()["data"])
    assert compacted.thread_id == compacted.dispatch.thread_id == child
    try:
        await asyncio.to_thread(control.acknowledged, compact_tool, phase, 30)
        compact_result = case.tool_result(session, compact_tool)
        assert "历史摘要已安排，task_id=" in compact_result["content"]
        assert "尚未切换当前上下文" in compact_result["content"]
        # 暂停前台下一次准备，等待原后台 owner 真正持久化 ready；不手写候选或 view。
        async with asyncio.timeout(30):
            while True:
                with sqlite3.connect((roots[child] / "index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
                    candidate_row, = connection.execute(
                        "SELECT candidate_id,task_id,result_detail_ref,applied_summary_item_id,discarded_reason "
                        "FROM context_compaction_candidates",
                    ).fetchall()
                assert candidate_row[4] is None, candidate_row
                if candidate_row[2] is not None:
                    break
                await asyncio.sleep(0.05)
        candidate_id, task_id = candidate_row[:2]
        assert candidate_row[3] is None and task_id in compact_result["content"]
        assert not _history_ranges(roots[child])
        with sqlite3.connect((roots[child] / "index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            input_key, = connection.execute(
                "SELECT detail_ref FROM context_plan_details WHERE assembly_id=? AND detail_kind='compaction_input'",
                (f"compaction-{candidate_id}",),
            ).fetchone()
        with RolloutCheckpointSaver(case.workspace / ".boxteam/sessions") as saver:
            frozen_input = saver._storage.compactions.read_task_input(session, child, detail_ref_from_key(input_key))
            assert frozen_input.frozen == saver._storage.compactions.read(session, child, candidate_id)
            recipe = frozen_input.model_recipe
            assert (recipe["provider_id"], recipe["model_id"], recipe["model"]) == ("backup_1", "default", "e2e-stub-model")
            assert frozen_input.frozen.model_config_hash == sha256_jcs(recipe)
            ready_result = saver._storage.compactions.read_result(session, child, candidate_id)
            assert ready_result is not None and ready_result.text == summary
            assert ready_result.coverage == frozen_input.frozen.refs
            assert ready_result.dispatch_ref.callback_model_call_id == summary_call
            assert (ready_result.dispatch_ref.session_id, ready_result.dispatch_ref.thread_id) == (session, child)
            assert case.request(summary_call)["model"] == recipe["model"]
            assert saver._storage.compactions.read_history(session, main) == ""
        archive_call, final_call = await case.plan(session, [
            _tool("read_compaction_history", {"line_offset": 1, "limit": 2000}, "child-read-archive"),
            {"content": "child-only-compacted-answer"},
        ], thread_id=child)
    finally:
        control.release(compact_tool, phase)
    await _completed(case.client, compacted.job_id)
    with sqlite3.connect((roots[child] / "index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        summaries = connection.execute(
            "SELECT item_id FROM item_catalog WHERE semantic_kind='compaction_summary'",
        ).fetchall()
    assert len(summaries) == 1
    before = json.dumps(case.request(compact_call)["messages"], ensure_ascii=False)
    candidate = json.dumps(case.request(summary_call)["messages"], ensure_ascii=False)
    effective = json.dumps(case.request(archive_call)["messages"], ensure_ascii=False)
    assert summary not in candidate and effective.count(summary) == 1
    assert "child-only-compaction-question" in effective
    assert all(question in before and answer in before for question, answer in shared)
    removed = [(question, answer) for question, answer in shared if answer in candidate and answer not in effective]
    assert removed, "工具必须实际移出中段原文，不能只返回调度成功"
    archive_payload = case.tool_result(session, archive_call + ":tool-call:child-read-archive")
    assert archive_payload["tool_outcome"] == "success"
    archive_result = json.dumps(archive_payload, ensure_ascii=False)
    archive_messages = _archive_messages(archive_payload["content"])
    assert [item["$[0].content"] for item in archive_messages if "$[0].content" in item] == [question for question, _ in removed]
    assert [item["$[0].content[0].text"] for item in archive_messages if "$[0].content[0].text" in item] == [answer for _, answer in removed]
    assert "read_compaction_history(line_offset=1, limit=200)" in effective
    assert all(text not in before + candidate + effective + archive_result for text in (
        "main-private-question", "main-private-answer",
    ))
    wire_tools = case.request(compact_call)["tools"]
    assert isinstance(wire_tools, list)
    assert all(tool in {entry["function"]["name"] for entry in wire_tools}
               for tool in ("compact_conversation", "read_compaction_history"))
    archive_message, = [message for message in case.request(final_call)["messages"]
                       if message.get("role") == "tool" and message.get("tool_call_id") == "child-read-archive"]
    assert archive_message["content"] == archive_payload["content"]
    assert state(roots[main]) == main_before
    assert await history(case, session, main) == main_history
    assert not _history_ranges(roots[main])
    ranges = _history_ranges(roots[child])
    assert ranges and [row[1] for row in ranges] == [ref.ref_id for ref in frozen_input.frozen.refs]
    assert [json.loads(row[3]) for row in ranges] == [ref.to_dict() for ref in frozen_input.frozen.refs]
    assert ranges[0][5] == 1
    assert all(right[5] == left[5] + left[6] for left, right in pairwise(ranges))
    total_lines = ranges[-1][5] + ranges[-1][6] - 1
    assert len(archive_payload["content"].splitlines()) == total_lines
    with RolloutCheckpointSaver(case.workspace / ".boxteam/sessions") as saver:
        assert saver._storage.compactions.read_history(session, child, limit=2000) == archive_payload["content"]
    assert (roots[child] / "rollout.jsonl").read_bytes().startswith(child_before.canonical)
    child_history = await history(case, session, child)
    assert [turn.turn_id for turn in child_history.items] == [accepted.turn_id for accepted in (*initial, compacted)]
    # 默认 history 不包含活动正文；显式请求正式详情，验证唯一摘要及工具配对。
    detail_request = {"direction": "head", "turns": 10, "include": [
        "user", "text", "reasoning_detail", "tool_call", "tool_result", "final_response", "internal",
    ]}
    details = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/history", json=detail_request)
    assert details.status_code == 200, details.text
    full_history = TurnHistoryPageDTO.model_validate(details.json()["data"])
    compact_turn = full_history.items[-1]
    summary_part, = [part for part in compact_turn.response_items if part.kind == "compaction_summary"]
    assert summary_part.source.item_id == summaries[0][0]
    tool_calls = [part for part in compact_turn.response_items if part.kind == "tool_call"]
    tool_results = [part for part in compact_turn.response_items if part.kind == "tool_result"]
    assert [part.tool_name for part in tool_calls] == ["compact_conversation", "read_compaction_history"]
    assert [part.source.model_call_id for part in tool_results] == [part.source.model_call_id for part in tool_calls]
    assert len({part.source.model_call_id for part in tool_calls}) == 2
    with RolloutCheckpointSaver(case.workspace / ".boxteam/sessions") as saver:
        result_items = {item.item_id: item for item in saver._storage.storage.read_items(
            session, child, item_ids=tuple(part.source.item_id for part in tool_results),
        )}
    # DTO 保留 canonical raw/scoped 表示；配对必须同时核 emitting ModelCall，不能只比裸 ID。
    for call, result, expected_id in zip(tool_calls, tool_results, ("child-compact", "child-read-archive"), strict=True):
        assert call.source.model_call_id is not None
        identity = ItemProjectionIdentity(model_call_id=call.source.model_call_id)
        assert call.tool_call_id is not None and result.tool_call_id is not None
        assert provider_tool_call_id(identity, call.tool_call_id) == expected_id
        assert provider_tool_call_id(identity, result.tool_call_id) == expected_id
        assert result.tool_name == call.tool_name
        # 调用汇总可合并同调用的安全证据；结果保留自身载体，没有的时序不得补造。
        evidence = result_items[result.source.item_id].tool_execution
        assert result.tool_execution == (None if evidence is None else ToolExecutionSummaryDTO.model_validate(evidence.to_dict()))
        assert call.tool_execution is not None
        if result.tool_execution is not None:
            assert result.tool_execution == call.tool_execution
        for execution in (call.tool_execution, result.tool_execution):
            if execution is not None:
                assert (execution.session_id, execution.thread_id) == (session, child)
                assert execution.status == "completed" and execution.outcome == "success"
    assert compact_turn.final_response == "child-only-compacted-answer"

    finished = {thread: state(roots[thread]) for thread in (main, child)}
    await case.hard_restart()
    for thread, page in ((main, main_history), (child, child_history)):
        assert state(roots[thread]) == finished[thread]
        assert await history(case, session, thread) == page
    cold_details = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/history", json=detail_request)
    assert cold_details.status_code == 200, cold_details.text
    assert TurnHistoryPageDTO.model_validate(cold_details.json()["data"]) == full_history
    assert _history_ranges(roots[child]) == ranges and not _history_ranges(roots[main])

    reread_call, successor_call = await case.plan(session, [
        _tool("read_compaction_history", {"line_offset": 1, "limit": 2000}, "child-reread-archive"),
        {"content": "child-cold-successor-answer"},
    ], thread_id=child)
    response = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/messages", json=with_message_run_operation_id({
        "message": {"content": "child-cold-successor-question"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    }))
    assert response.status_code == 200, response.text
    successor = MessageRunAccepted.model_validate(response.json()["data"])
    await _completed(case.client, successor.job_id)
    restored_archive = case.tool_result(session, reread_call + ":tool-call:child-reread-archive")
    assert restored_archive["content"] == archive_payload["content"]
    assert _history_ranges(roots[child]) == ranges and not _history_ranges(roots[main])
    assert state(roots[main]) == finished[main]
    assert await history(case, session, main) == main_history
    child_messages = case.request(reread_call)["messages"]
    assert isinstance(child_messages, list)
    child_wire = json.dumps(child_messages, ensure_ascii=False)
    assert all(text in child_wire for text in (summary, "child-only-compaction-question", "child-only-compacted-answer"))
    assert all(text not in child_wire for text in ("main-private-question", "main-private-answer"))
    # 模型主动读回的 tool 正文可以含归档原文；被移出的旧对话消息不能复活。
    direct_context = json.dumps([message for message in child_messages if message["role"] != "tool"], ensure_ascii=False)
    assert all(question not in direct_context and answer not in direct_context for question, answer in removed)
    reread_message, = [message for message in case.request(successor_call)["messages"]
                      if message.get("role") == "tool" and message.get("tool_call_id") == "child-reread-archive"]
    assert reread_message["content"] == archive_payload["content"]
    child_finished = state(roots[child])
    child_page = await history(case, session, child)
    planned_before = set(case.planned_calls)
    main_successor = await complete(case, session, main, "main-cold-successor-question", "main-cold-successor-answer")
    main_call, = case.planned_calls.keys() - planned_before
    assert state(roots[child]) == child_finished
    assert await history(case, session, child) == child_page
    main_wire = json.dumps(case.request(main_call)["messages"], ensure_ascii=False)
    assert all(question in main_wire and answer in main_wire for question, answer in shared)
    assert "main-private-question" in main_wire and "main-private-answer" in main_wire
    assert all(text not in main_wire for text in (
        summary, "child-only-compaction-question", "child-only-compacted-answer",
        "child-cold-successor-question", "child-cold-successor-answer",
    ))
    captures = {call: json.loads((case.replay.artifacts / (call + ".json")).read_text()) for call in case.planned_calls}
    logs = list((resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"))
    proofs = {thread: audit_sealed_requests(roots[thread] / "index.sqlite", logs, captures) for thread in (main, child)}
    assert len(case.planned_calls) == 14
    case.record("child-compaction-isolation", {
        "session_id": session, "main_thread_id": main, "child_thread_id": child,
        "compacted": compacted.model_dump(mode="json"), "summary_item_ids": summaries,
        "main_private": main_private.model_dump(mode="json"), "child_successor": successor.model_dump(mode="json"),
        "main_successor": main_successor.model_dump(mode="json"), "removed_questions": [question for question, _ in removed],
        "archive_tool_result": archive_payload, "request_audit": proofs,
        "frozen_model": {"provider_id": recipe["provider_id"], "model_id": recipe["model_id"],
                         "model": recipe["model"], "model_config_hash": frozen_input.frozen.model_config_hash},
        "compaction_history_ranges": ranges,
        "scope": "真实 Gateway/Backend、消息/fork/child 摘要工具/归档读取、完整 SQLite/JSONL、硬重启及双方续聊；不代替 Web、Windows 或全部 8.3",
    })
