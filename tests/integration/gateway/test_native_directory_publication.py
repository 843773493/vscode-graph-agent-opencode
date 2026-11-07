"""真实 Gateway 网络上的原生根回执发布、拒绝与原请求重试。"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from app.core.resource_uri import parse_resource_uri
from app.core.workspace_identity import read_workspace_id
from app.gateway.lifespan import lifespan
from app.gateway.routes import native_directories
from app.gateway.runtime.extension_management import GatewayExtensionManagement
from app.schemas.gateway.native_directories import (
    NativeDirectoryBindingDTO,
    NativeDirectoryResolveDTO,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.workspace_files.paths import (
    GatewayNativeDirectoryClient,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)

_Reply = TypeVar("_Reply", NativeDirectoryBindingDTO, NativeDirectoryResolveDTO)


def _copy_reply(operation: str, reply: _Reply) -> _Reply:
    fault = json.loads(Path(os.environ["BOXTEAM_TEST_NATIVE_PUBLICATION"]).read_text(encoding="utf-8"))
    # 仅污染真实路由构造后的回执；认证、SQLite 绑定和响应序列化仍走产品链路。
    return reply.model_copy(update={"uri": fault["uri"]}) if fault.get("operation") == operation else reply


def _binding_reply(**values: str) -> NativeDirectoryBindingDTO:
    return _copy_reply("bind", NativeDirectoryBindingDTO(**values))


def _resolve_reply(**values: str) -> NativeDirectoryResolveDTO:
    return _copy_reply("resolve", NativeDirectoryResolveDTO(**values))


@asynccontextmanager
async def publication_lifespan(application: FastAPI) -> AsyncIterator[dict[str, GatewayExtensionManagement | None]]:
    with patch.object(native_directories, "NativeDirectoryBindingDTO", _binding_reply), patch.object(
        native_directories, "NativeDirectoryResolveDTO", _resolve_reply,
    ):
        async with lifespan(application) as state:
            yield state


def create_gateway_app() -> FastAPI:
    from app.gateway.main import app

    app.router.lifespan_context = publication_lifespan
    return app


@pytest.fixture
def native_gateway(
    request: pytest.FixtureRequest, integration_backend_process: subprocess.Popen[str],
    integration_backend_port: int, integration_workspace_root_path: str,
) -> Iterator[tuple[TestRunContext, Path, httpx.Client]]:
    assert integration_backend_process.poll() is None
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    assert context.workspace_root == Path(integration_workspace_root_path)
    control = context.runtime_root_for_node(request.node.nodeid) / "publication.json"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text("{}", encoding="utf-8")
    port = integration_port_block_for_file(Path(__file__)).port(1)
    gateway = start_gateway_process(
        workspace_root=context.workspace_root, default_backend_url=f"http://127.0.0.1:{integration_backend_port}",
        port=port, artifacts_dir=context.artifacts_dir, asgi_app=f"{__name__}:create_gateway_app",
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        extra_env={"BOXTEAM_TEST_NATIVE_PUBLICATION": str(control)},
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS, timeout=20) as client:
            yield context, control, client
    finally:
        close_gateway_process(gateway)


def test_native_directory_rejects_copied_path_and_retries_same_binding(
    native_gateway: tuple[TestRunContext, Path, httpx.Client],
) -> None:
    context, control, client = native_gateway
    registry = client.get("/api/gateway/workspaces", params={"check_health": "false"})
    assert registry.status_code == 200, registry.text
    listing = registry.json()["data"]
    targets = listing["items"]
    active = listing["active_workspace"]
    workspace_id = active["workspace_id"]
    assert workspace_id == read_workspace_id(context.workspace_root)
    assert any(
        {"gateway_id": target["gateway_id"], "workspace_id": target["workspace_id"]} == active
        for target in targets
    )
    native_root = context.runtime_root_for_node("native-root") / "unregistered"
    native_root.mkdir(parents=True, exist_ok=True)
    original = native_root / "original.txt"
    original.write_text("原生目录原件", encoding="utf-8")
    owner = GatewayNativeDirectoryClient(
        workspace_id=workspace_id, connection=GatewayTransportConnection(gateway_url=str(client.base_url).rstrip("/")),
        local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"],
    )
    root_uri = owner.bind(native_root)
    parsed = parse_resource_uri(root_uri)
    assert parsed.scope == "gateway" and parsed.kind == "files" and len(parsed.tail_segments) == 1
    assert parsed.scope_id == active["gateway_id"]
    assert owner.resolve(parsed) == native_root
    endpoint = f"/api/gateway/native-directories/workspaces/{workspace_id}"
    evidence: list[dict[str, object]] = []
    for operation in ("bind", "resolve"):
        for index, private in enumerate((str(native_root), r"C:\private-native-root\selected", r"\\host\private-native-root\selected")):
            control.write_text(json.dumps({"operation": operation, "uri": private}), encoding="utf-8")
            request_id = f"req_native_publication_{operation}_{index}"
            method = "POST" if operation == "bind" else "GET"
            path = endpoint if operation == "bind" else f"{endpoint}/{parsed.scope_id}/{parsed.tail_segments[0]}"
            body = {"native_root": str(native_root)} if operation == "bind" else None
            rejected = client.request(method, path, headers={"X-Request-ID": request_id}, json=body)
            evidence.append({"operation": operation, "rejected_status": rejected.status_code, "rejected_body": rejected.text})
            (context.artifacts_dir / "publication-receipts.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            assert rejected.status_code == 500, rejected.text
            assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"] == request_id
            assert "PydanticSerializationError" in rejected.json()["message"]
            assert private not in rejected.text and "private-native-root" not in rejected.text
            assert str(native_root) not in rejected.text
            control.write_text("{}", encoding="utf-8")
            accepted = client.request(method, path, headers={"X-Request-ID": request_id}, json=body)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["request_id"] == accepted.headers["X-Request-ID"] == request_id
            assert accepted.json()["data"]["uri"] == root_uri
            if operation == "resolve":
                assert accepted.json()["data"]["native_root"] == str(native_root)
            else:
                assert str(native_root) not in accepted.text
            assert owner.bind(native_root) == root_uri and owner.resolve(parsed) == native_root
    assert original.read_text(encoding="utf-8") == "原生目录原件"
    assert not (native_root / ".boxteam").exists()
    after = client.get("/api/gateway/workspaces", params={"check_health": "false"})
    assert after.status_code == 200, after.text
    after_targets = after.json()["data"]["items"]
    (context.artifacts_dir / "workspace-registrations.json").write_text(
        json.dumps({"before": targets, "after": after_targets}, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    # 健康探测可更新运行投影；目录绑定不得修改注册身份、路由或管理配置。
    registration_keys = ("gateway_id", "workspace_id", "backend_generation", "affected_workspace_ids",
                         "parent_workspace", "name", "backend_url", "connection_kind", "active", "managed",
                         "removable", "system_default")
    assert [{key: item[key] for key in registration_keys} for item in after_targets] == [
        {key: item[key] for key in registration_keys} for item in targets
    ]
    diagnostic = (context.artifacts_dir / "gateway.stderr.log").read_text(encoding="utf-8")
    assert "PydanticSerializationError" in diagnostic
