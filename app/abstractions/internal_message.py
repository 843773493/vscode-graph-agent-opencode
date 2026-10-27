from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RuntimeContextMode = Literal["event", "state"]


@dataclass(frozen=True, slots=True)
class PreparedInternalMessage:
    content: str
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class RuntimeContextReceipt:
    """已保存的上下文版本引用；不表示模型已消费，也不代表执行已接受。"""

    session_id: str
    thread_id: str
    source_id: str
    item_id: str
    revision: str


__all__ = ["PreparedInternalMessage", "RuntimeContextMode", "RuntimeContextReceipt"]
