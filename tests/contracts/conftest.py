from collections.abc import Sequence

import pytest
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.descriptor_pb2 import FileDescriptorSet

from app.protocol.generated.boxteam.gateway.v1 import public_pb2 as gateway_public_pb2
from app.protocol.generated.boxteam.workspace.v2 import (
    public_pb2 as workspace_public_pb2,
)


@pytest.fixture
def http_descriptor_bundle() -> FileDescriptorSet:
    """HTTP 导出同时核对 Workspace 与 Gateway，依赖只登记一次。"""
    bundle = FileDescriptorSet()
    seen: set[str] = set()

    def collect(file: FileDescriptor) -> None:
        if file.name in seen:
            return
        seen.add(file.name)
        for dependency in file.dependencies:
            collect(dependency)
        file.CopyToProto(bundle.file.add())

    for descriptor in (workspace_public_pb2.DESCRIPTOR, gateway_public_pb2.DESCRIPTOR):
        collect(descriptor)
    return bundle


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    del config
    for item in items:
        item.add_marker(pytest.mark.contract)
