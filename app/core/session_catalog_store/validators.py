"""canonical 标识符 / locator / 路径预算验证器与生命周期栅栏原语。

承载 ``validate_session_id``/``validate_thread_id`` 的唯一形态校验（含
UUIDv7 位 profile）、``validate_storage_relative_locator`` 的分桶与 id 内嵌
日期一致性断言、``uuid7_embedded_utc_date`` 的唯一解码口径、
``validate_path_budget`` 的落盘预算校验，以及 ``SessionLifecycleFence`` 的
state + generation CAS 原语。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.core.session_catalog_store._schema import (
    _MAX_PATH_COMPONENT_BYTES,
    _MAX_PATH_TOTAL_BYTES,
    _SESSION_ID_PATTERN,
    _STORAGE_LOCATOR_PATTERN,
    _THREAD_ID_PATTERN,
    _UUID_VARIANT_HEX_CHARS,
    _UUID_VARIANT_HEX_INDEX,
    _UUID_VERSION_HEX_INDEX,
)


def validate_session_id(value: str) -> None:
    """校验 canonical session_id：``ses_`` 前缀 + 32 位小写 hex + UUIDv7 位 profile。

    斜杠、反斜杠、Unicode、``.``/``..``、大小写错误、前缀错误、长度错误及
    非 v7 位 profile 一律直接拒绝，不得清洗或截断。
    """
    if not isinstance(value, str):
        raise TypeError(f"session_id 必须是字符串: {value!r}")
    if _SESSION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"session_id 形态非法: {value!r}")
    _validate_uuid_payload(value[4:])


def validate_thread_id(value: str) -> None:
    """校验 canonical thread_id：``thr_`` 前缀 + 32 位小写 hex + UUIDv7 位 profile。"""
    if not isinstance(value, str):
        raise TypeError(f"thread_id 必须是字符串: {value!r}")
    if _THREAD_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"thread_id 形态非法: {value!r}")
    _validate_uuid_payload(value[4:])


def _validate_uuid_payload(payload: str) -> None:
    """校验 32 位 hex payload 的 UUIDv7 version/variant 位。"""
    if payload[_UUID_VERSION_HEX_INDEX] != "7":
        raise ValueError(f"ID payload 的 UUID version 位非法: {payload!r}")
    if payload[_UUID_VARIANT_HEX_INDEX] not in _UUID_VARIANT_HEX_CHARS:
        raise ValueError(f"ID payload 的 UUID variant 位非法: {payload!r}")


def validate_storage_relative_locator(value: str) -> None:
    """校验 storage locator：``sessions/YYYY/MM/DD/{session_id}`` 且日期真实存在。

    YYYY 为 4 位数字，MM 必须在 01-12，DD 按 ``calendar.monthrange`` 对应
    月份合法（含闰年）；叶名 session_id 必须过完整 session 验证器。

    日期段还必须与 session_id 内嵌的 48 bit Unix 毫秒时间戳按 UTC 推导出的
    日期逐段一致（design D4/§4.1）：本断言只凭 locator 字符串自身（id 内嵌
    时间 + 日期段）推出，MUST NOT 依赖另存的 ``created_at`` 或两次独立取时；
    不一致即 fail-closed，绝不扫盘修正或悄悄改桶。
    """
    if not isinstance(value, str):
        raise TypeError(f"storage_relative_locator 必须是字符串: {value!r}")
    match = _STORAGE_LOCATOR_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"storage_relative_locator 形态非法: {value!r}")
    year_text, month_text, day_text, session_id = match.groups()
    validate_session_id(session_id)
    month = int(month_text)
    if not 1 <= month <= 12:
        raise ValueError(f"storage_relative_locator 月份非法: {value!r}")
    day = int(day_text)
    _, last_day = calendar.monthrange(int(year_text), month)
    if not 1 <= day <= last_day:
        raise ValueError(f"storage_relative_locator 日期非法: {value!r}")
    locator_date = date(int(year_text), month, day)
    embedded_date = uuid7_embedded_utc_date(session_id[4:])
    if locator_date != embedded_date:
        raise ValueError(
            "storage_relative_locator 日期段必须等于 session_id 内嵌 48 bit "
            "毫秒时间戳的 UTC 日期（分桶与 id 漂移，fail-closed）: "
            f"locator={value!r}, locator_date={locator_date.isoformat()}, "
            f"embedded_date={embedded_date.isoformat()}"
        )


def uuid7_embedded_utc_date(payload: str) -> date:
    """从 32 位 v7 hex payload 的前 48 bit 解出内嵌 Unix 毫秒并按 UTC 求日期。

    RFC 9562 v7 的前 48 bit 即 big-endian Unix 毫秒时间戳，等于 payload
    前 12 个 hex；本函数是分桶日期与 id 内嵌时间的唯一解码口径。
    """
    embedded_ms = int(payload[:12], 16)
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=embedded_ms)).date()


def validate_path_budget(base: Path, locator: str) -> None:
    """校验解析后绝对路径的组件与总长预算。

    每个路径组件不超过 255 bytes，完整路径不超过 4096 bytes；超限直接
    拒绝，不截断、不改写叶名或以 path hash 替代真实叶名。
    """
    target = (base / locator).resolve()
    for part in target.parts:
        component_bytes = len(part.encode("utf-8"))
        if component_bytes > _MAX_PATH_COMPONENT_BYTES:
            raise ValueError(
                f"路径组件超出预算: {part!r} "
                f"({component_bytes} bytes > {_MAX_PATH_COMPONENT_BYTES})"
            )
    total_bytes = len(str(target).encode("utf-8"))
    if total_bytes > _MAX_PATH_TOTAL_BYTES:
        raise ValueError(
            f"路径总长超出预算: {target} "
            f"({total_bytes} bytes > {_MAX_PATH_TOTAL_BYTES})"
        )


class SessionLifecycleFence:
    """单 session 生命周期栅栏：state + generation 的 CAS 原语。

    ``active → deleting`` 是唯一合法转移，成功时 generation+1；
    ``deleting → active`` 恒拒绝（不可复活）。
    """

    def __init__(self, state: str = "active", generation: int = 0) -> None:
        if state not in ("active", "deleting"):
            raise ValueError(f"fence 初始状态非法: {state!r}")
        self.state = state
        self.generation = generation

    def cas_transition(self, expected_generation: int, new_state: str) -> bool:
        """generation 匹配且转移合法时推进状态并返回 True，否则返回 False。"""
        if new_state not in ("active", "deleting"):
            raise ValueError(f"fence 目标状态非法: {new_state!r}")
        if self.state != "active" or new_state != "deleting":
            return False
        if self.generation != expected_generation:
            return False
        self.generation += 1
        self.state = new_state
        return True
