"""普通图片 API 保留业务拒绝与 request_id，不发布存储或日志位置。"""

from __future__ import annotations

import errno
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_content_access,
    verify_gateway_token,
)
from app.gateway.config import GatewayConfig
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.main import gateway_http_exception_handler
from app.gateway.routes import ui_assets


@pytest.fixture
def asset_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, bytes]]:
    root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    state = GatewayStateStore(path=root / "gateway.sqlite")
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.exception_handler(HTTPException)(gateway_http_exception_handler)
    application.include_router(ui_assets.router)
    application.state.gateway_config = GatewayConfig()
    application.dependency_overrides[verify_gateway_token] = lambda: "test-token"
    application.dependency_overrides[verify_gateway_content_access] = lambda: GatewayAuthContext(kind="local")
    try:
        with TestClient(application) as client:
            image = BytesIO()
            Image.new("RGB", (2, 2), "green").save(image, format="PNG")
            yield client, root, image.getvalue()
    finally:
        state.close()


@pytest.mark.parametrize("operation", ["manifest-read", "manifest-decode", "manifest-validate", "image-write"])
def test_asset_response_preserves_failure_and_explicit_retry(
    asset_client, asset_diagnostics, operation: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, root, image = asset_client
    endpoint = "/api/gateway/ui-assets"
    manifest = root / "ui-assets/manifest.json"
    raw_error = PermissionError(errno.EACCES, "原生图片操作被拒绝", str(root / "private-secret"))
    original: bytes | None = None
    asset = None
    if operation != "image-write":
        uploaded = client.post(endpoint, files={"file": ("background.png", image, "image/png")})
        assert uploaded.status_code == 200
        asset = uploaded.json()["data"]
        original = manifest.read_bytes()
    if operation == "manifest-decode":
        manifest.write_bytes(b"\xff/private/manifest-secret")
    elif operation == "manifest-validate":
        manifest.write_text('{"/private/manifest-secret": {}}', encoding="utf-8")
    failed_manifest = manifest.read_bytes() if manifest.exists() else None
    method = "write_bytes" if operation == "image-write" else "read_text"
    real_method = getattr(Path, method)

    def fail(path: Path, *args: object, **kwargs: object):
        if (operation == "image-write" and path.name == "original.png") or (operation == "manifest-read" and path == manifest):
            raise raw_error
        return real_method(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        headers = {"X-Request-ID": "asset-failure-request"}
        response = (client.post(endpoint, files={"file": ("background.png", image, "image/png")}, headers=headers)
                    if operation == "image-write" else client.get(endpoint, headers=headers))
    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "asset-failure-request"
    for forbidden in (str(root), "private-secret", "manifest-secret", "theme-diagnostic-secret"):
        assert forbidden not in response.text
    records, sink_error = asset_diagnostics
    assert len(records) == 1 and records[0].exc_info is not None
    cause = records[0].exc_info[1]
    assert type(cause) is ({"manifest-decode": UnicodeDecodeError, "manifest-validate": ValueError}.get(operation, PermissionError))
    if operation in {"manifest-read", "image-write"}:
        assert cause is raw_error
        assert f"operation={operation}" in response.json()["detail"]
    if sink_error is not None:
        assert "diagnostic_sink_error=PermissionError" in response.json()["detail"]
        assert str(sink_error) in "".join(cause.__notes__)
    assert (manifest.read_bytes() if manifest.exists() else None) == failed_manifest
    if original is not None:
        assert asset is not None
        manifest.write_bytes(original)
        retried = client.get(endpoint)
        assert retried.status_code == 200 and retried.json()["data"]["items"] == [asset]
        downloaded = client.get(asset["url"])
        assert downloaded.status_code == 200 and downloaded.content == image
    else:
        retried = client.post(endpoint, files={"file": ("background.png", image, "image/png")})
        assert retried.status_code == 200
        downloaded = client.get(retried.json()["data"]["url"])
        assert downloaded.status_code == 200 and downloaded.content == image
