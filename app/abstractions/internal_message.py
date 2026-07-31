from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PreparedInternalMessage:
    content: str
    metadata: dict[str, object]


__all__ = ["PreparedInternalMessage"]
