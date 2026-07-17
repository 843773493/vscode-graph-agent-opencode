from __future__ import annotations

import uuid
from typing import Literal


IdentifierPrefix = Literal[
    "attempt",
    "bgm",
    "bgt",
    "evt",
    "job",
    "msg",
    "part",
    "patch",
    "ses",
    "src",
    "sub",
    "tooltest",
]


def create_uuid_hex() -> str:
    """生成保留 UUIDv4 全部位数的 32 位小写十六进制字符串。"""
    return uuid.uuid4().hex


def create_prefixed_id(prefix: IdentifierPrefix) -> str:
    """生成带领域前缀的完整 UUID。"""
    return f"{prefix}_{create_uuid_hex()}"
