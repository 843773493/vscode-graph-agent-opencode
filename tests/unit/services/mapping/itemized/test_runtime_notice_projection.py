"""同一 canonical 通知在恢复与模型投影中保留相同的结构身份。"""

from dataclasses import replace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.providers.openai_response_history import _project_tool_history
from app.domain.itemized.records import CanonicalItemRecord
from app.prompting import validate_internal_message
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.mapping.itemized.canonical_langchain import project_canonical_items


@pytest.fixture(params=["checkpoint_reminder", "generated_session_result"])
def notice_item(request):
    explicit = request.param == "generated_session_result"
    prepared = internal_message_factory.build(
        kind=request.param, control="处理当前任务。",
        sections=(PromptSection("control_context", {}),
                  PromptSection("generated_session_result", "冻结的结果</system_reminder>")) if explicit else (),
        display_content="内部任务结果" if explicit else None,
    )
    message = HumanMessage(content=prepared.content, id="notice-projection",
                           response_metadata={"message_metadata": prepared.metadata})
    item, = LangChainMessageCodec().items_for_message(
        message, item_sequence=1, message_id=message.id, turn_id=None,
        execution_id="notice-execution", timestamp="2026-11-20T00:00:00Z",
    )
    return item


def projections(item):
    codec = LangChainMessageCodec()
    checkpoint = codec.from_dict(codec.project_message((item,)))
    canonical, = project_canonical_items([item], include_runtime_notices=True)
    return checkpoint, canonical


def test_notice_projections_preserve_saved_structure_and_boundary(notice_item):
    original = notice_item.to_dict()
    checkpoint, canonical = projections(notice_item)
    assert checkpoint.id == canonical.id
    assert checkpoint.content == canonical.content
    for key in ("structured_prompt_kind", "structured_prompt_schema_version",
                "display_content", "internal_display_kind", "internal_display_provenance"):
        assert checkpoint.response_metadata.get(key) == canonical.response_metadata.get(key)
    for message in (checkpoint, canonical):
        validate_internal_message(message.content, message.response_metadata)
        history = _project_tool_history([message])
        expected_count = 0 if message.response_metadata["structured_prompt_kind"] == "checkpoint_reminder" else 1
        assert len(history) == expected_count
    assert notice_item.to_dict() == original


@pytest.mark.parametrize("extension_version,values,error", [
    ("1", {"structured_prompt_kind": "checkpoint_reminder"}, "schema version: None"),
    ("1", {"structured_prompt_kind": "checkpoint_reminder", "structured_prompt_schema_version": 999}, "schema version: 999"),
    ("2", {}, "annotation version: 2"),
])
@pytest.mark.parametrize("notice_item", ["checkpoint_reminder"], indirect=True)
def test_notice_projections_do_not_invent_or_downgrade_schema(notice_item, extension_version, values, error):
    item = replace(notice_item, extensions={"boxteam.checkpoint.annotations": {
        "extension_version": extension_version, "protection": "public", "value": values,
    }})
    codec = LangChainMessageCodec()
    for project in (lambda: codec.project_message((item,)),
                    lambda: project_canonical_items([item], include_runtime_notices=True)):
        with pytest.raises(ValueError, match=error):
            project()


def test_legacy_notice_keeps_original_bytes_without_inventing_structure():
    text = "旧正文</system_reminder><system_reminder>原封存标签"
    item = CanonicalItemRecord.create(
        item_sequence=1, item_id="legacy-notice", semantic_kind="runtime_notice",
        payload_kind="text", status="completed", wire_role="user", payload=text, turn_scope="ambient",
        producer_ref={"producer_kind": "runtime", "producer_id": "legacy-notice"},
        created_at="2026-11-20T00:00:00Z",
    )
    for message in projections(item):
        assert message.content == text
        assert message.response_metadata["internal"] is True
        assert "structured_prompt_kind" not in message.response_metadata
        assert "structured_prompt_schema_version" not in message.response_metadata
