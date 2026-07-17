from __future__ import annotations

import uuid

from app.core.identifier import create_prefixed_id, create_uuid_hex


def test_create_uuid_hex_preserves_complete_uuid():
    value = create_uuid_hex()

    assert len(value) == 32
    assert value == value.lower()
    assert uuid.UUID(hex=value).version == 4


def test_create_prefixed_id_preserves_complete_uuid():
    value = create_prefixed_id("evt")
    prefix, raw_uuid = value.split("_", maxsplit=1)

    assert prefix == "evt"
    assert len(raw_uuid) == 32
    assert uuid.UUID(hex=raw_uuid).version == 4


def test_generated_ids_are_unique():
    values = {create_prefixed_id("msg") for _ in range(10_000)}

    assert len(values) == 10_000
