"""模型工具的非法资源身份只留诊断，纠正调用沿原执行和持久链路完成。"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from langchain_core.messages import ToolMessage

from app.domain.itemized.serialization import normalize_wire_request
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchRequest,
)
from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 沿当前测试文件隔离工作区、端口和产物。
)


def _parallel_calls(calls: list[tuple[str, str, dict[str, object]]]) -> dict[str, object]:
    """只准备模型响应内容，协议编码复用原 model_stream。"""
    return {"tool_calls": [
        {"index": index, "id": call_id, "type": "function", "function": {
            "name": "invoke_extension_tool", "arguments": json.dumps({
                "tool_name": name, "arguments": arguments,
            }, ensure_ascii=False),
        }}
        for index, (call_id, name, arguments) in enumerate(calls)
    ]}


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_identity_errors_and_corrections_share_canonical_and_model_request_contract(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    session_id, thread_id, _ = await _target(case)
    identity = str(workspace_session_inventory_identity(case.container.workspace_id))
    common = {"resource_identity": identity, "uri": workspace_session_inventory_uri(case.container.workspace_id), "scope": "workspace"}
    valid: dict[str, dict[str, object]] = {
        "read_context": {**common, "view": "inventory"},
        "search_context": {**common, "query": "持久任务目标", "sources": ["session_catalog"]},
    }
    invalid_calls = [
        (f"invalid-{name}-{index}", name, {**arguments, "resource_identity": location})
        for name, arguments in valid.items()
        for index, location in enumerate(("/private/context-identity", r"C:\private\context-identity", r"\\target\private\context-identity"))
    ]
    valid_calls = [(f"corrected-{name}", name, arguments) for name, arguments in valid.items()]
    query = case.container.session_context_query_service
    read, search = query.read_context, query.search_context
    observed: list[tuple[str, str]] = []

    async def observe_read(request: SessionContextReadRequest):
        observed.append(("read_context", request.resource_identity))
        return await read(request)

    async def observe_search(request: SessionContextSearchRequest):
        observed.append(("search_context", request.resource_identity))
        return await search(request)

    monkeypatch.setattr(query, "read_context", observe_read)
    monkeypatch.setattr(query, "search_context", observe_search)
    calls = await case.plan(session_id, [
        _parallel_calls(invalid_calls), _parallel_calls(valid_calls), {"content": "资源身份已纠正，读取和搜索完成。"},
    ])
    accepted = await _send(case, session_id, "验证上下文引用后读取并搜索当前工作区。")
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    job = await _job(case, job_id)
    assert job["session_id"] == session_id and job["thread_id"] == thread_id
    assert case.call_count(session_id, thread_id) == 3
    # 被拒绝的六次调用均未进入业务查询；后两次只观察原方法，不替代其实现。
    assert sorted(observed) == [("read_context", identity), ("search_context", identity)]
    safe_error = "ResourceIdentity resource_identity 不得携带路径/URI/credential 形态"
    after_invalid = case.request(calls[1])["messages"]
    assert isinstance(after_invalid, list)
    rejected = [message for message in after_invalid if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in rejected] == [entry[0] for entry in invalid_calls]
    assert all(message["content"] == safe_error for message in rejected)
    assistant, = [message for message in after_invalid if message["role"] == "assistant" and message.get("tool_calls")]
    # 历史模型输入逐字保留，禁止把错误输出修复变成对原调用输入的改写。
    assert [json.loads(call["function"]["arguments"]) for call in assistant["tool_calls"]] == [
        {"tool_name": name, "arguments": arguments} for _, name, arguments in invalid_calls
    ]
    final_messages = case.request(calls[2])["messages"]
    assert isinstance(final_messages, list)
    final_tools = [message for message in final_messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in final_tools] == [entry[0] for entry in invalid_calls + valid_calls]
    for message in final_tools[-2:]:
        body = json.loads(message["content"])
        assert (body["resource_identity"], body["uri"], body["scope"]) == (identity, common["uri"], "workspace")
        assert body["items"] if message["tool_call_id"] == "corrected-read_context" else body["matches"]
    storage = case.container.checkpointer._storage.storage
    items = storage.read_items(session_id, thread_id)
    results = [item for item in items if item.semantic_kind == "tool_result"]
    # A 保存执行事实，P 保存请求投影；两者按原 typed 身份各自唯一，不能按正文去重。
    attempts = [item for item in results if item.projection_identity is not None
                and item.projection_identity.projection_message_id is None]
    projections = [item for item in results if item.projection_identity is not None
                   and item.projection_identity.projection_message_id is not None]
    assert len(results) == len(attempts) + len(projections) == 2 * len(final_tools)
    expected = {
        (calls[0] if index < len(invalid_calls) else calls[1], message["tool_call_id"]): message
        for index, message in enumerate(final_tools)
    }
    assert len(attempts) == len(projections) == len(expected)
    for item in results:
        assert isinstance(item.payload, Mapping)
        assert item.projection_identity is not None and item.status == "completed"
        call = item.projection_identity.model_call_id
        assert isinstance(call, str)
        is_attempt = item.projection_identity.projection_message_id is None
        provider_id = item.payload["tool_call_id"]
        assert isinstance(provider_id, str)
        if is_attempt:
            assert provider_id.startswith(call + ":tool-call:")
            provider_id = provider_id.removeprefix(call + ":tool-call:")
            assert storage.committed_tool_result_id(session_id, thread_id, call, provider_id) == item.item_id
        message = expected[(call, provider_id)]
        assert item.producer_ref["producer_kind"] == ("tool" if is_attempt else "provider")
        assert item.payload["name"] == "invoke_extension_tool" and item.payload["content"] == message["content"]
        assert item.payload["tool_outcome"] == ("failure" if call == calls[0] else "success")
        assert item.payload["execution_confirmed"] is True
    for carrier in (attempts, projections):
        assert len({(item.projection_identity.model_call_id, item.tool_call_id) for item in carrier
                    if item.projection_identity is not None}) == len(expected)
    projected = [message for message in project_canonical_items(items, preserve_order=True)
                 if isinstance(message, ToolMessage)]
    assert [(message.tool_call_id, message.content, message.status) for message in projected] == [
        (message["tool_call_id"], message["content"], "error" if index < len(invalid_calls) else "success")
        for index, message in enumerate(final_tools)
    ]
    assert "private" not in json.dumps([item.payload for item in results], ensure_ascii=False)
    failures = [record.exc_info[1] for record in caplog.records
                if record.name == "app.core.failures" and record.exc_info and isinstance(record.exc_info[1], ValueError)]
    assert len(failures) == len(invalid_calls)
    assert sorted(error.__notes__[0] for error in failures) == sorted(
        f"被拒绝的 ResourceIdentity resource_identity: {arguments['resource_identity']!r}" for _, _, arguments in invalid_calls
    )
    assemblies = case.container.checkpointer._storage.assemblies.list_context_assemblies(session_id, thread_id)
    assert {assembly.model_call_id for assembly in assemblies} == set(calls)
    for assembly in assemblies:
        assert (assembly.session_id, assembly.thread_id) == (session_id, thread_id)
        assert isinstance(assembly.model_call_id, str)
        assert assembly.request_hash_preimage == normalize_wire_request(case.request(assembly.model_call_id))
        assembly.validate_hashes()
    case.write_actual("resource-identity-errors", {
        "job": job, "observed_queries": observed, "calls": calls,
        "tool_results": [item.payload for item in results],
    })
