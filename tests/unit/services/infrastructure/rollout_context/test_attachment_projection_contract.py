"""目录附件跨 owner 投影迁齐坐标，原 canonical 和用户正文保持原文。"""

from copy import deepcopy
from xml.etree.ElementTree import fromstring

import pytest

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    _attachment_references,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.attachments import (
    RolloutAttachmentProjection,
)


@pytest.fixture
def attachment_projection_case():
    source = "boxteam://source/workspace/ws-1/resources/attachments/att_" + "1" * 32
    target = "boxteam://target/workspace/ws-2/resources/attachments/att_" + "2" * 32
    file_id = source + "/derived/%E5%88%86%E6%9E%90.txt"
    metadata = {"origin": "generated", "kind": "attachment_manifest", "schema_version": 1,
                "file_id": file_id, "path": file_id, "directory_uri": source}
    payload = {
        "attachments": [{"file_id": file_id, "directory_uri": source, "name": "分析.txt"}],
        "content": [
            {"type": "text", "text": "用户逐字输入：" + source},
            {"type": "text", "text": f'<attachment id="{file_id}" path="{file_id}" directory_uri="{source}" output_directory="{source}/derived">', "metadata": metadata},
            {"type": "image_url", "image_url": {"url": "data:image/webp;base64,Zmlyc3Q="},
             "metadata": {**metadata, "kind": "attachment_preview", "variant": "preview"}},
        ],
    }
    calls = []

    def project(session, thread, uri):
        calls.append((session, thread, uri))
        assert uri == file_id
        return {"file_id": target + "/derived/%E5%88%86%E6%9E%90.txt", "directory_uri": target, "availability": "available"}

    return source, target, payload, calls, RolloutAttachmentProjection(project)


def test_projection_updates_all_directory_coordinates_without_mutating_canonical(attachment_projection_case):
    source, target, payload, calls, owner = attachment_projection_case
    frozen = canonical_json_bytes(payload)
    result = owner.project_attachment_payload("session", "thread", payload)
    assert canonical_json_bytes(payload) == frozen
    assert result["content"][0] == payload["content"][0]
    assert result["content"][2]["image_url"] == payload["content"][2]["image_url"]
    assert result["attachments"][0] == {"file_id": target + "/derived/%E5%88%86%E6%9E%90.txt", "directory_uri": target, "name": "分析.txt", "availability": "available"}
    for block in result["content"][1:]:
        assert block["metadata"]["path"] == target + "/derived/%E5%88%86%E6%9E%90.txt"
        assert block["metadata"]["directory_uri"] == target
    attributes = fromstring(result["content"][1]["text"] + "</attachment>").attrib
    assert attributes == {"id": target + "/derived/%E5%88%86%E6%9E%90.txt", "path": target + "/derived/%E5%88%86%E6%9E%90.txt", "directory_uri": target, "output_directory": target + "/derived"}
    assert calls == [("session", "thread", source + "/derived/%E5%88%86%E6%9E%90.txt")]


@pytest.mark.parametrize("field", ["directory_uri", "output_directory", "metadata", "reference"])
def test_projection_rejects_conflicting_source_directory(attachment_projection_case, field):
    source, target, payload, _, owner = attachment_projection_case
    broken = deepcopy(payload)
    if field == "reference":
        broken["attachments"][0]["directory_uri"] = target
    elif field == "metadata":
        broken["content"][1]["metadata"]["directory_uri"] = target
    else:
        broken["content"][1]["text"] = broken["content"][1]["text"].replace(f'{field}="{source}', f'{field}="{target}')
    before = canonical_json_bytes(broken)
    with pytest.raises(ValueError, match="source-mismatch"):
        owner.project_attachment_payload("session", "thread", broken)
    assert canonical_json_bytes(broken) == before


def test_legacy_manifest_same_owner_keeps_original_text():
    uri = "boxteam://workspace/ws-1/resources/session/ses_019c3635a6c278efb831fe265bdcd656/attachments/att_" + "1" * 32
    block = {"type": "text", "text": f"<attachment id='{uri}' path='{uri}'>",
             "metadata": {"origin": "generated", "kind": "attachment_manifest", "schema_version": 1, "file_id": uri, "path": uri}}
    owner = RolloutAttachmentProjection(lambda session, thread, file_id: {"file_id": file_id, "availability": "available"})
    assert owner.project_attachment_payload("session", "thread", [block])[0]["text"] == block["text"]


