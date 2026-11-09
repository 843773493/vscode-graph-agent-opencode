"""协议集成测试运行正式插件桥；stdio 始终由桥持有。"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sse_starlette.sse import AppStatus
from starlette.types import ASGIApp

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.support.mcp.http_endpoint import HttpSchemaEndpoint, serve_http_app


@pytest.fixture
def bridge_endpoint_factory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def endpoint(*, args: list[str] | None = None, page_size: int | None = None,
                       notify_changes: bool = False) -> AsyncIterator[tuple[HttpSchemaEndpoint, str]]:
        monkeypatch.setattr(AppStatus, "should_exit", False)
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        definitions, receipts = (context.artifacts_dir / name for name in ("definitions.json", "receipts.jsonl"))
        definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
        server_args = args if args is not None else [
            str(context.project_root / "tests/integration/workspace_services/mcp/schema_server.py"),
            str(definitions), str(receipts),
        ]
        if page_size is not None:
            server_args += ["--page-size", str(page_size)]
        if notify_changes:
            server_args += ["--notify-changes"]
        token = uuid4().hex
        instance_id = uuid4().hex
        monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
        monkeypatch.setenv("BOXTEAM_PLUGIN_COMMANDS", "[]")
        monkeypatch.setenv("BOXTEAM_PLUGIN_INSTANCE", json.dumps({
            "gateway_id": "test-gateway", "workspace_id": load_or_create_workspace_id(context.workspace_root),
            "package_id": "test.mcp-bridge", "package_version": "1.0.0", "plugin_id": "probe",
            "instance_id": instance_id, "instance_scope": "workspace", "management_protocol_version": 1,
            "extension_contract_version": 1,
            "services": [{"service_id": "mcp", "service_type": "mcp", "protocol": "streamable-http"}],
        }))
        monkeypatch.setenv("BOXTEAM_RUNTIME_OBSERVATION", json.dumps({
            "instance_id": instance_id,
            "revision": "test-startup", "telemetry": {"enabled": False}, "statistics": {"enabled": False},
        }))
        monkeypatch.setenv("BOXTEAM_PLUGIN_PARAMETERS", json.dumps({
            "command": sys.executable,
            "args": server_args,
            "cwd": str(context.project_root),
        }))
        bridge_path = context.project_root / "resources/plugins/mcp-stdio/bridge.py"
        spec = importlib.util.spec_from_file_location(f"boxteam_mcp_bridge_{uuid4().hex}", bridge_path)
        if not isinstance(spec, ModuleSpec) or spec.loader is None:
            raise ImportError(f"无法加载 MCP stdio 插件桥: {bridge_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            del sys.modules[spec.name]
        create_app = cast(Callable[[], ASGIApp], module.create_app)
        async with serve_http_app(create_app()) as url:
            yield HttpSchemaEndpoint(url=f"{url}/services/mcp/", context=context, definitions=definitions, receipts=receipts), token

    return endpoint
