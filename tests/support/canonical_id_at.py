"""按指定 UTC 时刻生成 canonical v7 身份的测试工厂（单一入口）。

产品创建路径用 ``app.core.identifier.effective_now_ms`` 这一 D4 唯一时间源同时
推出 id 内嵌 48 bit 毫秒与 ``sessions/YYYY/MM/DD`` 分桶日期。测试若把创建时刻
固定在某个历史日期（例如 ``datetime(2026, 6, 1, 12, 0, tzinfo=UTC)``），就必须
用「同一时刻」生成 v7 id，否则分桶与 id 内嵌时间漂移，会撞上 §4.1/§4.3 的
fail-closed 完整性断言。本模块是这种「固定时刻 canonical id」的唯一入口，避免
各测试文件各写一套 uuid 拼装（AGENTS.md：彻底根除双轨）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from app.core.identifier import create_uuid_hex_at, to_epoch_ms

__all__ = [
    "session_id_at",
    "thread_id_at",
    "uuid7_hex_at",
    "uuid7_hex_from_name",
]

def uuid7_hex_at(moment: datetime) -> str:
    """按给定时刻（须带时区，取其 UTC 毫秒）生成 32 位 v7 hex payload。"""
    return create_uuid_hex_at(to_epoch_ms(moment))


def session_id_at(moment: datetime) -> str:
    """按给定时刻生成 ``ses_`` canonical session_id。"""
    return f"ses_{uuid7_hex_at(moment)}"


def thread_id_at(moment: datetime) -> str:
    """按给定时刻生成 ``thr_`` canonical thread_id。"""
    return f"thr_{uuid7_hex_at(moment)}"


def uuid7_hex_from_name(name: str) -> str:
    """由任意名称确定性派生一个合法 v7 位 profile 的 32 位 hex payload。

    内嵌时间戳固定落在 2026 年（可解析、分桶自洽），供需要「同一名称在同一
    测试会话内稳定得到同一 id」的夹具使用；version/variant 位固定为合法 v7。
    """
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    # 2026-01-01T00:00:00Z 的 Unix 毫秒，加 32 位派生偏移，锁进 2026 年内。
    base_ms = 1767225600000
    span_ms = 31_536_000_000  # 365 天
    ms = base_ms + int(digest[:8], 16) % span_ms
    chars = list(f"{ms:012x}" + digest[8:28])
    chars[12] = "7"
    chars[16] = "8"
    return "".join(chars)
