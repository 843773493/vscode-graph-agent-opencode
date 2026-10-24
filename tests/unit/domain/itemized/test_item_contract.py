from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from itertools import product

import pytest

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
)
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, content_hash
from app.domain.itemized.identity.item_projection import ToolResultEvidence
from app.domain.itemized.records import (
    CanonicalItemRecord,
    InternalDisplayPart,
    InternalDisplayProvenance,
    RuntimeNoticePayload,
    user_input_payload,
)
from app.domain.itemized.schema import validate_item_compatibility
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)

# 独立抄录规范值域；预期矩阵来自文本 fixture，不能从生产 ALLOWED_* 生成。
KINDS = (
    "user_input",
    "assistant_output",
    "reasoning",
    "tool_call",
    "tool_result",
    "runtime_notice",
    "compaction_summary",
    "attachment",
    "extension",
)
PAYLOADS = (
    "text",
    "structured_content",
    "tool_call",
    "tool_result",
    "summary",
    "attachment_ref",
    "opaque",
    "extension",
)
STATUSES = ("completed", "partial", "incomplete", "cancelled", "failed", "unknown")


@pytest.mark.parametrize("has_directory", [False, True])
def test_user_attachment_identity_survives_canonical_serialization_and_job_projection(
    user_item: CanonicalItemRecord, has_directory: bool,
) -> None:
    directory = "boxteam://workspace/admission-tests/resources/attachments/att_0123456789abcdef0123456789abcdef"
    reference = AttachmentRef(file_id=directory + "/original/content", name="输入.txt", content_type="text/plain",
                              directory_uri=directory if has_directory else None)
    original = reference.model_dump(mode="json", exclude_none=True)
    payload = user_input_payload("用户原文", [original])
    item = replace(user_item, payload_kind=PayloadKind.STRUCTURED_CONTENT, payload=payload,
                   content_hash=content_hash(PayloadKind.STRUCTURED_CONTENT, payload))
    restored = CanonicalItemRecord.from_dict(json.loads(canonical_json_bytes(item.to_dict())))
    codec = LangChainMessageCodec()
    message = codec.from_dict(codec.project_message((restored,)))
    projected, = message.response_metadata["attachments"]
    assert AttachmentRef.model_validate(projected) == reference
    assert message.content == "用户原文"
    assert restored.content_hash == item.content_hash
    if not has_directory:
        assert "directory_uri" not in restored.payload["attachments"][0]


