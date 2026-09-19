"""权威内存状态的内置适配器。

memory key 集合在构造时固定；读取与 version token 签发由注入的
MemoryStateReader（实际 domain owner）负责。适配器不缓存 payload、
不提供注册 API，也不把内部 key 暴露给模型协议。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AuthoritativeMemorySnapshot:
    """权威内存状态的不可变快照；version token 由 owner 递进。"""

    key: str
    version_token: str
    payload: Mapping[str, object]


class MemoryStateReader(Protocol):
    """实际 domain owner 的状态读取端口。"""

    async def read_state(self, key: str) -> AuthoritativeMemorySnapshot: ...


class MemoryStateAdapter:
    """固定 key 集合的权威内存状态适配面。"""

    def __init__(
        self,
        *,
        reader: MemoryStateReader,
        keys: tuple[str, ...],
    ) -> None:
        if not keys:
            raise ValueError("MemoryStateAdapter 至少需要一个固定 memory key")
        for key in keys:
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"内存状态 key 必须是非空字符串: {key!r}")
        if len(set(keys)) != len(keys):
            raise ValueError(f"内存状态 key 重复: {keys}")
        self._reader = reader
        self._keys = frozenset(keys)

    @property
    def keys(self) -> frozenset[str]:
        return self._keys

    async def state(self, key: str) -> AuthoritativeMemorySnapshot:
        """读取一个已登记 key 的权威状态；越界与空 token 显式失败。"""
        if key not in self._keys:
            raise KeyError(f"内存状态 key 未在固定装配中登记: {key}")
        snapshot = await self._reader.read_state(key)
        if snapshot.key != key:
            raise RuntimeError(
                f"内存状态 key 不一致: requested={key} returned={snapshot.key}"
            )
        if not snapshot.version_token:
            raise RuntimeError(f"内存状态缺少 version token: key={key}")
        return snapshot


__all__ = [
    "AuthoritativeMemorySnapshot",
    "MemoryStateAdapter",
    "MemoryStateReader",
]
