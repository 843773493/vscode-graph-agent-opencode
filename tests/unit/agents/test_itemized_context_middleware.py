from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agents import itemized_context_middleware
from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge


def _bridge() -> SealedAssemblyDispatchBridge:
    return SealedAssemblyDispatchBridge(checkpointer=object())


@pytest.mark.parametrize(
    "prepared",
    [
        {"messages": "not-a-list", "tools": []},
        {"messages": [], "tools": "not-a-list"},
    ],
)
def test_prepare_projection_rejects_illegal_structure(prepared: dict[str, object]) -> None:
    """projection 返回的 messages/tools 非 list 时必须原样抛 TypeError。"""
    bridge = _bridge()
    bridge._prepare = MagicMock(return_value=prepared)  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="itemized provider projection 返回结构非法"):
        bridge._prepare_projection(object())


def test_prepare_projection_records_capability_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    """projection 报告 capability losses 时必须带 session/turn/losses 记录日志。"""
    bridge = _bridge()
    prepared: dict[str, Any] = {"messages": [], "tools": [], "losses": ["image"]}
    bridge._prepare = MagicMock(return_value=prepared)  # type: ignore[method-assign]
    monkeypatch.setattr(itemized_context_middleware, "_runtime_session_id", lambda request: "ses_x")
    monkeypatch.setattr(itemized_context_middleware, "_request_turn_id", lambda request: "turn_y")
    warning = MagicMock()
    monkeypatch.setattr(itemized_context_middleware.logger, "warning", warning)

    bridge._prepare_projection(object())

    loss_calls = [
        call
        for call in warning.call_args_list
        if call.args[0].startswith("itemized provider projection capability loss")
    ]
    assert len(loss_calls) == 1
    assert loss_calls[0].args[1:] == ("ses_x", "turn_y", ["image"])


def test_prepare_projection_without_losses_skips_loss_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """losses 为空时必须短路，不得解析 session/turn，也不得记录 loss 日志。"""
    bridge = _bridge()
    bridge._prepare = MagicMock(  # type: ignore[method-assign]
        return_value={"messages": [], "tools": [], "losses": ()}
    )

    def _fail(request: object) -> str:
        raise AssertionError("losses 为空时不得解析 capability loss identity")

    monkeypatch.setattr(itemized_context_middleware, "_runtime_session_id", _fail)
    monkeypatch.setattr(itemized_context_middleware, "_request_turn_id", _fail)
    warning = MagicMock()
    monkeypatch.setattr(itemized_context_middleware.logger, "warning", warning)

    bridge._prepare_projection(object())

    assert not [
        call
        for call in warning.call_args_list
        if call.args[0].startswith("itemized provider projection capability loss")
    ]
