"""真实产品审批恢复原图，并保留替换前的 MCP 执行身份。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from configs.runtime import read_jsonc_object
from tests.integration.backend.agents.test_debug_prompt_flow import _tool_call
from tests.integration.backend.agents.test_mcp_connection_control import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 唯一真实 MCP 资产
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 产品装具的未授权兄弟连接
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    product_case as product_case,  # noqa: PLC0414 - 复用 Gateway／Backend／回放入口
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 本文件正式隔离工作区
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done


@pytest.fixture
def approval_plan():
    def plan(replay, session, thread, *, direct_calls=None):
        calls = direct_calls if direct_calls is not None else [_tool_call(
            call_index=index, name="invoke_extension_tool",
            arguments={"tool_name": "mcp__schema__probe", "arguments": {"proof": f"approval-{index}"}},
        ) for index in (1, 2)]
        deltas: list[dict[str, object]] = [] if direct_calls is not None else [{"tool_calls": [_tool_call(
            call_index=0, name="search_tools", arguments={"tool_ids": ["mcp__schema__probe"]},
        )]}]
        deltas.extend([{"tool_calls": calls}, {"content": "工具审批已按原调用逐一结算。"}])
        interactions = []
        for index, delta in enumerate(deltas):
            entries = build_cassette(
                asset_id=f"mcp-approval-{index}", model="control-replay-model",
                url="https://opencode.ai/zen/v1/chat/completions",
                frames=chat_completion_frames(
                    response_id=f"chatcmpl-mcp-approval-{index}", model="control-replay-model",
                    deltas=({"role": "assistant", **delta},), finish_reason="tool_calls" if "tool_calls" in delta else "stop",
                ),
            ).raw["interactions"]
            assert isinstance(entries, list) and isinstance(entries[0], dict)
            interactions.append(entries[0])
        return replay.control.plan_model_calls(session, thread, interactions), calls
    return plan


@pytest.fixture
def read_approvals():
    async def read(client, job):
        for _ in range(400):
            response = await client.get(f"/api/v1/jobs/{job}/approvals")
            assert response.status_code == 200, response.text
            requests = response.json()["data"]
            if requests:
                return requests
            state = (await client.get(f"/api/v1/jobs/{job}")).json()["data"]
            assert state["status"] not in {"failed", "completed", "cancelled"}, state
            await asyncio.sleep(0.1)
        raise TimeoutError("真实 Agent 尚未进入工具审批")
    return read


@pytest.mark.parametrize("selected", ["read_file", "unknown_tool"])
async def test_direct_tool_edit_preserves_original_call_and_completes_product_execution(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, approval_plan, read_approvals, selected,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("direct-approval") as original,
        product_case(secure, original, confirmation_required=["ls"]) as (client, replay, case),
    ):
        (case.mount.workspace_root / "approval-proof.txt").write_text("direct-edited-file-proof", encoding="utf-8")
        session = await create_session(client, "编辑直接工具")
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        planned, calls = approval_plan(replay, session, thread, direct_calls=[_tool_call(call_index=1, name="ls", arguments={"path": "."})])
        accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "列出目录，等待我的确认。"}, "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert accepted.status_code == 200, accepted.text
        job = accepted.json()["data"]["job_id"]
        approval, = await read_approvals(client, job)
        result = await client.post(f"/api/v1/jobs/{job}/approvals", json={
            "command_id": "direct-tool-edit", "checkpoint_id": approval["checkpoint_id"], "responses": [{
                "approval_id": approval["approval_id"], "decisions": [{"tool_call_id": calls[0]["id"], "type": "edit",
                    "edited_action": {"name": selected, "args": {"path": "approval-proof.txt"}}}],
            }],
        })
        assert result.status_code == 200, result.text
        await wait_for_job_done(client, job, max_attempts=80)
        records = [json.loads((replay.artifacts / f"{identity}.json").read_text()) for identity in planned]
        assert len(records) == 2
        emitting, = [message for message in records[-1]["request"]["messages"] if message.get("tool_calls")]
        assert emitting["tool_calls"][0]["function"]["name"] == "ls"
        assert json.loads(emitting["tool_calls"][0]["function"]["arguments"]) == {"path": "."}
        paired, = [message for message in records[-1]["request"]["messages"] if message.get("role") == "tool"]
        assert paired["tool_call_id"] == calls[0]["id"]
        assert ("direct-edited-file-proof" if selected == "read_file" else "unknown_tool is not a valid tool") in str(paired["content"])
        with RolloutCheckpointSaver(case.mount.workspace_root / ".boxteam/sessions") as saver:
            for record in records:
                with saver._storage.storage.read_connection(session, thread) as connection:
                    assembly_id, = connection.execute("SELECT assembly_id FROM context_assemblies WHERE model_call_id=?", (record["identity"]["model_call_id"],)).fetchone()
                assembly = saver.get_context_assembly(session, thread, assembly_id=assembly_id)
                assert assembly.request_hash_preimage == normalize_wire_request(record["request"])
                assembly.validate_hashes()


@pytest.mark.parametrize("first_decision", ["approve", "edit", "respond"])
async def test_approval_resumes_original_calls_across_http_replacement(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory,
    approval_plan, read_approvals, first_decision,
):
    async with (
        oauth_endpoint_factory() as secure,
        http_schema_endpoint_factory("original") as original,
        http_schema_endpoint_factory("replacement") as replacement,
        product_case(secure, original, tool_restrictions={"confirmation_required": ["mcp__schema__probe"]}) as (client, replay, case),
    ):
        session = await create_session(client, "MCP 原代际工具审批")
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        planned, calls = approval_plan(replay, session, thread)
        accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "发现并发出两个独立的 probe，分别等待我的决定。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert accepted.status_code == 200, accepted.text
        job = accepted.json()["data"]["job_id"]
        approvals = await read_approvals(client, job)
        assert len(approvals) == 1
        approval = approvals[0]
        assert [action["tool_call_id"] for action in approval["actions"]] == [call["id"] for call in calls]
        assert not original.receipts.exists() and not replacement.receipts.exists()
        before = json.loads((case.mount.workspace_root / ".boxteam/resources.json").read_text())
        qualified = [lease for lease in before["leases"] if lease["holder_session_id"] == session
                     and lease["lease_id"].startswith("mcp-prepared:") and lease["status"] == "active"]
        assert len(qualified) == 1 and qualified[0]["holder_thread_id"] == thread

        # 从产品配置输入替换 HTTP 连接；schema 保持相同，原资格不得迁投新 Server。
        path = case.mount.workspace_root / ".boxteam/workspace.jsonc"
        configuration = read_jsonc_object(path)
        mcp = configuration["mcp"]
        assert isinstance(mcp, dict) and isinstance(mcp["servers"], dict)
        mcp["servers"]["schema"]["url"] = replacement.url
        path.write_text(json.dumps(configuration), encoding="utf-8")
        for _ in range(200):
            reload = (await client.get("/api/v1/config/reload-status")).json()["data"]
            if "/mcp/servers/schema/url" in reload["applied_paths"]:
                assert reload["state"] == "active" and reload["healthy"] and not reload["restart_required"], reload
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("产品未完成 HTTP 连接替换")
        assert (await client.get(f"/api/v1/jobs/{job}/approvals")).json()["data"] == approvals

        decisions = [{"tool_call_id": calls[0]["id"], "type": first_decision},
                     {"tool_call_id": calls[1]["id"], "type": "reject", "message": "第二个调用不要执行"}]
        if first_decision == "edit":
            decisions[0]["edited_action"] = {"name": "mcp__schema__probe", "args": {"proof": "edited-by-user"}}
        elif first_decision == "respond":
            decisions[0]["message"] = "用户提供结果，不发送远端请求"
        payload = {"command_id": "approval-product-command", "checkpoint_id": approval["checkpoint_id"],
                   "responses": [{"approval_id": approval["approval_id"], "decisions": decisions}]}
        stale = await client.post(f"/api/v1/jobs/{job}/approvals", json={**payload, "checkpoint_id": "stale-checkpoint"})
        assert stale.status_code == 409, stale.text
        response = await client.post(f"/api/v1/jobs/{job}/approvals", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "accepted"
        await wait_for_job_done(client, job, max_attempts=120)
        assert (await client.get(f"/api/v1/jobs/{job}/approvals")).json()["data"] == []
        late = await client.post(f"/api/v1/jobs/{job}/approvals", json=payload)
        assert late.status_code == 409, late.text
        receipts = [json.loads(line) for line in original.receipts.read_text().splitlines()] if original.receipts.exists() else []
        expected = [] if first_decision == "respond" else [{"proof": "edited-by-user" if first_decision == "edit" else "approval-1"}]
        assert [receipt["arguments"] for receipt in receipts] == expected
        assert not replacement.receipts.exists()
        records = [json.loads((replay.artifacts / f"{call_id}.json").read_text()) for call_id in planned]
        assert len(records) == 3
        emitted = [message for message in records[-1]["request"]["messages"] if message.get("role") == "assistant" and message.get("tool_calls")]
        assert [json.loads(call["function"]["arguments"])["arguments"] for call in emitted[-1]["tool_calls"]] == [{"proof": "approval-1"}, {"proof": "approval-2"}]
        results = [message for message in records[-1]["request"]["messages"] if message.get("role") == "tool"
                   and message.get("tool_call_id") in {call["id"] for call in calls}]
        assert {message["tool_call_id"] for message in results} == {call["id"] for call in calls}
        assert any("第二个调用不要执行" in str(message["content"]) for message in results)
        with RolloutCheckpointSaver(case.mount.workspace_root / ".boxteam/sessions") as saver:
            for record in records:
                with saver._storage.storage.read_connection(session, thread) as connection:
                    assembly_id, = connection.execute("SELECT assembly_id FROM context_assemblies WHERE model_call_id=?", (record["identity"]["model_call_id"],)).fetchone()
                assembly = saver.get_context_assembly(session, thread, assembly_id=assembly_id)
                assert normalize_wire_request(record["request"]) == assembly.request_hash_preimage
                assembly.validate_hashes()
        after = json.loads((case.mount.workspace_root / ".boxteam/resources.json").read_text())
        assert {lease["status"] for lease in after["leases"] if lease["holder_session_id"] == session} == {"released"}


@pytest.mark.parametrize("boundary", ["expire", "disconnect", "revoke", "cancel"])
async def test_approval_wait_obeys_current_execution_boundary(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, approval_plan, read_approvals, boundary,
):
    from datetime import UTC, datetime
    from time import monotonic

    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory(f"approval-{boundary}") as original,
        product_case(secure, original, tool_restrictions={"confirmation_required": ["mcp__schema__probe"]},
                     sealed_call_timeout_seconds=60 if boundary == "expire" else None) as (client, replay, case),
    ):
        session = await create_session(client, f"工具审批边界 {boundary}")
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        planned, calls = approval_plan(replay, session, thread)
        response = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "发出两个 probe 并等待我的逐项审批。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        job = response.json()["data"]["job_id"]
        approvals = await read_approvals(client, job)
        assert (await client.get("/api/v1/jobs/tool-approvals", params={"session_id": session, "thread_id": thread})).json()["data"] == approvals
        approval = approvals[0]
        assert len(approval["actions"]) == 2
        ledger_path = case.mount.workspace_root / ".boxteam/resources.json"
        leases = json.loads(ledger_path.read_text())["leases"]
        original_qualification = [lease for lease in leases if lease["holder_session_id"] == session
                                  and lease["lease_id"].startswith("mcp-prepared:") and lease["status"] == "active"]
        assert len(original_qualification) == 1
        if boundary == "expire":
            deadline = datetime.fromisoformat(original_qualification[0]["expires_at"])
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            assert 0 < remaining <= 60
            started = monotonic()
            for _ in range(400):
                leases = json.loads(ledger_path.read_text())["leases"]
                original_lease = next(lease for lease in leases if lease["lease_id"] == original_qualification[0]["lease_id"])
                if original_lease["status"] == "released":
                    assert datetime.now(UTC) >= deadline
                    break
                await asyncio.sleep(0.2)
            else:
                raise TimeoutError("原审批资格未按真实期限释放")
            (case.context.artifacts_dir / "approval-expiry.json").write_text(json.dumps({
                "original_deadline": deadline.isoformat(), "remaining_at_approval": remaining,
                "observed_wait_seconds": monotonic() - started, "released": original_lease,
            }, ensure_ascii=False))
        elif boundary == "disconnect":
            disconnected = await client.post("/api/v1/mcp/connection-operations", json={
                "action": "disconnect", "server_id": "schema", "operation_id": "approval-explicit-disconnect",
            })
            assert disconnected.status_code == 200, disconnected.text
            operation = disconnected.json()["data"]
            for _ in range(200):
                if operation["status"] == "succeeded":
                    break
                operation = (await client.get(f"/api/v1/mcp/connection-operations/{operation['operation_id']}")).json()["data"]
                await asyncio.sleep(0.1)
            assert operation["status"] == "succeeded", operation
        elif boundary == "revoke":
            revoked = await client.patch("/api/v1/tools/selection", json={"agent_id": "default", "changes": [{
                "tool_id": "mcp__schema__probe", "execution_enabled": False, "model_visible": False,
            }]})
            assert revoked.status_code == 200, revoked.text
        else:
            cancelled = await client.post(f"/api/v1/jobs/{job}/control", json={"action": "cancel"})
            assert cancelled.status_code == 200, cancelled.text
            state = (await client.get(f"/api/v1/jobs/{job}")).json()["data"]
            for _ in range(200):
                state = (await client.get(f"/api/v1/jobs/{job}")).json()["data"]
                if state["status"] == "cancelled":
                    break
                await asyncio.sleep(0.1)
            assert state["status"] == "cancelled", state

        payload = {"command_id": f"approval-{boundary}", "checkpoint_id": approval["checkpoint_id"], "responses": [{
            "approval_id": approval["approval_id"], "decisions": [
                {"tool_call_id": calls[0]["id"], "type": "approve"},
                {"tool_call_id": calls[1]["id"], "type": "reject", "message": "不执行第二项"},
            ],
        }]}
        submitted = await client.post(f"/api/v1/jobs/{job}/approvals", json=payload)
        if boundary == "cancel":
            assert submitted.status_code == 409, submitted.text
        else:
            assert submitted.status_code == 200, submitted.text
            await wait_for_job_done(client, job, max_attempts=120)
            records = [json.loads((replay.artifacts / f"{identity}.json").read_text()) for identity in planned]
            assert len(records) == 3
            results = {message["tool_call_id"]: message for message in records[-1]["request"]["messages"]
                       if message.get("role") == "tool" and message.get("tool_call_id") in {call["id"] for call in calls}}
            assert set(results) == {call["id"] for call in calls}
            assert "不执行第二项" in str(results[calls[1]["id"]]["content"])
            assert any(word in str(results[calls[0]["id"]]["content"]) for word in {
                "expire": ["已到期"], "disconnect": ["不可用", "未就绪"], "revoke": ["禁用"],
            }[boundary])
            with RolloutCheckpointSaver(case.mount.workspace_root / ".boxteam/sessions") as saver:
                for record in records:
                    with saver._storage.storage.read_connection(session, thread) as connection:
                        assembly_id, = connection.execute("SELECT assembly_id FROM context_assemblies WHERE model_call_id=?", (record["identity"]["model_call_id"],)).fetchone()
                    assembly = saver.get_context_assembly(session, thread, assembly_id=assembly_id)
                    assert assembly.request_hash_preimage == normalize_wire_request(record["request"])
                    assembly.validate_hashes()
        assert not original.receipts.exists()
        assert (await client.get(f"/api/v1/jobs/{job}/approvals")).json()["data"] == []
        leases = json.loads(ledger_path.read_text())["leases"]
        assert {lease["status"] for lease in leases if lease["holder_session_id"] == session} == {"released"}