def test_fork_reference_collection_accepts_both_histories_and_rejects_directory(attachment_projection_case):
    source, _, _, _, _ = attachment_projection_case
    legacy = "boxteam://workspace/ws-1/resources/session/ses_019c3635a6c278efb831fe265bdcd656/attachments/att_" + "2" * 32
    references = ((source + "/original/content", 1), (legacy, 0))
    assert _attachment_references(references) == tuple((uri, bool(required)) for uri, required in references)
    with pytest.raises(ValueError):
        _attachment_references(((source, 1),))


def test_projection_rejects_conflicting_target_directory(attachment_projection_case):
    source, target, payload, _, _ = attachment_projection_case
    owner = RolloutAttachmentProjection(lambda session, thread, file_id: {
        "file_id": target + "/original/content", "directory_uri": source, "availability": "available",
    })
    before = canonical_json_bytes(payload)
    with pytest.raises(ValueError, match="source-mismatch"):
        owner.project_attachment_payload("session", "thread", payload)
    assert canonical_json_bytes(payload) == before


def test_regular_file_projection_keeps_reference_and_generated_blocks_without_directory_owner(attachment_projection_case):
    _, _, payload, calls, owner = attachment_projection_case
    uri = "boxteam://source/workspace/ws-1/resources/files/root/%E6%96%87%E4%BB%B6.txt"
    metadata = {"origin": "generated", "kind": "attachment_manifest", "schema_version": 1, "file_id": uri, "path": uri}
    manifest = {"type": "text", "text": f"<attachment id='{uri}' path='{uri}'>", "metadata": metadata}
    payload["attachments"].insert(0, {"file_id": uri, "name": "文件.txt"})
    payload["content"].insert(0, manifest)
    before = canonical_json_bytes(payload)
    result = owner.project_attachment_payload("session", "thread", payload)
    assert result["attachments"][0] == payload["attachments"][0]
    assert result["content"][0] == manifest
    assert canonical_json_bytes(payload) == before
    assert len(calls) == 1
    assert _attachment_references(((uri, 1), (payload["attachments"][1]["file_id"], 1))) == (
        (payload["attachments"][1]["file_id"], True),
    )


@pytest.mark.parametrize("uri", ["/etc/hosts", "relative.txt", "C:\\Users\\file.txt",
                                  "boxteam://workspace/ws-1/resources/files/root",
                                  "boxteam://workspace/ws-1/resources/config/workspace"])
def test_non_file_references_cannot_bypass_projection_or_fork_validation(uri):
    owner = RolloutAttachmentProjection(lambda *args: pytest.fail("非法引用不得调用目录 owner"))
    metadata = {"origin": "generated", "kind": "attachment_preview", "schema_version": 1, "file_id": uri, "path": uri}
    for payload in ({"attachments": [{"file_id": uri}]}, [{"metadata": metadata}]):
        with pytest.raises(ValueError):
            owner.project_attachment_payload("session", "thread", payload)
    with pytest.raises(ValueError):
        _attachment_references(((uri, 1),))


@pytest.mark.parametrize("target", ["/private/original", "boxteam://workspace/ws-2/resources/files/root/a.txt",
                                     "boxteam://workspace/ws-2/resources/attachments/att_" + "2" * 32])
def test_directory_projection_rejects_invalid_receipt_even_without_directory_field(attachment_projection_case, target):
    _, _, payload, _, _ = attachment_projection_case
    owner = RolloutAttachmentProjection(lambda *args: {"file_id": target})
    before = canonical_json_bytes(payload)
    with pytest.raises(ValueError):
        owner.project_attachment_payload("session", "thread", payload)
    assert canonical_json_bytes(payload) == before


@pytest.mark.parametrize("field", ["reference", "metadata", "manifest"])
def test_regular_file_cannot_claim_managed_directory(field):
    uri = "boxteam://workspace/ws-1/resources/files/root/a.txt"
    directory = "boxteam://workspace/ws-1/resources/files/root"
    metadata = {"origin": "generated", "kind": "attachment_manifest", "schema_version": 1, "file_id": uri, "path": uri}
    payload = {"attachments": [{"file_id": uri}], "content": [
        {"type": "text", "text": f'<attachment id="{uri}" path="{uri}">', "metadata": metadata},
    ]}
    if field == "reference":
        payload["attachments"][0]["directory_uri"] = directory
    elif field == "metadata":
        metadata["directory_uri"] = directory
    else:
        payload["content"][0]["text"] = f'<attachment id="{uri}" path="{uri}" directory_uri="{directory}" output_directory="{directory}/derived">'
    owner = RolloutAttachmentProjection(lambda *args: pytest.fail("普通文件不得调用目录 owner"))
    with pytest.raises(ValueError, match="source-mismatch"):
        owner.project_attachment_payload("session", "thread", payload)
