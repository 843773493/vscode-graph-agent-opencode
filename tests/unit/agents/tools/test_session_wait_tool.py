"""wait 工具把精确 invocation、selector 和有界预算交给唯一观察 owner。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.tools.session_wait import create_wait_for_session_tool
from tests.support.communication import SOURCE_SESSION, SOURCE_THREAD, TARGET_SESSION


@pytest.fixture
def wait_tool(communication_probe, communication_invocation):
    communication_probe.wait_result = {"status": "timed_out", "observed": [{"selector_kind": "communication", "selector_id": "comm_pending", "state": "pending"}]}
    return create_wait_for_session_tool(SOURCE_SESSION, communication_service=communication_probe, invocation_context=communication_invocation)


@pytest.mark.parametrize("selector", ["job", "turn", "communication"])
async def test_wait_forwards_exact_selector_and_original_call(wait_tool, communication_probe, selector):
    result = await wait_tool.ainvoke({"target_session_id": TARGET_SESSION, selector + "_id": "selector_1"})
    assert result is communication_probe.wait_result
    call = communication_probe.calls[0][1]
    assert call["session_id"] == SOURCE_SESSION
    assert call["thread_id"] == SOURCE_THREAD
    assert call["selector"].kind == selector
    assert call["selector"].selector_id == "selector_1"
    assert call["timeout_seconds"] == 60
    assert call["source_call_id"].startswith("cop_")
    assert call["source_call_created_at"].isoformat() == "2026-02-07T17:01:46.480000+00:00"


async def test_selectorless_wait_keeps_owner_scope(wait_tool, communication_probe):
    await wait_tool.ainvoke({"target_session_id": TARGET_SESSION, "until": "state_change"})
    assert communication_probe.calls[0][1]["selector"] is None
    assert communication_probe.calls[0][1]["until"] == "state_change"


async def test_multiple_selectors_fail_before_owner(wait_tool, communication_probe):
    with pytest.raises(ValueError, match="至多传一个"):
        await wait_tool.ainvoke({"target_session_id": TARGET_SESSION, "job_id": "job_1", "communication_id": "comm_1"})
    assert communication_probe.calls == []


@pytest.mark.parametrize("timeout", [0, 301])
async def test_wait_rejects_timeout_outside_contract(wait_tool, communication_probe, timeout):
    with pytest.raises(ValidationError):
        await wait_tool.ainvoke({"target_session_id": TARGET_SESSION, "timeout_seconds": timeout})
    assert communication_probe.calls == []


def test_wait_schema_hides_software_call_and_bounds_budget(wait_tool):
    properties = wait_tool.args_schema.model_json_schema()["properties"]
    assert "source_call_id" not in properties
    assert properties["timeout_seconds"]["default"] == 60
    assert properties["timeout_seconds"]["minimum"] == 1
    assert properties["timeout_seconds"]["maximum"] == 300
