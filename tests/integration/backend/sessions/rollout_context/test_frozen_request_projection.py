"""预算规划与封存共用实际 Provider 正文投影，不先发布假 assembly。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.agents.providers.request_budget import estimate_wire_input
from app.domain.context_compaction import ContextCapacityError, ModelCapacity
from app.services.infrastructure.rollout_context.checkpoint.owner.context_projection import (
    ProviderContextRequest,
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 复用原真实准备入口
)
from tests.support.rollout.context_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 显式注入 pytest fixture
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_frozen_request_projection_is_available_before_seal_and_repeatable_without_io(
    production_context, monkeypatch: pytest.MonkeyPatch, target_format: str,
) -> None:
    saver, _session, _thread, connection, manager, prepare = production_context
    original = saver.project_frozen_context_request
    requests = []
    pending = manager.prepare_pending()

    def project(context, items, bodies, **wire):
        assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == (0,)
        assert manager.prepare_pending() == pending
        frozen_items, frozen_bodies = deepcopy(items), deepcopy(bodies)
        result = original(context, frozen_items, frozen_bodies, **wire)
        assert isinstance(result, ProviderContextRequest)
        assert frozen_items == items and frozen_bodies == bodies
        budget = result.budget(ModelCapacity(max_input_tokens=200000, context_window=128000))
        assert budget.estimate == estimate_wire_input(result.wire_request)
        assert budget.estimate.input_tokens > 0
        assert budget.capacity.input_budget_tokens == 128000
        # 相同材料重复规划无新的 authority 读取，也不登记或发布 assembly。
        def reject_io(*args, **kwargs):
            raise AssertionError("冻结投影不得重读 canonical 或请求来源")

        with monkeypatch.context() as guard:
            guard.setattr(saver._storage.storage, "read_ref_items", reject_io)
            guard.setattr(saver, "_request_content_for_plan", reject_io)
            guard.setattr(manager, "prepare_sources", reject_io)
            repeated = original(context, frozen_items, frozen_bodies, **wire)
            assert repeated.budget(budget.capacity) == budget
        assert repeated.wire_request == result.wire_request
        assert repeated.projection.messages == result.projection.messages
        assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == (0,)
        requests.append(result)
        return result

    monkeypatch.setattr(saver, "project_frozen_context_request", project)
    prepared = prepare("readonly-frozen-request", target_format=target_format)
    assert len(requests) == 1
    snapshot = prepared["snapshot"]
    assert snapshot.request_hash_preimage == requests[0].wire_request
    assert prepared["messages"] == requests[0].projection.messages
    assert prepared["tools"] == requests[0].projection.tools
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == (1,)
    assert manager.prepare_pending() is None


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_frozen_request_rejection_does_not_seal_or_consume_sources(
    production_context, monkeypatch: pytest.MonkeyPatch, target_format: str,
) -> None:
    saver, _session, _thread, connection, manager, prepare = production_context
    pending = manager.prepare_pending()

    def reject(*args, **kwargs):
        raise ValueError("cannot_fit: 冻结请求预算拒绝")

    monkeypatch.setattr(saver, "project_frozen_context_request", reject)
    with pytest.raises(ValueError, match="cannot_fit"):
        prepare("rejected-frozen-request", target_format=target_format)
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == (0,)
    assert manager.prepare_pending() == pending


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_actual_capacity_rejects_before_publishing_assembly_or_acknowledging_sources(
    production_context, monkeypatch: pytest.MonkeyPatch, target_format: str,
) -> None:
    saver, _session, _thread, connection, manager, prepare = production_context
    original = saver.prepare_context_for_provider
    pending = manager.prepare_pending()

    def limited(*args, **kwargs):
        return original(*args, **kwargs, model_capacity=ModelCapacity(context_window=1))

    monkeypatch.setattr(saver, "prepare_context_for_provider", limited)
    with pytest.raises(ContextCapacityError) as rejected:
        prepare("capacity-rejected", target_format=target_format)
    assert rejected.value.budget.estimate.known_tokens > 1
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == (0,)
    assert manager.prepare_pending() == pending


def test_output_reservation_is_taken_from_final_wire_not_a_stale_metadata_value(
    production_context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, _session, _thread, _connection, _manager, prepare = production_context
    original = saver.project_frozen_context_request

    def project(*args, **kwargs):
        result = original(*args, **kwargs)
        capacity = ModelCapacity(context_window=1000, output_reservation_tokens=900)
        assert result.budget(capacity).capacity.output_reservation_tokens is None
        result.wire_request["max_tokens"] = 200
        budget = result.budget(capacity)
        assert budget.capacity.output_reservation_tokens == 200
        assert budget.capacity.input_budget_tokens == 800
        result.wire_request.pop("max_tokens")
        return result

    monkeypatch.setattr(saver, "project_frozen_context_request", project)
    prepare("final-wire-output-reservation")
