"""测试专用 canonical UUIDv7 工厂；固定 ID 仅从这里生成。"""

from __future__ import annotations

import hashlib
from datetime import datetime

from app.core.identifier import create_uuid_hex_at, to_epoch_ms

__all__ = [
    "session_id_at",
    "session_id_for_name_at",
    "thread_id_at",
    "thread_id_for_name_at",
    "uuid7_hex_at",
    "uuid7_hex_from_name",
    "uuid7_hex_from_name_at",
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
    return _uuid7_hex_at_ms(digest, ms)


def uuid7_hex_from_name_at(name: str, moment: datetime) -> str:
    """按名称生成稳定 ID，并让其 UUIDv7 时间戳与固定时刻一致。"""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return _uuid7_hex_at_ms(digest, to_epoch_ms(moment))


def session_id_for_name_at(name: str, moment: datetime) -> str:
    """按名称和固定时刻生成稳定的 ``ses_`` fixture ID。"""
    return f"ses_{uuid7_hex_from_name_at(name, moment)}"


def thread_id_for_name_at(name: str, moment: datetime) -> str:
    """按名称和固定时刻生成稳定的 ``thr_`` fixture ID。"""
    return f"thr_{uuid7_hex_from_name_at(name, moment)}"


def _uuid7_hex_at_ms(digest: str, epoch_ms: int) -> str:
    chars = list(f"{epoch_ms:012x}" + digest[8:28])
    chars[12] = "7"
    chars[16] = "8"
    return "".join(chars)
