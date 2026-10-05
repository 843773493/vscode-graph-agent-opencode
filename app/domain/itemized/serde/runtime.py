"""SessionThread execution relation 的严格 JSON 对象编解码。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from app.domain.itemized.runtime import (
    ExecutionRecord,
    ModelCallRecord,
    TurnExecutionLink,
)


def _record_object(value: object, record_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{record_name} 必须是 object")
    return dict(value)


def execution_record_to_dict(value: ExecutionRecord) -> dict[str, object]:
    return asdict(value)


def execution_record_from_dict(value: object) -> ExecutionRecord:
    return ExecutionRecord(**_record_object(value, "ExecutionRecord"))


def model_call_record_to_dict(value: ModelCallRecord) -> dict[str, object]:
    return asdict(value)


def model_call_record_from_dict(value: object) -> ModelCallRecord:
    return ModelCallRecord(**_record_object(value, "ModelCallRecord"))


def turn_execution_link_to_dict(value: TurnExecutionLink) -> dict[str, object]:
    return asdict(value)


def turn_execution_link_from_dict(value: object) -> TurnExecutionLink:
    return TurnExecutionLink(**_record_object(value, "TurnExecutionLink"))


__all__ = [
    "execution_record_from_dict",
    "execution_record_to_dict",
    "model_call_record_from_dict",
    "model_call_record_to_dict",
    "turn_execution_link_from_dict",
    "turn_execution_link_to_dict",
]
