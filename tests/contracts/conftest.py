import json
import sys
import traceback
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.descriptor_pb2 import FileDescriptorSet
from pydantic.json_schema import JsonSchemaValue

from app.protocol.generated.boxteam.gateway.v2 import plugin_commands_pb2
from app.protocol.generated.boxteam.gateway.v2 import public_pb2 as gateway_public_pb2
from app.protocol.generated.boxteam.search.v1 import public_pb2 as search_public_pb2
from app.protocol.generated.boxteam.workspace.message.v1 import message_stream_pb2
from app.protocol.generated.boxteam.workspace.v2 import (
    public_pb2 as workspace_public_pb2,
)
from app.protocol.generated.boxteam.workspace.v2 import (
    skill_sources_pb2,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


@pytest.fixture
def http_openapi() -> JsonSchemaValue:
    from app.main import app

    return deepcopy(app.openapi())


@pytest.fixture
def gateway_openapi() -> JsonSchemaValue:
    from app.gateway.main import app

    return deepcopy(app.openapi())


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

    for descriptor in (workspace_public_pb2.DESCRIPTOR, gateway_public_pb2.DESCRIPTOR, plugin_commands_pb2.DESCRIPTOR, skill_sources_pb2.DESCRIPTOR, message_stream_pb2.DESCRIPTOR, search_public_pb2.DESCRIPTOR):
        collect(descriptor)
    return bundle


@pytest.fixture
def openapi_output(tmp_path: Path) -> Path:
    """每个候选结构独占快照原件，拒绝发布时必须逐字节保留。"""
    output = tmp_path / "generated"
    for name in OPENAPI_SNAPSHOTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original snapshot\n")
    return output


@pytest.fixture
def openapi_export(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[Path, Path], None]:
    """批量候选复用真实导出入口；冷启动与进程退出另由CLI合同验证。"""
    from scripts.export_openapi_snapshot import main

    calls = 0

    def export(output: Path, descriptor: Path) -> None:
        nonlocal calls
        calls += 1
        argv = ["scripts.export_openapi_snapshot", "--output-root", str(output), "--descriptor-set", str(descriptor)]
        with monkeypatch.context() as patch:
            patch.setattr(sys, "argv", argv)
            try:
                main()
            finally:
                error = sys.exc_info()[1]
                prefix = tmp_path / f"export-inprocess-{calls}"
                prefix.with_suffix(".json").write_text(json.dumps({
                    "mode": "in-process", "argv": argv,
                    "error_type": type(error).__name__ if error is not None else None,
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                prefix.with_suffix(".log").write_text(
                    "".join(traceback.format_exception(error)) if error is not None else "",
                    encoding="utf-8",
                )

    return export


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    del config
    for item in items:
        item.add_marker(pytest.mark.contract)
