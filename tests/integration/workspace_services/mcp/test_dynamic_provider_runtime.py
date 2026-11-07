"""Provider resolver 为具名边界，真实 SDK／进程证明发布屏障与故障诊断。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import traceback
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.gateway.mcp_definitions import (
    McpProviderHttpLaunchDTO,
    McpProviderStdioLaunchDTO,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogError, McpCatalogOwner
from app.services.infrastructure.mcp import catalog_owner as owner_module
from app.services.infrastructure.mcp.provider import (
    McpProviderAdmission,
    McpProviderUnavailable,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.http_server import running_http_server
from tests.unit.services.infrastructure.conftest import (
    event_channel_service as event_channel_service,  # noqa: PLC0414
)
from tests.unit.services.infrastructure.mcp.test_provider import (
    provider_reference as provider_reference,  # noqa: PLC0414
)


@pytest.fixture
def provider_workspace(request: pytest.FixtureRequest) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = context.workspace_root / request.node.name
    workspace.mkdir(parents=True, exist_ok=True)
    load_or_create_workspace_id(workspace)
    return workspace


@pytest.fixture
def provider_resolver(provider_workspace: Path):
    class Resolver:
        def __init__(self):
            self.invalidated = asyncio.Event()
            self.launch = McpProviderStdioLaunchDTO(transport="stdio", command=sys.executable,
                args=[str(Path.cwd() / "tests/integration/workspace_services/mcp/private_env_server.py")],
                env={"MCP_PRIVATE_TOKEN": "runtime-private-value", "MCP_ENV_RECEIPTS": str(provider_workspace / "receipts.jsonl")}, unset_env=[])
            self.admission = McpProviderAdmission(self.launch, self.invalidated, self.check)

        def check(self):
            if self.invalidated.is_set():
                raise McpProviderUnavailable("原 Provider 定义在首次目录发布前已失效")

        def check_mcp_definition(self, _reference, *, workspace_id, transport):
            self.check()

        async def resolve_mcp_definition(self, _reference, *, workspace_id, transport):
            self.check()
            return self.admission

    return Resolver()


@pytest.fixture
def invalid_http_provider() -> Iterator[tuple[str, list[str]]]:
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *_arguments: object) -> None:
            return

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            secret = self.headers["Authorization"]
            received.append(hashlib.sha256(secret.encode()).hexdigest())
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
                "protocolVersion": secret, "capabilities": secret, "serverInfo": secret}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Echo-Token", secret)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as server:
        yield f"http://127.0.0.1:{server.server_port}/mcp", received


async def test_change_after_real_sdk_list_tools_before_first_publication_cancels_candidate(
    provider_reference, provider_workspace, provider_resolver, event_channel_service, monkeypatch,
) -> None:
    original = owner_module._adapt_server_tools
    observed = []

    def invalidate_after_list(*args, **kwargs):
        assert args[1] and not provider_resolver.admission.committed.is_set()
        observed.append([tool.name for tool in args[1]])
        provider_resolver.invalidated.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(owner_module, "_adapt_server_tools", invalidate_after_list)
    ledger = ExternalResourceLeaseLedger()
    owner = McpCatalogOwner(raw_config={"servers": {"dynamic": {"enabled": True, "transport": "stdio",
        "provider_definition": provider_reference}}}, workspace_root=provider_workspace, event_service=event_channel_service,
        provider_resolver=provider_resolver, leases=ledger)
    await owner.start()
    try:
        assert observed == [["private_env_probe"]]
        assert not provider_resolver.admission.committed.is_set()
        assert owner.get_tool_ids() == set()
        server, = owner.list_servers()
        assert server.status == "unavailable" and server.connection_ref is None
        assert all(record.status == "stopped" for record in ledger.records_by_kind("mcp_connection"))
    finally:
        await owner.shutdown()


@pytest.mark.parametrize("fault", ["wire", "initialize", "executable"])
async def test_real_transport_failure_diagnostics_do_not_persist_resolved_secret(
    provider_reference, provider_workspace, provider_resolver, event_channel_service, caplog, fault,
) -> None:
    script = provider_workspace / "malformed.py"
    response = "{'invalid':secret}" if fault == "wire" else "{'jsonrpc':'2.0','id':request['id'],'result':{'capabilities':secret,'serverInfo':secret}}"
    script.write_text("import json,os,sys\nrequest=json.loads(sys.stdin.readline())\nsecret=os.environ['MCP_PRIVATE_TOKEN']\n"
        + f"print(json.dumps({response}),flush=True)\n", encoding="utf-8")
    launch = provider_resolver.launch.model_copy(update={"args": [str(script)], **(
        {"command": str(provider_workspace / "runtime-private-value")} if fault == "executable" else {})})
    provider_resolver.admission = McpProviderAdmission(launch, provider_resolver.invalidated, provider_resolver.check)
    owner = McpCatalogOwner(raw_config={"servers": {"dynamic": {"enabled": True, "transport": "stdio",
        "provider_definition": provider_reference}}}, workspace_root=provider_workspace, event_service=event_channel_service,
        provider_resolver=provider_resolver, leases=ExternalResourceLeaseLedger())
    caplog.set_level(logging.DEBUG)
    with pytest.raises(McpCatalogError) as failure:
        await owner.start()
    diagnostics = "".join(traceback.format_exception(failure.value)) + caplog.text
    assert ("FileNotFoundError" if fault == "executable" else "ValidationError") in diagnostics
    assert "runtime-private-value" not in diagnostics
    assert not owner.started
    with pytest.raises(RuntimeError, match="尚未启动"):
        owner.get_tool_ids()


async def test_real_http_sdk_validation_and_wire_logs_do_not_persist_provider_headers(
    provider_reference, provider_workspace, provider_resolver, event_channel_service, caplog, invalid_http_provider,
) -> None:
    endpoint, received = invalid_http_provider
    launch = McpProviderHttpLaunchDTO(transport="streamable_http", url=endpoint, headers={"Authorization": "http-private-token"})
    provider_resolver.admission = McpProviderAdmission(launch, provider_resolver.invalidated, provider_resolver.check)
    owner = McpCatalogOwner(raw_config={"servers": {"dynamic": {"enabled": True, "transport": "streamable_http",
        "provider_definition": provider_reference}}}, workspace_root=provider_workspace, event_service=event_channel_service,
        provider_resolver=provider_resolver, leases=ExternalResourceLeaseLedger())
    caplog.set_level(logging.DEBUG)
    with pytest.raises(McpCatalogError) as failure:
        await owner.start()
    diagnostics = "".join(traceback.format_exception(failure.value)) + caplog.text
    assert received == [hashlib.sha256(b"http-private-token").hexdigest()]
    assert "ValidationError" in diagnostics and "http-private-token" not in diagnostics
    assert any(record.name.startswith("httpcore.") for record in caplog.records)
    assert not owner.started
