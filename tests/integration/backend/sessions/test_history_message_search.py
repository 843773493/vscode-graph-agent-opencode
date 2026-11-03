"""从真实消息入口生成 main/child 历史，验证 canonical 正文查询和原 around 读取。"""

import asyncio

import httpx
import pytest

from app.core.path_utils import workspace_context
from app.services.infrastructure.rollout_history import message_search
from tests.integration.backend.sessions.test_cold_rollout_history import (
    history_client as history_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _child,
    _session,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


@pytest.fixture
async def search_history(runtime_case: RuntimeCase, history_client: httpx.AsyncClient):
    case = runtime_case
    container = case.runner.container
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id)
    cold = await _child(case, session_id, creation_key="search-cold-child")
    base = f"/api/v1/sessions/{session_id}/threads"
    contents = {main: ["只属于 main 的正文"], child: ["前缀😀\n" + "长" * 66000 + "定位词\n😀定位词", "第二轮😀定位词"]}
    accepted = {}
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        for thread_id, values in contents.items():
            for index, content in enumerate(values):
                response = await history_client.post(f"{base}/{thread_id}/messages", json={
                    "message": {"content": content}, "run": {},
                })
                assert response.status_code == 200, response.text
                result = response.json()["data"]
                job = container.job_service._jobs[result["job_id"]]
                await asyncio.wait_for(asyncio.shield(job.task), 20)
                assert job.status.value == "completed", job.error_message
                accepted[thread_id, index] = result
        yield session_id, base, main, child, cold, contents, accepted


async def test_message_search_uses_exact_canonical_owner_and_resumes_positions(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, search_history,
) -> None:
    session_id, base, main, child, cold, contents, accepted = search_history
    saver = runtime_case.runner.saver
    cold_root = saver._storage.storage.root(session_id, thread_id=cold)
    assert not cold_root.exists()
    cold_response = await history_client.post(f"{base}/{cold}/history/search", json={"query": "定位词"})
    assert cold_response.status_code == 200, cold_response.text
    assert cold_response.json()["data"] == {
        "session_id": session_id, "thread_id": cold, "items": [],
        "cursor": None, "complete": True, "limited_reason": None, "scanned_items": 0, "scanned_bytes": 0,
    }
    assert not cold_root.exists()
    main_response = await history_client.post(f"{base}/{main}/history/search", json={"query": "定位词"})
    assert main_response.status_code == 200, main_response.text
    assert main_response.json()["data"]["items"] == []
    assert main_response.json()["data"]["complete"]

    all_hits, cursor = [], None
    for _ in range(4):
        response = await history_client.post(f"{base}/{child}/history/search", json={
            "query": "定位词", "limit": 1, **({"cursor": cursor} if cursor else {}),
        })
        assert response.status_code == 200, response.text
        envelope = response.json()
        assert envelope["request_id"] == "request-cold-history"
        page = envelope["data"]
        assert page["thread_id"] == child
        assert page["scanned_bytes"] <= message_search.MAX_SEARCH_BYTES
        all_hits.extend(page["items"])
        cursor = page["cursor"]
        if page["complete"]:
            assert cursor is None and page["limited_reason"] is None
            break
        assert cursor and page["limited_reason"] == "result_limit"
    else:
        pytest.fail("三处命中的独立正文游标未能收敛")
    assert len(all_hits) == 3
    assert [hit["message_id"] for hit in all_hits] == [accepted[child, 0]["message_id"]] * 2 + [accepted[child, 1]["message_id"]]
    for index, hit in enumerate(all_hits):
        source = contents[child][0 if index < 2 else 1]
        reference = hit["reference"]
        assert source[reference["start_codepoint"]:reference["end_codepoint"]] == "定位词"
        assert len(hit["preview"]) <= 240
        assert source[hit["preview_start_codepoint"]:][:len(hit["preview"])] == hit["preview"]
        response = await history_client.post(f"{base}/{child}/history", json={
            "direction": "around", "anchor_turn_id": hit["turn_id"], "anchor_message": reference,
            "before_turns": 0, "after_turns": 0,
        })
        assert response.status_code == 200, response.text
        located = response.json()["data"]
        assert located["thread_id"] == child
        assert located["located_message"] == {
            "turn_id": hit["turn_id"], "message_id": hit["message_id"], "role": "user",
            "reference": reference, "content": source,
        }
        assert [turn["turn_id"] for turn in located["items"]] == [hit["turn_id"]]
    assert all_hits[0]["reference"]["start_codepoint"] > 65536
    assistant = await history_client.post(f"{base}/{child}/history/search", json={"query": "真实编译图回复"})
    assert assistant.status_code == 200, assistant.text
    assert [hit["role"] for hit in assistant.json()["data"]["items"]] == ["assistant", "assistant"]
    assert len(runtime_case.model_admissions) == 3


async def test_message_search_limits_and_stale_references_are_explicit(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, search_history, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session_id, base, main, child, _cold, _contents, accepted = search_history
    query = {"query": "定位词", "limit": 1}
    response = await history_client.post(f"{base}/{child}/history/search", json=query)
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    cursor, hit = page["cursor"], page["items"][0]
    for thread, payload in ((main, {**query, "cursor": cursor}), (child, {**query, "query": "其它", "cursor": cursor})):
        rejected = await history_client.post(f"{base}/{thread}/history/search", json=payload)
        assert rejected.status_code == 400, rejected.text
    for thread, turn, reference in (
        (main, hit["turn_id"], hit["reference"]),
        (child, accepted[child, 1]["turn_id"], hit["reference"]),
        (child, hit["turn_id"], {**hit["reference"], "content_hash": "sha256:jcs:v1:" + "0" * 64}),
        (child, hit["turn_id"], {**hit["reference"], "end_codepoint": 999999}),
    ):
        rejected = await history_client.post(f"{base}/{thread}/history", json={
            "direction": "around", "anchor_turn_id": turn, "anchor_message": reference,
        })
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "stale_turn_reference"
    with monkeypatch.context() as patch:
        patch.setattr(message_search, "MAX_SEARCH_ITEMS", 1)
        limited = await history_client.post(f"{base}/{child}/history/search", json={"query": "不存在"})
        assert limited.status_code == 200, limited.text
        assert limited.json()["data"]["limited_reason"] == "scan_limit"
        assert limited.json()["data"]["cursor"] and not limited.json()["data"]["complete"]
    with monkeypatch.context() as patch:
        patch.setattr(message_search, "MAX_SEARCH_BYTES", 64)
        limited = await history_client.post(f"{base}/{child}/history/search", json=query)
        assert limited.status_code == 200, limited.text
        assert limited.json()["data"]["limited_reason"] == "item_size_limit"
        assert limited.json()["data"]["cursor"] is None and not limited.json()["data"]["complete"]
        assert limited.json()["data"]["scanned_bytes"] == 0
    # 新输入必须走产品入口推进历史版本，不能手改数据库来制造 stale。
    container = runtime_case.runner.container
    next_message = await history_client.post(f"{base}/{child}/messages", json={"message": {"content": "新输入"}, "run": {}})
    assert next_message.status_code == 200, next_message.text
    job = container.job_service._jobs[next_message.json()["data"]["job_id"]]
    await asyncio.wait_for(asyncio.shield(job.task), 20)
    assert job.status.value == "completed", job.error_message
    stale = await history_client.post(f"{base}/{child}/history/search", json={**query, "cursor": cursor})
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "stale_turn_cursor"
