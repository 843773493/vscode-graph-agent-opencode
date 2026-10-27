"""真实 Provider 自主发现受控 MCP；不使用模型回放，不把协议资产称为 E2E。"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
import tiktoken
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
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
from configs.runtime import read_jsonc_object
from tests.support.api_helpers import create_session
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)
from tests.support.sealed_request_audit import logged_provider_wire

_TOOL_ID = "mcp__schema__probe"
_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["proof", "lane"],
    "properties": {
        "proof": {"type": "string", "const": "MCP_DISCOVERY_FROM_FROZEN_DEFINITION"},
        "lane": {"type": "string", "enum": ["left", "right"]},
    },
}


@dataclass(frozen=True)
class ProviderCase:
    client: httpx.AsyncClient
    workspace: Path
    artifacts: Path
    receipts: Path


def _save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
async def provider_case(
    integration_workspace_root_path: str, integration_workspace_config_path: str,
    integration_backend_port: int,
) -> AsyncIterator[ProviderCase]:
    if not os.environ.get("CCTQ_API_KEY"):
        pytest.fail("UNMET_PREREQUISITE: CCTQ_API_KEY 未配置；本验收不能回退到模型回放")
    workspace = Path(integration_workspace_root_path)
    artifacts = workspace.parent / "artifacts"
    artifacts.mkdir(exist_ok=True)
    definitions, receipts = artifacts / "definitions.json", artifacts / "receipts.jsonl"
    _save(definitions, {"probe": _SCHEMA})
    config_path = Path(integration_workspace_config_path)
    config = cast(dict, read_jsonc_object(config_path))
    provider = next(value for value in config["llm"]["providers"] if value["id"] == "backup_3")
    config["llm"]["providers"] = [provider]
    for agent in config["agents"].values():
        agent["model"] = {"primary_provider": "backup_3", "fallback_providers": []}
    # 保留既有 Provider 请求设置；测试不增加采样、reasoning 或输出限制。
    config["agents"]["default"]["tools"]["custom"] = [{"tool_id": "connections.manage"}]
    config["agents"]["default"]["tools"]["confirmation_required"] = []
    config["mcp"] = {"servers": {"schema": {
        "enabled": True, "transport": "stdio", "command": sys.executable,
        "args": [str(Path.cwd() / "tests/integration/workspace_services/mcp/schema_server.py"),
                 str(definitions), str(receipts)], "cwd": str(Path.cwd()),
    }}}
    _save(config_path, config)
    gateway_port = integration_backend_port + 1
    backend = await asyncio.to_thread(
        start_backend_process, workspace_root=str(workspace), port=integration_backend_port,
        log_name="real-provider-discovery",
        env_overrides={"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}"},
    )
    try:
        gateway = await asyncio.to_thread(
            start_gateway_process, workspace_root=workspace,
            default_backend_url=f"http://127.0.0.1:{integration_backend_port}",
            port=gateway_port, artifacts_dir=artifacts / "gateway",
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway_port}", timeout=30,
                headers={**LOCAL_TOKEN_HEADERS, "X-Workspace-ID": backend.workspace_id},
            ) as client:
                await acquire_gateway_guest(client)
                for endpoint in ("/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"):
                    response = await client.get(endpoint)
                    assert response.status_code == 200, response.text
                    assert response.headers["x-request-id"] == response.json()["request_id"]
                    _save(artifacts / f"initial-{endpoint.rsplit('/', 1)[1]}.json", response.json())
                yield ProviderCase(client, workspace, artifacts, receipts)
        finally:
            await asyncio.to_thread(close_gateway_process, gateway)
    finally:
        await asyncio.to_thread(close_backend_process, backend)


async def _terminal(case: ProviderCase, job_id: str, ordinal: int) -> dict:
    for poll in range(240):
        response = await case.client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()["data"]
        if job["status"] in {"completed", "succeeded", "failed", "cancelled", "timed_out"}:
            _save(case.artifacts / f"turn-{ordinal}-job.json", job)
            return job
        if poll == 239:
            _save(case.artifacts / f"turn-{ordinal}-job.json", job)
        await asyncio.sleep(1)
    pytest.fail("真实 Provider 执行未在观察期限内结束，最后状态已保留")


def _audit_requests(case: ProviderCase, session: str, thread: str, job_ids: list[str]) -> list[dict]:
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    session_node = resolver.resolve_session_node(session)
    thread_node = resolver.resolve_thread_node(session, thread)
    logs = [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((session_node / "logs/llm_requests").glob("*.json"))]
    logs = [log for log in logs if log["job_id"] in job_ids]
    _save(case.artifacts / "provider-logs.json", logs)
    encoding = tiktoken.get_encoding("o200k_base")
    rows = []
    with closing(sqlite3.connect((thread_node / "rollout/index.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        for log in logs:
            for attempt in log["upstream"]["attempts"]:
                ref = attempt["sealed_dispatch"]
                assert (ref["session_id"], ref["thread_id"]) == (session, thread)
                manifest = connection.execute(
                    "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=? AND session_id=? AND thread_id=?",
                    (ref["assembly_id"], session, thread),
                ).fetchone()
                assert manifest is not None
                snapshot = ContextAssemblySnapshot.from_dict(load_snapshot(connection, manifest[0]))
                snapshot.validate_hashes()
                wire = logged_provider_wire(attempt, target_format=snapshot.target_format)
                assert canonical_json_bytes(wire) == canonical_json_bytes(snapshot.request_hash_preimage)
                # 单独计量实际输入中的完整定义；这是固定 tokenizer 的估算，非 Provider 计费归因。
                definitions = []
                for item in attempt["request"].get("input", []):
                    if item.get("type") != "function_call_output" or not isinstance(item.get("output"), str):
                        continue
                    content = item["output"]
                    if '"schema_state"' in content and '"input_schema"' in content:
                        definitions.append(content)
                response = attempt["response"]
                usage = response.get("usage") if response is not None else None
                rows.append({
                    "job_id": log["job_id"], "model_call_id": ref["callback_model_call_id"],
                    "request_hash": snapshot.request_hash, "assembly_id": snapshot.assembly_id,
                    "provider": attempt["provider"], "model": attempt["request"]["model"],
                    "error": attempt["error"], "provider_usage": usage,
                    "cached_tokens": usage.get("input_tokens_details", {}).get("cached_tokens") if usage is not None else None,
                    "definition_result_bytes": sum(len(body.encode()) for body in definitions),
                    "definition_result_estimated_tokens": sum(len(encoding.encode(body)) for body in definitions),
                    "toolset": snapshot.tool_set_refs[0].content_hash,
                    "epoch": ref["prefix_epoch"],
                })
    _save(case.artifacts / "request-metrics.json", rows)
    return rows


async def test_real_provider_discovers_complete_schema_and_records_cache_usage(provider_case: ProviderCase) -> None:
    case = provider_case
    session = await create_session(case.client, "真实 Provider 工具发现指标")
    response = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert response.status_code == 200, response.text
    thread = response.json()["data"]["thread_id"]
    jobs = []
    metrics: list[dict] = []
    for ordinal, prompt in enumerate((
        ("先用 search_tools 搜索 probe，取得它的完整定义后再调用。按定义里的 proof 常量，"
        "分别调用 lane=left 和 lane=right 各一次；可在同轮调用两个信封。不得猜测 proof，不调用其它工具。"
        "收到两份真实回执后只回复 MCP_PROVIDER_DISCOVERY_OK。"),
        ("继续使用刚才已发现的 probe 完整定义，再分别调用 lane=left 和 lane=right 各一次。"
        "收到两份真实回执后只回复 MCP_PROVIDER_REUSE_OK。"),
    ), 1):
        response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": prompt}, "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        entry = response.json()["data"]
        jobs.append(entry["job_id"])
        _save(case.artifacts / f"turn-{ordinal}-entry.json", entry)
        job = await _terminal(case, entry["job_id"], ordinal)
        metrics = _audit_requests(case, session, thread, jobs)
        assert job["status"] in {"completed", "succeeded"}, job
        assert metrics and all(row["error"] is None for row in metrics)

    with (
        RolloutCheckpointSaver(case.workspace / ".boxteam/sessions") as cold,
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
    ):
        checkpoint = await cold.aget_tuple(cast(RunnableConfig, build_checkpoint_config(thread)))
        assert checkpoint is not None
        messages = await acheckpoint_messages(cold, checkpoint)
    declarations = [(message, call) for message in messages if isinstance(message, AIMessage) for call in message.tool_calls]
    results = [message for message in messages if isinstance(message, ToolMessage)]
    search = [call for _, call in declarations if call["name"] == "search_tools"]
    invocations = [(message, call) for message, call in declarations if call["name"] == "invoke_extension_tool"]
    received = [json.loads(line) for line in case.receipts.read_text(encoding="utf-8").splitlines()]
    pairs = []
    for message, call in invocations:
        matching = [result for result in results if result.tool_call_id == call["id"]]
        assert len(matching) == 1
        pairs.append({"tool_call_id": call["id"], "model_call_id": message.response_metadata["model_call_id"],
                      "arguments": call["args"], "outcome": matching[0].status})
    successes = sum(pair["outcome"] == "success" for pair in pairs)
    _save(case.artifacts / "discovery-metrics.json", {
        "model_source": "real_provider", "mcp_source": "controlled_schema_server",
        "session_id": session, "thread_id": thread, "sample_executions": len(jobs),
        "discovery_calls": len(search), "invocation_attempts": len(pairs),
        "successful_invocations": successes, "success_rate": successes / len(pairs) if pairs else None,
        "first_invocation_succeeded": pairs[0]["outcome"] == "success" if pairs else None,
        "definition_token_estimator": "tiktoken o200k_base, complete search ToolResult text per actual request; excludes protocol overhead",
        "cache_evidence": "raw Provider response usage per request; absent is unavailable, zero is no reported hit; no savings guarantee",
        "pairs": pairs, "server_receipts": received, "requests": metrics,
    })
    assert search, "真实模型未使用发现入口"
    assert len(pairs) == successes == len(received) == 4
    assert {receipt["name"] for receipt in received} == {"probe"}
    assert all(receipt["arguments"]["proof"] == _SCHEMA["properties"]["proof"]["const"] for receipt in received)
    assert sorted(receipt["arguments"]["lane"] for receipt in received) == ["left", "left", "right", "right"]
    assert all(pair["arguments"]["tool_name"] == _TOOL_ID for pair in pairs)
    assert any(row["definition_result_estimated_tokens"] > 0 for row in metrics)
    assert all(row["provider_usage"] is not None for row in metrics), "真实 Provider 未提供用量，不能伪造缓存指标"
    assert len({row["toolset"] for row in metrics}) == 1
    assert len({row["epoch"] for row in metrics}) == 1
