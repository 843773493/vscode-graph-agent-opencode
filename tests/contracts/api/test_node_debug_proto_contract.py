"""由 descriptor 核对 Node Debug 公开结构，保留 REST 动作的业务外形。"""

from __future__ import annotations

from typing import cast

import pytest
from google.protobuf.descriptor import Descriptor

from app.protocol.generated.boxteam.workspace.v2 import public_pb2
from app.protocol.openapi import assert_public_http_schemas
from app.protocol.schema import assert_public_schema
from app.schemas.internal_v2 import node_debug

NODE_DEBUG_DESCRIPTORS = tuple(
    descriptor
    for name, descriptor in public_pb2.DESCRIPTOR.message_types_by_name.items()
    if (name.startswith("NodeDebug") and name != "NodeDebugActionRequest")
    or name == "ExtensionCatalogBindingAuditDTO"
)


@pytest.mark.parametrize("descriptor", NODE_DEBUG_DESCRIPTORS, ids=lambda value: value.name)
def test_node_debug_public_schema_matches_proto(descriptor: Descriptor) -> None:
    model = getattr(node_debug, descriptor.name)
    assert_public_schema(model.model_json_schema(mode="serialization"), descriptor)


def test_node_debug_action_request_matches_typed_proto_oneof() -> None:
    from app.main import app

    # 直接验证实际HTTP入口；映射只留在导出/生成共享的同一检查链。
    assert_public_http_schemas(app.openapi())


@pytest.mark.parametrize(("mutation", "path"), [
    ("scalar", "/thread_id"),
    ("container", "/breakpoints"),
    ("nested", "/call_stack/*/variables/*/value"),
])
def test_node_debug_contract_rejects_type_drift_with_unchanged_field_names(
    mutation: str, path: str,
) -> None:
    schema = node_debug.NodeDebugStateDTO.model_json_schema(mode="serialization")
    if mutation == "scalar":
        schema["properties"]["thread_id"] = {"type": "integer"}
    elif mutation == "container":
        schema["properties"]["breakpoints"] = {"type": "object"}
    else:
        schema["$defs"]["NodeDebugVariableDTO"]["properties"]["value"] = {"type": "integer"}
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_schema(schema, cast(Descriptor, public_pb2.NodeDebugStateDTO.DESCRIPTOR))
    assert path in str(error.value)
