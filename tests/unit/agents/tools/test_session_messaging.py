"""send schema 和软件 invocation 绑定；ledger/Job 行为由对应 owner 验证。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.model_tool_schema import filter_tool_event_injected_arguments
from app.agents.tools.session_messaging import create_send_message_to_session_tool
from app.schemas.internal_v2.communication import CommunicationReceipt
from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.business.communication.errors import CommunicationContractError
from tests.support.communication.probe import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    TARGET_SESSION,
    TARGET_THREAD,
    WORKSPACE_ID,
)


@pytest.fixture
def send_tool(communication_probe, communication_invocation):
    communication_probe.send_receipt = CommunicationReceipt(
        communication_id="comm_" + "1" * 32,
        source=GlobalThreadAddress("gateway_source", WORKSPACE_ID, SOURCE_SESSION, SOURCE_THREAD),
        target=GlobalThreadAddress("gateway_target", WORKSPACE_ID, TARGET_SESSION, TARGET_THREAD),
        payload_hash="1" * 64, admission_id="admission_" + "2" * 64,
        accepted_at="2026-10-07T00:00:00+00:00",
    )
    return create_send_message_to_session_tool(SOURCE_SESSION, communication_service=communication_probe, invocation_context=communication_invocation)


async def test_send_returns_durable_acceptance_and_software_identity(send_tool, communication_probe):
    result = await send_tool.ainvoke({"target_session_id": TARGET_SESSION, "content": "数据"})
    assert result["state"] == "target_accepted"
    assert "job_id" not in result
    operation, call = communication_probe.calls[0]
    assert operation == "send"
    assert call["thread_id"] == SOURCE_THREAD
    assert call["send_operation_id"].startswith("cop_")
    assert call["payload"].content == "数据"
    await send_tool.ainvoke({"target_session_id": TARGET_SESSION, "content": "数据"})
    assert communication_probe.calls[1][1]["send_operation_id"] == call["send_operation_id"]


def test_model_schema_contains_only_business_parameters(send_tool):
    fields = send_tool.args_schema.model_json_schema()["properties"]
    assert set(fields) == {"target_session_id", "target_workspace_id", "content", "kind", "reply_to_communication_id", "delivery_policy"}
    assert send_tool.args_schema.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize("field", ["team_state", "team_task", "goal_state"])
async def test_send_rejects_foreign_business_state_before_transport(send_tool, communication_probe, field):
    arguments = {"target_session_id": TARGET_SESSION, "content": "数据", field: {"status": "completed"}}
    assert filter_tool_event_injected_arguments(send_tool, arguments) == arguments
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        await send_tool.ainvoke(arguments)
    assert communication_probe.calls == []


@pytest.mark.parametrize("policy", ["after_turn", "after_tool_result", "after_interrupt"])
async def test_delivery_policy_reaches_single_communication_owner(send_tool, communication_probe, policy):
    await send_tool.ainvoke({"target_session_id": TARGET_SESSION, "content": "数据", "delivery_policy": policy})
    assert communication_probe.calls[0][1]["payload"].delivery_policy == policy


async def test_question_and_reply_keep_directional_correlation(send_tool, communication_probe):
    result = await send_tool.ainvoke({"target_session_id": TARGET_SESSION, "content": "确认？", "kind": "question"})
    assert result["reply_required"] is True
    await send_tool.ainvoke({"target_session_id": TARGET_SESSION, "content": "确认", "kind": "reply", "reply_to_communication_id": "comm_" + "3" * 32})
    assert communication_probe.calls[-1][1]["payload"].reply_to_communication_id == "comm_" + "3" * 32


async def test_reply_requires_correlation_before_transport(send_tool, communication_probe):
    with pytest.raises(CommunicationContractError, match="reply_to_communication_id"):
        await send_tool.ainvoke({"target_session_id": TARGET_SESSION, "content": "确认", "kind": "reply"})
    assert communication_probe.calls == []


async def test_send_propagates_explicit_owner_failure(send_tool, communication_probe):
    communication_probe.error = RuntimeError("communication-preimage-conflict")
    with pytest.raises(RuntimeError, match="preimage-conflict"):
        await send_tool.ainvoke({"target_session_id": TARGET_SESSION, "content": "冲突"})