@pytest.fixture(params=["legacy", "directory"])
def notice_attachment_payload(request):
    uri = "boxteam://workspace/notice-tests/resources/session/ses_019c3635a6c278efb831fe265bdcd656/attachments/att_00000000000000000000000000000000"
    metadata = {
        "origin": "generated", "schema_version": 1, "file_id": uri, "path": uri,
        "name": "input.png", "content_type": "image/png", "preview_status": "available",
    }
    directory = "boxteam://workspace/notice-tests/resources/attachments/att_00000000000000000000000000000000"
    if request.param == "directory":
        uri = directory + "/original/content"
        metadata.update(file_id=uri, path=uri, directory_uri=directory)
    payload = {
        "prompt": "<system_reminder>私有正文</system_reminder>",
        "display_part": {"kind": "generated_session_result", "text": "正在处理结果。", "provenance": {}},
        "attachments": [{"file_id": uri, "name": "input.png", "content_type": "image/png"}],
        "attachment_blocks": [
            {"type": "text", "text": f'<attachment id="{uri}" path="{uri}" name="input.png">',
             "metadata": {**metadata, "kind": "attachment_manifest"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="},
             "metadata": {**metadata, "kind": "attachment_preview", "variant": "preview"}},
        ],
    }
    if request.param == "directory":
        payload["attachments"][0]["directory_uri"] = directory
        payload["attachment_blocks"][0]["text"] = (
            f'<attachment id="{uri}" path="{uri}" name="input.png" directory_uri="{directory}" '
            f'output_directory="{directory}/derived" processing="保留原件，输出写入新文件">'
        )
    return payload


@pytest.mark.parametrize("hidden", [False, True])
def test_runtime_notice_attachment_payload_roundtrip_and_hash(notice_attachment_payload, hidden):
    raw = notice_attachment_payload
    if hidden:
        del raw["display_part"]
    frozen = RuntimeNoticePayload.from_dict(raw)
    assert frozen.to_dict() == raw
    assert RuntimeNoticePayload.from_dict(json.loads(canonical_json_bytes(frozen.to_dict()))) == frozen
    for field in ("prompt", "attachments", "attachment_blocks"):
        altered = deepcopy(raw)
        if field == "prompt":
            altered[field] += "改变"
        elif field == "attachments":
            altered[field][0]["name"] = "another.png"
        else:
            altered[field][1]["image_url"]["url"] = "data:image/png;base64,YW5vdGhlcg=="
        assert content_hash("structured_content", altered) != content_hash("structured_content", raw)


@pytest.mark.parametrize("fault", [
    "raw_data", "physical_path", "unknown_payload", "unpaired", "missing_manifest",
    "wrong_manifest", "raw_prompt_block", "wrong_origin", "wrong_version", "preview_order", "live_preview",
])
def test_runtime_notice_attachment_payload_rejects_unfrozen_or_unbound_content(notice_attachment_payload, fault):
    raw = notice_attachment_payload
    if fault == "raw_data":
        raw["attachments"][0]["data_url"] = "data:image/png;base64,aGVsbG8="
    elif fault == "physical_path":
        raw["attachments"][0]["file_id"] = "/tmp/private.png"
    elif fault == "unknown_payload":
        raw["run_id"] = "forged-control"
    elif fault == "unpaired":
        del raw["attachment_blocks"]
    elif fault == "missing_manifest":
        raw["attachment_blocks"].pop(0)
    elif fault == "wrong_manifest":
        raw["attachment_blocks"][0]["text"] = '<attachment id="another" path="another">'
    elif fault == "raw_prompt_block":
        raw["attachment_blocks"][0]["text"] = raw["prompt"]
    elif fault == "wrong_origin":
        raw["attachment_blocks"][0]["metadata"]["origin"] = "user"
    elif fault == "wrong_version":
        raw["attachment_blocks"][0]["metadata"]["schema_version"] = True
    elif fault == "preview_order":
        raw["attachment_blocks"].reverse()
    elif fault == "live_preview":
        raw["attachment_blocks"][1]["image_url"]["url"] = "file:///tmp/private.png"
    with pytest.raises(ValueError):
        RuntimeNoticePayload.from_dict(raw)


def test_runtime_notice_display_payload_round_trips_with_one_content_hash() -> None:
    payload = RuntimeNoticePayload(
        prompt=(
            "<system_reminder>内部控制正文\n<generated_session_result>分支结果"
            "</generated_session_result></system_reminder>"
        ),
        display_part=InternalDisplayPart(
            kind="generated_session_result",
            text="生成分支已结束，主会话正在处理返回结果。",
            provenance=InternalDisplayProvenance(
                source_session_id="session-source"
            ),
        ),
    ).to_dict()
    item = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-internal-result",
        semantic_kind=SemanticKind.RUNTIME_NOTICE,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "runtime",
            "producer_id": "message-internal-result",
        },
        payload=payload,
        extensions={},
        turn_scope="pending_next_turn",
        wire_role="user",
    )

    restored = CanonicalItemRecord.from_dict(
        json.loads(canonical_json_bytes(item.to_dict()))
    )

    assert restored.payload == payload
    assert restored.content_hash == item.content_hash
    assert restored.turn_id is None
    assert restored.turn_scope == "pending_next_turn"


