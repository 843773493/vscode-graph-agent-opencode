"""进程内 per-key 串行锁的中立实现：固定分片锁池。

同一 key 恒映射到同一把 ``asyncio.Lock``（保住同 key 临界区互斥），锁对象
数量恒为分片数（不随历史 key 数无界增长）。分片用 ``crc32`` 而非 ``hash()``
以保证跨进程确定，不受哈希随机化影响。

用于会话创建、child thread 创建与子树删除等长驻服务：它们的幂等键
（``session-<uuid4>``、``delegation_id``、删除 ``idempotency_key``）都是逐次
操作的唯一值，无界字典锁表会随历史操作数单调增长。
"""

from __future__ import annotations

import asyncio
import zlib
from typing import Final

# 固定分片数：锁对象数量恒为此值，与历史 key 数无关。
KEY_LOCK_SHARDS: Final[int] = 64


class KeyLockPool:
    """固定分片锁池：同一 key 恒得同一把锁，数量恒为 ``KEY_LOCK_SHARDS``。"""

    __slots__ = ("_shards",)

    def __init__(self, shards: int = KEY_LOCK_SHARDS) -> None:
        if not isinstance(shards, int) or shards < 1:
            raise ValueError(f"分片数必须是正整数: {shards!r}")
        self._shards = tuple(asyncio.Lock() for _ in range(shards))

    def lock_for(self, key: str) -> asyncio.Lock:
        """返回该 key 恒定命中的锁；锁池数量不随出现过的 key 数增长。"""
        if not isinstance(key, str) or not key:
            raise ValueError(f"key 必须是非空字符串: {key!r}")
        shard = zlib.crc32(key.encode("utf-8")) % len(self._shards)
        return self._shards[shard]

    def __len__(self) -> int:
        return len(self._shards)
