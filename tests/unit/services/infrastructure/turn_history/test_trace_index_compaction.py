"""Trace Turn 索引 payload 压缩边界测试。"""

from __future__ import annotations

from typing import cast

import pytest

from app.schemas.event import Event
from app.services.infrastructure.turn_history import trace_index_compaction


def test_compaction_stops_iterating_mapping_after_entry_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingDict(dict[str, object]):
        def __init__(self, values: dict[str, object]) -> None:
            super().__init__(values)
            self.visited_entries = 0

        def items(self):
            for entry in super().items():
                self.visited_entries += 1
                yield entry

    payload = CountingDict({f"key-{index}": "value" for index in range(1000)})

    class EventWithPayload:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"payload": payload}

    monkeypatch.setattr(
        trace_index_compaction._AnyEvent,
        "model_validate",
        staticmethod(lambda value: value),
    )
    compacted = trace_index_compaction.compact_event(cast(Event, EventWithPayload()))

    compact_payload = compacted["payload"]
    assert isinstance(compact_payload, dict)
    assert tuple(compact_payload) == tuple(f"key-{index}" for index in range(64))
    assert payload.visited_entries == 64
