"""Fork 的真实 HTTP 根及嵌套公开结构沿同一 descriptor 检查。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.schemas.internal_v2.session import (
    SessionForkRequest,
    SessionForkResourceChoice,
)
from app.schemas.internal_v2.session_fork_transfer import SessionForkTransferResultDTO


def test_fork_routes_register_their_actual_public_json_roots(http_openapi: JsonSchemaValue) -> None:
    prefix = "/api/v1/workspaces/{workspace_id}/sessions"
    schemas = http_openapi["components"]["schemas"]
    roots: set[str] = set()
    for suffix in ("/{session_id}/fork-source-point", "/{session_id}/fork-context", "/fork-transfer"):
        for operation in http_openapi["paths"][prefix + suffix].values():
            request = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if request is not None:
                roots.add(request["schema"]["$ref"].rsplit("/", 1)[1])
            for status, response in operation["responses"].items():
                body = response.get("content", {}).get("application/json")
                if not status.startswith("2") or body is None:
                    continue
                envelope = schemas[body["schema"]["$ref"].rsplit("/", 1)[1]]
                roots.update(branch["$ref"].rsplit("/", 1)[1]
                             for branch in envelope["properties"]["data"]["anyOf"] if "$ref" in branch)
    assert roots == {"SessionForkRequest", "SessionForkSourcePointDTO", "SessionForkResultDTO",
                     "SessionForkTransferRequest", "SessionForkTransferResultDTO"}
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"fork 入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("SessionForkRequest", "anchor_item_id", {"type": "integer"}),
    ("SessionForkSourcePointDTO", "source_view_revision", {"type": "string"}),
    ("SessionForkResourceSourceDTO", "required_for_context", {"type": "string"}),
    ("SessionForkOwnerReference", "expected_revision", {"type": "string"}),
    ("SessionForkResultDTO", "target_gateway_id", {"type": "integer"}),
    ("SessionForkResourceResult", "committed_revision", {"type": "string"}),
    ("ForkSourceCaptureDTO", "snapshot_sha256", {"type": "integer"}),
    ("ForkTransferFileDTO", "size_bytes", {"type": "string"}),
    ("SessionForkTransferRequest", "offset", {"type": "string"}),
    ("SessionForkTransferResultDTO", "intent", {"type": "array", "items": {}}),
])
def test_fork_http_schema_rejects_anchor_owner_capture_and_transfer_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_fork_structure_checks_preserve_pydantic_selection_and_chunk_constraints() -> None:
    request = {"idempotency_key": "fork-contract", "source_uri": "source-thread",
               "target_container_uri": "target-container"}
    with pytest.raises(ValidationError, match="view_prefix_fork 必须提供点击 item"):
        SessionForkRequest.model_validate(request)
    with pytest.raises(ValidationError, match="full_rollout_copy 不能携带截断锚点"):
        SessionForkRequest.model_validate({**request, "mode": "full_rollout_copy", "source_view_id": "view",
                                          "source_view_revision": 1, "anchor_item_id": "item"})
    with pytest.raises(ValidationError, match="必须且只能提供静态 URI 或 typed owner reference"):
        SessionForkResourceChoice.model_validate({"action": "reuse_reference"})
    with pytest.raises(ValidationError, match="同一 fork resource 不能重复选择"):
        SessionForkRequest.model_validate({**request, "anchor_item_id": "item", "resources": [
            {"resource_uri": "same-resource", "action": "omit"},
            {"resource_uri": "same-resource", "action": "omit"},
        ]})
    with pytest.raises(ValidationError, match="offset"):
        SessionForkTransferResultDTO(offset=-1)
