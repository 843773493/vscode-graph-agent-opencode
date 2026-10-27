"""代码登记的获取端口；正文与条件缓存只归配置来源 owner，不进入上下文。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class SkillFetchConfig(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    protocol: str


class SkillFetchError(RuntimeError):
    """已登记获取实现报告的传输失败；原始原因由异常链保留。"""


@dataclass(frozen=True, slots=True)
class SkillFetchResult:
    body: bytes | None
    # 条件缓存是获取实现的私有协议值；owner 只随 accepted 正文原子替换。
    validators: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


class SkillFetchRuntime(Protocol):
    async def fetch(
        self, config: SkillFetchConfig, *, validators: Mapping[str, str],
        has_snapshot: bool, max_bytes: int,
    ) -> SkillFetchResult: ...

    async def close(self) -> None: ...


class SkillChangeSubscription(Protocol):
    """只通知应重新获取；初始／重连窗口与 gap 均归来源实现核对。"""

    async def wait(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SkillFetchCapability:
    config_type: type[SkillFetchConfig]
    create: Callable[[], SkillFetchRuntime]
    subscribe: Callable[[SkillFetchRuntime, SkillFetchConfig], Awaitable[SkillChangeSubscription]] | None = None
