"""私有引用持久化、身份拒绝与生产 Gateway HTTP session 边界。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import SecretStr, ValidationError

from app.api.runtime_control import verify_local_control_request
from app.core.failures import failure_detail
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.server import mcp_upstream_proxy
from app.schemas.internal_v2.mcp import McpPrivateHeaderRefDTO
from app.services.infrastructure.config.source_document import (
    parse_source_text,
    redact_source_payload,
    redact_source_text,
    restore_source_text,
)
from app.services.infrastructure.mcp import session_runtime
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from app.services.infrastructure.mcp.private_inputs import McpPrivateInputStore

ENDPOINT = "https://service.test/mcp?region=1"
HEADER = "Authorization"
SECRET = "Bearer private-unit-only"


@pytest.fixture
def private_header(empty_workspace_scope: tuple[str, Path]):
    _, root = empty_workspace_scope
    store = McpPrivateInputStore(workspace_root=root)
    reference = store.write(server_id="secure", header_name=HEADER, endpoint=ENDPOINT, value=SecretStr(SECRET))
    return root, store, reference


def _config(reference: McpPrivateHeaderRefDTO, **changes: object) -> dict[str, object]:
    return {"servers": {"secure": {
        "transport": "streamable_http", "url": ENDPOINT,
        "headers": {HEADER: reference.model_dump(mode="json"), "X-Plain": "unchanged"}, **changes,
    }}}


def test_private_header_recovers_exact_version_and_rotates_without_overwriting(private_header):
    root, store, reference = private_header
    second = store.write(server_id="secure", header_name=HEADER, endpoint=ENDPOINT, value=SecretStr("Bearer rotated"))
    assert second.version != reference.version
    restarted = McpPrivateInputStore(workspace_root=root)
    assert restarted.resolve(reference, server_id="secure", header_name=HEADER, endpoint=ENDPOINT) == SECRET
    assert restarted.resolve(second, server_id="secure", header_name=HEADER, endpoint=ENDPOINT) == "Bearer rotated"
    files = list((root / ".boxteam/credentials/mcp-headers").glob("*.json"))
    assert len(files) == 2
    assert SECRET not in reference.model_dump_json()
    if os.name != "nt":
        assert all(path.stat().st_mode & 0o077 == 0 for path in files)


@pytest.mark.parametrize(("server_id", "header_name", "endpoint"), [
    ("another", HEADER, ENDPOINT), ("secure", "authorization", ENDPOINT),
    ("secure", HEADER, "https://service.test/mcp?region=2"),
])
def test_reference_rejects_changed_server_header_or_exact_endpoint(private_header, server_id, header_name, endpoint):
    _, store, reference = private_header
    with pytest.raises(ValueError, match="不属于"):
        store.resolve(reference, server_id=server_id, header_name=header_name, endpoint=endpoint)


def test_reference_cannot_cross_workspace_or_borrow_unknown_version(private_header):
    root, store, reference = private_header
    other = root / "workspace-b"
    load_or_create_workspace_id(other)
    with pytest.raises(ValueError, match="不属于"):
        McpPrivateInputStore(workspace_root=other).resolve(
            reference, server_id="secure", header_name=HEADER, endpoint=ENDPOINT,
        )
    unknown = reference.model_copy(update={"version": "0" * 32})
    with pytest.raises(ValueError, match="版本不存在"):
        store.resolve(unknown, server_id="secure", header_name=HEADER, endpoint=ENDPOINT)


@pytest.mark.parametrize("endpoint", [
    "https://user:private-url-password@service.test/mcp", "https://user@service.test/mcp",
    "https://service.test/mcp#private-fragment", "https://service.test/mcp#",
])
def test_handwritten_reference_rejects_public_endpoint_credentials_and_fragments(private_header, endpoint):
    root, _, reference = private_header
    raw_reference = {**reference.model_dump(mode="json"), "endpoint": endpoint}
    raw = {"servers": {"secure": {"transport": "streamable_http", "url": ENDPOINT,
                                  "headers": {HEADER: raw_reference}}}}
    with pytest.raises(ValidationError, match="不得包含凭据或 fragment") as caught:
        parse_mcp_server_configs(raw, workspace_root=root)
    assert "private-url-password" not in str(caught.value)
    redacted = redact_source_payload({"mcp": raw})
    assert isinstance(redacted, dict)
    mcp = redacted["mcp"]
    assert isinstance(mcp, dict)
    servers = mcp["servers"]
    assert isinstance(servers, dict)
    secure = servers["secure"]
    assert isinstance(secure, dict)
    assert "private-url-password" not in json.dumps(secure["headers"])
    # 新引用的公开身份约束不改变旧明文 HTTP 配置合同。
    original, = parse_mcp_server_configs({"servers": {"secure": {
        "transport": "streamable_http", "url": ENDPOINT, "headers": {HEADER: "existing"},
    }}}, workspace_root=root)
    assert original.connection is not None
    assert original.connection["transport"] == "streamable_http"
    assert original.connection["url"] == ENDPOINT


@pytest.mark.parametrize("invalid_value", [SECRET + "\r\ninvalid", " " + SECRET, SECRET + " "])
def test_tampered_private_identity_and_value_fail_without_disclosing_input(private_header, caplog, invalid_value):
    root, store, reference = private_header
    path, = (root / ".boxteam/credentials/mcp-headers").glob("*.json")
    document = json.loads(path.read_text())
    document["reference"]["endpoint"] = "https://other.test/mcp"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="身份不一致"):
        store.resolve(reference, server_id="secure", header_name=HEADER, endpoint=ENDPOINT)
    document["reference"] = reference.model_dump(mode="json")
    document["value"] = invalid_value
    path.write_text(json.dumps(document))
    with pytest.raises(ValidationError) as caught:
        store.resolve(reference, server_id="secure", header_name=HEADER, endpoint=ENDPOINT)
    assert SECRET not in str(caught.value)
    detail = failure_detail(caught.value, stage="private_header", label="MCP")
    assert SECRET not in detail and SECRET not in caplog.text
    assert "ValidationError" in detail


def test_config_keeps_reference_identity_and_never_materializes_private_value(private_header, monkeypatch):
    root, store, reference = private_header
    monkeypatch.setenv("MCP_PRIVATE_PLAIN", "expanded-old-contract")
    server, = parse_mcp_server_configs(_config(reference, headers={
        HEADER: reference.model_dump(mode="json"), "X-Env": "${MCP_PRIVATE_PLAIN}",
    }), workspace_root=root)
    assert server.private_headers == (reference,)
    assert server.connection is not None
    assert server.connection["transport"] == "streamable_http"
    assert server.connection.get("headers") == {"X-Env": "expanded-old-contract"}
    assert SECRET not in repr(asdict(server))
    rotated = store.write(server_id="secure", header_name=HEADER, endpoint=ENDPOINT, value=SecretStr(SECRET))
    updated, = parse_mcp_server_configs(_config(rotated), workspace_root=root)
    original, = parse_mcp_server_configs(_config(reference), workspace_root=root)
    assert original.connection == updated.connection and original != updated
    with pytest.raises(ValueError, match="不属于"):
        parse_mcp_server_configs(_config(reference, url=ENDPOINT + "&changed=1"), workspace_root=root)
    with pytest.raises(ValueError, match="同时配置"):
        parse_mcp_server_configs(_config(reference, oauth={"redirect_uri": "https://owner.test/callback"}), workspace_root=root)


async def test_production_session_sends_private_header_through_gateway_without_leaking_local_token(
    private_header, monkeypatch,
):
    root, _, reference = private_header
    local_token = "gateway-local-only"
    monkeypatch.setattr(session_runtime, "get_gateway_local_token", lambda: local_token)
    server, = parse_mcp_server_configs(_config(reference), workspace_root=root)
    config_service = Mock()
    config_service.get_gateway_connection_url.return_value = "http://gateway.test"

    upstream_requests: list[httpx.Request] = []

    async def fake_upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "header-probe", "version": "1"},
                },
            })
        assert message["method"] == "notifications/initialized"
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_upstream)) as upstream_client:
        gateway_app = FastAPI()
        gateway_app.add_middleware(TraceMiddleware)
        gateway_app.include_router(mcp_upstream_proxy.router)
        gateway_app.state.http_client = upstream_client

        async def authenticate_gateway_request(request: Request) -> str:
            assert request.headers["x-local-token"] == local_token
            return local_token

        gateway_app.dependency_overrides[mcp_upstream_proxy.verify_gateway_token] = authenticate_gateway_request
        gateway_app.dependency_overrides[verify_local_control_request] = lambda: None
        real_gateway_transport = session_runtime._GatewayMcpTransport

        def gateway_transport(*, gateway_url, endpoint_url, endpoint_headers, transport=None):
            return real_gateway_transport(
                gateway_url=gateway_url,
                endpoint_url=endpoint_url,
                endpoint_headers=endpoint_headers,
                transport=httpx.ASGITransport(app=gateway_app),
            )

        monkeypatch.setattr(
            session_runtime, "_GatewayMcpTransport", gateway_transport,
        )

        async with session_runtime.production_mcp_session_factory(
            server, lambda: None, workspace_root=root, config_service=config_service,
        ):
            pass

    assert [json.loads(request.content)["method"] for request in upstream_requests] == [
        "initialize", "notifications/initialized",
    ]
    assert all(str(request.url) == ENDPOINT for request in upstream_requests)
    assert all(request.headers["authorization"] == SECRET for request in upstream_requests)
    assert all("x-local-token" not in request.headers for request in upstream_requests)
    assert all("x-boxteam-mcp-endpoint" not in request.headers for request in upstream_requests)
    assert all(request.headers.get("x-request-id") for request in upstream_requests)


def test_source_jsonc_round_trip_exposes_reference_and_masks_existing_literals(private_header):
    _, _, reference = private_header
    payload = {"mcp": _config(reference)}
    text = "// 用户注释\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    redacted = redact_source_text(text)
    parsed = parse_source_text(redacted)
    assert SECRET not in redacted and "unchanged" not in redacted
    assert parsed == redact_source_payload(payload)
    assert isinstance(parsed, dict)
    mcp = parsed["mcp"]
    assert isinstance(mcp, dict)
    servers = mcp["servers"]
    assert isinstance(servers, dict)
    secure = servers["secure"]
    assert isinstance(secure, dict)
    headers = secure["headers"]
    assert isinstance(headers, dict)
    assert headers[HEADER] == reference.model_dump(mode="json")
    assert restore_source_text(redacted, text) == text
    moved = redacted.replace(ENDPOINT, "https://other.test/mcp")
    with pytest.raises(ValueError, match="地址已改变"):
        restore_source_text(moved, text)
    invalid = {"mcp": {"servers": {"secure": {"headers": {"X-Bad": {"kind": "mcp_private_header", "value": SECRET}}}}}}
    assert SECRET not in json.dumps(redact_source_payload(invalid))