def test_runtime_notice_cannot_masquerade_as_user_turn_root() -> None:
    payload = RuntimeNoticePayload(
        prompt=(
            "<system_reminder>内部控制正文\n<generated_session_result>分支结果"
            "</generated_session_result></system_reminder>"
        ),
        display_part=InternalDisplayPart(
            kind="generated_session_result",
            text="生成分支已结束，主会话正在处理返回结果。",
            provenance=InternalDisplayProvenance(
                source_session_id="session-source",
            ),
        ),
    ).to_dict()
    item = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-runtime-notice-turn-root-negative",
        semantic_kind=SemanticKind.RUNTIME_NOTICE,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "runtime",
            "producer_id": "message-runtime-notice-turn-root-negative",
            "invocation_id": "execution-runtime-notice-turn-root-negative",
        },
        payload=payload,
        extensions={},
        turn_scope="pending_next_turn",
        wire_role="user",
    )

    assert item.content_hash == content_hash(PayloadKind.STRUCTURED_CONTENT, payload)
    assert item.producer_ref["producer_kind"] == "runtime"
    with pytest.raises(
        ItemSchemaError,
        match="turn_root 必须是带 turn_id 的 user_input",
    ):
        replace(item, turn_id="turn-runtime-notice", turn_scope="turn_root")


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "<system_reminder/>", "display_part": {"kind": "unknown"}},
        {
            "prompt": "<system_reminder/>",
            "display_part": {
                "kind": "generated_session_result",
                "text": "安全展示",
                "provenance": {},
                "extra": "禁止的字段",
            },
        },
        {
            "prompt": "<system_reminder/>",
            "display_part": {
                "kind": "generated_session_result",
                "text": "安全展示",
                "provenance": {"run_id": "禁止的来源推断"},
            },
        },
        {"prompt": "<system_reminder/>"},
        {
            "prompt": "<system_reminder/>",
            "display_part": {"kind": "generated_session_result", "provenance": {}},
        },
        {
            "prompt": "<system_reminder/>",
            "display_part": {
                "kind": "generated_session_result", "text": "", "provenance": {},
            },
        },
        *[
            {
                "prompt": "<system_reminder/>",
                "display_part": {
                    "kind": "generated_session_result", "text": "安全展示", "provenance": {},
                },
                field: {"private_result": "禁止的第二份正文"},
            }
            for field in ("metadata", "extensions")
        ],
    ],
)
def test_runtime_notice_display_payload_rejects_unknown_kind_and_fields(
    payload: object,
) -> None:
    with pytest.raises(ItemSchemaError):
        CanonicalItemRecord.create(
            item_sequence=1,
            item_id="item-invalid-internal-result",
            semantic_kind=SemanticKind.RUNTIME_NOTICE,
            payload_kind=PayloadKind.STRUCTURED_CONTENT,
            status=CanonicalItemStatus.COMPLETED,
            producer_ref={
                "producer_kind": "runtime",
                "producer_id": "message-invalid-internal-result",
            },
            payload=payload,
            turn_scope="pending_next_turn",
        )


@pytest.mark.parametrize("missing_field", ["display_part", "kind", "text", "provenance"])
def test_runtime_notice_cold_decode_rejects_missing_display_with_matching_hash(
    missing_field: str,
) -> None:
    payload = RuntimeNoticePayload(
        prompt="仅供 Provider 的私有分支结果",
        display_part=InternalDisplayPart(
            kind="generated_session_result", text="任务已完成。",
            provenance=InternalDisplayProvenance(),
        ),
    ).to_dict()
    item = CanonicalItemRecord.create(
        item_sequence=1, item_id="item-notice-missing-display",
        semantic_kind=SemanticKind.RUNTIME_NOTICE,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "runtime", "producer_id": "notice-missing-display"},
        payload=payload, turn_scope="pending_next_turn",
    )
    raw = json.loads(canonical_json_bytes(item.to_dict()))
    if missing_field == "display_part":
        del raw["payload"][missing_field]
    else:
        del raw["payload"]["display_part"][missing_field]
    # 重新计算摘要，证明拒绝来自展示合同，而非碰巧命中旧 hash 不匹配。
    raw["content_hash"] = content_hash(PayloadKind.STRUCTURED_CONTENT, raw["payload"])
    with pytest.raises(ItemSchemaError, match="缺少字段"):
        CanonicalItemRecord.from_dict(raw)


@pytest.fixture
def item_arguments(user_item: CanonicalItemRecord) -> dict[str, object]:
    raw = user_item.to_dict()
    for name in ("format_version", "record_type", "content_hash"):
        raw.pop(name)
    for name in ("projection_identity", "projection_group", "tool_result_evidence", "source_manifest", "provenance_relations"):
        raw[name] = getattr(user_item, name)
    return raw


