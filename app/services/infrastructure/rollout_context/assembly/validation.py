"""sealed assembly 的 SQLite manifest 严格解析边界。"""

from __future__ import annotations

import json
from collections.abc import Sequence


def required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"assembly manifest {field} 必须是非空字符串")
    return value


def optional_text(value: object, *, field: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise RuntimeError(f"assembly manifest {field} 必须是非空字符串或 NULL")
    return value


def non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"assembly manifest {field} 必须是非负整数")
    return value


def sqlite_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value not in {0, 1}:
        raise RuntimeError(f"assembly manifest {field} 必须是 SQLite 0/1")
    return value == 1


def one_of_text(value: object, allowed: set[str], *, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise RuntimeError(f"assembly manifest {field} 值非法: {value!r}")
    return value


def json_text(value: object, *, field: str) -> object:
    if not isinstance(value, str):
        raise TypeError(f"assembly manifest {field} 必须是 JSON 文本")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"assembly manifest {field} JSON 非法") from error


def _exact_value(actual: object, expected: object, *, field: str) -> None:
    if isinstance(expected, (tuple, list)):
        if type(actual) is not type(expected) or len(actual) != len(expected):
            raise RuntimeError(f"{field} 字段数量或容器类型不一致")
        for index, (actual_value, expected_value) in enumerate(
            zip(actual, expected, strict=True)
        ):
            _exact_value(actual_value, expected_value, field=f"{field}[{index}]")
        return
    if expected is None:
        valid = actual is None
    else:
        valid = type(actual) is type(expected) and actual == expected
    if not valid:
        raise RuntimeError(
            f"{field} 与 sealed snapshot 不一致: "
            f"expected={expected!r}, actual={actual!r}"
        )


def exact_row(
    actual: Sequence[object],
    expected: Sequence[object],
    *,
    field: str,
) -> None:
    """按类型和值同时比较 SQLite row，禁止 ``True == 1`` 穿透。"""
    _exact_value(actual, expected, field=field)




__all__ = [
    "exact_row",
    "json_text",
    "non_negative_int",
    "one_of_text",
    "optional_text",
    "required_text",
    "sqlite_bool",
]
