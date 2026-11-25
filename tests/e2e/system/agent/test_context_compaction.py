"""会话上下文压缩端到端测试。"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from tests.support.api_helpers import (
    read_llm_request_logs,
    with_message_run_operation_id,
)
from tests.support.api_waiters import wait_for_job_done


async def _send_message(
    client: httpx.AsyncClient,
    session_id: str,
    content: str,
) -> str:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json=with_message_run_operation_id({
            "message": {"content": content},
            "run": {"mode": "single_agent", "agent_id": "default"},
        }),
    )
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    job = await wait_for_job_done(client, job_id, max_attempts=90)
    assert job["status"] in {"completed", "succeeded"}
    return job_id


async def _job_request_log(
    client: httpx.AsyncClient,
    session_id: str,
    job_id: str,
) -> dict:
    logs = await read_llm_request_logs(client, session_id)
    matching_logs = [
        log for log in logs if log.get("job_id") == job_id
    ]
    assert matching_logs
    return matching_logs[-1]["request"]


async def _job_llm_log(
    client: httpx.AsyncClient,
    session_id: str,
    job_id: str,
) -> dict:
    logs = await read_llm_request_logs(client, session_id)
    matching_logs = [
        log for log in logs if log.get("job_id") == job_id
    ]
    assert matching_logs
    return matching_logs[-1]


async def _job_llm_logs(
    client: httpx.AsyncClient,
    session_id: str,
    job_id: str,
) -> list[dict]:
    logs = await read_llm_request_logs(client, session_id)
    matching_logs = [
        log for log in logs if log.get("job_id") == job_id
    ]
    assert matching_logs
    return matching_logs


def _cache_read_tokens(log: dict) -> int:
    results = log.get("response", {}).get("result", [])
    assert results, "LLM 日志缺少响应消息"
    usage = results[-1].get("usage_metadata")
    assert isinstance(usage, dict), "LLM 响应缺少 usage_metadata"
    details = usage.get("input_token_details") or {}
    assert isinstance(details, dict), "input_token_details 类型无效"
    value = details.get("cache_read", 0)
    assert isinstance(value, int) and value >= 0
    return value


def _message_content_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TypeError(f"模型日志消息 content 类型无效: {type(content).__name__}")
    texts: list[str] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n".join(texts)


@pytest.mark.asyncio
async def test_workspace_agents_change_flows_through_context_source_items(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    # D3-B 合同：AGENTS 不再进入 system prompt；首帧与 delta 均为 CSM 上下文 item。
    workspace_root = Path(e2e_workspace_root_path)
    agents_path = workspace_root / "AGENTS.md"
    initial_content = "# E2E AGENTS\n\n始终遵循 agents-cache-version-one。\n"
    changed_content = "# E2E AGENTS\n\n始终遵循 agents-cache-version-two。\n"
    agents_path.write_text(initial_content, encoding="utf-8")

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "AGENTS Context Source E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    first_job_id = await _send_message(
        client,
        session_id,
        "请只回复 FIRST_OK，不要调用工具。",
    )
    first_request = await _job_request_log(client, session_id, first_job_id)
    first_system = first_request["system_message"]
    assert first_system is not None
    assert "agents-cache-version-one" not in _message_content_text(first_system)
    first_messages_text = "\n".join(
        _message_content_text(message) for message in first_request["messages"]
    )
    assert "agents-cache-version-one" in first_messages_text

    agents_path.write_text(changed_content, encoding="utf-8")
    second_job_id = await _send_message(
        client,
        session_id,
        "请只回复 SECOND_OK，不要调用工具。",
    )
    second_request = await _job_request_log(client, session_id, second_job_id)
    second_messages_text = "\n".join(
        _message_content_text(message) for message in second_request["messages"]
    )
    assert "工作区 AGENTS.md 指令来源已按 delta 注入" in second_messages_text
    assert "+始终遵循 agents-cache-version-two。" in second_messages_text


@pytest.mark.skipif(
    os.environ.get("OPENCODE_ZEN_API_KEY") is None,
    reason="需要 OPENCODE_ZEN_API_KEY 才能验证真实 Prompt Cache",
)
@pytest.mark.asyncio
async def test_cache_preserving_compaction_keeps_and_grows_upstream_cache_hit(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Cache Preserving Compact E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    stable_material = "\n".join(
        f"缓存稳定资料-{index:04d}：该行必须保持原样。" for index in range(260)
    )
    first_job_id = await _send_message(
        client,
        session_id,
        f"记住以下稳定资料，只回复 CACHE-FIRST，不要调用工具。\n{stable_material}",
    )
    first_log = await _job_llm_log(client, session_id, first_job_id)

    second_material = "，".join(f"第二轮-{index:04d}" for index in range(180))
    second_job_id = await _send_message(
        client,
        session_id,
        f"继续记住这些内容，只回复 CACHE-SECOND，不要调用工具：{second_material}",
    )
    second_log = await _job_llm_log(client, session_id, second_job_id)
    second_cached_tokens = _cache_read_tokens(second_log)
    second_upstream_request = second_log["upstream"]["attempts"][-1]["request"]

    for index in range(8):
        await _send_message(
            client,
            session_id,
            f"保留第 {index} 份维护约束，后续仍要检查它。"
            + f"压缩历史资料-{index:02d}。" * 90,
        )
    compact_response = await client.post(
        f"/api/v1/sessions/{session_id}/compact",
        timeout=120,
    )
    assert compact_response.status_code == 200
    compact_result = compact_response.json()["data"]
    assert compact_result["status"] == "scheduled"
    assert compact_result["strategy"] is None
    assert compact_result["effective_message_count_after"] == (
        compact_result["effective_message_count_before"]
    )
    assert compact_result["summarized_message_count"] == 0

    third_job_id = await _send_message(
        client,
        session_id,
        "压缩后继续对话，只回复 CACHE-THIRD，不要调用工具。",
    )
    third_logs = await _job_llm_logs(client, session_id, third_job_id)
    assert len(third_logs) == 2
    summary_log, third_log = third_logs
    summary_cached_tokens = _cache_read_tokens(summary_log)
    third_cached_tokens = _cache_read_tokens(third_log)
    third_upstream_request = third_log["upstream"]["attempts"][-1]["request"]

    fourth_job_id = await _send_message(
        client,
        session_id,
        "继续压缩后的对话，只回复 CACHE-FOURTH，不要调用工具。",
    )
    fourth_log = await _job_llm_log(client, session_id, fourth_job_id)
    fourth_cached_tokens = _cache_read_tokens(fourth_log)

    first_request_messages = first_log["request"]["messages"]
    second_request_messages = second_log["request"]["messages"]
    third_request_messages = third_log["request"]["messages"]
    assert third_request_messages[: len(second_request_messages)] == (
        second_request_messages
    )
    assert first_request_messages[0] == second_request_messages[0]
    upstream_prefix_size = len(second_upstream_request["messages"])
    assert third_upstream_request["messages"][:upstream_prefix_size] == (
        second_upstream_request["messages"]
    )
    third_extra_body = third_upstream_request.get("extra_body") or {}
    assert "prompt_cache_key" not in third_extra_body
    assert second_cached_tokens > 0
    assert summary_cached_tokens >= second_cached_tokens, {
        "second_cached_tokens": second_cached_tokens,
        "summary_cached_tokens": summary_cached_tokens,
    }
    assert third_cached_tokens >= second_cached_tokens, {
        "second_cached_tokens": second_cached_tokens,
        "first_post_compaction_cached_tokens": third_cached_tokens,
    }
    assert fourth_cached_tokens > second_cached_tokens, {
        "second_cached_tokens": second_cached_tokens,
        "first_post_compaction_cached_tokens": third_cached_tokens,
        "next_post_compaction_cached_tokens": fourth_cached_tokens,
    }