@pytest.mark.parametrize("semantic_kind", KINDS)
@pytest.mark.parametrize(("payload_kind", "status"), list(product(PAYLOADS, STATUSES)))
def test_full_semantic_payload_status_matrix(
    item_matrix: dict[str, object],
    item_arguments: dict[str, object],
    semantic_kind: str,
    payload_kind: str,
    status: str,
) -> None:
    row = next(
        row for row in item_matrix["rows"] if row["semantic_kind"] == semantic_kind
    )
    allowed = (
        payload_kind in row["allowed_payloads"] and status in row["allowed_statuses"]
    )
    arguments = {
        **item_arguments,
        "semantic_kind": semantic_kind,
        "payload_kind": payload_kind,
        "status": status,
        "turn_id": None,
        "turn_scope": "ambient",
        "payload": row["examples"].get(payload_kind, {}),
        "tool_result_evidence": ToolResultEvidence.from_dict(row["tool_result_evidence_by_payload"][payload_kind]) if payload_kind in row.get("tool_result_evidence_by_payload", {}) else None,
    }
    if not allowed:
        with pytest.raises(ItemSchemaError, match="item-schema-incompatible"):
            CanonicalItemRecord.create(**arguments)
        return
    validate_item_compatibility(semantic_kind, payload_kind, status)
    item = CanonicalItemRecord.create(**arguments)
    restored = CanonicalItemRecord.from_dict(
        json.loads(canonical_json_bytes(item.to_dict()))
    )
    assert restored == item


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize(
    "outcome", [None, "success", "failure", "cancelled", "unknown", "invalid"]
)
@pytest.mark.parametrize("confirmed", [True, False])
def test_tool_outcome_marker_matrix(
    item_arguments: dict[str, object],
    status: str,
    outcome: str | None,
    confirmed: bool,
) -> None:
    payload = {"tool_call_id": "call-1", "result_id": "result-1", "content": "result"}
    payload["execution_confirmed"] = confirmed
    if outcome is not None:
        payload["tool_outcome"] = outcome
    allowed = outcome == "unknown" or (
        confirmed
        and (
            outcome in {None, "success", "failure", "cancelled"}
            if status == "completed"
            else outcome is None
        )
    )
    arguments = {
        **item_arguments,
        "semantic_kind": "tool_result",
        "payload_kind": "tool_result",
        "status": status,
        "turn_scope": "turn_member",
        "payload": payload,

    }
    if allowed:
        item = CanonicalItemRecord.create(**arguments)
        assert item.payload.get("tool_outcome") == outcome
    else:
        with pytest.raises(ItemSchemaError, match="item-schema-incompatible"):
            CanonicalItemRecord.create(**arguments)


@pytest.mark.parametrize("status", STATUSES)
def test_text_result_preserves_exact_body_and_explicit_identity(
    item_arguments: dict[str, object],
    status: str,
) -> None:
    text = "  é é 中文 😀\r\n"
    item = CanonicalItemRecord.create(
        **{
            **item_arguments,
            "semantic_kind": "tool_result",
            "payload_kind": "text",
            "status": status,
            "turn_scope": "turn_member",
            "payload": text,
            "tool_result_evidence": ToolResultEvidence("call-explicit", "result-explicit", True),
        }
    )
    assert item.payload == text
    assert item.tool_result_evidence.tool_call_id == "call-explicit"
    assert (
        CanonicalItemRecord.from_dict(json.loads(canonical_json_bytes(item.to_dict())))
        == item
    )
    changed_identity = replace(
        item, tool_result_evidence=replace(item.tool_result_evidence, result_id="another-result")
    )
    assert changed_identity.content_hash == item.content_hash


@pytest.mark.parametrize("field", ["tool_call_id", "result_id"])
def test_text_result_does_not_guess_missing_identity(
    item_arguments: dict[str, object], field: str
) -> None:
    metadata = {"tool_call_id": "call-explicit", "result_id": "result-explicit"}
    metadata.pop(field)
    with pytest.raises((ItemSchemaError, TypeError)):
        CanonicalItemRecord.create(
            **{
                **item_arguments,
                "semantic_kind": "tool_result",
                "payload_kind": "text",
                "payload": "正文不是 identity",
                "tool_result_evidence": ToolResultEvidence(**metadata, execution_confirmed=True),
            }
        )


