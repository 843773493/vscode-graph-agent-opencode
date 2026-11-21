"""单 Item 的内容类型与只读投影，不从展示摘要补造完整正文。"""

from dataclasses import replace

import pytest

from app.domain.itemized.identity.item_projection import ItemSourceManifest
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.history_turns.item_content import item_contents


@pytest.fixture
def item():
    def create(payload, *, kind="assistant_output", payload_kind="text"):
        return CanonicalItemRecord.create(
            item_sequence=1, item_id="item-model:block:0", semantic_kind=kind,
            payload_kind=payload_kind, status="completed", payload=payload,
            producer_ref={"producer_kind": "provider", "producer_id": "model", "invocation_id": "call"},
        )
    return create


@pytest.mark.parametrize(("text", "language", "expected"), [
    ("## 说明\n内容😀", "markdown", "## 说明\n内容😀"),
    ('{"answer":42}', "json", '{"answer":42}'),
    ('{"未完成', "markdown", '{"未完成'),
    ("```python\nprint(42)\n```", "python", "print(42)"),
])
def test_content_keeps_markdown_or_identifies_complete_source(item, text, language, expected):
    content, = item_contents(item(text))
    assert (content.language, content.content, content.encoding) == (language, expected, "utf-8")


def test_open_tool_result_uses_provided_full_output_and_arguments_stay_json(item):
    result = item({"tool_call_id": "call", "result_id": "result", "execution_confirmed": True, "content": "截断摘要"}, kind="tool_result", payload_kind="tool_result")
    content, = item_contents(result, tool_output='{"完整":true}')
    assert content.language == "json" and content.content == '{"完整":true}'
    call = item({"tool_calls": [{"id": "call", "name": "read_file", "args": {"path": "a.py"}}]}, kind="tool_call", payload_kind="tool_call")
    content, = item_contents(call)
    assert content.language == "json" and '"read_file"' in content.content


def test_inline_media_preserves_bytes_instead_of_rendering_base64_as_text(item):
    image = item([{"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}], payload_kind="structured_content")
    content, = item_contents(image)
    assert (content.content, content.mime_type, content.encoding) == ("aGVsbG8=", "image/png", "base64")


def test_attachment_uses_its_canonical_reference_and_unknown_structure_stays_json(item):
    uri = "boxteam://gateway_1/workspace/workspace_1/resources/session/session_1/attachments/image/content"
    attachment = item({"ref": uri, "length": 12, "hash": "hash"}, kind="attachment", payload_kind="attachment_ref")
    content, = item_contents(attachment)
    assert content.uri == uri and content.content is None
    content, = item_contents(item({"unknown": 42}, payload_kind="structured_content"))
    assert content.language == "json" and '"unknown": 42' in content.content


def test_protected_item_is_not_exposed_by_content_open(item):
    protected = replace(item("hidden"), source_manifest=ItemSourceManifest(
        source_revision="revision", visibility="public", protection="protected",
    ))
    with pytest.raises(ValueError, match="受保护"):
        item_contents(protected)
