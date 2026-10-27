"""真实产品入口核对初始禁用目录的身份、隐藏与执行拒绝。"""

from __future__ import annotations

import json

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.backend.agents.test_debug_prompt_flow import _tool_call
from tests.integration.backend.agents.test_mcp_connection_control import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 同一真实 MCP Server
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 同一真实 OAuth Server
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    product_case as product_case,  # noqa: PLC0414 - 同一 Gateway／Backend 装具
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 本文件正式工作区
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done


async def test_initially_disabled_tool_is_known_but_never_reaches_server(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, tool_restrictions={
            "execution_disabled": ["mcp__schema__probe"], "model_hidden": ["connections.manage"],
        }) as (client, replay, case),
    ):
        session = await create_session(client, "初始禁用仍可辨认")
        response = await client.get(f"/api/v1/sessions/{session}/main-thread")
        assert response.status_code == 200, response.text
        thread = response.json()["data"]["thread_id"]
        operations = [
            ("search_tools", {"tool_ids": ["mcp__schema__probe", "connections.manage", "missing"]}),
            ("invoke_extension_tool", {"tool_name": "mcp__schema__probe", "arguments": {"proof": "must-not-run"}}),
        ]
        interactions = []
        for index in range(3):
            delta = {"content": "已确认禁用与隐藏，没有执行远端工具。"} if index == 2 else {
                "tool_calls": [_tool_call(call_index=index, name=operations[index][0], arguments=operations[index][1])],
            }
            interactions.append(build_cassette(
                asset_id=f"initial-discovery-policy-{index}", model="control-replay-model",
                url="https://opencode.ai/zen/v1/chat/completions",
                frames=chat_completion_frames(
                    response_id=f"chatcmpl-initial-discovery-policy-{index}", model="control-replay-model",
                    deltas=({"role": "assistant", **delta},), finish_reason="stop" if index == 2 else "tool_calls",
                ),
            ).raw["interactions"][0])
        calls = replay.control.plan_model_calls(session, thread, interactions)
        response = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "核查已知工具的禁用、隐藏与未知身份，并尝试一次受禁用策略保护的调用。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        await wait_for_job_done(client, response.json()["data"]["job_id"], max_attempts=100)
        records = [json.loads((replay.artifacts / f"{identity}.json").read_text())["request"] for identity in calls]
        results = [message for message in records[-1]["messages"] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == ["debug-e2e-call-0", "debug-e2e-call-1"]
        found = json.loads(results[0]["content"])["results"]
        assert [item["error"]["code"] for item in found] == ["tool-disabled", "tool-model-hidden", "tool-not-found"]
        assert all("input_schema" not in item and "description" not in item for item in found)
        assert "策略禁用" in results[1]["content"]
        assert not sibling.receipts.exists() or not sibling.receipts.read_text().strip()
        resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
        with RolloutCheckpointSaver(resolver.sessions_root) as saver:
            assemblies = saver.list_context_assemblies(session, thread)
            assert len(assemblies) == len(records) == 3
            for assembly, record in zip(assemblies, records, strict=True):
                wire = normalize_wire_request(record)
                assert assembly.request_hash_preimage == wire
                assert assembly.request_hash == context_request_hash(
                    assembly.as_sealed_plan(), assembly.provider_version, projector_id=assembly.projector_id,
                    projector_version=assembly.projector_version, target_format=assembly.target_format, wire_request=wire,
                )