@pytest.mark.parametrize("status", STATUSES)
def test_unconfirmed_text_result_requires_typed_unknown_marker(
    item_arguments: dict[str, object],
    status: str,
) -> None:
    with pytest.raises(ItemSchemaError, match="纯 text 无法承载 tool_outcome"):
        CanonicalItemRecord.create(
            **{
                **item_arguments,
                "semantic_kind": "tool_result",
                "payload_kind": "text",
                "status": status,
                "payload": "result",
                "tool_result_evidence": ToolResultEvidence("call-explicit", "result-explicit", False),
            }
        )


@pytest.mark.parametrize("kind", KINDS)
def test_tool_marker_extension_has_no_execution_authority(
    item_matrix: dict[str, object], item_arguments: dict[str, object], kind: str,
) -> None:
    row = next(row for row in item_matrix["rows"] if row["semantic_kind"] == kind)
    payload_kind = row["allowed_payloads"][0]
    if kind == "tool_result":
        payload_kind = "tool_result"
    item = CanonicalItemRecord.create(**{
        **item_arguments, "semantic_kind": kind, "payload_kind": payload_kind,
        "payload": row["examples"][payload_kind], "turn_id": None, "turn_scope": "ambient",
        "extensions": {"boxteam.test.annotations": {
            "extension_version": "1", "protection": "public", "value": {"tool_outcome": "success"},
        }},
    })
    assert item.extensions["boxteam.test.annotations"]["value"] == {"tool_outcome": "success"}
    if kind == "tool_result":
        assert item.payload["tool_outcome"] == "unknown"


@pytest.mark.parametrize(
    "semantic_kind", [kind for kind in KINDS if kind != "tool_result"]
)
def test_tool_marker_forbidden_in_other_semantic_kinds(
    item_matrix: dict[str, object],
    item_arguments: dict[str, object],
    semantic_kind: str,
) -> None:
    row = next(
        row for row in item_matrix["rows"] if row["semantic_kind"] == semantic_kind
    )
    payload_kind, payload = next(
        (kind, value)
        for kind, value in row["examples"].items()
        if isinstance(value, dict)
    )
    with pytest.raises(ItemSchemaError, match="item-schema-incompatible"):
        CanonicalItemRecord.create(
            **{
                **item_arguments,
                "semantic_kind": semantic_kind,
                "payload_kind": payload_kind,
                "turn_scope": "ambient",
                "turn_id": None,
                "payload": {**payload, "tool_outcome": "unknown"},
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("semantic_kind", "assistant_text"),
        ("semantic_kind", "future_kind"),
        ("payload_kind", "future_payload"),
        ("status", "active"),
        ("status", "completed_empty"),
    ],
)
def test_unknown_core_kind_is_not_reinterpreted(
    item_arguments: dict[str, object],
    field: str,
    value: str,
) -> None:
    with pytest.raises(ItemSchemaError):
        CanonicalItemRecord.create(**{**item_arguments, field: value})


@pytest.mark.parametrize(
    ("kind", "payload_kind"),
    [
        ("reasoning", "opaque"),
        ("reasoning", "extension"),
        ("extension", "opaque"),
        ("extension", "extension"),
    ],
)
@pytest.mark.parametrize(
    "field", ["encoding", "value", "wire_type", "schema_version", "protection"]
)
def test_protected_envelope_requires_its_explicit_fields(
    item_matrix: dict[str, object],
    item_arguments: dict[str, object],
    kind: str,
    payload_kind: str,
    field: str,
) -> None:
    row = next(row for row in item_matrix["rows"] if row["semantic_kind"] == kind)
    payload = dict(row["examples"][payload_kind])
    payload.pop(field)
    with pytest.raises(ItemSchemaError):
        CanonicalItemRecord.create(
            **{
                **item_arguments,
                "semantic_kind": kind,
                "payload_kind": payload_kind,
                "turn_scope": "turn_member",
                "payload": payload,
            }
        )


