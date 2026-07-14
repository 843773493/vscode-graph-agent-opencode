from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class EmbeddingComputerProtocol(Protocol):
    provider_id: str
    model: str

    async def compute(self, inputs: Sequence[str]) -> list[list[float]]: ...
