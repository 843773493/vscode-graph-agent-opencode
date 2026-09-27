"""通用字节哈希原语。

裸 sha256 十六进制摘要被 rollout v2 持久化、Node Debug source capture 与
workspace 内容修订等多条链路共享；此处单点承载，避免同一实现散落多处后漂移。
"""

from __future__ import annotations

import hashlib

__all__ = ["sha256_hex"]


def sha256_hex(value: bytes) -> str:
    """返回字节内容的裸 sha256 摘要（64 位小写十六进制）。"""
    return hashlib.sha256(value).hexdigest()
