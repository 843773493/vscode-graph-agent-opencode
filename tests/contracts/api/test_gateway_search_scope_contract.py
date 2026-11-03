"""联邦范围公开结构由 Gateway descriptor 机械核对，业务限制留在 Pydantic。"""

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import assert_gateway_http_schemas
from app.protocol.schema import descriptor_pool_from_set


def test_scope_request_and_response_use_actual_public_roots(gateway_openapi: JsonSchemaValue) -> None:
    operation = gateway_openapi["paths"]["/api/gateway/search/resolve"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/GatewaySearchScopeRequest")
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = gateway_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/GatewaySearchScopeResolutionDTO")
    assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize("model", [
    "GatewaySearchScopeRequest", "GatewaySearchScopeResolutionDTO", "GatewaySearchScopeDTO", "GatewaySearchScopeBranchDTO",
])
def test_scope_members_are_checked_against_candidate_descriptor(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="missing_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        assert_gateway_http_schemas(gateway_openapi, pool)
    assert model in str(caught.value) and "missing_public_field" in str(caught.value)
