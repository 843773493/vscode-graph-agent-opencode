"""真实后端硬重启丢失审批 handle，旧决定不得迁投新连接。"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    approval_plan as approval_plan,  # noqa: PLC0414 - 同一回放响应资产
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 原真实 MCP
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 同一未授权兄弟连接
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    product_case as product_case,  # noqa: PLC0414 - 原 Gateway／Backend 装具
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    read_approvals as read_approvals,  # noqa: PLC0414 - 读取产品审批入口
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 本文件正式隔离工作区
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.processes.workspace import start_backend_process


async def test_process_exit_during_approval_rejects_old_decision_and_allows_new_execution(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, approval_plan, read_approvals,
):
    backends = []
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("approval-restart") as original,
        product_case(secure, original, backend_processes=backends,
                     tool_restrictions={"confirmation_required": ["mcp__schema__probe"]}) as (client, replay, case),
    ):
        session = await create_session(client, "审批等待中硬重启")
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        planned, calls = approval_plan(replay, session, thread, complete=False)
        accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "调用两个 probe，等待我的审批。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert accepted.status_code == 200, accepted.text
        job = accepted.json()["data"]["job_id"]
        approval, = await read_approvals(client, job)
        next_call = str(UUID(hex=replay.control.next_uuid(f"model_call/{session}/{thread}")))
        ledger_path = case.mount.workspace_root / ".boxteam/resources.json"
        before = json.loads(ledger_path.read_text())
        qualified = [lease for lease in before["leases"] if lease["holder_session_id"] == session and lease["status"] == "active"]
        assert {lease["lease_id"].split(":", 1)[0] for lease in qualified} == {"mcp-prepared", "mcp-execution"}
        old_connection, = {lease["resource_id"] for lease in qualified}
        assert not original.receipts.exists()
        backends[0].process.kill()
        await asyncio.to_thread(backends[0].process.wait, 10)
        restarted = await asyncio.to_thread(
            start_backend_process, workspace_root=str(case.mount.workspace_root), port=backends[0].port,
            log_name="mcp-approval-restarted", asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={**replay.environment,
                "BOXTEAM_HOME": str(case.context.boxteam_home_for_node(f"mcp-product-gateway:{case.mount.workspace_id}")),
                "BOXTEAM_GATEWAY_URL": str(client.base_url).rstrip("/")},
        )
        backends.append(restarted)
        response = await client.get(f"/api/v1/sessions/{session}/traces", params={"thread_id": thread})
        assert response.status_code == 200, response.text
        loss, = [event for event in response.json()["data"]["items"] if event["job_id"] == job and event["type"] == "session_interrupted"]
        assert loss["thread_id"] == thread and loss["raw"]["payload"]["code"] == "execution_lost"
        assert loss["raw"]["payload"]["phase"] == "process_exit"
        assert (await client.get("/api/v1/jobs/tool-approvals", params={"session_id": session, "thread_id": thread})).json()["data"] == []
        late = await client.post(f"/api/v1/jobs/{job}/approvals", json={
            "command_id": "lost-handle-must-not-resume", "checkpoint_id": approval["checkpoint_id"], "responses": [{
                "approval_id": approval["approval_id"], "decisions": [{"tool_call_id": call["id"], "type": "approve"} for call in calls],
            }],
        })
        assert late.status_code == 404 and job in late.text, late.text
        after = json.loads(ledger_path.read_text())
        assert {lease["status"] for lease in after["leases"] if lease["holder_session_id"] == session} == {"released"}
        # 冷 owner 没有原 socket 的核实证据；原账本将其标为 orphaned，不宣称已停止。
        assert next(record for record in after["records"] if record["resource_id"] == old_connection)["status"] == "orphaned"
        assert not original.receipts.exists() and not (replay.artifacts / f"{next_call}.json").exists()

        # 新用户输入必须产生独立执行与新连接资格，不能恢复丢失的旧审批。
        new_plan, new_calls = approval_plan(replay, session, thread, call_offset=3)
        assert new_plan[0] == next_call
        new_message = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "重新发起独立 probe，这次只执行第一项。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert new_message.status_code == 200, new_message.text
        new_job = new_message.json()["data"]["job_id"]
        assert new_job != job
        new_approval, = await read_approvals(client, new_job)
        assert new_approval["approval_id"] != approval["approval_id"]
        submitted = await client.post(f"/api/v1/jobs/{new_job}/approvals", json={
            "command_id": "new-execution-approval", "checkpoint_id": new_approval["checkpoint_id"], "responses": [{
                "approval_id": new_approval["approval_id"], "decisions": [
                    {"tool_call_id": new_calls[0]["id"], "type": "approve"},
                    {"tool_call_id": new_calls[1]["id"], "type": "reject", "message": "重启后仍拒绝第二项"},
                ],
            }],
        })
        assert submitted.status_code == 200, submitted.text
        await wait_for_job_done(client, new_job, max_attempts=90)
        assert [json.loads(line)["arguments"] for line in original.receipts.read_text().splitlines()] == [{"proof": "approval-4"}]
        after = json.loads(ledger_path.read_text())
        active_connections = {record["resource_id"] for record in after["records"] if record["kind"] == "mcp_connection" and record["status"] == "running"}
        assert len(active_connections) == 1 and old_connection not in active_connections
        with RolloutCheckpointSaver(case.mount.workspace_root / ".boxteam/sessions") as saver:
            with saver._storage.storage.read_connection(session, thread) as connection:
                executions = connection.execute("SELECT job_id,execution_id,outcome FROM executions ORDER BY created_at").fetchall()
            assert [(row[0], row[2]) for row in executions] == [(job, "execution_lost"), (new_job, "completed")]
            assert executions[0][1] != executions[1][1]
            items = saver._storage.storage.read_items(session, thread)
            lost_model_call = planned[-1]
            notices = [item for item in items if item.semantic_kind == "tool_result"
                       and item.producer_ref["producer_kind"] == "runtime"]
            assert len(notices) == 2
            recovered_calls = set()
            for item in notices:
                assert item.projection_identity is not None and item.tool_call_id is not None
                assert isinstance(item.payload, dict)
                recovered_calls.add(provider_tool_call_id(item.projection_identity, item.tool_call_id))
                assert item.producer_ref["producer_id"] == executions[0][1]
                assert item.projection_identity.model_call_id == lost_model_call
                assert item.payload["tool_outcome"] == "unknown" and item.payload["execution_confirmed"] is False
                assert "tool_attempt_id" not in item.payload and "tool_invocation_id" not in item.payload
                with saver._storage.storage.read_connection(session, thread) as connection:
                    assert connection.execute(
                        "SELECT item.execution_id=execution.execution_id AND item.turn_id=execution.turn_id, "
                        "commit_record.commit_kind FROM item_catalog item "
                        "JOIN executions execution ON execution.job_id=? "
                        "JOIN storage_commits commit_record ON commit_record.commit_id=item.commit_id "
                        "WHERE item.item_id=?", (job, item.item_id),
                    ).fetchone() == (1, "terminal_convergence")
            assert recovered_calls == {call["id"] for call in calls}
            for identity in [*planned, *new_plan]:
                record = json.loads((replay.artifacts / f"{identity}.json").read_text())
                with saver._storage.storage.read_connection(session, thread) as connection:
                    assembly_id, = connection.execute("SELECT assembly_id FROM context_assemblies WHERE model_call_id=?", (record["identity"]["model_call_id"],)).fetchone()
                assembly = saver.get_context_assembly(session, thread, assembly_id=assembly_id)
                assert assembly.request_hash_preimage == normalize_wire_request(record["request"])
                assembly.validate_hashes()
