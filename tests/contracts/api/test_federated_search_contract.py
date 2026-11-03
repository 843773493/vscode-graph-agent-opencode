"""联邦合同引用原领域页；候选 descriptor 漂移必须在导出前拒绝。"""

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import assert_gateway_http_schemas
from app.protocol.schema import descriptor_pool_from_set


def test_search_stream_publishes_typed_request_and_event(gateway_openapi: JsonSchemaValue) -> None:
    operation = gateway_openapi["paths"]["/api/gateway/search"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/FederatedSearchRequest")
    response = operation["responses"]["200"]
    assert set(response["content"]) == {"text/event-stream"}
    assert response["x-sse-events"] == {"search": {
        "$ref": "#/components/schemas/FederatedSearchEventDTO", "x-protobuf-message": "boxteam.search.v1.FederatedSearchEventDTO",
    }}
    event = gateway_openapi["components"]["schemas"]["FederatedSearchEventDTO"]
    assert event["properties"]["page"]["anyOf"][0]["$ref"].endswith("/WorkspaceSearchResultDTO")
    assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize("name", ["FederatedSearchRequest", "FederatedSearchEventDTO", "FederatedSearchQueryDTO",
                                  "FederatedSearchBranchDTO", "FederatedSearchContinuationDTO", "FederatedSearchSummaryDTO",
                                  "WorkspaceSearchResultDTO", "FederatedFileSearchTargetDTO", "FederatedSearchBranchKeyDTO",
                                  "WorkspaceFileSearchTargetDTO", "GatewayContextSearchRequest", "GatewayContextSearchResultDTO"])
def test_federated_root_and_nested_drift_are_rejected(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, name: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == name)
    message.field.add(name="unimplemented_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        assert_gateway_http_schemas(gateway_openapi, pool)
    assert name in str(caught.value)
