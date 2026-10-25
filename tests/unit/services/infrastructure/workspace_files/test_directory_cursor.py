"""目录游标拒绝非法引用时，不把请求正文重新发布为错误。"""

from __future__ import annotations

import base64
import copy
import json

import pytest
from pydantic import ValidationError

from app.schemas.internal_v2.workspace_file_operations import WorkspaceDirectoryKeyDTO
from app.services.infrastructure.workspace_files.store import (
    decode_directory_cursor,
    encode_directory_cursor,
)


@pytest.fixture
def directory_key():
    root = "boxteam://workspace/00000000-0000-4000-8000-000000000001/resources/files/root"
    return WorkspaceDirectoryKeyDTO(
        workspace_id="00000000-0000-4000-8000-000000000001",
        scope="workspace", root=root, path=root + "/%E7%9B%AE%E5%BD%95",
    )


@pytest.mark.parametrize("native", [
    "/private-cursor/secret.txt", r"C:\private-cursor\secret.txt",
    r"\\private-cursor\share\secret.txt", "file:///private-cursor/secret.txt",
])
@pytest.mark.parametrize("field", ["path", "root", "extra_key"])
def test_invalid_directory_key_preserves_private_cause_only_in_diagnostics(
    directory_key, caplog, native, field,
):
    key = directory_key.model_dump()
    if field == "extra_key":
        key[native] = "invalid"
    else:
        key[field] = native
    original = copy.deepcopy(key)
    payload = {"directory_key": key, "revision": 7, "after": [True, "a", "A"]}
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(ValueError) as failure:
        decode_directory_cursor(cursor)
    assert "private-cursor" not in str(failure.value)
    assert str(failure.value) == "目录分页游标内容错误"
    assert isinstance(failure.value.__cause__, ValidationError)
    assert "private-cursor" in caplog.text
    assert key == original


def test_valid_directory_cursor_keeps_owner_revision_and_order(directory_key):
    order = (False, "中文 %.txt", "中文 %.txt")
    cursor = encode_directory_cursor(directory_key, 7, order)
    assert decode_directory_cursor(cursor) == {
        "directory_key": directory_key.model_dump(), "revision": 7, "after": order,
    }


@pytest.mark.parametrize("cursor", ["%%", "e30", "bm90LWpzb24", "_w"])
def test_invalid_cursor_encoding_is_still_rejected(cursor):
    with pytest.raises(ValueError, match="目录分页游标"):
        decode_directory_cursor(cursor)
