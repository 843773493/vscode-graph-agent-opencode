"""Gateway 受认证快照的内置适配器。

locator 集合在构造时固定，本进程只能读取登记过的精确 locator；认证、
版本 token 与正文获取由注入的 GatewaySnapshotReader（实际 owner）负责。
适配器不缓存正文、不提供注册 API，也不把 locator 暴露给模型协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AuthenticatedGatewaySnapshot:
    """Gateway 已认证返回的不可变快照；version token 由 Gateway 签发。"""

    locator: str
    version_token: str
    content: bytes


class GatewaySnapshotReader(Protocol):
    """实际 owner 的认证读取端口；实现方负责认证与完整性校验。"""

    async def read_snapshot(self, locator: str) -> AuthenticatedGatewaySnapshot: ...


class GatewaySnapshotAdapter:
    """固定 locator 集合的 Gateway 快照适配面。"""

    def __init__(
        self,
        *,
        reader: GatewaySnapshotReader,
        locators: tuple[str, ...],
    ) -> None:
        if not locators:
            raise ValueError("GatewaySnapshotAdapter 至少需要一个固定 locator")
        for locator in locators:
            if not isinstance(locator, str) or not locator.strip():
                raise ValueError(f"Gateway 快照 locator 必须是非空字符串: {locator!r}")
        if len(set(locators)) != len(locators):
            raise ValueError(f"Gateway 快照 locator 重复: {locators}")
        self._reader = reader
        self._locators = frozenset(locators)

    @property
    def locators(self) -> frozenset[str]:
        return self._locators

    async def snapshot(self, locator: str) -> AuthenticatedGatewaySnapshot:
        """读取一个已登记 locator 的受认证快照；越界与空 token 显式失败。"""
        if locator not in self._locators:
            raise KeyError(f"Gateway 快照 locator 未在固定装配中登记: {locator}")
        snapshot = await self._reader.read_snapshot(locator)
        if snapshot.locator != locator:
            raise RuntimeError(
                f"Gateway 快照 locator 不一致: requested={locator} returned={snapshot.locator}"
            )
        if not snapshot.version_token:
            raise RuntimeError(f"Gateway 快照缺少 version token: locator={locator}")
        return snapshot


__all__ = [
    "AuthenticatedGatewaySnapshot",
    "GatewaySnapshotAdapter",
    "GatewaySnapshotReader",
]
