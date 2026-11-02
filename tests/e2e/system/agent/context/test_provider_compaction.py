"""真实消息生成历史，经手动压缩、封存请求及进程重启验证同一恢复链。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
from langchain_core.messages import AnyMessage, messages_to_dict
from langchain_core.runnables import RunnableConfig

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    acheckpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.e2e.system.agent.context.test_provider_provenance import (
    _audit_log_api,
    _checkpoint_state,
    _response,
    _save,
    _terminal_job,
)
from tests.e2e.system.agent.context.test_provider_provenance import (
    client as client,  # noqa: PLC0414 - 端口、工作区及产物由本模块路径隔离
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
from tests.support.sealed_request_audit import logged_provider_wire


async def _checkpoint(
    workspace: Path, owner: MutationIntentOwner,
) -> tuple[list[AnyMessage], dict]:
    """只经正式 Saver 冷读；不替产品生成或修补历史。"""
    saver = RolloutCheckpointSaver(sessions_dir=workspace / ".boxteam" / "sessions")
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        checkpoint = await saver.aget_tuple(cast(RunnableConfig, build_checkpoint_config(owner.thread_id)))
        assert checkpoint is not None
        messages = await acheckpoint_messages(saver, checkpoint)
    return messages, checkpoint.checkpoint["channel_values"]


def _sealed_calls(thread_node: Path, logs: list[dict], owner: MutationIntentOwner) -> list[dict]:
    """摘要与普通调用都核实际 wire、owner 及 hash，不假设摘要输出是普通回复。"""
    snapshots = []
    index = thread_node / "rollout" / "index.sqlite"
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as connection:
        for log in logs:
            assert log["session_id"] == owner.session_id
            assert log["response"]["error"] is None
            attempts = log["upstream"]["attempts"]
            assert attempts and attempts[-1]["error"] is None
            for attempt in attempts:
                reference = attempt["sealed_dispatch"]
                row = connection.execute(
                    "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=? "
                    "AND session_id=? AND thread_id=?",
                    (reference["assembly_id"], owner.session_id, owner.thread_id),
                ).fetchone()
                assert row is not None
                snapshot = ContextAssemblySnapshot.from_dict(load_snapshot(connection, row[0]))
                snapshot.validate_hashes()
                expected = SealedContextDispatchRef.from_snapshot(
                    snapshot, checkpoint_ns=reference["checkpoint_ns"], attempt_key=reference["attempt_key"],
                    callback_model_call_id=reference["callback_model_call_id"],
                )
                assert reference == json.loads(canonical_json_bytes(asdict(expected)))
                assert logged_provider_wire(attempt, target_format=snapshot.target_format) == snapshot.request_hash_preimage
                assert connection.execute(
                    "SELECT session_id,thread_id,execution_id,assembly_id FROM model_calls WHERE model_call_id=?",
                    (reference["sealed_model_call_id"],),
                ).fetchone() == (owner.session_id, owner.thread_id, reference["execution_id"], snapshot.assembly_id)
                snapshots.append(snapshot.to_dict())
    assert len({snapshot["assembly_id"] for snapshot in snapshots}) == len(snapshots)
    return snapshots


async def test_real_provider_compacts_product_history_and_resumes_after_restart(
    client: httpx.AsyncClient, e2e_workspace_root_path: str, e2e_backend_process: subprocess.Popen[str],
    provenance_backend_start: Callable[[str], subprocess.Popen[str]], provenance_artifacts: Path,
) -> None:
    artifacts = provenance_artifacts
    workspace = Path(e2e_workspace_root_path)
    for endpoint in ("/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"):
        _response(await client.get(endpoint), artifacts / f"{endpoint.rsplit('/', 1)[1]}-initialization.json")
    tools = _response(await client.get("/api/v1/tools"), artifacts / "tools.json")["data"]
    assert next(tool for tool in tools if tool["tool_id"] == "compact_conversation")["parameters"]["properties"] == {}
    # 本场景只需摘要相关工具，避免无关定义和上游回退共同耗尽有界诊断日志。
    # 不提高产品日志上限，仍逐项核对实际请求与完整封存事实。
    compact_tools = {"compact_conversation", "read_compaction_history"}
    selected = _response(await client.patch("/api/v1/tools/selection", json={
        "agent_id": "default", "changes": [
            {"tool_id": tool["tool_id"], "execution_enabled": tool["tool_id"] in compact_tools,
             "model_visible": tool["tool_id"] in compact_tools}
            for tool in tools if tool["execution_enabled"] or tool["tool_id"] in compact_tools
        ],
    }), artifacts / "selected-tools.json")["data"]
    assert {tool["tool_id"] for tool in selected if tool["execution_enabled"]} == compact_tools
    assert {tool["tool_id"] for tool in selected if tool["model_visible"]} == compact_tools
    session = _response(await client.post("/api/v1/sessions", json={
        "title": "真实 Provider 产品历史压缩与冷恢复",
    }), artifacts / "created-session.json")["data"]
    session_id, thread_id = session["session_id"], session["thread_id"]
    owner = MutationIntentOwner(session_id, thread_id)
    _response(await client.patch(f"/api/v1/sessions/{session_id}", json={
        "provider_id": "backup_3",
    }), artifacts / "selected-provider.json")
    marker = f"COMPACTION_COLD_{uuid4().hex}_OK"
    archive_markers = [f"COMPACTION_DETAIL_{uuid4().hex}" for _ in range(6)]
    prompts = []
    turn_ids = []

    async def send(prompt: str, phase: str) -> str:
        sent = _response(await client.post(f"/api/v1/sessions/{session_id}/messages", json={
            "message": {"content": prompt}, "run": {"mode": "single_agent", "agent_id": "default"},
        }), artifacts / f"{phase}-entry.json")["data"]
        job = await _terminal_job(client, sent["job_id"], artifacts / f"{phase}-job.json")
        assert job["status"] in {"completed", "succeeded"}, job
        assert isinstance(sent["turn_id"], str) and sent["turn_id"] not in turn_ids
        turn_ids.append(sent["turn_id"])
        return sent["job_id"]

    for ordinal, archive_marker in enumerate(archive_markers):
        material = "\n".join(
            f"{archive_marker} 第{line:03d}条：保留本轮独立背景资料，历史归档必须来自真实用户输入。"
            for line in range(60)
        )
        prompt = (
            f"记住这些资料供稍后压缩。只回复 ACK_{ordinal}，不要调用工具，也不要复述资料。\n{material}"
        )
        if ordinal == 0:
            prompt += f"\n会话恢复回执是 {marker}；以后只在我要求恢复回执时输出它。"
        prompts.append(prompt)
        await send(prompt, f"history-{ordinal}")
    before, before_channels = await _checkpoint(workspace, owner)
    assert before_channels.get("_summarization_event") is None
    assert [message.type for message in before] == ["human", "ai"] * 6
    assert [message.content for message in before[::2]] == prompts
    assert [message.text.strip() for message in before[1::2]] == [f"ACK_{index}" for index in range(6)]
    assert len({message.id for message in before}) == len(before) and all(message.id for message in before)
    _save(artifacts / "before-messages.json", messages_to_dict(before))
    compact = _response(await client.post(f"/api/v1/sessions/{session_id}/compact"), artifacts / "compact.json")["data"]
    assert compact["status"] == "scheduled" and compact["strategy"] is None and compact["summary"] is None
    for field in ("before_message_count", "effective_message_count_before", "effective_message_count_after", "retained_message_count"):
        assert compact[field] == 12
    assert compact["summarized_message_count"] == 0 and "history_file_path" not in compact
    scheduled, scheduled_channels = await _checkpoint(workspace, owner)
    assert messages_to_dict(scheduled) == messages_to_dict(before)
    assert scheduled_channels["_force_cache_compaction"] is True
    compact_job_id = await send("执行已安排的压缩，只回复 COMPACTED_OK，不要调用工具。", "compact-trigger")
    messages, channels = await _checkpoint(workspace, owner)
    assert messages_to_dict(messages[:len(before)]) == messages_to_dict(before)
    assert messages[-1].text.strip() == "COMPACTED_OK"
    event = channels["_summarization_event"]
    _save(artifacts / "compaction-event.json", {
        **event, "cache_prefix_messages": messages_to_dict(event["cache_prefix_messages"]),
        "summary_message": messages_to_dict([event["summary_message"]]),
    })
    assert event["strategy"] == "cache_preserving" and channels["_force_cache_compaction"] is False
    prefix = event["cache_prefix_messages"]
    cutoff = event["cutoff_index"]
    assert 2 <= len(prefix) < cutoff < len(before)
    assert len(prefix) % 2 == 0, "缓存前缀停在已完成轮次之后；安全摘要切点可以位于轮次内"
    assert messages_to_dict(prefix) == messages_to_dict(before[:len(prefix)])
    assert event["cutoff_message_id"] == before[cutoff - 1].id
    summary = event["summary_message"]
    assert summary.additional_kwargs["lc_source"] == "summarization"
    assert "read_compaction_history(line_offset=1, limit=200)" in str(summary.content)
    assert "session-artifacts/" not in str(summary.content) and "history.md" not in str(summary.content)
    resolver = get_session_path_resolver(workspace / ".boxteam" / "sessions")
    session_node = resolver.resolve_session_node(session_id)
    thread_node = resolver.resolve_thread_node(session_id, thread_id)
    history_file = session_node / "context" / thread_id / "history.md"
    history_bytes = history_file.read_bytes()
    archive = history_bytes.decode()
    assert "Summarized at" in archive
    for ordinal, archive_marker in enumerate(archive_markers):
        assert (archive_marker in archive) == (len(prefix) <= ordinal * 2 < cutoff)
    logs = await _audit_log_api(client, session_node, session_id, artifacts, phase="live")
    assert len([log for log in logs if log["job_id"] == compact_job_id]) == 2, "摘要调用及后继回复各派发一次"
    snapshots = _sealed_calls(thread_node, logs, owner)
    _save(artifacts / "live-sealed-assemblies.json", snapshots)
    request = {"direction": "head", "turns": len(turn_ids), "include": ["user", "text", "final_response", "metadata"]}
    live = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json=request), artifacts / "live-history.json")["data"]
    assert [turn["turn_id"] for turn in live["items"]] == turn_ids
    assert [turn["user_messages"][0]["content"] for turn in live["items"][:6]] == prompts
    assert [turn["final_response"].strip() for turn in live["items"]] == [*[f"ACK_{i}" for i in range(6)], "COMPACTED_OK"]
    storage_before = _checkpoint_state(thread_node)
    _save(artifacts / "before-restart-storage.json", storage_before)
    terminate_process(e2e_backend_process)
    provenance_backend_start("provider-compaction-cold-backend")
    cold = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json=request), artifacts / "cold-history.json")["data"]
    assert cold["items"] == live["items"]
    cold_messages, cold_channels = await _checkpoint(workspace, owner)
    assert messages_to_dict(cold_messages) == messages_to_dict(messages)
    assert cold_channels["_summarization_event"] == event
    assert cold_channels["_force_cache_compaction"] is False
    assert await _audit_log_api(client, session_node, session_id, artifacts, phase="cold") == logs
    assert _sealed_calls(thread_node, logs, owner) == snapshots
    assert _checkpoint_state(thread_node) == storage_before
    assert history_file.read_bytes() == history_bytes
    continuation = "不要调用工具。只回复我们第一轮约定的完整会话恢复回执，不加其它内容。"
    assert marker not in continuation
    continued_job_id = await send(continuation, "continued")
    continued = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json={
        "turn_ids": [turn_ids[-1]], "include": request["include"],
    }), artifacts / "continued-history.json")["data"]["items"]
    assert len(continued) == 1 and continued[0]["final_response"].strip() == marker
    continued_logs = await _audit_log_api(client, session_node, session_id, artifacts, phase="continued")
    assert [log for log in continued_logs if log["job_id"] != continued_job_id] == logs
    all_snapshots = _sealed_calls(thread_node, continued_logs, owner)
    assert all_snapshots[:len(snapshots)] == snapshots
    continued_calls = [log for log in continued_logs if log["job_id"] == continued_job_id]
    assert len(continued_calls) == 1
    assert sum(message["content"] == summary.content for message in continued_calls[0]["request"]["messages"]) == 1
    assert history_file.read_bytes() == history_bytes
    _save(artifacts / "compaction-audit.json", {
        "status": "passed", "session_id": session_id, "thread_id": thread_id, "turn_ids": turn_ids,
        "compact_job_id": compact_job_id, "continued_job_id": continued_job_id,
        "prefix_message_ids": [message.id for message in prefix], "cutoff_message_id": event["cutoff_message_id"],
        "assemblies": all_snapshots,
        "boundary": "real_gateway_backend_external_provider_async_execution_safe_restart",
    })
