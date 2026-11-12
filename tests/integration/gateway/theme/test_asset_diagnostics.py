"""真实 HTTP 和 OS 故障验证图片资源的诊断边界与同入口重试。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from tests.harness.python.run_context import TestRunContext
from tests.support.diagnostic_sink import install_failing_diagnostic_sink
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)


def create_asset_diagnostic_failure_app() -> FastAPI:
    from app.gateway.main import app

    install_failing_diagnostic_sink(
        logger_names=("app.gateway.theme.assets", "app.core.failures"),
        control=Path(os.environ["BOXTEAM_TEST_ASSET_SINK_CONTROL"]),
        destination=Path(os.environ["BOXTEAM_TEST_ASSET_SINK_DESTINATION"]),
        audit=Path(os.environ["BOXTEAM_TEST_ASSET_SINK_AUDIT"]),
    )
    return app


@pytest.fixture
def asset_images() -> tuple[bytes, bytes]:
    images = []
    for color in ("red", "blue"):
        image = BytesIO()
        Image.new("RGB", (2, 2), color).save(image, format="PNG")
        images.append(image.getvalue())
    return images[0], images[1]


@pytest.fixture
def asset_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str) -> Iterator[tuple[TestRunContext, GatewayProcess, Path, Path, Path]]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    runtime = context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    control, audit, blocked = runtime / "sink-enabled", runtime / "sink-audit.jsonl", runtime / "blocked-log-directory"
    blocked.write_text("本次测试的日志阻塞文件", encoding="utf-8")
    ports = integration_port_block_for_file(Path(request.node.fspath))
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=ports.port(0), artifacts_dir=context.artifacts_dir,
            asgi_app=f"{__name__}:create_asset_diagnostic_failure_app",
            extra_env={"BOXTEAM_TEST_ASSET_SINK_CONTROL": str(control),
                       "BOXTEAM_TEST_ASSET_SINK_DESTINATION": str(blocked / "service.log"),
                       "BOXTEAM_TEST_ASSET_SINK_AUDIT": str(audit)},
        )
        processes.callback(close_gateway_process, gateway)
        yield context, gateway, control, audit, blocked


async def test_asset_and_diagnostic_failures_keep_http_state_and_explicit_retry(asset_gateway, asset_images) -> None:
    context, gateway, control, audit, blocked = asset_gateway
    endpoint = "/api/gateway/ui-assets"
    root = context.boxteam_home / "state/gateway/themes/assets/ui-assets"
    manifest = root / "manifest.json"
    evidence = []
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=15) as client:
        uploaded = await client.post(endpoint, files={"file": ("first.png", asset_images[0], "image/png")})
        assert uploaded.status_code == 200, uploaded.text
        first = uploaded.json()["data"]
        original_manifest = manifest.read_bytes()
        second_dir = root / hashlib.sha256(asset_images[1]).hexdigest()
        blocked_image = second_dir / "original.png"
        for sink_failure in (False, True):
            for operation in ("manifest-read", "manifest-decode", "manifest-validate", "image-write"):
                manifest.write_bytes(original_manifest)
                audit.unlink(missing_ok=True)
                if operation == "manifest-read":
                    manifest.unlink()
                    manifest.mkdir()
                elif operation == "manifest-decode":
                    manifest.write_bytes(b"\xff/private/image-secret")
                elif operation == "manifest-validate":
                    manifest.write_text('{"/private/image-secret": {}}', encoding="utf-8")
                else:
                    blocked_image.mkdir(parents=True)
                if sink_failure:
                    control.touch()
                try:
                    rejected_bytes = manifest.read_bytes() if manifest.is_file() else None
                    response = (await client.post(endpoint, files={"file": ("second.png", asset_images[1], "image/png")})
                                if operation == "image-write" else await client.get(endpoint))
                    assert response.status_code == 409, response.text
                    assert response.json()["request_id"] == response.headers["X-Request-ID"]
                    for native in (str(root), str(blocked), "image-secret"):
                        assert native not in response.text
                    expected_error = {"manifest-decode": "UnicodeDecodeError", "manifest-validate": "ValueError"}.get(operation, "IsADirectoryError")
                    assert f"error={expected_error}" in response.text
                    assert ("diagnostic_sink_error=NotADirectoryError" in response.text) == sink_failure
                    assert (manifest.read_bytes() if manifest.is_file() else None) == rejected_bytes
                    if sink_failure:
                        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
                        assert len(records) == 1 and records[0]["error_type"] == expected_error
                        assert str(blocked_image if operation == "image-write" else manifest) in records[0]["diagnostic"]
                    evidence.append({"operation": operation, "sink_failure": sink_failure, "rejected": response.json()})
                finally:
                    control.unlink(missing_ok=True)
                    if operation == "manifest-read":
                        manifest.rmdir()
                    if operation == "image-write":
                        blocked_image.rmdir()
                    manifest.write_bytes(original_manifest)
                # 同一 Gateway、同一资源根显式重试；不通过重启或修复writer制造成功事实。
                if operation == "image-write":
                    retry = await client.post(endpoint, files={"file": ("second.png", asset_images[1], "image/png")})
                    assert retry.status_code == 200, retry.text
                    second = retry.json()["data"]
                    downloaded = await client.get(second["url"])
                    assert downloaded.status_code == 200 and downloaded.content == asset_images[1]
                    deleted = await client.delete(second["url"])
                    assert deleted.status_code == 200, deleted.text
                else:
                    retry = await client.get(endpoint)
                    assert retry.status_code == 200 and retry.json()["data"]["items"] == [first]
                downloaded = await client.get(first["url"])
                assert downloaded.status_code == 200 and downloaded.content == asset_images[0]
        assert manifest.read_bytes() == original_manifest
        (context.artifacts_dir / "asset-failure-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
