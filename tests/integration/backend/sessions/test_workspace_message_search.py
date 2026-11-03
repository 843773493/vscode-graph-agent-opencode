"""工作区消息查询经真实消息入口建立历史，核未打开 child、共享预算及失效。"""

import asyncio
import base64
import json
import time

import httpx
import pytest

from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.services.business.session_turn_history import workspace_search
from app.services.infrastructure.rollout_history import message_search
from tests.integration.backend.sessions.test_history_message_search import (
    history_client as history_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_history_message_search import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_history_message_search import (
    runtime_case as runtime_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_history_message_search import (
    search_history as search_history,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    COMPACTION_GATEWAY_ID,
    RuntimeCase,
    _child,
    _session,
)

ENDPOINT = "/api/v1/sessions/history/search"


@pytest.fixture
def workspace_query(history_client: httpx.AsyncClient):
    """仅查询请求绑定已认证 Gateway；生成历史不依赖开发服务的 Skill 控制面。"""
    async def query(*, endpoint=ENDPOINT, **kwargs):
        token = set_current_gateway_id(COMPACTION_GATEWAY_ID)
        try:
            return await history_client.post(endpoint, **kwargs)
        finally:
            reset_current_gateway_id(token)
    return query


async def test_unified_provider_preserves_literal_body_cursor_and_original_errors(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, search_history, workspace_query,
) -> None:
    session_id, base, main, child, cold, *_rest = search_history
    payload = {"workspace_id": runtime_case.runner.container.workspace_id, "category": "message",
               "operation": "content", "match_mode": "literal", "query": "定位词", "limit": 1}

    async def query(**changes):
        return await workspace_query(endpoint="/api/v1/search", json={**payload, **changes})

    first = await query()
    assert first.status_code == 200, first.text
    result = first.json()["data"]
    assert result["provider_id"] == "canonical-message" and result["state"] == "limited"
    assert result["catalog"] is result["threads"] is None
    page = result["messages"]
    assert page["items"][0]["thread"]["thread_id"] == child
    direct = await workspace_query(json={"query": payload["query"], "limit": 1})
    assert direct.status_code == 200 and direct.json()["data"] == page
    following = await query(cursor=page["cursor"])
    direct_following = await workspace_query(json={"query": payload["query"], "limit": 1, "cursor": page["cursor"]})
    assert following.status_code == direct_following.status_code == 200
    assert following.json()["data"]["messages"] == direct_following.json()["data"]
    assert following.json()["data"]["messages"]["items"][0]["match"]["reference"]["start_codepoint"] > page["items"][0]["match"]["reference"]["start_codepoint"]
    original = json.loads(base64.urlsafe_b64decode(page["cursor"] + "=" * (-len(page["cursor"]) % 4)))
    invalid_positions = []
    for changes, reason in (({"workspace_id": "another-workspace"}, "owner_mismatch"),
                            ({"gateway_id": "another-gateway"}, "owner_mismatch"),
                            ({"node_offset": 1000000}, "position_out_of_bounds")):
        cursor = base64.urlsafe_b64encode(json.dumps({**original, **changes}).encode()).decode().rstrip("=")
        invalid_positions.append(({"cursor": cursor}, reason))
    for change, reason in [*invalid_positions,
                           ({"query": "其它词", "cursor": page["cursor"]}, "query_mismatch"),
                           ({"category": "tool_result", "cursor": page["cursor"]}, "category_mismatch"),
                           ({"cursor": "not-base64!"}, "malformed"),
                           ({"cursor": "MTIz"}, "malformed"),
                           ({"cursor": "e30"}, "malformed")]:
        wrong = await query(**change)
        assert wrong.status_code == 400, wrong.text
        detail = wrong.json()["detail"]
        assert (detail["owner"], detail["code"], detail["reason"]) == (
            "workspace_message_search", "invalid_turn_cursor", reason)
        direct = await workspace_query(json={"query": payload["query"], "limit": 1, **change})
        assert direct.status_code == 400 and direct.json()["detail"] == detail, direct.text
    whitespace = await query(query=" ")
    assert whitespace.status_code == 200, whitespace.text
    assert whitespace.json()["data"]["messages"]["items"]
    for changes, code in (({"match_mode": "fuzzy"}, "unsupported_option"),
                          ({"query": "a" * 513}, "invalid_query"),
                          ({"limit": 101}, "invalid_query")):
        rejected = await query(**changes)
        assert rejected.status_code == 400 and rejected.json()["detail"]["code"] == code, rejected.text
    expired = await query(deadline_at=time.time() - 1)
    assert expired.status_code == 409 and expired.json()["detail"]["code"] == "deadline", expired.text
    # 真实执行推进历史版本；统一入口仍返回原 owner 的冲突代码。
    source = await query(query="真实编译图回复")
    cursor = source.json()["data"]["messages"]["cursor"]
    submitted = await history_client.post(f"{base}/{main}/messages", json={"message": {"content": "推进历史"}, "run": {}})
    assert submitted.status_code == 200, submitted.text
    job = runtime_case.runner.container.job_service._jobs[submitted.json()["data"]["job_id"]]
    await asyncio.wait_for(asyncio.shield(job.task), 20)
    stale = await query(query="真实编译图回复", cursor=cursor)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_turn_cursor", stale.text
    assert not runtime_case.runner.saver._storage.storage.root(session_id, thread_id=cold).exists()


async def test_workspace_search_discovers_exact_children_and_resumes_canonical_positions(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, search_history, workspace_query,
) -> None:
    session_id, _base, main, child, cold, _contents, accepted = search_history
    saver = runtime_case.runner.saver
    cold_root = saver._storage.storage.root(session_id, thread_id=cold)
    found, cursor = [], None
    for _ in range(8):
        response = await workspace_query(json={
            "query": "定位词", "limit": 1, **({"cursor": cursor} if cursor else {}),
        })
        assert response.status_code == 200, response.text
        assert response.json()["request_id"] == "request-cold-history"
        page = response.json()["data"]
        assert page["scanned_items"] <= message_search.MAX_SEARCH_ITEMS
        assert page["scanned_bytes"] <= message_search.MAX_SEARCH_BYTES
        found.extend(page["items"])
        cursor = page["cursor"]
        if page["complete"]:
            assert cursor is None and page["limited_reason"] is None
            break
        assert cursor and page["limited_reason"] == "result_limit"
    else:
        pytest.fail("工作区消息 cursor 未能收敛")
    assert len(found) == 3
    assert {hit["thread"]["thread_id"] for hit in found} == {child}
    assert all(hit["thread"]["session_id"] == session_id and hit["thread"]["kind"] == "child" for hit in found)
    assert all(hit["thread"]["thread_uri"].startswith(f"boxteam://{COMPACTION_GATEWAY_ID}/") for hit in found)
    assert [hit["match"]["message_id"] for hit in found] == [accepted[child, 0]["message_id"]] * 2 + [accepted[child, 1]["message_id"]]
    assert all(hit["thread"]["thread_id"] != main for hit in found)
    for hit in found:
        target, match = hit["thread"], hit["match"]
        around = await history_client.post(
            f"/api/v1/sessions/{target['session_id']}/threads/{target['thread_id']}/history",
            json={"direction": "around", "anchor_turn_id": match["turn_id"], "anchor_message": match["reference"]},
        )
        assert around.status_code == 200, around.text
        assert match["turn_id"] in [turn["turn_id"] for turn in around.json()["data"]["items"]]
    assert not cold_root.exists()
    assert len(runtime_case.model_admissions) == 3


async def test_workspace_search_shares_body_and_thread_budgets_without_skips(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, search_history, workspace_query, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, _base, _main, _child_id, cold, *_rest = search_history
    with monkeypatch.context() as patch:
        patch.setattr(workspace_search, "MAX_SCANNED_THREADS", 1)
        patch.setattr(message_search, "MAX_SEARCH_ITEMS", 1)
        cursor, examined, reasons = None, 0, set()
        for _ in range(16):
            response = await workspace_query(json={
                "query": "没有这段正文", **({"cursor": cursor} if cursor else {}),
            })
            assert response.status_code == 200, response.text
            page = response.json()["data"]
            assert page["items"] == []
            assert page["scanned_threads"] <= 1 and page["scanned_items"] <= 1
            examined += page["scanned_items"]
            cursor = page["cursor"]
            if page["complete"]:
                assert cursor is None
                break
            assert cursor and page["limited_reason"] in {"thread_limit", "scan_limit"}
            reasons.add(page["limited_reason"])
        else:
            pytest.fail("零命中的受限工作区扫描未能继续完成")
        assert examined == 6 and reasons == {"thread_limit", "scan_limit"}
    with monkeypatch.context() as patch:
        patch.setattr(message_search, "MAX_SEARCH_ITEMS", 1)
        cursor, examined, crossed_thread = None, 0, False
        for _ in range(10):
            response = await workspace_query(json={
                "query": "没有这段正文", **({"cursor": cursor} if cursor else {}),
            })
            assert response.status_code == 200, response.text
            page = response.json()["data"]
            assert page["scanned_items"] <= 1
            examined += page["scanned_items"]
            crossed_thread |= page["scanned_threads"] > 1
            cursor = page["cursor"]
            if page["complete"]:
                break
            assert cursor and page["limited_reason"] == "scan_limit"
        else:
            pytest.fail("跨 Thread 的共用正文预算未能继续完成")
        assert examined == 6 and crossed_thread
    cold_root = runtime_case.runner.saver._storage.storage.root(session_id, thread_id=cold)
    assert not cold_root.exists()
    with monkeypatch.context() as patch:
        patch.setattr(message_search, "SEARCH_SECONDS", 0)
        response = await workspace_query(json={"query": "定位词"})
        page = response.json()["data"]
        assert response.status_code == 200, response.text
        assert not page["complete"] and page["limited_reason"] == "deadline"
        assert page["scanned_items"] == page["scanned_threads"] == 0
    expired = await workspace_query(json={"query": "定位词"},
        headers={"X-BoxTeam-Content-Search-Deadline": str(time.time() - 1)})
    assert expired.status_code == 409, expired.text


async def test_workspace_search_rejects_changed_query_thread_catalog_and_history(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, search_history, workspace_query,
) -> None:
    session_id, base, main, _child_id, _cold, *_rest = search_history
    query = {"query": "真实编译图回复", "limit": 1}
    first = await workspace_query(json=query)
    assert first.status_code == 200, first.text
    page = first.json()["data"]
    assert page["items"][0]["thread"]["thread_id"] == main
    assert page["cursor"]
    wrong = await workspace_query(json={**query, "query": "其它词", "cursor": page["cursor"]})
    assert wrong.status_code == 400, wrong.text
    submitted = await history_client.post(f"{base}/{main}/messages", json={"message": {"content": "新正文"}, "run": {}})
    assert submitted.status_code == 200, submitted.text
    job = runtime_case.runner.container.job_service._jobs[submitted.json()["data"]["job_id"]]
    await asyncio.wait_for(asyncio.shield(job.task), 20)
    stale = await workspace_query(json={**query, "cursor": page["cursor"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_turn_cursor", stale.text
    fresh = await workspace_query(json=query)
    assert fresh.status_code == 200, fresh.text
    cursor = fresh.json()["data"]["cursor"]
    await _child(runtime_case, session_id, creation_key="workspace-search-new-child")
    changed = await workspace_query(json={**query, "cursor": cursor})
    assert changed.status_code == 409 and "Thread 目录已更新" in changed.text, changed.text
    assert (changed.json()["detail"]["owner"], changed.json()["detail"]["code"], changed.json()["detail"]["reason"]) == (
        "session_thread_catalog", "stale_thread_catalog", "thread_revision")
    fresh = await workspace_query(json=query)
    cursor = fresh.json()["data"]["cursor"]
    await _session(runtime_case.runner)
    changed = await workspace_query(json={**query, "cursor": cursor})
    assert changed.status_code == 409 and "目录已更新" in changed.text, changed.text
    assert (changed.json()["detail"]["owner"], changed.json()["detail"]["code"], changed.json()["detail"]["reason"]) == (
        "workspace_message_search", "stale_message_search_cursor", "catalog_revision")