def test_attachment_length_cannot_be_boolean(
    item_matrix: dict[str, object], item_arguments: dict[str, object]
) -> None:
    row = next(
        row for row in item_matrix["rows"] if row["semantic_kind"] == "attachment"
    )
    with pytest.raises(ItemSchemaError, match="length"):
        CanonicalItemRecord.create(
            **{
                **item_arguments,
                "semantic_kind": "attachment",
                "payload_kind": "attachment_ref",
                "turn_scope": "turn_member",
                "payload": {**row["examples"]["attachment_ref"], "length": True},
            }
        )


def test_payload_corruption_cannot_reuse_the_original_hash(
    user_item: CanonicalItemRecord,
) -> None:
    with pytest.raises(ItemSchemaError, match="content_hash"):
        replace(user_item, payload="篡改正文")


@pytest.mark.parametrize("marker", [True, 1, [], {}])
def test_tool_marker_rejects_non_string_values(
    item_arguments: dict[str, object], marker: object
) -> None:
    with pytest.raises(ItemSchemaError, match="item-schema-incompatible"):
        CanonicalItemRecord.create(
            **{
                **item_arguments,
                "semantic_kind": "tool_result",
                "payload_kind": "tool_result",
                "turn_scope": "turn_member",
                "payload": {
                    "tool_call_id": "call",
                    "result_id": "result",
                    "tool_outcome": marker,
                    "execution_confirmed": True,
                },
            }
        )


def test_canonical_item_round_trip_rejects_legacy_message_fields(
    user_item: CanonicalItemRecord,
) -> None:
    item = user_item
    restored = CanonicalItemRecord.from_dict(item.to_dict())
    assert restored == item
    legacy = {**item.to_dict(), "message": {"data": {"content": "旧格式"}}}
    with pytest.raises(FormatDispatchError, match="legacy message envelope"):
        CanonicalItemRecord.from_dict(legacy)


@pytest.mark.parametrize("format_version", [2.0, True, "2"])
def test_canonical_item_rejects_non_exact_format_version(
    user_item: CanonicalItemRecord,
    format_version: object,
) -> None:
    raw = user_item.to_dict()
    raw["format_version"] = format_version
    with pytest.raises(FormatDispatchError, match="v2 item envelope"):
        CanonicalItemRecord.from_dict(raw)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("semantic_kind", 1),
        ("payload_kind", None),
        ("status", True),
        ("created_at", ""),
        ("extensions", []),
        ("turn_scope", 1),
    ],
)
def test_canonical_item_factory_rejects_coerced_ingress_values(
    field_name: str,
    value: object,
) -> None:
    kwargs: dict[str, object] = {
        "item_sequence": 1,
        "item_id": "item-factory-contract",
        "semantic_kind": SemanticKind.USER_INPUT,
        "payload_kind": PayloadKind.TEXT,
        "status": CanonicalItemStatus.COMPLETED,
        "producer_ref": {
            "producer_kind": "user",
            "producer_id": "ingress-factory-contract",
        },
        "payload": "factory contract",
        "created_at": "2026-09-07T00:00:00+00:00",
        "extensions": {},
        "turn_id": "turn-factory-contract",
        "turn_scope": "turn_root",
    }
    kwargs[field_name] = value
    with pytest.raises(ItemSchemaError):
        CanonicalItemRecord.create(**kwargs)


@pytest.mark.parametrize("fault", ["reference", "metadata", "manifest", "output"])
def test_runtime_notice_rejects_conflicting_attachment_directory(notice_attachment_payload, fault):
    raw = notice_attachment_payload
    other = "boxteam://workspace/other/resources/attachments/att_11111111111111111111111111111111"
    if fault == "reference":
        raw["attachments"][0]["directory_uri"] = other
    elif fault == "metadata":
        raw["attachment_blocks"][0]["metadata"]["directory_uri"] = other
    else:
        attribute = "directory_uri" if fault == "manifest" else "output_directory"
        raw["attachment_blocks"][0]["text"] = raw["attachment_blocks"][0]["text"][:-1] + f' {attribute}="{other}">'
    with pytest.raises(ValueError):
        RuntimeNoticePayload.from_dict(raw)
