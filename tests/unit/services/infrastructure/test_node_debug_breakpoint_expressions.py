from __future__ import annotations

import pytest

from app.services.infrastructure.node_debug_breakpoint_expressions import (
    inspector_breakpoint_condition,
    parse_logpoint_output,
)


def test_plain_condition_is_passed_through() -> None:
    assert (
        inspector_breakpoint_condition(
            breakpoint_id="node-bp-test",
            condition=" value > 2 ",
            hit_condition=None,
            log_message=None,
        )
        == "value > 2"
    )


def test_hit_condition_and_logpoint_compile_to_non_pausing_expression() -> None:
    expression = inspector_breakpoint_condition(
        breakpoint_id="node-bp-test",
        condition="index > 0",
        hit_condition=3,
        log_message="index={index}, object={JSON.stringify({ value: index })}",
    )

    assert expression is not None
    assert "hit === 3" in expression
    assert "Boolean((index > 0))" in expression
    assert "process.stdout.write" in expression
    assert "String((index))" in expression
    assert "return false" in expression


@pytest.mark.parametrize(
    "message",
    ["value={", "value=}", "value={   }"],
)
def test_invalid_logpoint_interpolation_is_rejected(message: str) -> None:
    with pytest.raises(ValueError, match="日志点"):
        inspector_breakpoint_condition(
            breakpoint_id="node-bp-test",
            condition=None,
            hit_condition=None,
            log_message=message,
        )


def test_logpoint_output_prefix_is_removed() -> None:
    assert parse_logpoint_output("__BOXTEAM_NODE_LOGPOINT__value=23") == "value=23"
    assert parse_logpoint_output("ordinary output") is None
