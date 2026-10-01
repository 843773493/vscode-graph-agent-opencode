"""``KeyLockPool`` 固定分片锁池的有界性、稳定性与互斥性。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.key_lock_pool import KeyLockPool


def test_lock_table_stays_bounded_across_many_keys() -> None:
    """锁对象数量必须有固定上界，不随出现过的 key 数增长。"""
    pool = KeyLockPool()
    for index in range(5000):
        pool.lock_for("key-" + str(index))
    assert len(pool) <= 64


def test_lock_is_stable_per_key() -> None:
    """同一 key 恒得同一把锁。"""
    pool = KeyLockPool()
    assert pool.lock_for("shared") is pool.lock_for("shared")


def test_rejects_empty_key() -> None:
    """空 key 快速失败，不得静默落到固定槽位。"""
    pool = KeyLockPool()
    with pytest.raises(ValueError, match="key"):
        pool.lock_for("")


def test_rejects_non_positive_shards() -> None:
    """非法分片数快速失败。"""
    with pytest.raises(ValueError, match="分片数"):
        KeyLockPool(shards=0)


@pytest.mark.asyncio
async def test_same_key_critical_section_is_serialized() -> None:
    """同 key 并发临界区严格互斥（峰值恒为 1）。"""
    pool = KeyLockPool()
    lock = pool.lock_for("shared")
    concurrent = 0
    peak = 0

    async def worker() -> None:
        nonlocal concurrent, peak
        async with lock:
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 1
