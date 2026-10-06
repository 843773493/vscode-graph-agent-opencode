from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agents import itemized_context_middleware
from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)


@pytest.fixture
def bridge(registered_tool_set_producer_factory):
    return SealedAssemblyDispatchBridge(
        checkpointer=object(), session_id="session-unit", thread_id="thread-unit",
        context_source_manager=ContextSourceManager(),
        tool_set_producer=registered_tool_set_producer_factory(),
    )


@pytest.mark.parametrize("prepared", [
    {"messages": "not-a-list", "tools": []},
    {"messages": [], "tools": "not-a-list"},
])
def test_prepare_projection_rejects_illegal_structure(bridge, prepared):
    bridge._prepare = MagicMock(return_value=prepared)
    with pytest.raises(TypeError, match="itemized provider projection 返回结构非法"):
        bridge._prepare_projection(object())


def test_prepare_projection_records_capability_loss(bridge, monkeypatch):
    bridge._prepare = MagicMock(return_value={"messages": [], "tools": [], "losses": ["image"], "include_structured_output": False})
    monkeypatch.setattr(itemized_context_middleware, "_runtime_thread_id", lambda request: "thread-unit")
    monkeypatch.setattr(itemized_context_middleware, "require_current_execution_admission", lambda **_: SimpleNamespace(job_id="job-unit"))
    warning = MagicMock()
    monkeypatch.setattr(itemized_context_middleware.logger, "warning", warning)
    bridge._prepare_projection(object())
    loss_calls = [call for call in warning.call_args_list if call.args[0].startswith("itemized provider projection capability loss")]
    assert len(loss_calls) == 1
    assert loss_calls[0].args[1:] == ("session-unit", "job-unit", ["image"])


def test_prepare_projection_without_losses_skips_loss_identity(bridge, monkeypatch):
    bridge._prepare = MagicMock(return_value={"messages": [], "tools": [], "losses": (), "include_structured_output": False})

    def fail(**kwargs):
        raise AssertionError("losses 为空时不得解析 capability loss identity")

    monkeypatch.setattr(itemized_context_middleware, "require_current_execution_admission", fail)
    warning = MagicMock()
    monkeypatch.setattr(itemized_context_middleware.logger, "warning", warning)
    bridge._prepare_projection(object())
    assert not [call for call in warning.call_args_list if call.args[0].startswith("itemized provider projection capability loss")]
