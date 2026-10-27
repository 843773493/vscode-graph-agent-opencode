"""真实 HTTP OAuth 资产；MCP handler 复用 schema_server，不再实现另一套。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import pytest
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.auth import OAuthClientMetadata
from sse_starlette.sse import AppStatus
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.mcp.schema_server import build_server


@dataclass
class OAuthEndpoint:
    base_url: str
    context: TestRunContext
    definitions: Path
    receipts: Path
    metadata_enabled: bool = True
    issuer_matches: bool = True
    pkce_supported: bool = True
    fail_token: bool = False
    deny_next_authorization: bool = False
    expires_in: int = 3600
    registrations: int = 0
    exchanges: int = 0
    refreshes: int = 0
    denials: int = 0
    metadata: OAuthClientMetadata | None = None
    codes: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    authorizations: list[dict[str, str]] = field(default_factory=list, repr=False)
    access_token: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    refresh_token: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    private_marker: str = "private-oauth-response-must-not-leak"

    @property
    def url(self) -> str:
        return self.base_url + "/mcp/"

    def configuration(self, *, redirect_uri: str = "http://127.0.0.1/callback") -> dict[str, object]:
        return {"enabled": True, "transport": "streamable_http", "url": self.url,
                "oauth": {"redirect_uri": redirect_uri}}

    async def resource(self, _request: Request) -> Response:
        if not self.metadata_enabled:
            return Response(status_code=404)
        return JSONResponse({"resource": self.url.rstrip("/"), "authorization_servers": [self.base_url]})

    async def authorization_metadata(self, _request: Request) -> Response:
        if not self.metadata_enabled:
            return Response(status_code=404)
        return JSONResponse({
            "issuer": self.base_url if self.issuer_matches else self.base_url + "/wrong-issuer",
            "authorization_endpoint": self.base_url + "/authorize", "token_endpoint": self.base_url + "/oauth/token",
            "registration_endpoint": self.base_url + "/oauth/register",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"] if self.pkce_supported else ["plain"],
        })

    async def register(self, request: Request) -> Response:
        self.registrations += 1
        self.metadata = OAuthClientMetadata.model_validate(await request.json())
        return JSONResponse({**self.metadata.model_dump(mode="json"), "client_id": "boxteam-test-client"}, status_code=201)

    async def authorize(self, request: Request) -> Response:
        values = dict(request.query_params)
        if (values.get("client_id") != "boxteam-test-client" or values.get("code_challenge_method") != "S256"
                or self.metadata is None
                or values.get("redirect_uri") not in [str(uri) for uri in self.metadata.redirect_uris or []]):
            return Response(status_code=400)
        if self.deny_next_authorization:
            self.deny_next_authorization = False
            self.denials += 1
            return RedirectResponse(values["redirect_uri"] + "?" + urlencode({
                "error": "access_denied", "state": values["state"],
            }), status_code=303)
        code = secrets.token_urlsafe(24)
        self.codes[code] = values
        self.authorizations.append({**values, "code": code})
        return RedirectResponse(values["redirect_uri"] + "?" + urlencode({"code": code, "state": values["state"]}), status_code=303)

    async def token(self, request: Request) -> Response:
        values = {key: items[0] for key, items in parse_qs((await request.body()).decode()).items()}
        if self.fail_token:
            return JSONResponse({"error": self.private_marker, "access_token": self.access_token}, status_code=400)
        if values.get("grant_type") == "refresh_token":
            if values.get("refresh_token") != self.refresh_token:
                return Response(status_code=400)
            self.refreshes += 1
        else:
            authorization = self.codes.pop(values.get("code", ""), None)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(values.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
            if (authorization is None or challenge != authorization["code_challenge"]
                    or values.get("redirect_uri") != authorization["redirect_uri"]):
                return Response(status_code=400)
            self.exchanges += 1
        self.access_token = secrets.token_urlsafe(24)
        return JSONResponse({"access_token": self.access_token, "token_type": "Bearer", "refresh_token": self.refresh_token,
                             "expires_in": self.expires_in})


@pytest.fixture
def oauth_endpoint_factory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(AppStatus, "should_exit", False)

    @asynccontextmanager
    async def start(label: str = "oauth") -> AsyncIterator[OAuthEndpoint]:
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        definitions = context.artifacts_dir / f"{label}-definitions.json"
        definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
        receipts = context.artifacts_dir / f"{label}-receipts.jsonl"
        manager = StreamableHTTPSessionManager(app=build_server(definitions, receipts))

        @asynccontextmanager
        async def lifespan(_app):
            async with manager.run():
                yield

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            endpoint = OAuthEndpoint(f"http://127.0.0.1:{listener.getsockname()[1]}", context, definitions, receipts)

            async def protected(scope, receive, send):
                if scope["type"] == "http" and Headers(scope=scope).get("authorization") != f"Bearer {endpoint.access_token}":
                    await Response(status_code=401, headers={
                        "WWW-Authenticate": f'Bearer resource_metadata="{endpoint.base_url}/.well-known/oauth-protected-resource"',
                    })(scope, receive, send)
                    return
                await manager.handle_request(scope, receive, send)

            app = Starlette(routes=[
                Route("/.well-known/oauth-protected-resource", endpoint.resource),
                Route("/.well-known/oauth-authorization-server", endpoint.authorization_metadata),
                Route("/oauth/register", endpoint.register, methods=["POST"]),
                Route("/authorize", endpoint.authorize), Route("/oauth/token", endpoint.token, methods=["POST"]),
                Mount("/mcp", app=protected),
            ], lifespan=lifespan)
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError("OAuth HTTP 资产提前退出")
                        await asyncio.sleep(0.01)
                yield endpoint
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)

    return start
