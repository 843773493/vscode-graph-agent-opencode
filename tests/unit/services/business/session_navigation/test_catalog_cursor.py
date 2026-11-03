"""原目录游标校验点输出原因，损坏输入不冒充目录版本变化。"""

import base64
import json

import pytest

from app.services.business.session_navigation.catalog_query import (
    CatalogCursorError,
    decode_catalog_cursor,
    encode_catalog_cursor,
)


@pytest.fixture
def cursor_payload() -> dict[str, object]:
    return {"offset": 1, "revision": "r1", "catalog_revision": 7, "scope": "search:subsequence-v1\nneedle"}


@pytest.mark.parametrize("changes,reason,code", [
    ({"revision": "r0"}, "revision", "stale_catalog_cursor"),
    ({"catalog_revision": 6}, "catalog_revision", "stale_catalog_cursor"),
    ({"scope": "other"}, "scope", "invalid_catalog_cursor"),
    ({"offset": True}, "offset", "invalid_catalog_cursor"),
    ({"offset": -1}, "offset", "invalid_catalog_cursor"),
    ({"offset": 3}, "offset_out_of_range", "invalid_catalog_cursor"),
    ({"revision": None}, "payload", "invalid_catalog_cursor"),
    ({"catalog_revision": True}, "payload", "invalid_catalog_cursor"),
])
def test_cursor_rejection_keeps_actual_validation_reason(
    cursor_payload: dict[str, object], changes: dict[str, object], reason: str, code: str,
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps({**cursor_payload, **changes}).encode()).decode()
    with pytest.raises(CatalogCursorError) as caught:
        decode_catalog_cursor(cursor, "r1", 7, str(cursor_payload["scope"]), collection_size=3)
    detail = caught.value.detail()
    assert (detail["owner"], detail["code"], detail["reason"]) == ("session_catalog", code, reason)
    assert detail["message"] == str(caught.value)


@pytest.mark.parametrize("cursor,reason", [
    ("%%%", "format"), ("非ASCII", "format"),
    (base64.urlsafe_b64encode(b"not-json").decode(), "format"),
    (base64.urlsafe_b64encode(b"[]").decode(), "payload"),
    (base64.urlsafe_b64encode(b"{}").decode(), "payload"),
])
def test_malformed_cursor_does_not_become_stale(cursor: str, reason: str) -> None:
    with pytest.raises(CatalogCursorError) as caught:
        decode_catalog_cursor(cursor, "r1", 7, None, collection_size=3)
    assert caught.value.code == "invalid_catalog_cursor" and caught.value.reason == reason


def test_valid_cursor_keeps_existing_encoding_and_offset() -> None:
    assert decode_catalog_cursor(None, "r1", 7, None, collection_size=3) == 0
    cursor = encode_catalog_cursor(1, "r1", 7, None)
    assert decode_catalog_cursor(cursor, "r1", 7, None, collection_size=3) == 1
