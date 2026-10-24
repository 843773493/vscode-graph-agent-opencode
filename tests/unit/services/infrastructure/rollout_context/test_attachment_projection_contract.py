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
