"""统一搜索的公开请求、能力及领域结果递归使用同一 proto 合同。"""

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import assert_public_http_schemas
from app.protocol.schema import descriptor_pool_from_set


@pytest.mark.parametrize("model", [
    "WorkspaceSearchRequest", "WorkspaceSearchCapabilitiesDTO", "WorkspaceSearchResultDTO", "SearchProviderCapabilityDTO",
])
def test_search_checks_actual_public_structure(http_openapi: JsonSchemaValue, model: str) -> None:
    paths = http_openapi["paths"]
    assert "/api/v1/workspaces/{workspace_id}/search" in paths
    assert "/api/v1/workspaces/{workspace_id}/search/capabilities" in paths
    assert_public_http_schemas(http_openapi)
    del http_openapi["components"]["schemas"][model]["properties"][next(iter(http_openapi["components"]["schemas"][model]["properties"]))]
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致"):
        assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize("model", ["SearchProviderCapabilityDTO", "WorkspaceSearchResultDTO", "WorkspaceFileSearchTargetDTO", "ResourceNavigationSearchOptions", "WorkspaceContextSearchOptionsDTO"])
def test_search_rejects_candidate_protocol_drift(
    http_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_search_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致"):
        assert_public_http_schemas(http_openapi, descriptor_pool_from_set(http_descriptor_bundle.SerializeToString()))
