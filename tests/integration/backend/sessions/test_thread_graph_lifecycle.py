"""真实 Thread graph 卸载后的非空 HTTP 历史冷读与精确绑定再准入。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import httpx
import pytest

from app.agents.graph_binding import GraphBindingOwnerKey
from app.core.path_utils import workspace_context
from tests.integration.backend.sessions.test_runner_cancellation import _session
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _child,
    _complete_job,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414 - fixture 使用当前测试文件的独立工作区
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414 - 复用真实 Runner/compiler
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_clock as runtime_clock,  # noqa: PLC0414 - 复用单调 fake clock
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    thread_message_history_client as thread_message_history_client,  # noqa: PLC0414 - 复用真实 HTTP 入口
)


def _durable_snapshot(
    case: RuntimeCase, session_id: str, thread_ids: tuple[str, ...],
) -> dict[str, tuple[str, ...] | bytes]:
    """比较逻辑 SQLite 内容与原始 JSONL，排除 WAL 布局差异。"""
    storage = case.runner.saver._storage.storage
    session_node = case.runner.container.session_service.path_resolver.resolve_session_node(session_id)
    control_path = session_node / "session-control.sqlite"
    with closing(sqlite3.connect(f"{control_path.as_uri()}?mode=ro", uri=True)) as connection:
        facts: dict[str, tuple[str, ...] | bytes] = {
            "session-control.sqlite": tuple(connection.iterdump()),
        }
    for thread_id in thread_ids:
        with storage.read_connection(session_id, thread_id) as connection:
            facts[f"{thread_id}/index.sqlite"] = tuple(connection.iterdump())
        facts[f"{thread_id}/rollout.jsonl"] = storage.jsonl_path(
            session_id, thread_id=thread_id,
        ).read_bytes()
    return facts


@pytest.mark.asyncio
async def test_real_cold_nonempty_http_history_preserves_graph_binding_until_readmission(
    runtime_case: RuntimeCase, runtime_clock: list[float],
    thread_message_history_client: httpx.AsyncClient,
) -> None:
    """30 分钟卸载后冷读不建图、不写事实，下一真实执行只重建目标 Thread。"""
    case = runtime_case
    container = case.runner.container
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id, creation_key="cold-history-child")
    sibling = await _child(case, session_id, creation_key="cold-history-sibling")
    threads = (main, child, sibling)
    store = container.agent_execution_service._graph_binding_store
    bindings = {
        thread: store.load_graph_binding(GraphBindingOwnerKey(session_id, thread))
        for thread in threads
    }
    assert all(binding is not None for binding in bindings.values())
    assert case.compiled == []

    admissions = [await _complete_job(case, session_id, thread) for thread in threads]
    assert [pair for pair, _graph, _scope in case.compiled] == [
        (session_id, thread) for thread in threads
    ]
    assert len({id(graph) for _pair, graph, _scope in case.compiled}) == 3
    assert case.bindings == [bindings[thread] for thread in threads]
    first_compiled = tuple(case.compiled)
    generations = {thread: case.tracker.snapshot(session_id, thread).generation for thread in threads}
    requests_before = len(case.model_requests)
    resident_pages = {}
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        for thread, admission in zip(threads, admissions, strict=True):
            endpoint = f"/api/v1/sessions/{session_id}/threads/{thread}/history"
            pages = []
            for payload in ({}, {
                "turn_ids": [admission.turn_id],
                "include": ["user", "assistant_text", "final_response"],
            }):
                response = await thread_message_history_client.post(endpoint, json=payload)
                assert response.status_code == 200, response.text
                pages.append(response.json()["data"])
            resident_pages[thread] = pages

    runtime_clock[0] = 1799
    await case.tracker.sweep()
    assert all(case.tracker.snapshot(session_id, thread).residency == "resident" for thread in threads)
    assert all(scope.state == "open" for _pair, _graph, scope in first_compiled)
    runtime_clock[0] = 1800
    await case.tracker.sweep()
    assert all(case.tracker.snapshot(session_id, thread).residency == "cold" for thread in threads)
    assert all(scope.state == "closed" for _pair, _graph, scope in first_compiled)
    cold_snapshots = {thread: case.tracker.snapshot(session_id, thread) for thread in threads}
    durable_before = _durable_snapshot(case, session_id, threads)

    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        for thread, admission in zip(threads, admissions, strict=True):
            endpoint = f"/api/v1/sessions/{session_id}/threads/{thread}/history"
            summary_response = await thread_message_history_client.post(endpoint, json={})
            assert summary_response.status_code == 200, summary_response.text
            summary_page = summary_response.json()["data"]
            assert summary_page == resident_pages[thread][0]
            assert summary_page["thread_id"] == thread
            summary, = summary_page["summaries"]
            assert (summary["session_id"], summary["turn_id"], summary["job_id"]) == (
                session_id, admission.turn_id, admission.job_id,
            )
            assert summary["items_view"] == "summary"
            assert summary["status"] == "completed"
            assert summary["response_preview"] == "真实编译图回复"
            assert summary["user_messages"][0]["preview"] == "真实准入输入"

            detail_response = await thread_message_history_client.post(endpoint, json={
                "turn_ids": [admission.turn_id],
                "include": ["user", "assistant_text", "final_response"],
            })
            assert detail_response.status_code == 200, detail_response.text
            detail_page = detail_response.json()["data"]
            assert detail_page == resident_pages[thread][1]
            assert detail_page["thread_id"] == thread
            detail, = detail_page["items"]
            assert (detail["session_id"], detail["turn_id"], detail["job_id"]) == (
                session_id, admission.turn_id, admission.job_id,
            )
            assert detail["items_view"] == "full"
            assert detail["status"] == "completed"
            assert detail["final_response"] == "真实编译图回复"
            assert detail["assistant_text"]
            assert detail["user_messages"][0]["content"] == "真实准入输入"

    assert len(case.compiled) == len(case.build_scopes) == 3
    assert len(case.model_requests) == requests_before
    assert case.model_admissions == admissions
    assert all(scope.state == "closed" for _pair, _graph, scope in first_compiled)
    for thread in threads:
        assert case.tracker.snapshot(session_id, thread) == cold_snapshots[thread]
        assert store.load_graph_binding(GraphBindingOwnerKey(session_id, thread)) == bindings[thread]
    assert _durable_snapshot(case, session_id, threads) == durable_before

    resumed = await _complete_job(case, session_id, child)
    assert resumed.execution_id != admissions[1].execution_id
    assert resumed.turn_id != admissions[1].turn_id
    assert len(case.compiled) == 4
    assert case.compiled[-1][0] == (session_id, child)
    assert case.compiled[-1][1] is not first_compiled[1][1]
    assert case.compiled[-1][2].state == "open"
    assert case.bindings[-1] == bindings[child]
    assert case.tracker.snapshot(session_id, child).generation > generations[child]
    assert all(case.tracker.snapshot(session_id, thread).residency == "cold" for thread in (main, sibling))

    generation = case.tracker.snapshot(session_id, child).generation
    subsequent = await _complete_job(case, session_id, child)
    assert subsequent.turn_id != resumed.turn_id
    assert len(case.compiled) == 4
    assert case.tracker.snapshot(session_id, child).generation == generation
    assert case.compiled[-1][2].state == "open"
