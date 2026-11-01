"""随机文件链驱动真实 Provider 工具循环，并审计封存请求和冷历史。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import httpx

from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import resource_display_uri
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import stable_prefix_bytes
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)
from app.services.infrastructure.rollout_context.storage.catalog.item_reader import (
    RolloutCanonicalReadMixin,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.mapping.itemized.canonical_langchain import project_tool_result
from app.services.mapping.itemized.carrier_dedup import superseded_item_ids
from tests.e2e.system.agent.context.test_provider_provenance import (
    _assembly_history,
    _audit_attempt,
    _audit_calls,
    _audit_log_api,
    _checkpoint_state,
    _response,
    _save,
    _terminal_job,
    _trace_events,
)
from tests.e2e.system.agent.context.test_provider_provenance import (
    client as client,  # noqa: PLC0414 - 显式复用装具，工作区与端口按本文件隔离
)
from tests.e2e.system.agent.context.test_provider_provenance import (
    e2e_backend_composition as e2e_backend_composition,  # noqa: PLC0414
)
from tests.e2e.system.agent.context.test_provider_provenance import (
    provenance_artifacts as provenance_artifacts,  # noqa: PLC0414
)
from tests.e2e.system.agent.context.test_provider_provenance import (
    provenance_backend_start as provenance_backend_start,  # noqa: PLC0414
)
from tests.e2e.system.agent.context.test_provider_provenance import (
    provenance_gateway as provenance_gateway,  # noqa: PLC0414
)
from tests.support.processes.workspace import terminate_process


def _canonical_pairs(
    thread_node: Path, *, session_id: str, thread_id: str, job_id: str,
    uris: list[str], bodies: list[str],
) -> list[dict]:
    """只读真实 writer 的正文与索引，拒绝零工具、额外工具或跨 owner 配对。"""
    index = thread_node / "rollout" / "index.sqlite"
    with (
        closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as connection,
        index.with_name("rollout.jsonl").open("rb") as stream,
    ):
        rows = connection.execute(
            "SELECT i.item_sequence,i.jsonl_offset,i.jsonl_length,e.session_id,e.thread_id,e.job_id "
            "FROM item_catalog i JOIN executions e USING(execution_id) "
            "WHERE i.semantic_kind IN ('tool_call','tool_result') ORDER BY i.item_sequence",
        ).fetchall()
        assert all(row[3:] == (session_id, thread_id, job_id) for row in rows)
        raw_items = [RolloutCanonicalReadMixin._read_v2_item_at(
            connection, stream, sequence=row[0], offset=row[1], length=row[2],
        ) for row in rows]
    superseded = superseded_item_ids(raw_items)
    items = [item for item in raw_items if item.item_id not in superseded]
    assert [item.semantic_kind for item in items] == ["tool_call", "tool_result"] * len(uris)
    pairs = []
    for ordinal, (uri, body) in enumerate(zip(uris, bodies, strict=True)):
        call, result = items[ordinal * 2:ordinal * 2 + 2]
        assert call.status == result.status == "completed"
        assert call.tool_call_id and call.tool_call_id == result.tool_call_id
        assert call.projection_identity is not None and result.projection_identity is not None
        model_call_id = call.projection_identity.model_call_id
        assert model_call_id and model_call_id == result.projection_identity.model_call_id
        assert call.turn_id and call.turn_id == result.turn_id
        assert isinstance(call.payload, dict)
        calls = call.payload.get("tool_calls", [call.payload])
        assert isinstance(calls, list) and len(calls) == 1
        raw_call = calls[0]
        assert raw_call["name"] == "read_file"
        assert raw_call["args"]["path"] == uri
        tool_result = project_tool_result(result)
        assert tool_result.status == "success"
        assert tool_result.response_metadata["execution_confirmed"] is True
        assert tool_result.response_metadata["tool_outcome"] == "success"
        assert tool_result.tool_call_id == provider_tool_call_id(call.projection_identity, call.tool_call_id)
        assert isinstance(tool_result.content, str)
        assert all(line in tool_result.content for line in body.splitlines())
        assert f"sha256:{hashlib.sha256(body.encode()).hexdigest()}" in tool_result.content
        raw_carriers = [item for item in raw_items
                        if item.projection_identity is not None
                        and item.projection_identity.model_call_id == model_call_id]
        for carrier in raw_carriers:
            assert carrier.turn_id == call.turn_id
            assert carrier.tool_call_id is not None
            assert provider_tool_call_id(carrier.projection_identity, carrier.tool_call_id) == tool_result.tool_call_id
            if carrier.semantic_kind == "tool_result":
                assert project_tool_result(carrier).content == tool_result.content
            else:
                assert isinstance(carrier.payload, dict)
                assert (carrier.payload.get("tool_calls", [carrier.payload]))[0]["args"] == raw_call["args"]
        pairs.append({
            "canonical_call": call.to_dict(), "canonical_result": result.to_dict(),
            "raw_carriers": [item.to_dict() for item in raw_carriers],
            "model_call_id": model_call_id, "turn_id": call.turn_id,
            "wire_id": tool_result.tool_call_id, "name": "read_file", "args": raw_call["args"],
            "result": tool_result.content,
        })
    assert len({pair["wire_id"] for pair in pairs}) == len(uris)
    assert len({pair["model_call_id"] for pair in pairs}) == len(uris)
    assert len({pair["turn_id"] for pair in pairs}) == 1
    return pairs


def _wire_pairs(snapshot: ContextAssemblySnapshot, pairs: list[dict]) -> None:
    """已用正式编码核对日志与封存 bytes；这里额外核 call/result 的 ID、参数、正文和顺序。"""
    wire = snapshot.request_hash_preimage
    assert isinstance(wire, dict)
    expected_calls = [{"id": pair["wire_id"], "name": pair["name"], "args": pair["args"]} for pair in pairs]
    expected_results = [(pair["wire_id"], pair["result"]) for pair in pairs]
    calls = []
    results = []
    order = []
    if snapshot.target_format == "responses":
        inputs = wire["input"]
        assert isinstance(inputs, list)
        for item in inputs:
            assert isinstance(item, dict)
            if item.get("type") == "function_call":
                calls.append({"id": item["call_id"], "name": item["name"], "args": json.loads(item["arguments"])})
                order.append(("call", item["call_id"]))
            elif item.get("type") == "function_call_output":
                results.append((item["call_id"], item["output"]))
                order.append(("result", item["call_id"]))
    else:
        assert snapshot.target_format == "chat_completions"
        messages = wire["messages"]
        assert isinstance(messages, list)
        for message in messages:
            assert isinstance(message, dict)
            for call in message.get("tool_calls", []):
                calls.append({"id": call["id"], "name": call["function"]["name"], "args": json.loads(call["function"]["arguments"])})
                order.append(("call", call["id"]))
            if message["role"] == "tool":
                results.append((message["tool_call_id"], message["content"]))
                order.append(("result", message["tool_call_id"]))
    assert calls == expected_calls
    assert results == expected_results
    assert order == [entry for pair in pairs for entry in (("call", pair["wire_id"]), ("result", pair["wire_id"]))]


async def test_real_provider_follows_tool_results_and_preserves_cold_history(
    client: httpx.AsyncClient, e2e_workspace_root_path: str, e2e_backend_process: subprocess.Popen[str],
    provenance_backend_start: Callable[[str], subprocess.Popen[str]], provenance_artifacts: Path,
) -> None:
    artifacts = provenance_artifacts
    workspace = Path(e2e_workspace_root_path)
    for endpoint in ("/api/gateway/health", "/api/gateway/workspaces"):
        _response(await client.get(endpoint), artifacts / f"{endpoint.rsplit('/', 1)[1]}-initialization.json")
    workspace_info = _response(await client.get("/api/v1/workspace"), artifacts / "workspace-initialization.json")["data"]
    names = [f"tool-chain-{uuid4().hex}.txt" for _ in range(3)]
    uris = [resource_display_uri(scope="workspace", scope_id=workspace_info["workspace_id"],
        kind="files", tail_segments=("root", name)) for name in names]
    marker = f"PROVIDER_TOOL_CHAIN_{uuid4().hex}_OK"
    bodies = [f"下一份文件：{uris[1]}\n", f"下一份文件：{uris[2]}\n", f"最终回执：{marker}\n"]
    for name, body in zip(names, bodies, strict=True):
        (workspace / name).write_bytes(body.encode())
    prompt = (
        f"请从 {uris[0]} 开始，只使用 read_file 工具按文件中给出的 URI 逐份读取。"
        "每次 read_file 的 path 原样使用完整 URI；后续文件名只在前一次工具结果中提供，不要猜测或搜索。"
        "读到最终回执后，只回复其完整标记，不加解释；不要使用其它工具。"
    )
    assert marker not in prompt and all(uri not in prompt for uri in uris[1:])
    _save(artifacts / "input-chain.json", {"uris": uris, "bodies": bodies, "marker": marker, "prompt": prompt})
    session = _response(await client.post("/api/v1/sessions", json={"title": "真实 Provider 多次工具循环"}), artifacts / "created-session.json")["data"]
    session_id, thread_id = session["session_id"], session["thread_id"]
    session = _response(await client.patch(f"/api/v1/sessions/{session_id}", json={"provider_id": "backup_3"}), artifacts / "selected-provider.json")["data"]
    desired = ToolSelectionStore(boxteam_root=workspace / ".boxteam").snapshot("default")
    sent = _response(await client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": prompt}, "run": {"mode": "single_agent", "agent_id": "default"},
    }), artifacts / "message-entry.json")["data"]
    # TODO：补齐真实 Provider 限流／开流超时及回退参数纠错后的完整验收；保留既有等待期限和严格工具配对断言。
    job = await _terminal_job(client, sent["job_id"], artifacts / "job.json")
    logs = await _audit_log_api(client, get_session_path_resolver(workspace / ".boxteam" / "sessions").resolve_session_node(session_id),
        session_id, artifacts, phase="live")
    assert job["status"] in {"completed", "succeeded"}, job
    resolver = get_session_path_resolver(workspace / ".boxteam" / "sessions")
    session_node = resolver.resolve_session_node(session_id)
    thread_node = resolver.resolve_thread_node(session_id, thread_id)
    pairs = _canonical_pairs(thread_node, session_id=session_id, thread_id=thread_id, job_id=sent["job_id"], uris=uris, bodies=bodies)
    _save(artifacts / "canonical-tool-pairs.json", pairs)
    logs = [log for log in logs if log["job_id"] == sent["job_id"]]
    assert len(logs) == 4, "三次依赖前次结果的工具调用必须各自产生后续模型请求"
    audited = []
    successful = []
    for ordinal, log in enumerate(logs):
        assert log["response"]["error"] is None
        attempts = log["upstream"]["attempts"]
        assert attempts and attempts[-1]["error"] is None
        for attempt_index, attempt in enumerate(attempts):
            snapshot, receipt = _audit_attempt(log, attempt, thread_node,
                session_id=session_id, thread_id=thread_id, desired_revision=desired.revision,
                artifacts=artifacts, ordinal=f"{ordinal + 1}-attempt-{attempt_index + 1}")
            assert snapshot.turn_id == pairs[0]["turn_id"]
            _wire_pairs(snapshot, pairs[:ordinal])
            if ordinal < 3:
                serialized = canonical_json_bytes(snapshot.request_hash_preimage).decode()
                assert marker not in serialized
                assert all(uri not in serialized for uri in uris[ordinal + 1:])
            audited.append((snapshot, receipt))
        snapshot, receipt = audited[-1]
        successful.append(snapshot)
        assert {message["response_metadata"]["model_call_id"] for message in log["response"]["result"]} == {
            receipt["dispatch_ref"]["callback_model_call_id"],
        }
        if ordinal < 3:
            assert receipt["dispatch_ref"]["callback_model_call_id"] == pairs[ordinal]["model_call_id"]
    assert len({snapshot.model_call_id for snapshot in successful}) == 4
    for previous, current in pairwise(successful):
        old, new = previous.seal_binding, current.seal_binding
        assert old is not None and new is not None
        assert new.previous_assembly_id == previous.assembly_id
        if old.prefix.state.provider_profile == new.prefix.state.provider_profile:
            assert old.prefix.state.epoch == new.prefix.state.epoch
            assert stable_prefix_bytes(new.prefix.state.frames).startswith(stable_prefix_bytes(old.prefix.state.frames))
    traces = await _trace_events(client, session_id, artifacts, 1)
    assert [event["raw"]["payload"]["model"] for event in traces
            if event["job_id"] == sent["job_id"] and event["type"] == "llm_request"] == [
        receipt["upstream_model"] for _, receipt in audited
    ]
    request = {"turn_ids": [successful[-1].turn_id], "include": ["user", "text", "final_response", "metadata"]}
    live = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json=request), artifacts / "live-history.json")["data"]
    assert len(live["items"]) == 1
    turn = live["items"][0]
    assert (turn["turn_id"], turn["job_id"]) == (successful[-1].turn_id, sent["job_id"])
    assert turn["user_messages"][0]["content"] == prompt
    assert turn["final_response"].strip() == marker
    for snapshot, receipt in audited:
        await _assembly_history(client, session, snapshot, artifacts, phase="live",
            selected_message_ids=receipt["selected_history_message_ids"])
    before = _checkpoint_state(thread_node)
    _save(artifacts / "before-cold-checkpoint-state.json", before)
    # 改掉原输入后再经正式入口冷读：历史必须来自已提交版本，不能重读当前文件。
    terminate_process(e2e_backend_process)
    for name in names:
        (workspace / name).write_text("CURRENT_CONTENT_IS_NOT_EXECUTION_HISTORY\n", encoding="utf-8")
    provenance_backend_start("provider-tool-loop-cold-backend")
    cold = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json=request), artifacts / "cold-history.json")["data"]
    assert cold["items"] == live["items"]
    assert await _audit_log_api(client, session_node, session_id, artifacts, phase="cold") == logs
    assert _canonical_pairs(thread_node, session_id=session_id, thread_id=thread_id, job_id=sent["job_id"], uris=uris, bodies=bodies) == pairs
    with closing(sqlite3.connect((thread_node / "rollout" / "index.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        for snapshot, receipt in audited:
            manifest, = connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (snapshot.assembly_id,)).fetchone()
            assert canonical_json_bytes(load_snapshot(connection, manifest)) == canonical_json_bytes(snapshot.to_dict())
            await _assembly_history(client, session, snapshot, artifacts, phase="cold",
                selected_message_ids=receipt["selected_history_message_ids"])
    after = _checkpoint_state(thread_node)
    _save(artifacts / "after-cold-checkpoint-state.json", after)
    assert after == before, "冷历史、assembly 和工具结果查询不能改写 checkpoint 或正文引用"
    # 继续经消息入口执行一次，证明新进程真正恢复旧模型上下文，而非只读历史展示。
    continuation_prompt = "不要调用工具或重新读取文件。只复述上一任务得到的完整最终回执标记，不加其它内容。"
    assert marker not in continuation_prompt
    continued = _response(await client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": continuation_prompt}, "run": {"mode": "single_agent", "agent_id": "default"},
    }), artifacts / "continued-message-entry.json")["data"]
    continued_job = await _terminal_job(client, continued["job_id"], artifacts / "continued-job.json")
    assert continued_job["status"] in {"completed", "succeeded"}, continued_job
    continued_calls = _audit_calls(session_node, thread_node, session_id=session_id, thread_id=thread_id,
        job_id=continued["job_id"], desired_revision=desired.revision, artifacts=artifacts, ordinal=2, succeeded=True)
    for snapshot, receipt in continued_calls:
        assert snapshot.turn_id != successful[-1].turn_id
        _wire_pairs(snapshot, pairs)
        await _assembly_history(client, session, snapshot, artifacts, phase="continued",
            selected_message_ids=receipt["selected_history_message_ids"])
    continued_history = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json={
        **request, "turn_ids": [continued_calls[-1][0].turn_id],
    }), artifacts / "continued-history.json")["data"]
    assert len(continued_history["items"]) == 1
    continued_turn = continued_history["items"][0]
    assert continued_turn["job_id"] == continued["job_id"]
    assert continued_turn["user_messages"][0]["content"] == continuation_prompt
    assert continued_turn["final_response"].strip() == marker
    assert _canonical_pairs(thread_node, session_id=session_id, thread_id=thread_id,
        job_id=sent["job_id"], uris=uris, bodies=bodies) == pairs, "续聊不得重新执行旧工具"
    assert ToolSelectionStore(boxteam_root=workspace / ".boxteam").snapshot("default").revision == desired.revision
    _save(artifacts / "tool-loop-audit.json", {
        "status": "passed", "session_id": session_id, "thread_id": thread_id, "job_id": sent["job_id"],
        "marker": marker, "pairs": pairs, "calls": [receipt for _, receipt in audited],
        "continued_job_id": continued["job_id"], "continued_calls": [receipt for _, receipt in continued_calls],
        "boundary": "real_gateway_backend_external_provider_async_execution_safe_restart",
    })
