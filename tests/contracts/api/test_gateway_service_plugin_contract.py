"""自有服务插件的跨语言结构由 Gateway descriptor 核对。"""

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic.json_schema import JsonSchemaValue

from app.protocol.generated.boxteam.gateway.v2 import (
    plugin_commands_pb2,
    service_plugin_pb2,
)
from app.protocol.openapi import assert_gateway_http_schemas
from app.protocol.schema import descriptor_pool_from_set


def test_plugin_command_ndjson_response_references_the_proto_event(
    gateway_openapi: JsonSchemaValue,
) -> None:
    operation = gateway_openapi["paths"]["/api/gateway/service-plugins/commands/call"]["post"]
    response = operation["responses"]["200"]
    assert set(response["content"]) == {"application/x-ndjson"}
    ndjson = response["content"]["application/x-ndjson"]
    event_schema = ndjson["x-ndjson-item"]

    assert ndjson["schema"] == {"type": "string"}
    assert event_schema["$ref"] == "#/components/schemas/PluginCommandEventDTO"
    event = gateway_openapi["components"]["schemas"]["PluginCommandEventDTO"]
    assert event["x-protobuf-message"] == plugin_commands_pb2.PluginCommandEventDTO.DESCRIPTOR.full_name


def test_plugin_manifest_structure_is_checked_against_candidate_descriptor(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet,
) -> None:
    descriptor_bundle = FileDescriptorSet()
    descriptor_bundle.CopyFrom(http_descriptor_bundle)
    file = next(file for file in descriptor_bundle.file if file.name == service_plugin_pb2.DESCRIPTOR.name)
    message = next(message for message in file.message_type if message.name == "PluginPackageManifest")
    message.field.add(name="undeclared_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(descriptor_bundle.SerializeToString())

    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        assert_gateway_http_schemas(gateway_openapi, pool)

    assert "PluginPackageManifest" in str(caught.value)
    assert "undeclared_public_field" in str(caught.value)
