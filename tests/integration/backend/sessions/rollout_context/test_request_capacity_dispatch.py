"""真实 Bridge/Saver/SDK 回放：容量拒绝发生在 seal 与网络之前。"""

from __future__ import annotations

import pytest

from app.core.failures import is_provider_failure
from app.domain.context_compaction import ContextCapacityError
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    native_dispatch_workspace as native_dispatch_workspace,  # noqa: PLC0414 - 原装具显式注入
)
from tests.integration.backend.sessions.test_itemized_dispatch_contract import (
    dispatch_bridge as dispatch_bridge,  # noqa: PLC0414 - 原装具显式注入
)
from tests.integration.backend.sessions.test_itemized_dispatch_contract import (
    dispatch_saver as dispatch_saver,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_itemized_dispatch_contract import (
    toolset_dispatch_case as toolset_dispatch_case,  # noqa: PLC0414
)


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
async def test_real_dispatch_rejects_model_capacity_before_seal_and_retains_pending_sources(
    toolset_dispatch_case, target_format,
):
    case = toolset_dispatch_case
    case.model.profile = {"context_window": 1}
    pending = case.manager.prepare_pending()
    with pytest.raises(ContextCapacityError) as rejected:
        await case.dispatch("reject-small-capacity")
    assert not is_provider_failure(rejected.value)
    assert rejected.value.budget.capacity.context_window == 1
    assert case.controller.transport.call_count == 0
    assert case.dispatches == []
    assert case.saver.list_context_assemblies(case.session, case.thread) == ()
    assert case.manager.prepare_pending() == pending
    with case.saver._storage.storage.read_connection(case.session, case.thread, "") as connection:
        outcomes = connection.execute("SELECT error_code FROM context_plan_seal_failures").fetchall()
    assert outcomes == [("context-capacity-exceeded",)]


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
async def test_real_dispatch_unknown_capacity_and_known_room_keep_actual_wire_and_cold_retry(
    toolset_dispatch_case, target_format,
):
    case = toolset_dispatch_case
    case.model.profile = None
    first = await case.dispatch("unknown-capacity")
    case.model.profile = {"max_input_tokens": 200000, "context_window": 128000}
    second = await case.dispatch("known-capacity")
    assert first.assembly_id != second.assembly_id
    retry = await case.dispatch("known-capacity")
    assert retry.assembly_id == second.assembly_id
    for snapshot in (first, second, retry):
        snapshot.validate_hashes()
        assert snapshot.request_hash_preimage == case.request(snapshot)
        assert not {"max_tokens", "max_output_tokens"} & snapshot.request_hash_preimage.keys()
