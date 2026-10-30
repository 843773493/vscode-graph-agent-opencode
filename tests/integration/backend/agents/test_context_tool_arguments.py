"""从消息入口验证本地上下文工具发现、参数拒绝及纠正后的真实查询。"""

from __future__ import annotations

import json

from app.domain.itemized.serialization import normalize_wire_request
from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用产品装具，按本文件隔离工作区与产物。
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done


async def test_context_tools_publish_closed_schema_and_pair_argument_errors(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    marker = "context-argument-correction-target"
    target_session = await create_session(case.client, marker)
    session = await create_session(case.client, "上下文工具参数纠正")
    response = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert response.status_code == 200, response.text
    thread = response.json()["data"]["thread_id"]
    workspace_id = case.container.session_context_query_service.workspace_id
    identity = {
        "resource_identity": str(workspace_session_inventory_identity(workspace_id)),
        "uri": workspace_session_inventory_uri(workspace_id), "scope": "workspace",
    }
    valid_read = {**identity, "view": "inventory", "limit": "7"}
    valid_search = {**identity, "query": marker, "sources": ["session_catalog"], "max_results": "7"}
    unknown_key = "/private/context/argument.json"
    operations = [
        ("search_tools", {"tool_ids": ["read_context", "search_context"]}),
        ("invoke_extension_tool", {"tool_name": "read_context", "arguments": {**valid_read, unknown_key: "private-value"}}),
        ("invoke_extension_tool", {"tool_name": "read_context", "arguments": valid_read}),
        ("invoke_extension_tool", {"tool_name": "search_context", "arguments": {**valid_search, unknown_key: "private-value"}}),
        ("invoke_extension_tool", {"tool_name": "search_context", "arguments": valid_search}),
    ]
    raw_ids = [f"context-arguments-{index}" for index in range(len(operations))]
    calls = await case.plan(session, [
        *[_tool_reply(name, arguments, call_id) for (name, arguments), call_id in zip(operations, raw_ids, strict=True)],
        {"content": "已拒绝未知参数，纠正后读取和搜索成功。"},
    ])
    accepted = await _send(case, session, "发现上下文工具，拒绝未知参数后使用合法参数继续查询。")
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    await wait_for_job_done(case.client, job_id, max_attempts=120)
    job_response = await case.client.get(f"/api/v1/jobs/{job_id}")
    assert job_response.status_code == 200, job_response.text
    assert job_response.json()["data"]["status"] == "completed", job_response.text
    requests = [case.request(call) for call in calls]
    messages = requests[-1]["messages"]
    assert isinstance(messages, list)
    results = [message for message in messages if message["role"] == "tool"]
    expected_ids = [f"{call}:tool-call:{raw}" for call, raw in zip(calls[:-1], raw_ids, strict=True)]
    assert [(message["name"], message["tool_call_id"]) for message in results] == [
        (operation[0], call_id) for operation, call_id in zip(operations, raw_ids, strict=True)
    ]
    definitions = json.loads(results[0]["content"])["results"]
    assert len(definitions) == 2
    for definition in definitions:
        assert definition["schema_state"] == "inline_complete"
        assert definition["input_schema"]["additionalProperties"] is False
    for index in (1, 3):
        error = results[index]["content"]
        assert "参数校验失败" in error and "stage=extension-tool-arguments" in error
        assert json.loads(error.split("issues=", 1)[1]) == [
            {"parameter": None, "depth": 1, "rule": "extra_forbidden"},
        ]
        assert "private" not in error and "argument.json" not in error
    read_result = json.loads(results[2]["content"])
    assert read_result["uri"] == identity["uri"] and read_result["view"] == "inventory"
    assert any(item["data"]["session_id"] == target_session for item in read_result["items"])
    search_result = json.loads(results[4]["content"])
    assert search_result["uri"] == identity["uri"] and search_result["query"] == marker
    assert any(marker in match["preview"] for match in search_result["matches"])

    # 只冷读产品实际生成的 canonical 与 assembly，不替产品补建执行状态。
    with RolloutCheckpointSaver(case.workspace / ".boxteam/sessions") as cold:
        items = cold._storage.storage.read_items(session, thread)
        attempts = [item for item in items if item.semantic_kind == "tool_result"
                    and item.projection_identity is not None
                    and item.projection_identity.projection_message_id is None]
        assert len(attempts) == len(results)
        for item, message, call_id, outcome in zip(
            attempts, results, expected_ids, ["success", "failure", "success", "failure", "success"], strict=True,
        ):
            assert isinstance(item.payload, dict)
            assert item.payload["tool_call_id"] == call_id
            assert item.payload["content"] == message["content"]
            assert item.payload["tool_outcome"] == outcome
        assemblies = cold.list_context_assemblies(session, thread)
        assert len(assemblies) == len(requests) == len(operations) + 1
        for assembly, request in zip(assemblies, requests, strict=True):
            assert (assembly.session_id, assembly.thread_id) == (session, thread)
            assert assembly.request_hash_preimage == normalize_wire_request(request)
            assembly.validate_hashes()
