"""目录读侧 HTTP 结构由 proto 推导，身份、状态和分页语义留在原 owner。"""

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic.json_schema import JsonSchemaValue

from app.protocol import openapi
from app.protocol.schema import descriptor_pool_from_set
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


@pytest.mark.parametrize("method,path,model", [
    ("get", "/api/v1/workspaces/{workspace_id}/session-catalog/children", "SessionCatalogPageDTO"),
    ("get", "/api/v1/workspaces/{workspace_id}/session-catalog/roots", "SessionCatalogPageDTO"),
    ("post", "/api/v1/workspaces/{workspace_id}/session-catalog/refresh", "SessionCatalogPageDTO"),
    ("get", "/api/v1/workspaces/{workspace_id}/session-catalog/breadcrumb/{node_id}", "SessionCatalogBreadcrumbDTO"),
    ("get", "/api/v1/workspaces/{workspace_id}/session-catalog/search", "SessionCatalogSearchResultsDTO"),
    ("get", "/api/v1/workspaces/{workspace_id}/session-catalog/export", "SessionCatalogExportDTO"),
])
def test_catalog_read_contract_follows_actual_routes(http_openapi: JsonSchemaValue, method: str, path: str, model: str) -> None:
    original = deepcopy(http_openapi)
    schema = http_openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = http_openapi["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + model)
    assert model in openapi.HTTP_JSON_DESCRIPTORS
    openapi.assert_public_http_schemas(http_openapi)
    assert http_openapi == original


@pytest.mark.parametrize("name", [
    "SessionCatalogPageDTO", "SessionCatalogBreadcrumbDTO", "SessionCatalogSearchResultsDTO", "SessionCatalogExportDTO",
])
def test_catalog_read_requires_response_roots(http_openapi: JsonSchemaValue, name: str) -> None:
    del http_openapi["components"]["schemas"][name]
    with pytest.raises(ValueError, match=f"公开 HTTP 结构未被导出: {name}"):
        openapi.assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize("model,field,shape", [
    ("SessionCatalogNodeDTO", "node_id", {"type": "integer"}),
    ("SessionCatalogPageDTO", "catalog_revision", {"type": "string"}),
    ("SessionCatalogSearchResultDTO", "breadcrumb", {"type": "object"}),
    ("SessionCatalogSearchResultsDTO", "cursor", {"type": "integer"}),
    ("SessionCatalogExportDTO", "items", {"type": "object"}),
])
def test_catalog_read_rejects_nested_structure_drift(http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致"):
        openapi.assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize("model", [
    "SessionCatalogNodeDTO", "SessionCatalogPageDTO", "SessionCatalogBreadcrumbDTO",
    "SessionCatalogSearchResultDTO", "SessionCatalogSearchResultsDTO", "SessionCatalogExportDTO",
])
def test_catalog_read_checks_candidate_descriptors(http_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_catalog_contract_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi.assert_public_http_schemas(http_openapi, pool)
    assert model in str(caught.value) and "new_catalog_contract_field" in str(caught.value)


def test_catalog_read_rejects_publish_before_touching_snapshots(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, openapi_output: Path,
    openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == "SessionCatalogNodeDTO")
    message.field.add(name="new_catalog_contract_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor = tmp_path / "candidate.binpb"
    descriptor.write_bytes(http_descriptor_bundle.SerializeToString())
    before = {name: (openapi_output / name).read_bytes() for name in OPENAPI_SNAPSHOTS}
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致"):
        openapi_export(openapi_output, descriptor)
    assert {name: (openapi_output / name).read_bytes() for name in OPENAPI_SNAPSHOTS} == before
